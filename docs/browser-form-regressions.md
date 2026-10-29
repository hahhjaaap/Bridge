# Browser form and link regressions

Baseline: `9ec663c` on `main` (PR #23). A user report identified native dropdown
failures, disabled-field timeouts, repeated form-submit timeouts and an
intermittent failure following an external link.

## Reproduction and changes

Three real Chromium fixture tests failed before the changes:

- `fill` on a native select returned `unsupported_control`.
- A disabled input returned a generic timeout instead of its blocked state.
- A form submitted to the same URL was not observed as a navigation.

Native selects now share one selection path for `fill` and `select_option`.
Exact values take precedence over labels; ambiguous matches fail without events.
Empty option values are supported. Disabled selects, options and optgroups remain
blocked. Direct option clicks return `use_select_option` instead of waiting for
a native popup option to become a separate mouse target.

`read` and `snapshot` expose effective disabled/readonly states and up to 20
options per select, with `options_truncated` for longer lists. Secret-like
controls retain value redaction and do not expose option values.

Disabled and readonly controls fail immediately. A caller that expects a field
to become enabled can explicitly use `wait_for(state="enabled")`. Input/change
handlers are not replayed after a context-loss error or rejected selection.

Click navigation compares both the target URL and main-frame loader ID. The
same-URL fixture submits three times and checks four HTTP requests in total,
including the initial page load. Navigation observation can retry read-only
context/response failures within the original deadline. This does not retry the
click, form submission or field mutation.

## Validation

- Full local Linux/Qt suite: **903 passed, 9 skipped**, plus **27 subtests**.
- Live Chromium fixtures cover select values/labels/empty options, ambiguity,
  disabled optgroups and fieldsets, readonly fields, change-event counts, option
  click guidance, repeated same-URL submissions and a cross-origin link.
- The disposable Windows host scenario now checks native selection and
  disabled/readonly results through the real mailbox. CI validates Windows,
  wheels, frozen EXE integration and installer update/rollback.
- Ruff, repository audit and import-boundary checks pass.

An additional live public-site probe from cloud used a disposable browser:
`example.com` → IANA succeeded **3/3** times; Selenium's public web form selected
“Two”, returned `disabled`/`readonly` for its locked controls, and submitted
successfully **3/3** times. The current example.com link leads to
`https://www.iana.org/help/example-domains`.

The original intermittent external-link failure was not reproduced on the
baseline local cross-origin fixture. Read-only timeout recovery is covered by
fault injection; the public-site successes do not establish the exact cause of
the original failure. No personal-device or installed-plugin update was tested.

## Reproduce

```sh
dbus-run-session -- python -m pytest tests/core/test_owned_browser_live.py tests/core/test_host_background.py
```

The live Linux adapter permits only its exact disposable HTTP fixture URLs.
Production private-URL validation, origin checks, owner grants and physical-input
boundaries remain in force.
