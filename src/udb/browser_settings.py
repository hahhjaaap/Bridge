"""Local-owner preferences for UDB's own browser profile, never a personal profile."""

import json
import shutil

from .core import PolicyError, reject_links
from .host_client import atomic_json, status


def remember_login(agent):
    path = agent.state / "browser-settings.json"
    reject_links(path)
    try:
        with path.open("rb") as stream:
            raw = stream.read(1025)
        value = json.loads(raw)
        return (
            len(raw) <= 1024
            and isinstance(value, dict)
            and value.get("remember_login") is True
        )
    except (OSError, ValueError):
        return False


def set_remember_login_local(agent, value):
    current = status(agent)
    if any(row.get("kind") == "browser" for row in current.get("sessions", [])):
        raise PolicyError(
            "Close the UDB browser session before changing saved-login preferences"
        )
    profile = agent.state / "host" / "browser-profile"
    reject_links(profile)
    if not value and profile.exists():
        if current.get("worker_ready") is not True:
            raise PolicyError(
                "Start the local UDB manager and close its browser before forgetting saved login"
            )
        shutil.rmtree(profile)
    atomic_json(agent.state / "browser-settings.json", {"remember_login": bool(value)})
