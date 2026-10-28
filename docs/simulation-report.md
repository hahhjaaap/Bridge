# Project simulation and fault injection

Baseline: `99ac628`. Tests use temporary owner homes, disposable files and owned
subprocesses. No production computer-access grant, user browser profile or
existing application is changed by these simulations.

## Reproduced faults and fixes

| Area | Reproduction | Result after the fix |
| --- | --- | --- |
| Host health and replies | Invalid booleans, session shapes, NaN, deep JSON, oversized records, missing reply fields | Bounded parsing; unavailable health or an explicit unknown-outcome error |
| RPC wait deadline | System clock jumps forward/backward by an hour | Elapsed-time wait stays bounded; one command is enqueued |
| Host mailbox | A linked, locked or directory entry sorts before a valid command | Bad entries are isolated; later valid commands are still processed |
| Session cleanup | Process close or handoff cleanup fails | Session stays tracked as closing; new actions are blocked; cleanup can be retried |
| Multiple owned sessions | One close fails while another session is healthy | Other sessions still close; failed cleanup remains visible |
| Browser handoff | Restart fails before a visible owner window exists, or return fails with the owner window still alive | Input-pause flag follows the surviving window; explicit emergency pause is preserved |
| Computer jobs | Start after shutdown, failed stop, unavailable grant observer | Closed managers refuse launches, stop can retry cleanup, observation failure revokes owned jobs |
| Result classification | Timeout, output quota, revocation, expired approval or cleanup failure | Error status is preserved, including when a stale PASS header is present |
| GUI shutdown | One owned component fails to close | Remaining cleanup is attempted; the window explains the failure and allows retry |
| Cold MCP startup | Query computer access before starting the host worker | Grant status remains readable; process availability is explicitly unknown |

The initial targeted reproductions failed in 18 host-case variants, four job
cases, six outcome cases and one stale-label case. Positive and boundary cases
were added alongside those regressions. The fault suite now contains 49 checks.

## Coverage

| Project area | Automated evidence |
| --- | --- |
| Startup, offline mode, connection recovery | CLI subprocess, headless lifecycle, onboarding and reconnect tests |
| MCP, HTTP and permissions | Actual stdio client startup, tool discovery, denied requests followed by ping/status, HTTP validation and grant-isolation tests |
| Browser | Live Linux CDP fixtures; native Windows headed/background restart, screenshot and real host-mailbox scenarios |
| Desktop and Windows utilities | Cooperative input tests, native owned fixtures, pointer rendering and no-focus/no-mouse-takeover probes |
| Files, source proposals and recovery | Real temporary files, optimistic hash conflicts, streaming limits, crash/restart and recovery tests |
| Terminal and concurrent jobs | Real subprocess crash tests, UTF-8, native ConPTY, shared-capacity and cancellation tests |
| Office/accounting/media | Disposable documents, corrupt archives, JSON serialization and worker-failure scenarios |
| GUI and extension | Actual Qt geometry/control tests plus per-tab extension ordering/race checks |
| Packaging | Isolated wheel, frozen Windows integration and installer update/rollback acceptance |

Local Linux validation with Qt installed: **884 passed, 9 skipped**, plus
**27 subtests passed**. Qt self-test exercises all six pages at 1220×820 and
960×680 with motion enabled and disabled. The extension behavior test and
isolated wheel acceptance also pass; the public catalog remains 61 tools.

Native Windows results are captured by the `package-and-regressions` workflow.
Each matrix job uploads `scenarios-<os>-<python>` containing its JUnit report.
JUnit includes subtest records, so its check total includes the 27 subtests.

## Reproduce

```sh
python -m pytest tests/test_fault_scenarios.py tests/test_protocol_scenarios.py
python -m pytest --junitxml=reports/simulation/results.xml
node tests/browser/test_chrome_aura.cjs
```

On headless Linux use `QT_QPA_PLATFORM=offscreen` and `dbus-run-session` for
the full suite with Qt and a local Chromium. On Windows, the additional native
acceptance command is:

```powershell
python tools/check_windows_workflows.py --visible-browser --host-scenario --pointer-preview reports/simulation/pointer.png
```

The browser mailbox fixture has its own marked temporary home and never creates
a computer-access grant. Protocol denial tests retain the owner-consent gate.
Authorized business logic is exercised with isolated internal fixtures.

Full Windows Sandbox guest boot, personal-device interaction, actual ChatGPT
workspace connectivity and visual review of the login window still require the
corresponding attended environment. Automated simulation results describe the
tested fixtures and platform branches.
