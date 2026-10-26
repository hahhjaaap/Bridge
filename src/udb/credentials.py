"""Local-only runtime credentials; never return secret text to MCP or logs."""

from __future__ import annotations

import uuid
from typing import Any

TARGET = "UniversalDesktopBridge/Tunnel"


def normalize_key(value: str) -> str:
    key = value.strip()
    if not key:
        raise ValueError(
            "API key is empty. Paste the key into the local credential field."
        )
    if any(ord(character) < 33 or ord(character) > 126 for character in key):
        raise ValueError(
            "The key contains a space, line break, or non-ASCII character. "
            "Copy the key again without quotes or explanatory text."
        )
    if len(key.encode("utf-16-le")) > 2560:
        raise ValueError(
            "Credential exceeds the Windows Credential Manager size limit."
        )
    return key


def save_key(
    value: str,
    backend: Any | None = None,
    target: str = TARGET,
) -> None:
    if backend is None:
        import win32cred as backend

    key = normalize_key(value)
    # pywin32 312 expects Unicode and performs the Windows encoding itself.
    backend.CredWrite(
        {
            "Type": backend.CRED_TYPE_GENERIC,
            "TargetName": target,
            "UserName": "CONTROL_PLANE_API_KEY",
            "CredentialBlob": key,
            "Persist": backend.CRED_PERSIST_LOCAL_MACHINE,
        },
        0,
    )


def load_key(backend: Any | None = None, target: str = TARGET) -> str:
    if backend is None:
        import win32cred as backend

    value = backend.CredRead(target, backend.CRED_TYPE_GENERIC)["CredentialBlob"]
    if isinstance(value, bytes):
        value = value.decode("utf-16-le")
    return normalize_key(value)


def self_test() -> None:
    """Exercise Windows Credential Manager without touching the runtime key."""
    import win32cred

    target = f"UniversalDesktopBridge/SelfTest/{uuid.uuid4().hex}"
    fake_key = "sk-synthetic-NOT-A-REAL-KEY_123-" * 15
    try:
        save_key(fake_key, target=target)
        if load_key(target=target) != fake_key:
            raise RuntimeError("Synthetic credential round-trip failed")
    finally:
        try:
            win32cred.CredDelete(target, win32cred.CRED_TYPE_GENERIC, 0)
        except win32cred.error:
            pass
