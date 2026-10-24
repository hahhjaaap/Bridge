# Security policy

## Reporting a vulnerability

Do not publish live API keys, access grants, private configuration, or exploit details in a public issue.

Use GitHub's private vulnerability reporting or security-advisory feature for this repository if it is enabled. If it is unavailable, contact the repository owner privately through GitHub and include a concise description, affected version, impact, and reproducible steps that do not expose credentials.

## Credential and local-state rules

- Never commit .env files, mcp.json, runtime grants, connection keys, local installation state, deployment reports, or private work records.
- Keep runtime credentials in the local Windows Credential Manager or another approved secret store. Do not print credential values in logs or test output.
- If a real credential may have been committed or shared, revoke or rotate it immediately. Removing the current file does not remove it from Git history.
- Use synthetic credentials in tests. Do not use a production key to test parsing, storage, or connectivity.
- Test destructive operations only against disposable fixtures or an explicitly approved project.

## Scope and limitations

UDB runs with the permissions of the Windows account that launches it. An owner-approved computer-access mode can be unsandboxed; it is not a security boundary against code already running as that user. Application compatibility and the effect of window messages vary by application.

scripts/validate_source.py checks the current tracked working tree for selected high-confidence credential patterns and local runtime artifacts. It does not scan all historical Git objects and is not a substitute for GitHub secret scanning, credential rotation, code review, or a full security audit.
