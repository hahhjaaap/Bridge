# UDB architecture and module ownership

UDB has **one canonical Python implementation** under `src/udb/`.
The source checkout, installed wheel and frozen Windows executable use the
same code. The root-level `bridge.py` and `udb_*.py` files are
**compatibility launchers**, not independent backends. Do not duplicate logic
inside them.

## Request path

```text
ChatGPT / MCP client
        |
        v
udb.cli / udb.proxy (single client-facing MCP transport)
        |
        +--> udb.server (registered-project permissions, proposals, Sandbox)
        |
        +--> udb.companion.runtime (worker lifecycle and sessions)
                    |
                    v
              companion.dispatch (single companion tool router)
                    |
                    +--> companion.files + terminal (bounded local operations)
                    +--> companion.extensions (optional file/terminal helpers)
                    +--> companion.features (jobs, documents, processes)
                                |
                                +--> companion.background (job coordinator)
                                |       +--> companion.jobs (task state/cancellation)
                                |       +--> companion.search (bounded worker)
                                |       +--> companion.journal (durable checkpoints)
                                +--> companion.documents
                                +--> companion.system_processes
```

`udb.catalog` translates/consolidates public tool schemas and `udb.registry`
publishes them. The companion's `dispatch.validate_catalog()` rejects schema
names that lack a handler, unexpected handlers and duplicate schemas.
`udb.proxy` owns backend routing, concurrency and response normalization.
Keep this separate from file permissions and owner approvals.

## Core policy and approval queues

- `udb.core.Agent`: stable public entry point for project registration, path
  validation, read/write proposals, audit records and diagnostics.
- `udb.core_common`: shared `PolicyError`, canonical JSON and SHA-256 helpers.
  Existing `udb.core` imports are retained for compatibility.
- `udb.core_autonomy.AutonomyMixin`: cohesive owner-only grant management and
  queued source/media/Sandbox work. These methods are inherited by `Agent`;
  they do not become new MCP or HTTP endpoints.
- `requests(status, created)` and `jobs(status)` indexes accelerate queue
  lookup on existing SQLite databases; schema initialization is idempotent.
- Source write proposals store full bounded new text, but only a bounded diff
  preview. Large/truncated diffs explicitly instruct local full-file review.
  Do not silently truncate approval context or bypass owner authorization.
- File listing filters unsupported binary extensions before expensive per-file
  policy checks; every visible entry still passes `Agent.path`.

## Desktop and isolation

- `udb.manager`: Qt MainWindow composition, event/lifecycle handling.
- `udb.manager_dialogs`: local source browser, connection settings, and
  owner-only approval dialogs; no remote approval API.
- `udb.manager_refresh`: status polling and project list refresh.
- `udb.manager_widgets`: reusable UI widgets and styles.
- `udb.companion.jobs`: cancellation, task status and checkpoint lifecycle.
- `udb.companion.search`: search controller plus isolated worker entrypoint;
  `background.search_worker` remains a compatibility export.
- `udb.companion.background`: task pool, bounded transfers, uploads, copy/move
  and recovery. Tree inventory uses iterative DFS to avoid Python recursion
  limits without eagerly loading entire directories.
- `udb.desktop`: owner-side controller.
- `udb.desktop_vm` and `udb.sandbox`: Windows Sandbox orchestration.
- `udb.desktop_guest`: intentionally self-contained guest payload copied
  into an isolated Sandbox. Do not split it into imports without changing
  the guest deployment protocol and testing the resulting boundary.

## Where new code belongs

1. Define the public schema in the appropriate companion schema/feature/
   extension module or in the native core server.
2. Add its handler to `companion.dispatch.NATIVE_HANDLERS`, or to the
   corresponding extension/feature registry. Avoid new dispatch branches in
   `Runtime`.
3. Add regression tests for schema parity, permissions, error responses and
   recovery behavior.
4. Run `python -m pytest`, `python -m ruff check src tools tests`,
   `python tools/audit_repository.py --strict`,
   `python tools/check_import_boundaries.py`, and the Windows wheel/EXE
   smoke tests. CI now gates the entire source/tools/tests tree on Ruff.
   The import check guards all canonical modules against eager import cycles;
   it intentionally does not treat deferred function-local imports as cycles.

## Compatibility and safety

Do not remove root or `src/` shim modules solely because their text is
identical: direct source invocation and installed wheel imports both use them.
Do not delete third-party license notices, private owner state, backups,
credentials, datasets or checkpoints as part of source cleanup.

Existing security, browser and accounting PRs are separate. Integrate them
only after their CI gates and compatibility checks; no unreviewed replacement
of a running UDB installation.
