# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec - single-file Windows .exe for the Nordic OTA Flasher.

Build:  pyinstaller NordicOTAFlasher.spec        (output: dist/RFLab.io OTA Flasher.exe)
"""
from PyInstaller.utils.hooks import collect_all, collect_submodules

datas, binaries, hiddenimports = [], [], []

# bleak's WinRT backend pulls in the split winrt-* extension packages; collect them all.
for pkg in ("bleak", "winrt"):
    d, b, h = collect_all(pkg)
    datas += d
    binaries += b
    hiddenimports += h
hiddenimports += collect_submodules("winrt")
hiddenimports += ["qasync"]

# Trim the large unused Qt modules so the single file stays as small as practical.
excludes = [
    "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets", "PySide6.QtWebEngineQuick",
    "PySide6.QtQml", "PySide6.QtQuick", "PySide6.QtQuick3D", "PySide6.QtQuickWidgets",
    "PySide6.Qt3DCore", "PySide6.Qt3DRender", "PySide6.Qt3DInput",
    "PySide6.Qt3DAnimation", "PySide6.Qt3DExtras",
    "PySide6.QtMultimedia", "PySide6.QtMultimediaWidgets",
    "PySide6.QtCharts", "PySide6.QtDataVisualization", "PySide6.QtGraphs",
    "PySide6.QtPdf", "PySide6.QtPdfWidgets", "PySide6.QtDesigner",
    "PySide6.QtSql", "PySide6.QtTest", "PySide6.QtWebSockets", "PySide6.QtWebChannel",
    "PySide6.QtSerialPort", "PySide6.QtSensors", "PySide6.QtNfc", "PySide6.QtBluetooth",
    "PySide6.QtLocation", "PySide6.QtPositioning", "PySide6.QtRemoteObjects",
    "tkinter", "matplotlib", "numpy", "PIL",
]

a = Analysis(
    ["run.py"],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="RFLab.io OTA Flasher",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    runtime_tmpdir=None,
    console=False,          # GUI app — no console window
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
