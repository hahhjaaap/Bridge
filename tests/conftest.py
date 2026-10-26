"""Component programs run in subprocesses through test_regressions.py."""

# Several programs launch fixtures at import time; do not collect them twice.
collect_ignore = ["components"]
