# Universal Desktop Bridge 0.8.0

Windows MCP bridge: 60 public tools / 66 backend implementations. Uses an existing UDB Windows installation and its locally issued owner permissions; no Desktop Commander dependency.

## New in 0.8.0

- **ConPTY**: terminal_start accepts mode=conpty, columns and rows. terminal_interact can resize the terminal and send VT sequences (arrow keys, Ctrl+C). The default mode stays pipe. Output is VT UTF-8, not a rendered terminal screen. Windows Job Objects, resource limits and timeout cleanup remain active.
- **Virtual AI pointer**: virtual_pointer list observes windows, then move/click/drag/scroll sends bounded Win32 mouse messages to the observed client area. A transparent nonactivating overlay displays the AI pointer. Supports right/middle/double clicks and horizontal scrolling. It never calls SendInput or relocates the system cursor and never falls back to physical input. Apps may still change focus/capture or ignore messages; dispatched does not prove application success. Inspect the app after actions. The overlay automatically hides after 10 seconds; hide closes it.
- **Work recovery**: work_recovery list/inspect reads durable job results and the last 64 KiB of saved terminal output. Explicit resume restarts searches, unchanged document reads, or unchanged single-file copy into a new destination. OS leases prevent concurrent resume; repeated resume returns the same new job ID. Windows records are DPAPI encrypted per user and excluded from file APIs and Git.

Recovery does not restore a dead process's memory, replay shell commands or uncertain writes, or continue byte offsets of uploads/tree copies. Transcript checkpoints run about every 0.5 seconds, so the latest unsaved output can be lost. Local records retain at most 64 recent entries per kind when inactive records can be pruned; small lease files may remain.

## Configuration

Run the existing installation with udb. Refresh the ChatGPT plugin and start a new chat to receive the new tool schemas. Source alone is not a bundled Windows installer. MCP_CONFIGURATION.example.json contains placeholders for a local stdio setup; adapt them locally. Never commit grants, connection keys or installation state.

Office dependencies: install requirements-office.txt into vendor-office beside the Companion modules using the installation's Python runtime. ConPTY and the pointer use Windows APIs without a third-party terminal package.

## Validation

Windows acceptance: four native ConPTY tests (console mode, VT arrow/Unicode/resize, Ctrl+C, stop, abrupt owner death); six work recovery tests including actual backend crashes; real MCP virtual-pointer button effects/right/double/drag/horizontal wheel; real MCP ConPTY and recovery after crash. Existing feature (13), kernel durability (5), document (7), consolidation (10), MCP and lifecycle regressions were checked. Core 0.7.0 remains unchanged in this update.

Run test.cmd in a configured Windows installation. Native tests create and close their own forms/processes; GUI tests need an unlocked attended desktop. Multi-DPI/application compatibility is not comprehensively certified.

The repo contains source and tests. Local deployment reports, machine paths and private work records are excluded.
