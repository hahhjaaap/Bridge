# Universal Desktop Bridge

## Install on Windows x64 with one command

Run this in your own PowerShell terminal:

~~~powershell
irm https://github.com/hahhjaaap/Universal-Desktop-Bridge/releases/latest/download/install.ps1 | iex
~~~

The installer downloads and verifies the standalone release, installs under your own LocalAppData, adds `udb` to your user PATH, and starts console setup. No Python, pip, Node.js, Git or Desktop Commander installation is required. It does not require administrator access or a reboot. The current terminal receives the command immediately; reopen older terminals if their PATH is stale.

First-run setup can start the local runtime immediately in offline mode. Connecting ChatGPT requires your own supported tunnel/account configuration; the installer does not create an account, mint a tunnel, or share the developer's credentials. Runtime keys are entered hidden in the attended local terminal and stored in Windows Credential Manager. Computer access remains an explicit local owner decision.

Use `udb start` as the normal entry point. In an attended Windows terminal, a first start without a saved connection opens setup automatically. Later starts reuse your saved online/local choice and attach live logs in the same console. An already running agent is reused in its current mode. `udb start --json`, `udb start --detach`, and redirected invocations never enter automatic setup. An offline agent is not a ChatGPT connection.

For local stdio MCP clients, the installer writes `%LOCALAPPDATA%\Programs\UDB\mcp.json`. Data is kept separately under `%LOCALAPPDATA%\UniversalDesktopBridge`. Updates retain this data and restart a previously running agent in its actual online/local mode. SHA256 provides integrity checks; the release is not code-signed.

### Everyday commands

| Command | Result |
| --- | --- |
| `udb start` | Reuse the agent and saved connection mode, or guide the first attended setup. |
| `udb setup --edit` | Re-enter the Tunnel ID/key locally, then restart the agent to apply the connection. Enter at the ID prompt keeps the current ID; an empty key cancels without replacing it. |
| `udb setup --local` | Switch to local mode and remember that choice for future starts. |
| `udb setup` | Connect or return to the saved ChatGPT connection. |
| `udb doctor` / `udb doctor --json` | Check runtime integrity, helpers, saved mode, agent heartbeat, local tunnel, installation and logon startup; print specific repair commands. Keys are never read. |
| `udb autostart enable` | Opt into hidden startup when this Windows user signs in, after setup. No admin or new computer permission is required. |
| `udb autostart disable` / `udb autostart status` | Remove or inspect only this home's logon entry. Disabling leaves an already running agent alone. |
| `udb update --check` | Check the latest stable public GitHub release without installing. |
| `udb update` | Update the managed installation using a pinned release URL and verified package inventory. Keep the previous release. |
| `udb rollback` | Restore the retained previous managed release and its running mode; keep current owner data and connection settings. |

Logon startup is off until explicitly enabled. It runs without a GUI or visible log window; `udb start` attaches logs later. It starts at sign-in, not before sign-in, and does not wake a sleeping PC. A managed startup entry uses the stable launcher across updates and rollback. Re-enable it after relocating a portable/source installation. Use `udb start --online` or `--offline` to override the saved choice for that start; stop an agent in another mode first.

The connection watchdog already retries exited tunnel processes with a 1–30 second backoff. It also replaces its own tunnel child after 90 seconds of continuous failed readiness checks; a long monitor gap (including suspend/resume) starts a fresh grace period. Stop/close cancels recovery. Reconnecting can end existing MCP sessions: unknown writes and terminal commands are never automatically replayed. A successful local readiness check cannot diagnose or repair a ChatGPT webpage failing to load a conversation.

Update and rollback require the one-command managed layout and must run outside the target agent's own tool jobs, normally in your terminal. Stopping the agent would also stop an updater inside its owned job; `udb update --check` is safe remotely. A portable installation remains supported; install the managed layout to use these commands, stopping its old agent before reusing the same data home. Updates retain the previous release and atomically switch the launcher record. If activation fails, the installer restores the old record/MCP configuration and attempts to restart the old agent. A network outage with a live agent triggers reconnect retries instead of rolling back code. Owner data is shared across releases and is not reverted; keep independent backups. An interrupted/timed-out update has an unknown outcome: inspect `udb --version` and `udb status` before retrying.

Setup stores only the selected online/local mode in its startup preference file. This setting grants no computer permissions. Existing owner access remains separate and is reused; keys are never accepted as CLI arguments or echoed. Connection editing and setup require an attended local terminal.

### Connect your ChatGPT workspace

Run `udb setup` in your own Windows terminal. The console shows the official setup pages and lets you:

