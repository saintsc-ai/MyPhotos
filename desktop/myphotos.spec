# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec — single-file bundle of the MyPhotos desktop client.
#
# QtWebEngine is heavy (~150 MB once you count Chromium + ICU + locales
# + the pak resource files), so the produced exe will land around
# 180–220 MB. That's fine for an internal app; the win is no Python
# install or pip wrangling on the user's machine.
#
# collect_all('PySide6') pulls in the WebEngine helper exe, ICU data,
# locales, and the qtwebengine_resources*.pak files automatically;
# leaving any one of those out makes the page render blank.

from PyInstaller.utils.hooks import collect_all

datas, binaries, hiddenimports = [], [], []

for pkg in ("PySide6",):
    d, b, h = collect_all(pkg)
    datas += d
    binaries += b
    hiddenimports += h


a = Analysis(
    ["app.py"],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="MyPhotos",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    # UPX corrupts the QtWebEngineProcess.exe extraction on some Windows
    # builds — keep it off so the bundle launches reliably.
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    # Drop an icon.ico beside app.py to pick it up; commented out so
    # the build doesn't fail when no icon is provided.
    # icon="icon.ico",
)
