"""Native workspace dashboard: discussions, task board and the owned browser's live cache."""

import argparse
import datetime
import hashlib
import json
import os
from pathlib import Path
import queue
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
import tkinter as tk
from tkinter import ttk, scrolledtext, simpledialog, messagebox

import room
from fast_live import Mirror, singleton

sys.path.insert(0, str(room.ROOT / "browser_deps"))
from PIL import Image, ImageTk

VIETNAM = datetime.timezone(datetime.timedelta(hours=7))
STATE_LABELS = {
    "todo": "Chờ làm",
    "doing": "Đang làm",
    "blocked": "Đang vướng",
    "done": "Hoàn tất",
}


def local_time(value):
    try:
        return (
            datetime.datetime.fromisoformat(value)
            .astimezone(VIETNAM)
            .strftime("%d/%m %H:%M")
        )
    except (ValueError, TypeError):
        return value or ""


def history(db, topic, query="", limit=200):
    db.create_function("casefold", 1, str.casefold, deterministic=True)
    rows = db.execute(
        """SELECT * FROM messages WHERE topic=?
        AND (?='' OR instr(casefold(body),casefold(?))>0 OR instr(casefold(author),casefold(?))>0)
        ORDER BY id DESC LIMIT ?""",
        (topic, query, query, query, limit + 1),
    ).fetchall()
    return {
        "messages": [dict(r) for r in reversed(rows[:limit])],
        "more": len(rows) > limit,
    }


def read_browser_cache(root):
    base = Path(root) / "browser-cache"
    health = json.loads((base / "health.json").read_text(encoding="utf-8"))
    meta = json.loads((base / "latest.json").read_text(encoding="utf-8"))
    path = (Path(root) / meta["file"]).resolve()
    if path.parent != base.resolve() or path.suffix.lower() != ".jpg":
        raise ValueError("Ảnh nằm ngoài cache browser")
    data = path.read_bytes()
    if hashlib.sha256(data).hexdigest() != meta["sha256"]:
        raise ValueError("Checksum ảnh không khớp")
    age = max(0, time.time() - meta["captured_unix_ms"] / 1000)
    alive = (
        bool(health["running"])
        and time.time() - health["heartbeat_unix_ms"] / 1000 < 10
    )
    return health, meta, data, age, alive


