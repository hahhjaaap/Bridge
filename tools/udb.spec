from pathlib import Path
import os

from PyInstaller.utils.hooks import collect_data_files, collect_submodules, copy_metadata
from udb.components import verified_assets

base = Path(SPECPATH).parent
asset_root = Path(os.environ.get("UDB_BUILD_ASSETS", str(base / "build-assets")))
components = verified_assets(asset_root)
binaries = [
    (str(asset_root / item["binary"]), "udb/runtime/bin")
    for item in components
]
assets = collect_data_files(
    "udb",
    includes=["*.ps1", "companion/*.ps1", "desktop_guest.py", "build_info.json", "plugin.template.json"],
)
assets += collect_data_files("mcp") + copy_metadata("mcp", recursive=True)
if components:
    assets.append((str(asset_root / "components.json"), "udb/runtime"))
    assets += [
        (str(asset_root / item["license"]), "udb/runtime/licenses")
        for item in components
    ]
hidden = (
    collect_submodules("udb")
    + collect_submodules("uvicorn")
    + ["anyio._backends._asyncio", "win32cred", "win32timezone"]
)
a = Analysis(
    [str(base / "tools/frozen_entry.py")],
    pathex=[str(base / "src")],
    binaries=binaries,
    datas=assets,
    hiddenimports=hidden,
    hookspath=[],
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, a.binaries, a.datas, [],
    name="udb", debug=False, strip=False, upx=False, console=True,
)
