# Project audit: UDB 0.9.1

Scope: the canonical package, legacy adapters, source path policy, owner CLI/configuration, terminal/process lifetime, durable jobs/recovery, proxy routing/protocol, document/desktop integration and build/CI assets. All Python source files were parsed, the full regression suite was run, and targeted failure-path reproductions were added. This is a bounded code review and test run, not proof that every possible bug has been eliminated.

## Confirmed defects and fixes

- The advertised `udb revoke-access` command was missing from the consolidated CLI. It now routes to the existing local revocation implementation; tests mock that implementation and never revoke/create real grants.
- Explicit missing TOML files silently fell back to defaults; equivalent `..`/Windows short paths could discard effective manager settings. Explicit paths now fail closed and configured paths are canonicalized after link checks. `init` writes the selected file and requested home/root/terminal/backend settings, create-only.
- Failed lease acquisition, journal save, setup callback or worker-thread startup left ghost running jobs/held leases. Setup failures now release capacity and remove their unstarted records. Reloaded job reads apply result paging and check live ownership before reporting interruption.
- Long text reads advanced the byte offset past discarded text. Responses now retain the complete bounded content, including final whitespace, and expose the original structured read result.
- A terminal output EOF could stop its input writer while the process still waited for stdin. Output/input lifetimes are now independent; output EOF alone is not recorded as process completion. If transcript persistence fails after process startup, the response retains the session ID and warns against replay; the live process stays inspectable/stoppable.
- `.udb-recovery`, private state/database and legacy HTTP token paths could follow redirected storage. These paths now reject links/reparse points/hardlinked private files as applicable.
- Scoped Companion paths exposed common `.env.*`/credential variants and training/model directories. The filter now blocks those components consistently for read/write/search. This does not pretend to sandbox the separately owner-enabled arbitrary command capability.
- Process-install protection guessed the installation from the project path. The proxy now passes the actual bridge home to Companion so external project locations do not remove that protection.
- Invalid JSON-RPC IDs/version/method shapes could reach dispatch. The proxy now rejects those request envelopes before forwarding or starting a request thread.
- PyInstaller omitted `plugin.template.json`, so the frozen manifest command could fail even while `tools` passed. The asset is included and CI now exercises CLI metadata plus a real isolated MCP worker tree with no grant/core EXE.

## Validation

- Linux full suite: 371 passed, 5 skipped, 27 subtests passed. The added audit module has 42 cases; before fixes, targeted runs reproduced the defects in the original 0.9.0 source.
- Source/wheel builds passed. A fresh isolated virtual environment installed the 0.9.1 wheel outside the checkout and verified version 0.9.1 / 60 tools without an external core EXE. All 64 package Python files parsed; the source distribution includes the audit, regression module, frozen checker and UI fixture.
- The first CI run built and exercised the frozen Windows EXE successfully, including its manifest and actual MCP workers. It exposed two test-fixture assumptions: platform newline translation and OS pipe capacity. The fixtures now use exact bytes and a synchronized blocked writer, without weakening the byte-integrity/backpressure assertions. The final GitHub Actions run 40156645683 passed all five jobs: Linux Python 3.11/3.12, Windows Python 3.11/3.12, and Windows EXE. Each Windows version passed all 376 tests, source/wheel build and clean-wheel validation. The frozen checker verified version/manifest/build metadata, 60 tools, real MCP worker startup, and denial without owner computer access.
- The existing user-computer connection returned `Session terminated` before native deployment. Direct PowerShell parsing, credential self-test, attended GUI workflows and live 0.9.1 deployment have not run; previous 0.9.0 results are not evidence for this revision.

No new Windows Sandbox VM, UAC/elevation workflow or fresh attended grant/connection setup is asserted by this audit. Real credentials/owner state must stay out of source, test fixtures and release archives.

CI evidence: https://github.com/hahhjaaap/Bridge/actions/runs/40156645683. Artifact upload remains quota-limited and best-effort; successful tests/builds do not imply that GitHub stored downloadable binaries.

## Follow-up maintenance (2026-10-25)

