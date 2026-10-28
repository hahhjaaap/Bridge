"""Policy-enforced Windows agent. No host shell execution is exposed."""

from __future__ import annotations

import ast
import difflib
import json
import os
import re
import sqlite3
import stat
import threading
import time
import uuid
from pathlib import Path, PureWindowsPath

from .core_autonomy import AutonomyMixin
from .core_common import PolicyError, canonical, digest
from .path_policy import is_data_container

MAX_TEXT = 512 * 1024
SOURCE_EXT = {
    ".py",
    ".ps1",
    ".json",
    ".toml",
    ".ini",
    ".cfg",
    ".md",
    ".txt",
    ".yaml",
    ".yml",
    ".js",
    ".ts",
    ".tsx",
    ".jsx",
    ".html",
    ".css",
    ".c",
    ".cpp",
    ".h",
    ".cs",
    ".sql",
    ".xml",
    ".gml",
    ".gitignore",
}
BLOCK_DIRS = {
    "checkpoint",
    "checkpoints",
    "data",
    "dataset",
    "datasets",
    "training_data",
    "training-data",
    "weights",
    "models",
    "backups",
    ".git",
    ".venv",
    "venv",
    "node_modules",
    "__pycache__",
    "state",
    ".ssh",
    ".aws",
    ".azure",
    ".config",
}
BLOCK_EXT = {
    ".pt",
    ".pth",
    ".ckpt",
    ".safetensors",
    ".bin",
    ".gguf",
    ".onnx",
    ".parquet",
    ".arrow",
    ".jsonl",
    ".csv",
    ".tsv",
    ".npy",
    ".npz",
    ".zip",
    ".7z",
    ".tar",
    ".gz",
    ".db",
    ".sqlite",
    ".pem",
    ".key",
    ".pfx",
}
RESERVED = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    "CLOCK$",
    *(f"COM{i}" for i in range(10)),
    *(f"LPT{i}" for i in range(10)),
}


class ScopedConnection(sqlite3.Connection):
    """Commit/rollback normally, then release the Windows database handle."""

    def __exit__(self, exc_type, exc_value, traceback):
        try:
            return super().__exit__(exc_type, exc_value, traceback)
        finally:
            self.close()


