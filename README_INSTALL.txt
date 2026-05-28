# Py Map Stitcher QGIS 4 Plugin

QGIS 4 build of Py Map Stitcher. It downloads XYZ tiles, stitches them to TIFF/BigTIFF where possible, and writes EPSG:3857 georeferencing sidecars (`.tfw` and `.prj`) so QGIS can place the raster.

## QGIS 4 notes

- `metadata.txt` is set to `qgisMinimumVersion=4.0` and `qgisMaximumVersion=4.99`.
- The plugin uses `qgis.PyQt` imports and Qt enum compatibility helpers for PyQt6/QGIS 4.
- The preview uses a native `QgsMapCanvas` XYZ layer, not QtWebEngine.
- Required Python packages in the QGIS Python environment: `requests`, `pillow`; optional for direct BigTIFF streaming: `tifffile`, `numpy`.

## Usage

1. Install the ZIP through QGIS Plugin Manager.
2. Open **Py Map Stitcher QGIS 4** from the plugin menu or toolbar.
3. Select an area in the preview or with **Select area in QGIS map**.
4. Choose zoom, output file, thread count and start the download.

Only download tiles from services where this is permitted.
