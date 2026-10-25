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
