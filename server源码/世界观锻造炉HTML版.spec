# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ['E:/世界观/server源码/cangjingge_server.py'],
    pathex=[],
    binaries=[],
    datas=[('E:/世界观/server源码/世界观锻造炉.html', '.'), ('E:/世界观/server源码/ICON.png', '.'), ('E:/世界观/server源码/ICON.ico', '.')],
    hiddenimports=[],
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
    a.binaries,
    a.datas,
    [],
    name='世界观锻造炉HTML版',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=['E:/世界观/server源码/ICON.ico'],
)
