Simply download the .exe here:


.exe setup:
https://tarekwasfy01.itch.io/pymapstitcher-planetary-version


Py Map Stitcher - NoniMapView Style

Windows start:
1. Run START_WINDOWS.bat
or:
   python py_map_stitcher.py

Installation for Python 3.8 / Anaconda:
   python -m pip install requests pillow numpy==1.22.4 tifffile==2023.7.10

New in this version:
- Every successfully downloaded/generated tile is immediately saved as an individual TIFF file.
- The folder is created next to the output file, e.g. map_output_single_tiff_tiles_z18.
- The final TIFF file is written tile-by-tile: only one 256x256 tile is loaded at a time.
- Existing raw tiles and TIFF tiles are skipped automatically.
- Selection is limited to the currently visible map area.
- File names contain z/x/y, e.g. z18_x123_y456.tif.

Note: Only use tile servers where downloading/stitching is allowed.
