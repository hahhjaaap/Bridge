# Universal Desktop Bridge 0.7.0

Windows MCP bridge with 58 public tools, recoverable backends, scoped file and terminal operations, advanced desktop actions, and XLSX/DOCX/PDF background jobs.

Source and tests only. Local connection configuration, installation state, grants, deployment reports and machine-specific metadata are intentionally excluded. Configure the existing Windows installation locally. Office dependencies are pinned in requirements-office.txt.

Validated on Windows: 301 core tests, 27 subtests, recovery fault injection, native GUI workflow, and real MCP document operations. Some tests require local MCP configuration and an attended Windows desktop. Shared Windows pointer; no independent second cursor. Jobs lost after backend failure are not replayed.
