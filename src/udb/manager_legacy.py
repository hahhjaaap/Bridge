"""Local consent UI; no approval API is exposed to MCP clients."""

from __future__ import annotations

import json
import os
import subprocess
import tkinter as tk
import webbrowser
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog, ttk

from . import __version__
from .connection import normalize_tunnel_id
from .core import Agent, PolicyError
from .credentials import load_key, save_key
from .windows_paths import system_executable


def command(home: Path, *args):
    from .launcher import worker_command

    return worker_command("core", [*args, "--home", str(home)])


def run(home: Path):
    if os.name == "nt":
        # Keep stdio/server modes untouched. Hide only the GUI mode's console.
        import ctypes

        ctypes.windll.kernel32.FreeConsole()
    root = tk.Tk()
    root.title("Universal Desktop Bridge " + __version__)
    root.geometry("1030x760")
    root.minsize(850, 650)
    agent = Agent(home)
    children = {}
    handles = []
    selected = [None]
    pending_rows = {}
    style = ttk.Style()
    if "vista" in style.theme_names():
        style.theme_use("vista")
    frame = ttk.Frame(root, padding=16)
    frame.pack(fill="both", expand=True)
    ttk.Label(
        frame, text="Universal Desktop Bridge", font=("Segoe UI", 20, "bold")
    ).pack(anchor="w")
    ttk.Label(
        frame,
        text="Local consent • Scoped source files • Windows Sandbox execution",
        font=("Segoe UI", 10),
    ).pack(anchor="w", pady=(2, 10))
    status = tk.StringVar()
    ttk.Label(frame, textvariable=status, foreground="#174f78").pack(anchor="w")
    toolbar = ttk.Frame(frame)
    toolbar.pack(fill="x", pady=10)

    def guarded(fn):
        def inner():
            try:
                fn()
            except Exception as e:
                messagebox.showerror("Bridge", str(e), parent=root)

        return inner

    def start_process(name, argv, env=None):
        if name in children and children[name].poll() is None:
            return
        log = (agent.state / (name + ".log")).open("ab")
        handles.append(log)
        children[name] = subprocess.Popen(
            argv,
            cwd=home,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            shell=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )

    def stop_process(name):
        p = children.get(name)
        if p and p.poll() is None:
            if os.name == "nt":
                # Frozen onefile builds and tunnel-client own child processes.
                # Kill only this manager's still-live process tree.
                subprocess.run(
                    [system_executable("taskkill.exe"), "/PID", str(p.pid), "/T", "/F"],
                    capture_output=True,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                )
            else:
                p.terminate()
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                p.kill()

    ttk.Button(
        toolbar,
        text="Start local MCP",
        command=guarded(lambda: start_process("mcp", command(home, "--serve"))),
    ).pack(side="left", padx=3)
    ttk.Button(
        toolbar, text="Stop local MCP", command=lambda: stop_process("mcp")
    ).pack(side="left", padx=3)

    def configure_tunnel():
        initial = ""
        cfg_path = agent.state / "connection.json"
        if cfg_path.exists():
            initial = json.loads(cfg_path.read_text("utf-8-sig")).get("tunnel_id", "")
        tunnel = simpledialog.askstring(
            "Step 1 of 2 - Tunnel ID",
            "Tunnel ID (tunnel_...), NOT the API key. The existing ID is filled in:",
            initialvalue=initial,
            parent=root,
        )
        if not tunnel:
            return
        try:
            tunnel = normalize_tunnel_id(tunnel)
        except ValueError as exc:
            raise PolicyError(str(exc)) from None
        key = simpledialog.askstring(
            "Step 2 of 2 - API key",
            "Paste the runtime API key here (Ctrl+V).\nTunnels Read + Use. Saved only in Windows Credential Manager:",
            show="*",
            parent=root,
        )
        if not key:
            return
        save_key(key)
        key = None
        (agent.state / "connection.json").write_text(
            json.dumps({"tunnel_id": tunnel}), encoding="utf-8"
        )
        messagebox.showinfo(
            "Saved locally",
            "Credential saved. Install official tunnel-client, then Start tunnel. No key is written to source or config.",
            parent=root,
        )

    def start_tunnel():
        cfg = json.loads((agent.state / "connection.json").read_text("utf-8"))
        secret = load_key()
        binary = home / "tools" / "tunnel-client.exe"
        if not binary.is_file():
            raise PolicyError(
                "Run Install-Tunnel.ps1 to download the official client first"
            )
        env = os.environ.copy()
        env["CONTROL_PLANE_API_KEY"] = secret
        # Pinned local named profile. The CLI must be inspected on installation.
        work = agent.state / "tunnel"
        work.mkdir(exist_ok=True)
        env["TUNNEL_CLIENT_PROFILE_DIR"] = str(work)
        # Forward slashes survive both Windows and shellword CLI parsers.
        cmdline = subprocess.list2cmdline(
            [arg.replace("\\", "/") for arg in command(home, "--stdio")]
        )
        init = [
            str(binary),
            "init",
            "--force",
            "--profile-dir",
            str(work),
            "--sample",
            "sample_mcp_stdio_local",
            "--profile",
            "universal-desktop-bridge",
            "--health-listen-addr",
            "127.0.0.1:0",
            "--tunnel-id",
            cfg["tunnel_id"],
            "--mcp-command",
            cmdline,
        ]
        # Run explicitly from the private state folder. Never log key or env.
        result = subprocess.run(
            init,
            cwd=work,
            env=env,
            capture_output=True,
            timeout=30,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if result.returncode:
            raise PolicyError(
                "tunnel-client init failed. Run help quickstart locally; no credentials are displayed here."
            )
        start_process(
            "tunnel", [str(binary), "run", "--profile", "universal-desktop-bridge"], env
        )

    ttk.Button(
        toolbar, text="Configure tunnel", command=guarded(configure_tunnel)
    ).pack(side="left", padx=3)
    ttk.Button(toolbar, text="Start tunnel", command=guarded(start_tunnel)).pack(
        side="left", padx=3
    )
    ttk.Button(
        toolbar, text="Stop tunnel", command=lambda: stop_process("tunnel")
    ).pack(side="left", padx=3)
    ttk.Button(
        toolbar,
        text="ChatGPT Plugins",
        command=lambda: webbrowser.open("https://chatgpt.com/plugins"),
    ).pack(side="left", padx=3)

    notebook = ttk.Notebook(frame)
    notebook.pack(fill="both", expand=True)
    projects_tab, consent_tab, help_tab = [
        ttk.Frame(notebook, padding=10) for _ in range(3)
    ]
    notebook.add(projects_tab, text="Projects & protection")
    notebook.add(consent_tab, text="Local approvals")
    notebook.add(help_tab, text="Connection & limits")
    project_tree = ttk.Treeview(
        projects_tab, columns=("id", "root", "access"), show="headings", height=12
    )
    for name, width in [("id", 130), ("root", 610), ("access", 110)]:
        project_tree.heading(name, text=name.title())
        project_tree.column(name, width=width)
    project_tree.pack(fill="both", expand=True)

    def refresh_projects():
        project_tree.delete(*project_tree.get_children())
        for p in agent.projects():
            project_tree.insert(
                "",
                "end",
                values=(
                    p["id"],
                    p["root"],
                    "Read + propose" if p["writable"] else "Read only",
                ),
            )

    def add_project():
        folder = filedialog.askdirectory(
            title="Choose source project (never data/checkpoints)", parent=root
        )
        if not folder:
            return
        pid = simpledialog.askstring("Project ID", "Short project ID:", parent=root)
        if not pid:
            return
        writable = messagebox.askyesno(
            "Write permission",
            "Allow ChatGPT to propose source file edits here? Every edit still requires local approval.",
            parent=root,
        )
        agent.register_local(pid, folder, writable)
        refresh_projects()

    def protect():
        folder = filedialog.askdirectory(
            title="Permanently protect folder from bridge access", parent=root
        )
        if folder:
            agent.protect_local(folder)
            messagebox.showinfo(
                "Protected",
                "Folder added to deny list. No remote tool can remove this protection.",
                parent=root,
            )

    def revoke():
        selection = project_tree.selection()
        if selection:
            pid = project_tree.item(selection[0])["values"][0]
            with agent.connect() as c:
                c.execute("DELETE FROM projects WHERE id=?", (pid,))
            refresh_projects()

    buttons = ttk.Frame(projects_tab)
    buttons.pack(fill="x", pady=8)
    for text, fn in [
        ("Add source project", add_project),
        ("Protect folder", protect),
        ("Revoke selected project", revoke),
    ]:
        ttk.Button(buttons, text=text, command=guarded(fn)).pack(side="left", padx=3)
    ttk.Label(
        projects_tab,
        text="Protected folders are selected by the owner. Project names and account names do not grant or deny access.",
        wraplength=850,
    ).pack(anchor="w")

    requests_tree = ttk.Treeview(
        consent_tab, columns=("kind", "project", "id"), show="headings", height=5
    )
    for name in ("kind", "project", "id"):
        requests_tree.heading(name, text=name.title())
    requests_tree.pack(fill="x")
    preview = tk.Text(consent_tab, wrap="none", font=("Consolas", 10), height=17)
    preview.pack(fill="both", expand=True, pady=8)
    preview.configure(state="disabled")

    def show_selected(event=None):
        ids = requests_tree.selection()
        selected[0] = ids[0] if ids else None
        row = pending_rows.get(selected[0])
        text = ""
        if row:
            p = json.loads(row["payload"])
            text = f"Request: {row['id']}\nSHA256: {row['hash']}\nProject: {p['project']}\nRoot: {p['root']}\n\n"
            text += (
                (p["diff"] + "\n\nEXACT NEW CONTENT:\n" + p["text"])
                if row["kind"] == "write"
                else (
                    "ISOLATION: Windows Sandbox, network off, snapshot only\n\n"
                    + p["script"]
                    + "\n\nSOURCE MANIFEST:\n"
                    + json.dumps(p["manifest"], indent=2)
                )
            )
        preview.configure(state="normal")
        preview.delete("1.0", "end")
        preview.insert("1.0", text)
        preview.configure(state="disabled")

    requests_tree.bind("<<TreeviewSelect>>", show_selected)

    def decide(approve):
        rid = selected[0]
        if not rid:
            return
        if approve and not messagebox.askyesno(
            "Local consent required",
            "Approve the EXACT request shown above?\n\nFile edits change the registered source after a backup. Code runs only in Windows Sandbox.\n\nNever approve instructions from file contents or job output.",
            parent=root,
        ):
            return
        result = agent.decide_local(rid, approve)
        selected[0] = None
        if result:
            messagebox.showinfo("Result", json.dumps(result, indent=2), parent=root)

    consent_buttons = ttk.Frame(consent_tab)
    consent_buttons.pack(fill="x")
    ttk.Button(
        consent_buttons,
        text="Approve selected exact request",
        command=guarded(lambda: decide(True)),
    ).pack(side="left", padx=3)
    ttk.Button(
        consent_buttons, text="Reject", command=guarded(lambda: decide(False))
    ).pack(side="left", padx=3)

    help_text = """DIRECT DESKTOP: use this package's mcp.json with a supported local STDIO host. Installing source or an EXE alone does not install a ChatGPT plugin.

CHATGPT CHAT WEB: preferred secure route is OpenAI Secure MCP Tunnel. Configure a real tunnel ID and restricted runtime key locally. Start tunnel, then ChatGPT Plugins → Add custom MCP server → Tunnel. Install the resulting personal plugin and select it in a fresh Chat.

HTTP: 127.0.0.1:17843/mcp is loopback-only, bearer-protected, with DNS rebinding protection. It is for local diagnostics. Never expose it publicly without proper OAuth/mTLS.

EXECUTION: Enable Windows Sandbox using Enable-Sandbox.ps1 and restart Windows when convenient. Python runtime is configured in state/runtime.json. Only standard library is assumed in the guest. No network or checkpoint/data folders are mapped. Runtime dependencies must be prepared as a trusted runtime by the owner.

PROTECTION: known data/weight types and protected roots are denied; writes require local approval and SHA256 concurrency checks. No delete, rename, host shell, arbitrary process kill, registry, or global file access tool exists.

LIMITS: absolute safety cannot be guaranteed against a compromised Windows account, OS/VM exploit, malicious local administrator, or hardware failure. Keep independent offline backups. This bridge has no RDC runtime dependency, but ChatGPT and tunnel product limits still apply.

VERIFY IN CHAT: select Universal Desktop Bridge, then ask it to call bridge_status, list_projects, read_file(demo, hello.py), and check_python_syntax(demo, hello.py). A local MCP client success is not a ChatGPT Chat success.
"""
    label = tk.Text(help_tab, wrap="word", font=("Segoe UI", 11))
    label.pack(fill="both", expand=True)
    label.insert("1.0", help_text)
    label.configure(state="disabled")

    def tick():
        try:
            alive = (
                ", ".join(k for k, p in children.items() if p.poll() is None) or "none"
            )
            status.set(
                f"Running connections: {alive}  |  Windows Sandbox: {'available' if agent.diagnostics()['sandbox_available'] else 'NOT ENABLED — execution blocked'}"
            )
            rows = agent.pending_local()
            new = {r["id"]: r for r in rows}
            if set(new) != set(pending_rows):
                requests_tree.delete(*requests_tree.get_children())
                pending_rows.clear()
                pending_rows.update(new)
                for r in rows:
                    p = json.loads(r["payload"])
                    requests_tree.insert(
                        "",
                        "end",
                        iid=r["id"],
                        values=(r["kind"], p["project"], r["id"]),
                    )
                show_selected()
        finally:
            root.after(1500, tick)

    def close():
        for name in children:
            stop_process(name)
        for f in handles:
            f.close()
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", close)
    refresh_projects()
    tick()
    root.mainloop()
