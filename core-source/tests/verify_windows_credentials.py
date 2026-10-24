"""Windows integration test with ONLY an isolated synthetic test credential."""
import sys
import uuid
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import win32cred
from udb.credentials import save_key, load_key

target = 'UniversalDesktopBridge/SelfTest/'+uuid.uuid4().hex
fake = 'sk-synthetic-NOT-A-REAL-KEY_123-'*15
try:
    save_key(fake, target=target)
    assert load_key(target=target) == fake
    print('SYNTHETIC_WINDOWS_CREDENTIAL_ROUNDTRIP=PASS')
finally:
    try:
        win32cred.CredDelete(target, win32cred.CRED_TYPE_GENERIC, 0)
    except win32cred.error:
        pass
