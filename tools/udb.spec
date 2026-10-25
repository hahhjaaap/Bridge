from pathlib import Path
from PyInstaller.utils.hooks import collect_data_files, collect_submodules, copy_metadata
base=Path(SPECPATH).parent
assets=collect_data_files('udb',includes=['*.ps1','companion/*.ps1','desktop_guest.py','build_info.json'])
assets+=collect_data_files('mcp')+copy_metadata('mcp',recursive=True)
hidden=collect_submodules('udb')+collect_submodules('uvicorn')+['anyio._backends._asyncio','win32cred','win32timezone']
a=Analysis([str(base/'tools/frozen_entry.py')],pathex=[str(base/'src')],binaries=[],datas=assets,hiddenimports=hidden,hookspath=[],runtime_hooks=[],excludes=[],noarchive=False)
pyz=PYZ(a.pure)
exe=EXE(pyz,a.scripts,a.binaries,a.datas,[],name='udb',debug=False,strip=False,upx=False,console=True)
