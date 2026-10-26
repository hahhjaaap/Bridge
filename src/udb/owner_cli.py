"""Owner command arguments and presentation, without lifecycle or state I/O."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from ._version import VERSION


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Open UDB or inspect local readiness without changing permissions."
    )
    parser.add_argument(
        "action",
        nargs="?",
        default="start",
        choices=("start", "stop", "status", "doctor", "test", "revoke-access"),
    )
    parser.add_argument(
        "--access",
        choices=("computer",),
        help="Attended local-console opt-in, then connect",
    )
    parser.add_argument("--home", type=Path, required=True)
    parser.add_argument(
        "--wait", type=int, default=30, choices=range(0, 61), metavar="0..60"
    )
    parser.add_argument("--json", action="store_true", dest="as_json")
    view = parser.add_mutually_exclusive_group()
    view.add_argument(
        "--detach",
        action="store_true",
        help="Start in the background without attaching live logs",
    )
    view.add_argument(
        "--follow",
        action="store_true",
        help="Attach live logs even with redirected console streams",
    )
    parser.add_argument(
        "--offline", action="store_true", help="Start local agent without a tunnel"
    )
    parser.add_argument("--version", action="version", version=VERSION)
    return parser.parse_args(argv)


def render_report(report, as_json=False):
    if as_json:
        print(json.dumps(report, ensure_ascii=True))
        return
    tunnel = "ready" if report["local_tunnel_ready"] else "not ready"
    print(
        f"UDB: local tunnel {tunnel}; background {report['background']}; mode {report['manager_mode']}."
    )
    print(report["next_action"])
    for check in report.get("checks", []):
        mark = (
            "OK"
            if check["ok"] is True
            else "CHECK"
            if check["ok"] is False
            else "UNVERIFIED"
        )
        print(f"{mark} {check['check']}: {check['action']}")


def report_healthy(action, report):
    if action == "stop":
        return not report["manager_running"]
    running = report["local_tunnel_ready"] or (
        report["manager_running"] and report.get("connection_mode") == "offline"
    )
    return running and all(
        check["ok"] is not False for check in report.get("checks", [])
    )
