# Universal Desktop Bridge (UDB)

**Windows-first MCP bridge for ChatGPT and other supported MCP clients.** UDB provides one Python package, one `udb` CLI and **59 public tools** for guarded local computer work. Its source of truth is `src/udb/`; the root-level `bridge.py` and `udb_*.py` files are compatibility launchers, not separate implementations.

**Tiếng Việt:** UDB kết nối trợ lý AI với máy tính Windows theo quyền do chủ máy cấp. Hỗ trợ thao tác tệp, terminal, ứng dụng, trình duyệt riêng biệt và rà soát hóa đơn Excel. **Không tự cấp quyền, không tự kê khai thuế, không tự sửa dữ liệu kế toán gốc.**

> **Release status (2026-10-27):** The `main` source version is **0.10.1**. The latest published stable Windows release is **v0.10.0** until a newer release is built and verified. The installer below installs the *latest published release*, not automatically the newest `main` commit. Check [Releases](https://github.com/hahhjaaap/Universal-Desktop-Bridge/releases) before updating a working installation.

## What UDB does

| Area | Supported functionality | Important boundary |
| --- | --- | --- |
| Files and terminals | Scoped reads, guarded writes, search, background jobs, Windows terminal/ConPTY | Owner permissions, protected paths, quotas and no uncertain-write replay |
| Desktop and applications | Owner-enabled Windows UI and process operations | Windows session/UAC limits; local owner consent |
| Browser | Isolated headless Chrome/Edge, accessible snapshots, semantic locators, waits and navigation | Own profile, local login handoff; explicit same-origin file transfers require separate file access |
| Accounting work tools | Read-only Vietnamese XLSX invoice audit and a **new** review workbook | No original-file overwrite, tax filing, MISA posting or legal tax determination |
| Operations | Activity logs, status/doctor, up to 10 concurrent RPC calls and 10 shared background work slots | Capacity is a limit, not a promise of 10 simultaneous heavy workloads |
| Recovery | Saved job state, safe read/recovery paths and managed installer rollback | Uncertain mutations are **never** automatically replayed |

The local HTTP MCP boundary requires authorization, accepts loopback clients only, rejects ambiguous authentication headers and bounds request bodies. These controls do not protect against a compromised Windows account or malicious authorized client. See [ARCHITECTURE.md](ARCHITECTURE.md) for ownership, dispatch and isolation.

## Windows x64: install and start

Review the installer and [published release](https://github.com/hahhjaaap/Universal-Desktop-Bridge/releases/latest) before executing downloaded code. In your own PowerShell terminal:

```powershell
irm https://github.com/hahhjaaap/Universal-Desktop-Bridge/releases/latest/download/install.ps1 | iex
udb --version
udb doctor
udb start
```

The managed release bundles its Python runtime and workers: **Python, Node.js, Git and Desktop Commander are not required to run it**. It installs under the current user's LocalAppData and stores user state separately in `%LOCALAPPDATA%\UniversalDesktopBridge`. It does not need administrator access. The standalone ZIP is checksum-verified but **not code-signed**.

The first attended `udb start` can guide setup. Offline mode works without a ChatGPT connection:

```powershell
udb setup --local
udb start --offline
udb status --json
udb logs
udb stop
```

To connect a supported ChatGPT workspace, run `udb setup` in your own terminal and follow the displayed tunnel/account instructions. A working local agent is **not** proof that a ChatGPT workspace is connected. Connection secrets are entered in hidden local prompts, not CLI arguments; owner grants are separate from connection settings.

For use from a second device, or when a ChatGPT plugin claims to be Connected but a tool returns `Session terminated`, see [Remote-device diagnostics](docs/remote-device-troubleshooting.md). Check the local tunnel and the ChatGPT session independently; never blindly replay a timed-out mutation.

### Common commands

| Command | Purpose |
| --- | --- |
| `udb tools` | Show the actual public tool catalog |
| `udb start` / `udb stop` | Start or stop the local agent |
| `udb status` / `udb doctor --json` | Inspect health, runtime and setup |
| `udb logs --details` | Follow redacted tool activity |
| `udb setup` / `udb setup --local` | Configure online or offline mode locally |
| `udb update --check` | Check the latest published stable release |
| `udb update` / `udb rollback` | Managed installation update / previous-release rollback |
| `udb autostart enable` / `udb autostart disable` | Opt in/out of startup at Windows sign-in |
| `udb mcp-config --home PATH --root PATH` | Print a local stdio MCP configuration |
| `udb --access computer` | Attended local owner opt-in to broader computer access |

Updates and rollback must be started **outside UDB's own running tool job**. They preserve the owner's state, not a snapshot of that state. Keep independent backups. Never assume a timed-out update failed; inspect `udb --version` and `udb status` first.

## Accounting-first workflow: Vietnamese invoice XLSX

The `spreadsheet` tool supports two guarded actions:

- `audit_invoices` — read an authorized XLSX file, identify missing invoice fields, possible repeated identifiers, invalid/formula amounts and differences between net + VAT and total. Up to 10,000 data rows are scanned; the source is not changed.
- `export_audit` — create a **separate** XLSX review report after checking the source's expected SHA-256. Existing destinations are rejected.

Example tool arguments (not a shell command):

```json
{"action":"audit_invoices","path":"invoices.xlsx","sheet":"Sheet1"}
```

Reports are for human review. Possible duplicates may be legitimate invoice line items; the tool does not decide VAT liabilities or submit anything to a tax authority. For column mapping, Vietnamese headers, export parameters and safeguards, see **[Accounting Work Tools](docs/accounting-worktools.md)**.

## Isolated browser workflow

With locally authorized host-browser access (**Máy tính → Cho phép mở phiên nền trên máy này** in the Windows manager), use `host_browser(action="open", url="https://example.org/")`. This starts a private headless Chrome/Edge session automatically or reuses the current unexpired browser session. Keep the returned `session_id` and `tab_id`, inspect `snapshot`, then use `click`, `wait_for`, `press`, `select_option`, `check`, `uncheck`, `back`, `forward` or `reload` as appropriate. Prefer accessible role/name locators over brittle CSS selectors. Snapshots are bounded and redact secret-like fields; page content is untrusted and cannot grant permissions.

For HTML dropdowns, use `select_option` with the select locator and an option value or exact label; `fill` on a native select uses the same selection path. Values take precedence over labels, ambiguous labels are rejected, and disabled options/optgroups remain blocked. `read` and `snapshot` include up to 20 options per select (`options_truncated` flags longer lists), effective disabled state and `readonly`. A direct option click returns `use_select_option`. Disabled/readonly controls fail immediately; use `wait_for(state="enabled")` explicitly for a temporarily disabled control. Clicks observe URL and main-frame loader changes, including repeated same-URL form submissions. A navigation read may recover from a transient context/response timeout within the existing deadline; uncertain writes are never automatically replayed.

The browser uses a separate headless profile. It does not take over the user's signed-in browser or permit arbitrary caller-supplied page JavaScript. Automatic Chrome downloads stay blocked; explicit `upload`/`download` transfers are limited to 8 MiB and require separate owner-enabled computer access, an observed origin and file checks. UDB-owned pages display a compact **UDB · AI** status pill, an AI favicon and a page-local AI cursor in screenshots. The cursor follows CDP clicks inside that page, fades after the action, and never moves the physical mouse. The pill distinguishes background work from local owner control. The label is a visual aid; use session/tab IDs and `owner="UDB"` response metadata to establish ownership. No Chrome extension is required for this private-browser identity.

This is the default route for automated web tasks: it does not open a visible personal Chrome tab. Observe it through `host_screenshot(session_id=..., tab_id=...)`. Existing `host_start_session(kind="browser", task="...")` and `host_browser(action="new", session_id=..., url="...")` calls remain supported. Reusing a session does not extend its owner-authorized lifetime. Errors are returned for inspection, without retrying an uncertain action or falling back to physical desktop input.

## Browser login, Windows control and utilities

In **Máy tính**, choose **Tôi đăng nhập** to use UDB's own browser window. While it is handed to you, AI browser operations and UDB desktop input pause. Choose **Trả về cho AI** when finished; pages reload and old tab IDs must be refreshed. Enable **Nhớ đăng nhập trong browser UDB** while the browser is closed to retain that profile across sessions. Disabling it forgets only UDB's saved login.

For native desktop control, UDB yields to recent user input. **Ctrl+Alt+Shift+Backspace** or the manager's pause button latches a local pause; only a local resume clears it. The manager reports when the shortcut could not be registered. This is cooperative yielding, not exclusive input ownership or cancellation of external commands already running.

The unified `computer_desktop` tool also provides text clipboard read/write with version checks, UDB notifications, and Start Menu app discovery/launch. Its UIA actions can read text, set a checkbox state, select an item, expand/collapse and scroll an item into view. `computer_process(action="start")` launches an owned job; list/read/stop use the same tool. Legacy `computer_utilities` and `computer_execute` calls remain supported. Existing `udb --access computer` activation is required. See [Windows control tools](docs/windows-control-tools.md) for the consolidated interfaces and examples.

## Visible tab in an existing Chrome window (owner computer access)

Use this separate option only when explicitly asking for a **visible personal Chrome tab**; it can activate Chrome and is not background automation. For a tab in a Chrome window that is already open, call
`computer_desktop(action="open_chrome_tab", text="https://example.org/")` after
the owner has locally enabled `udb --access computer`. UDB checks for a real
running Google Chrome process and asks Chrome to open a **new tab**, without
creating a separate automation profile or injecting mouse/keyboard input.
The action refuses to launch Chrome if no existing Chrome window is found.

This is **not** the isolated `host_browser` API: it does not attach CDP,
claim ownership of personal tabs, or provide DOM/CSS automation. To interact
with the visible tab, use the owner-granted `computer_desktop` UIA/screenshot
operations after observing a fresh window ID. Chrome may select a different
window when several Chrome windows/profiles are open; inspect the returned
window-reuse observation and the actual desktop before continuing. The tab
uses the user's current Chrome profile and signed-in website context.

### UDB Aura tab identity (optional Chrome extension)

The existing-Chrome action can use the **UDB Aura** Manifest V3 extension in
`chrome-extension/udb-aura/`. When installed and explicitly enabled, UDB
opens a local extension launch page; the extension opens the destination in
the **existing Chrome window**, groups it under **UDB • AI** (cyan), sets an
**AI** toolbar badge, and injects a compact mint AI label **only into
explicitly marked public HTTP(S) tabs**. It also adds a best-effort
UDB favicon on marked tabs, without changing their page titles. It does not
read page content or upload browsing data. Removing the mark removes the
UDB favicon; Chrome does not allow arbitrary RGB effects in its native tab strip.

1. In Chrome, visit `chrome://extensions`, turn on **Developer mode**,
   choose **Load unpacked**, and select `chrome-extension/udb-aura/`.
2. Verify that the extension ID is `ancaoelohcjcbbmncfhimpookhdelhle`.
3. In a **local owner terminal**, run
   `powershell -NoProfile -File tools/enable_chrome_aura.ps1` and confirm
   the ID displayed in Chrome. This writes an opt-in marker under
   `%LOCALAPPDATA%\UniversalDesktopBridge`.
4. After deploying a new UDB build, call `computer_desktop` with
   `action="open_chrome_tab"` and a public URL in `text`. The result
   reports `branding_requested`; actual group/badge application must be
   verified visually. Without the marker, the ordinary visible tab is used.
5. Disable via `powershell -NoProfile -File tools/enable_chrome_aura.ps1 -Disable`.

**Permissions:** The extension requests Chrome `tabs`, `tabGroups`,
`storage`, `scripting` and HTTP(S) site access to decorate only the
marked tabs. This is broader Chrome permission than the original headless
browser; review the extension source before installing. It is never
silently installed or enabled by UDB.

### Automatic independent AI cursor

When a client initializes UDB and **owner-granted desktop computer access**
is already active, the companion displays a compact, click-through **UDB • AI**
cursor overlay on the current Windows desktop. The physical system cursor is
unchanged. At rest, a small identity pill sits near the edge of the work area; the arrow appears near actions and returns to its dock after a short idle period. The overlay uses per-pixel alpha and monitor DPI scaling. It is hidden during local pause/handoff and closed when
the MCP proxy disconnects, the desktop grant is revoked, or the companion
stops. It does not grant access or perform mouse actions by itself.
`virtual_pointer(action="show")` can display it manually, and
`virtual_pointer(action="hide")` hides it for the current connection.
Existing `virtual_pointer` move/click actions remain bounded Win32
messages; compatibility depends on the application, and an independent
visual cursor is **not** a second hardware/system mouse.

## Interface and visual checks

`udb gui` opens a compact control panel when a system tray is available. It keeps
connection, Windows pause/resume, browser handoff and background-session closure
together, with recent metadata-only tool activity. Closing its windows returns
to the tray; use **Thoát UDB** in the tray menu to stop the GUI-owned runtime.
Without a tray, the full manager stays reachable; **Thu gọn** opens the small
panel and closing it returns to the manager. See [Compact controls](docs/compact-controls.md).

The full manager opens on **Trợ lý trên máy**, with pinned pause/resume controls,
separate browser/session cards and expandable resource details. Long content
scrolls without hiding the pause controls. A connected client is not displayed
as active work; the UI distinguishes readiness, local pause and owner handoff.
Browser and native cursor decoration share a quiet mint/charcoal palette.

To render the actual Qt UI with disposable synthetic data:
`python -c "from udb.manager import self_test; self_test('ui-previews')"`.
For headless Linux, install `PySide6-Essentials==6.10.2` and set
`QT_QPA_PLATFORM=offscreen`. To render and check the
actual Windows pointer without capturing the user's desktop, run
`python tools/check_windows_workflows.py --pointer-preview PATH.png` on Windows.

## Build from source (developers)

Python **3.11+** is required for development. Windows is required for native desktop/GUI and frozen EXE acceptance; Linux supports source, protocol and policy tests.

```powershell
python -m pip install -e ".[office,dev]"
python -m ruff check .
python tools/check_import_boundaries.py
python tools/audit_repository.py --strict
python -m pytest
python -m build
```

The repository audit uses `git` to enumerate tracked files. For a Windows standalone build:

```powershell
python -m pip install ".[office,freeze]"
python tools/fetch_build_assets.py
udb build --exe
python tools/check_frozen.py dist/udb.exe
```

The build fetches only pinned public helper binaries with SHA-256 checks; retain their license notices in `THIRD_PARTY_NOTICES/`. CI tests Linux/Windows Python matrices and Windows EXE/installer smoke tests. An Actions success does not by itself mean a new GitHub Release was published.

Run `python tools/benchmark_runtime.py` for repeatable synthetic file-memory,
idle polling, terminal checkpoint and activity-log measurements. See
[Performance measurements](docs/performance.md) for scope, baseline and limits.
The [simulation report](docs/simulation-report.md) describes fault cases,
platform coverage and commands to reproduce the checks. CI uploads JUnit reports
for each Linux/Windows Python matrix job.

## Shared ChatGPT workspace (optional)

The [Common Room example](examples/chatgpt-workspace/README.md) adds a native
Windows dashboard for shared discussions, topic search, replies and revision-checked
task assignments. It includes batched room/browser commands and a separate
headless Edge worker with a bounded 1 FPS screenshot cache. Long messages wrap
and remain readable in full.

This optional source example requires Python and its listed image/browser
dependencies; it is separate from the managed UDB executable. Copy it into an
authorized UDB project before using it. Conversation databases, image caches,
installed dependencies and machine-specific data stay local. Tasks do not wake
closed ChatGPT chats, and browser actions use a fresh owned context.

## Repository layout

```text
src/udb/             Canonical package, CLI, MCP proxy, policy, browser and manager
src/udb/companion/   Scoped files, terminals, jobs, search and accounting worker
tests/               Automated regression and platform tests
tools/               Build, verification, packaging and repository audit
.github/workflows/   CI, Windows release and public-installer acceptance
docs/                Feature documentation
build-assets/        Public helper manifest and license inputs (not private binaries)
THIRD_PARTY_NOTICES/ Bundled third-party notices required for distribution
install.ps1          Managed Windows installer
```

Compatibility launchers and import aliases are intentionally retained. Generated `build/`, `dist/`, virtual environments, credentials, private state, model checkpoints and local caches **must not be committed**. Historical 0.9.x audit reports were retired from the active tree; their commits remain available in Git history.

## Safety and limitations

- No MCP request can silently grant computer, protected-source or Sandbox access. Owner approvals are local and explicit.
- Project-scoped operations and broad computer access are different permission paths; enabling one does not bypass protected roots.
- Unknown-result writes, terminal commands and other mutations must be reviewed before retrying.
- Source work, document reviews and browser pages can contain untrusted content. Review outputs before acting.
- This project is a local bridge, **not ChatGPT Work**, a tax-compliance service or an autonomous account administrator.

**Documentation:** [Architecture](ARCHITECTURE.md) · [Accounting Work Tools](docs/accounting-worktools.md) · [Example configuration](udb.example.toml) · [MCP example](MCP_CONFIGURATION.example.json) · [Releases](https://github.com/hahhjaaap/Universal-Desktop-Bridge/releases)

**License:** [MIT](LICENSE). Third-party components retain their own licenses.

