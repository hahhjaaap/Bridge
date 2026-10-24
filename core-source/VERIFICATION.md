# Release verification procedure

This checklist is not evidence that a particular release has passed. Record the exact commit, build runner, artifact hash and result for each candidate.

- [ ] Ruff format/check, mypy and the full source test suite pass.
- [ ] Synthetic Qt GUI acceptance passes on Windows.
- [ ] Frozen EXE `--version`, `--build-info` and `--list-tools` checks pass.
- [ ] Credential self-test uses only synthetic credentials and disposable state.
- [ ] Isolated MCP smoke and reviewed live integration tests pass in a disposable, attended Windows environment.
- [ ] `Prepare-Vendor.py` validates CPython 3.11, tunnel client, license bundle and dependency hashes.
- [ ] `Verify-Package.py` verifies the final ZIP.
- [ ] The extracted ZIP passes `Install.ps1 -VerifyOnly`; clean install, relaunch and rollback are tested in a disposable Windows profile.

Record the Git commit SHA, version, toolchain/runtime versions, ZIP SHA-256, test results, signing status and known limitations outside the source repository.

Do not distribute if a required source file is missing, EXE provenance differs from source, any vendor file differs from its dependency inventory, the ZIP contains an unapproved path or sensitive data, or installation/rollback remains untested. SHA-256 is an integrity check, not a digital signature.
