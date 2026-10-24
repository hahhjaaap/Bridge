"""UDB v2 companion MCP server: scoped file tools and interactive terminal.

Stdlib-only; no GUI; never changes existing UniversalDesktopBridge.exe or permissions.
Example: python -u udb_companion.py --root PROJECT_ROOT
"""
from __future__ import annotations

import argparse
import collections
import codecs
import fnmatch
import hashlib
import json
import os
import queue
import shutil
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from typing import Any
sys.path.insert(0, str(Path(__file__).resolve().parent))
from udb_managed_process import ManagedProcess, watch_parent

VERSION = "0.5.0"
MAX_SESSIONS = 2
MAX_RETAINED_SESSIONS = 64
MAX_BUFFER_BYTES = 1024 * 1024
MAX_READ_BYTES = 128 * 1024
MAX_TERMINAL_OUTPUT_BYTES = 10 * 1024
MAX_FILE_BYTES = 8 * 1024 * 1024
MAX_SCAN_FILES = 20000
MAX_OUTPUT_CHARS = 14000
HIDDEN_DIRS = {".git", ".udb-recovery", ".udb-work-state", "__pycache__", ".venv", "venv", "node_modules"}
HIDDEN_DIRS_CASEFOLD = {name.casefold() for name in HIDDEN_DIRS}
PROTECTED_NAMES = {".env", "id_rsa", "id_ed25519", "credentials.json", "connection.json", "runtime.json", "http-token", "computer-access.dpapi"}
PROTECTED_SUFFIXES = {".pem", ".p12", ".pfx", ".key", ".ckpt", ".safetensors", ".pt", ".pth"}
PROTECTED_NAMES.update({"checkpoints", "datasets", "aiviet", "aiviet_datasets", "aiviet_quarantine"})


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def respond_text(status: str, headline: str, details: str = "") -> dict[str, Any]:
    readable = f"{status} | {headline}"
    if details:
        readable += "\n" + details.rstrip()
    return {"content": [{"type": "text", "text": readable}], "isError": status == "ERROR"}


def is_reparse(path: Path) -> bool:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    return path.is_symlink() or bool(getattr(info, "st_file_attributes", 0) & 0x400)


