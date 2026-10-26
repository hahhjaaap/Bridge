"""Owner-issued exceptions for source files inside otherwise protected projects.

No MCP tool exposes issuance or revocation. The protected-root records remain
intact; a local, DPAPI-bound receipt permits source files only, never model/data
directories, credentials, agent state or the private installation.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import time
import uuid
from pathlib import Path

from . import computer_access
from .core import BLOCK_DIRS, SOURCE_EXT, Agent, PolicyError, is_under, reject_links
from .path_policy import is_data_container

ENTROPY = b"UDB/current-user-source-access/v1"
MAX_RECEIPT = 16 * 1024
SOURCE_SUFFIXES = SOURCE_EXT | {".pyi", ".psm1", ".cmd", ".bat", ".sh", ".rst"}
PRIVATE_DIRS = BLOCK_DIRS | {
    "results",
    "outputs",
    "artifacts",
    "logs",
    "run",
    "runs",
    ".udb-recovery",
    ".udb-work-state",
    ".udb-work-journal",
}
PRIVATE_PREFIXES = (
    ".env",
    ".udb-",
    "credential",
    "secret",
    "id_rsa",
    "id_ed25519",
)
PRIVATE_NAMES = {
    "connection.json",
    "runtime.json",
    "http-token",
    "computer-access.dpapi",
    "token",
    "tokens",
    "password",
    "passwords",
    "credential",
    "secret",
    "api-key",
    "api_key",
    "apikey",
    "access_token",
    "refresh_token",
    "auth_token",
    "authorization",
    "cookie",
    "cookies",
}


def _console(input_stream=None, output_stream=None):
    incoming = sys.stdin if input_stream is None else input_stream
    outgoing = sys.stdout if output_stream is None else output_stream
    if (
        os.name != "nt"
        or incoming is None
        or outgoing is None
        or not incoming.isatty()
        or not outgoing.isatty()
    ):
        raise PolicyError(
            "Source access requires your own attended Windows terminal; "
            "redirected input and MCP requests cannot grant permission."
        )
    return incoming, outgoing


def _protect(data):
    import win32crypt

    return win32crypt.CryptProtectData(
        data, "UDB owner source access", ENTROPY, None, None, 1
    )


def _unprotect(data):
    import win32crypt

    return win32crypt.CryptUnprotectData(data, ENTROPY, None, None, 1)[1]


def receipt_path(agent, root):
    key = os.path.normcase(str(root.resolve()))
    name = hashlib.sha256(key.encode("utf-8")).hexdigest() + ".dpapi"
    path = agent.state / "source-access" / name
    reject_links(path)
    return path


def source_path(path, root):
    """Recognize bounded source paths, not the contents of arbitrary text files."""
    reject_links(path)
    root = root.resolve()
    path = path.resolve()
    if not is_under(path, root):
        return False
    parts = path.relative_to(root).parts
    for part in parts:
        name = part.casefold()
        if (
            name in PRIVATE_DIRS
            or is_data_container(name)
            or name in PRIVATE_NAMES
            or Path(name).stem in PRIVATE_NAMES
            or name.startswith(PRIVATE_PREFIXES)
        ):
            return False
    if path == root or path.is_dir():
        return True
    return path.suffix.casefold() in SOURCE_SUFFIXES or path.name == ".gitignore"


def load(agent, root):
    """Fail closed when a receipt is missing, corrupt, redirected or revoked."""
    try:
        grant = computer_access.load(agent)
        if not grant:
            return None
        path = receipt_path(agent, root)
        with path.open("rb") as stream:
            blob = stream.read(MAX_RECEIPT + 1)
        if len(blob) > MAX_RECEIPT:
            return None
        record = json.loads(_unprotect(blob))
        if (
            not isinstance(record, dict)
            or record.get("policy") != 1
            or record.get("scope") != "source-files-only"
            or record.get("home") != str(agent.home.resolve())
            or record.get("root") != str(root.resolve())
            or record.get("sid") != computer_access.identity()
            or record.get("computer_grant_id") != grant["id"]
            or record.get("acknowledged_source_only") is not True
            or not isinstance(record.get("id"), str)
            or len(record["id"]) != 32
        ):
            return None
        return record
    except Exception:
        # Never disclose DPAPI payloads or Windows identity/credential errors.
        return None


def allows(agent, path, protected_roots):
    """Every overlapping protected root must have its own valid owner receipt."""
    try:
        for root in protected_roots:
            if not source_path(path, root) or not load(agent, root):
                return False
        return bool(protected_roots)
    except (OSError, ValueError):
        return False


def selected_root(agent, value):
    path = Path(value).expanduser().absolute()
    reject_links(path)
    if os.name == "nt" and (str(path).startswith("\\\\") or ":" in str(path)[2:]):
        raise PolicyError(
            "Select an absolute local source project without alternate data streams."
        )
    path = path.resolve()
    if (
        not path.is_dir()
        or path == path.parent
        or any(
            part.casefold() in PRIVATE_DIRS
            or is_data_container(part)
            or part.casefold() in PRIVATE_NAMES
            or part.casefold().startswith(PRIVATE_PREFIXES)
            for part in path.parts
        )
        or is_under(path, agent.home.resolve())
        or is_under(agent.home.resolve(), path)
        or is_under(Path.home().resolve(), path)
    ):
        raise PolicyError(
            "Select one protected source project, never private/data folders or a broad parent."
        )
    with agent.connect() as connection:
        protected = {
            Path(row[0]).resolve()
            for row in connection.execute("SELECT root FROM protected")
        }
    if path not in protected:
        raise PolicyError("The selected folder is not an exact protected project root.")
    return path


def enable_local(agent, root, input_stream=None, output_stream=None):
    incoming, outgoing = _console(input_stream, output_stream)
    path = selected_root(agent, root)
    grant = computer_access.load(agent)
    if not grant:
        raise PolicyError(
            "Enable computer access locally with udb --access computer first."
        )
    outgoing.write(
        f"Allow UDB source-file access in this protected project?\n{path}\n"
        "This permits reading and changing source/configuration files.\n"
        "Checkpoints, models, datasets, results, credentials and agent state stay blocked.\n"
        "Source-file recognition uses paths/extensions; it does not classify arbitrary text contents.\n"
        "Windows/UAC restrictions remain. No Desktop Commander is required.\n"
        "Type ALLOW SOURCE to confirm, or press Enter to cancel: "
    )
    outgoing.flush()
    if incoming.readline().strip() != "ALLOW SOURCE":
        raise PolicyError("Source access was not enabled.")
    record = {
        "policy": 1,
        "scope": "source-files-only",
        "id": uuid.uuid4().hex,
        "root": str(path),
        "home": str(agent.home.resolve()),
        "sid": computer_access.identity(),
        "computer_grant_id": grant["id"],
        "issued": time.time(),
        "acknowledged_source_only": True,
    }
    blob = _protect(json.dumps(record).encode("utf-8"))
    target = receipt_path(agent, path)
    target.parent.mkdir(exist_ok=True)
    reject_links(target)
    temporary = target.with_name(target.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with temporary.open("xb") as stream:
            stream.write(blob)
            stream.flush()
            os.fsync(stream.fileno())
        reject_links(target)
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)
    agent.audit(
        "source_access_enabled_locally",
        {"root": str(path), "scope": "source-files-only"},
    )
    return {"enabled": True, "root": str(path), "scope": "source-files-only"}


def revoke_local(agent, root, input_stream=None, output_stream=None):
    incoming, outgoing = _console(input_stream, output_stream)
    path = selected_root(agent, root)
    outgoing.write(
        f"Revoke UDB source access in {path}?\nType REVOKE SOURCE to confirm: "
    )
    outgoing.flush()
    if incoming.readline().strip() != "REVOKE SOURCE":
        raise PolicyError("Source access was not revoked.")
    receipt_path(agent, path).unlink(missing_ok=True)
    agent.audit("source_access_revoked_locally", {"root": str(path)})
    return {"enabled": False, "root": str(path)}


def command(home, root, revoke=False):
    # Check attendance BEFORE constructing Agent or touching any owner state.
    incoming, outgoing = _console()
    agent = Agent(Path(home))
    result = (revoke_local if revoke else enable_local)(
        agent, root, input_stream=incoming, output_stream=outgoing
    )
    outgoing.write(
        f"\nSource access {'disabled' if revoke else 'enabled'}: {result['root']}\n"
        "Restart UDB to load updated source: run udb stop, then udb.\n"
    )
    outgoing.flush()
    return 0
