"""Attended console setup. No credentials or owner decisions pass through MCP."""

from __future__ import annotations

import getpass
import json
import os
import sys
import uuid

from .config import checked, prepare_workspace
from .connection import is_tunnel_id, normalize_tunnel_id

TUNNELS_URL = "https://platform.openai.com/settings/organization/tunnels"
KEYS_URL = "https://platform.openai.com/settings/organization/api-keys"
CHATGPT_URL = "https://chatgpt.com/#settings/Connectors"


def attended_console():
    return bool(
        os.name == "nt"
        and sys.stdin
        and sys.stdout
        and sys.stdin.isatty()
        and sys.stdout.isatty()
    )


def saved_connection(settings, *, repair=False):
    from . import owner_command

    path = checked(settings.home / "state/connection.json")
    if not path.exists():
        return None
    try:
        data = owner_command.read_object(path, 4096)
    except (OSError, ValueError, owner_command.CommandError):
        if repair:
            return None
        raise ValueError(
            "Saved connection could not be read. Run udb setup --edit locally."
        ) from None
    value = data.get("tunnel_id")
    return value if is_tunnel_id(value) else None


def saved_mode(settings):
    from . import owner_command

    path = checked(settings.home / "state/startup-preferences.json")
    if not path.exists():
        return None
    try:
        data = owner_command.read_object(path, 512)
        if (
            set(data) != {"schema", "mode"}
            or type(data["schema"]) is not int
            or data["schema"] != 1
            or data["mode"] not in ("online", "offline")
        ):
            raise ValueError()
    except (OSError, ValueError, owner_command.CommandError):
        raise ValueError(
            "Saved startup mode is invalid. Run udb setup to select it again."
        ) from None
    return data["mode"]


def save_mode(settings, mode):
    if mode not in ("online", "offline"):
        raise ValueError("Invalid startup mode")
    path = checked(settings.home / "state/startup-preferences.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    checked(path)
    temporary = path.with_name(".startup-" + uuid.uuid4().hex + ".tmp")
    try:
        with temporary.open("x", encoding="utf-8") as stream:
            json.dump({"schema": 1, "mode": mode}, stream)
            stream.write("\n")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


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


def run(settings, *, edit=False, local=False):
    if os.name != "nt" or not sys.stdin or not sys.stdin.isatty():
        raise ValueError(
            "Setup requires your own attended Windows terminal; redirected input is refused."
        )
    if edit and local:
        raise ValueError("Choose either connection editing or local mode")
    from . import owner_command
    from .desktop import DesktopController

    # Validate the destination before changing an existing agent or connection.
    checked(settings.home / "state/startup-preferences.json")
    print("UDB console setup. Python, Node.js and Desktop Commander are not required.")
    current = None if local else saved_connection(settings, repair=edit)
    connect = not local and bool(current or edit)
    if not local and not connect:
        answer = input("Connect this PC to ChatGPT now? [Y/n]: ").strip().lower()
        if answer not in ("", "y", "yes", "n", "no"):
            raise ValueError("Choose Y or N; setup was not changed.")
        connect = answer in ("", "y", "yes")
    changed = False
    pending = False
    key = ""
    try:
        if connect and (edit or not current):
            connection_help()
            while True:
                prompt = (
                    "Tunnel ID or MCP URL (Enter keeps the current ID; cancel exits): "
                    if edit and current
                    else "Tunnel ID or MCP URL (? repeats help; Enter cancels): "
                )
                pasted = input(prompt).strip()
                if pasted != "?":
                    break
                connection_help()
            if pasted.lower() == "cancel" or (edit and not current and not pasted):
                print("Connection unchanged.")
                return 0
            if not pasted and not current:
                connect = False
            else:
                tunnel = normalize_tunnel_id(pasted or current)
                print("Runtime key comes from: " + KEYS_URL)
                key = getpass.getpass(
                    "Runtime key (hidden; Enter cancels without saving): "
                )
                if not key.strip():
                    if edit:
                        print("Connection unchanged.")
                        return 0
                    connect = False
                else:
                    from .credentials import normalize_key

                    key = normalize_key(key)
                    pending = True

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
                "Local mode selected. ChatGPT is not connected yet. "
                "Future udb start commands will reuse local mode. "
                "Run udb setup to connect later, or udb mcp-config for a local client."
            )

        prepare_workspace(settings)
        if pending:
            controller = DesktopController(settings.home, monitor=False)
            try:
                controller.configure(tunnel, key)
                changed = True
            except Exception:
                raise ValueError(
                    "Could not save the connection. Retry udb setup --edit locally."
                ) from None
            finally:
                controller.close()
    finally:
        key = ""

    state = owner_command.headless_state(settings.home) or {}
    desired = "online" if connect else "offline"
    if changed or (
        state.get("connection_mode") and state["connection_mode"] != desired
    ):
        print("Applying the selected connection settings...")
        if owner_command.main(
            ["stop", "--home", str(settings.home), "--wait", "60", "--json"]
        ):
            raise ValueError(
                "The previous agent did not stop. Run udb stop and try setup again."
            )
    save_mode(settings, desired)
    return owner_command.main(args)
