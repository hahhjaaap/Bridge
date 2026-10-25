# Validation of source consolidation (0.9.0)

Actually run in the Linux development environment:

- `python -m pytest`: 325 passed, 5 skipped, 27 subtests passed. This includes the original core regressions plus package/configuration/CLI tests and component regression programs. Native Windows programs invoked inside the regression harness skip their native cases on Linux; they are not Windows test evidence.
- Real source-only MCP subprocesses: initialize, discover 60 public tools / 66 implementations, call bridge_status, and reject a terminal request without owner computer access. No installed core EXE, existing connection key or grant was used.
- Source distribution and wheel built successfully. A fresh isolated virtual environment installed the wheel and resolved package/legacy aliases from that environment, outside this checkout. Its generated catalog contained 60 tools and version 0.9.0.
- Runtime helper assets and compatibility modules were checked in the wheel. Configuration parsing, path-link rejection, explicit terminal enable/disable, effective manager settings and unchanged permission boundaries have regression coverage.

Not yet run for this refactor:

- Windows native/attended GUI, ConPTY, Credential Manager and Sandbox acceptance.
- Windows frozen executable build and execution.
- A live ChatGPT call through the new package on the user's computer.

The existing desktop connection returned HTTP 429 during this task. No attempt was made to bypass it or change owner state. The running Windows installation was not replaced with an unverified refactor. Windows CI is defined in `.github/workflows/ci.yml`; a workflow definition is not evidence of a passing run. Earlier 0.8.0 Windows results must not be presented as validation of these package changes.

The source is now one package with one CLI. Core/Companion remain separate owned worker processes for failure isolation, launched by the same `udb` entry point. The installed-core backend is an explicit migration option, not a source-build dependency. Legacy success payloads, RPC error codes and tool aliases are preserved; common status/version/no-replay metadata is additive.
