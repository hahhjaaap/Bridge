# Universal Desktop Bridge

One Python package (`udb`), one public CLI (`udb`), one source version. Core, Companion and isolated workers are built from this same source; a pre-existing core EXE is no longer required. Process isolation, plugin identity, endpoint configuration, owner grants and legacy tool names are preserved.

```powershell
python -m pip install ".[office]"
udb --version
udb tools
udb serve --home PATH_TO_EXISTING_UDB_HOME --root PATH_TO_APPROVED_PROJECT --enable-terminal
```

`udb` starts/reuses the attended Windows manager. `udb status`, `udb stop`, `udb doctor` and `udb test` use that same CLI. `python -m udb` is the equivalent entry point. Windows GUI/native operations require Windows; Linux supports development, source builds and protocol/policy tests.

Public runtime settings live in one optional TOML file (`udb.toml` in the selected home, or `--config FILE`). CLI values override it. See `udb.example.toml`. Credentials, project grants, journals and endpoint identity remain in existing private owner storage and must never be committed. Runtime configuration cannot grant permissions. `udb init` creates only a non-secret configuration, not grants or a connection.

Source core is the default. `core_backend = "installed"` with an explicit existing core executable is available for migration/rollback. The MCP template calls `udb serve`; callers no longer configure separate Python script/Companion paths. Existing `udb_unified.py`, `udb_companion.py` and module imports delegate through tiny compatibility shims, not copied implementations.

Tool counts come from `udb tools` by inspecting the actual registry. Current specialized tool names and same-backend aliases remain available. Native payloads are preserved; `_meta.udb` supplies a common version/status/no-replay contract. An `ERROR` or timeout must not trigger automatic replay of an uncertain mutation.

ConPTY streams VT UTF-8; it does not render a TUI screenshot. The virtual AI pointer uses bounded Win32 messages and a nonactivating overlay, not a second independent Windows user. Shared focus/capture and app compatibility limits remain. Recovery explicitly restarts supported unchanged reads/searches/create-only copies and restores saved terminal transcripts; it cannot resurrect a dead process's memory or replay uncertain writes.

## Build and test

```powershell
python -m pip install ".[office,dev]"
python -m pytest
python -m build
```

Windows EXE: install `.[office,freeze]`, then `udb build --exe`. The release directory contains the built executable, public templates and hashes. An existing connection may still need its existing tunnel client. No keys, grants, local state or vendor installation are bundled. CI builds source/wheel on Linux and Windows; native attended UI acceptance is separate from headless CI and must not be reported as passed merely because a workflow exists.

Legacy manual GUI/Sandbox acceptance helpers are in `tests/core`. New package/CLI tests cover clean installation, arbitrary working directories, one MCP catalog, compatibility imports, configuration and secret-free packaging. See `VALIDATION.md` for checks actually run for this change.

Attended MCP GUI acceptance can use the same TOML via `UDB_TEST_CONFIG`; no second test connection configuration is required. An existing legacy `mcp.json` is still accepted by the compatibility test helpers.

Artifact upload is best-effort and reports a warning if the account storage quota is exhausted; build/test failures still fail CI. Platform jobs run independently. A passing test run does not imply a downloadable artifact was stored.

## 0.9.1 audit fixes

The audit fixes CLI revocation routing and configuration initialization, rejects missing explicit configurations and canonicalizes equivalent home paths. Recovery/job setup failures release their slots and locks; reloaded job reads preserve paging and distinguish another live backend. Text reads retain the complete returned byte range, including trailing whitespace. Terminal stdout EOF no longer closes a live stdin channel; transcript failures after startup retain a controllable session ID and explicitly forbid command replay.

Scoped Companion paths now also exclude common credential variants and training/model directories. Private SQLite/HTTP token and recovery paths reject redirected storage. Process protection receives the bridge home explicitly for external projects. Invalid JSON-RPC request envelopes are rejected before dispatch. The frozen EXE includes its manifest template and CI exercises actual MCP worker startup, metadata commands and owner-grant denial. No existing tool name, endpoint, owner grant or plugin identity changes. See `AUDIT.md` for validation and limits.
