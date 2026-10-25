"""Local-only runtime credentials; never return secret text to MCP or logs."""
from __future__ import annotations

TARGET = 'UniversalDesktopBridge/Tunnel'

def normalize_key(value: str) -> str:
    key = value.strip()
    if not key:
        raise ValueError('API key is empty. Paste the runtime key into the API key field.')
    if any(ord(c) < 33 or ord(c) > 126 for c in key):
        raise ValueError('The key contains a space, line break, or non-ASCII character. Copy the key again without quotes or explanatory text.')
    if len(key.encode('utf-16-le')) > 2560:
        raise ValueError('Credential exceeds the Windows Credential Manager size limit.')
    return key

def save_key(value: str, backend=None, target=TARGET):
    if backend is None:
        import win32cred as backend
    key = normalize_key(value)
    # pywin32 312 expects Unicode here and performs the Windows encoding.
    # Passing encoded bytes raises "can not be converted to Unicode".
    backend.CredWrite({'Type': backend.CRED_TYPE_GENERIC,
        'TargetName': target, 'UserName': 'CONTROL_PLANE_API_KEY',
        'CredentialBlob': key, 'Persist': backend.CRED_PERSIST_LOCAL_MACHINE}, 0)

def load_key(backend=None, target=TARGET):
    if backend is None:
        import win32cred as backend
    value = backend.CredRead(target, backend.CRED_TYPE_GENERIC)['CredentialBlob']
    if isinstance(value, bytes):
        value = value.decode('utf-16-le')
    return normalize_key(value)

def self_test():
    """Exercise the real Windows backend without touching the runtime key."""
    import uuid
    import win32cred
    target = 'UniversalDesktopBridge/SelfTest/' + uuid.uuid4().hex
    fake = 'sk-synthetic-NOT-A-REAL-KEY_123-' * 15
    try:
        save_key(fake, target=target)
        if load_key(target=target) != fake:
            raise RuntimeError('Synthetic credential round-trip failed')
    finally:
        try:
            win32cred.CredDelete(target, win32cred.CRED_TYPE_GENERIC, 0)
        except win32cred.error:
            pass
