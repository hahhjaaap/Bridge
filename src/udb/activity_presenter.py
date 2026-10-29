"""Bounded tool-call history for the local UI; no task-progress inference."""

import math
import time
from collections import OrderedDict

from .activity import EventReader, clean

TOOL_LABELS = {
    "host_browser": "Trình duyệt riêng",
    "host_screenshot": "Ảnh browser",
    "host_start_session": "Mở phiên riêng",
    "host_close_session": "Đóng phiên riêng",
    "host_status": "Trạng thái tác nhân",
    "computer_desktop": "Điều khiển Windows",
    "computer_files": "Tệp trên máy",
    "computer_process": "Tiến trình trên máy",
    "computer_status": "Quyền điều khiển",
    "spreadsheet": "Bảng tính",
    "word_document": "Tài liệu Word",
    "pdf_document": "Tài liệu PDF",
    "terminal": "Terminal",
    "read_file": "Đọc tệp dự án",
    "propose_file_write": "Đề xuất sửa tệp",
}
ACTION_LABELS = {
    "open": "Mở trang",
    "navigate": "Chuyển trang",
    "click": "Nhấp trên trang",
    "fill": "Điền biểu mẫu",
    "select_option": "Chọn dropdown",
    "press": "Nhấn phím",
    "read": "Đọc nội dung",
    "snapshot": "Quan sát trang",
    "wait_for": "Chờ điều kiện",
    "upload": "Chọn tệp tải lên",
    "download": "Tải tệp",
    "list": "Xem danh sách",
    "check": "Đánh dấu lựa chọn",
    "uncheck": "Bỏ đánh dấu",
    "read_text": "Đọc văn bản",
    "set_checked": "Đặt trạng thái checkbox",
    "select": "Chọn mục",
    "expand": "Mở nội dung",
    "collapse": "Thu nội dung",
    "scroll_into_view": "Đưa mục vào vùng nhìn",
    "close_window": "Yêu cầu đóng cửa sổ",
    "apps": "Danh sách ứng dụng",
    "launch_app": "Mở ứng dụng",
    "clipboard_get": "Đọc clipboard",
    "clipboard_set": "Ghi clipboard",
    "start": "Khởi chạy",
    "stop": "Dừng tác vụ",
}


def describe(row):
    tool = row.get("tool", "")
    details = row.get("details")
    details = details if isinstance(details, dict) else {}
    name = TOOL_LABELS.get(tool, clean(tool, 60) or "Công cụ UDB")
    action_value = details.get("action")
    action = ACTION_LABELS.get(action_value) if isinstance(action_value, str) else None
    return name + (" · " + action if action else "")


def event_state(row):
    if row.get("kind") == "call":
        return "Chờ kết quả"
    return {
        "PASS": "Đã trả kết quả",
        "ERROR": "Cần kiểm tra",
        "RUNNING": "Đã giao tác vụ nền",
    }.get(row.get("status"), "Chưa xác nhận")


class ActivityFeed:
    def __init__(self, home):
        self.reader = EventReader(home)
        self.after = None
        self.rows = OrderedDict()

    def poll(self):
        events = self.reader.read(after=self.after, limit=80)
        if events and self.after is not None and events[0]["seq"] <= self.after:
            self.rows.clear()  # The disposable or owner log was replaced/rotated.
        for row in events:
            self.after = row["seq"]
            row = dict(row)
            if row.get("kind") not in ("call", "result"):
                continue
            for name in ("tool", "call_id", "status"):
                row[name] = clean(row.get(name, ""), 80)
            stamp = row.get("at")
            if (
                type(stamp) not in (int, float)
                or not math.isfinite(stamp)
                or not 0 <= stamp <= time.time() + 86400
            ):
                row["at"] = 0
            key = (row.get("tool"), row.get("call_id") or str(row["seq"]))
            self.rows.pop(key, None)
            self.rows[key] = row
            while len(self.rows) > 40:
                self.rows.popitem(last=False)
        return list(reversed(self.rows.values()))


def summary(rows, now=None):
    if not rows:
        return "Chưa có hoạt động", "Giao việc trong ChatGPT để bắt đầu.", "neutral"
    current = next((row for row in rows if row.get("kind") == "call"), rows[0])
    title, tone = event_state(current), "neutral"
    if current.get("kind") == "call":
        age = (time.time() if now is None else now) - current.get("at", 0)
        title = "Đang gọi công cụ" if 0 <= age < 60 else "Chưa nhận được kết quả"
    elif current.get("status") == "ERROR":
        tone = "paused"
    return title, describe(current), tone
