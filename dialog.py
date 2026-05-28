# -*- coding: utf-8 -*-
"""Core functions copied from the standalone Py Map Stitcher script.
No GDAL/rasterio/geopandas imports are used here.
"""
import concurrent.futures as cf
import dataclasses
import io
import math
import os
import random
import sqlite3
import sys
import threading
import time
from pathlib import Path
from typing import Dict, Optional, Tuple

try:
    import requests
except Exception:
    requests = None

try:
    from PIL import Image, ImageFile
    ImageFile.LOAD_TRUNCATED_IMAGES = True
except Exception:
    Image = None

TILE_SIZE = 256
USER_AGENT = "PyMapStitcher/1.0 (+local user tool)"
MAX_INFLIGHT_PER_WORKER = 4  # prevents millions of Futures in RAM
HARD_TILE_WARNING = 5_000_000
DEFAULT_CHUNK_SIZE = 64
MAX_DIRECT_TIFF_BYTES = 1_000_000_000_000  # 1 TB safety limit for sparse BigTIFF output
MAX_PIL_FALLBACK_TIFF_BYTES = 750_000_000  # fallback when tifffile is not available in QGIS Python



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


def lonlat_to_webmercator(lon: float, lat: float) -> Tuple[float, float]:
    lat = clamp_lat(lat)
    r = 6378137.0
    x = r * math.radians(lon)
    y = r * math.log(math.tan(math.pi / 4.0 + math.radians(lat) / 2.0))
    return x, y


def tile_webmercator_bounds(x: int, y: int, z: int) -> Tuple[float, float, float, float]:
    west_lon, north_lat = tile_to_lonlat(x, y, z)
    east_lon, south_lat = tile_to_lonlat(x + 1, y + 1, z)
    west, north = lonlat_to_webmercator(west_lon, north_lat)
    east, south = lonlat_to_webmercator(east_lon, south_lat)
    return west, south, east, north


def mosaic_webmercator_bounds(x_min: int, y_min: int, x_max: int, y_max: int, z: int) -> Tuple[float, float, float, float]:
    west_lon, north_lat = tile_to_lonlat(x_min, y_min, z)
    east_lon, south_lat = tile_to_lonlat(x_max + 1, y_max + 1, z)
    west, north = lonlat_to_webmercator(west_lon, north_lat)
    east, south = lonlat_to_webmercator(east_lon, south_lat)
    return west, south, east, north


def write_worldfile_and_prj(tif_path: Path, width: int, height: int, bounds_3857: Tuple[float, float, float, float]) -> None:
    # Georeferenzierung exakt wie im alten funktionierenden Standalone-Skript:
    # BigTIFF/TIFF bleibt unverändert, QGIS liest die Position über .tfw + .prj.
    west, south, east, north = bounds_3857
    px_w = (east - west) / float(width)
    px_h = (south - north) / float(height)
    tfw = tif_path.with_suffix(".tfw")
    prj = tif_path.with_suffix(".prj")
    tfw.write_text(
        f"{px_w:.12f}\n0.0\n0.0\n{px_h:.12f}\n{west + px_w / 2.0:.12f}\n{north + px_h / 2.0:.12f}\n",
        encoding="utf-8",
    )
    prj.write_text(
        'PROJCS["WGS 84 / Pseudo-Mercator",GEOGCS["WGS 84",DATUM["WGS_1984",SPHEROID["WGS 84",6378137,298.257223563]],PRIMEM["Greenwich",0],UNIT["degree",0.0174532925199433]],PROJECTION["Mercator_1SP"],PARAMETER["central_meridian",0],PARAMETER["scale_factor",1],PARAMETER["false_easting",0],PARAMETER["false_northing",0],UNIT["metre",1],AUTHORITY["EPSG","3857"]]',
        encoding="utf-8",
    )



def geotiff_extratags_epsg3857(width: int, height: int, bounds_3857: Tuple[float, float, float, float]):
    """Return internal GeoTIFF tags for EPSG:3857 Web Mercator.

    QGIS 4 / GDAL should read these tags directly from the TIFF/BigTIFF, so the
    output remains georeferenced even when .tfw/.prj sidecar files are moved or
    ignored. The world file is still written as an extra compatibility fallback.
    """
    west, south, east, north = bounds_3857
    px_w = (east - west) / float(width)
    px_h = (north - south) / float(height)

    # GeoTIFF key directory, version 1.1.0, 5 keys:
    # GTModelTypeGeoKey=Projected, GTRasterTypeGeoKey=PixelIsArea,
    # GeographicTypeGeoKey=WGS84, ProjectedCSTypeGeoKey=EPSG:3857,
    # ProjLinearUnitsGeoKey=metre.
    geokeys = (
        1, 1, 0, 5,
        1024, 0, 1, 1,
        1025, 0, 1, 1,
        2048, 0, 1, 4326,
        3072, 0, 1, 3857,
        3076, 0, 1, 9001,
    )
    return [
        (33550, "d", 3, (px_w, px_h, 0.0), False),
        (33922, "d", 6, (0.0, 0.0, 0.0, west, north, 0.0), False),
        (34735, "H", len(geokeys), geokeys, False),
    ]

