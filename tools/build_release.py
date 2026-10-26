"""Compatibility release helper; canonical command is udb build --exe."""

from udb.cli import main

raise SystemExit(main(["build", "--exe"]))
