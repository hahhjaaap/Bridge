"""Optional current-user computer access, issued only from an attended local console.

No MCP tool exposes the setter. Broad command execution is NOT a sandbox and
cannot preserve a promise that arbitrary commands cannot access protected data.
"""
from __future__ import annotations
import json
import os
from pathlib import Path
import sys
import time
import uuid
from .core import PolicyError, reject_links

NAME = "computer-access.dpapi"
ENTROPY = b"UDB/current-user-computer-access/v1"
CAPABILITIES = ("files", "execute", "desktop")


def identity():
    import win32api, win32con, win32security
    token = win32security.OpenProcessToken(win32api.GetCurrentProcess(), win32con.TOKEN_QUERY)
    try:
        return win32security.ConvertSidToStringSid(win32security.GetTokenInformation(token, win32security.TokenUser)[0])
    finally:
        token.Close()


def load(agent, unprotect=None, sid=None):
    try:
        path = agent.state / NAME
        reject_links(path)
        with path.open("rb") as stream:
            blob = stream.read(16385)
        if len(blob) > 16384:
            return None
        if unprotect is None:
            import win32crypt
            unprotect = lambda b: win32crypt.CryptUnprotectData(b, ENTROPY, None, None, 1)[1]
        data = json.loads(unprotect(blob))
        if (not isinstance(data, dict) or data.get("policy") != 1 or
                data.get("home") != str(agent.home.resolve()) or
                data.get("sid") != (identity() if sid is None else sid) or
                data.get("capabilities") != list(CAPABILITIES) or
                not isinstance(data.get("id"), str) or len(data["id"]) != 32 or
                data.get("acknowledged_unsandboxed") is not True):
            return None
        return data
    except Exception:
        # Never echo DPAPI payloads, OS credential exceptions or identity details.
        return None


def summary(agent):
    grant = load(agent)
    return dict(enabled=bool(grant), capabilities=list(CAPABILITIES) if grant else [],
                scope="current_windows_user" if grant else "disabled",
                elevated_by_udb=False, sandboxed=False,
                activation="Run udb --access computer in your own local terminal; no MCP tool can grant this access.")


def require(agent):
    grant = load(agent)
    if not grant:
        raise PolicyError("Computer access is disabled. The owner must run udb --access computer locally. AI cannot grant permission.")
    return grant["id"]


def enable_local(agent, input_stream=None, output_stream=None):
    incoming = sys.stdin if input_stream is None else input_stream
    outgoing = sys.stdout if output_stream is None else output_stream
    # Never turn redirected stdin into a console read. PowerShell may pipe only
    # stdout in an attended terminal; that case may reopen console OUTPUT only.
    if not incoming or not incoming.isatty():
        raise PolicyError("Computer access requires an attended Windows terminal, never redirected input or an MCP request.")
    if (os.name=='nt' and input_stream is None and output_stream is None and
            (not outgoing or not outgoing.isatty())):
        try:
            with open('CONOUT$','w',encoding='utf-8') as console_out:
                return enable_local(agent,incoming,console_out)
        except OSError:
            raise PolicyError('Computer access requires your own attended Windows console; no console was available.') from None
    if os.name != "nt" or not incoming or not outgoing or not incoming.isatty() or not outgoing.isatty():
        raise PolicyError("Computer access requires an attended Windows terminal, never redirected input or an MCP request.")
    if load(agent):return summary(agent)
    import win32crypt
    outgoing.write("Enable UDB computer access for this Windows account?\n"
                   "This permits file changes, program/shell execution, screenshots and real mouse/keyboard input.\n"
                   "Commands are not sandboxed and can access data your account can access, including protected folders.\n"
                   "Windows/UAC restrictions remain. Revoke later with: udb revoke-access\n"
                   "Type ALLOW COMPUTER to enable, or press Enter to cancel: ")
    outgoing.flush()
    if incoming.readline().strip() != "ALLOW COMPUTER":
        raise PolicyError("Computer access was not enabled.")
    record = dict(policy=1, id=uuid.uuid4().hex, home=str(agent.home.resolve()),
                  sid=identity(), issued=time.time(), capabilities=list(CAPABILITIES),
                  acknowledged_unsandboxed=True)
    blob = win32crypt.CryptProtectData(json.dumps(record).encode(), "UDB owner computer access", ENTROPY, None, None, 1)
    target = agent.state / NAME
    reject_links(target)
    tmp = target.with_name(target.name+"."+uuid.uuid4().hex+".tmp")
    try:
        with tmp.open("xb") as stream:
            stream.write(blob)
        tmp.replace(target)
    finally:
        tmp.unlink(missing_ok=True)
    agent.audit("computer_access_enabled_locally", {"scope":"current_windows_user"})
    return summary(agent)


def revoke_local(agent):
    path = agent.state / NAME
    reject_links(path)
    path.unlink(missing_ok=True)
    agent.audit("computer_access_revoked_locally", {})
    return dict(enabled=False)
