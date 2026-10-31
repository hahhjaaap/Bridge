# Remote-device connection diagnostics

UDB can accept requests from a connected ChatGPT workspace on a different device
or network while the original Windows computer is online and the owner-approved
agent remains running. **A locally ready tunnel does not prove that the current
ChatGPT plugin session can call its tools.**

## When the plugin displays Connected but calls fail

1. On the original Windows computer, run `udb status --json` and
   `udb doctor --json` in a local terminal. Neither command creates grants.
2. Check `manager_running`, `local_tunnel_ready`, and the
   `chatgpt_session` doctor check separately. A successful local health probe
   cannot validate the ChatGPT session from another device.
3. From the remote device, call `bridge_status` in ChatGPT. When it succeeds,
   verify the reported version and project scope before attempting other tools.
4. If a plugin reports **Connected** but tool calls return
   `Session terminated`, reconnect/refresh that ChatGPT plugin session and
   retry the **read-only** `bridge_status` check. If it still fails, inspect
   `udb doctor --json` and redacted `udb logs --details` on the original
   computer. Do not share keys or raw private logs in chat.
5. If the local tunnel is unavailable, troubleshoot the Windows agent and
   network first. A plugin UI badge alone is not sufficient evidence of
   connectivity.

A computer that is powered off, asleep or disconnected from the Internet
cannot serve remote requests. Network access from a second device is a
separate test from accessing UDB across two devices on the same network.

**Safety:** A failed call can have an unknown outcome. Never blindly replay
file writes, shell commands, browser actions, downloads or other mutations.
Inspect state before repeating them. UDB never uses a remote ChatGPT request
to enable computer-access permissions; the owner must activate any such grant
attended on the original computer.

This guide covers diagnosis, not a guarantee of automatic remote recovery.
