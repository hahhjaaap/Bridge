"""Companion feature API. Existing proxy owner grant remains mandatory."""

from __future__ import annotations

import time

from udb.companion.background import Background
from udb.companion.documents import Documents
from udb.companion.system_processes import ProcessCatalog
from udb.protocol import result


class Features:
    def __init__(self, rt):
        self.rt = rt
        self.background = Background(rt.files, getattr(rt, "capacity", None))
        self.catalog = ProcessCatalog(rt.files.root, getattr(rt, "bridge_home", None))
        self.documents = Documents(self.background)

    def ensure_pointer(self):
        if not hasattr(self, "pointer"):
            from udb.companion.virtual_pointer import VirtualPointer

            self.pointer = VirtualPointer()
        return self.pointer

    def close(self):
        if hasattr(self, "pointer"):
            self.pointer.close()
            del self.pointer
        try:
            self.background.close()
        finally:
            self.catalog.observations.clear()

    def tool(self, name, a):
        if name == "work_recovery":
            from udb.companion.journal import WorkRecovery

            return result(WorkRecovery(self.rt).call(**a))
        if name == "virtual_pointer":
            return result(self.ensure_pointer().call(**a))
        if name in ("spreadsheet", "word_document", "pdf_document"):
            return result(self.documents.start(name, **a))
        if name == "terminal_interact":
            session = self.rt.sessions.get(a["session_id"])
            if not session:
                raise ValueError("Unknown terminal session")
            if "columns" in a or "rows" in a:
                session.process.resize(a.get("columns", 100), a.get("rows", 30))
            baseline = session.position
            if a.get("text"):
                session.send(a["text"])
            wait = max(0, min(1500, int(a.get("wait_ms", 1000))))
            deadline = time.monotonic() + wait / 1000
            while True:
                ready, prompt = session.prompt(a.get("expected_prompt", ""))
                if a.get("text") and session.position <= baseline:
                    ready = False
                    prompt = ""
                if (
                    ready
                    or session.process.poll() is not None
                    or time.monotonic() >= deadline
                ):
                    break
                time.sleep(0.025)
            data = session.read(a.get("offset"), 0)
            data.update(
                prompt_detected=ready,
                prompt=prompt,
                prompt_confidence="heuristic",
                waiting_for_input=ready and data["state"] == "RUNNING",
            )
            return result(data, data["state"])
        if name == "search_start":
            return result(self.background.search(**a))
        if name == "search_read":
            return result(
                self.background.task(
                    a.get("job_id", ""), "read", a.get("offset", 0), a.get("limit", 50)
                )
            )
        if name == "search_stop":
            return result(self.background.task(a["job_id"], "cancel"))
        if name == "file_read_chunk":
            return result(self.background.read_chunk(**a))
        if name == "file_upload":
            return result(self.background.upload(**a))
        if name == "file_operation":
            return result(self.background.file_operation(**a))
        if name == "system_process_list":
            return result(self.catalog.list(**a))
        if name == "system_process_terminate":
            return result(self.catalog.terminate(a["observation_id"]))
        raise ValueError("Unknown feature")


def schema(name, description, properties, required=()):
    return dict(
        name=name,
        description=description,
        inputSchema=dict(
            type="object",
            properties=properties,
            required=list(required),
            additionalProperties=False,
        ),
    )


