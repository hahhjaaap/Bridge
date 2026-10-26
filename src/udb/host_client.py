"""Bounded, expiring RPC to the GUI's single host worker. Never retries writes."""

from __future__ import annotations

import json
import time
import uuid
from pathlib import Path

from .core import PolicyError, reject_links


def atomic_json(path, value):
    path = Path(path)
    reject_links(path)
    tmp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        tmp.write_text(json.dumps(value, ensure_ascii=True), encoding="utf-8")
        tmp.replace(path)
    finally:
        tmp.unlink(missing_ok=True)


def enabled(agent):
    p = agent.state / "host-settings.json"
    reject_links(p)
    try:
        value = json.loads(p.read_text("utf-8-sig"))
        return isinstance(value, dict) and value.get("enabled") is True
    except (OSError, ValueError):
        return False


def status(agent):
    p = agent.state / "host" / "status.json"
    reject_links(p)
    try:
        if p.stat().st_size > 128 * 1024:
            raise ValueError("oversized")
        result = json.loads(p.read_text("utf-8"))
        if not isinstance(result, dict) or not isinstance(
            result.get("resources"), dict
        ):
            raise ValueError("invalid health object")
        stamp = result["resources"]["at"]
        if type(stamp) not in (int, float) or not -2 <= time.time() - stamp <= 6:
            raise ValueError("stale or invalid health timestamp")
        result["enabled"] = enabled(agent)
        return result
    except (OSError, ValueError, KeyError):
        return dict(
            enabled=enabled(agent),
            worker_ready=False,
            sessions=[],
            resources=dict(
                at=0,
                cpu_percent=None,
                ram_percent=None,
                available_mib=0,
                admission_ready=False,
                paused=True,
                reason="monitor_unavailable",
            ),
        )


def command(agent, op, **args):
    s = status(agent)
    if not s.get("worker_ready") and op not in ("close", "stop_all"):
        raise PolicyError("Run udb to start the background agent first")
    if op == "computer":
        mode = args.get("mode")
        arguments = args.get("arguments", {})
        if mode != "status" and not (
            mode == "process" and arguments.get("action") == "stop"
        ):
            from .computer_access import require

            require(agent)
    elif not s["enabled"] and op not in ("close", "stop_all"):
        raise PolicyError("Host background mode is disabled locally")
    folder = agent.state / "host"
    inbox = folder / "inbox"
    replies = folder / "replies"
    reject_links(inbox)
    reject_links(replies)
    if not inbox.is_dir() or not replies.is_dir():
        raise PolicyError("Host worker is not running")
    if len(list(inbox.iterdir())) >= 8:
        raise PolicyError("Host command queue full; retry later")
    rid = uuid.uuid4().hex
    until = time.time() + 25
    payload = dict(id=rid, expires=until, op=op, args=args)
    if len(json.dumps(payload, ensure_ascii=True).encode()) > 16 * 1048576:
        raise PolicyError("Command payload exceeds 16 MiB")
    atomic_json(inbox / (rid + ".json"), payload)
    result_path = replies / (rid + ".json")
    while time.time() < until + 2:
        if result_path.exists():
            reject_links(result_path)
            if result_path.stat().st_size > 12 * 1048576:
                raise PolicyError("Host reply too large")
            result = json.loads(result_path.read_text("utf-8"))
            result_path.unlink(missing_ok=True)
            if not result.get("ok"):
                raise PolicyError(result.get("error", "Host operation failed"))
            return result["result"]
        time.sleep(0.04)
    raise PolicyError(
        "Host operation timed out; inspect host status before retrying a write"
    )
