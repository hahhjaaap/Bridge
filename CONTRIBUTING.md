# Contributing to Universal Desktop Bridge

## Development setup

Use a supported Windows machine for the native terminal, desktop, and full acceptance tests. The lightweight source checks can run without the installed UDB desktop manager.

1. Create a virtual environment with Python 3.12 or another version explicitly supported by the project.
2. Install development test dependencies:

       py -3.12 -m venv .venv
       .\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt

3. Run source and syntax validation:

       .\.venv\Scripts\python.exe -m compileall -q .
       .\.venv\Scripts\python.exe scripts\validate_source.py

4. Run the portable regression tests:

       $env:PYTHONPATH = "core-source"
       .\.venv\Scripts\python.exe -m pytest -q core-source\tests\test_credentials.py
       .\.venv\Scripts\python.exe test_integration.py
       .\.venv\Scripts\python.exe -m unittest -v test_backend_recovery test_tool_consolidation test_work_recovery test_conpty

5. Run test.cmd only in a configured local UDB installation. Some acceptance tests require the local mcp.json, the owner-approved installation, native Windows APIs, and an unlocked desktop.

## Change requirements

- Preserve existing permission boundaries, project scopes, owner grants, and explicit recovery behavior.
- Never replay shell commands or uncertain writes during recovery.
- Keep runtime keys, grants, installation state, logs, and private work records out of source control.
- Add regression tests for behavior changes, especially process lifetime, credential handling, file publication, and access revocation.
- Update plugin.json and udb_unified.py together when changing the proxy version. Keep the README version heading at the stable major.minor.patch version.
- Do not claim that a tool action succeeded solely because it was dispatched; verify the resulting application or file state.
- Prefer focused commits and explain any compatibility or permission changes in the pull request.
