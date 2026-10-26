"""Attended console setup. No credentials or owner decisions pass through MCP."""

from __future__ import annotations

import getpass
import os
import sys

from .config import prepare_workspace
from .connection import is_tunnel_id, normalize_tunnel_id

TUNNELS_URL = "https://platform.openai.com/settings/organization/tunnels"
KEYS_URL = "https://platform.openai.com/settings/organization/api-keys"
CHATGPT_URL = "https://chatgpt.com/#settings/Connectors"


def connection_help():
    print(
        "1/3 Create or reuse your tunnel in the intended OpenAI organization and "
        "ChatGPT workspace:\n"
        + TUNNELS_URL
        + "\n2/3 Create a restricted runtime key with Tunnels Read + Use:\n"
        + KEYS_URL
        + "\n3/3 Once UDB is running, add or finish your ChatGPT MCP connection, "
        "choose Connection: Tunnel, and select that same tunnel:\n"
        + CHATGPT_URL
        + "\nTunnel access depends on your account/workspace. If it is unavailable, "
        "ask its administrator or use the generated local stdio MCP configuration. "
        "A ChatGPT account alone does not provision a tunnel."
    )


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
        configured = is_tunnel_id(data.get("tunnel_id"))
    connect = (
        configured
        or input("Connect this PC to your own ChatGPT tunnel now? [y/N]: ")
        .strip()
        .lower()
        == "y"
    )
    if connect and not configured:
        connection_help()
        while True:
            pasted = input(
                "Tunnel ID or MCP URL (? repeats help, Enter uses local mode): "
            ).strip()
            if pasted != "?":
                break
            connection_help()
        if pasted:
            tunnel = normalize_tunnel_id(pasted)
            print("Runtime key comes from: " + KEYS_URL)
            key = getpass.getpass(
                "Runtime key (hidden; Enter cancels without saving): "
            )
            try:
                if key:
                    from .credentials import normalize_key

                    normalize_key(key)
                    prepare_workspace(settings)
                    controller = DesktopController(settings.home, monitor=False)
                    try:
                        controller.configure(tunnel, key)
                    finally:
                        controller.close()
                else:
                    connect = False
            finally:
                key = ""
        else:
            connect = False
    prepare_workspace(settings)
    args = ["start", "--home", str(settings.home), "--wait", "60", "--follow"]
    if connect:
        print(
            "Finish or verify the ChatGPT connection at "
            + CHATGPT_URL
            + "\nChoose Connection: Tunnel and the same Tunnel ID. "
            "Local readiness alone does not verify a ChatGPT tool call."
        )
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
            "Local runtime is ready. ChatGPT is not connected yet. "
            "Run udb mcp-config for a local stdio client, or udb setup to connect later."
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
