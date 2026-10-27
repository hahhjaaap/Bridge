"""Owner-controlled source, media and Windows Sandbox grants and queues.

These methods are mixed into Agent, not exposed as public MCP setters.
All grants remain stored in Agent's local SQLite database. Queue dispatch
revalidates authorization and the submitted payload before execution.
"""

from __future__ import annotations

import json
import threading
import time
import uuid

from .core_common import PolicyError, canonical, digest


class AutonomyMixin:
    # Owner-controlled persistent grants. No MCP tool exports the setters.
    # Changes to dependency manifests, launch scripts and CI remain manual.
    AUTO_PROTECTED_NAMES = frozenset(
        (
            "setup.py",
            "pyproject.toml",
            "package.json",
            "package-lock.json",
            "pnpm-lock.yaml",
            "yarn.lock",
            "requirements.txt",
            "pipfile",
            "dockerfile",
            "compose.yaml",
            "docker-compose.yml",
            "install.ps1",
            "uninstall.ps1",
            "start.ps1",
            "run.ps1",
            "build.ps1",
            "postinstall.js",
            "vite.config.js",
            "webpack.config.js",
            "tox.ini",
            ".gitignore",
            "web.config",
            "settings.json",
        )
    )

    def source_autonomy_authorization(self, project, root, token=None):
        with self.connect() as c:
            row = c.execute(
                "SELECT * FROM source_grants WHERE project=? AND root=?",
                (project, root),
            ).fetchone()
        if not row or (token is not None and row["token"] != token):
            return None
        try:
            record, actual = self.project(project)
            if str(actual) != root or not record["writable"]:
                return None
        except PolicyError:
            return None
        return row["token"]

    def source_autonomy_projects(self):
        with self.connect() as c:
            rows = [
                dict(r)
                for r in c.execute("SELECT * FROM source_grants ORDER BY project")
            ]
        return [
            r["project"]
            for r in rows
            if self.source_autonomy_authorization(r["project"], r["root"], r["token"])
        ]

    def set_source_autonomy_local(self, project, enabled):
        record, root = self.project(project)
        if enabled and not record["writable"]:
            raise PolicyError("Enable project source writes locally before autonomy")
        with self.lock, self.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            c.execute("DELETE FROM source_grants WHERE project=?", (project,))
            if enabled:
                c.execute(
                    "INSERT INTO source_grants VALUES (?,?,?,?)",
                    (project, str(root), uuid.uuid4().hex, time.time()),
                )
        self.audit(
            "source_autonomy",
            {
                "project": project,
                "enabled": bool(enabled),
                "scope": "registered project source files only",
            },
        )

    def auto_apply_write(self, payload):
        project, root, relative = payload["project"], payload["root"], payload["path"]
        parts = [x.lower() for x in relative.replace("\\", "/").split("/")]
        if any(x in self.AUTO_PROTECTED_NAMES for x in parts) or any(
            x.startswith((".github", ".vscode", ".idea")) for x in parts
        ):
            raise PolicyError(
                "Important configuration or build file: automatic modification blocked"
            )
        with self.lock:
            if not self.source_autonomy_authorization(project, root):
                raise PolicyError(
                    "Owner auto-write grant revoked or project root changed"
                )
            rid = uuid.uuid4().hex
            h = digest(canonical(payload).encode())
            with self.connect() as c:
                c.execute(
                    "INSERT INTO requests VALUES (?,?,?,?,?,?,?)",
                    (
                        rid,
                        "write",
                        canonical(payload),
                        h,
                        time.time(),
                        "executing",
                        "{}",
                    ),
                )
            try:
                result = self.apply_write(rid, payload)
                state = "applied"
            except Exception as e:
                result, state = {"error": str(e)}, "failed"
            with self.connect() as c:
                c.execute(
                    "UPDATE requests SET status=?,result=? WHERE id=?",
                    (state, canonical(result), rid),
                )
            self.audit(
                "auto_write",
                {"id": rid, "project": project, "path": relative, "status": state},
            )
            if state == "failed":
                raise PolicyError(result["error"])
            return {
                "request_id": rid,
                "status": "applied_auto",
                "sha256": h,
                "file_sha256": result["sha256"],
                "backup_id": result["backup_id"],
                "scope": "owner_granted_project_only",
            }

    def media_autonomy(self):
        with self.connect() as c:
            r = c.execute(
                "SELECT token FROM media_grants WHERE scope='approved_https_hls'"
            ).fetchone()
        return bool(r)

    def set_media_autonomy_local(self, enabled):
        with self.lock, self.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            c.execute("DELETE FROM media_grants WHERE scope='approved_https_hls'")
            if enabled:
                c.execute(
                    "INSERT INTO media_grants VALUES (?,?,?)",
                    ("approved_https_hls", uuid.uuid4().hex, time.time()),
                )
        self.audit(
            "media_autonomy",
            {
                "enabled": bool(enabled),
                "scope": "allowlisted HTTPS HLS to Downloads, no overwrite",
            },
        )

    def pump_media_queue_local(self):
        # This function is only called by the Windows-owned DesktopController.
        # Downloads run asynchronously; no script or arbitrary command is accepted.
        t = getattr(self, "_media_thread", None)
        if t is not None and t.is_alive():
            return
        with self.lock, self.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            c.execute(
                "UPDATE requests SET status='expired' WHERE status='queued_media' AND created<?",
                (time.time() - 600,),
            )
            row = c.execute(
                "SELECT * FROM requests WHERE status='queued_media' ORDER BY created LIMIT 1"
            ).fetchone()
            if row is None:
                return
            if (
                not self.media_autonomy()
                or row["kind"] != "media"
                or digest(row["payload"].encode()) != row["hash"]
            ):
                c.execute(
                    "UPDATE requests SET status='rejected',result=? WHERE id=?",
                    (
                        canonical(
                            {
                                "error": "Media grant missing, revoked or request modified"
                            }
                        ),
                        row["id"],
                    ),
                )
                return
            c.execute(
                "UPDATE requests SET status='executing' WHERE id=? AND status='queued_media'",
                (row["id"],),
            )
            rid = row["id"]
            payload = json.loads(row["payload"])

        def worker():
            try:
                if not self.media_autonomy():
                    raise PolicyError("Media autonomy revoked")
                from .media import run_download

                result = run_download(self, rid, payload)
                state = "applied"
            except Exception as e:
                result, state = {"error": str(e)}, "failed"
            with self.connect() as c:
                c.execute(
                    "UPDATE requests SET status=?,result=? WHERE id=?",
                    (state, canonical(result), rid),
                )
            self.audit("auto_media", {"id": rid, "status": state})

        self._media_thread = threading.Thread(
            target=worker, name="UDBAutoMedia", daemon=True
        )
        self._media_thread.start()

    def sandbox_authorization(self, project, root, token=None):
        """Read-only scope check. No MCP tool can create or widen this grant."""
        with self.connect() as c:
            row = c.execute(
                "SELECT * FROM sandbox_grants WHERE project=? AND root=?",
                (project, root),
            ).fetchone()
        if not row or (token is not None and row["token"] != token):
            return None
        try:
            if str(self.project(project)[1]) != root:
                return None
        except PolicyError:
            return None
        return row["token"]

    def sandbox_autonomy_projects(self):
        with self.connect() as c:
            rows = [
                dict(r)
                for r in c.execute("SELECT * FROM sandbox_grants ORDER BY project")
            ]
        return [
            r["project"]
            for r in rows
            if self.sandbox_authorization(r["project"], r["root"], r["token"])
        ]

    def set_sandbox_autonomy_local(self, project, enabled):
        """Owner-only local setting, never exported by MCP. Does not grant host writes."""
        _, root = self.project(project)
        with self.lock, self.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            c.execute("DELETE FROM sandbox_grants WHERE project=?", (project,))
            if enabled:
                token = uuid.uuid4().hex
                c.execute(
                    "INSERT INTO sandbox_grants VALUES (?,?,?,?)",
                    (project, str(root), token, time.time()),
                )
                # Existing unexpired guest proposals share the same owner-granted scope.
                for row in c.execute(
                    "SELECT * FROM requests WHERE kind IN ('desktop','job') AND status='pending' AND created>?",
                    (time.time() - 600,),
                ).fetchall():
                    p = json.loads(row["payload"])
                    if p.get("project") == project and p.get("root") == str(root):
                        c.execute(
                            "UPDATE requests SET status='queued_sandbox',result=? WHERE id=?",
                            (canonical({"authorization": token}), row["id"]),
                        )
            else:
                for row in c.execute(
                    "SELECT * FROM requests WHERE status='queued_sandbox'"
                ).fetchall():
                    if json.loads(row["payload"]).get("project") == project:
                        c.execute(
                            "UPDATE requests SET status='rejected',result=? WHERE id=?",
                            (
                                canonical(
                                    {"error": "Sandbox autonomy revoked locally"}
                                ),
                                row["id"],
                            ),
                        )
                for row in c.execute(
                    "SELECT * FROM jobs WHERE status IN ('running','reviewing')"
                ).fetchall():
                    info = json.loads(row["info"])
                    if info.get("project") == project and info.get("authorization"):
                        c.execute(
                            "UPDATE jobs SET status='stopping' WHERE id=?", (row["id"],)
                        )
        self.audit(
            "sandbox_autonomy",
            {"project": project, "enabled": bool(enabled), "scope": "guest only"},
        )

    def pump_sandbox_queue_local(self):
        """GUI-owned executor for previously granted scopes; never calls decide_local."""
        with self.lock, self.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            c.execute(
                "UPDATE requests SET status='expired' WHERE status='queued_sandbox' AND created<?",
                (time.time() - 600,),
            )
            if c.execute(
                "SELECT count(*) FROM jobs WHERE status IN ('running','reviewing','stopping')"
            ).fetchone()[0]:
                return
            row = c.execute(
                "SELECT * FROM requests WHERE status='queued_sandbox' ORDER BY created LIMIT 1"
            ).fetchone()
            if not row:
                return
            p = json.loads(row["payload"])
            authorization = json.loads(row["result"]).get("authorization")
            grant = c.execute(
                "SELECT token FROM sandbox_grants WHERE project=? AND root=?",
                (p.get("project"), p.get("root")),
            ).fetchone()
            if (
                row["kind"] not in ("desktop", "job")
                or not grant
                or grant["token"] != authorization
                or digest(canonical(p).encode()) != row["hash"]
            ):
                c.execute(
                    "UPDATE requests SET status='failed',result=? WHERE id=?",
                    (
                        canonical(
                            {"error": "Sandbox authorization invalid or revoked"}
                        ),
                        row["id"],
                    ),
                )
                return
            c.execute(
                "UPDATE requests SET status='executing' WHERE id=? AND status='queued_sandbox'",
                (row["id"],),
            )
        try:
            if not self.sandbox_authorization(p["project"], p["root"], authorization):
                raise PolicyError("Sandbox grant revoked or project root changed")
            p["authorization"] = authorization
            if row["kind"] == "job":
                result = self.start_job(row["id"], p)
            else:
                from .desktop_vm import launch

                result = launch(self, row["id"], p)
            state = "started"
        except Exception as e:
            result, state = {"error": str(e)}, "failed"
        with self.connect() as c:
            c.execute(
                "UPDATE requests SET status=?,result=? WHERE id=?",
                (state, canonical(result), row["id"]),
            )
        self.audit("sandbox_grant_dispatch", {"id": row["id"], "status": state})
        return result
