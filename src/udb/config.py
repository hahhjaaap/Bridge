"""Runtime configuration. Secrets and grants remain in local owner storage."""

from __future__ import annotations

import os
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class Settings:
    home: Path
    project_root: Path
    enable_terminal: bool = False
    core_backend: str = "source"
    core_exe: Path | None = None


def _portable_home(anchor: Path) -> Path | None:
    """Find an existing portable installation without assuming its folder name."""
    for candidate in (anchor, *anchor.parents):
        if all(
            (candidate / name).is_file()
            for name in ("udb.toml", "plugin.json", "mcp.json")
        ):
            return candidate
    return None


def default_home() -> Path:
    configured_home = os.environ.get("UDB_HOME")
    if configured_home:
        return Path(configured_home).expanduser().absolute()
    anchor = (
        Path(sys.executable).absolute().parent
        if getattr(sys, "frozen", False)
        else Path(__file__).absolute().parent
    )
    portable = _portable_home(anchor)
    if portable is not None:
        return portable
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
        return (base / "UniversalDesktopBridge").expanduser().absolute()
    base = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    return (base / "universal-desktop-bridge").expanduser().absolute()


def checked(path: str | Path) -> Path:
    """Normalize a path after rejecting symbolic links and Windows reparse points."""
    normalized = Path(path).expanduser().absolute()
    for item in (normalized, *normalized.parents):
        if item.is_symlink():
            raise ValueError("Linked configuration path refused")
        if item.exists() and getattr(item.lstat(), "st_file_attributes", 0) & 0x400:
            raise ValueError("Linked configuration path refused")
    return normalized.resolve(strict=False)


def _read_config(path: Path) -> dict[str, Any]:
    """Read and validate the configuration file's structure and size."""
    if not path.exists():
        return {}
    if not path.is_file() or path.stat().st_nlink != 1 or path.stat().st_size > 65536:
        raise ValueError("Invalid configuration file")

    with path.open("rb") as stream:
        raw = stream.read(65537)
    if len(raw) > 65536:
        raise ValueError("Configuration exceeds 64 KiB")
    try:
        data = tomllib.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeError):
        raise ValueError("Invalid UDB TOML configuration") from None

    if set(data) - {"bridge"} or not isinstance(data.get("bridge", {}), dict):
        raise ValueError("Unknown configuration section")
    values = data.get("bridge", {})
    allowed_fields = {
        "home",
        "project_root",
        "enable_terminal",
        "core_backend",
        "core_exe",
    }
    if set(values) - allowed_fields:
        raise ValueError(
            "Unknown bridge configuration field; credentials belong in local owner storage"
        )
    return values


def _configured_path(value: Any, config_file: Path) -> Path:
    """Resolve a configured path relative to the config file when needed."""
    if not isinstance(value, str) or not value or "\0" in value:
        raise ValueError("Invalid configured path")
    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        candidate = config_file.parent / candidate
    return checked(candidate)


def load(
    home: str | Path | None = None,
    config: str | Path | None = None,
    root: str | Path | None = None,
    enable_terminal: bool | None = None,
    core_exe: str | Path | None = None,
    allow_missing_config: bool = False,
) -> Settings:
    """Load settings from explicit arguments and the selected TOML file."""
    initial_home = checked(home) if home else checked(default_home())
    selected_config = checked(config) if config else checked(initial_home / "udb.toml")
    if config and not selected_config.exists() and not allow_missing_config:
        raise ValueError("Explicit configuration file does not exist")

    values = _read_config(selected_config)
    actual_home = (
        checked(home)
        if home
        else _configured_path(values["home"], selected_config)
        if "home" in values
        else initial_home
    )
    project_root = (
        checked(root)
        if root
        else _configured_path(values["project_root"], selected_config)
        if "project_root" in values
        else actual_home / "projects" / "workspace"
    )

    terminal_enabled = (
        enable_terminal
        if enable_terminal is not None
        else values.get("enable_terminal", False)
    )
    if type(terminal_enabled) is not bool:
        raise ValueError("enable_terminal must be boolean")

    backend = "installed" if core_exe else values.get("core_backend", "source")
    if backend not in ("source", "installed"):
        raise ValueError("core_backend must be source or installed")
    executable = (
        checked(core_exe)
        if core_exe
        else _configured_path(values["core_exe"], selected_config)
        if "core_exe" in values
        else actual_home / "UniversalDesktopBridge.exe"
        if backend == "installed"
        else None
    )
    if backend == "source" and executable is not None:
        raise ValueError("core_exe requires installed backend")

    return Settings(
        home=actual_home,
        project_root=project_root,
        enable_terminal=terminal_enabled,
        core_backend=backend,
        core_exe=executable,
    )


# Effective settings are passed to the manager through bounded arguments.
# They are never persisted as grants or inserted into the connection manifest.
_active: Settings | None = None


def activate(settings: Settings) -> Settings | None:
    """Temporarily expose settings to the legacy manager entry point."""
    global _active
    previous = _active
    _active = settings
    return previous


def effective(home: str | Path) -> Settings:
    normalized_home = checked(home)
    if _active is not None and checked(_active.home) == normalized_home:
        return _active
    return load(home=normalized_home)


def manager_options(home: str | Path) -> list[str]:
    settings = effective(home)
    args = [
        "--runtime-root",
        str(settings.project_root),
        "--runtime-terminal",
        "on" if settings.enable_terminal else "off",
    ]
    if settings.core_exe:
        args.extend(["--runtime-core-exe", str(settings.core_exe)])
    return args


def serve_options(home: str | Path) -> list[str]:
    settings = effective(home)
    args = [
        "serve",
        "--home",
        str(settings.home),
        "--root",
        str(settings.project_root),
        "--enable-terminal" if settings.enable_terminal else "--disable-terminal",
    ]
    if settings.core_exe:
        args.extend(["--core-exe", str(settings.core_exe)])
    return args


def prepare_workspace(settings: Settings) -> None:
    """Create application data/workspace folders, never grants or registration."""
    for selected in (settings.home, settings.project_root):
        folder = checked(selected)
        folder.mkdir(parents=True, exist_ok=True)
        checked(folder)
