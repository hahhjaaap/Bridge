#!/usr/bin/env python3
"""Validate UDB source metadata and guard against accidental local-secret commits.

This checks the current tracked working tree, not every historical Git object.
It deliberately reports only the file, line, and pattern category for a match;
it never prints matched credential text.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TEXT_SUFFIXES = {
    ".py", ".ps1", ".json", ".md", ".yml", ".yaml", ".txt",
    ".cmd", ".bat", ".ini", ".toml", ".cfg", ".spec",
}
ALLOWED_ENV_EXAMPLES = {".env.example"}
FORBIDDEN_NAMES = {
    "mcp.json", "mcp.local.json", "mcp_configuration.local.json",
    "credentials.json", "secrets.json",
}
FORBIDDEN_DIRS = {
    "secrets", "local-config", "private-config", ".udb-recovery",
}
FORBIDDEN_SUFFIXES = {
    ".pem", ".p12", ".pfx", ".key", ".sqlite", ".sqlite3", ".db", ".log",
}
PLACEHOLDER_LINE = re.compile(
    r"(?i)(synthetic|not[- _]?a[- _]?real|placeholder|example|dummy|fake|redacted|replace[- _]?me|changeme)"
)
SECRET_PATTERNS = (
    ("OpenAI-style API key", re.compile(r"\bsk-(?:proj-|svcacct-|admin-)?[A-Za-z0-9_-]{24,}\b")),
    ("GitHub token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9_]{30,}|github_pat_[A-Za-z0-9_]{40,})\b")),
    ("Google API key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
    ("AWS access key ID", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("Slack token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{20,}\b")),
    ("private key block", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----")),
)


def tracked_paths() -> list[Path]:
    """Return Git-tracked paths; fall back to a source-tree walk for exported archives."""
    try:
        result = subprocess.run(
            ["git", "ls-files", "-z"],
            cwd=ROOT,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        paths = [ROOT / item for item in result.stdout.decode("utf-8").split("\0") if item]
        if paths:
            return paths
    except (OSError, subprocess.CalledProcessError):
        pass

    ignored = {".git", ".venv", "venv", "__pycache__", "vendor", "vendor-office"}
    return [
        path for path in ROOT.rglob("*")
        if path.is_file() and not any(part in ignored for part in path.parts)
    ]


def is_forbidden_local_artifact(path: Path) -> bool:
    relative = path.relative_to(ROOT)
    parts = {part.lower() for part in relative.parts}
    name = path.name.lower()
    suffix = path.suffix.lower()

    if name in FORBIDDEN_NAMES or suffix in FORBIDDEN_SUFFIXES:
        return True
    if name == ".env" or (name.startswith(".env.") and name not in ALLOWED_ENV_EXAMPLES):
        return True
    if parts.intersection(FORBIDDEN_DIRS):
        return True
    if relative.parts and relative.parts[0].lower() in {"state", "logs", "reports", "deployment"}:
        return True
    return False


def main() -> int:
    failures: list[str] = []
    paths = tracked_paths()
    relative_paths = {path.relative_to(ROOT).as_posix() for path in paths}

    required_files = (
        "README.md",
        "plugin.json",
        "MCP_CONFIGURATION.example.json",
        "udb_unified.py",
        "udb_companion.py",
        "udb_tool_catalog.py",
        ".gitignore",
    )
    for name in required_files:
        if name not in relative_paths:
            failures.append(f"required source file is missing: {name}")

    manifest_path = ROOT / "plugin.json"
    unified_path = ROOT / "udb_unified.py"
    readme_path = ROOT / "README.md"
    config_path = ROOT / "MCP_CONFIGURATION.example.json"

    manifest: dict = {}
    if manifest_path.is_file():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            failures.append(f"plugin.json is not valid UTF-8 JSON: {type(exc).__name__}")

    plugin_version = manifest.get("version")
    if manifest.get("name") != "universal-desktop-bridge":
        failures.append("plugin.json has an unexpected plugin name")
    if not isinstance(plugin_version, str) or not re.fullmatch(
        r"\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?", plugin_version or ""
    ):
        failures.append("plugin.json version is missing or not a valid version string")

    if unified_path.is_file():
        source = unified_path.read_text(encoding="utf-8")
        match = re.search(r'(?m)^VERSION\s*=\s*["\']([^"\']+)["\']', source)
        if not match:
            failures.append("udb_unified.py does not declare VERSION")
        elif plugin_version and match.group(1) != plugin_version:
            failures.append("plugin.json and udb_unified.py versions do not match")

    if readme_path.is_file() and plugin_version:
        readme = readme_path.read_text(encoding="utf-8")
        match = re.search(r"(?m)^# Universal Desktop Bridge (\d+\.\d+\.\d+)\s*$", readme)
        stable_version = plugin_version.split("-", 1)[0]
        if not match or match.group(1) != stable_version:
            failures.append("README heading must match the stable part of the plugin version")

    if config_path.is_file():
        try:
            config = json.loads(config_path.read_text(encoding="utf-8"))
            server = config["mcpServers"]["universal-desktop-bridge"]
            args = server["args"]
            required_placeholders = {
                "PATH_TO_SOURCE/udb_unified.py",
                "PATH_TO_UDB_INSTALL",
                "PATH_TO_SOURCE/udb_companion.py",
                "PATH_TO_APPROVED_PROJECT",
            }
            if not required_placeholders.issubset(set(args)):
                failures.append("MCP configuration example is missing safe path placeholders")
        except (OSError, json.JSONDecodeError, KeyError, TypeError) as exc:
            failures.append(f"MCP configuration example is invalid: {type(exc).__name__}")

    gitignore_path = ROOT / ".gitignore"
    if gitignore_path.is_file():
        ignored_text = gitignore_path.read_text(encoding="utf-8")
        for required_pattern in (".env", "mcp.json", "*.pem", "state/", "vendor/", "build/", "dist/"):
            if required_pattern not in ignored_text:
                failures.append(f".gitignore is missing a local/build exclusion: {required_pattern}")

    for path in paths:
        try:
            if is_forbidden_local_artifact(path):
                failures.append(f"local runtime or credential artifact is tracked: {path.relative_to(ROOT).as_posix()}")
                continue
            if path.suffix.lower() not in TEXT_SUFFIXES and path.name not in {
                ".gitignore", ".gitattributes", ".editorconfig"
            }:
                continue
            content = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue

        for line_number, line in enumerate(content.splitlines(), start=1):
            if PLACEHOLDER_LINE.search(line):
                continue
            for label, pattern in SECRET_PATTERNS:
                if pattern.search(line):
                    failures.append(
                        f"possible {label} in {path.relative_to(ROOT).as_posix()}:{line_number}"
                    )

    if failures:
        print("Source validation FAILED:")
        for failure in sorted(set(failures)):
            print(f" - {failure}")
        return 1

    print(
        "Source validation PASS: "
        f"version={plugin_version}, tracked_files={len(paths)}, "
        f"secret_pattern_families={len(SECRET_PATTERNS)}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
