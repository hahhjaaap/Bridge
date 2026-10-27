# Windows lifecycle (0.10.0)

Checks actually run on 2026-10-27:

- Final runtime source (`8c6a312d43eac0cbabaeaf2fdc41b954ce80997d`): Linux 658 passed, 10 skipped, 27 subtests; owner's Windows PC 656 passed, 12 skipped, 27 subtests. Ruff passed. The native run used a separate source review folder and synthetic/temporary homes.
- Recovery tests simulate short outages, continuous failed readiness, long monitor gaps, stale responses, stop/close races and failed child termination. They verify cancellation and no tool-call replay. These are simulations, not a physical sleep/network-interruption experiment on the owner's PC.
- Windows logon tests use an isolated registry key outside Run, execute the actual generated helper against a temporary offline home, verify its heartbeat, stop it and remove the helper/key. The real user's startup configuration is not changed by these tests. Commands stay within the Windows Run-key length limit; paths are literal and Unicode helpers include the PowerShell 5 BOM.
- A native frozen candidate from `51d2d4d0d84df1746f0e95e7aed806bfd520dff5` passed copied-EXE MCP/runtime/Office/lifecycle checks with Python/Node removed from PATH. Its HTTP installer test passed reinstall, transient 503 retry, running offline update, manual rollback and synthetic startup-failure rollback. This candidate predates the final short logon helper and updater-ancestry guard and is not the final release binary.
- The release pipeline builds the final source again, requires pytest/Ruff, frozen checks and the expanded installer scenarios before publication, then downloads and installs the public HTTPS release. Consult that release's Actions run for the final binary result.

No production credentials were read or created, no owner grants were issued, and AI VIET/data/training were not changed. ChatGPT webpage load failures and actual power/network changes remain outside these automated checks. Update/rollback must run outside the target agent's own job tree; otherwise stopping the agent would terminate the updater. This is reported before mutation.

# Validation of source consolidation (0.9.0)

This is the historical 0.9.0 record. See `AUDIT.md` for the current 0.9.1 audit and its own validation status.

Actually run in the Linux development environment:

- Full regression suite after the CLI compatibility fix: 329 passed, 5 skipped, 27 subtests passed. Native Windows cases invoked inside the component harness skip their native cases on Linux and are not Windows evidence.
- Source-only MCP subprocesses: initialize, discover 60 public tools / 66 implementations, call bridge_status, and reject a terminal request without owner computer access. No installed core EXE, connection key or grant was used.
- Source distribution and wheel built successfully. A fresh isolated virtual environment installed the wheel outside this checkout, checked package/legacy aliases and runtime assets, and reported version 0.9.0 / 60 tools without an external core EXE.

Actually run on the owner's Windows computer using an isolated installed 0.9.0 wheel and existing owner grants:

- Full regression suite: 326 passed, 5 skipped, 27 subtests passed. Native ConPTY and Windows process durability component programs ran through this suite.
- Real MCP UI Automation: standard Edit/Button providers, Unicode ValuePattern readback, InvokePattern changing the test form, missing-selector refusal and owned-form cleanup.
- Real MCP virtual pointer: overlay, button effect, right/double click, drag and horizontal wheel, invalid-coordinate refusal, fixture cleanup. The physical cursor remained unchanged during this run.
- Real MCP ConPTY stdin / VT output / resize / exit; interrupted copy recovery; duplicate resume returns the same job; saved terminal transcript survives a new backend and the lost process is not replayed.
- Real MCP XLSX write/read/hash edit/no-overwrite, DOCX replacement, Unicode PDF and path-scope enforcement.
- Deliberately terminated only test-owned core/Companion workers, including Windows venv redirector layers. Later read-only probes recovered the backends; kernel cleanup stopped the lost fixture terminal; its old session ID was refused. A dispatch/EOF race may return an explicit no-replay error before the next independent read recovers.
- Unified MCP smoke: initialization, 60-tool catalog, original/Companion forwarding, scoped file tools, guarded writes, legacy aliases and Unicode terminal round trip. Test files removed.

GitHub Actions built and ran the Windows frozen EXE (`--version`, `tools`). Linux Python 3.11/3.12 test, package build and clean-wheel jobs passed. After the CLI compatibility fix, Windows Python 3.11 and 3.12 each passed all 334 tests, built source/wheel and passed clean-wheel verification. Windows CI exposed a short-path-versus-resolved-path test expectation; the corrected expectation matches the CLI's resolved home. All five jobs in workflow run 40155025914 passed (Linux 3.11/3.12, Windows 3.11/3.12 and Windows EXE). Artifact upload is best-effort because account storage quota can prevent uploads independently of successful builds/tests.

Live deployment on the owner's computer also passed: the existing transport now runs the installed 0.9.0 source package; a real ChatGPT `bridge_status` returned core and Unified version 0.9.0, headless mode, 60 public / 66 backend tools, healthy backends and no Desktop Commander dependency. The existing manager/host worker was retained; owner capabilities (files, execute, desktop), endpoint identity and grants were unchanged. The user PATH launcher was updated to the same package/configuration and its readiness was checked. After the CLI compatibility fix, the actual PATH command passed all four forms: bare `udb`, `udb --wait 0 --json`, options before `status`, and options after `status`. A ConPTY process invoked directly through the connected ChatGPT tools printed Vietnamese/emoji and completed with exit code 0. The temporary Windows acceptance directory and development-only build/test packages were removed after verification. The first maintenance attempt rolled back after treating transient not-ready status as fatal; the updater was corrected and the second switch passed.

Not covered by this refactor's acceptance: a new Windows Sandbox session and a full attended connection setup from scratch. Existing grants/credentials were reused through normal application APIs, not recreated by the agent.

Core/Companion remain separate owned worker processes for failure isolation, launched by the same `udb` entry point. The installed-core backend is an explicit migration option, not a source-build dependency. Legacy payloads, RPC error codes and tool aliases remain; common status/version/no-replay metadata is additive.