def is_under(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def reject_links(path: Path):
    """Reject every symlink/junction/reparse point, including ancestors."""
    for part in [path, *path.parents]:
        if not part.exists() and not part.is_symlink():
            continue
        s = part.lstat()
        if stat.S_ISLNK(s.st_mode) or getattr(s, "st_file_attributes", 0) & 0x400:
            raise PolicyError("Symlink/junction/reparse points are not allowed")
    if path.is_file() and path.stat().st_nlink != 1:
        raise PolicyError("Hardlinked files are not allowed")


_DIFF_PREVIEW_LIMIT = 16000
_DIFF_MAX_LINES = 2048
_DIFF_MAX_BYTES = 128 * 1024


def diff_preview(before: bytes | None, text: str, relative: str) -> str:
    """Bound approval preview CPU, memory and SQLite payload size.

    A large or truncated preview is explicitly marked as incomplete so
    owner approval never silently presents a partial diff as the full change.
    The proposal still stores the full new text (bounded by MAX_TEXT).
    """
    old = (before or b"").decode("utf-8-sig")
    if (
        len(before or b"") > _DIFF_MAX_BYTES
        or len(text.encode("utf-8")) > _DIFF_MAX_BYTES
        or old.count("\n") > _DIFF_MAX_LINES
        or text.count("\n") > _DIFF_MAX_LINES
    ):
        return (
            "[UDB] Diff preview omitted: file exceeds safe preview limits. "
            "Review the complete old and new file before approving.\n"
        )
    chunks = difflib.unified_diff(
        old.splitlines(True),
        text.splitlines(True),
        fromfile=relative,
        tofile=relative,
    )
    parts = []
    size = 0
    notice = (
        "\n[UDB] Diff preview truncated; review the complete change before approving.\n"
    )
    for chunk in chunks:
        if size + len(chunk) > _DIFF_PREVIEW_LIMIT - len(notice):
            parts.append(notice)
            break
        parts.append(chunk)
        size += len(chunk)
    return "".join(parts)


class Agent(AutonomyMixin):
    def __init__(self, home: Path):
        self.home = home.absolute()
        self.state = self.home / "state"
        reject_links(self.home)
        reject_links(self.state)
        self.state.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.db = self.state / "agent.sqlite3"
        with self.connect() as c:
            c.executescript("""
            CREATE TABLE IF NOT EXISTS projects(id TEXT PRIMARY KEY, root TEXT UNIQUE, writable INTEGER);
            CREATE TABLE IF NOT EXISTS protected(root TEXT PRIMARY KEY);
            CREATE TABLE IF NOT EXISTS requests(id TEXT PRIMARY KEY, kind TEXT, payload TEXT,
              hash TEXT, created REAL, status TEXT, result TEXT);
            CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY, status TEXT, info TEXT);
            CREATE TABLE IF NOT EXISTS sandbox_grants(project TEXT PRIMARY KEY, root TEXT, token TEXT, created REAL);
            CREATE TABLE IF NOT EXISTS source_grants(project TEXT PRIMARY KEY, root TEXT, token TEXT, created REAL);
            CREATE TABLE IF NOT EXISTS media_grants(scope TEXT PRIMARY KEY, token TEXT, created REAL);
            CREATE TABLE IF NOT EXISTS events(seq INTEGER PRIMARY KEY AUTOINCREMENT,
              at REAL, event TEXT, details TEXT);
            CREATE INDEX IF NOT EXISTS idx_requests_status_created
              ON requests(status, created);
            CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
            """)
            # Protected roots are owner policy, never inferred from project names.
            # Existing records are retained; initialization does not issue grants.

    def connect(self):
        reject_links(self.db)
        c = sqlite3.connect(self.db, timeout=10, factory=ScopedConnection)
        c.row_factory = sqlite3.Row
        return c

    def audit(self, event, details):
        # Never store source text, tokens, or environment variables in audit.
        with self.connect() as c:
            c.execute(
                "INSERT INTO events(at,event,details) VALUES (?,?,?)",
                (time.time(), event, canonical(details)),
            )

    def protect_local(self, root: str):
        p = Path(root).absolute().resolve()
        if not p.is_dir():
            raise PolicyError("Protected folder must exist")
        with self.connect() as c:
            c.execute("INSERT OR IGNORE INTO protected VALUES (?)", (str(p),))
        self.audit("protect", {"root": str(p)})

    def projects(self):
        with self.connect() as c:
            return [dict(r) for r in c.execute("SELECT * FROM projects ORDER BY id")]

    def project(self, project: str):
        with self.connect() as c:
            r = c.execute("SELECT * FROM projects WHERE id=?", (project,)).fetchone()
        if not r:
            raise PolicyError("Unknown project; register it locally in the EXE manager")
        p = Path(r["root"])
        reject_links(p)
        if not p.is_dir() or p.resolve() != p:
            raise PolicyError("Project root changed")
        self.check_protected(p)
        return r, p

    def check_protected(self, p: Path):
        # Internal agent files must never be exposed as a registered project.
        if is_under(p.resolve(), self.state.resolve()):
            raise PolicyError("Agent state is private")
        if is_under(p.resolve(), self.home.resolve()) and not is_under(
            p.resolve(), (self.home / "projects").resolve()
        ):
            raise PolicyError(
                "Bridge installation is private; use the projects subfolder"
            )
        with self.connect() as c:
            roots = [Path(r[0]) for r in c.execute("SELECT root FROM protected")]
        protected = [q for q in roots if is_under(p.resolve(), q)]
        if protected:
            from .source_access import allows

            if not allows(self, p, protected):
                raise PolicyError(
                    "Protected root: access denied. Source-only access requires "
                    "local confirmation with udb allow-source --path PROJECT."
                )

    def register_local(self, project: str, root: str, writable=False):
        if not re.fullmatch(r"[a-zA-Z0-9_-]{1,48}", project):
            raise PolicyError("Project ID: 1–48 letters, digits, hyphen or underscore")
        p = Path(root).absolute()
        reject_links(p)
        p = p.resolve()
        if not p.is_dir() or p == p.parent:
            raise PolicyError("Choose a project folder, never a drive root")
        # Account home, installation and their ancestors are too broad.
        for private in (self.home.resolve(), Path.home().resolve()):
            if is_under(private, p):
                raise PolicyError("Root too broad or contains bridge installation")
        self.check_protected(p)
        if p.name.lower() in BLOCK_DIRS or is_data_container(p.name):
            raise PolicyError("Data/backup folders cannot be projects")
        with self.connect() as c:
            c.execute(
                "INSERT INTO projects VALUES (?,?,?) ON CONFLICT(id) DO UPDATE SET root=excluded.root,writable=excluded.writable",
                (project, str(p), int(writable)),
            )
        self.audit(
            "register", {"project": project, "root": str(p), "writable": bool(writable)}
        )

    def path(self, project: str, relative: str = ".", write=False):
        r, root = self.project(project)
        if write and not r["writable"]:
            raise PolicyError("Project is read-only")
        if not isinstance(relative, str) or len(relative) > 240 or "\x00" in relative:
            raise PolicyError("Invalid relative path")
        w = PureWindowsPath(relative)
        if (
            w.is_absolute()
            or w.drive
            or w.root
            or ":" in relative
            or relative.startswith(("/", "\\"))
        ):
            raise PolicyError("Only project-relative paths are allowed")
        parts = relative.replace("\\", "/").split("/")
        for part in parts:
            if part in ("", "."):
                continue
            if (
                part == ".."
                or part.endswith((" ", "."))
                or any(ch in part for ch in '<>"|?*')
            ):
                raise PolicyError("Unsafe path segment")
            lower = part.lower()
            if (
                part.split(".")[0].upper() in RESERVED
                or lower in BLOCK_DIRS
                or is_data_container(part)
                or "checkpoint" in lower
                or "training" in lower
                or lower.startswith((".env", "credentials", "secrets", "id_rsa"))
                or Path(part).suffix.lower() in BLOCK_EXT
            ):
                raise PolicyError("Protected data, credential, or reserved path")
        p = root.joinpath(*(q for q in parts if q not in ("", ".")))
        reject_links(p)
        if not is_under(p.resolve(), root):
            raise PolicyError("Path escaped project")
        self.check_protected(p)
        return p

    def read_bytes(self, project, relative):
        p = self.path(project, relative)
        if (
            not p.is_file()
            or p.suffix.lower() not in SOURCE_EXT
            or p.stat().st_size > MAX_TEXT
        ):
            raise PolicyError("Only small source/text files may be read")
        data = p.read_bytes()
        if len(data) > MAX_TEXT or b"\0" in data:
            raise PolicyError("Binary/oversized file blocked")
        data.decode("utf-8-sig")
        return data

    def read(self, project, relative):
        data = self.read_bytes(project, relative)
        sha = digest(data)
        self.audit("read", {"project": project, "path": relative, "sha256": sha})
        return {
            "path": relative,
            "sha256": sha,
            "text": data.decode("utf-8-sig"),
        }

    def listing(self, project, relative=".", offset=0, limit=100):
        p = self.path(project, relative)
        offset = max(0, min(int(offset), 100000))
        limit = max(1, min(int(limit), 200))
        _, root = self.project(project)
        # Bound traversal even for unusually large directories.
        visible = []
        for i, f in enumerate(p.iterdir()):
            if i >= 10000:
                break
            # Exclude unsupported files before the expensive per-path policy
            # check; all visible entries still pass the full policy.
            if f.is_file() and f.suffix.lower() not in SOURCE_EXT:
                continue
            rel = f.relative_to(root).as_posix()
            try:
                self.path(project, rel)
                visible.append({"path": rel, "directory": f.is_dir()})
            except PolicyError:
                continue
        visible.sort(key=lambda x: x["path"].lower())
        return {
            "items": visible[offset : offset + limit],
            "next_offset": offset + limit if len(visible) > offset + limit else None,
            "protected_items_hidden": True,
            "scan_cap": 10000,
        }

    def propose(self, kind, payload):
        data = canonical(payload)
        rid = uuid.uuid4().hex
        h = digest(data.encode())
        automatic = (
            kind in ("desktop", "job")
            and self.sandbox_authorization(payload["project"], payload["root"])
        ) or (kind == "media" and self.media_autonomy())
        initial = (
            ("queued_sandbox" if kind in ("desktop", "job") else "queued_media")
            if automatic
            else "pending"
        )
        with self.connect() as c:
            if (
                c.execute(
                    "SELECT count(*) FROM requests WHERE status IN ('pending','queued_sandbox','queued_media') AND created>?",
                    (time.time() - 600,),
                ).fetchone()[0]
                >= 30
            ):
                raise PolicyError("Too many pending approvals")
            c.execute(
                "INSERT INTO requests VALUES (?,?,?,?,?,?,?)",
                (
                    rid,
                    kind,
                    data,
                    h,
                    time.time(),
                    initial,
                    canonical({"authorization": automatic}) if automatic else "{}",
                ),
            )
        self.audit("propose", {"id": rid, "kind": kind, "sha256": h})
        return {
            "request_id": rid,
            "sha256": h,
            "status": initial if automatic else "pending_local_approval",
            "expires_in_seconds": 600,
            "next": (
                "Existing owner grant: keep Bridge open and poll get_request_status; no per-action approval."
                if automatic
                else "Enable the corresponding autonomy switch in Bridge, or review this exceptional request locally. AI cannot create grants."
            ),
        }

    def propose_write(self, project, relative, text, expected_sha256):
        p = self.path(project, relative, write=True)
        if p == self.project(project)[1] or p.suffix.lower() not in SOURCE_EXT:
            raise PolicyError("Write only source/text file extensions")
        if not p.parent.is_dir():
            raise PolicyError("Parent directory must exist")
        if len(text.encode()) > MAX_TEXT or "\0" in text:
            raise PolicyError("Text too large or binary")
        before = self.read_bytes(project, relative) if p.exists() else None
        actual = digest(before) if before is not None else "NEW"
        if expected_sha256 != actual:
            raise PolicyError(
                "Stale file: read current SHA256; use NEW only for new files"
            )
        diff = diff_preview(before, text, relative)
        payload = {
            "project": project,
            "path": relative,
            "text": text,
            "expected": actual,
            "root": str(self.project(project)[1]),
            "diff": diff,
        }
        if self.source_autonomy_authorization(project, payload["root"]):
            return self.auto_apply_write(payload)
        response = self.propose("write", payload)
        response["diff"] = diff
        return response

    def snapshot(self, project):
        _, root = self.project(project)
        manifest = {}
        total = 0
        visited = 0
        for parent, dirs, files in os.walk(root, followlinks=False):
            visited += 1
            if visited > 2000:
                raise PolicyError("Source tree too large; register a narrower folder")
            safe_dirs = []
            for d in dirs:
                try:
                    self.path(project, (Path(parent) / d).relative_to(root).as_posix())
                    safe_dirs.append(d)
                except PolicyError:
                    pass
            dirs[:] = safe_dirs
            for name in files:
                if Path(name).suffix.lower() not in SOURCE_EXT:
                    continue
                rel = (Path(parent) / name).relative_to(root).as_posix()
                try:
                    data = self.read_bytes(project, rel)
                except (PolicyError, UnicodeError):
                    continue
                total += len(data)
                if total > 16 * 1024 * 1024 or len(manifest) >= 2000:
                    raise PolicyError("Source snapshot limit: 16 MiB / 2000 files")
                manifest[rel] = digest(data)
        return manifest

    def propose_job(self, project, language, script, timeout=120):
        from .sandbox import available

        if not available():
            raise PolicyError(
                "Windows Sandbox unavailable. Host execution is disabled; see Enable-Sandbox.ps1"
            )
        if language not in ("python", "powershell") or not 1 <= timeout <= 600:
            raise PolicyError("Invalid language or timeout (1–600 seconds)")
        if len(script.encode()) > 64000 or "\0" in script:
            raise PolicyError("Script too large or invalid")
        # Running tests also executes arbitrary code, so uses identical isolation.
        return self.propose(
            "job",
            {
                "project": project,
                "root": str(self.project(project)[1]),
                "language": language,
                "script": script,
                "timeout": timeout,
                "manifest": self.snapshot(project),
            },
        )

    def propose_desktop(self, project, task, duration_seconds=600):
        from .desktop_vm import propose

        return propose(self, project, task, duration_seconds)

    def pending_local(self):
        with self.connect() as c:
            c.execute(
                "UPDATE requests SET status='expired' WHERE status IN ('pending','queued_sandbox','queued_media') AND created<?",
                (time.time() - 600,),
            )
            return [
                dict(r)
                for r in c.execute(
                    "SELECT * FROM requests WHERE status='pending' ORDER BY created"
                )
            ]

    def request_status(self, rid):
        with self.connect() as c:
            row = c.execute(
                "SELECT id,kind,hash,created,status,result FROM requests WHERE id=?",
                (rid,),
            ).fetchone()
        if not row:
            raise PolicyError("Unknown request ID")
        r = dict(row)
        r["result"] = json.loads(r["result"])
        return r

    def decide_local(self, rid, approve: bool):
        """Intentionally not exported as an MCP tool or HTTP endpoint."""
        with self.lock, self.connect() as c:
            row = c.execute(
                "SELECT * FROM requests WHERE id=? AND status='pending'", (rid,)
            ).fetchone()
            if not row or row["created"] < time.time() - 600:
                raise PolicyError("Request expired or already consumed")
            if not approve:
                c.execute("UPDATE requests SET status='rejected' WHERE id=?", (rid,))
                return
            p = json.loads(row["payload"])
            if digest(canonical(p).encode()) != row["hash"]:
                raise PolicyError("Approval integrity check failed")
            claimed = c.execute(
                "UPDATE requests SET status='executing' WHERE id=? AND status='pending'",
                (rid,),
            )
            if claimed.rowcount != 1:
                raise PolicyError("Request already consumed by another local manager")
        try:
            if (
                row["kind"] != "media"
                and str(self.project(p["project"])[1]) != p["root"]
            ):
                raise PolicyError("Project root changed since proposal")
            if row["kind"] == "write":
                result = self.apply_write(rid, p)
            elif row["kind"] == "job":
                result = self.start_job(rid, p)
            elif row["kind"] == "desktop":
                from .desktop_vm import launch

                result = launch(self, rid, p)
            elif row["kind"] == "media":
                from .media import run_download

                result = run_download(self, rid, p)
            else:
                raise PolicyError("Unknown request kind")
            status = "applied" if row["kind"] in ("write", "media") else "started"
        except Exception as e:
            result, status = {"error": str(e)}, "failed"
        with self.connect() as c:
            c.execute(
                "UPDATE requests SET status=?,result=? WHERE id=?",
                (status, canonical(result), rid),
            )
        self.audit("decision", {"id": rid, "status": status})
        return result

    def apply_write(self, rid, payload):
        project, rel = payload["project"], payload["path"]
        p = self.path(project, rel, write=True)
        old = self.read_bytes(project, rel) if p.exists() else None
        if (digest(old) if old is not None else "NEW") != payload["expected"]:
            raise PolicyError(
                "File changed after approval was requested; submit a new proposal"
            )
        backup = None
        if old is not None:
            folder = self.state / "backups"
            folder.mkdir(exist_ok=True)
            backup = folder / (rid + ".bak")
            with backup.open("xb") as f:
                f.write(old)
        # Exclusive temporary file + atomic rename. Never follow an existing link.
        temp = p.with_name(".udb-" + rid + ".tmp")
        try:
            with temp.open("xb") as f:
                f.write(payload["text"].encode("utf-8"))
                f.flush()
                os.fsync(f.fileno())
            self.path(project, rel, write=True)
            current = p.read_bytes() if p.exists() else None
            if (digest(current) if current is not None else "NEW") != payload[
                "expected"
            ]:
                raise PolicyError("Concurrent modification detected")
            os.replace(temp, p)
        finally:
            temp.unlink(missing_ok=True)
        return {
            "path": rel,
            "sha256": digest(payload["text"].encode()),
            "backup_id": rid if backup else None,
        }

    def start_job(self, rid, payload):
        from .sandbox import launch

        return launch(self, rid, payload)

    def syntax_check(self, project, relative):
        if not relative.lower().endswith(".py"):
            raise PolicyError("Python source only")
        data = self.read_bytes(project, relative)
        try:
            ast.parse(data.decode("utf-8-sig"), filename=relative)
            return {"valid": True, "executes_code": False}
        except SyntaxError as e:
            return {
                "valid": False,
                "line": e.lineno,
                "column": e.offset,
                "message": e.msg,
                "executes_code": False,
            }

    def job_status(self, jid, offset=0, limit=8000):
        if not re.fullmatch("[a-f0-9]{32}", jid):
            raise PolicyError("Invalid job ID")
        with self.connect() as c:
            row = c.execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone()
        if not row:
            raise PolicyError("Unknown job")
        r = dict(row)
        r["info"] = json.loads(r["info"])
        folder = self.state / "jobs" / jid / "output"
        log = folder / "output.log"
        reject_links(log)
        offset, limit = max(0, int(offset)), max(1, min(int(limit), 32000))
        if log.exists():
            with log.open("rb") as f:
                f.seek(offset)
                data = f.read(limit)
            r.update(
                output=data.decode("utf-8", errors="replace"),
                next_offset=offset + len(data),
            )
        return r

    def diagnostics(self):
        from . import __version__
        from .host_client import status as host_status
        from .sandbox import available

        hs = host_status(self)
        from .computer_access import summary as computer_summary
        from .headless import manager_status

        computer = computer_summary(self)
        return {
            "name": "Universal Desktop Bridge",
            "version": __version__,
            "manager": manager_status(self.state),
            "computer_access": computer,
            "host_background": {
                "enabled": hs["enabled"],
                "worker_ready": hs["worker_ready"],
                "scope": "owned browser and native text workspace only",
                "user_mouse_keyboard": False,
            },
            "resources": hs["resources"],
            "platform": os.name,
            "sandbox_available": available(),
            "host_execution": computer["enabled"],
            "auto_write_projects": self.source_autonomy_projects(),
            "media_auto": self.media_autonomy(),
            "media_download": "owner-approved, HTTPS allowlist, FFmpeg only",
            "approvals": "Existing project/Sandbox grants retained. Optional computer access requires attended local owner opt-in; no MCP setter.",
            "sandbox_autonomy_projects": self.sandbox_autonomy_projects(),
            "protected_data": "Scoped APIs enforce protected paths; optional computer commands are current-user and unsandboxed",
            "projects": len(self.projects()),
            "rdc_dependency": False,
        }
