# -*- coding: utf-8 -*-

def classFactory(iface):
    from .pymapstitcher_plugin import PyMapStitcherPlugin
    return PyMapStitcherPlugin(iface)