class ScopedFiles:
    def __init__(self, root: str):
        home = Path(root).expanduser().absolute()
        if not home.is_dir() or is_reparse(home):
            raise ValueError("Project root must exist and must not be a link")
        self.root = home.resolve(strict=True)

    def path(self, given: str, allow_missing: bool = False) -> Path:
        if not isinstance(given, str) or not given or "\x00" in given:
            raise ValueError("Missing or invalid file path")
        p = Path(given)
        raw = (p if p.is_absolute() else self.root / p).absolute()
        full = Path(os.path.normpath(str(raw)))
        if os.name == "nt" and (str(full).startswith("\\\\") or ":" in str(full)[2:]):
            raise PermissionError("UNC and alternate data streams are not allowed")
        try:
            rel = full.relative_to(self.root)
        except ValueError:
            raise PermissionError("Path is outside the authorized project")
        if any(part.casefold() in PROTECTED_NAMES or part.lower().endswith(tuple(PROTECTED_SUFFIXES))
               for part in rel.parts):
            raise PermissionError("Protected file is not accessible")
        if any(part.casefold().startswith((".udb-stage-", ".udb-upload-")) or part.casefold() in HIDDEN_DIRS_CASEFOLD for part in rel.parts):
            raise PermissionError("Protected directory is not accessible")
        walk = self.root
        for item in rel.parts:
            walk = walk / item
            if is_reparse(walk):
                raise PermissionError("Links / reparse points are not allowed")
        if not full.resolve(strict=False).is_relative_to(self.root):
            raise PermissionError("Resolved path is outside the authorized project")
        if full.is_file() and full.stat().st_nlink > 1:
            raise PermissionError("Hardlinked data files are not allowed")
        if not allow_missing and not full.exists():
            raise FileNotFoundError("File or directory not found")
        return full

    def list(self, path: str = ".", limit: int = 100) -> dict[str, Any]:
        base = self.path(path)
        if not base.is_dir():
            raise NotADirectoryError("Target is not a directory")
        count = max(1, min(int(limit), 500))
        items = []
        for entry in sorted(base.iterdir(), key=lambda p: p.name.casefold()):
            try:
                safe = self.path(str(entry))
            except (PermissionError, ValueError):
                continue
            items.append({"name": safe.name, "kind": "dir" if safe.is_dir() else "file",
                          "size": safe.stat().st_size if safe.is_file() else None})
            if len(items) == count:
                break
        return {"path": str(base.relative_to(self.root)), "items": items, "limit": count}

    def read(self, path: str, offset: int = 0, limit: int = 8192) -> dict[str, Any]:
        p = self.path(path)
        if not p.is_file():
            raise ValueError("Target is not a file")
        if p.stat().st_size > MAX_FILE_BYTES:
            raise ValueError("File is too large; use a dedicated large-file processor")
        start = max(0, int(offset))
        count = max(1, min(MAX_READ_BYTES, int(limit)))
        hasher = hashlib.sha256()
        with p.open("rb") as stream:
            size = os.fstat(stream.fileno()).st_size
            if size > MAX_FILE_BYTES:
                raise ValueError("File is too large; use a dedicated large-file processor")
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                hasher.update(block)
            stream.seek(start)
            view = stream.read(count)
        try:
            displayed = view.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError("Read range must contain valid UTF-8; binary files are unsupported") from exc
        return {"path": str(p.relative_to(self.root)), "content": displayed,
                "sha256": hasher.hexdigest(), "size": size, "next_offset": start + len(view),
                "remaining": max(0, size-start-len(view))}

    def write(self, path: str, content: str, expected_sha256: str = "NEW") -> dict[str, Any]:
        p = self.path(path, allow_missing=True)
        if not isinstance(content, str):
            raise ValueError("content must be text")
        data = content.encode("utf-8")
        if len(data) > MAX_FILE_BYTES:
            raise ValueError("8 MiB write limit exceeded")
        p.parent.mkdir(parents=True, exist_ok=True)
        is_existing = p.exists()
        if is_existing and not p.is_file():
            raise ValueError("Target is not a normal file")
        if expected_sha256 == "NEW":
            if is_existing:
                raise FileExistsError("File already exists. Supply its current SHA256 to replace.")
        elif not re.fullmatch(r"[0-9a-f]{64}", expected_sha256, re.I):
            raise ValueError("expected_sha256 must be NEW or a 64-char SHA256")
        if is_existing:
            if p.stat().st_size > MAX_FILE_BYTES:
                raise ValueError("Existing file exceeds 8 MiB guarded-write limit")
            previous = p.read_bytes()
            if digest(previous).lower() != expected_sha256.lower():
                raise ValueError("File changed since reading; write cancelled")
        elif expected_sha256 != "NEW":
            raise FileNotFoundError("Cannot replace a file that no longer exists")
        handle, temp_name = tempfile.mkstemp(prefix=".udb-write-", dir=str(p.parent))
        try:
            with os.fdopen(handle, "wb") as target:
                target.write(data)
                target.flush()
                os.fsync(target.fileno())
            if not is_existing:
                # Atomic create-only: hard links refuse an existing destination.
                # Both names are on the same filesystem.
                os.link(temp_name, p)
            else:
                # Best effort CAS; noncooperating external writers may still
                # race a replacement. Never suppress failed comparisons.
                if digest(p.read_bytes()).lower() != expected_sha256.lower():
                    raise ValueError("File changed before commit; write cancelled")
                if not p.name.startswith(".udb-companion-test-"):
                    recovery = self.root / ".udb-recovery"
                    recovery.mkdir(exist_ok=True)
                    backup = recovery / (digest(str(p).encode())[:12] + "-" + str(int(time.time()*1000)) + ".bak")
                    with backup.open("xb") as b:
                        b.write(previous)
                        b.flush()
                        os.fsync(b.fileno())
                os.replace(temp_name, p)
        finally:
            if os.path.exists(temp_name):
                os.unlink(temp_name)
        return {"path": str(p.relative_to(self.root)), "bytes": len(data), "sha256": digest(data),
                "created": not is_existing}

    def replace(self, path: str, old: str, new: str, expected_sha256: str, occurrences: int = 1) -> dict[str, Any]:
        p = self.path(path)
        data = p.read_bytes()
        if len(data) > MAX_FILE_BYTES:
            raise ValueError("File too large")
        if digest(data) != expected_sha256:
            raise ValueError("File changed since reading; edit cancelled")
        source = data.decode("utf-8")
        actual = source.count(old) if old else 0
        if actual != int(occurrences) or actual == 0:
            raise ValueError(f"Expected {occurrences} matches, found {actual}; edit cancelled")
        return self.write(path, source.replace(old, new), expected_sha256) | {"replacements": actual}

    def search(self, path: str = ".", pattern: str = "*", text: str = "", limit: int = 30) -> dict[str, Any]:
        root = self.path(path)
        if not root.is_dir():
            raise NotADirectoryError("Search path must be a folder")
        matches = []
        scanned = 0
        cap = max(1, min(200, int(limit)))
        for base, dirs, files in os.walk(root, followlinks=False):
            dirs[:] = [d for d in dirs if d.casefold() not in HIDDEN_DIRS_CASEFOLD and not is_reparse(Path(base)/d)]
            for name in files:
                scanned += 1
                if scanned > MAX_SCAN_FILES:
                    return {"matches": matches, "scanned": MAX_SCAN_FILES, "truncated": True}
                if not fnmatch.fnmatch(name, pattern):
                    continue
                p = Path(base) / name
                try:
                    p = self.path(str(p))
                    if not p.is_file():
                        continue
                    if text:
                        if p.stat().st_size > 1024 * 1024:
                            continue
                        if text.casefold() not in p.read_text(encoding="utf-8", errors="replace").casefold():
                            continue
                except (OSError, ValueError, PermissionError):
                    continue
                matches.append(str(p.relative_to(self.root)))
                if len(matches) >= cap:
                    return {"matches": matches, "scanned": scanned, "truncated": True}
        return {"matches": matches, "scanned": scanned, "truncated": False}


