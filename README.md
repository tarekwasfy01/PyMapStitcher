# -*- coding: utf-8 -*-
from pathlib import Path

from qgis.PyQt.QtCore import QCoreApplication
from qgis.PyQt.QtGui import QIcon
from qgis.PyQt.QtWidgets import QAction

from .dialog import PyMapStitcherDialog


class PyMapStitcherPlugin:
    def __init__(self, iface):
        self.iface = iface
        self.plugin_dir = Path(__file__).resolve().parent
        self.action = None
        self.dialog = None

    def tr(self, message):
        return QCoreApplication.translate("PyMapStitcher", message)

    def initGui(self):
        icon = QIcon(str(self.plugin_dir / "icon.png"))
        self.action = QAction(icon, self.tr("Py Map Stitcher"), self.iface.mainWindow())
        self.action.setObjectName("PyMapStitcherAction")
        self.action.setWhatsThis(self.tr("Open Py Map Stitcher"))
        self.action.setStatusTip(self.tr("Download and stitch map tiles without GDAL"))
        self.action.triggered.connect(self.run)
        self.iface.addToolBarIcon(self.action)
        self.iface.addPluginToMenu(self.tr("Py Map Stitcher"), self.action)

    def unload(self):
        if self.action is not None:
            self.iface.removePluginMenu(self.tr("Py Map Stitcher"), self.action)
            self.iface.removeToolBarIcon(self.action)
            self.action = None
        if self.dialog is not None:
            self.dialog.close()
            self.dialog = None

    def run(self):
        if self.dialog is None:
            self.dialog = PyMapStitcherDialog(self.iface)
        self.dialog.show()
        self.dialog.raise_()
        self.dialog.activateWindow()
