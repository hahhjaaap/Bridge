"""Build source or a standalone Windows application from the canonical package."""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

from ._version import VERSION


def runtime_manifest(package=None):
    package = Path(package) if package else Path(__file__).parent
    result = {}
    for p in sorted(package.rglob("*")):
        if not p.is_file() or p.suffix not in (".py", ".ps1"):
            continue
        if p.is_symlink() or getattr(p.lstat(), "st_file_attributes", 0) & 0x400:
            raise ValueError("Linked source component refused")
        result["udb/" + p.relative_to(package).as_posix()] = hashlib.sha256(
            p.read_bytes()
        ).hexdigest()
    return result


def build_info():
    embedded = Path(__file__).with_name("build_info.json")
    if getattr(sys, "frozen", False) and embedded.is_file():
        return json.loads(embedded.read_text("utf-8"))
    return dict(
        version=VERSION,
        kind="source",
        code_signed=False,
        source_files=runtime_manifest(),
    )


def source_root():
    import tomllib

    for candidate in (Path.cwd(), Path(__file__).resolve().parents[2]):
        project = candidate / "pyproject.toml"
        if not project.is_file():
            continue
        data = tomllib.loads(project.read_text("utf-8"))
        if (
            data.get("project", {}).get("name") == "universal-desktop-bridge"
            and (candidate / "src/udb/_version.py").is_file()
        ):
            return candidate
    raise ValueError("Build requires a source checkout")


def copy_notices(destination):
    """Retain installed runtime dependency licenses without copying owner data."""
    from packaging.requirements import Requirement
    from packaging.utils import canonicalize_name

    pending = {
        canonicalize_name(n)
        for n in (
            "mcp",
            "websocket-client",
            "pywin32",
            "PySide6-Essentials",
            "shiboken6",
            "openpyxl",
            "python-docx",
            "pypdf",
            "reportlab",
            "pillow",
            "uvicorn",
            "pyinstaller",
        )
    }
    seen, files = set(), []
    while pending:
        name = pending.pop()
        if name in seen:
            continue
        seen.add(name)
        try:
            distribution = importlib.metadata.distribution(name)
        except importlib.metadata.PackageNotFoundError:
            continue
        for value in distribution.requires or []:
            requirement = Requirement(value)
            if requirement.marker is None or requirement.marker.evaluate({"extra": ""}):
                pending.add(canonicalize_name(requirement.name))
        for relative in distribution.files or []:
            if not any(
                word in relative.name.casefold()
                for word in ("license", "copying", "notice")
            ):
                continue
            if not any(part.endswith(".dist-info") for part in relative.parts):
                continue
            source = Path(distribution.locate_file(relative))
            if (
                not source.is_file()
                or source.is_symlink()
                or source.stat().st_size > 4 * 1024 * 1024
            ):
                continue
            parts = relative.parts
            index = next(
                i for i, part in enumerate(parts) if part.endswith(".dist-info")
            )
            target = destination / "THIRD_PARTY_NOTICES" / Path(*parts[index:])
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
            files.append(target)
    return files