class TerminalSession:
    def __init__(self, process: subprocess.Popen[bytes], command: list[str]):
        self.id = uuid.uuid4().hex[:12]
        self.process = process
        self.command = command
        self.chunks: collections.deque[tuple[int, bytes]] = collections.deque()
        self.position = 0
        self.read_cursor = 0
        self.lock = threading.Lock()
        self.arrival = threading.Event()
        self.done = False
        self.tail=b'';self.last_output=0;self.reason='';self.deadline=time.monotonic()+3600;self.mode='pipe'
        self.input_queue=queue.Queue(maxsize=4);self.input_done=threading.Event()
        threading.Thread(target=self._write_input,daemon=True,name='UDB terminal input').start()
        self.thread = threading.Thread(target=self._pump, name=f"udb-pump-{self.id}", daemon=True)
        self.thread.start()

    def _pump(self):
        try:
            assert self.process.stdout
            while True:
                piece = os.read(self.process.stdout.fileno(), 4096)
                if not piece:
                    break
                with self.lock:
                    self.tail=(self.tail+piece)[-2048:];self.last_output=time.monotonic()
                    self.chunks.append((self.position, piece))
                    self.position += len(piece)
                    while self.chunks and self.position - self.chunks[0][0] > MAX_BUFFER_BYTES:
                        self.chunks.popleft()
                self.arrival.set()
        finally:
            self.done = True
            self.input_done.set()
            self.arrival.set()

    def read(self, offset: int | None, wait_ms: int = 100) -> dict[str, Any]:
        wait_ms = max(0, min(1000, int(wait_ms)))
        if wait_ms:
            self.arrival.wait(wait_ms / 1000)
            self.arrival.clear()
        with self.lock:
            start = self.chunks[0][0] if self.chunks else self.position
            cursor = self.read_cursor if offset is None else int(offset)
            stale = cursor < start
            cursor = max(cursor, start)
            output = bytearray()
            for chunk_start, chunk in self.chunks:
                chunk_end = chunk_start + len(chunk)
                if chunk_end <= cursor:
                    continue
                part = chunk[max(0, cursor - chunk_start):]
                available = MAX_TERMINAL_OUTPUT_BYTES - len(output)
                if available <= 0:
                    break
                output += part[:available]
                cursor += min(len(part), available)
            raw=bytes(output)
            decoder=codecs.getincrementaldecoder('utf-8')(errors='replace')
            decoded=decoder.decode(raw,final=self.done and cursor>=self.position)
            pending=decoder.getstate()[0]
            if pending:
                # Incomplete UTF-8 bytes remain available at the next offset.
                cursor-=len(pending);raw=raw[:-len(pending)]
            code = self.process.poll()
            self.read_cursor = cursor
        return {"session_id": self.id, "state": "RUNNING" if code is None or not self.done else ("PASS" if code == 0 else "ERROR"),
                "exit_code": code, "output": decoded,
                "next_offset": cursor, "earliest_offset": start, "output_truncated": stale,
                "has_more": cursor < self.position, "completion_reason":self.reason,
                "output_base64":__import__("base64").b64encode(raw).decode(),"terminal_mode":self.mode,"output_format":"vt_utf8" if self.mode=='conpty' else 'utf8'}

    def finish_io(self):
        self.input_done.set()
        try:self.process.stdin.close()
        except (OSError,ValueError):pass
        if self.done:
            try:self.process.stdout.close()
            except (OSError,ValueError):pass

    def prompt(self, expected=''):
        if not isinstance(expected,str) or len(expected)>256:raise ValueError('Literal prompt limit: 256 characters')
        with self.lock:
            text=self.tail.decode('utf-8',errors='replace').replace('\r','')
            line=text.rsplit('\n',1)[-1].strip()
            quiet=time.monotonic()-self.last_output>=.08
        match=(text.endswith(expected) if expected else bool(re.search(r'^(>>>|\.\.\.) ?$|^PS .{0,200}> ?$|^[A-Za-z]:\\.{0,200}> ?$|.{0,200}[$#] ?$',line)))
        return bool(quiet and match),line[-256:] if match else ''

    def _write_input(self):
        while not self.input_done.is_set():
            try:data=self.input_queue.get(timeout=.2)
            except queue.Empty:continue
            try:
                view=memoryview(data)
                while view:
                    count=self.process.stdin.write(view)
                    if not count:raise BrokenPipeError()
                    view=view[count:]
            except (OSError,ValueError):return

    def send(self, text: str) -> dict[str, Any]:
        if self.process.poll() is not None:raise RuntimeError('Terminal already finished')
        if not isinstance(text,str) or len(text.encode('utf-8'))>10000:raise ValueError('Input limit: 10,000 UTF-8 bytes')
        try:self.input_queue.put_nowait(text.encode('utf-8'))
        except queue.Full:raise RuntimeError('Terminal input queue full; inspect output before retrying')
        return dict(session_id=self.id,accepted_chars=len(text),input_queued=True)

    def stop(self, reason='stopped') -> dict[str, Any]:
        if self.process.poll() is None:self.reason=self.reason or reason
        self.input_done.set()
        self.process.close()
        try:self.process.stdin.close()
        except (OSError,ValueError):pass
        self.thread.join(timeout=1)
        if not self.thread.is_alive():
            try:self.process.stdout.close()
            except (OSError,ValueError):pass
        return dict(session_id=self.id,exit_code=self.process.poll(),state='STOPPED',completion_reason=self.reason)