1. Create or reuse a tunnel in the intended organization and ChatGPT workspace at https://platform.openai.com/settings/organization/tunnels.
2. Paste either its full Tunnel ID or its `https://api.openai.com/v1/mcp/...` URL. Legacy IDs and four-character namespace aliases are supported. Create a restricted runtime key with Tunnels Read + Use at https://platform.openai.com/settings/organization/api-keys, then enter it in the hidden local prompt.
3. Keep UDB running, open https://chatgpt.com/#settings/Connectors, add or finish the private MCP connection using **Connection: Tunnel**, select that same tunnel, and verify a `bridge_status` tool call.

Enter `?` at the Tunnel ID prompt to repeat the links. An empty Tunnel ID or runtime-key response cancels to local mode without saving a partial connection. Existing valid connections are reused without asking for their key again.

Tunnel provisioning and permissions depend on your OpenAI account/workspace. If the Tunnels page is unavailable, ask its administrator or use the generated local stdio MCP configuration; the installer cannot enable the service or supply an automatic account-pairing flow. Local tunnel readiness alone does not prove that ChatGPT has connected.

Developer acceptance: `python tools/check_installer.py install.ps1 dist/udb-0.10.0-windows-standalone.zip` exercises actual HTTP downloads, running-agent updates, explicit rollback and a synthetic startup-failure rollback in disposable folders with a system-only PATH. It does not change the real user PATH, grant access, or configure a live account.

One Python package (`udb`), one public CLI (`udb`), one source version. Core, Companion and isolated workers are built from this same source; a pre-existing core EXE is no longer required. Process isolation, plugin identity, endpoint configuration, owner grants and legacy tool names are preserved.

```powershell
python -m pip install ".[office]"
udb --version
udb tools
udb serve --home PATH_TO_EXISTING_UDB_HOME --root PATH_TO_APPROVED_PROJECT --enable-terminal
```

`udb` starts/reuses the attended Windows manager. `udb status`, `udb stop`, `udb doctor` and `udb test` use that same CLI. `python -m udb` is the equivalent entry point. Windows GUI/native operations require Windows; Linux supports development, source builds and protocol/policy tests.

Public runtime settings live in one optional TOML file (`udb.toml` in the selected home, or `--config FILE`). CLI values override it. See `udb.example.toml`. Credentials, project grants, journals and endpoint identity remain in existing private owner storage and must never be committed. Runtime configuration cannot grant permissions. `udb init` creates only a non-secret configuration, not grants or a connection.

Source core is the default. `core_backend = "installed"` with an explicit existing core executable is available for migration/rollback. The MCP template calls `udb serve`; callers no longer configure separate Python script/Companion paths. Existing worker entry scripts (`udb_unified.py`, `udb_companion.py`, `udb_documents.py`, `udb_background.py`) delegate through tiny compatibility shims. Import-only aliases live once under `src` and remain available in installed packages.

### Regenerate an MCP host configuration

After moving or reinstalling UDB, run udb mcp-config --home PATH_TO_UDB_HOME --root PATH_TO_APPROVED_PROJECT. It prints a ready-to-copy stdio server entry using the current interpreter and effective runtime settings. Absolute paths are intentional: MCP hosts do not consistently expand environment variables or resolve relative executable paths. This command only prints configuration; it does not modify a host manifest, create grants, or change permissions. Re-run it after relocating the installation.

Companion responsibilities are separated under `src/udb/companion`: `files.py` owns scoped file access, `terminal.py` owns terminal I/O, `schemas.py` owns the tool catalog, and `runtime.py` owns orchestration and stdio dispatch. Extensions use explicit dispatch; importing the catalog does not patch runtime classes. Legacy `Runtime`, `ScopedFiles`, `TerminalSession`, `TOOLS` and helper imports remain available through `udb_companion`.

Tool counts come from `udb tools` by inspecting the actual registry. Current specialized tool names and same-backend aliases remain available. Native payloads are preserved; `_meta.udb` supplies a common version/status/no-replay contract. An `ERROR` or timeout must not trigger automatic replay of an uncertain mutation.

ConPTY streams VT UTF-8; it does not render a TUI screenshot. The virtual AI pointer uses bounded Win32 messages and a nonactivating overlay, not a second independent Windows user. Shared focus/capture and app compatibility limits remain. Recovery explicitly restarts supported unchanged reads/searches/create-only copies and restores saved terminal transcripts; it cannot resurrect a dead process's memory or replay uncertain writes.

## One-console startup and parallel work