S = {"type": "string"}
INTEGER = {"type": "integer"}
B = {"type": "boolean"}
TOOLS = [
    schema(
        "terminal_interact",
        "Queue input; ConPTY accepts VT keys and supports columns/rows resize. Output is VT UTF-8 for ConPTY, plain pipe otherwise. Prompt detection is heuristic.",
        dict(
            session_id=S,
            text=S,
            wait_ms=INTEGER,
            offset=INTEGER,
            expected_prompt=S,
            columns=INTEGER,
            rows=INTEGER,
        ),
        ("session_id",),
    ),
    schema(
        "search_start",
        "Start an isolated background filename/content search inside the project. Regex supported; worker time/memory bounded. Partial results can be paged while running. No secret/protected paths.",
        dict(
            path=S,
            pattern=S,
            text=S,
            search_type=dict(type="string", enum=["files", "content"]),
            regex=B,
            case_sensitive=B,
            timeout_seconds=INTEGER,
        ),
    ),
    schema(
        "search_read",
        "Read progressive search results/status by job ID and offset, up to 100 rows. Retained jobs are lost on server restart.",
        dict(job_id=S, offset=INTEGER, limit=INTEGER),
        ("job_id",),
    ),
    schema(
        "search_stop",
        "Cancel this Companion-owned search only. Partial matches remain readable.",
        dict(job_id=S),
        ("job_id",),
    ),
    schema(
        "file_read_chunk",
        "Read a project file up to 16 GiB in base64 chunks <=1 MiB. Pass expected_version across reads to detect changes; chunk SHA256 verifies bytes.",
        dict(path=S, offset=INTEGER, limit=INTEGER, expected_version=S),
        ("path",),
    ),
    schema(
        "file_upload",
        "Create a new large project file using begin/write/status/commit/abort. Begin needs size and full SHA256. Sequential base64 chunks <=1 MiB; destination never overwritten. Commit is an async job; read/cancel it with file_operation.",
        dict(
            action=dict(
                type="string", enum=["begin", "write", "status", "commit", "abort"]
            ),
            path=S,
            upload_id=S,
            content=S,
            offset=INTEGER,
            size=INTEGER,
            sha256=S,
        ),
        ("action",),
    ),
    schema(
        "file_operation",
        "Background copy/move of project files/trees; read/list/cancel jobs INCLUDING spreadsheet/word_document/pdf_document jobs. Copy/move limit 20,000 entries/16 GiB, no links/protected entries or overwrite; publishes after verification.",
        dict(
            action=dict(type="string", enum=["copy", "move", "read", "list", "cancel"]),
            source=S,
            destination=S,
            job_id=S,
            offset=INTEGER,
            limit=INTEGER,
        ),
        ("action",),
    ),
    schema(
        "system_process_list",
        "Read system process names, PIDs, CPU and memory, without command lines/environment. Observation IDs expire after 60s. Only eligible current-user processes may be terminated; UDB ancestors/install and critical/other-user processes are protected.",
        dict(match=S, offset=INTEGER, limit=INTEGER),
    ),
    schema(
        "system_process_terminate",
        "Terminate one recently observed, eligible same-user process. Requires observation_id, rechecks creation time/owner/critical status on the opened handle. No raw PID, no elevation; use only for a user-authorized stop.",
        dict(observation_id=S),
        ("observation_id",),
    ),
]
COMMON = dict(path=S, expected_sha256=S, destination=S, timeout_seconds=INTEGER)
TOOLS.extend(
    [
        schema(
            "work_recovery",
            "Durable work list/inspect/resume. Recover saved job results and last 64 KiB terminal output after backend loss. Resume explicitly restarts searches, unchanged document reads, or unchanged create-only file copies; never replay terminals, moves or uncertain writes. Not a process-memory restore.",
            dict(
                action=dict(type="string", enum=["list", "inspect", "resume"]),
                kind=dict(type="string", enum=["job", "terminal"]),
                work_id=S,
            ),
        ),
        schema(
            "virtual_pointer",
            "Independent visual AI pointer and bounded Win32 message mouse actions on observed windows. No SendInput/cursor relocation or automatic physical fallback. Compatibility depends on app; dispatched messages do not prove app success. Shared focus may change. list first; IDs expire 60s. Coordinates are physical screen pixels. Supports status/hide/move/click/drag/scroll.",
            dict(
                action=S,
                window_id=S,
                x=INTEGER,
                y=INTEGER,
                from_x=INTEGER,
                from_y=INTEGER,
                button=dict(type="string", enum=["left", "right", "middle"]),
                clicks=INTEGER,
                delta=INTEGER,
                horizontal=B,
                duration_ms=INTEGER,
            ),
            ("action",),
        ),
        schema(
            "spreadsheet",
            "Project XLSX read/write/edit plus Vietnamese accounting audit_invoices (read-only) and export_audit (separate NEW XLSX review report). For export_audit supply destination and source expected_sha256; original is never edited. Optional header_row 1..25, column_map field-to-header/column, tolerance_vnd. Detect possible duplicate invoice identifiers, missing fields and net+VAT vs total discrepancies; no tax-law conclusions or formula evaluation. Bounded background job, poll/cancel with file_operation. Office XML <=64 MiB; max 10000 invoice rows. Other spreadsheet formats unsupported.",
            dict(
                COMMON,
                action=dict(
                    type="string", enum=["inspect", "read", "write", "edit_cells", "audit_invoices", "export_audit"]
                ),
                sheet=S,
                range=S,
                sheets={"type": "object"},
                cells={"type": "object"},
                data_only=B,
                allow_formulas=B,
                header_row=INTEGER,
                column_map={"type": "object"},
                tolerance_vnd=INTEGER,
            ),
            ("action", "path"),
        ),
        schema(
            "word_document",
            "Project DOCX inspect/read/write/replace_text background job; poll/cancel with file_operation. read uses 0-based paragraph offset/limit<=200, body/tables only. write text or paragraphs plus tables (array of 2D text arrays). replace_text old/new/occurrences requires current source expected_sha256 and preserves unaffected run formatting. Optional destination creates a new file. No headers/footers/track-change editing. Never execute macros or external resources.",
            dict(
                COMMON,
                action=dict(
                    type="string", enum=["inspect", "read", "write", "replace_text"]
                ),
                text=S,
                paragraphs={"type": "array", "items": S},
                tables={
                    "type": "array",
                    "items": {"type": "array", "items": {"type": "array", "items": S}},
                },
                old=S,
                new=S,
                occurrences=INTEGER,
                offset=INTEGER,
                limit=INTEGER,
            ),
            ("action", "path"),
        ),
        schema(
            "pdf_document",
            "Project PDF inspect/read/write/merge/extract background job; poll/cancel with file_operation. <=32 MiB input, <=200 pages; read start_page is 1-based, limit<=10, text extraction only (no OCR). write creates Unicode text PDF from text. merge inputs (2..20 project PDFs) into path. extract pages (1-based list) from path into new destination, needs source expected_sha256. Existing write/merge output needs SHA256; NEW is create-only. No encrypted PDFs.",
            dict(
                COMMON,
                action=dict(
                    type="string", enum=["inspect", "read", "write", "merge", "extract"]
                ),
                text=S,
                inputs={"type": "array", "items": S},
                pages={"type": "array", "items": INTEGER},
                start_page=INTEGER,
                limit=INTEGER,
            ),
            ("action", "path"),
        ),
    ]
)
NAMES = {x["name"] for x in TOOLS}
