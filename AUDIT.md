# Project audit: UDB 0.9.1

Scope: the canonical package, legacy adapters, source path policy, owner CLI/configuration, terminal/process lifetime, durable jobs/recovery, proxy routing/protocol, document/desktop integration and build/CI assets. All Python source files were parsed, the full regression suite was run, and targeted failure-path reproductions were added. This is a bounded code review and test run, not proof that every possible bug has been eliminated.

## Confirmed defects and fixes

- The advertised `udb revoke-access` command was missing from the consolidated CLI. It now routes to the existing local revocation implementation; tests mock that implementation and never revoke/create real grants.
- Explicit missing TOML files silently fell back to defaults; equivalent `..`/Windows short paths could discard effective manager settings. Explicit paths now fail closed and configured paths are canonicalized after link checks. `init` writes the selected file and requested home/root/terminal/backend settings, create-only.
- Failed lease acquisition, journal save, setup callback or worker-thread startup left ghost running jobs/held leases. Setup failures now release capacity and remove their unstarted records. Reloaded job reads apply result paging and check live ownership before reporting interruption.
- Long text reads advanced the byte offset past discarded text. Responses now retain the complete bounded content, including final whitespace, and expose the original structured read result.
- A terminal output EOF could stop its input writer while the process still waited for stdin. Output/input lifetimes are now independent. If transcript persistence fails after process startup, the response retains the session ID and warns against replay; the live process stays inspectable/stoppable.
- `.udb-recovery`, private state/database and legacy HTTP token paths could follow redirected storage. These paths now reject links/reparse points/hardlinked private files as applicable.
- Scoped Companion paths exposed common `.env.*`/credential variants and training/model directories. The filter now blocks those components consistently for read/write/search. This does not pretend to sandbox the separately owner-enabled arbitrary command capability.
- Process-install protection guessed the installation from the project path. The proxy now passes the actual bridge home to Companion so external project locations do not remove that protection.
- Invalid JSON-RPC IDs/version/method shapes could reach dispatch. The proxy now rejects those request envelopes before forwarding or starting a request thread.
- PyInstaller omitted `plugin.template.json`, so the frozen manifest command could fail even while `tools` passed. The asset is included and CI now exercises CLI metadata plus a real isolated MCP worker tree with no grant/core EXE.

## Validation

- Linux full suite before the final whitespace assertion: 370 passed, 5 skipped, 27 subtests passed. The added audit module has 41 cases; before fixes, targeted runs reproduced the defects in the original 0.9.0 source.
- Standalone acceptance checker was exercised against the source console entry point. Source/wheel build and isolated installation are checked separately.
- Windows native tests, PowerShell parsing, credential self-test, attended MCP workflows, frozen EXE acceptance, GitHub CI and live deployment: pending for this revision. Previous 0.9.0 results are not evidence for 0.9.1.

No new Windows Sandbox VM, UAC/elevation workflow or fresh attended grant/connection setup is asserted by this audit. Real credentials/owner state must stay out of source, test fixtures and release archives.
