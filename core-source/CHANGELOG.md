# Changelog

## Unreleased

- Make desktop controller cleanup safe against repeated GUI/finalizer calls.
- Add isolated Windows GUI and frozen-EXE acceptance checks.
- Separate root proxy metadata/versioning from the standalone core EXE package.
- Require a complete, checksummed offline runtime before creating or verifying a release ZIP.
- Make CI read-only; formatting is check-only by default and writing requires an explicit local `--write` flag.

## 0.7.0-dev.1

- Development-preview baseline for the standalone Windows desktop manager and EXE.