def standalone_package(base, destination, asset_root, components):
    """Create a binary-only distribution; logs, state and source are excluded."""
    executable = destination / "udb.exe"
    if not executable.is_file():
        raise ValueError("Standalone executable was not produced")
    files = [executable]
    installer = destination / "install.ps1"
    shutil.copyfile(base / "install.ps1", installer)
    files.append(installer)
    license_file = destination / "LICENSE"
    shutil.copyfile(base / "LICENSE", license_file)
    files.append(license_file)
    readme = destination / "STANDALONE.md"
    readme.write_text(
        "# Universal Desktop Bridge standalone\n\n"
        "The udb.exe application includes Python, UDB workers and installed Python runtime libraries. "
        "No Python installation, pip, Node.js, Git checkout or Desktop Commander is required on the target PC.\n\n"
        "Commands (PowerShell, from this folder):\n\n"
        "~~~powershell\n"
        ".\\udb.exe runtime --probe\n"
        ".\\udb.exe init\n"
        ".\\udb.exe start --offline\n"
        ".\\udb.exe status --json\n"
        ".\\udb.exe logs --details\n"
        ".\\udb.exe stop\n"
        ".\\udb.exe gui\n"
        "~~~\n\n"
        "Interactive start follows live logs in that same console; use start --detach for background-only startup. "
        "The proxy and shared background-work capacity are limited to 10 each, subject to CPU/RAM availability. "
        "The logs command follows live tool calls with timestamps, status and duration. "
        "Use logs --once --json for a snapshot. Ctrl+C closes only the viewer. "
        "Only bounded metadata is retained; source text, shell arguments, terminal output and credentials are not recorded.\n\n"
        "Offline mode starts the local agent without a network connection or permission changes. "
        "Stop it before changing connection modes or opening the settings GUI. "
        "To use optional computer access, run .\\udb.exe --access computer in your own attended terminal; "
        "no permission is granted by installation or tests.\n\n"
        "ChatGPT connectivity still needs network access, account configuration and the supported tunnel service. "
        "Bundled helper versions/hashes are listed by runtime and build-info. "
        "Private credentials, grants and journals are stored in the owner's application data, never in this EXE or ZIP. "
        "An existing portable installation retains its selected home and settings. "
        "The standalone executable launches its embedded MCP workers instead of a development-venv manifest.\n\n"
        "Optional features retain their platform requirements: Windows Sandbox plus its configured guest runtime, "
        "an installed supported browser, and FFmpeg/FFprobe for media work when those helpers are not bundled. "
        "Commands that intentionally launch user-selected software require that software. "
        "The legacy installed-core backend is an explicit compatibility mode, not a standalone default.\n\n"
        "Optional UDB Aura: in Chrome, Load unpacked from chrome-extension/udb-aura, then run "
        "tools/enable_chrome_aura.ps1 in an attended PowerShell window and confirm the extension ID. "
        "The extension requests HTTP(S) site access only to draw labels on explicitly marked AI tabs; "
        "it is never silently installed.\n\n"
        "SHA256 detects accidental corruption, not publisher identity. This build is not code-signed.\n",
        encoding="utf-8",
    )
    files.append(readme)
    # Optional Chrome Aura extension is shipped as unpacked browser resources.
    # It is never installed into the owner's Chrome profile automatically.
    aura_root = base / "chrome-extension" / "udb-aura"
    if aura_root.is_dir():
        for source in sorted(aura_root.rglob("*")):
            if not source.is_file():
                continue
            if source.is_symlink() or source.suffix.lower() not in {
                ".json",
                ".html",
                ".js",
                ".css",
                ".png",
            }:
                raise ValueError("Unsafe Chrome Aura package resource")
            target = (
                destination
                / "chrome-extension"
                / "udb-aura"
                / source.relative_to(aura_root)
            )
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
            files.append(target)
        helper = base / "tools" / "enable_chrome_aura.ps1"
        if helper.is_file():
            target = destination / "tools" / "enable_chrome_aura.ps1"
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(helper, target)
            files.append(target)
    files.extend(copy_notices(destination))
    for record in components:
        source = asset_root / record["license"]
        target = (
            destination
            / "THIRD_PARTY_NOTICES"
            / "components"
            / (record["name"] + ".txt")
        )
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        files.append(target)
    checksums = destination / "STANDALONE-SHA256SUMS"
    checksums.write_text(
        "".join(
            hashlib.sha256(p.read_bytes()).hexdigest()
            + "  "
            + p.relative_to(destination).as_posix()
            + "\n"
            for p in sorted(set(files))
        ),
        encoding="ascii",
    )
    files.append(checksums)
    archive = destination / f"udb-{VERSION}-windows-standalone.zip"
    temporary = archive.with_suffix(".zip.tmp")
    try:
        with zipfile.ZipFile(temporary, "w", zipfile.ZIP_DEFLATED) as package:
            for path in sorted(set(files)):
                package.write(path, "UDB/" + path.relative_to(destination).as_posix())
        temporary.replace(archive)
    finally:
        temporary.unlink(missing_ok=True)
    return archive


def run(exe=False, output=None, assets=None):
    base = source_root()
    destination = Path(output).absolute() if output else base / "dist"
    asset_root = Path(assets).absolute() if assets else base / "build-assets"
    if assets and not (asset_root / "components.json").is_file():
        raise ValueError("Explicit helper inventory is missing")
    from .components import verified_assets

    components = verified_assets(asset_root) if exe else []
    if exe:
        if os.name != "nt":
            raise ValueError("A Windows executable must be built on Windows")
        info = base / "src/udb/build_info.json"
        with info.open("x", encoding="utf-8") as stream:
            json.dump(
                dict(
                    version=VERSION,
                    source_files=runtime_manifest(base / "src/udb"),
                    components=components,
                    python_embedded=True,
                    source_checkout_required=False,
                    desktop_commander_required=False,
                    kind="frozen",
                    release_provenance=True,
                    code_signed=False,
                ),
                stream,
                sort_keys=True,
                indent=2,
            )
            stream.write("\n")
        try:
            env = os.environ.copy()
            env["PYTHONPATH"] = (
                str(base / "src") + os.pathsep + env.get("PYTHONPATH", "")
            )
            env["UDB_BUILD_ASSETS"] = str(asset_root)
            subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "PyInstaller",
                    "--noconfirm",
                    "--clean",
                    "--distpath",
                    str(destination),
                    str(base / "tools/udb.spec"),
                ],
                cwd=base,
                env=env,
                check=True,
            )
        finally:
            info.unlink(missing_ok=True)
    else:
        subprocess.run(
            [sys.executable, "-m", "build", "--outdir", str(destination)],
            cwd=base,
            check=True,
        )
    destination.mkdir(parents=True, exist_ok=True)
    from .metadata import manifest

    (destination / "plugin.json").write_text(json.dumps(manifest(), indent=2) + "\n")
    for template in ("udb.example.toml", "MCP_CONFIGURATION.example.json"):
        (destination / template).write_bytes((base / template).read_bytes())
    if exe:
        standalone_package(base, destination, asset_root, components)
    files = [
        p
        for p in destination.iterdir()
        if p.is_file() and p.suffix in (".exe", ".whl", ".gz", ".toml", ".json", ".zip")
    ]
    (destination / "SHA256SUMS").write_text(
        "".join(
            hashlib.sha256(p.read_bytes()).hexdigest() + "  " + p.name + "\n"
            for p in sorted(set(files))
        ),
        encoding="ascii",
    )
    return 0
