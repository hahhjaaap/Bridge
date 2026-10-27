"""Audit tracked UDB source for accidental generated/private artifacts.

Read-only: does not delete files, rewrite source or inspect ignored owner data.
Compatibility shims and third-party license notices are intentionally retained.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
from collections import defaultdict
from pathlib import Path, PurePosixPath

CACHE_DIRS = {
    "__pycache__", ".pytest_cache", ".ruff_cache", ".mypy_cache",
    ".venv", "venv", "node_modules", "htmlcov", ".tox", ".nox",
    ".udb-work-state", ".udb-recovery", "state",
}
PRIVATE_NAMES = {
    ".env", "credentials.json", "secrets.json", "http-token",
    "computer-access.dpapi", "connection.json", "runtime.json",
}
PRIVATE_SUFFIXES = (
    ".pyc", ".pyo", ".dpapi", ".sqlite3", ".sqlite3-shm", ".sqlite3-wal",
    ".ckpt", ".safetensors", ".pth", ".pt", ".log", ".pid",
)
GENERATED_SUFFIXES = (".exe", ".dll", ".pyd", ".tmp", ".swp", ".swo")
ROOT_COMPAT = {
    "_udb_compat.py", "bridge.py", "udb_background.py", "udb_companion.py",
    "udb_documents.py", "udb_unified.py",
}
EXCLUDED_DUPLICATE_ROOTS = ("THIRD_PARTY_NOTICES/", "build-assets/licenses/")


def reason(path: str) -> str:
    """Flag only high-confidence files that must not be committed."""
    normalized = PurePosixPath(path)
    if normalized.is_absolute() or ".." in normalized.parts:
        return "unsafe repository path"
    parts = {part.casefold() for part in normalized.parts[:-1]}
    name = normalized.name.casefold()
    if parts & CACHE_DIRS:
        return "generated/runtime directory"
    if name in PRIVATE_NAMES or name.startswith((".env.", "credentials.", "secrets.")):
        return "private credential/configuration"
    if name.endswith(PRIVATE_SUFFIXES):
        return "private runtime/model artifact"
    if name.endswith(GENERATED_SUFFIXES):
        return "generated binary/temporary artifact"
    return ""


def tracked_files(root: Path) -> list[str]:
    git = shutil.which("git")
    if not git:
        raise RuntimeError("Git executable is required for tracked-file auditing")
    result = subprocess.run(
        [git, "ls-files", "-z", "--cached"],
        cwd=root,
        capture_output=True,
        check=True,
    )
    return sorted(
        name.decode("utf-8", "surrogateescape")
        for name in result.stdout.split(b"\0")
        if name
    )


def duplicates(root: Path, paths: list[str]) -> list[list[str]]:
    """Report identical source files, but never assume they are safe to delete."""
    groups: dict[str, list[str]] = defaultdict(list)
    for name in paths:
        if name.startswith(EXCLUDED_DUPLICATE_ROOTS):
            continue  # Legal notices may be identical and must be preserved.
        if Path(name).suffix.lower() not in {".py", ".ps1", ".json", ".toml", ".md"}:
            continue
        source = root / name
        if not source.is_file() or source.is_symlink():
            continue
        digest = hashlib.sha256(source.read_bytes()).hexdigest()
        groups[digest].append(name)
    return sorted(
        (sorted(names) for names in groups.values() if len(names) > 1),
        key=lambda group: group[0],
    )


def audit(root: Path, paths: list[str]) -> dict:
    problems = [{"path": p, "reason": reason(p)} for p in paths if reason(p)]
    repeated = duplicates(root, paths)
    return {
        "tracked_files": len(paths),
        "unsafe_files": problems,
        "identical_source_groups": repeated,
        "compatibility_shims_retained": sorted(p for p in paths if p in ROOT_COMPAT),
        "third_party_notices_retained": sum(
            p.startswith("THIRD_PARTY_NOTICES/") for p in paths
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--strict", action="store_true")
    args = parser.parse_args()
    root = args.root.resolve()
    result = audit(root, tracked_files(root))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 1 if args.strict and result["unsafe_files"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