- Added the mcp-config command to regenerate a host-specific stdio configuration from the current interpreter and effective UDB settings. Absolute paths remain intentional for each installation; the protected installation-root mcp.json is not rewritten by the general computer-file API.
- Media download User-Agent now derives from the package's single version constant instead of a stale 0.3.2 string, with a regression test.
- Windows taskkill.exe, powershell.exe, and shutdown.exe launches now resolve from the Windows system directory rather than PATH. The guest worker contains its own resolver because its source is copied into Windows Sandbox as a standalone payload.
- Enabled Ruff Bugbear (B), async (ASYNC), and Bandit S607 checks by default. Fixed unused loop bindings, callback closure binding, exception chaining/sanitization, and synchronous path/file-open operations inside the async acceptance probe.
- Reviewed broad exception suppression in desktop focus, gesture cleanup, tunnel retry, browser close, optional resource metrics, revocation cleanup, status reporting, mailbox polling, and cosmetic DWM styling. These remain best-effort or fail-safe paths; comments now explain why failures are suppressed rather than masking the original operation.

- Post-change Windows validation: full pytest suite passed with 501 passed, 10 skipped, and 27 subtests passed; the focused CLI/media/UIA suite passed with 42 passed and 1 skipped. Ruff, Python compilation, source/wheel build, and wheel-content/version checks passed. The first full-suite run exposed an outdated UIA test expectation for the now-absolute PowerShell path; the test was updated to validate an absolute executable path, and the full suite then passed.

## Performance optimization pass (2026-10-25)

- Terminal recovery snapshots now keep a per-session signature and skip chunk joining, base64 encoding, and journal serialization when output position, retained chunk count, completion state, exit code, reason, and mode are unchanged. Cache entries are removed when sessions are evicted.
- Background work uses checkpoint versions. Search matches/progress, copy progress, cancellation requests, commit transitions, and terminal task outcomes mark a task dirty. Periodic checkpointing persists only dirty tasks; explicit initial, pre-commit, post-commit, and final persistence remains in place.
- Qt manager refresh now computes the source-autonomy project set once per refresh instead of revalidating it once per displayed project. Project grant checks run on project/selection changes and at most every 10 seconds otherwise. Job panes avoid resetting identical text.
- DesktopController.status now calls the lightweight Sandbox availability check directly instead of rebuilding Agent.diagnostics (which performs additional host, resource, grant, and project checks) every UI poll.
- Synthetic microbenchmarks (not whole-application speed estimates): 1,000 unchanged 64 KiB terminal snapshots fell from a 174.37 ms median to 0.952 ms; 100 unchanged background checkpoints with 2,000 result rows fell from a 184.22 ms median to 0.044 ms.
- A separate status-poll microbenchmark (100 calls, 5 runs) fell from a 475.38 ms median when each call rebuilt full diagnostics to 2.96 ms with the lightweight status check (about 160x faster for that synthetic path).
- Final validation after these changes: 505 passed, 10 skipped, 27 subtests passed; Ruff and Python compilation passed; pip check reported no broken requirements; GUI self-test passed in offscreen mode after the status optimization; wheel and source distribution built successfully; generated MCP config parsed and referenced the current interpreter; the registry exposed 60 unique public tool names at version 0.9.1.

## UI module split follow-up (2026-10-25)

- Extracted the 271-line MainWindow.refresh method into udb/manager_refresh.py as ManagerRefreshMixin; MainWindow retains its class name and method behavior. The footer reuses the permission snapshot already obtained during refresh.
- Extracted the 159-line GUI integration self-test into udb/manager_selftest.py. udb.manager.self_test() remains as a lazy compatibility wrapper, so importing the UI module is no longer required just to import the self-test implementation.
- Added tests/test_manager_refresh.py. Its Qt imports are intentionally lazy to preserve headless-test isolation.
- manager.py decreased from 1,743 to 1,316 lines (427 fewer lines, about 24.5%); the extracted logic is now in focused modules.
- Final verification after the refactor: 506 passed, 10 skipped, 27 subtests passed; Ruff, compileall, GUI self-test, pip check, wheel/sdist build, package version and 60-name tool registry checks all passed. Both new modules are present in the built wheel. Version remains 0.9.1.

