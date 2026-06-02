# -*- coding: utf-8 -*-
import io
import math
import os
import sys
import threading
from pathlib import Path

from qgis.PyQt.QtCore import Qt, QPoint, QRect, QThread, pyqtSignal, QObject, pyqtSlot, QUrl
from qgis.PyQt.QtGui import QPixmap, QPainter, QColor, QPen, QImage
from qgis.PyQt.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QProgressBar,
    QSizePolicy,
    QSpinBox,
    QTextEdit,
    QVBoxLayout,
    QWidget,
 )

from qgis.core import (
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsProject,
    QgsRasterLayer,
    QgsRectangle,
    QgsPointXY,
    QgsWkbTypes,
)
from qgis.gui import QgsMapCanvas, QgsRubberBand, QgsMapTool

from .core import (
    DEFAULT_CHUNK_SIZE,
    MAP_PRESETS,
    TILE_SIZE,
    USER_AGENT,
    StitchConfig,
    expand_url,
    project_tiles_dir,
    safe_cache_path,
    sqlite_path_for,
    stitch_tiles,
    tile_bounds_for_bbox,
    tile_to_lonlat,
    clamp_lat,
)

try:
    import requests
except Exception:
    requests = None

# No QtWebEngine dependency: preview uses native QGIS QgsMapCanvas + XYZ raster layer only.

class StitchWorker(QThread):
    progress = pyqtSignal(int, int, str)
    log = pyqtSignal(str)
    finished_ok = pyqtSignal()
    failed = pyqtSignal(str)

    def __init__(self, cfg, stop_event, parent=None):
        super().__init__(parent)
        self.cfg = cfg
        self.stop_event = stop_event

    def run(self):
        try:
            stitch_tiles(
                self.cfg,
                lambda done, total, phase: self.progress.emit(int(done), int(total), str(phase)),
                lambda msg: self.log.emit(str(msg)),
                self.stop_event,
            )
            self.finished_ok.emit()
        except Exception as exc:
            self.failed.emit(str(exc))


class PreviewLoader(QThread):
    tile_ready = pyqtSignal(int, int, QPixmap)
    status = pyqtSignal(str)

    def __init__(self, template, z, jobs, left, top, cache_root, generation, parent=None):
        super().__init__(parent)
        self.template = template
        self.z = z
        self.jobs = jobs
        self.left = left
        self.top = top
        self.cache_root = Path(cache_root)
        self.generation = generation
        self._cancel = False

    def cancel(self):
        self._cancel = True

    def run(self):
        if requests is None:
            self.status.emit("Preview unavailable: requests is not installed.")
            return
        for x, y in self.jobs:
            if self._cancel:
                return
            pix = QPixmap()
            p = safe_cache_path(self.cache_root / "_preview", self.z, x, y)
            try:
                data = None
                if p.exists() and p.stat().st_size > 100:
                    data = p.read_bytes()
                else:
                    url = expand_url(self.template, x, y, self.z)
                    r = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=7)
                    r.raise_for_status()
                    data = r.content
                    p.parent.mkdir(parents=True, exist_ok=True)
                    tmp = p.with_suffix(".tmp")
                    tmp.write_bytes(data)
                    os.replace(tmp, p)
                pix.loadFromData(data)
            except Exception:
                img = QImage(TILE_SIZE, TILE_SIZE, QImage.Format_RGB32)
                img.fill(QColor(255, 255, 255))
                pix = QPixmap.fromImage(img)
            sx = int(x * TILE_SIZE - self.left)
            sy = int(y * TILE_SIZE - self.top)
            self.tile_ready.emit(sx, sy, pix.scaled(TILE_SIZE, TILE_SIZE, Qt.IgnoreAspectRatio, Qt.SmoothTransformation))
        self.status.emit("Preview loaded")


