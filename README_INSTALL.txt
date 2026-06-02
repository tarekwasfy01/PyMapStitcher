Py Map Stitcher QGIS Plugin
===========================

Installation:
1. Open QGIS.
2. Plugins > Manage and Install Plugins > Install from ZIP.
3. Select the ZIP file.
4. Enable "Py Map Stitcher".
5. Open it from the toolbar icon or from Extensions/Plugins > Py Map Stitcher.

Notes:
- This plugin is based on the supplied standalone script and keeps its tile download/stitch workflow.
- It does not import/use GDAL, rasterio, geopandas, or QGIS processing for stitching.
- Optional Python packages are still required for the same actions as the script: requests, Pillow, tifffile, numpy.
- Output georeferencing is written through .tfw + .prj sidecar files, exactly like the old working standalone script.
- Use tile servers only where downloading/stitching is permitted.

Update in this build:
- The preview part was rebuilt completely.
- Preview no longer uses QtWebEngine, Leaflet, external JavaScript, or the old Python tile-preview widget.
- Preview is now a native QGIS QgsMapCanvas with a simple XYZ WebMap layer.
- Preview uses fixed Google Hybrid URL: https://mt1.google.com/vt/lyrs=y&x={x}&y={y}&z={z}&hl=de
- The provider/URL selection on the left still controls the downloader only.
- The plugin entry is added to the normal Extensions/Plugins menu, not only the Raster menu.

QGIS plugin repository/upload checklist:
- ZIP top level is the plugin folder: PyMapStitcher/
- metadata.txt and __init__.py are directly inside the top-level plugin folder.
- No __pycache__ or compiled Python files are included.
- Description is in English and explains the purpose clearly.
- homepage/tracker/repository metadata links are public.
- Tested target: QGIS 3.22+ API layout.
- Test the ZIP in the target QGIS version before upload.
- The ZIP and repository should contain the same source code.