def save_tile_as_tif(data: Optional[bytes], out_path: Path, z: int, x: int, y: int) -> None:
    # Schreibt genau eine erzeugte Kachel sofort als TIFF.
    # Vorhandene TIFF-Tiles werden nicht erneut geschrieben.
    if out_path.exists() and out_path.stat().st_size > 100:
        return
    out_path.parent.mkdir(parents=True, exist_ok=True)
    im = decode_tile(data)
    tmp = out_path.with_suffix(".tmp.tif")
    im.save(tmp, format="TIFF", compression="tiff_deflate")
    os.replace(tmp, out_path)
    write_worldfile_and_prj(out_path, TILE_SIZE, TILE_SIZE, tile_webmercator_bounds(x, y, z))


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


def open_direct_bigtiff(cfg: StitchConfig, width: int, height: int, bounds_3857: Tuple[float, float, float, float], log_cb):
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
        tifffile.imwrite(
            str(cfg.output_file),
            data=None,
            shape=(height, width, 3),
            dtype="uint8",
            bigtiff=bigtiff,
            photometric="rgb",
            metadata=None,
            extratags=geotiff_extratags_epsg3857(width, height, bounds_3857),
        )
        mem = tifffile.memmap(str(cfg.output_file), mode="r+")
        log_cb(f"Direct GeoTIFF/BigTIFF writer opened with internal EPSG:3857 tags: {cfg.output_file}")
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

    mosaic_bounds_3857 = mosaic_webmercator_bounds(x_min, y_min, x_max, y_max, cfg.z)
    direct_mem, direct_kind = open_direct_bigtiff(cfg, width, height, mosaic_bounds_3857, log_cb)
    fallback_mosaic = None
    output_upper = cfg.output_format.upper()
    if direct_mem is not None:
        try:
            write_worldfile_and_prj(cfg.output_file, width, height, mosaic_bounds_3857)
            log_cb(f"Georeferencing sidecars written: {cfg.output_file.with_suffix('.tfw')} and {cfg.output_file.with_suffix('.prj')} (EPSG:3857)")
        except Exception as exc:
            log_cb(f"Georeferencing sidecar write failed: {exc}")
    elif output_upper == "TIFF":
        estimated = width * height * 3
        if estimated <= MAX_PIL_FALLBACK_TIFF_BYTES:
            cfg.output_file.parent.mkdir(parents=True, exist_ok=True)
            fallback_mosaic = Image.new("RGB", (width, height), (255, 255, 255))
            log_cb(f"Pillow TIFF fallback active: {cfg.output_file}")
            log_cb("This is used because tifffile is not available in this QGIS Python environment.")
            try:
                write_worldfile_and_prj(cfg.output_file, width, height, mosaic_bounds_3857)
                log_cb(f"Georeferencing sidecars written: {cfg.output_file.with_suffix('.tfw')} and {cfg.output_file.with_suffix('.prj')} (EPSG:3857)")
            except Exception as exc:
                log_cb(f"Georeferencing sidecar write failed: {exc}")
        else:
            log_cb(f"Merged TIFF fallback disabled: estimated in-memory raster is {estimated/1_000_000_000:.2f} GB.")
            log_cb("Install tifffile in the QGIS Python environment or use a smaller area/lower zoom.")
    if output_upper in {"TILES", "KACHELN"}:
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
                        # Only skip completed SQLite tiles when no image output has to be written.
                        # For TIFF output we must still read cached tiles and paste/write them into the mosaic.
                        if db_tile_done(db, job.z, job.x, job.y) and output_upper in {"TILES", "KACHELN"} and not cfg.save_individual_tifs:
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
                                    save_tile_as_tif(data, safe_tile_tif_path(tile_tif_dir, job.z, job.x, job.y), job.z, job.x, job.y)
                                if direct_mem is not None:
                                    import numpy as np
                                    tile = decode_tile(data)
                                    r0 = job.row * TILE_SIZE
                                    c0 = job.col * TILE_SIZE
                                    direct_mem[r0:r0+TILE_SIZE, c0:c0+TILE_SIZE, :] = np.asarray(tile, dtype="uint8")
                                elif fallback_mosaic is not None:
                                    tile = decode_tile(data)
                                    r0 = job.row * TILE_SIZE
                                    c0 = job.col * TILE_SIZE
                                    fallback_mosaic.paste(tile, (c0, r0))
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

    if fallback_mosaic is not None:
        try:
            tmp = cfg.output_file.with_suffix(cfg.output_file.suffix + ".tmp")
            cfg.output_file.parent.mkdir(parents=True, exist_ok=True)
            fallback_mosaic.save(tmp, format="TIFF", compression="tiff_deflate")
            os.replace(tmp, cfg.output_file)
            log_cb(f"Finished Pillow TIFF output: {cfg.output_file}")
        finally:
            try:
                fallback_mosaic.close()
            except Exception:
                pass

    log_cb(f"Finished streaming. Downloaded/processed: {done:,}; skipped from SQLite: {skipped:,}; errors: {errors:,}")
    if output_upper == "TIFF":
        if direct_mem is not None:
            log_cb(f"Finished BigTIFF/direct output: {cfg.output_file}")
        elif fallback_mosaic is None:
            log_cb("Merged TIFF was not created. Install tifffile in QGIS Python or reduce the selected area/zoom so Pillow fallback can be used.")
        if cfg.output_file.exists():
            log_cb(f"TIFF saved: {cfg.output_file}")
        else:
            log_cb(f"TIFF file missing after run: {cfg.output_file}")