class MapPreview(QWidget):
    selectionChanged = pyqtSignal(float, float, float, float)
    log = pyqtSignal(str)
    status = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(360)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.template = MAP_PRESETS["Google Satellite"]["url"]
        self.cache_root = str(Path.home() / "py_map_stitcher_cache")
        self.view_z = 3
        self.center_lon = 10.0
        self.center_lat = 51.0
        self.tiles = []
        self.loader = None
        self.generation = 0
        self._drag_start = None
        self._drag_center = None
        self._select_start = None
        self._select_rect = None
        self.setMouseTracking(True)

    def setTemplate(self, template):
        self.template = template
        self.refresh()

    def setCacheRoot(self, cache_root):
        self.cache_root = cache_root

    def _world_px(self, lon, lat, z=None):
        z = self.view_z if z is None else z
        lat = clamp_lat(lat)
        n = (2 ** z) * TILE_SIZE
        x = (lon + 180.0) / 360.0 * n
        lat_rad = math.radians(lat)
        y = (1.0 - math.asinh(math.tan(lat_rad)) / math.pi) / 2.0 * n
        return x, y

    def _lonlat_from_world_px(self, px, py, z=None):
        z = self.view_z if z is None else z
        return tile_to_lonlat(px / TILE_SIZE, py / TILE_SIZE, z)

    def _screen_to_lonlat(self, sx, sy):
        cw = max(1, self.width())
        ch = max(1, self.height())
        cx, cy = self._world_px(self.center_lon, self.center_lat)
        return self._lonlat_from_world_px(cx + sx - cw / 2, cy + sy - ch / 2)

    def _visible_tiles(self):
        cw = max(1, self.width())
        ch = max(1, self.height())
        cx, cy = self._world_px(self.center_lon, self.center_lat)
        left = cx - cw / 2
        top = cy - ch / 2
        ntiles = 2 ** self.view_z
        x0 = max(0, int(math.floor(left / TILE_SIZE)) - 1)
        y0 = max(0, int(math.floor(top / TILE_SIZE)) - 1)
        x1 = min(ntiles - 1, int(math.floor((left + cw) / TILE_SIZE)) + 1)
        y1 = min(ntiles - 1, int(math.floor((top + ch) / TILE_SIZE)) + 1)
        return x0, y0, x1, y1, left, top

    def refresh(self):
        self.generation += 1
        if self.loader and self.loader.isRunning():
            self.loader.cancel()
        self.tiles = []
        self.update()
        try:
            x0, y0, x1, y1, left, top = self._visible_tiles()
        except Exception:
            return
        self.status.emit(f"Zoom {self.view_z} | visible x {x0}-{x1}, y {y0}-{y1} | left drag move | right/Shift-left select")
        jobs = [(x, y) for y in range(y0, y1 + 1) for x in range(x0, x1 + 1)]
        self.loader = PreviewLoader(self.template, self.view_z, jobs, left, top, self.cache_root, self.generation, self)
        self.loader.tile_ready.connect(self._add_tile)
        self.loader.status.connect(self.status.emit)
        self.loader.start()

    def _add_tile(self, sx, sy, pix):
        self.tiles.append((sx, sy, pix))
        self.update()

    def zoom(self, delta, pos=None):
        old_z = self.view_z
        new_z = max(0, min(22, self.view_z + delta))
        if new_z == old_z:
            return
        sx = self.width() / 2 if pos is None else pos.x()
        sy = self.height() / 2 if pos is None else pos.y()
        anchor_lon, anchor_lat = self._screen_to_lonlat(sx, sy)
        self.view_z = new_z
        ax, ay = self._world_px(anchor_lon, anchor_lat, new_z)
        cx = ax - sx + self.width() / 2
        cy = ay - sy + self.height() / 2
        self.center_lon, self.center_lat = self._lonlat_from_world_px(cx, cy, new_z)
        self.refresh()

    def paintEvent(self, event):
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(232, 238, 245))
        for sx, sy, pix in self.tiles:
            p.drawPixmap(sx, sy, pix)
        if not self.tiles:
            p.setPen(QColor(50, 50, 50))
            p.drawText(12, 24, "Preview loading visible tiles...")
        if self._select_rect is not None:
            pen = QPen(QColor(220, 0, 0), 2)
            p.setPen(pen)
            p.drawRect(self._select_rect.normalized())
        p.end()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.refresh()

    def wheelEvent(self, event):
        self.zoom(1 if event.angleDelta().y() > 0 else -1, event.position().toPoint())

    def mousePressEvent(self, event):
        if event.button() == Qt.RightButton or (event.button() == Qt.LeftButton and event.modifiers() & Qt.ShiftModifier):
            self._select_start = event.pos()
            self._select_rect = QRect(event.pos(), event.pos())
            self.update()
        elif event.button() == Qt.LeftButton:
            self._drag_start = event.pos()
            self._drag_center = (self.center_lon, self.center_lat)
            self.setCursor(Qt.ClosedHandCursor)

    def mouseMoveEvent(self, event):
        if self._select_start is not None:
            self._select_rect = QRect(self._select_start, event.pos()).intersected(self.rect())
            self.update()
        elif self._drag_start is not None and self._drag_center is not None:
            lon0, lat0 = self._drag_center
            cx, cy = self._world_px(lon0, lat0)
            dx = event.pos().x() - self._drag_start.x()
            dy = event.pos().y() - self._drag_start.y()
            self.center_lon, self.center_lat = self._lonlat_from_world_px(cx - dx, cy - dy)
            self.refresh()

    def mouseReleaseEvent(self, event):
        if self._select_start is not None:
            rect = QRect(self._select_start, event.pos()).normalized().intersected(self.rect())
            self._select_start = None
            self._select_rect = rect
            self.update()
            if rect.width() > 5 and rect.height() > 5:
                lon_a, lat_a = self._screen_to_lonlat(rect.left(), rect.top())
                lon_b, lat_b = self._screen_to_lonlat(rect.right(), rect.bottom())
                self.selectionChanged.emit(min(lat_a, lat_b), min(lon_a, lon_b), max(lat_a, lat_b), max(lon_a, lon_b))
                self.log.emit("Selection imported from map preview.")
        self._drag_start = None
        self._drag_center = None
        self.setCursor(Qt.ArrowCursor)


