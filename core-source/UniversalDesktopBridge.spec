# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_data_files
from PyInstaller.utils.hooks import collect_submodules
from PyInstaller.utils.hooks import copy_metadata

datas = [('udb/desktop_guest.py', 'udb'), ('udb/desktop_uia.ps1', 'udb'), ('udb/desktop_overlay.ps1', 'udb'), ('udb/desktop_accessibility.ps1', 'udb')]
datas += [('udb/build_info.json', 'udb')]
datas += [('udb/computer_uia.ps1', 'udb')]
hiddenimports = ['anyio._backends._asyncio', 'win32cred', 'win32timezone']
datas += collect_data_files('mcp')
datas += copy_metadata('mcp')
hiddenimports += collect_submodules('uvicorn')


a = Analysis(
    ['bridge.py'],
    pathex=[],
    binaries=[],
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
    a.binaries,
    a.datas,
    [],
    name='UniversalDesktopBridge',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
