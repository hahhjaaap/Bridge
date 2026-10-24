# Third-party components and notices

This release may bundle CPython 3.11 for Windows, the tunnel client and its license bundle, and packages pinned in `requirements.txt` and `requirements-gui.txt` (including MCP SDK, websocket-client, PySide6 Essentials and conditional pywin32). Build-only dependencies in `requirements-build.txt` include PyInstaller, Ruff and mypy.

The package builder collects license/copying/notice files from installed distribution metadata where available. CPython's license and the tunnel-client license bundle are staged under `vendor/`. The `tunnel-client-source.json` file records the official upstream release tag, published archive/license digests and hashes of the staged executable and license file; it does not replace independent publisher authentication.

This inventory is descriptive, not legal advice and not a determination that all redistribution conditions are satisfied. Before distribution, inspect `THIRD_PARTY_NOTICES/`, `vendor/python-runtime/LICENSE.txt` and `vendor/tunnel-LICENSES.txt`; confirm the applicable licenses, Qt obligations, attribution and source-offer/relinking requirements for the exact bundled builds. Do not distribute if required notices are missing or unclear.

The ZIP manifest records file hashes and sizes, but does not replace license review or publisher authentication.