class WebMapBridge(QObject):
    bboxSelected = pyqtSignal(float, float, float, float)
    log = pyqtSignal(str)
    viewChanged = pyqtSignal(float, float, int)

    @pyqtSlot(float, float, float, float)
    def select_bbox(self, south, west, north, east):
        self.bboxSelected.emit(float(south), float(west), float(north), float(east))
        self.log.emit("Selection imported from web map preview.")

    @pyqtSlot(float, float, int)
    def update_view(self, lat, lon, zoom):
        self.viewChanged.emit(float(lat), float(lon), int(zoom))


class WebMapPreview(QWidget):
    selectionChanged = pyqtSignal(float, float, float, float)
    log = pyqtSignal(str)
    status = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(360)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.view_z = 5
        self.center_lon = 10.0
        self.center_lat = 51.0
        self._loaded = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.web = QWebEngineView(self)
        layout.addWidget(self.web)

        self.bridge = WebMapBridge(self)
        self.channel = QWebChannel(self.web.page())
        self.channel.registerObject("pyBridge", self.bridge)
        self.web.page().setWebChannel(self.channel)
        self.bridge.bboxSelected.connect(self.selectionChanged.emit)
        self.bridge.log.connect(self.log.emit)
        self.bridge.viewChanged.connect(self._on_view_changed)
        self.web.loadFinished.connect(self._on_load_finished)
        self.refresh()

    def _on_view_changed(self, lat, lon, zoom):
        self.center_lat = float(lat)
        self.center_lon = float(lon)
        self.view_z = int(zoom)
        self.status.emit(f"Simple WebMap Zoom {self.view_z} | drag: move | right-drag or Shift+left-drag: select area")

    def setTemplate(self, template):
        # Preview is deliberately independent from the download URL/provider.
        # The selected provider still controls the downloader only.
        return

    def setCacheRoot(self, cache_root):
        return

    def refresh(self):
        self._loaded = False
        self.status.emit("Loading simple Google Hybrid WebMap preview...")
        self.web.setHtml(self._html(), QUrl("https://local.pymapstitcher/"))

    def zoom(self, delta, pos=None):
        if self._loaded:
            self.web.page().runJavaScript("map.zoomIn();" if delta > 0 else "map.zoomOut();")

    def _on_load_finished(self, ok):
        self._loaded = bool(ok)
        self.status.emit("Simple WebMap preview loaded" if ok else "Simple WebMap preview failed to load")

    def _html(self):
        lat = float(self.center_lat)
        lon = float(self.center_lon)
        z = int(self.view_z)
        # Intentionally minimal: one Leaflet map, one fixed tile layer, one selection rectangle.
        return f"""<!doctype html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<script src="qrc:///qtwebchannel/qwebchannel.js"></script>
<style>
  html, body, #map {{ width:100%; height:100%; margin:0; padding:0; overflow:hidden; background:#e8eef5; }}
  .hint {{ position:absolute; z-index:1000; left:10px; top:10px; padding:6px 8px; border-radius:4px; background:rgba(255,255,255,.90); font:12px Arial, sans-serif; color:#111; }}
</style>
</head>
<body>
<div id="map"></div>
<div class="hint">Simple WebMap preview: Google Hybrid. Drag to move. Right-drag or Shift+left-drag selects the download area.</div>
<script>
let pyBridge = null;
new QWebChannel(qt.webChannelTransport, function(channel) {{ pyBridge = channel.objects.pyBridge; }});

let map = L.map('map', {{
  zoomControl: true,
  attributionControl: false,
  preferCanvas: true,
  worldCopyJump: false
}}).setView([{lat}, {lon}], {z});

L.tileLayer('https://mt1.google.com/vt/lyrs=y&x={{x}}&y={{y}}&z={{z}}&hl=de', {{
  minZoom: 0,
  maxZoom: 22,
  tileSize: 256,
  noWrap: true,
  crossOrigin: false,
  errorTileUrl: ''
}}).addTo(map);

map.on('moveend zoomend', function() {{
  if (pyBridge) {{
    let c = map.getCenter();
    pyBridge.update_view(c.lat, c.lng, map.getZoom());
  }}
}});

let selecting = false;
let startLatLng = null;
let rect = null;
map.getContainer().addEventListener('contextmenu', function(e) {{ e.preventDefault(); }});

map.on('mousedown', function(e) {{
  const oe = e.originalEvent;
  if (oe.button === 2 || oe.shiftKey) {{
    selecting = true;
    startLatLng = e.latlng;
    map.dragging.disable();
    if (rect) map.removeLayer(rect);
    rect = L.rectangle([startLatLng, startLatLng], {{color: 'red', weight: 2, fill: false}}).addTo(map);
    oe.preventDefault();
    oe.stopPropagation();
  }}
}});

map.on('mousemove', function(e) {{
  if (selecting && rect) rect.setBounds(L.latLngBounds(startLatLng, e.latlng));
}});

map.on('mouseup', function(e) {{
  if (!selecting) return;
  selecting = false;
  map.dragging.enable();
  if (!rect) return;
  let b = rect.getBounds();
  let south = b.getSouth(), west = b.getWest(), north = b.getNorth(), east = b.getEast();
  if (Math.abs(north - south) > 0.000001 && Math.abs(east - west) > 0.000001 && pyBridge) {{
    pyBridge.select_bbox(south, west, north, east);
  }}
}});
</script>
</body>
</html>"""

