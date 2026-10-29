# Browser and Windows workflows

UDB keeps ordinary web work in a private background Chrome/Edge profile. Windows
desktop actions and utilities use the existing locally issued computer-access
grant. No remote tool enables that grant or clears a local emergency pause.

## Sign in without giving the AI your password

1. In the local UDB manager, open **Máy tính · chạy nền** and enable private
   background sessions.
2. If desired, enable **Giữ đăng nhập trong trình duyệt riêng của UDB** while
   its browser is closed. This saves only UDB's profile, never imports personal
   Chrome/Edge profiles. Leave it off for an ephemeral profile.
3. Choose **Mở để tôi đăng nhập**, then sign in directly in that browser window.
   You do not enter the password in chat. AI browser operations and UDB desktop
   input are suspended during the handoff.
4. Choose **Trả về chạy nền**. UDB restarts its private browser in background,
   retains session cookies in memory for the handoff, and reopens its pages.
   Pages reload: unsaved forms may be lost. Old tab IDs become invalid; use
   `host_browser(action="list", session_id=...)` to observe the new ones.

Only the local manager changes presentation and saved-login preferences. A
remote `open` never makes a visible browser appear. Local handoff grants at least
15 minutes for this owner-started session; ordinary remote calls do not extend it.
Close the browser before changing saved-login preferences. Disabling the option
removes UDB's saved profile. Persistent site login also depends on the site's
cookie expiration, reauthentication and MFA policies.

## Explicit browser file transfers

Automatic Chrome downloads and uploads via arbitrary JavaScript remain disabled.
These explicit actions require owner-enabled computer access, an allowed absolute
local path and `expected_origin` from a freshly observed page. Maximum size: 8 MiB.

- **Upload:** read the file's SHA256 with `computer_files`, then call
  `host_browser(action="upload", session_id=..., tab_id=..., selector="input[type=file]",
  path=..., expected_sha256=..., expected_origin=...)`. Exactly one enabled file
  input must match. UDB stages verified bytes in its own profile until the session
  closes. Selection does not submit the form; inspect it before the next action.
  At most eight staged uploads are retained per session.
- **Download:** call `host_browser(action="download", session_id=..., tab_id=...,
  url=..., destination=..., expected_origin=...)`. The URL must have the same
  origin as the observed page. Browser cookies remain in the browser. Redirects
  are rejected. The destination must be new; UDB returns file size/SHA256 and never
  opens or executes the downloaded file. CDN redirects/cross-origin links and
  browser-generated blob downloads are not supported by this action.
- **Scroll:** `host_browser(action="scroll", ..., delta_x=0, delta_y=500)` sends
  browser-local wheel events, never moves the physical pointer.

## Native desktop ownership

UDB checks recent Windows input before actions that can change the desktop and
between chunks of synthetic input. Recent human input causes it to yield and
report a wait interval; observe the window again before continuing. Injected UDB
input is tracked separately so it does not continuously block its own next step.

`computer_desktop(action="control_status")` remains available under resource
pressure and reports `available`, `user_active`, `browser_handoff`, or `paused`.
The local manager offers pause/resume, and the host registers
**Ctrl+Alt+Shift+Backspace** as an emergency pause chord. It reports registration
failure instead of claiming the shortcut works. A latched pause survives backend
restarts; returning a browser to background does not clear an emergency pause.

This is best-effort cooperative yielding, not a security boundary or exclusive
physical input ownership. Tiny races remain between OS observation and input.
Already-running external programs or synchronous UI Automation can finish after
a pause; completed side effects are not undone. Key/button release cleanup still
runs when an input operation is interrupted. Broad program execution remains
current-user and unsandboxed, as stated in the existing owner grant.

## Windows utilities

Use `computer_desktop` in the unified catalog after the existing attended
`udb --access computer` grant. The legacy `computer_utilities` name remains callable:

| Action | Behavior |
|---|---|
| `clipboard_get` | Returns bounded Unicode text and a sequence `version`. Non-text formats are identified without conversion. Read only when the user's work needs the clipboard. |
| `clipboard_set` | Requires `text` and `expected_version` from a prior read. Locks and rechecks the clipboard; refuses if another app changed it. Yields while the user is active or desktop control is paused. |
| `apps` | Lists up to 300 Start Menu apps, including localized display names and app IDs. |
| `launch_app` | Uses one exact listed `name` or app ID; ambiguous names are rejected. No shell-command interpolation. It can activate an app window, so it also yields to the user. |
| `notify` | Sends a text-only toast under UDB's own notification identity. Registers only UDB's current-user AppUserModelID. Notification settings/Focus Assist may suppress display; a successful request does not claim it was seen. |

Text is passed to the fixed PowerShell helper as JSON through stdin. Titles and
messages are never interpolated into commands, scripts or XML markup. Clipboard
text and page content are untrusted data, not instructions or permission grants.

## Validation

The test suite exercises Chromium CDP, cookie-preserving handoff and owned-profile
persistence, upload bytes, authenticated same-origin downloads and redirect
rejection. On Linux, a headless process adapter is used for handoff tests; it does
not verify visible Windows window behavior. Native clipboard, hotkey registration,
toast display, Start Menu launching and Windows input observation still need the
Windows acceptance run. Unit tests cover their policy and API boundaries.

On the target Windows machine, run `python tools/check_windows_workflows.py` after
installing the source package. It probes native input observation, local pause,
hotkey registration, Start Menu lookup, clipboard format and owned-browser
capture. It never prints clipboard content, changes the clipboard, grants access
or injects desktop input. Add `--visible-browser` to exercise the local browser
handoff, and `--notify` to request a test toast. Verify the actual visible window,
label and toast manually; a CDP/notification acknowledgement alone is insufficient.