Run `udb start` in CMD: UDB starts/reuses its headless agent and follows live tool activity in the same console. Status lines use 📞 CALL, ✅ PASS, ⏳ RUNNING and ❌ ERROR when the terminal supports Unicode; other terminals receive plain-text labels. Ctrl+C closes only the viewer. Use `udb start --detach` to return to the prompt; `--json` stays nonblocking and machine-readable. `--follow` explicitly attaches a redirected stream.

The proxy accepts at most 10 concurrent RPC calls. Computer commands, Companion terminals and file/document/search background tasks share 10 crash-safe work slots per bridge home. When full, a new launch is rejected before it starts; read/stop calls remain available. CPU/RAM admission checks, per-process memory/output limits and platform-specific browser/desktop serialization remain in force. Ten is an upper bound, not a guarantee that ten heavy workloads fit in memory. ChatGPT decides which independent tool calls to submit concurrently.

## Live activity log

Run `udb logs` in a terminal to follow tool calls with timestamps, status and duration. `udb logs --details` includes safe argument/result metadata; `udb logs --once --tail 100 --json` prints a snapshot. Ctrl+C closes only the viewer, leaving UDB running. The latest 2,000 events are retained. Log viewing does not activate permissions or require a GUI.

Source text, shell arguments, terminal output, file contents and credentials are omitted. `RUNNING` means a tool returned while its background task remains pending; the job's eventual result appears when a subsequent read/status call reports it. Activity logging is best-effort and never replays a failed call.


## Browser automation

The optional host browser is a separately launched, headless Chrome/Edge profile. It never attaches to the user's existing profile or tabs and does not use the operating-system mouse or keyboard. Enable host background mode locally before use; the resource governor can pause new browser work when the machine is under pressure.

Recommended flow:

1. Call host_start_session with kind="browser" and an authorized task.
2. Call host_browser with action="new" and a public HTTP(S) URL.
3. Call host_browser with action="snapshot" to inspect bounded visible controls, roles, accessible names and form state.
4. Use host_browser action="click" with role="button" and name="Search", or a CSS selector when a semantic locator is unavailable.
5. Use host_browser action="wait_for" with role="heading", name="Results", state="visible" and timeout_ms=5000 instead of blindly repeating a click after navigation.
6. Use press for an allowlisted key, select_option for native select controls, and check/uncheck for checkbox/radio controls. back, forward and reload operate only on the owned tab.

Semantic locators require a unique visible match. exact=true is the default; set it false for a bounded substring match, and inspect the result before acting. wait_for states are attached, visible, hidden, enabled and text, with a 100..10000 ms timeout. Snapshot output is bounded to 160 controls and redacts password, file and token-like field values. CSS selectors remain supported for compatibility.

Browser file upload/download and attaching to the user's existing browser profile remain disabled. This keeps the private profile and filesystem boundary explicit; do not try to bypass it with page JavaScript. Page content is untrusted and cannot authorize external actions.

## Standalone Windows application

A frozen release is one `udb.exe` with its Python runtime, UDB workers and Python libraries embedded. Runtime use does not require Python, pip, Node.js, a source checkout or Desktop Commander. Public helper binaries can be embedded in the same executable through a checksum-verified `build-assets/components.json` inventory. Inventory entries have fixed helper names/paths, release provenance and accompanying license notices; arbitrary files and owner state are not packaged.

Build-machine command:

```powershell
python -m pip install ".[office,freeze]"
python -m udb build --exe --assets ./build-assets --output ./dist/standalone
```

The output contains the EXE and a binary-only ZIP with licenses/notices and hashes. Build logs, source files, credentials, state and grants are excluded from that ZIP. `udb.exe runtime --probe` reports the embedded runtime, Office parser availability and helper startup checks.

```powershell
.\udb.exe gui
# Or run the local agent without a network connection:
.\udb.exe start --offline
.\udb.exe status --json
.\udb.exe stop
```

Use an existing `--home` to retain that installation's settings. Stop an existing source manager with its current launcher before starting the EXE with the same home. The frozen app launches its own embedded MCP workers even when an old manifest points at a development venv. Windows/UAC and local owner consent remain enforced. No permission is created by building or installing the app.

ChatGPT connectivity still requires network/account configuration and a supported transport service. Windows Sandbox plus its configured guest runtime and an installed supported browser remain optional platform features. Media helpers need no PATH installation when they are bundled. User-selected programs still have to exist. These are feature/platform requirements, not a dependency on another desktop bridge.

## Build and test

```powershell
python -m pip install ".[office,dev]"
python -m pytest
python -m build
```