class NativeWebMapPreview(QgsMapCanvas):
    """Simple native QGIS XYZ web map preview.

    This avoids QtWebEngine/Leaflet completely. The preview is a normal QGIS map
    canvas with one XYZ raster layer. The downloader still uses the URL template
    selected on the left; this preview intentionally stays simple and fixed.
    """

    selectionChanged = pyqtSignal(float, float, float, float)
    log = pyqtSignal(str)
    status = pyqtSignal(str)

    GOOGLE_HYBRID_XYZ = "https://mt1.google.com/vt/lyrs=y&x={x}&y={y}&z={z}&hl=de"

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(360)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.setCanvasColor(QColor(232, 238, 245))
        self.setDestinationCrs(QgsCoordinateReferenceSystem("EPSG:3857"))
        self.setWheelFactor(2.0)

        self.view_z = 5
        self.center_lon = 10.0
        self.center_lat = 51.0
        self._drag_start = None
        self._drag_extent = None
        self._selecting = False
        self._select_start = None
        self._rubber = QgsRubberBand(self, QgsWkbTypes.PolygonGeometry)
        self._rubber.setColor(QColor(220, 0, 0, 80))
        self._rubber.setStrokeColor(QColor(220, 0, 0))
        self._rubber.setWidth(2)

        self._layer = None
        self.refresh()

    def setTemplate(self, template):
        # The preview is deliberately not tied to the download template.
        # This keeps the preview reliable and simple.
        return

    def setCacheRoot(self, cache_root):
        return

    def _make_xyz_layer(self):
        # QGIS XYZ provider needs inner URL query separators encoded, but the
        # tile placeholders {x}, {y}, {z} must stay literal. Encoding the braces
        # makes QGIS request %7Bx%7D instead of real tile coordinates, which
        # results in an empty preview.
        url = "https://mt1.google.com/vt/lyrs%3Dy%26x%3D{x}%26y%3D{y}%26z%3D{z}%26hl%3Dde"
        uri = f"type=xyz&url={url}&zmin=0&zmax=22"
        layer = QgsRasterLayer(uri, "Google Hybrid Preview", "wms")
        return layer

    def refresh(self):
        self._rubber.reset(QgsWkbTypes.PolygonGeometry)
        self._layer = self._make_xyz_layer()
        if not self._layer.isValid():
            self.setLayers([])
            self.status.emit("Native QGIS WebMap preview failed: XYZ layer is invalid")
            self.log.emit("Preview error: Google Hybrid XYZ layer could not be created.")
            return
        self.setLayers([self._layer])
        # Always set an initial useful extent. Some QGIS versions report a finite
        # default extent even though no map has been drawn yet.
        self._set_center_zoom(self.center_lon, self.center_lat, self.view_z)
        self.status.emit("Native QGIS WebMap preview: Google Hybrid | left drag move | right/Shift-left select area")

    def _lonlat_to_3857(self, lon, lat):
        src = QgsCoordinateReferenceSystem("EPSG:4326")
        dst = QgsCoordinateReferenceSystem("EPSG:3857")
        tr = QgsCoordinateTransform(src, dst, QgsProject.instance())
        return tr.transform(QgsPointXY(float(lon), float(lat)))

    def _3857_to_lonlat(self, pt):
        src = QgsCoordinateReferenceSystem("EPSG:3857")
        dst = QgsCoordinateReferenceSystem("EPSG:4326")
        tr = QgsCoordinateTransform(src, dst, QgsProject.instance())
        return tr.transform(QgsPointXY(float(pt.x()), float(pt.y())))

    def _set_center_zoom(self, lon, lat, z):
        center = self._lonlat_to_3857(lon, lat)
        meters_per_pixel = 156543.03392804097 / (2 ** int(z))
        width_m = max(1, self.width()) * meters_per_pixel
        height_m = max(1, self.height()) * meters_per_pixel
        rect = QgsRectangle(
            center.x() - width_m / 2,
            center.y() - height_m / 2,
            center.x() + width_m / 2,
            center.y() + height_m / 2,
        )
        self.setExtent(rect)
        self.refreshAllLayers()

    def zoom(self, delta, pos=None):
        if delta > 0:
            self.zoomIn()
        else:
            self.zoomOut()
        self.status.emit("Native QGIS WebMap preview: Google Hybrid")

    def _to_map_point(self, event):
        return self.getCoordinateTransform().toMapCoordinates(event.pos().x(), event.pos().y())

    def _update_rubber(self, p1, p2):
        self._rubber.reset(QgsWkbTypes.PolygonGeometry)
        rect = QgsRectangle(p1, p2)
        self._rubber.addPoint(QgsPointXY(rect.xMinimum(), rect.yMinimum()), False)
        self._rubber.addPoint(QgsPointXY(rect.xMinimum(), rect.yMaximum()), False)
        self._rubber.addPoint(QgsPointXY(rect.xMaximum(), rect.yMaximum()), False)
        self._rubber.addPoint(QgsPointXY(rect.xMaximum(), rect.yMinimum()), False)
        self._rubber.addPoint(QgsPointXY(rect.xMinimum(), rect.yMinimum()), True)
        self._rubber.show()

    def mousePressEvent(self, event):
        if event.button() == Qt.RightButton or (event.button() == Qt.LeftButton and event.modifiers() & Qt.ShiftModifier):
            self._selecting = True
            self._select_start = self._to_map_point(event)
            self._rubber.reset(QgsWkbTypes.PolygonGeometry)
            event.accept()
            return
        if event.button() == Qt.LeftButton:
            self._drag_start = event.pos()
            self._drag_extent = QgsRectangle(self.extent())
            self.setCursor(Qt.ClosedHandCursor)
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._selecting and self._select_start is not None:
            self._update_rubber(self._select_start, self._to_map_point(event))
            event.accept()
            return
        if self._drag_start is not None and self._drag_extent is not None:
            start_map = self.getCoordinateTransform().toMapCoordinates(self._drag_start.x(), self._drag_start.y())
            now_map = self._to_map_point(event)
            dx = start_map.x() - now_map.x()
            dy = start_map.y() - now_map.y()
            rect = QgsRectangle(self._drag_extent)
            rect.setXMinimum(self._drag_extent.xMinimum() + dx)
            rect.setXMaximum(self._drag_extent.xMaximum() + dx)
            rect.setYMinimum(self._drag_extent.yMinimum() + dy)
            rect.setYMaximum(self._drag_extent.yMaximum() + dy)
            self.setExtent(rect)
            self.refreshAllLayers()
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if self._selecting and self._select_start is not None:
            end = self._to_map_point(event)
            self._update_rubber(self._select_start, end)
            rect = QgsRectangle(self._select_start, end)
            self._selecting = False
            self._select_start = None
            if rect.width() > 0 and rect.height() > 0:
                sw = self._3857_to_lonlat(QgsPointXY(rect.xMinimum(), rect.yMinimum()))
                ne = self._3857_to_lonlat(QgsPointXY(rect.xMaximum(), rect.yMaximum()))
                south = min(sw.y(), ne.y())
                north = max(sw.y(), ne.y())
                west = min(sw.x(), ne.x())
                east = max(sw.x(), ne.x())
                self.selectionChanged.emit(south, west, north, east)
                self.log.emit("Selection imported from native QGIS WebMap preview.")
            event.accept()
            return
        self._drag_start = None
        self._drag_extent = None
        self.setCursor(Qt.ArrowCursor)
        super().mouseReleaseEvent(event)