class Runtime:
    def __init__(self, root: str, enable_terminal: bool = False):
        self.files = ScopedFiles(root)
        self.enable_terminal = enable_terminal
        self.sessions: dict[str, TerminalSession] = {}
        self.lock=threading.RLock();self.done=threading.Event();self.monitor_errors=0
        from udb_features import Features
        self.features=Features(self)
        self.journal=self.features.background.journal;self.last_checkpoint=0
        threading.Thread(target=self._watch,daemon=True,name='UDB terminal deadlines').start()

    def _watch(self):
        while not self.done.wait(.1):
            try:self.features.background.expire()
            except (OSError,RuntimeError,ValueError):self.monitor_errors+=1
            with self.lock:
                for session in list(self.sessions.values()):
                    try:
                        code=session.process.poll()
                        if code is not None:session.finish_io()
                        if code is None and time.monotonic()>session.deadline:session.stop('timed_out')
                    except (OSError,RuntimeError,ValueError):
                        self.monitor_errors+=1
                        try:session.stop('monitor_error')
                        except (OSError,RuntimeError,ValueError):pass
                if time.monotonic()-self.last_checkpoint>.5:
                    try:self.checkpoint()
                    except (OSError,ValueError):self.monitor_errors+=1
                    self.last_checkpoint=time.monotonic()

    def checkpoint(self):
        self.features.background.checkpoint()
        for session in list(self.sessions.values()):
            with session.lock:
                chunks=b''.join(c for _,c in session.chunks)[-65536:]
                row=dict(session_id=session.id,state='finished' if session.done else 'interrupted',exit_code=session.process.poll(),
                    output_base64=__import__('base64').b64encode(chunks).decode(),earliest_offset=session.position-len(chunks),next_offset=session.position,
                    terminal_mode=session.mode,process_restorable=False,replay_safe=False)
            self.journal.save('terminal-'+session.id,row)

    def close(self):
        self.done.set();self.shutdown()


    def start(self, program: str, arguments: list[str] | None = None, cwd: str = ".", wait_ms: int = 150, timeout_seconds: int = 3600, memory_mib: int = 1024, mode='pipe',columns=100,rows=30) -> dict[str, Any]:
        if not self.enable_terminal:
            raise PermissionError("Terminal disabled. Owner must restart with --enable-terminal")
        active = sum(session.process.poll() is None for session in self.sessions.values())
        if active >= MAX_SESSIONS:
            raise RuntimeError("Maximum 2 concurrent processes; stop one first")
        # Keep a bounded history. Evict only finished sessions; never detach
        # a still-running process or replay an earlier command.
        if len(self.sessions) >= MAX_RETAINED_SESSIONS:
            for session_id in list(self.sessions):
                if self.sessions[session_id].process.poll() is not None:
                    self.sessions[session_id].stop("retired")
                    del self.sessions[session_id]
                    if len(self.sessions) < MAX_RETAINED_SESSIONS:
                        break
        if len(self.sessions) >= MAX_RETAINED_SESSIONS:
            raise RuntimeError("Session history full; running processes were kept")
        location = self.files.path(cwd)
        if not location.is_dir():
            raise ValueError("cwd is not a directory")
        if not isinstance(program, str) or not program.strip() or not isinstance(arguments or [], list):
            raise ValueError("Invalid program/arguments")
        argv = [program] + (arguments or [])
        if any(not isinstance(arg, str) for arg in argv):
            raise ValueError("Arguments must be strings")
        if not isinstance(timeout_seconds,int) or not 1<=timeout_seconds<=3600 or not isinstance(memory_mib,int) or not 128<=memory_mib<=4096:
            raise ValueError('Terminal timeout 1..3600s, memory 128..4096 MiB')
        executable=shutil.which(program) if not Path(program).is_absolute() else program
        if not executable or not Path(executable).is_file():raise ValueError('Program was not found')
        argv[0]=str(Path(executable).resolve())
        if len(argv)>129 or any('\0' in a for a in argv) or sum(len(a) for a in argv)>24000:raise ValueError('Bounded program arguments required')
        if mode not in ('pipe','conpty'):raise ValueError('Terminal mode pipe or conpty')
        process=ManagedProcess(argv,str(location),memory_mib,conpty=mode=='conpty',columns=columns,rows=rows)
        session = TerminalSession(process, argv)
        session.mode=mode
        session.deadline=time.monotonic()+timeout_seconds
        self.sessions[session.id] = session
        self.checkpoint()
        return session.read(offset=0, wait_ms=wait_ms)

    def shutdown(self) -> None:
        """Stop all child processes owned by this Companion when MCP closes."""
        for session in list(self.sessions.values()):
            try:session.stop('shutdown')
            except (OSError, RuntimeError, subprocess.SubprocessError):pass
        self.features.close()
        self.checkpoint()

    def tool(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        from udb_features import NAMES
        if name in NAMES:return self.features.tool(name,args)
        if name == "terminal_start":
            r = self.start(args["program"], args.get("arguments", []), args.get("cwd", "."), args.get("wait_ms", 150),args.get('timeout_seconds',3600),args.get('memory_mib',1024),args.get('mode','pipe'),args.get('columns',100),args.get('rows',30))
            return respond_text(r["state"], "Lệnh đã bắt đầu" if r["state"] == "RUNNING" else "Lệnh đã hoàn tất",
                                f"Phiên: {r['session_id']} | Exit: {r['exit_code']} | Next offset: {r['next_offset']}\n{r['output'][:MAX_OUTPUT_CHARS]}")
        if name in {"terminal_read", "terminal_send", "terminal_stop"}:
            ident = args["session_id"]
            if ident not in self.sessions:
                raise ValueError("Unknown session; sessions are lost when server restarts")
            session = self.sessions[ident]
            if name == "terminal_send":
                r = session.send(args["text"])
                return respond_text("PASS", "Đã gửi dữ liệu vào phiên terminal", f"Phiên: {r['session_id']}")
            if name == "terminal_stop":
                r = session.stop()
                return respond_text("PASS", "Đã dừng phiên terminal", f"Phiên: {r['session_id']} | Exit: {r['exit_code']}")
            r = session.read(args.get("offset"), args.get("wait_ms", 100))
            headline = "Tiến trình còn chạy" if r["state"] == "RUNNING" else "Tiến trình hoàn tất" if r["state"] == "PASS" else "Tiến trình lỗi"
            note = "[Earlier output expired]\n" if r["output_truncated"] else ""
            return respond_text(r["state"], headline,
                                f"Phiên: {r['session_id']} | Exit: {r['exit_code']} | Next offset: {r['next_offset']} | More: {r['has_more']}\n" + note + r["output"][:MAX_OUTPUT_CHARS])
        if name == "file_list":
            r = self.files.list(args.get("path", "."), args.get("limit", 100))
            lines = [f"{entry['kind']:4} {entry['name']}" for entry in r["items"]]
            return respond_text("PASS", f"Đã liệt kê {len(lines)} mục trong {r['path']}", "\n".join(lines))
        if name == "file_read":
            r = self.files.read(args["path"], args.get("offset", 0), args.get("limit", 8192))
            return respond_text("PASS", f"Đã đọc {r['path']} ({r['size']} bytes)",
                                f"SHA256: {r['sha256']} | Next offset: {r['next_offset']} | Remaining: {r['remaining']}\n{r['content'][:MAX_OUTPUT_CHARS]}")
        if name == "file_write":
            r = self.files.write(args["path"], args["content"], args.get("expected_sha256", "NEW"))
            return respond_text("PASS", "Đã tạo file" if r["created"] else "Đã cập nhật file",
                                f"{r['path']} | Bytes: {r['bytes']} | SHA256: {r['sha256']}")
        if name == "file_replace":
            r = self.files.replace(args["path"], args["old"], args["new"], args["expected_sha256"], args.get("occurrences", 1))
            return respond_text("PASS", f"Đã sửa file ({r['replacements']} vị trí)", f"{r['path']} | SHA256: {r['sha256']}")
        if name == "file_search":
            r = self.files.search(args.get("path", "."), args.get("pattern", "*"), args.get("text", ""), args.get("limit", 30))
            return respond_text("PASS", f"Tìm thấy {len(r['matches'])} file", "\n".join(r["matches"]) + f"\nScanned: {r['scanned']} | Truncated: {r['truncated']}")
        raise ValueError("Unknown tool")


TOOLS: list[dict[str, Any]] = [
    {"name":"terminal_start", "description":"Start a process inside an approved project and return PASS/RUNNING/ERROR. No GUI.","inputSchema":{"type":"object","properties":{"program":{"type":"string"},"arguments":{"type":"array","items":{"type":"string"}},"cwd":{"type":"string"},"wait_ms":{"type":"integer"}},"required":["program"]}},
    {"name":"terminal_read", "description":"Read command output and exit state by session ID and offset.","inputSchema":{"type":"object","properties":{"session_id":{"type":"string"},"offset":{"type":"integer"},"wait_ms":{"type":"integer"}},"required":["session_id"]}},
    {"name":"terminal_send", "description":"Write text/REPL input to a managed terminal session.","inputSchema":{"type":"object","properties":{"session_id":{"type":"string"},"text":{"type":"string"}},"required":["session_id","text"]}},
    {"name":"terminal_stop", "description":"Stop only a process started by this companion.","inputSchema":{"type":"object","properties":{"session_id":{"type":"string"}},"required":["session_id"]}},
    {"name":"file_list", "description":"List readable project files.","inputSchema":{"type":"object","properties":{"path":{"type":"string"},"limit":{"type":"integer"}}}},
    {"name":"file_read", "description":"Read UTF-8 file with offset, limit and SHA256.","inputSchema":{"type":"object","properties":{"path":{"type":"string"},"offset":{"type":"integer"},"limit":{"type":"integer"}},"required":["path"]}},
    {"name":"file_write", "description":"Atomic write; NEW for create, SHA256 for replacing existing file.","inputSchema":{"type":"object","properties":{"path":{"type":"string"},"content":{"type":"string"},"expected_sha256":{"type":"string"}},"required":["path","content"]}},
    {"name":"file_replace", "description":"Atomic guarded exact replacement with recovery copy.","inputSchema":{"type":"object","properties":{"path":{"type":"string"},"old":{"type":"string"},"new":{"type":"string"},"expected_sha256":{"type":"string"},"occurrences":{"type":"integer"}},"required":["path","old","new","expected_sha256"]}},
    {"name":"file_search", "description":"Search files in project by glob and optional text.","inputSchema":{"type":"object","properties":{"path":{"type":"string"},"pattern":{"type":"string"},"text":{"type":"string"},"limit":{"type":"integer"}}}},
]


sys.path.insert(0, str(Path(__file__).resolve().parent))
from udb_extensions import EXTENSION_TOOLS, install as install_extensions
from udb_features import TOOLS as FEATURE_TOOLS
TOOLS.extend(EXTENSION_TOOLS)
TOOLS.extend(FEATURE_TOOLS)
TOOLS[0]["inputSchema"]["properties"].update(timeout_seconds={"type":"integer"},memory_mib={"type":"integer"})
TOOLS[0]['inputSchema']['properties'].update(mode={'type':'string','enum':['pipe','conpty']},columns={'type':'integer'},rows={'type':'integer'})
install_extensions(Runtime, ScopedFiles, respond_text)


def serve(rt: Runtime) -> None:
    if hasattr(sys.stdout,'reconfigure'):sys.stdout.reconfigure(encoding='utf-8',line_buffering=True)
    def send(row):
        sys.stdout.write(json.dumps(row,ensure_ascii=False)+'\n');sys.stdout.flush()
    while True:
        line=sys.stdin.buffer.readline(16*1024*1024+1)
        if not line:break
        ident=None
        try:
            if len(line)>16*1024*1024:
                while line and not line.endswith(b'\n'):line=sys.stdin.buffer.readline(65536)
                raise ValueError('Frame limit exceeded')
            message=json.loads(line)
            if not isinstance(message,dict):raise ValueError('JSON-RPC object required')
            if 'id' not in message:continue
            ident=message['id']
            if isinstance(ident,bool) or not isinstance(ident,(str,int,type(None))):raise ValueError('Invalid request ID')
            method=message.get('method');params=message.get('params') or {}
            if not isinstance(params,dict):raise ValueError('Object parameters required')
            if method=='initialize':
                value={'protocolVersion':'2024-11-05','capabilities':{'tools':{'listChanged':False}},'serverInfo':{'name':'UDB Companion v2','version':VERSION}}
            elif method=='ping':value={}
            elif method=='udb/close-sessions':rt.shutdown();value={}
            elif method=='udb/close-desktop':
                if hasattr(rt.features,'pointer'):rt.features.pointer.close();del rt.features.pointer
                value={}
            elif method=='tools/list':value={'tools':TOOLS}
            elif method=='tools/call':
                args=params.get('arguments') or {}
                if not isinstance(args,dict) or not isinstance(params.get('name'),str):raise ValueError('Tool name/object arguments required')
                try:value=rt.tool(params['name'],args)
                except (OSError,ValueError,KeyError,RuntimeError,UnicodeError,TypeError,AttributeError,OverflowError,subprocess.SubprocessError) as exc:
                    value=respond_text('ERROR',type(exc).__name__+': '+str(exc))
            else:
                send(dict(jsonrpc='2.0',id=ident,error=dict(code=-32601,message='Method not found')));continue
            send(dict(jsonrpc='2.0',id=ident,result=value))
        except (json.JSONDecodeError,UnicodeDecodeError) as exc:
            send(dict(jsonrpc='2.0',id=None,error=dict(code=-32700,message='Invalid JSON-RPC JSON')))
        except (ValueError,TypeError,KeyError,UnicodeError) as exc:
            send(dict(jsonrpc='2.0',id=ident,error=dict(code=-32602,message=str(exc))))


def selftest(root: str) -> int:
    rt = Runtime(root, enable_terminal=True)
    filename = f".udb-companion-test-{uuid.uuid4().hex[:8]}.txt"
    try:
        create = rt.files.write(filename, "line one\n", "NEW")
        assert create["created"]
        found = rt.files.search(".", filename)
        assert filename in found["matches"]
        viewed = rt.files.read(filename)
        assert viewed["content"] == "line one\n"
        edited = rt.files.replace(filename, "one", "two", viewed["sha256"])
        assert rt.files.read(filename)["content"] == "line two\n"
        print("PASS | File create/search/read/edit",flush=True)
        # A managed terminal with normal command completion.
        task = rt.start(sys.executable, ["-u", "-c", "print('hello-UDB')"], ".", 300)
        ident = task["session_id"]
        cursor = task["next_offset"]
        chunks = [task["output"]]
        for _ in range(20):
            read = rt.sessions[ident].read(cursor, 200)
            chunks.append(read["output"])
            cursor = read["next_offset"]
            if read["state"] != "RUNNING" and not read["has_more"]:
                break
        assert "hello-UDB" in "".join(chunks)
        assert read["state"] == "PASS"
        print("PASS | Terminal execute/read/completion",flush=True)
        # Interactive REPL sends data to one process without launching a new one.
        task = rt.start(sys.executable, ["-u", "-i"], ".", 150)
        ident = task["session_id"]
        rt.sessions[ident].send("print('interactive-ok')\n")
        cursor = task["next_offset"]
        data = task["output"]
        for _ in range(20):
            row = rt.sessions[ident].read(cursor, 150)
            data += row["output"]
            cursor = row["next_offset"]
            if "interactive-ok" in data:
                break
        assert "interactive-ok" in data
        rt.sessions[ident].stop()
        print("PASS | Terminal interactive input/stop",flush=True)
        try:
            rt.files.read("../outside.txt")
            raise AssertionError("Escaped root")
        except PermissionError:
            print("PASS | Root scope guard",flush=True)
        print("PASS | Self-test complete",flush=True)
        return 0
    except Exception as e:
        print(f"ERROR | Self-test: {type(e).__name__}: {e}",flush=True)
        return 1
    finally:
        p = rt.files.path(filename, allow_missing=True)
        p.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="Independent text-first MCP companion for UDB")
    parser.add_argument("--root", required=True, help="Existing approved project folder")
    parser.add_argument("--selftest", action="store_true")
    parser.add_argument("--parent-pid", type=int, default=0)
    parser.add_argument("--enable-terminal", action="store_true", help="Owner opt-in for subprocess execution")
    args = parser.parse_args()
    if args.selftest:
        return selftest(args.root)
    runtime = Runtime(args.root, enable_terminal=args.enable_terminal)
    def parent_gone():
        try:runtime.close()
        finally:os._exit(0)
    watch_parent(args.parent_pid,parent_gone)
    try:
        serve(runtime)
    finally:
        runtime.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

