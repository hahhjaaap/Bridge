"""Stage a deliberately non-runnable vendor payload for CI archive tests only.

Never distribute the resulting ZIP. Real release builds must use Prepare-Vendor.py
with a verified local CPython runtime and the official tunnel client's license bundle.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from udb.release import sha256  # noqa: E402


def main() -> None:
    vendor = ROOT / "vendor"
    if vendor.exists():
        raise SystemExit("Refusing to replace an existing vendor directory")
    payloads = {
        "vendor/python-runtime/python.exe": b"CI-SYNTHETIC-NOT-RUNNABLE-python",
        "vendor/python-runtime/python3.dll": b"CI-SYNTHETIC-NOT-RUNNABLE-python3",
        "vendor/python-runtime/python311.dll": b"CI-SYNTHETIC-NOT-RUNNABLE-python311",
        "vendor/python-runtime/vcruntime140.dll": b"CI-SYNTHETIC-NOT-RUNNABLE-vcruntime140",
        "vendor/python-runtime/vcruntime140_1.dll": b"CI-SYNTHETIC-NOT-RUNNABLE-vcruntime140_1",
        "vendor/python-runtime/LICENSE.txt": b"Synthetic test fixture only",
        "vendor/python-runtime/python311._pth": b"Lib\n.\nDLLs\n",
        "vendor/python-runtime/DLLs/_ssl.pyd": b"CI-SYNTHETIC-NOT-RUNNABLE-ssl",
        "vendor/python-runtime/Lib/encodings/__init__.py": b"# synthetic test fixture\n",
        "vendor/python-runtime/Lib/os.py": b"# synthetic test fixture\n",
        "vendor/tunnel-client.exe": b"CI-SYNTHETIC-NOT-RUNNABLE-tunnel",
        "vendor/tunnel-LICENSES.txt": b"Synthetic test fixture; not a real license bundle",
    }
    for relative, content in payloads.items():
        path = ROOT / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    records = {
        name: {"sha256": sha256(content), "bytes": len(content)}
        for name, content in payloads.items()
    }
    (vendor / "DEPENDENCIES.json").write_text(
        json.dumps({
            "python_version": "Python 3.11.9",
            "origin": "CI synthetic non-runnable fixture; not for distribution",
            "files": records,
        }, sort_keys=True, indent=2),
        encoding="utf-8",
    )
    print("SYNTHETIC_VENDOR_READY_NOT_INSTALLABLE")


if __name__ == "__main__":
    main()