class QgisCanvasBBoxSelectTool(QgsMapTool):
    """Map tool for selecting the download bbox directly in the main QGIS map."""

    bboxSelected = pyqtSignal(float, float, float, float)  # south, west, north, east in EPSG:4326
    log = pyqtSignal(str)

    def __init__(self, canvas, parent=None):
        super().__init__(canvas)
        self.canvas = canvas
        self._start = None
        self._rubber = QgsRubberBand(canvas, QgsWkbTypes.PolygonGeometry)
        self._rubber.setColor(QColor(0, 180, 255, 60))
        self._rubber.setStrokeColor(QColor(0, 120, 255))
        self._rubber.setWidth(2)

    def activate(self):
        super().activate()
        try:
            self.canvas.setCursor(Qt.CrossCursor)
        except Exception:
            pass
        self._rubber.reset(QgsWkbTypes.PolygonGeometry)
        self.log.emit("QGIS map selection active: drag a rectangle in the main QGIS map.")

    def deactivate(self):
        self._rubber.reset(QgsWkbTypes.PolygonGeometry)
        try:
            self.canvas.unsetCursor()
        except Exception:
            pass
        super().deactivate()

    def _to_map_point(self, event):
        return self.canvas.getCoordinateTransform().toMapCoordinates(event.pos().x(), event.pos().y())

    def _update_rubber(self, p1, p2):
        self._rubber.reset(QgsWkbTypes.PolygonGeometry)
        rect = QgsRectangle(p1, p2)
        self._rubber.addPoint(QgsPointXY(rect.xMinimum(), rect.yMinimum()), False)
        self._rubber.addPoint(QgsPointXY(rect.xMinimum(), rect.yMaximum()), False)
        self._rubber.addPoint(QgsPointXY(rect.xMaximum(), rect.yMaximum()), False)
        self._rubber.addPoint(QgsPointXY(rect.xMaximum(), rect.yMinimum()), False)
        self._rubber.addPoint(QgsPointXY(rect.xMinimum(), rect.yMinimum()), True)
        self._rubber.show()

    def canvasPressEvent(self, event):
        if event.button() != Qt.LeftButton:
            return
        self._start = self._to_map_point(event)
        self._rubber.reset(QgsWkbTypes.PolygonGeometry)

    def canvasMoveEvent(self, event):
        if self._start is None:
            return
        self._update_rubber(self._start, self._to_map_point(event))

    def canvasReleaseEvent(self, event):
        if self._start is None or event.button() != Qt.LeftButton:
            return
        end = self._to_map_point(event)
        rect = QgsRectangle(self._start, end)
        self._start = None
        if rect.width() <= 0 or rect.height() <= 0:
            self.log.emit("QGIS map selection ignored: rectangle too small.")
            return

        try:
            src = self.canvas.mapSettings().destinationCrs()
            dst = QgsCoordinateReferenceSystem("EPSG:4326")
            tr = QgsCoordinateTransform(src, dst, QgsProject.instance())
            p1 = tr.transform(QgsPointXY(rect.xMinimum(), rect.yMinimum()))
            p2 = tr.transform(QgsPointXY(rect.xMaximum(), rect.yMaximum()))
            south = min(p1.y(), p2.y())
            north = max(p1.y(), p2.y())
            west = min(p1.x(), p2.x())
            east = max(p1.x(), p2.x())
            self.bboxSelected.emit(south, west, north, east)
            self.log.emit("Selection imported from main QGIS map.")
        except Exception as exc:
            self.log.emit("QGIS map selection failed: " + str(exc))


