"""Bounded, expiring RPC to the GUI's single host worker. Never retries writes."""

from __future__ import annotations

import json
import math
import re
import time
import uuid
from pathlib import Path

from .core import PolicyError, reject_links
from .resources import LIMITS


def _read_json(path, limit, encoding="utf-8"):
    with path.open("rb") as stream:
        raw = stream.read(limit + 1)
    if len(raw) > limit:
        raise ValueError("Host JSON exceeds its size limit")

    def invalid_constant(value):
        raise ValueError("Non-finite host JSON number")

    try:
        return json.loads(raw.decode(encoding), parse_constant=invalid_constant)
    except (RecursionError, OverflowError) as exc:
        raise ValueError("Invalid host JSON structure") from exc


def _validate_health(result):
    if not isinstance(result, dict) or not isinstance(result.get("resources"), dict):
        raise ValueError("invalid health object")
    if type(result.get("worker_ready", False)) is not bool:
        raise ValueError("invalid worker state")
    resource = result["resources"]
    stamp = resource["at"]
    if type(stamp) not in (int, float) or not -2 <= time.time() - stamp <= 6:
        raise ValueError("stale or invalid health timestamp")
    if type(resource.get("admission_ready", False)) is not bool:
        raise ValueError("invalid admission state")
    sessions = result.get("sessions", [])
    if not isinstance(sessions, list) or len(sessions) > LIMITS["max_sessions"]:
        raise ValueError("invalid session inventory")
    for session in sessions:
        if not isinstance(session, dict):
            raise ValueError("invalid session")
        sid, tabs = session.get("session_id"), session.get("tabs", 0)
        if (
            not isinstance(sid, str)
            or not re.fullmatch("[a-f0-9]{32}", sid)
            or session.get("kind") not in ("browser", "workspace")
            or type(tabs) is not int
            or tabs < 0
            or not isinstance(session.get("task", ""), str)
            or session.get("presentation", "background")
            not in ("visible", "background")
            or type(session.get("closing", False)) is not bool
        ):
            raise ValueError("invalid session fields")
        expires = session.get("expires")
        if type(expires) not in (int, float) or not math.isfinite(expires):
            raise ValueError("invalid session expiry")
    result["worker_ready"] = result.get("worker_ready", False)
    result["sessions"] = sessions
    return result


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
        value = _read_json(p, 4096, "utf-8-sig")
        return isinstance(value, dict) and value.get("enabled") is True
    except (OSError, ValueError):
        return False


def status(agent):
    p = agent.state / "host" / "status.json"
    reject_links(p)
    try:
        result = _validate_health(_read_json(p, 128 * 1024))
        result["enabled"] = enabled(agent)
        return result
    except (OSError, ValueError, KeyError, OverflowError):
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
    if op == "computer" and args.get("mode") == "status" and not s.get("worker_ready"):
        from .computer import status_report

        return status_report(agent, worker_ready=False)
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
    wait_deadline = time.monotonic() + 27
    payload = dict(id=rid, expires=until, op=op, args=args)
    if len(json.dumps(payload, ensure_ascii=True).encode()) > 16 * 1048576:
        raise PolicyError("Command payload exceeds 16 MiB")
    atomic_json(inbox / (rid + ".json"), payload)
    result_path = replies / (rid + ".json")
    while time.monotonic() < wait_deadline:
        if result_path.exists():
            reject_links(result_path)
            try:
                result = _read_json(result_path, 12 * 1048576)
            except (OSError, ValueError) as exc:
                raise PolicyError(
                    "Invalid host reply; outcome unknown, inspect host status before retrying"
                ) from exc
            result_path.unlink(missing_ok=True)
            if not isinstance(result, dict) or type(result.get("ok")) is not bool:
                raise PolicyError(
                    "Invalid host reply; outcome unknown, inspect host status"
                )
            if result["ok"] is False:
                message = result.get("error")
                raise PolicyError(
                    message[:500]
                    if isinstance(message, str)
                    else "Host operation failed; inspect status"
                )
            if "result" not in result:
                raise PolicyError(
                    "Invalid host reply; outcome unknown, inspect host status"
                )
            return result["result"]
        time.sleep(0.04)
    raise PolicyError(
        "Host operation timed out; inspect host status before retrying a write"
    )
