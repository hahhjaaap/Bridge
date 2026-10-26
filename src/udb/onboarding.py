"""Attended console setup. No credentials or owner decisions pass through MCP."""

from __future__ import annotations

import getpass
import os
import re
import sys

from .config import prepare_workspace


def run(settings):
    if os.name != "nt" or not sys.stdin or not sys.stdin.isatty():
        raise ValueError(
            "Setup requires your own attended Windows terminal; redirected input is refused."
        )
    from . import owner_command
    from .desktop import DesktopController

    print(
        "UDB first-run setup. Python, Node.js and Desktop Commander are not required."
    )
    connection = settings.home / "state/connection.json"
    configured = False
    if connection.exists():
        data = owner_command.read_object(connection, 4096)
        tunnel = data.get("tunnel_id", "")
        configured = isinstance(tunnel, str) and bool(
            re.fullmatch(r"tunnel_[a-z0-9]{32}", tunnel)
        )
    connect = (
        configured
        or input("Connect this PC to your own ChatGPT tunnel now? [y/N]: ")
        .strip()
        .lower()
        == "y"
    )
    if connect and not configured:
        print(
            "Use your own supported tunnel configuration. This installer does not create a ChatGPT account or a tunnel."
        )
        tunnel = input("Tunnel ID: ").strip()
        if not re.fullmatch(r"tunnel_[a-z0-9]{32}", tunnel):
            raise ValueError("Invalid tunnel ID; configuration was not saved.")
        key = getpass.getpass(
            "Runtime key (hidden, saved only in Windows Credential Manager): "
        )
        from .credentials import normalize_key

        normalize_key(key)
        prepare_workspace(settings)
        controller = DesktopController(settings.home, monitor=False)
        try:
            controller.configure(tunnel, key)
        finally:
            controller.close()
            key = ""
    else:
        prepare_workspace(settings)
    args = ["start", "--home", str(settings.home), "--wait", "60", "--follow"]
    if connect:
        if (
            input("Open the local computer-access consent prompt? [y/N]: ")
            .strip()
            .lower()
            == "y"
        ):
            args.extend(["--access", "computer"])
    else:
        args.append("--offline")
        print(
            "Local runtime is ready. ChatGPT is not connected yet. Run udb setup when you have your own tunnel configuration."
        )
    state = owner_command.headless_state(settings.home) or {}
    desired = "online" if connect else "offline"
    if state.get("connection_mode") and state["connection_mode"] != desired:
        print("Switching the local agent to the selected connection mode...")
        if owner_command.main(
            ["stop", "--home", str(settings.home), "--wait", "60", "--json"]
        ):
            raise ValueError("The previous agent did not stop; setup preserved it.")
    return owner_command.main(args)
