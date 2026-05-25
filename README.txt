# Py Map Stitcher - NoniMapView Style

Simply download the .exe of Version 2 here:




https://github.com/tarekwasfy01/PyMapStitcher/releases/download/MapStitcher/PyMapStitcher_2.exe



![Overviev of PyMapStiticher2](https://github.com/tarekwasfy01/PyMapStitcher/blob/main/Screenshot%202026-05-25%20235310.png?raw=true)





Py Map Stitcher - NoniMapView Style

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
