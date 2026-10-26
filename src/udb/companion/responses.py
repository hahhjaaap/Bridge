"""Text responses shared by the Companion's explicit tool dispatchers."""

from __future__ import annotations

from typing import Any


def respond_text(status: str, headline: str, details: str = "") -> dict[str, Any]:
    readable = f"{status} | {headline}"
    if details:
        readable += "\n" + details.rstrip()
    return {
        "content": [{"type": "text", "text": readable}],
        "isError": status == "ERROR",
    }
