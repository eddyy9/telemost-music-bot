# -*- mode: python ; coding: utf-8 -*-

from PyInstaller.utils.hooks import collect_all


datas = [("README.md", "."), ("PORTABLE_README.txt", ".")]
binaries = []
hiddenimports = []

# Эти пакеты загружают часть модулей и бинарников динамически, поэтому одного
# анализа import-ов недостаточно. collect_all делает сборку больше, но зато
# воспроизводимой и пригодной для передачи на другой компьютер.
for package in ("yt_dlp", "playwright", "imageio_ffmpeg", "_sounddevice_data"):
    package_datas, package_binaries, package_hidden = collect_all(package)
    datas += package_datas
    binaries += package_binaries
    hiddenimports += package_hidden

hiddenimports += ["sounddevice", "_sounddevice"]

a = Analysis(
    ["bot.py"],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="TelemostMusicBot",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="TelemostMusicBot",
)
