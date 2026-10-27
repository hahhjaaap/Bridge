"""Local installation lifecycle; never reads credentials or permission receipts."""

from __future__ import annotations

import json
import re
import subprocess
import sys
import urllib.request
from pathlib import Path

from ._version import VERSION
from .owner_command import CommandError, read_object, regular

REPOSITORY = "hahhjaaap/Universal-Desktop-Bridge"


def installation(executable=None):
    """Discover only the installation owning this image, never an unrelated default."""
    exe = Path(executable or sys.executable).absolute()
    # Managed layout: <root>/releases/<digest>/udb.exe.
    if exe.parent.parent.name != "releases":
        return None
    root = regular(exe.parent.parent.parent)
    record = read_object(root / "install.json", 8192)
    validate_record(root, record)
    if Path(record["exe"]).absolute() != exe:
        raise CommandError(
            "This image is not the active installation. Use its bin/udb.cmd launcher."
        )
    return root, record


def validate_record(root, record):
    if (
        record.get("product") != "UniversalDesktopBridge"
        or record.get("schema") != 1
        or not isinstance(record.get("version"), str)
        or not re.fullmatch(r"\d+\.\d+\.\d+", record["version"])
        or not isinstance(record.get("archive_sha256"), str)
        or not re.fullmatch(r"[a-f0-9]{64}", record["archive_sha256"])
        or not isinstance(record.get("home"), str)
        or not isinstance(record.get("exe"), str)
    ):
        raise CommandError(
            "Invalid UDB installation record. Re-run the official installer."
        )
    image = regular(Path(record["exe"]))
    if (
        not image.is_absolute()
        or image.name != "udb.exe"
        or image.parent.parent != root / "releases"
        or image.parent.name != record["archive_sha256"][:24]
        or not image.is_file()
        or not Path(record["home"]).is_absolute()
    ):
        raise CommandError(
            "Invalid UDB release location. Re-run the official installer."
        )
    regular(Path(record["home"]))


def latest_release():
    # Fixed public GitHub endpoint; no auth, local key, user feed or raw server errors.
    req = urllib.request.Request(
        f"https://api.github.com/repos/{REPOSITORY}/releases/latest",
        headers={"User-Agent": "UDB-update", "Accept": "application/vnd.github+json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as reply:
            raw = reply.read(262145)
        if len(raw) > 262144:
            raise ValueError()
        data = json.loads(raw)
        tag = data["tag_name"]
        if (
            data.get("draft")
            or data.get("prerelease")
            or not re.fullmatch(r"v\d+\.\d+\.\d+", tag)
        ):
            raise ValueError()
        return tag[1:]
    except (OSError, ValueError, KeyError, TypeError):
        raise CommandError(
            "Cannot check GitHub releases. Check the network and retry udb update --check."
        ) from None


def newer(candidate, current):
    return tuple(map(int, candidate.split("."))) > tuple(map(int, current.split(".")))


def installer_script():
    embedded = Path(__file__).with_name("install.ps1")
    if embedded.is_file():
        return regular(embedded)
    source = Path(__file__).resolve().parents[2] / "install.ps1"
    if source.is_file():
        return regular(source)
    raise CommandError(
        "The bundled installer is missing. Re-run the official installer."
    )


def run(action, settings, *, check=False, as_json=False):
    from .windows_paths import system_executable

    found = installation()
    if action == "update" and check:
        latest = latest_release()
        report = dict(
            current=VERSION,
            latest=latest,
            update_available=newer(latest, VERSION),
            managed=bool(found),
        )
        print(
            json.dumps(report)
            if as_json
            else f"UDB {VERSION}; latest {latest}; update available: {report['update_available']}."
        )
        return 0
    if not found:
        raise CommandError(
            "Update/rollback requires the one-command installation. Portable users should run the official installer; existing portable files are preserved."
        )
    root, record = found
    if Path(record["home"]) != settings.home:
        raise CommandError(
            "Use the managed launcher with its saved data home for update/rollback."
        )
    args = [
        system_executable("powershell.exe"),
        "-NoLogo",
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(installer_script()),
        "-InstallRoot",
        str(root),
        "-DataHome",
        record["home"],
        "-NoPath",
        "-NoStart",
    ]
    if action == "rollback":
        args.append("-Rollback")
    else:
        latest = latest_release()
        if not newer(latest, record["version"]):
            print(
                json.dumps({"updated": False, "version": record["version"]})
                if as_json
                else "UDB is up to date."
            )
            return 0
        args += [
            "-FeedUrl",
            f"https://github.com/{REPOSITORY}/releases/download/v{latest}",
        ]
    # Installer switches an atomic record and restores the old agent on failure.
    # Output is intentionally not forwarded: a child error can include local context.
    try:
        result = subprocess.run(args, capture_output=True, timeout=900, shell=False)
    except subprocess.TimeoutExpired:
        raise CommandError(
            "Installer timed out; the outcome is unknown. Inspect udb status and the active version before retrying."
        ) from None
    if result.returncode:
        raise CommandError(
            "Installation change failed. The previous release is retained. Run udb doctor; use the existing launcher to inspect the active version."
        )
    active = read_object(root / "install.json", 8192)
    validate_record(root, active)
    print(
        json.dumps(
            {"action": action, "version": active["version"], "home_preserved": True}
        )
        if as_json
        else f"UDB {active['version']} active. Settings retained. Use udb start to view logs."
    )
    return 0