class PyMapStitcherDialog(QDialog):
    def __init__(self, iface, parent=None):
        super().__init__(parent or iface.mainWindow())
        self.iface = iface
        self.stop_event = threading.Event()
        self.worker = None
        self._previous_map_tool = None
        self._qgis_select_tool = None
        self.setWindowTitle("Py Map Stitcher - QGIS Plugin")
        self.resize(1220, 760)
        self._build_ui()

    def _build_ui(self):
        root = QHBoxLayout(self)
        left_box = QGroupBox("Map / Download")
        left = QFormLayout(left_box)
        left.setFieldGrowthPolicy(QFormLayout.ExpandingFieldsGrow)
        root.addWidget(left_box, 0)

        logo = QLabel()
        pix = QPixmap(str(Path(__file__).resolve().parent / "icon.png"))
        logo.setPixmap(pix.scaled(96, 96, Qt.KeepAspectRatio, Qt.SmoothTransformation))
        logo.setAlignment(Qt.AlignCenter)
        left.addRow(logo)

        self.map_preset = QComboBox()
        self.map_preset.addItems(list(MAP_PRESETS.keys()))
        self.map_preset.setCurrentText("Google Satellite")
        left.addRow("Map Selection", self.map_preset)

        self.url = QLineEdit(MAP_PRESETS["Google Satellite"]["url"])
        left.addRow("URL Template", self.url)
        self.note = QLabel(MAP_PRESETS["Google Satellite"]["note"])
        self.note.setWordWrap(True)
        left.addRow(self.note)
        ph = QLabel("Placeholders: {z} {x} {y} {s} {rnd} {q}/{quadkey}; Noni: *GMX* *GMY* *ZM1* *RND*")
        ph.setWordWrap(True)
        left.addRow(ph)

        self.z = QSpinBox(); self.z.setRange(0, 22); self.z.setValue(18)
        self.min_lat = QLineEdit(""); self.min_lon = QLineEdit("")
        self.max_lat = QLineEdit(""); self.max_lon = QLineEdit("")
        self.workers = QSpinBox(); self.workers.setRange(1, 64); self.workers.setValue(8)
        self.rate = QSpinBox(); self.rate.setRange(0, 5000); self.rate.setSingleStep(10); self.rate.setValue(20)
        self.chunk_size = QSpinBox(); self.chunk_size.setRange(8, 1024); self.chunk_size.setSingleStep(8); self.chunk_size.setValue(DEFAULT_CHUNK_SIZE)
        self.save_tile_tifs = QCheckBox()
        self.outfmt = QComboBox(); self.outfmt.addItems(["TIFF", "TILES"])
        self.outfile = QLineEdit(str(Path.home() / "Desktop" / "map_output.tif"))
        self.cache = QLineEdit(str(Path.home() / "py_map_stitcher_cache"))
        self.raw_tiles_folder = QLineEdit(); self.raw_tiles_folder.setReadOnly(True)
        self.single_tiff_folder = QLineEdit(); self.single_tiff_folder.setReadOnly(True)

        left.addRow("Download Zoom", self.z)
        left.addRow("South / min lat", self.min_lat)
        left.addRow("West / min lon", self.min_lon)
        left.addRow("North / max lat", self.max_lat)
        left.addRow("East / max lon", self.max_lon)
        self.qgis_select_btn = QPushButton("Select area in QGIS map")
        self.qgis_select_btn.clicked.connect(self.enable_qgis_map_selection)
        left.addRow("QGIS canvas", self.qgis_select_btn)
        left.addRow("Download Threads", self.workers)
        left.addRow("Delay per Request ms", self.rate)
        left.addRow("Chunk size tiles", self.chunk_size)
        left.addRow("Save single TIFF tiles", self.save_tile_tifs)
        left.addRow("Output Format", self.outfmt)

        out_row = QHBoxLayout(); out_row.addWidget(self.outfile)
        out_btn = QPushButton("…"); out_btn.clicked.connect(self.pick_output); out_row.addWidget(out_btn)
        left.addRow("Output File", out_row)
        cache_row = QHBoxLayout(); cache_row.addWidget(self.cache)
        cache_btn = QPushButton("…"); cache_btn.clicked.connect(self.pick_cache); cache_row.addWidget(cache_btn)
        left.addRow("Cache", cache_row)

        raw_row = QHBoxLayout(); raw_row.addWidget(self.raw_tiles_folder)
        raw_btn = QPushButton("Open"); raw_btn.clicked.connect(lambda: self.open_folder(self._raw_tiles_dir())); raw_row.addWidget(raw_btn)
        left.addRow("Raw Tiles Folder", raw_row)
        tif_row = QHBoxLayout(); tif_row.addWidget(self.single_tiff_folder)
        tif_btn = QPushButton("Open"); tif_btn.clicked.connect(lambda: self.open_folder(self._single_tiff_dir())); tif_row.addWidget(tif_btn)
        left.addRow("Single TIFF Folder", tif_row)

        btn_row = QHBoxLayout()
        calc_btn = QPushButton("Calculate"); calc_btn.clicked.connect(self.calculate); btn_row.addWidget(calc_btn)
        self.start_btn = QPushButton("Start"); self.start_btn.clicked.connect(self.start); btn_row.addWidget(self.start_btn)
        stop_btn = QPushButton("Stop"); stop_btn.clicked.connect(self.stop); btn_row.addWidget(stop_btn)
        left.addRow(btn_row)

        warning = QLabel("Only use servers where downloading/stitching is allowed. Google/Bing/OSM may restrict bulk downloads.")
        warning.setWordWrap(True)
        left.addRow(warning)

        right = QVBoxLayout()
        root.addLayout(right, 1)
        map_box = QGroupBox("Native QGIS WebMap Preview / Selection")
        map_layout = QVBoxLayout(map_box)
        controls = QHBoxLayout()
        reload_btn = QPushButton("Reload"); reload_btn.clicked.connect(lambda: self.preview.refresh()); controls.addWidget(reload_btn)
        plus = QPushButton("+"); plus.setMaximumWidth(36); plus.clicked.connect(lambda: self.preview.zoom(1)); controls.addWidget(plus)
        minus = QPushButton("-"); minus.setMaximumWidth(36); minus.clicked.connect(lambda: self.preview.zoom(-1)); controls.addWidget(minus)
        self.preview_status = QLabel("Native QGIS WebMap: Google Hybrid | left drag move | mouse wheel zoom | right/Shift-left select area")
        controls.addWidget(self.preview_status, 1)
        map_layout.addLayout(controls)
        self.preview = NativeWebMapPreview()
        map_layout.addWidget(self.preview, 1)
        right.addWidget(map_box, 2)

        status_box = QGroupBox("Status / Log")
        status_layout = QVBoxLayout(status_box)
        self.progress = QProgressBar(); status_layout.addWidget(self.progress)
        self.status = QLabel("Ready"); status_layout.addWidget(self.status)
        self.log = QTextEdit(); self.log.setReadOnly(True); status_layout.addWidget(self.log, 1)
        right.addWidget(status_box, 1)

        self.map_preset.currentTextChanged.connect(self.on_preset_changed)
        self.url.textChanged.connect(lambda _=None: self.preview.setTemplate(self.url.text().strip()))
        self.cache.textChanged.connect(self._update_folder_fields)
        self.cache.textChanged.connect(lambda _=None: self.preview.setCacheRoot(self.cache.text()))
        self.z.valueChanged.connect(self._update_folder_fields)
        self.outfile.textChanged.connect(self._update_folder_fields)
        self.preview.selectionChanged.connect(self.set_bbox_from_preview)
        self.preview.log.connect(self._log)
        self.preview.status.connect(self.preview_status.setText)
        self._update_folder_fields()
        self.preview.refresh()

    def on_preset_changed(self, name):
        preset = MAP_PRESETS.get(name, MAP_PRESETS["Custom"])
        self.url.setText(preset["url"])
        self.note.setText(preset.get("note", ""))
        self.preview.setTemplate(preset["url"])

    def _raw_tiles_dir(self):
        try:
            return Path(self.cache.text()).expanduser() / str(int(self.z.value()))
        except Exception:
            return Path(self.cache.text()).expanduser()

    def _single_tiff_dir(self):
        try:
            out = Path(self.outfile.text()).expanduser()
            return out.parent / f"{out.stem or 'map_output'}_single_tiff_tiles_z{int(self.z.value())}"
        except Exception:
            return Path.home()

    def _update_folder_fields(self):
        self.raw_tiles_folder.setText(str(self._raw_tiles_dir()))
        self.single_tiff_folder.setText(str(self._single_tiff_dir()))
        self.preview.setCacheRoot(self.cache.text())

    def open_folder(self, path):
        path = Path(path).expanduser()
        path.mkdir(parents=True, exist_ok=True)
        try:
            if os.name == "nt":
                os.startfile(str(path))
            elif sys.platform == "darwin":
                import subprocess; subprocess.Popen(["open", str(path)])
            else:
                import subprocess; subprocess.Popen(["xdg-open", str(path)])
        except Exception as exc:
            QMessageBox.warning(self, "Open folder", str(exc))

    def pick_output(self):
        path, _ = QFileDialog.getSaveFileName(self, "Output File", self.outfile.text(), "TIFF (*.tif *.tiff);;All files (*.*)")
        if path:
            self.outfile.setText(path)
            self._update_folder_fields()

    def pick_cache(self):
        path = QFileDialog.getExistingDirectory(self, "Cache Folder", self.cache.text())
        if path:
            self.cache.setText(path)
            self._update_folder_fields()

    def set_bbox_from_preview(self, min_lat, min_lon, max_lat, max_lon):
        self.min_lat.setText(f"{min_lat:.8f}")
        self.min_lon.setText(f"{min_lon:.8f}")
        self.max_lat.setText(f"{max_lat:.8f}")
        self.max_lon.setText(f"{max_lon:.8f}")
        self.calculate()


    def enable_qgis_map_selection(self):
        canvas = self.iface.mapCanvas()
        if canvas is None:
            QMessageBox.warning(self, "QGIS map selection", "QGIS map canvas is not available.")
            return
        if self._qgis_select_tool is None:
            self._qgis_select_tool = QgisCanvasBBoxSelectTool(canvas, self)
            self._qgis_select_tool.bboxSelected.connect(self.set_bbox_from_qgis_canvas)
            self._qgis_select_tool.log.connect(self._log)
        self._previous_map_tool = canvas.mapTool()
        canvas.setMapTool(self._qgis_select_tool)
        self.status.setText("Drag a rectangle in the main QGIS map.")
        self._log("QGIS map selection active: drag a rectangle in the main QGIS map.")
        try:
            self.raise_()
        except Exception:
            pass

    def set_bbox_from_qgis_canvas(self, min_lat, min_lon, max_lat, max_lon):
        self.set_bbox_from_preview(min_lat, min_lon, max_lat, max_lon)
        self.status.setText("Selection imported from main QGIS map")
        try:
            canvas = self.iface.mapCanvas()
            if self._previous_map_tool is not None and canvas.mapTool() == self._qgis_select_tool:
                canvas.setMapTool(self._previous_map_tool)
        except Exception:
            pass

    def _config(self):
        out = Path(self.outfile.text()).expanduser()
        return StitchConfig(
            url_template=self.url.text().strip(),
            output_file=out,
            cache_dir=project_tiles_dir(out),
            z=int(self.z.value()),
            min_lat=float(self.min_lat.text().replace(",", ".")),
            min_lon=float(self.min_lon.text().replace(",", ".")),
            max_lat=float(self.max_lat.text().replace(",", ".")),
            max_lon=float(self.max_lon.text().replace(",", ".")),
            workers=int(self.workers.value()),
            rate_limit_ms=int(self.rate.value()),
            output_format=self.outfmt.currentText(),
            tile_tif_dir=out.parent / f"{out.stem or 'map_output'}_single_tiff_tiles_z{int(self.z.value())}",
            chunk_size=int(self.chunk_size.value()),
            use_sqlite=True,
            direct_bigtiff=True,
            save_individual_tifs=bool(self.save_tile_tifs.isChecked()),
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
            if self.save_tile_tifs.isChecked():
                self._log(f"Single TIFF folder: {self._single_tiff_dir()}")
            if self.outfmt.currentText().upper() == "TILES":
                self._log("Output: raw tiles + SQLite resume database, no merged file.")
            else:
                self._log("Output: direct BigTIFF streaming where possible; if too large, it continues as tile/cache mode.")
        except Exception as exc:
            QMessageBox.critical(self, "Error", str(exc))

    def start(self):
        if self.worker is not None and self.worker.isRunning():
            QMessageBox.information(self, "Running", "A job is already running.")
            return
        try:
            cfg = self._config()
        except Exception as exc:
            QMessageBox.critical(self, "Error", str(exc))
            return
        self._update_folder_fields()
        self._log(f"Raw tile folder: {self._raw_tiles_dir()}")
        self._log(f"SQLite resume database: {sqlite_path_for(cfg)}")
        if self.save_tile_tifs.isChecked():
            self._log(f"Single TIFF folder: {self._single_tiff_dir()}")
        self.stop_event.clear()
        self.progress.setValue(0)
        self.start_btn.setEnabled(False)
        self.worker = StitchWorker(cfg, self.stop_event, self)
        self.worker.progress.connect(self._on_progress)
        self.worker.log.connect(self._log)
        self.worker.finished_ok.connect(self._on_finished)
        self.worker.failed.connect(self._on_failed)
        self.worker.start()

    def stop(self):
        self.stop_event.set()
        self._log("Stop requested. Download state is preserved in SQLite and existing tile files are skipped on resume.")

    def _on_progress(self, done, total, phase):
        self.progress.setMaximum(max(1, total))
        self.progress.setValue(done)
        self.status.setText(f"{phase}: {done:,}/{total:,}")

    def _on_finished(self):
        self.status.setText("Finished")
        self.start_btn.setEnabled(True)

    def _on_failed(self, msg):
        self.status.setText("Error")
        self.start_btn.setEnabled(True)
        self._log(f"ERROR: {msg}")
        QMessageBox.critical(self, "Py Map Stitcher", msg)

    def _log(self, msg):
        self.log.append(str(msg))

    def closeEvent(self, event):
        if self.worker is not None and self.worker.isRunning():
            self.stop_event.set()
        try:
            canvas = self.iface.mapCanvas()
            if self._qgis_select_tool is not None and canvas.mapTool() == self._qgis_select_tool and self._previous_map_tool is not None:
                canvas.setMapTool(self._previous_map_tool)
        except Exception:
            pass
        event.accept()
