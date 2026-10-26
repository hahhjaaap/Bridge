"""Public tunnel identifiers and console input normalization; no authentication."""

from __future__ import annotations

import re
from urllib.parse import urlsplit

_TUNNEL_ID = re.compile(r"tunnel_(?:[a-z0-9]{4}_)?[a-z0-9]{32}")


def is_tunnel_id(value):
    return isinstance(value, str) and _TUNNEL_ID.fullmatch(value) is not None


def normalize_tunnel_id(value):
    """Accept an ID or the official MCP URL, preserving namespace aliases."""
    if not isinstance(value, str) or len(value) > 512:
        raise ValueError("Invalid tunnel ID or MCP URL.")
    text = value.strip()
    if is_tunnel_id(text):
        return text
    try:
        url = urlsplit(text)
        if (
            url.scheme == "https"
            and url.hostname == "api.openai.com"
            and url.port in (None, 443)
            and not url.username
            and not url.password
            and not url.query
            and not url.fragment
        ):
            match = re.fullmatch(r"/v1/mcp/([^/]+)/?", url.path)
            if match and is_tunnel_id(match[1]):
                return match[1]
    except ValueError:
        pass
    # Never put pasted values in an exception: an owner may paste a key by mistake.
    raise ValueError(
        "Invalid tunnel ID or MCP URL. Paste the full tunnel_ ID or its "
        "https://api.openai.com/v1/mcp/... URL."
    )
