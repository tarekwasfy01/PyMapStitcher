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
MAX_INFLIGHT_PER_WORKER = 4  # verhindert Millionen Futures im RAM
HARD_TILE_WARNING = 5_000_000



MAP_PRESETS = {
    "Custom": {
        "url": "https://your-tile-server.example/{z}/{x}/{y}.png",
        "note": "Enter a custom URL template manually.",
        "preview": True,
    },
    "Google Satellite": {
        "url": "https://mt{rnd}.google.com/vt/lyrs=s&x={x}&y={y}&z={z}&hl=de",
        "note": "Google Satellite. Nutzungsbedingungen beachten; kein Massendownload ohne Erlaubnis.",
        "preview": True,
    },
    "Google Hybrid": {
        "url": "https://mt{rnd}.google.com/vt/lyrs=y&x={x}&y={y}&z={z}&hl=de",
        "note": "Google Satellite mit Beschriftung. Nutzungsbedingungen beachten.",
        "preview": True,
    },
    "Bing Satellite": {
        "url": "https://ecn.t{snum}.tiles.virtualearth.net/tiles/a{q}.jpeg?g=14574&mkt=de-DE&n=z",
        "note": "Bing aerial/satellite via QuadKey {q}. Respect the terms of use.",
        "preview": True,
    },
    "Bing Hybrid": {
        "url": "https://ecn.t{snum}.tiles.virtualearth.net/tiles/h{q}.jpeg?g=14574&mkt=de-DE&n=z",
        "note": "Bing hybrid via QuadKey {q}. Respect the terms of use.",
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
    "NoniMapView Legacy: Google Satellitee": {
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
    # Vorhandene TIFF-Kacheln werden nicht erneut geschrieben.
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


def stitch_tiles(cfg: StitchConfig, progress_cb, log_cb, stop_event: threading.Event):
    if Image is None:
        raise RuntimeError("Pillow is required. Install with: pip install pillow requests")
    x_min, y_min, x_max, y_max = tile_bounds_for_bbox(cfg.min_lat, cfg.min_lon, cfg.max_lat, cfg.max_lon, cfg.z)
    cols = x_max - x_min + 1
    rows = y_max - y_min + 1
    total = cols * rows
    width = cols * TILE_SIZE
    height = rows * TILE_SIZE
    log_cb(f"Tiles: {cols} x {rows} = {total:,}")
    log_cb(f"Image size: {width:,} x {height:,} px")
    if width * height > 300_000_000 and cfg.output_format.upper() in {"PNG", "JPEG"}:
        log_cb("Warning: PNG/JPEG für sehr große Bilder kann langsam oder instabil sein; TIFF ist besser.")

    if total > HARD_TILE_WARNING:
        log_cb(f"Warning: very large area mit {total:,} Kacheln. Es wird trotzdem RAM-schonend als Stream gearbeitet, aber der Download kann extrem lange dauern.")

    # Resume-Prüfung ebenfalls gestreamt, ohne Job-Liste im RAM.
    existing = count_existing_tiles(cfg.cache_dir, x_min, y_min, x_max, y_max, cfg.z)
    if existing:
        log_cb(f"Resume: {existing:,} existing tiles will be skipped/read from cache.")

    tile_tif_dir = cfg.tile_tif_dir or default_tile_tif_dir(cfg)
    log_cb(f"Single TIFF tiles: {tile_tif_dir}")

    # Download parallel, aber mit begrenzter Zahl gleichzeitig offener Futures.
    # Wichtig: keine jobs=[] und keine futs=[] für Milliarden Kacheln.
    done = 0
    errors = 0
    max_workers = max(1, cfg.workers)
    max_inflight = max_workers * MAX_INFLIGHT_PER_WORKER
    job_iter = iter(iter_tile_jobs(x_min, y_min, x_max, y_max, cfg.z))
    with cf.ThreadPoolExecutor(max_workers=max_workers) as pool:
        pending = set()
        while not stop_event.is_set():
            while len(pending) < max_inflight:
                try:
                    job = next(job_iter)
                except StopIteration:
                    break
                pending.add(pool.submit(download_one, job, cfg, stop_event))
            if not pending:
                break
            done_set, pending = cf.wait(pending, return_when=cf.FIRST_COMPLETED)
            for fut in done_set:
                job, data, err = fut.result()
                done += 1
                if err:
                    errors += 1
                    if errors <= 20:
                        log_cb(f"Error {job.z}/{job.x}/{job.y}: {err}")
                else:
                    try:
                        save_tile_as_tif(data, safe_tile_tif_path(tile_tif_dir, job.z, job.x, job.y))
                    except Exception as exc:
                        errors += 1
                        if errors <= 20:
                            log_cb(f"Could not write TIFF tile {job.z}/{job.x}/{job.y}: {exc}")
                if done % 10 == 0 or done == total:
                    progress_cb(done, total, "Download")
    if stop_event.is_set():
        log_cb("Abgebrochen. Readys vorhandene Kacheln bleiben im Cache und werden beim nächsten Start übersprungen.")
        return

    if cfg.output_format.upper() in {"KACHELN", "TILES"}:
        log_cb(f"Tile mode complete. Raw tiles: {safe_cache_path(cfg.cache_dir, cfg.z, x_min, y_min).parent}")
        log_cb(f"Single TIFF tiles: {tile_tif_dir}")
        progress_cb(total, total, "Kacheln")
        return

    cfg.output_file.parent.mkdir(parents=True, exist_ok=True)
    log_cb("Merging started tile-by-tile: only one tile is loaded into RAM at a time...")

    # RAM-schonend: zeilenweise Streifen erzeugen und in eine TIFF-Datei schreiben.
    # Pillow kann multipage nicht als echtes großes Raster anhängen; daher erstellen wir bei moderaten Größen direkt.
    # Für extrem große TIFFs wird eine BigTIFF-taugliche Option via tifffile angeboten.
    if cfg.output_format.upper() == "TIFF":
        try:
            import numpy as np
            import tifffile
            bigtiff = width * height * 3 > 3_800_000_000
            mem = tifffile.memmap(str(cfg.output_file), shape=(height, width, 3), dtype="uint8", bigtiff=bigtiff)
            written = 0
            for row in range(rows):
                if stop_event.is_set():
                    break
                for col in range(cols):
                    if stop_event.is_set():
                        break
                    x = x_min + col
                    y = y_min + row
                    tif_p = safe_tile_tif_path(tile_tif_dir, cfg.z, x, y)
                    if tif_p.exists():
                        tile = Image.open(tif_p).convert("RGB").resize((TILE_SIZE, TILE_SIZE))
                    else:
                        raw_p = safe_cache_path(cfg.cache_dir, cfg.z, x, y)
                        data = raw_p.read_bytes() if raw_p.exists() else None
                        tile = decode_tile(data)
                        save_tile_as_tif(data, tif_p)
                    mem[row*TILE_SIZE:(row+1)*TILE_SIZE, col*TILE_SIZE:(col+1)*TILE_SIZE, :] = np.asarray(tile, dtype="uint8")
                    written += 1
                    if written % 10 == 0 or written == total:
                        progress_cb(written, total, "Write")
            mem.flush()
            del mem
        except Exception as exc:
            log_cb(f"tifffile nicht verfügbar/fehlgeschlagen ({exc}); fallback auf Pillow, nur für kleinere Karten.")
            if width * height > 250_000_000:
                raise RuntimeError("Für diese Größe bitte installieren: pip install tifffile numpy")
            out = Image.new("RGB", (width, height))
            for row in range(rows):
                strip = Image.new("RGB", (width, TILE_SIZE))
                for col in range(cols):
                    x = x_min + col
                    y = y_min + row
                    p = safe_cache_path(cfg.cache_dir, cfg.z, x, y)
                    data = p.read_bytes() if p.exists() else None
                    strip.paste(decode_tile(data), (col * TILE_SIZE, 0))
                out.paste(strip, (0, row * TILE_SIZE))
                progress_cb(row + 1, rows, "Write")
            out.save(cfg.output_file, format="TIFF", compression="tiff_deflate")
    else:
        if width * height > 250_000_000:
            raise RuntimeError("PNG/JPEG ist für diese Größe zu groß. Bitte TIFF wählen.")
        out = Image.new("RGB", (width, height))
        for row in range(rows):
            strip = Image.new("RGB", (width, TILE_SIZE))
            for col in range(cols):
                x = x_min + col
                y = y_min + row
                p = safe_cache_path(cfg.cache_dir, cfg.z, x, y)
                data = p.read_bytes() if p.exists() else None
                strip.paste(decode_tile(data), (col * TILE_SIZE, 0))
            out.paste(strip, (0, row * TILE_SIZE))
            progress_cb(row + 1, rows, "Write")
        fmt = cfg.output_format.upper()
        save_kwargs = {"quality": 92} if fmt == "JPEG" else {}
        out.save(cfg.output_file, format=fmt, **save_kwargs)

    if stop_event.is_set():
        log_cb("Cancelled during writing.")
    else:
        log_cb(f"Finished: {cfg.output_file}")


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
        self.outfmt = tk.StringVar(value="TIFF")
        self.cache = tk.StringVar(value=str(Path.home() / "py_map_stitcher_cache"))
        self.outfile = tk.StringVar(value=str(Path.home() / "Desktop" / "map_output.tif"))

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
        row("Output Format", ttk.Combobox(left, textvariable=self.outfmt, values=["TIFF", "KACHELN", "PNG", "JPEG"], state="readonly"))

        out_frame = ttk.Frame(left)
        ttk.Entry(out_frame, textvariable=self.outfile, width=35).pack(side="left", fill="x", expand=True)
        ttk.Button(out_frame, text="…", command=self.pick_output).pack(side="right")
        row("Output File", out_frame)
        cache_frame = ttk.Frame(left)
        ttk.Entry(cache_frame, textvariable=self.cache, width=35).pack(side="left", fill="x", expand=True)
        ttk.Button(cache_frame, text="…", command=self.pick_cache).pack(side="right")
        row("Cache", cache_frame)

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

    def pick_output(self):
        ext = {"KACHELN": "", "TIFF": ".tif", "PNG": ".png", "JPEG": ".jpg"}.get(self.outfmt.get(), ".tif")
        p = filedialog.asksaveasfilename(defaultextension=ext)
        if p:
            self.outfile.set(p)

    def pick_cache(self):
        p = filedialog.askdirectory()
        if p:
            self.cache.set(p)

    def _config(self) -> StitchConfig:
        return StitchConfig(
            url_template=self.url.get().strip(),
            output_file=Path(self.outfile.get()).expanduser(),
            cache_dir=Path(self.cache.get()).expanduser(),
            z=int(self.z.get()),
            min_lat=float(self.min_lat.get().replace(",", ".")),
            min_lon=float(self.min_lon.get().replace(",", ".")),
            max_lat=float(self.max_lat.get().replace(",", ".")),
            max_lon=float(self.max_lon.get().replace(",", ".")),
            workers=int(self.workers.get()),
            rate_limit_ms=int(self.rate.get()),
            output_format=self.outfmt.get(),
            tile_tif_dir=Path(self.outfile.get()).expanduser().parent / f"{Path(self.outfile.get()).expanduser().stem or 'map_output'}_einzelkacheln_tif_z{int(self.z.get())}",
        )

    def calculate(self):
        try:
            cfg = self._config()
            x_min, y_min, x_max, y_max = tile_bounds_for_bbox(cfg.min_lat, cfg.min_lon, cfg.max_lat, cfg.max_lon, cfg.z)
            cols = x_max - x_min + 1
            rows = y_max - y_min + 1
            self._log(f"Calculation: x={x_min}..{x_max}, y={y_min}..{y_max}")
            self._log(f"Tiles: {cols} x {rows} = {cols*rows:,}; Pixel: {cols*TILE_SIZE:,} x {rows*TILE_SIZE:,}")
            if self.outfmt.get().upper() == "KACHELN":
                self._log("Ausgabe: nur Roh-Kacheln + Single TIFF tiles, keine Gesamtdatei.")
            elif self.outfmt.get().upper() == "TIFF":
                self._log("Ausgabe: Single TIFF tiles + finale TIFF-Datei, kachelweise mit maximal einer Kachel im RAM.")
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
