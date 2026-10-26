"""Companion MCP schemas; importing the catalog does not patch runtime classes."""

from __future__ import annotations

from typing import Any

from .extensions import EXTENSION_TOOLS
from .features import TOOLS as FEATURE_TOOLS

TOOLS: list[dict[str, Any]] = [
    {
        "name": "terminal_start",
        "description": "Start a process inside an approved project and return PASS/RUNNING/ERROR. No GUI.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "program": {"type": "string"},
                "arguments": {"type": "array", "items": {"type": "string"}},
                "cwd": {"type": "string"},
                "wait_ms": {"type": "integer"},
                "timeout_seconds": {"type": "integer"},
                "memory_mib": {"type": "integer"},
                "mode": {"type": "string", "enum": ["pipe", "conpty"]},
                "columns": {"type": "integer"},
                "rows": {"type": "integer"},
            },
            "required": ["program"],
        },
    },
    {
        "name": "terminal_read",
        "description": "Read command output and exit state by session ID and offset.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "session_id": {"type": "string"},
                "offset": {"type": "integer"},
                "wait_ms": {"type": "integer"},
            },
            "required": ["session_id"],
        },
    },
    {
        "name": "terminal_send",
        "description": "Write text/REPL input to a managed terminal session.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "session_id": {"type": "string"},
                "text": {"type": "string"},
            },
            "required": ["session_id", "text"],
        },
    },
    {
        "name": "terminal_stop",
        "description": "Stop only a process started by this companion.",
        "inputSchema": {
            "type": "object",
            "properties": {"session_id": {"type": "string"}},
            "required": ["session_id"],
        },
    },
    {
        "name": "file_list",
        "description": "List readable project files.",
        "inputSchema": {
            "type": "object",
            "properties": {"path": {"type": "string"}, "limit": {"type": "integer"}},
        },
    },
    {
        "name": "file_read",
        "description": "Read UTF-8 file with offset, limit and SHA256.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "offset": {"type": "integer"},
                "limit": {"type": "integer"},
            },
            "required": ["path"],
        },
    },
    {
        "name": "file_write",
        "description": "Atomic write; NEW for create, SHA256 for replacing existing file.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "content": {"type": "string"},
                "expected_sha256": {"type": "string"},
            },
            "required": ["path", "content"],
        },
    },
    {
        "name": "file_replace",
        "description": "Atomic guarded exact replacement with recovery copy.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "old": {"type": "string"},
                "new": {"type": "string"},
                "expected_sha256": {"type": "string"},
                "occurrences": {"type": "integer"},
            },
            "required": ["path", "old", "new", "expected_sha256"],
        },
    },
    {
        "name": "file_search",
        "description": "Search files in project by glob and optional text.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "pattern": {"type": "string"},
                "text": {"type": "string"},
                "limit": {"type": "integer"},
            },
        },
    },
]

TOOLS.extend(EXTENSION_TOOLS)
TOOLS.extend(FEATURE_TOOLS)
