# Universal Desktop Bridge — Windows companion core

**Core version:** `0.7.0-dev.1` (development preview)

This directory builds the standalone Windows desktop manager and its EXE. It is separate from the repository-root MCP proxy, whose manifest is maintained at `../plugin.json`. The proxy and EXE have independent version numbers and release checks. The core release ZIP deliberately excludes the root proxy's `plugin.json` and `MCP_CONFIGURATION.example.json`.

## Build

Use a clean checkout, trusted Windows x64 CPython 3.11, and the pinned build requirements:

```powershell
python -m pip install -r requirements-build.txt
python Prepare-Vendor.py --python-home C:\Python311 --tunnel .\tools\tunnel-client.exe --tunnel-licenses .\tools\tunnel-LICENSES.txt
python Build-Release.py --revision "YOUR_GIT_COMMIT_SHA" --output .\release-output
python Verify-Package.py .\release-output\Universal-Desktop-Bridge-0.7.0-dev.1-Windows.zip
```

Replace example paths and versioned filenames with verified local values. `Prepare-Vendor.py` does not download dependencies: supply a trusted local CPython runtime, tunnel client and its license bundle after checking their source and digest. Build fails before PyInstaller if the offline runtime is missing, linked or inconsistent with its dependency inventory.

## Install and rollback

Extract the verified ZIP to a new folder. Run `Install.ps1 -VerifyOnly` first, then `Install.ps1 -NoLaunch` for an attended installation. The default destination is under the current Windows user's Local AppData. Review the scripts before running them. `Rollback.ps1` restores the previous installed EXE release.

The installer does not grant new owner permissions or package project files. Keep runtime state, grants, tokens and project data local; never commit them.

## Security boundaries

SHA-256 hashes detect corruption but do not authenticate the publisher. This preview is not code-signed and is not production-certified. The package builder rejects incomplete runtime inputs, linked files, unexpected paths, common secret signatures and nested ZIPs. Test installation, rollback, owner consent and end-to-end MCP behavior in a disposable Windows environment before distribution. A dispatched UI or pointer action does not prove that an application accepted it.