class Dashboard:
    def __init__(self, root=room.ROOT, interval_ms=25, test_mode=False):
        self.root = Path(root)
        self.interval_ms = interval_ms
        self.test_mode = test_mode
        self.mirror = Mirror(self.root, interval_ms)
        self.db = self.mirror.db
        self.app = tk.Tk()
        self.app.title("UDB Workspace · Common Room")
        self.app.geometry("1160x800")
        self.app.minsize(960, 680)
        self.topic = tk.StringVar(value="general")
        self.search = tk.StringVar()
        self.name = tk.StringVar(value="Bạn")
        self.kind = tk.StringVar(value="message")
        self.reply = None
        self.topic_names = []
        self.messages = {}
        self.tasks = {}
        self.force = True
        self.last_frame = None
        self.photo = None
        self.browser_process = None
        self.events = queue.Queue()
        self.pending = None
        self.pending_result = None
        self.closed = False
        self._build()
        self.search_job = None
        self.search.trace_add("write", lambda *_: self.schedule_search())
        self.app.protocol("WM_DELETE_WINDOW", self.close)
        self.refresh(force=True)

    def _build(self):
        style = ttk.Style(self.app)
        style.theme_use("clam")
        style.configure(".", font=("Segoe UI", 10))
        style.configure("Treeview", rowheight=29)
        style.configure("Header.TLabel", font=("Segoe UI", 18, "bold"))
        style.configure("Small.TLabel", foreground="#64748b")
        header = ttk.Frame(self.app, padding=(16, 12))
        header.pack(fill="x")
        ttk.Label(header, text="Common Room", style="Header.TLabel").pack(side="left")
        self.stats = ttk.Label(header, text="", style="Small.TLabel")
        self.stats.pack(side="left", padx=20)
        ttk.Label(header, text="Tên của bạn:").pack(side="left", padx=(20, 4))
        ttk.Entry(header, textvariable=self.name, width=20).pack(side="left")
        ttk.Button(header, text="Tham gia", command=lambda: self.safe(self.join)).pack(
            side="left", padx=6
        )
        self.meta = ttk.Label(
            self.app,
            text="Đang kết nối...",
            padding=(16, 0, 16, 8),
            style="Small.TLabel",
        )
        self.meta.pack(fill="x")
        panes = ttk.Panedwindow(self.app, orient="horizontal")
        panes.pack(fill="both", expand=True, padx=12, pady=(0, 12))
        sidebar = ttk.Frame(panes, padding=8)
        panes.add(sidebar, weight=1)
        ttk.Label(sidebar, text="CHỦ ĐỀ", font=("Segoe UI", 10, "bold")).pack(
            anchor="w"
        )
        self.topics = tk.Listbox(
            sidebar,
            exportselection=False,
            height=10,
            font=("Segoe UI", 10),
            relief="flat",
            width=25,
        )
        self.topics.pack(fill="x", pady=8)
        self.topics.bind("<<ListboxSelect>>", self.choose_topic)
        ttk.Button(
            sidebar, text="+ Tạo chủ đề", command=lambda: self.safe(self.add_topic)
        ).pack(fill="x")
        ttk.Label(
            sidebar,
            text="THÀNH VIÊN",
            font=("Segoe UI", 10, "bold"),
            padding=(0, 20, 0, 6),
        ).pack(anchor="w")
        self.members = tk.Listbox(
            sidebar,
            font=("Segoe UI", 9),
            relief="flat",
            width=25,
            exportselection=False,
        )
        self.members.pack(fill="both", expand=True)
        ttk.Label(
            sidebar,
            text="Thời gian: Việt Nam\nLần hoạt động ≠ đang online",
            style="Small.TLabel",
        ).pack(anchor="w", pady=10)
        self.tabs = ttk.Notebook(panes)
        panes.add(self.tabs, weight=5)
        discussion = ttk.Frame(self.tabs, padding=8)
        tasks = ttk.Frame(self.tabs, padding=8)
        browser = ttk.Frame(self.tabs, padding=8)
        self.tabs.add(discussion, text="  Thảo luận  ")
        self.tabs.add(tasks, text="  Công việc  ")
        self.tabs.add(browser, text="  Browser · 1 FPS  ")
        self._discussion(discussion)
        self._task_board(tasks)
        self._browser(browser)

    def _discussion(self, page):
        bar = ttk.Frame(page)
        bar.pack(fill="x", pady=(0, 8))
        self.goal = ttk.Label(bar, text="general", font=("Segoe UI", 11, "bold"))
        self.goal.pack(side="left")
        ttk.Entry(bar, textvariable=self.search, width=30).pack(side="right")
        ttk.Label(bar, text="Tìm trong chủ đề: ").pack(side="right")
        self.message_list = scrolledtext.ScrolledText(
            page,
            height=14,
            wrap="word",
            font=("Segoe UI", 10),
            state="disabled",
            padx=12,
            pady=10,
            relief="flat",
            spacing1=3,
            spacing3=8,
        )
        self.message_list.pack(fill="both", expand=True)
        self.message_list.tag_configure(
            "header", font=("Segoe UI", 10, "bold"), foreground="#334155"
        )
        self.message_list.tag_configure("selected", background="#e0f2fe")
        self.message_list.bind("<Button-1>", self.click_message)
        self.history_note = ttk.Label(page, text="", style="Small.TLabel")
        self.history_note.pack(anchor="w", pady=3)
        self.detail = scrolledtext.ScrolledText(
            page, height=3, wrap="word", font=("Segoe UI", 10), state="disabled"
        )
        self.detail.pack(fill="x", pady=(0, 6))
        replybar = ttk.Frame(page)
        replybar.pack(fill="x")
        self.reply_label = ttk.Label(replybar, text="Tin mới", style="Small.TLabel")
        self.reply_label.pack(side="left")
        ttk.Button(replybar, text="Bỏ trả lời", command=self.clear_reply).pack(
            side="left", padx=8
        )
        ttk.Combobox(
            replybar,
            textvariable=self.kind,
            values=room.KINDS,
            state="readonly",
            width=12,
        ).pack(side="right")
        self.editor = scrolledtext.ScrolledText(
            page, height=3, wrap="word", font=("Segoe UI", 10)
        )
        self.editor.pack(fill="x", pady=5)
        self.editor.bind("<Control-Return>", lambda _: self.send_event())
        sendbar = ttk.Frame(page)
        sendbar.pack(fill="x")
        ttk.Label(
            sendbar,
            text="Ctrl+Enter để gửi · chọn một tin để trả lời",
            style="Small.TLabel",
        ).pack(side="left")
        self.send_button = ttk.Button(
            sendbar, text="Gửi tin", command=lambda: self.safe(self.send)
        )
        self.send_button.pack(side="right")
        # Reserve the composer first; history scrolls within the remaining height.
        for widget in (sendbar, self.editor, replybar, self.detail, self.history_note):
            widget.pack_configure(before=self.message_list, side="bottom")

    def _task_board(self, page):
        bar = ttk.Frame(page)
        bar.pack(fill="x", pady=(0, 10))
        ttk.Label(
            bar, text="Công việc trong chủ đề", font=("Segoe UI", 11, "bold")
        ).pack(side="left")
        ttk.Button(
            bar, text="+ Tạo việc", command=lambda: self.safe(self.add_task)
        ).pack(side="right")
        self.task_stats = ttk.Label(page, text="", style="Small.TLabel")
        self.task_stats.pack(anchor="w", pady=(0, 8))
        self.task_list = ttk.Treeview(
            page,
            columns=("title", "assignee", "state", "revision"),
            show="tree headings",
        )
        for key, text, width in [
            ("#0", "#", 45),
            ("title", "Công việc", 350),
            ("assignee", "Người phụ trách", 160),
            ("state", "Trạng thái", 110),
            ("revision", "Bản", 50),
        ]:
            self.task_list.heading(key, text=text)
            self.task_list.column(key, width=width, stretch=key == "title")
        self.task_list.pack(fill="both", expand=True)
        controls = ttk.Frame(page)
        controls.pack(fill="x", pady=8)
        for state, label in STATE_LABELS.items():
            ttk.Button(
                controls,
                text=label,
                command=lambda s=state: self.safe(lambda: self.update_task(s)),
            ).pack(side="left", padx=3)
        ttk.Label(
            page,
            text="Chọn việc rồi đổi trạng thái. Nếu chat khác đã sửa, hãy đọc lại trước khi cập nhật.",
            style="Small.TLabel",
            wraplength=700,
        ).pack(anchor="w")

    def _browser(self, page):
        bar = ttk.Frame(page)
        bar.pack(fill="x")
        self.start_button = ttk.Button(
            bar, text="Bật browser riêng", command=lambda: self.safe(self.start_browser)
        )
        self.start_button.pack(side="left")
        ttk.Button(
            bar,
            text="Dừng browser của cửa sổ này",
            command=lambda: self.safe(self.stop_browser),
        ).pack(side="left", padx=6)
        self.browser_note = ttk.Label(
            page,
            text="Chưa có ảnh. Bật browser để tạo cache 1 FPS.",
            style="Small.TLabel",
            wraplength=800,
        )
        self.browser_note.pack(anchor="w", pady=8)
        self.image_label = ttk.Label(page, anchor="center", text="Ảnh browser mới nhất")
        self.image_label.pack(fill="both", expand=True)
        ttk.Label(
            page,
            text="Chuỗi JSON · chỉ thực hiện các bước đã được giao",
            style="Small.TLabel",
        ).pack(anchor="w", pady=(8, 0))
        self.browser_editor = scrolledtext.ScrolledText(
            page, height=4, font=("Consolas", 10), wrap="word"
        )
        self.browser_editor.insert(
            "1.0",
            json.dumps(
                {"steps": [{"op": "status"}, {"op": "read"}]}, ensure_ascii=False
            ),
        )
        self.browser_editor.pack(fill="x", pady=5)
        self.run_button = ttk.Button(
            page,
            text="Chạy chuỗi",
            command=lambda: self.safe(self.run_browser),
            state="disabled",
        )
        self.run_button.pack(anchor="e")
        self.browser_result = scrolledtext.ScrolledText(
            page, height=4, font=("Consolas", 9), wrap="word", state="disabled"
        )
        self.browser_result.pack(fill="x", pady=(6, 0))

    def safe(self, action):
        try:
            return action()
        except (ValueError, TypeError, KeyError, sqlite3.Error, OSError) as exc:
            if self.test_mode:
                raise
            messagebox.showerror("Không thể thực hiện", str(exc), parent=self.app)

    def schedule_search(self):
        if self.search_job:
            self.app.after_cancel(self.search_job)
        self.search_job = self.app.after(180, lambda: self.safe(self.render_messages))

    def choose_topic(self, _=None):
        selection = self.topics.curselection()
        if selection:
            self.topic.set(self.topic_names[selection[0]])
            self.clear_reply()
            self.detail_text("")
            self.render_messages()
            self.render_tasks()

    def detail_text(self, text):
        self.detail.configure(state="normal")
        self.detail.delete("1.0", "end")
        self.detail.insert("1.0", text)
        self.detail.configure(state="disabled")

    def click_message(self, event):
        for tag in self.message_list.tag_names("@" + str(event.x) + "," + str(event.y)):
            if tag.startswith("message-"):
                self.choose_message(int(tag.split("-", 1)[1]))
                break

    def choose_message(self, message_id):
        item = self.messages.get(int(message_id))
        if item is None:
            return
        self.reply = item["id"]
        self.message_list.tag_remove("selected", "1.0", "end")
        ranges = self.message_list.tag_ranges("message-" + str(message_id))
        if ranges:
            self.message_list.tag_add("selected", *ranges)
        self.reply_label.configure(
            text="Trả lời #" + str(self.reply) + " · " + item["author"]
        )
        self.detail_text(
            "#"
            + str(item["id"])
            + " · "
            + item["author"]
            + " · "
            + local_time(item["created_at"])
            + "\n\n"
            + item["body"]
        )

    def clear_reply(self):
        self.message_list.tag_remove("selected", "1.0", "end")
        self.reply = None
        self.reply_label.configure(text="Tin mới")

    def join(self):
        room.join(self.db, self.name.get(), "Người dùng trong cửa sổ workspace")
        self.force = True
        self.refresh(force=True)

    def send_event(self):
        self.safe(self.send)
        return "break"

    def send(self):
        author = room.check_text(self.name.get(), 80, "name")
        body = room.check_text(self.editor.get("1.0", "end-1c"), 32000, "body")
        room.post(
            self.db,
            argparse.Namespace(
                author=author,
                topic=self.topic.get(),
                kind=self.kind.get(),
                body=body,
                reply_to=self.reply,
                request_id=str(uuid.uuid4()),
            ),
        )
        self.editor.delete("1.0", "end")
        self.clear_reply()
        self.refresh(force=True)

    def add_topic(self):
        name = simpledialog.askstring("Chủ đề mới", "Tên chủ đề:", parent=self.app)
        if not name:
            return
        goal = simpledialog.askstring(
            "Mục tiêu", "Chủ đề này cần giải quyết điều gì?", parent=self.app
        )
        if goal:
            room.topic(self.db, name, goal)
            self.topic.set(name.strip())
            self.clear_reply()
            self.refresh(force=True)

    def render_messages(self):
        topic = self.topic.get()
        result = history(self.db, topic, self.search.get().strip())
        self.messages = {m["id"]: m for m in result["messages"]}
        at_bottom = self.message_list.yview()[1] >= 0.99
        anchor = self.message_list.index("@0,0")
        self.message_list.configure(state="normal")
        self.message_list.delete("1.0", "end")
        for tag in self.message_list.tag_names():
            if tag.startswith("message-"):
                self.message_list.tag_delete(tag)
        for m in result["messages"]:
            tag = "message-" + str(m["id"])
            relation = (" · trả lời #" + str(m["reply_to"])) if m["reply_to"] else ""
            header = (
                "#"
                + str(m["id"])
                + " · "
                + m["author"]
                + " · "
                + m["kind"]
                + " · "
                + local_time(m["created_at"])
                + relation
                + "\n"
            )
            self.message_list.insert("end", header, ("header", tag))
            self.message_list.insert("end", m["body"] + "\n\n", (tag,))
            if self.reply == m["id"]:
                ranges = self.message_list.tag_ranges(tag)
                if ranges:
                    self.message_list.tag_add("selected", *ranges)
        self.message_list.configure(state="disabled")
        if at_bottom:
            self.message_list.see("end")
        else:
            self.message_list.yview(anchor)
        count = len(result["messages"])
        self.history_note.configure(
            text=str(count)
            + " tin · nội dung đầy đủ, tự xuống dòng"
            + (
                " · còn lịch sử, dùng room.py export để lấy đủ"
                if result["more"]
                else ""
            )
        )
        goal = self.db.execute(
            "SELECT goal FROM topics WHERE name=?", (topic,)
        ).fetchone()
        self.goal.configure(text=topic + (" · " + goal["goal"][:55] if goal else ""))

    def add_task(self):
        title = simpledialog.askstring(
            "Tạo công việc", "Nội dung công việc:", parent=self.app
        )
        if not title:
            return
        assignee = simpledialog.askstring(
            "Phụ trách",
            "Tên thành viên đã tham gia (để trống nếu chưa giao):",
            parent=self.app,
        )
        if assignee is None:
            return
        room.task_create(self.db, title, self.topic.get(), assignee.strip() or None)
        self.refresh(force=True)

    def render_tasks(self):
        items = room.task_list(self.db, topic=self.topic.get())
        selected = self.task_list.selection()
        self.task_list.delete(*self.task_list.get_children())
        self.tasks = {item["id"]: item for item in items["tasks"]}
        counts = {s: 0 for s in room.TASK_STATES}
        for item in reversed(items["tasks"]):
            counts[item["state"]] += 1
            self.task_list.insert(
                "",
                "end",
                iid=str(item["id"]),
                text=str(item["id"]),
                values=(
                    item["title"],
                    item["assignee"] or "Chưa giao",
                    STATE_LABELS[item["state"]],
                    item["revision"],
                ),
            )
        if selected and self.task_list.exists(selected[0]):
            self.task_list.selection_set(selected[0])
        self.task_stats.configure(
            text=" · ".join(
                STATE_LABELS[s] + ": " + str(counts[s]) for s in room.TASK_STATES
            )
            + (" · còn việc chưa hiển thị" if items["more"] else "")
        )

    def update_task(self, state):
        selection = self.task_list.selection()
        if not selection:
            raise ValueError("Hãy chọn một công việc")
        task = self.tasks[int(selection[0])]
        try:
            room.task_update(self.db, task["id"], state, task["revision"])
        finally:
            self.refresh(force=True)

    def refresh(self, force=False):
        changed, elapsed = self.mirror.refresh(force=force)
        if changed:
            status = room.status(self.db)
            self.topic_names = [t["name"] for t in status["topics"]]
            self.topics.delete(0, "end")
            for topic in status["topics"]:
                self.topics.insert(
                    "end", topic["name"] + "  (" + str(topic["messages"]) + ")"
                )
            if self.topic.get() not in self.topic_names:
                self.topic.set("general")
            self.topics.selection_set(self.topic_names.index(self.topic.get()))
            self.members.delete(0, "end")
            for member in status["participants"]:
                self.members.insert(
                    "end", member["name"] + " · " + local_time(member["last_seen"])
                )
            count = self.db.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
            self.stats.configure(
                text=str(len(status["participants"]))
                + " thành viên · "
                + str(len(status["topics"]))
                + " chủ đề · "
                + str(count)
                + " tin"
            )
            self.meta.configure(
                text="LIVE · kiểm tra "
                + str(self.interval_ms)
                + " ms · snapshot "
                + str(round(elapsed, 2))
                + " ms · dữ liệu lưu trên máy"
            )
            self.render_messages()
            self.render_tasks()
        return changed

    def start_browser(self):
        if self.browser_process and self.browser_process.poll() is None:
            return
        try:
            health = json.loads(
                (self.root / "browser-cache" / "health.json").read_text(
                    encoding="utf-8"
                )
            )
            if (
                health.get("running")
                and time.time() - health["heartbeat_unix_ms"] / 1000 < 10
            ):
                raise ValueError(
                    "Một browser worker khác đang chạy. Cửa sổ này chỉ xem cache; hãy dùng worker trong chat đã tạo nó."
                )
        except FileNotFoundError:
            pass
        self.browser_process = subprocess.Popen(
            [sys.executable, "-B", "-u", str(room.ROOT / "browser_flow.py")],
            cwd=str(self.root),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            bufsize=1,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        process = self.browser_process
        self.start_button.configure(state="disabled")

        def read_output():
            for line in process.stdout:
                self.events.put(line.rstrip())
            self.events.put("__BROWSER_EXIT__")

        threading.Thread(target=read_output, daemon=True).start()

    def stop_browser(self):
        if self.browser_process and self.browser_process.poll() is None:
            self.browser_process.stdin.close()
            try:
                self.browser_process.wait(timeout=4)
            except subprocess.TimeoutExpired:
                self.browser_process.terminate()
        self.browser_process = None
        self.pending = None
        self.run_button.configure(state="disabled")
        self.start_button.configure(state="normal")

    def run_browser(self):
        if not self.browser_process or self.browser_process.poll() is not None:
            raise ValueError("Hãy bật browser trong cửa sổ này trước")
        if self.pending:
            raise ValueError("Chuỗi trước chưa có kết quả; không gửi lại hành động")
        req = json.loads(self.browser_editor.get("1.0", "end-1c"))
        if not isinstance(req, dict):
            raise ValueError("Yêu cầu phải là JSON object")
        req.setdefault("id", "ui-" + str(uuid.uuid4()))
        self.pending = req["id"]
        self.pending_result = None
        self.browser_process.stdin.write(json.dumps(req, ensure_ascii=False) + "\n")
        self.browser_process.stdin.flush()
        self.run_button.configure(state="disabled")
        self.browser_note.configure(text="Đang chạy " + str(self.pending) + "...")

    def drain_events(self):
        for _ in range(100):
            try:
                line = self.events.get_nowait()
            except queue.Empty:
                break
            if line == "BROWSER_READY":
                self.run_button.configure(state="normal")
            elif line == "__BROWSER_EXIT__":
                self.run_button.configure(state="disabled")
                self.start_button.configure(state="normal")
                self.browser_note.configure(text="Browser của cửa sổ này đã dừng.")
            elif line.startswith("BROWSER_DONE:"):
                rid = line.split(":", 1)[1]
                if self.pending_result and str(self.pending_result.get("id")) == rid:
                    self.browser_result.configure(state="normal")
                    self.browser_result.delete("1.0", "end")
                    self.browser_result.insert(
                        "1.0",
                        json.dumps(self.pending_result, ensure_ascii=False, indent=2)[
                            :16000
                        ],
                    )
                    self.browser_result.configure(state="disabled")
                    self.pending = None
                    self.pending_result = None
                    self.run_button.configure(state="normal")
            else:
                try:
                    result = json.loads(line)
                    if isinstance(result, dict) and "ok" in result:
                        self.pending_result = result
                except ValueError:
                    if line:
                        self.browser_note.configure(text=line[:250])

    def render_browser(self):
        import io

        try:
            health, meta, data, age, alive = read_browser_cache(self.root)
            fresh = alive and age < 4 and not health.get("last_error")
            self.browser_note.configure(
                text=("LIVE" if fresh else "ẢNH CŨ / WORKER DỪNG")
                + " · "
                + meta["url"]
                + " · ảnh "
                + str(round(age, 1))
                + " giây trước · mục tiêu 1 FPS"
            )
            key = (meta["worker"], meta["sequence"])
            if key != self.last_frame:
                image = Image.open(io.BytesIO(data))
                image.thumbnail((760, 340), Image.Resampling.LANCZOS)
                self.photo = ImageTk.PhotoImage(image, master=self.app)
                self.image_label.configure(image=self.photo, text="")
                self.last_frame = key
        except (OSError, ValueError, KeyError) as exc:
            self.browser_note.configure(
                text="Chưa có ảnh hợp lệ / cache đang xoay vòng · " + str(exc)[:100]
            )

    def tick(self):
        if self.closed:
            return
        try:
            self.refresh()
            self.drain_events()
        except (sqlite3.Error, OSError) as exc:
            self.meta.configure(text="Đang thử lại: " + str(exc))
        self.app.after(self.interval_ms, self.tick)

    def browser_tick(self):
        if self.closed:
            return
        self.render_browser()
        self.app.after(1000, self.browser_tick)

    def close(self):
        if self.closed:
            return
        self.closed = True
        self.stop_browser()
        self.mirror.close()
        self.app.destroy()

    def run(self):
        lock = singleton(self.root)
        try:
            self.app.after(0, self.tick)
            self.app.after(0, self.browser_tick)
            print(
                json.dumps({"dashboard": "ready", "pid": os.getpid(), "version": 2}),
                flush=True,
            )
            self.app.mainloop()
        finally:
            if lock:
                import ctypes

                kernel = ctypes.WinDLL("kernel32")
                kernel.CloseHandle.argtypes = [ctypes.c_void_p]
                kernel.CloseHandle(lock)


def selftest():
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        dash = Dashboard(root, test_mode=True)
        try:
            dash.app.withdraw()
            room.join(dash.db, "Tester", "UI test")
            dash.name.set("Tester")
            long_body = (
                "Tin nhắn dài phải được đọc đầy đủ — kiểm tra tiếng Việt.\\n" * 100
            ).replace("\\n", "\n")
            dash.editor.insert("1.0", long_body)
            dash.send()
            assert len(dash.messages) == 1
            assert long_body in dash.message_list.get("1.0", "end-1c")
            assert dash.message_list.cget("wrap") == "word"
            dash.choose_message(1)
            dash.editor.insert("1.0", "Reply")
            dash.send()
            assert (
                dash.db.execute("SELECT reply_to FROM messages WHERE id=2").fetchone()[
                    0
                ]
                == 1
            )
            room.topic(dash.db, "architecture", "Architecture discussion")
            room.task_create(
                dash.db, "Review worker", assignee="Tester", request_id="ui-task"
            )
            dash.refresh(force=True)
            assert len(dash.tasks) == 1
            dash.task_list.selection_set("1")
            dash.update_task("doing")
            assert dash.tasks[1]["state"] == "doing"
            dash.search.set("Reply")
            dash.render_messages()
            assert len(dash.messages) == 1
            writer = room.connect(root / "workspace.sqlite3")
            room.post(
                writer,
                argparse.Namespace(
                    author="Tester",
                    topic="general",
                    kind="message",
                    body="Remote reply",
                    reply_to=1,
                    request_id="remote",
                ),
            )
            writer.close()
            assert dash.refresh()
            assert len(dash.messages) == 2
            cache = root / "browser-cache"
            cache.mkdir()
            image = Image.new("RGB", (1280, 720), "#334155")
            path = cache / "frame-test.jpg"
            image.save(path)
            data = path.read_bytes()
            (cache / "latest.json").write_text(
                json.dumps(
                    {
                        "file": "browser-cache/frame-test.jpg",
                        "sha256": hashlib.sha256(data).hexdigest(),
                        "captured_unix_ms": round(time.time() * 1000),
                        "url": "about:blank",
                        "worker": "test",
                        "sequence": 1,
                    }
                ),
                encoding="utf-8",
            )
            (cache / "health.json").write_text(
                json.dumps(
                    {"running": True, "heartbeat_unix_ms": round(time.time() * 1000)}
                ),
                encoding="utf-8",
            )
            dash.render_browser()
            assert dash.photo is not None
            dash.app.geometry("960x680")
            dash.app.deiconify()
            dash.app.update()
            assert (
                dash.send_button.winfo_rooty() + dash.send_button.winfo_height()
                <= dash.app.winfo_rooty() + dash.app.winfo_height()
            )
            assert dash.message_list.winfo_height() > 50
            return {
                "ok": True,
                "checks": [
                    "full multiline messages without truncation",
                    "word wrapping",
                    "composer visible at minimum window size",
                    "native UI sends message",
                    "reply-to preserved",
                    "task state update",
                    "topic creation",
                    "search",
                    "cross-chat refresh",
                    "verified browser image preview",
                ],
                "production_data_touched": False,
            }
        finally:
            dash.close()


if __name__ == "__main__":
    room.configure_output()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--selftest", action="store_true")
    args = parser.parse_args()
    if args.selftest:
        print(json.dumps(selftest(), ensure_ascii=False), flush=True)
        print("DASHBOARD_TESTS_OK", flush=True)
    else:
        Dashboard().run()
