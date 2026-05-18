#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Py Map Stitcher - standalone tile downloader/merger with Tkinter GUI.

Use only with map/tile servers for which you have permission. Many public map
providers prohibit bulk downloading. The app intentionally uses a conservative
rate limit and requires user-supplied/custom URL templates.
"""

import concurrent.futures as cf
import dataclasses
import io
import math
import os
import queue
import random
import sys
import sqlite3
import threading
import time
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

import tkinter as tk
from tkinter import ttk, filedialog, messagebox

try:
    import requests
except Exception as exc:  # pragma: no cover
    requests = None

try:
    from PIL import Image, ImageFile, ImageTk
    ImageFile.LOAD_TRUNCATED_IMAGES = True
except Exception:  # pragma: no cover
    Image = None

TILE_SIZE = 256
USER_AGENT = "PyMapStitcher/1.0 (+local user tool)"
MAX_INFLIGHT_PER_WORKER = 4  # prevents millions of Futures in RAM
HARD_TILE_WARNING = 5_000_000
DEFAULT_CHUNK_SIZE = 64
MAX_DIRECT_TIFF_BYTES = 1_000_000_000_000  # 1 TB safety limit for sparse BigTIFF output



MAP_PRESETS = {
    "Custom": {
        "url": "https://your-tile-server.example/{z}/{x}/{y}.png",
        "note": "Enter a custom URL template manually.",
        "preview": True,
    },
    "Google Satellite": {
        "url": "https://mt{rnd}.google.com/vt/lyrs=s&x={x}&y={y}&z={z}&hl=de",
        "note": "Google Satellite. Respect the terms of use; no bulk downloading without permission.",
        "preview": True,
    },
    "Google Hybrid": {
        "url": "https://mt{rnd}.google.com/vt/lyrs=y&x={x}&y={y}&z={z}&hl=de",
        "note": "Google Satellite with labels. Respect the terms of use.",
        "preview": True,
    },
    "Bing Satellite": {
        "url": "https://ecn.t{snum}.tiles.virtualearth.net/tiles/a{q}.jpeg?g=14574&mkt=de-DE&n=z",
        "note": "Bing Aerial/Satellite via QuadKey {q}. Respect the terms of use.",
        "preview": True,
    },
    "Bing Hybrid": {
        "url": "https://ecn.t{snum}.tiles.virtualearth.net/tiles/h{q}.jpeg?g=14574&mkt=de-DE&n=z",
        "note": "Bing Hybrid via QuadKey {q}. Respect the terms of use.",
        "preview": True,
    },
    "Esri World Imagery": {
        "url": "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
        "note": "Satellite/aerial tiles. Respect Esri terms of use.",
        "preview": True,
    },
    "OpenStreetMap Mapnik": {
        "url": "https://tile.openstreetmap.org/{z}/{x}/{y}.png",
        "note": "OSM standard map. Respect the terms of use; no bulk downloading.",
        "preview": True,
    },
    "OpenTopoMap": {
        "url": "https://{s}.tile.opentopomap.org/{z}/{x}/{y}.png",
        "note": "Topographic map. Respect the terms of use.",
        "preview": True,
    },
    "CartoDB Positron": {
        "url": "https://{s}.basemaps.cartocdn.com/light_all/{z}/{x}/{y}.png",
        "note": "Light basemap. Respect the terms of use.",
        "preview": True,
    },
    "NoniMapView Legacy: Google Satellite": {
        "url": "http://khm{rnd}.google.com/kh/v=47&x={x}&y={y}&z={z}&s=&hl=de",
        "note": "Legacy NoniMapView profile; may be outdated or blocked today.",
        "preview": True,
    },
    "NoniMapView Legacy: Google Road": {
        "url": "http://mt{rnd}.google.com/vt/lyrs=m&hl=de&x={x}&y={y}&z={z}",
        "note": "Legacy NoniMapView profile; may be outdated or blocked today.",
        "preview": True,
    },
}



@dataclasses.dataclass(frozen=True)
class TileJob:
    x: int
    y: int
    z: int
    col: int
    row: int


@dataclasses.dataclass
class StitchConfig:
    url_template: str
    output_file: Path
    cache_dir: Path
    z: int
    min_lat: float
    min_lon: float
    max_lat: float
    max_lon: float
    workers: int = 8
    rate_limit_ms: int = 50
    retries: int = 3
    output_format: str = "TIFF"
    tile_tif_dir: Optional[Path] = None
    skip_existing_cache: bool = True
    timeout: int = 20
    headers: Optional[Dict[str, str]] = None
    chunk_size: int = DEFAULT_CHUNK_SIZE
    use_sqlite: bool = True
    direct_bigtiff: bool = True
    save_individual_tifs: bool = False


def clamp_lat(lat: float) -> float:
    return max(min(lat, 85.05112878), -85.05112878)


def lonlat_to_tile(lon: float, lat: float, z: int) -> Tuple[int, int]:
    lat = clamp_lat(lat)
    n = 2 ** z
    x = int((lon + 180.0) / 360.0 * n)
    lat_rad = math.radians(lat)
    y = int((1.0 - math.asinh(math.tan(lat_rad)) / math.pi) / 2.0 * n)
    return max(0, min(n - 1, x)), max(0, min(n - 1, y))


def tile_to_lonlat(x: float, y: float, z: int) -> Tuple[float, float]:
    n = 2 ** z
    lon = x / n * 360.0 - 180.0
    lat_rad = math.atan(math.sinh(math.pi * (1.0 - 2.0 * y / n)))
    lat = math.degrees(lat_rad)
    return lon, lat


def tile_bounds_for_bbox(min_lat: float, min_lon: float, max_lat: float, max_lon: float, z: int):
    # NW and SE tile indices for Web Mercator XYZ.
    x1, y1 = lonlat_to_tile(min_lon, max_lat, z)
    x2, y2 = lonlat_to_tile(max_lon, min_lat, z)
    return min(x1, x2), min(y1, y2), max(x1, x2), max(y1, y2)


def tile_to_quadkey(x: int, y: int, z: int) -> str:
    q = []
    for i in range(z, 0, -1):
        digit = 0
        mask = 1 << (i - 1)
        if x & mask:
            digit += 1
        if y & mask:
            digit += 2
        q.append(str(digit))
    return "".join(q)


def expand_url(template: str, x: int, y: int, z: int) -> str:
    rnd = random.randint(0, 3)
    sub = ["a", "b", "c"][rnd % 3]
    snum = str(rnd % 4)
    q = tile_to_quadkey(x, y, z)
    # Unterstützt moderne Platzhalter, Bing QuadKey und viele alte NoniMapView-Platzhalter.
    return (template.replace("{x}", str(x))
                    .replace("{y}", str(y))
                    .replace("{z}", str(z))
                    .replace("{q}", q)
                    .replace("{quadkey}", q)
                    .replace("{rnd}", str(rnd))
                    .replace("{snum}", snum)
                    .replace("{s}", sub)
                    .replace("*GMX*", str(x))
                    .replace("*GMY*", str(y))
                    .replace("*ZM1*", str(z))
                    .replace("*IZM*", str(z))
                    .replace("*RND*", str(rnd))
                    .replace("*LAN*", "de")
                    .replace("*LAN-LAN*", "de-DE"))




def project_tiles_dir(output_file: Path) -> Path:
    return output_file.parent / f"{output_file.stem}_tiles"

def project_sqlite_dir(output_file: Path) -> Path:
    return output_file.parent / f"{output_file.stem}_sqlite"

def project_single_tiff_dir(output_file: Path) -> Path:
    return output_file.parent / f"{output_file.stem}_single_tiff_tiles"

def safe_cache_path(cache_dir: Path, z: int, x: int, y: int) -> Path:
    # Dateiname enthält jetzt ausdrücklich Zoom, X und Y.
    # Dadurch sieht man auch nach einem Abbruch sofort, welche Kachel vorhanden ist.
    return cache_dir / str(z) / f"z{z}_x{x}_y{y}.tile"


def default_tile_tif_dir(cfg: "StitchConfig") -> Path:
    base = cfg.output_file.parent if cfg.output_file.parent else Path.cwd()
    stem = cfg.output_file.stem or "map_output"
    return base / f"{stem}_einzelkacheln_tif_z{cfg.z}"


def safe_tile_tif_path(tile_tif_dir: Path, z: int, x: int, y: int) -> Path:
    return tile_tif_dir / f"z{z}_x{x}_y{y}.tif"


def save_tile_as_tif(data: Optional[bytes], out_path: Path) -> None:
    # Schreibt genau eine erzeugte Kachel sofort als TIFF.
    # Vorhandene TIFF-Tiles werden nicht erneut geschrieben.
    if out_path.exists() and out_path.stat().st_size > 100:
        return
    out_path.parent.mkdir(parents=True, exist_ok=True)
    im = decode_tile(data)
    tmp = out_path.with_suffix(".tmp.tif")
    im.save(tmp, format="TIFF", compression="tiff_deflate")
    os.replace(tmp, out_path)


def download_one(job: TileJob, cfg: StitchConfig, stop_event: threading.Event) -> Tuple[TileJob, Optional[bytes], Optional[str]]:
    if stop_event.is_set():
        return job, None, "cancelled"
    cache_path = safe_cache_path(cfg.cache_dir, job.z, job.x, job.y)
    if cfg.skip_existing_cache and cache_path.exists() and cache_path.stat().st_size > 100:
        try:
            return job, cache_path.read_bytes(), None
        except Exception:
            pass
    if requests is None:
        return job, None, "requests is not installed"
    url = expand_url(cfg.url_template, job.x, job.y, job.z)
    headers = {"User-Agent": USER_AGENT}
    if cfg.headers:
        headers.update(cfg.headers)
    last_err = None
    for attempt in range(cfg.retries):
        if stop_event.is_set():
            return job, None, "cancelled"
        try:
            if cfg.rate_limit_ms:
                time.sleep(cfg.rate_limit_ms / 1000.0)
            r = requests.get(url, headers=headers, timeout=cfg.timeout, stream=True)
            r.raise_for_status()
            data = r.content
            if len(data) < 50:
                raise RuntimeError("empty/invalid tile")
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = cache_path.with_suffix(".tmp")
            tmp.write_bytes(data)
            os.replace(tmp, cache_path)
            return job, data, None
        except Exception as exc:
            last_err = str(exc)
            time.sleep(0.5 * (attempt + 1))
    return job, None, last_err


def make_blank_tile() -> "Image.Image":
    return Image.new("RGB", (TILE_SIZE, TILE_SIZE), (255, 255, 255))


def decode_tile(data: Optional[bytes]) -> "Image.Image":
    if Image is None:
        raise RuntimeError("Pillow is not installed")
    if not data:
        return make_blank_tile()
    try:
        im = Image.open(io.BytesIO(data))
        return im.convert("RGB").resize((TILE_SIZE, TILE_SIZE))
    except Exception:
        return make_blank_tile()



def iter_tile_jobs(x_min: int, y_min: int, x_max: int, y_max: int, z: int):
    # Generator statt Liste: selbst riesige Bereiche erzeugen keine RAM-Spitze.
    for y in range(y_min, y_max + 1):
        for x in range(x_min, x_max + 1):
            yield TileJob(x, y, z, x - x_min, y - y_min)


def count_existing_tiles(cache_dir: Path, x_min: int, y_min: int, x_max: int, y_max: int, z: int) -> int:
    existing = 0
    for y in range(y_min, y_max + 1):
        for x in range(x_min, x_max + 1):
            p = safe_cache_path(cache_dir, z, x, y)
            if p.exists() and p.stat().st_size > 100:
                existing += 1
    return existing



def sqlite_path_for(cfg: StitchConfig) -> Path:
    return cfg.cache_dir / f"download_state_z{cfg.z}.sqlite"


def init_state_db(cfg: StitchConfig):
    if not cfg.use_sqlite:
        return None
    cfg.cache_dir.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(str(sqlite_path_for(cfg)), timeout=30)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA synchronous=NORMAL")
    db.execute("CREATE TABLE IF NOT EXISTS tiles (z INTEGER, x INTEGER, y INTEGER, status TEXT, updated REAL, error TEXT, PRIMARY KEY(z,x,y))")
    db.execute("CREATE TABLE IF NOT EXISTS chunks (z INTEGER, x0 INTEGER, y0 INTEGER, x1 INTEGER, y1 INTEGER, status TEXT, updated REAL, PRIMARY KEY(z,x0,y0,x1,y1))")
    db.commit()
    return db


def db_tile_done(db, z: int, x: int, y: int) -> bool:
    if db is None:
        return False
    row = db.execute("SELECT status FROM tiles WHERE z=? AND x=? AND y=?", (z, x, y)).fetchone()
    return bool(row and row[0] == "done")


def db_mark_tile(db, z: int, x: int, y: int, status: str, error: Optional[str] = None):
    if db is None:
        return
    db.execute("INSERT OR REPLACE INTO tiles(z,x,y,status,updated,error) VALUES(?,?,?,?,?,?)", (z, x, y, status, time.time(), error))


def db_mark_chunk(db, z: int, x0: int, y0: int, x1: int, y1: int, status: str):
    if db is None:
        return
    db.execute("INSERT OR REPLACE INTO chunks(z,x0,y0,x1,y1,status,updated) VALUES(?,?,?,?,?,?,?)", (z, x0, y0, x1, y1, status, time.time()))
    db.commit()


def iter_chunks(x_min: int, y_min: int, x_max: int, y_max: int, chunk_size: int):
    """Spatial chunk scheduler. Yields chunk bounds only; never builds a global tile list."""
    chunk_size = max(1, int(chunk_size))
    for cy in range(y_min, y_max + 1, chunk_size):
        for cx in range(x_min, x_max + 1, chunk_size):
            yield cx, cy, min(cx + chunk_size - 1, x_max), min(cy + chunk_size - 1, y_max)


def iter_chunk_jobs(cx0: int, cy0: int, cx1: int, cy1: int, z: int, x_min: int, y_min: int):
    """Yields jobs for one chunk only."""
    for y in range(cy0, cy1 + 1):
        for x in range(cx0, cx1 + 1):
            yield TileJob(x, y, z, x - x_min, y - y_min)


def open_direct_bigtiff(cfg: StitchConfig, width: int, height: int, log_cb):
    """Create a writable BigTIFF memmap. This writes directly to disk and does not allocate the full image in RAM.

    Note: tifffile.memmap creates a BigTIFF-compatible on-disk array. It is block/chunk written by our scheduler,
    but not LZW/DEFLATE-compressed while writing. This is intentional for random tile writes and resume safety.
    """
    if cfg.output_format.upper() != "TIFF" or not cfg.direct_bigtiff:
        return None, None
    estimated = width * height * 3
    if estimated > MAX_DIRECT_TIFF_BYTES:
        log_cb(f"Direct BigTIFF disabled: estimated output is {estimated/1_000_000_000_000:.2f} TB. Continuing as streaming tile/cache download with SQLite resume.")
        return None, None
    try:
        import tifffile
        cfg.output_file.parent.mkdir(parents=True, exist_ok=True)
        bigtiff = estimated > 3_800_000_000
        mem = tifffile.memmap(str(cfg.output_file), shape=(height, width, 3), dtype="uint8", bigtiff=bigtiff)
        log_cb(f"Direct BigTIFF writer opened: {cfg.output_file}")
        return mem, "memmap"
    except Exception as exc:
        log_cb(f"Direct BigTIFF writer unavailable ({exc}). Continuing with tile/cache output.")
        return None, None


def stitch_tiles(cfg: StitchConfig, progress_cb, log_cb, stop_event: threading.Event):
    if Image is None:
        raise RuntimeError("Pillow is required. Install with: pip install pillow requests")

    x_min, y_min, x_max, y_max = tile_bounds_for_bbox(cfg.min_lat, cfg.min_lon, cfg.max_lat, cfg.max_lon, cfg.z)
    cols = x_max - x_min + 1
    rows = y_max - y_min + 1
    total = cols * rows
    width = cols * TILE_SIZE
    height = rows * TILE_SIZE
    chunk_size = max(1, int(cfg.chunk_size))

    log_cb(f"Tile range: x={x_min}..{x_max}, y={y_min}..{y_max}")
    log_cb(f"Tiles: {cols} x {rows} = {total:,}")
    log_cb(f"Image size: {width:,} x {height:,} px")
    log_cb(f"Streaming chunk scheduler active: chunk size {chunk_size} x {chunk_size} tiles")
    log_cb("No global tile list is created in memory.")
    log_cb(f"Raw tile folder: {cfg.cache_dir / str(cfg.z)}")

    if total > HARD_TILE_WARNING:
        log_cb(f"Warning: very large selection with {total:,} tiles. This can run for days/weeks and may violate server terms if not authorized.")

    db = init_state_db(cfg)
    if db is not None:
        log_cb(f"SQLite resume database: {sqlite_path_for(cfg)}")

    tile_tif_dir = cfg.tile_tif_dir or default_tile_tif_dir(cfg)
    if cfg.save_individual_tifs:
        log_cb(f"Individual TIFF tiles enabled: {tile_tif_dir}")
    else:
        log_cb("Individual TIFF tiles disabled for streaming mode to avoid millions of filesystem files.")

    direct_mem, direct_kind = open_direct_bigtiff(cfg, width, height, log_cb)
    if cfg.output_format.upper() in {"TILES", "KACHELN"}:
        log_cb("Output mode: raw tiles + SQLite resume database. No merged image will be created.")

    max_workers = max(1, cfg.workers)
    max_inflight = max_workers * MAX_INFLIGHT_PER_WORKER
    done = 0
    skipped = 0
    errors = 0

    try:
        with cf.ThreadPoolExecutor(max_workers=max_workers) as pool:
            for cx0, cy0, cx1, cy1 in iter_chunks(x_min, y_min, x_max, y_max, chunk_size):
                if stop_event.is_set():
                    break
                log_cb(f"Chunk start: x={cx0}..{cx1}, y={cy0}..{cy1}")
                db_mark_chunk(db, cfg.z, cx0, cy0, cx1, cy1, "running")
                job_iter = iter_chunk_jobs(cx0, cy0, cx1, cy1, cfg.z, x_min, y_min)
                pending = set()
                while not stop_event.is_set():
                    while len(pending) < max_inflight:
                        try:
                            job = next(job_iter)
                        except StopIteration:
                            break
                        if db_tile_done(db, job.z, job.x, job.y):
                            skipped += 1
                            done += 1
                            if done % 100 == 0:
                                progress_cb(done, total, "Stream")
                            continue
                        pending.add(pool.submit(download_one, job, cfg, stop_event))
                    if not pending:
                        break
                    done_set, pending = cf.wait(pending, return_when=cf.FIRST_COMPLETED)
                    for fut in done_set:
                        job, data, err = fut.result()
                        done += 1
                        if err:
                            errors += 1
                            db_mark_tile(db, job.z, job.x, job.y, "error", err)
                            if errors <= 30:
                                log_cb(f"Error {job.z}/{job.x}/{job.y}: {err}")
                        else:
                            try:
                                if cfg.save_individual_tifs:
                                    save_tile_as_tif(data, safe_tile_tif_path(tile_tif_dir, job.z, job.x, job.y))
                                if direct_mem is not None:
                                    import numpy as np
                                    tile = decode_tile(data)
                                    r0 = job.row * TILE_SIZE
                                    c0 = job.col * TILE_SIZE
                                    direct_mem[r0:r0+TILE_SIZE, c0:c0+TILE_SIZE, :] = np.asarray(tile, dtype="uint8")
                                db_mark_tile(db, job.z, job.x, job.y, "done", None)
                            except Exception as exc:
                                errors += 1
                                db_mark_tile(db, job.z, job.x, job.y, "error", str(exc))
                                if errors <= 30:
                                    log_cb(f"Write error {job.z}/{job.x}/{job.y}: {exc}")
                        if done % 25 == 0 or done == total:
                            progress_cb(done, total, "Stream")
                            if db is not None:
                                db.commit()
                db_mark_chunk(db, cfg.z, cx0, cy0, cx1, cy1, "done" if not stop_event.is_set() else "stopped")
                if direct_mem is not None:
                    try:
                        direct_mem.flush()
                    except Exception:
                        pass
    finally:
        if direct_mem is not None:
            try:
                direct_mem.flush()
                del direct_mem
            except Exception:
                pass
        if db is not None:
            db.commit()
            db.close()

    if stop_event.is_set():
        log_cb("Stopped. Download state is preserved in SQLite and existing tile files will be skipped on resume.")
        return

    log_cb(f"Finished streaming. Downloaded/processed: {done:,}; skipped from SQLite: {skipped:,}; errors: {errors:,}")
    if cfg.output_format.upper() == "TIFF":
        if width * height * 3 <= MAX_DIRECT_TIFF_BYTES:
            log_cb(f"Finished BigTIFF/direct output: {cfg.output_file}")
        else:
            log_cb("Merged TIFF was intentionally not created because the requested raster is too large. Use TILES/SQLite mode or a lower zoom.")

def open_folder_in_file_manager(path: Path) -> None:
    """Open a folder in the OS file manager. Safe no-op if it cannot be opened."""
    path = Path(path).expanduser()
    path.mkdir(parents=True, exist_ok=True)
    try:
        if os.name == "nt":
            os.startfile(str(path))  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            import subprocess
            subprocess.Popen(["open", str(path)])
        else:
            import subprocess
            subprocess.Popen(["xdg-open", str(path)])
    except Exception:
        pass


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Py Map Stitcher - NoniMapView Style")
        self.geometry("1220x760")
        self.stop_event = threading.Event()
        self.worker_thread = None
        self.q = queue.Queue()
        self.view_z = 3
        self.center_lon = 10.0
        self.center_lat = 51.0
        self._view_generation = 0
        self._tile_images = []
        self._tile_cache_mem = {}
        self._mouse_last = None
        self._select_start = None
        self._select_rect = None
        self._build_ui()
        self.after(100, self._poll)

    def _build_ui(self):
        root = ttk.Frame(self, padding=8)
        root.pack(fill="both", expand=True)
        left = ttk.LabelFrame(root, text="Map / Download", padding=8)
        left.pack(side="left", fill="y", padx=(0, 8))
        right = ttk.Frame(root)
        right.pack(side="right", fill="both", expand=True)

        self.map_preset = tk.StringVar(value="Google Satellite")
        self.url = tk.StringVar(value=MAP_PRESETS["Google Satellite"]["url"])
        self.preset_note = tk.StringVar(value=MAP_PRESETS["Google Satellite"]["note"])
        self.z = tk.IntVar(value=18)
        self.min_lat = tk.StringVar(value="")
        self.min_lon = tk.StringVar(value="")
        self.max_lat = tk.StringVar(value="")
        self.max_lon = tk.StringVar(value="")
        self.workers = tk.IntVar(value=8)
        self.rate = tk.IntVar(value=20)
        self.chunk_size = tk.IntVar(value=64)
        self.save_tile_tifs = tk.BooleanVar(value=False)
        self.outfmt = tk.StringVar(value="TIFF")
        self.cache = tk.StringVar(value=str(Path.home() / "py_map_stitcher_cache"))
        self.outfile = tk.StringVar(value=str(Path.home() / "Desktop" / "map_output.tif"))
        self.raw_tiles_folder = tk.StringVar()
        self.single_tiff_folder = tk.StringVar()

        self._update_folder_fields()
        self.cache.trace_add("write", lambda *_: self._update_folder_fields())
        self.z.trace_add("write", lambda *_: self._update_folder_fields())
        self.outfile.trace_add("write", lambda *_: self._update_folder_fields())

        r = 0
        def row(label, widget):
            nonlocal r
            ttk.Label(left, text=label).grid(row=r, column=0, sticky="w", pady=3)
            widget.grid(row=r, column=1, sticky="ew", pady=3)
            r += 1
        left.columnconfigure(1, weight=1)

        preset_box = ttk.Combobox(left, textvariable=self.map_preset, values=list(MAP_PRESETS.keys()), state="readonly", width=45)
        preset_box.bind("<<ComboboxSelected>>", self.on_preset_changed)
        row("Map Selection", preset_box)
        row("URL Template", ttk.Entry(left, textvariable=self.url, width=50))
        ttk.Label(left, text="Placeholders: {z} {x} {y} {s} {rnd} {q}/{quadkey}; Noni: *GMX* *GMY* *ZM1* *RND*", wraplength=380).grid(row=r, column=0, columnspan=2, sticky="w"); r += 1
        ttk.Label(left, textvariable=self.preset_note, wraplength=380, foreground="#555").grid(row=r, column=0, columnspan=2, sticky="w"); r += 1
        row("Download Zoom", ttk.Spinbox(left, from_=0, to=22, textvariable=self.z, width=8))
        row("South / min lat", ttk.Entry(left, textvariable=self.min_lat))
        row("West / min lon", ttk.Entry(left, textvariable=self.min_lon))
        row("North / max lat", ttk.Entry(left, textvariable=self.max_lat))
        row("East / max lon", ttk.Entry(left, textvariable=self.max_lon))
        row("Download Threads", ttk.Spinbox(left, from_=1, to=64, textvariable=self.workers, width=8))
        row("Delay per Request ms", ttk.Spinbox(left, from_=0, to=5000, increment=10, textvariable=self.rate, width=8))
        row("Chunk size tiles", ttk.Spinbox(left, from_=8, to=1024, increment=8, textvariable=self.chunk_size, width=8))
        row("Save single TIFF tiles", ttk.Checkbutton(left, variable=self.save_tile_tifs))
        row("Output Format", ttk.Combobox(left, textvariable=self.outfmt, values=["TIFF", "TILES"], state="readonly"))

        out_frame = ttk.Frame(left)
        ttk.Entry(out_frame, textvariable=self.outfile, width=35).pack(side="left", fill="x", expand=True)
        ttk.Button(out_frame, text="…", command=self.pick_output).pack(side="right")
        row("Output File", out_frame)
        cache_frame = ttk.Frame(left)
        ttk.Entry(cache_frame, textvariable=self.cache, width=35).pack(side="left", fill="x", expand=True)
        ttk.Button(cache_frame, text="…", command=self.pick_cache).pack(side="right")
        row("Cache", cache_frame)

        raw_tiles_frame = ttk.Frame(left)
        ttk.Entry(raw_tiles_frame, textvariable=self.raw_tiles_folder, width=35, state="readonly").pack(side="left", fill="x", expand=True)
        ttk.Button(raw_tiles_frame, text="Open", command=self.open_raw_tiles_folder).pack(side="right")
        row("Raw Tiles Folder", raw_tiles_frame)

        tiff_tiles_frame = ttk.Frame(left)
        ttk.Entry(tiff_tiles_frame, textvariable=self.single_tiff_folder, width=35, state="readonly").pack(side="left", fill="x", expand=True)
        ttk.Button(tiff_tiles_frame, text="Open", command=self.open_single_tiff_folder).pack(side="right")
        row("Single TIFF Folder", tiff_tiles_frame)

        btns = ttk.Frame(left)
        btns.grid(row=r, column=0, columnspan=2, sticky="ew", pady=12)
        ttk.Button(btns, text="Calculate", command=self.calculate).pack(side="left", expand=True, fill="x", padx=2)
        ttk.Button(btns, text="Start", command=self.start).pack(side="left", expand=True, fill="x", padx=2)
        ttk.Button(btns, text="Stop", command=self.stop).pack(side="left", expand=True, fill="x", padx=2)
        r += 1
        ttk.Label(left, text="Only use servers where downloading/stitching is allowed. Google/Bing/OSM may restrict bulk downloads.", wraplength=380, foreground="#555").grid(row=r, column=0, columnspan=2, sticky="w")

        map_frame = ttk.LabelFrame(right, text="Map Preview / Selection like NoniMapView", padding=6)
        map_frame.pack(fill="both", expand=True)
        controls = ttk.Frame(map_frame)
        controls.pack(fill="x", pady=(0, 4))
        self.view_status = tk.StringVar(value="Mouse wheel: zoom | Left drag: move map | Right drag or Shift+Left: select area")
        ttk.Button(controls, text="Reload", command=self.refresh_map).pack(side="left", padx=3)
        ttk.Button(controls, text="+", width=3, command=lambda: self.zoom_map(1)).pack(side="left", padx=1)
        ttk.Button(controls, text="-", width=3, command=lambda: self.zoom_map(-1)).pack(side="left", padx=1)
        ttk.Label(controls, textvariable=self.view_status, foreground="#555").pack(side="left", padx=8)

        self.map_canvas = tk.Canvas(map_frame, bg="#d9e6f2", height=500, highlightthickness=1, highlightbackground="#aaa")
        self.map_canvas.pack(fill="both", expand=True)
        self.map_canvas.bind("<Configure>", lambda e: self.refresh_map())
        self.map_canvas.bind("<ButtonPress-1>", self._left_press)
        self.map_canvas.bind("<B1-Motion>", self._left_drag)
        self.map_canvas.bind("<ButtonRelease-1>", self._left_release)
        self.map_canvas.bind("<ButtonPress-3>", self._select_press)
        self.map_canvas.bind("<B3-Motion>", self._select_drag)
        self.map_canvas.bind("<ButtonRelease-3>", self._select_release)
        self.map_canvas.bind("<MouseWheel>", self._mousewheel)
        self.map_canvas.bind("<Button-4>", lambda e: self.zoom_map(1, e.x, e.y))
        self.map_canvas.bind("<Button-5>", lambda e: self.zoom_map(-1, e.x, e.y))

        bottom = ttk.LabelFrame(right, text="Status / Log", padding=6)
        bottom.pack(fill="both", expand=True, pady=(8, 0))
        self.progress = ttk.Progressbar(bottom, mode="determinate")
        self.progress.pack(fill="x", pady=(0, 6))
        self.status = tk.StringVar(value="Ready")
        ttk.Label(bottom, textvariable=self.status).pack(anchor="w")
        self.log = tk.Text(bottom, wrap="word", height=8)
        self.log.pack(fill="both", expand=True, pady=(6, 0))
        self.after(300, self.refresh_map)

    def on_preset_changed(self, event=None):
        name = self.map_preset.get()
        preset = MAP_PRESETS.get(name, MAP_PRESETS["Custom"])
        self.url.set(preset["url"])
        self.preset_note.set(preset.get("note", ""))
        self.refresh_map()

    def _world_px(self, lon, lat, z=None):
        z = self.view_z if z is None else z
        lat = clamp_lat(lat)
        n = 2 ** z * TILE_SIZE
        x = (lon + 180.0) / 360.0 * n
        lat_rad = math.radians(lat)
        y = (1.0 - math.asinh(math.tan(lat_rad)) / math.pi) / 2.0 * n
        return x, y

    def _lonlat_from_world_px(self, px, py, z=None):
        z = self.view_z if z is None else z
        return tile_to_lonlat(px / TILE_SIZE, py / TILE_SIZE, z)

    def _screen_to_lonlat(self, sx, sy):
        cw = max(1, self.map_canvas.winfo_width())
        ch = max(1, self.map_canvas.winfo_height())
        cx, cy = self._world_px(self.center_lon, self.center_lat)
        wx = cx + sx - cw / 2
        wy = cy + sy - ch / 2
        return self._lonlat_from_world_px(wx, wy)

    def _visible_tiles(self):
        cw = max(1, self.map_canvas.winfo_width())
        ch = max(1, self.map_canvas.winfo_height())
        cx, cy = self._world_px(self.center_lon, self.center_lat)
        left = cx - cw / 2
        top = cy - ch / 2
        ntiles = 2 ** self.view_z
        x0 = max(0, int(math.floor(left / TILE_SIZE)) - 1)
        y0 = max(0, int(math.floor(top / TILE_SIZE)) - 1)
        x1 = min(ntiles - 1, int(math.floor((left + cw) / TILE_SIZE)) + 1)
        y1 = min(ntiles - 1, int(math.floor((top + ch) / TILE_SIZE)) + 1)
        return x0, y0, x1, y1, left, top

    def refresh_map(self):
        if Image is None:
            return
        self._view_generation += 1
        gen = self._view_generation
        self.map_canvas.delete("all")
        self._tile_images.clear()
        cw = max(1, self.map_canvas.winfo_width())
        ch = max(1, self.map_canvas.winfo_height())
        self.map_canvas.create_rectangle(0, 0, cw, ch, fill="#e8eef5", outline="")
        self.map_canvas.create_text(10, 10, anchor="nw", text="Preview loading visible tiles...", fill="#333")
        try:
            x0, y0, x1, y1, left, top = self._visible_tiles()
        except Exception:
            return
        self.view_status.set(f"Zoom {self.view_z} | visible: x {x0}-{x1}, y {y0}-{y1} | Left drag: move | Right/Shift+Left: select area")
        template = self.url.get().strip()
        jobs = [(x, y) for y in range(y0, y1 + 1) for x in range(x0, x1 + 1)]

        def worker():
            for x, y in jobs:
                if gen != self._view_generation:
                    return
                key = (template, self.view_z, x, y)
                pil_img = self._tile_cache_mem.get(key)
                if pil_img is None:
                    data = None
                    p = safe_cache_path(Path(self.cache.get()).expanduser() / "_preview", self.view_z, x, y)
                    try:
                        if p.exists() and p.stat().st_size > 100:
                            data = p.read_bytes()
                        elif requests is not None:
                            url = expand_url(template, x, y, self.view_z)
                            r = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=6)
                            r.raise_for_status()
                            data = r.content
                            p.parent.mkdir(parents=True, exist_ok=True)
                            tmp = p.with_suffix(".tmp")
                            tmp.write_bytes(data)
                            os.replace(tmp, p)
                        if data:
                            pil_img = Image.open(io.BytesIO(data)).convert("RGB").resize((TILE_SIZE, TILE_SIZE))
                            if len(self._tile_cache_mem) > 500:
                                self._tile_cache_mem.clear()
                            self._tile_cache_mem[key] = pil_img
                    except Exception:
                        pil_img = make_blank_tile()
                sx = int(x * TILE_SIZE - left)
                sy = int(y * TILE_SIZE - top)
                self.q.put(("maptile", gen, sx, sy, pil_img))
            self.q.put(("mapdone", gen))
        threading.Thread(target=worker, daemon=True).start()

    def _draw_tile(self, gen, sx, sy, pil_img):
        if gen != self._view_generation:
            return
        img = ImageTk.PhotoImage(pil_img)
        self._tile_images.append(img)
        self.map_canvas.create_image(sx, sy, image=img, anchor="nw")

    def zoom_map(self, delta, sx=None, sy=None):
        old_z = self.view_z
        new_z = max(0, min(22, self.view_z + delta))
        if new_z == old_z:
            return
        cw = max(1, self.map_canvas.winfo_width())
        ch = max(1, self.map_canvas.winfo_height())
        sx = cw / 2 if sx is None else sx
        sy = ch / 2 if sy is None else sy
        anchor_lon, anchor_lat = self._screen_to_lonlat(sx, sy)
        self.view_z = new_z
        ax, ay = self._world_px(anchor_lon, anchor_lat, new_z)
        cx = ax - sx + cw / 2
        cy = ay - sy + ch / 2
        self.center_lon, self.center_lat = self._lonlat_from_world_px(cx, cy, new_z)
        self.refresh_map()

    def _mousewheel(self, event):
        self.zoom_map(1 if event.delta > 0 else -1, event.x, event.y)

    def _left_press(self, event):
        if event.state & 0x0001:  # Shift links = Auswahl
            self._select_press(event)
            return
        self._mouse_last = (event.x, event.y, self.center_lon, self.center_lat)
        self.map_canvas.configure(cursor="fleur")

    def _left_drag(self, event):
        if self._select_start:
            self._select_drag(event)
            return
        if not self._mouse_last:
            return
        x0, y0, lon0, lat0 = self._mouse_last
        cx, cy = self._world_px(lon0, lat0)
        new_cx = cx - (event.x - x0)
        new_cy = cy - (event.y - y0)
        self.center_lon, self.center_lat = self._lonlat_from_world_px(new_cx, new_cy)
        self.refresh_map()

    def _left_release(self, event):
        if self._select_start:
            self._select_release(event)
        self._mouse_last = None
        self.map_canvas.configure(cursor="")

    def _select_press(self, event):
        self._select_start = self._clamp_screen_point(event.x, event.y)
        if self._select_rect:
            self.map_canvas.delete(self._select_rect)
            self._select_rect = None

    def _clamp_screen_point(self, x, y):
        cw = max(1, self.map_canvas.winfo_width())
        ch = max(1, self.map_canvas.winfo_height())
        return max(0, min(cw - 1, x)), max(0, min(ch - 1, y))

    def _select_drag(self, event):
        if not self._select_start:
            return
        x0, y0 = self._select_start
        x1, y1 = self._clamp_screen_point(event.x, event.y)
        if self._select_rect:
            self.map_canvas.coords(self._select_rect, x0, y0, x1, y1)
        else:
            self._select_rect = self.map_canvas.create_rectangle(x0, y0, x1, y1, outline="red", width=2)

    def _select_release(self, event):
        if not self._select_start:
            return
        x0, y0 = self._select_start
        x1, y1 = self._clamp_screen_point(event.x, event.y)
        self._select_start = None
        if abs(x1 - x0) < 5 or abs(y1 - y0) < 5:
            return
        lon_a, lat_a = self._screen_to_lonlat(x0, y0)
        lon_b, lat_b = self._screen_to_lonlat(x1, y1)
        self.min_lon.set(f"{min(lon_a, lon_b):.8f}")
        self.max_lon.set(f"{max(lon_a, lon_b):.8f}")
        self.min_lat.set(f"{min(lat_a, lat_b):.8f}")
        self.max_lat.set(f"{max(lat_a, lat_b):.8f}")
        self._log("Selection imported from map preview.")
        self.calculate()


    def _raw_tiles_dir(self) -> Path:
        try:
            return Path(self.cache.get()).expanduser() / str(int(self.z.get()))
        except Exception:
            return Path(self.cache.get()).expanduser()

    def _single_tiff_dir(self) -> Path:
        try:
            out = Path(self.outfile.get()).expanduser()
            return out.parent / f"{out.stem or 'map_output'}_single_tiff_tiles_z{int(self.z.get())}"
        except Exception:
            return Path(self.outfile.get()).expanduser().parent

    def _update_folder_fields(self):
        try:
            self.raw_tiles_folder.set(str(self._raw_tiles_dir()))
            self.single_tiff_folder.set(str(self._single_tiff_dir()))
        except Exception:
            pass

    def open_raw_tiles_folder(self):
        open_folder_in_file_manager(self._raw_tiles_dir())

    def open_single_tiff_folder(self):
        open_folder_in_file_manager(self._single_tiff_dir())

    def pick_output(self):
        ext = {"TILES": "", "TIFF": ".tif"}.get(self.outfmt.get(), ".tif")
        p = filedialog.asksaveasfilename(defaultextension=ext)
        if p:
            self.outfile.set(p)
            self._update_folder_fields()

    def pick_cache(self):
        p = filedialog.askdirectory()
        if p:
            self.cache.set(p)
            self._update_folder_fields()

    def _config(self) -> StitchConfig:
        return StitchConfig(
            url_template=self.url.get().strip(),
            output_file=Path(self.outfile.get()).expanduser(),
            cache_dir=project_tiles_dir(Path(self.outfile.get()).expanduser()),
            z=int(self.z.get()),
            min_lat=float(self.min_lat.get().replace(",", ".")),
            min_lon=float(self.min_lon.get().replace(",", ".")),
            max_lat=float(self.max_lat.get().replace(",", ".")),
            max_lon=float(self.max_lon.get().replace(",", ".")),
            workers=int(self.workers.get()),
            rate_limit_ms=int(self.rate.get()),
            output_format=self.outfmt.get(),
            tile_tif_dir=Path(self.outfile.get()).expanduser().parent / f"{Path(self.outfile.get()).expanduser().stem or 'map_output'}_single_tiff_tiles_z{int(self.z.get())}",
            chunk_size=int(self.chunk_size.get()),
            use_sqlite=True,
            direct_bigtiff=True,
            save_individual_tifs=bool(self.save_tile_tifs.get()),
        )

    def calculate(self):
        try:
            cfg = self._config()
            x_min, y_min, x_max, y_max = tile_bounds_for_bbox(cfg.min_lat, cfg.min_lon, cfg.max_lat, cfg.max_lon, cfg.z)
            cols = x_max - x_min + 1
            rows = y_max - y_min + 1
            self._log(f"Calculation: x={x_min}..{x_max}, y={y_min}..{y_max}")
            self._log(f"Tiles: {cols} x {rows} = {cols*rows:,}; Pixel: {cols*TILE_SIZE:,} x {rows*TILE_SIZE:,}")
            self._log(f"Raw tile folder: {self._raw_tiles_dir()}")
            self._log(f"SQLite resume database: {sqlite_path_for(cfg)}")
            if self.save_tile_tifs.get():
                self._log(f"Single TIFF folder: {self._single_tiff_dir()}")
            if self.outfmt.get().upper() == "TILES":
                self._log("Output: raw tiles + SQLite resume database, no merged file.")
            elif self.outfmt.get().upper() == "TIFF":
                self._log("Output: direct BigTIFF streaming where possible; if too large, it continues as tile/cache mode.")
        except Exception as exc:
            messagebox.showerror("Error", str(exc))

    def start(self):
        if self.worker_thread and self.worker_thread.is_alive():
            messagebox.showinfo("Running", "A job is already running.")
            return
        try:
            cfg = self._config()
        except Exception as exc:
            messagebox.showerror("Error", str(exc))
            return
        self._update_folder_fields()
        self._log(f"Raw tile folder: {self._raw_tiles_dir()}")
        self._log(f"SQLite resume database: {sqlite_path_for(cfg)}")
        if self.save_tile_tifs.get():
            self._log(f"Single TIFF folder: {self._single_tiff_dir()}")
        self.stop_event.clear()
        self.progress["value"] = 0
        self.worker_thread = threading.Thread(target=self._run_job, args=(cfg,), daemon=True)
        self.worker_thread.start()

    def stop(self):
        self.stop_event.set()
        self.status.set("Stop requested...")

    def _run_job(self, cfg):
        try:
            stitch_tiles(cfg, self._progress, self._log_thread, self.stop_event)
        except Exception as exc:
            self._log_thread(f"ERROR: {exc}")
            self.q.put(("status", "Error"))

    def _progress(self, done, total, phase):
        self.q.put(("progress", done, total, phase))

    def _log_thread(self, msg):
        self.q.put(("log", msg))

    def _log(self, msg):
        self.log.insert("end", msg + "\n")
        self.log.see("end")

    def _poll(self):
        try:
            while True:
                item = self.q.get_nowait()
                if item[0] == "log":
                    self._log(item[1])
                elif item[0] == "progress":
                    _, done, total, phase = item
                    self.progress["maximum"] = total
                    self.progress["value"] = done
                    self.status.set(f"{phase}: {done:,}/{total:,}")
                elif item[0] == "status":
                    self.status.set(item[1])
                elif item[0] == "maptile":
                    _, gen, sx, sy, pil_img = item
                    self._draw_tile(gen, sx, sy, pil_img)
                elif item[0] == "mapdone":
                    _, gen = item
                    if gen == self._view_generation:
                        self.status.set("Preview loaded")
        except queue.Empty:
            pass
        self.after(60, self._poll)


if __name__ == "__main__":
    App().mainloop()