Windows EXE: install `.[office,freeze]`, then `udb build --exe`. The release directory contains the built executable, public templates and hashes. An existing connection may still need its existing tunnel client. No keys, grants, local state or vendor installation are bundled. CI builds source/wheel on Linux and Windows; native attended UI acceptance is separate from headless CI and must not be reported as passed merely because a workflow exists.

Component and manual MCP acceptance programs are in `tests/components`; the default suite runs its selected component programs in subprocesses. Their UIA fixture is in `tests/fixtures`. Legacy manual GUI/Sandbox acceptance helpers are in `tests/core`. New package/CLI tests cover clean installation, arbitrary working directories, one MCP catalog, compatibility imports, configuration and secret-free packaging. See `VALIDATION.md` for checks actually run for this change.

Attended MCP GUI acceptance can use the same TOML via `UDB_TEST_CONFIG`; no second test connection configuration is required. An existing legacy `mcp.json` is still accepted by the compatibility test helpers.

Artifact upload is best-effort and reports a warning if the account storage quota is exhausted; build/test failures still fail CI. Platform jobs run independently. A passing test run does not imply a downloadable artifact was stored.

## 0.9.1 audit fixes

The audit fixes CLI revocation routing and configuration initialization, rejects missing explicit configurations and canonicalizes equivalent home paths. Recovery/job setup failures release their slots and locks; reloaded job reads preserve paging and distinguish another live backend. Text reads retain the complete returned byte range, including trailing whitespace. Terminal stdout EOF no longer closes a live stdin channel; transcript failures after startup retain a controllable session ID and explicitly forbid command replay.

Scoped Companion paths now also exclude common credential variants and training/model directories. Private SQLite/HTTP token and recovery paths reject redirected storage. Process protection receives the bridge home explicitly for external projects. Invalid JSON-RPC request envelopes are rejected before dispatch. The frozen EXE includes its manifest template and CI exercises actual MCP worker startup, metadata commands and owner-grant denial. No existing tool name, endpoint, owner grant or plugin identity changes. See `AUDIT.md` for validation and limits.

## General computer access

UDB is independent of project names and Windows account names. With owner-enabled computer access, `computer_files` accepts absolute local paths across the current account's accessible folders; it is not limited to `project_root`. The project/Companion tools retain an optional scoped workspace for source and isolated jobs. Windows/UAC limits, private agent state and explicitly protected roots remain enforced.

Use `udb --access computer` once in your own attended terminal to enable general computer access. No special application or personal folder is automatically registered, granted access or placed on the protected-root list. Existing owner configuration and protection records are preserved.

Installation discovery uses `UDB_HOME` first, then an existing portable installation identified by its configuration and manifests. A standalone installation defaults to `%LOCALAPPDATA%\\UniversalDesktopBridge` on Windows or the current account's XDG data directory elsewhere. Scoped workspaces default to `projects/workspace`; an explicit `project_root` remains unchanged.

## Optional source access in a protected project

Computer access and protected-folder access are separate. To allow only source/configuration files in a protected project, run this yourself in an attended Windows terminal:

```powershell
udb allow-source --path "PATH_TO_PROTECTED_SOURCE_PROJECT"
# Type ALLOW SOURCE at the local prompt, then reload the updated backend:
udb stop
udb
```

The root stays protected. Only recognized source files and traversable source directories receive an exception; model/checkpoint/data/results directories, credentials, agent state and the private installation stay denied. Classification uses file names and extensions, not content inspection. The encrypted receipt belongs to this Windows user, installation and existing computer-access grant. Revoking computer access invalidates these exceptions; enabling it again does not restore old source receipts. `udb revoke-source --path PATH` revokes one exception locally. No MCP tool, redirected input or noninteractive switch can issue a receipt. A source exception does not register a project or change its separate source-write/Sandbox grants. Desktop Commander is not involved.

## Runtime efficiency

Terminal checkpoints copy only the final 64 KiB of retained output. Terminal admission and shutdown share a lock, while waiting for terminal output leaves that lock free. Closing a runtime waits for its monitor and refuses later process launches.

The headless status heartbeat is written once per second; local stop requests are still checked every 300 ms. The live viewer uses one read-only SQLite connection per poll and recovers if the log is recreated. The ten-call and shared ten-background-job limits remain unchanged.

Run `python tools/benchmark_runtime.py` from a development checkout to measure these local hot paths with synthetic data. Its JSON output covers checkpoint allocation/time, idle log connections, and simulated heartbeat writes. It does not measure network, model, browser, or end-to-end task latency.
