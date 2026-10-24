# Universal Desktop Bridge 0.9.0

Windows MCP bridge with **one public MCP tool** named \`udb\`, routing internally to 60 operations across 66 backend tool implementations. Uses the existing UDB Windows installation and its locally issued owner permissions; no Desktop Commander dependency.

## Single-entrypoint interface

Clients discover exactly one tool through MCP \`tools/list\`: \`udb\`. Operation names and their schemas are returned on demand instead of being registered as dozens of separate MCP tools.

First discover operations:

\`\`\`json
{"action":"list"}
\`\`\`

Then call an operation through the same MCP tool:

\`\`\`json
{
  "action": "call",
  "tool": "terminal_start",
  "arguments": {
    "command": "python --version"
  }
}
\`\`\`

The example shows the wrapper shape; use the exact arguments returned by \`action=list\` for each operation. Calls to old per-operation MCP tool names are no longer part of the public interface. Internally, UDB retains backend routing and compatibility mappings where needed.

## Features retained

- **ConPTY**: \`terminal_start\` accepts \`mode=conpty\`, \`columns\` and \`rows\`. \`terminal_interact\` can resize the terminal and send VT sequences (arrow keys, Ctrl+C). The default mode stays pipe. Output is VT UTF-8, not a rendered terminal screen. Windows Job Objects, resource limits and timeout cleanup remain active.
- **Virtual AI pointer**: \`virtual_pointer\` observes windows, then move/click/drag/scroll sends bounded Win32 mouse messages to the observed client area. A transparent nonactivating overlay displays the AI pointer. It never calls SendInput or relocates the system cursor and never falls back to physical input. Apps may still change focus/capture or ignore messages; dispatched does not prove application success. Inspect the app after actions.
- **Work recovery**: \`work_recovery\` lists and inspects durable job results and saved terminal output. Explicit resume restarts only supported safe operations such as searches, unchanged document reads, or unchanged single-file copy into a new destination. OS leases prevent concurrent resume; repeated resume returns the same new job ID. Windows records are DPAPI encrypted per user and excluded from file APIs and Git.

Recovery does not restore a dead process's memory, replay shell commands or uncertain writes, or continue byte offsets of uploads/tree copies. Transcript checkpoints run about every 0.5 seconds, so the latest unsaved output can be lost. Local records retain at most 64 recent entries per kind when inactive records can be pruned; small lease files may remain.

## Permissions and safety

- The single entrypoint is only a routing change; it does not grant new access.
- Existing owner grants, registered-project boundaries, local review requirements, host resource admission, and desktop/execute capability checks remain enforced by the dispatcher.
- Unknown operation names and malformed wrapper arguments are rejected before backend dispatch.
- Operations that can write or control the computer keep their original checks. A dispatched action is not proof that the target application completed it.
- Do not commit runtime keys, grants, machine-specific configuration, logs, or private work records.

## Configuration

Use the existing UDB Unified installation and refresh the ChatGPT plugin so it receives the single-tool schema. The source repository is not a bundled Windows installer. \`MCP_CONFIGURATION.example.json\` contains placeholders for a local stdio setup; adapt them locally. Never commit grants, connection keys or installation state.

Office dependencies: install \`requirements-office.txt\` into \`vendor-office\` beside the Companion modules using the installation's Python runtime. ConPTY and the pointer use Windows APIs without a third-party terminal package.

## Validation

Windows acceptance includes native ConPTY tests, crash-recovery tests, virtual-pointer input tests, tool-catalog and permission-boundary regressions, MCP and lifecycle checks. Run \`test.cmd\` in a configured Windows installation. Native tests create and close their own forms/processes; GUI tests need an unlocked attended desktop. Multi-DPI/application compatibility is not comprehensively certified.

For development, run \`python -m compileall -q .\`, then \`python scripts/validate_source.py\` and the focused regression tests. The Windows source-validation workflow checks syntax, repository hygiene, credential handling, integration, recovery, catalog and ConPTY. See \`CONTRIBUTING.md\` and \`SECURITY.md\` for details.
