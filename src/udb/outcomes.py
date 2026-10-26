"""One classification policy for backend reports and public tool responses."""

from __future__ import annotations

import json
import re
from typing import Any, NamedTuple

RUNNING_STATES = frozenset(
    {
        "running",
        "pending",
        "queued",
        "started",
        "waiting",
        "executing",
        "needs_approval",
        "pending_approval",
        "awaiting_approval",
        "committing",
        "owned_by_live_backend",
    }
)
FAILED_STATES = frozenset(
    {
        "failed",
        "error",
        "denied",
        "rejected",
        "cancelled",
        "canceled",
        "interrupted",
    }
)
FINISHED_STATES = frozenset({"exited", "finished", "completed", "complete", "done"})
ERROR_PREFIXES = (
    "Error executing tool",
    "Error calling MCP tool",
    "Access denied:",
    "Permission denied:",
    "Traceback (most recent call last)",
)


class Outcome(NamedTuple):
    status: str
    label: str


def native_status(text: str) -> str | None:
    match = re.match(r"^(PASS|RUNNING|ERROR)\s*\|", text.strip())
    return match.group(1) if match else None


def report_from_text(text: str) -> dict[str, Any] | None:
    candidate = text
    for _ in range(3):
        try:
            parsed = json.loads(candidate)
        except (TypeError, ValueError):
            return None
        if isinstance(parsed, dict):
            return parsed
        if not isinstance(parsed, str):
            return None
        candidate = parsed
    return None


def report_outcome(report: dict[str, Any]) -> Outcome:
    error = report.get("error")
    result = report.get("result")
    if (
        report.get("isError") is True
        or report.get("success") is False
        or report.get("ok") is False
        or report.get("failed") is True
        or error not in (None, "", False, {}, [])
        or isinstance(result, str)
        and result.upper() in {"FAIL", "FAILED", "ERROR"}
    ):
        return Outcome("ERROR", "Backend reported failure")
    state = str(report.get("status", report.get("state", ""))).lower()
    code = report.get("exit_code")
    if state in FAILED_STATES:
        return Outcome("ERROR", "Task failed")
    if isinstance(code, int) and not isinstance(code, bool) and code != 0:
        return Outcome("ERROR", "Process exit code is nonzero")
    if state in RUNNING_STATES:
        return Outcome("RUNNING", "Task pending or running")
    if state in FINISHED_STATES:
        return Outcome("PASS", "Process finished")
    return Outcome("PASS", "Tool returned")
