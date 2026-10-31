"""Owned headless Edge worker: bounded action chains and a ready-to-read 1 FPS image cache."""

import argparse
import asyncio
import contextlib
import hashlib
import http.server
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import time
import uuid
from urllib.parse import urlsplit

import room

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "browser_deps"))

FIELDS = {
    "navigate": {"op", "url"},
    "click": {"op", "selector"},
    "fill": {"op", "selector", "value"},
    "press": {"op", "selector", "key"},
    "wait_for": {"op", "selector", "state"},
    "read": {"op", "selector"},
    "snapshot": {"op"},
    "status": {"op"},
}


def atomic_json(path, data):
    temp = path.with_suffix(".tmp")
    temp.write_text(
        json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8"
    )
    os.replace(temp, path)


def validate(req):
    if not isinstance(req, dict) or set(req) - {"id", "steps"}:
        raise ValueError("Envelope fields: id, steps")
    rid = req.get("id")
    if (
        not isinstance(rid, str)
        or not 1 <= len(rid) <= 100
        or any(
            c
            not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_.:"
            for c in rid
        )
    ):
        raise ValueError("id must be a stable ASCII identifier")
    steps = req.get("steps")
    if not isinstance(steps, list) or not 1 <= len(steps) <= 24:
        raise ValueError("Expected 1..24 steps")
    for step in steps:
        if (
            not isinstance(step, dict)
            or not isinstance(step.get("op"), str)
            or step["op"] not in FIELDS
        ):
            raise ValueError("Unsupported browser operation")
        op = step["op"]
        if set(step) - FIELDS[op]:
            raise ValueError("Unknown fields for " + op)
        for key, value in step.items():
            if not isinstance(value, str) or len(value) > 16000:
                raise ValueError("Step fields must be bounded strings")
        if op == "navigate":
            url = urlsplit(step.get("url", ""))
            if (
                url.scheme not in ("http", "https")
                or not url.hostname
                or url.username
                or url.password
            ):
                raise ValueError(
                    "navigate requires an HTTP(S) URL without embedded credentials"
                )
        if op in ("click", "fill", "press", "wait_for") and not step.get("selector"):
            raise ValueError("Unique selector required")
        if op == "fill" and "value" not in step:
            raise ValueError("fill requires value")
        if op == "press" and not step.get("key"):
            raise ValueError("press requires key")
        if op == "wait_for" and step.get("state", "visible") not in (
            "visible",
            "hidden",
            "attached",
            "detached",
        ):
            raise ValueError("Invalid wait state")
    return rid, steps


class BrowserWorker:
    def __init__(self, cache):
        self.cache = Path(cache)
        self.cache.mkdir(parents=True, exist_ok=True)
        self.identity = uuid.uuid4().hex
        self.sequence = 0
        self.frame = None
        self.last_error = None
        self.capture_lock = asyncio.Lock()
        self.db = sqlite3.connect(str(self.cache / "requests.sqlite3"))
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS requests(id TEXT PRIMARY KEY,digest TEXT NOT NULL,result TEXT)"
        )
        self.db.commit()
        self.playwright = None
        self.browser = None
        self.capture_task = None
        self.worker_lock = None
        self.started = False
        self.intervals = []
        self.last_capture_start = None

    async def start(self):
        # A lock on this worker's own cache prevents competing writers, with automatic OS release.
        if os.name == "nt":
            import msvcrt

            self.worker_lock = (self.cache / "worker.lock").open("a+b")
            self.worker_lock.seek(0)
            self.worker_lock.write(b"0")
            self.worker_lock.flush()
            self.worker_lock.seek(0)
            msvcrt.locking(self.worker_lock.fileno(), msvcrt.LK_NBLCK, 1)
        from playwright.async_api import async_playwright

        self.playwright = await async_playwright().start()
        self.browser = await self.playwright.chromium.launch(
            channel="msedge", headless=True
        )
        self.context = await self.browser.new_context(
            viewport={"width": 1280, "height": 720},
            device_scale_factor=1,
            accept_downloads=False,
            service_workers="block",
        )
        self.context.set_default_timeout(8000)
        self.context.set_default_navigation_timeout(15000)
        self.page = await self.context.new_page()
        # This service owns just one tab. Popups must not escape its capture/action scope.
        self.context.on(
            "page",
            lambda page: (
                asyncio.create_task(page.close()) if page != self.page else None
            ),
        )
        self.started = True
        await self.capture()
        self.capture_task = asyncio.create_task(self.capture_loop())

    async def capture(self):
        async with self.capture_lock:
            started = time.perf_counter()
            content = await self.page.screenshot(
                type="jpeg", quality=65, full_page=False, timeout=3000
            )
            self.sequence += 1
            name = "frame-" + self.identity[:8] + "-" + str(self.sequence) + ".jpg"
            path = self.cache / name
            path.write_bytes(
                content
            )  # Unique immutable file; publish pointer only when complete.
            self.frame = {
                "file": str(path.relative_to(ROOT))
                if path.is_relative_to(ROOT)
                else name,
                "sequence": self.sequence,
                "worker": self.identity,
                "captured_unix_ms": round(time.time() * 1000),
                "url": self.page.url,
                "width": 1280,
                "height": 720,
                "bytes": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
                "capture_ms": round((time.perf_counter() - started) * 1000, 3),
            }
            atomic_json(self.cache / "latest.json", self.frame)
            self.last_error = None
            # Keep a short ring so a tool can fetch the exact image named in an earlier pointer.
            frames = sorted(
                self.cache.glob("frame-*.jpg"), key=lambda p: p.stat().st_mtime_ns
            )
            for old in frames[:-8]:
                old.unlink(missing_ok=True)
            self.health(True)
            return dict(self.frame)

    def health(self, running):
        atomic_json(
            self.cache / "health.json",
            {
                "running": running,
                "pid": os.getpid(),
                "worker": self.identity,
                "heartbeat_unix_ms": round(time.time() * 1000),
                "target_fps": 1,
                "last_error": self.last_error,
                "latest_sequence": self.sequence,
                "last_frame_unix_ms": self.frame["captured_unix_ms"]
                if self.frame
                else None,
            },
        )

    async def capture_loop(self):
        deadline = asyncio.get_running_loop().time() + 1
        while True:
            await asyncio.sleep(max(0, deadline - asyncio.get_running_loop().time()))
            started = time.perf_counter()
            if self.last_capture_start is not None:
                self.intervals.append(started - self.last_capture_start)
                self.intervals = self.intervals[-60:]
            self.last_capture_start = started
            try:
                await self.capture()
            except Exception as exc:
                self.last_error = type(exc).__name__ + ": " + str(exc)[:300]
                self.health(True)
            deadline += 1
            if deadline < asyncio.get_running_loop().time():
                deadline = (
                    asyncio.get_running_loop().time() + 1
                )  # Drop late ticks; no capture backlog.

    async def execute(self, req):
        start = time.perf_counter()
        results = []
        rid, steps = validate(req)
        digest = hashlib.sha256(
            json.dumps(req, sort_keys=True, ensure_ascii=False).encode()
        ).hexdigest()
        old = self.db.execute(
            "SELECT digest,result FROM requests WHERE id=?", (rid,)
        ).fetchone()
        if old:
            if old[0] != digest:
                raise ValueError("Request id already used for different actions")
            if old[1] is None:
                raise ValueError(
                    "Previous outcome is uncertain; inspect state before issuing new actions"
                )
            out = json.loads(old[1])
            out["cached_result"] = True
            return out
        with self.db:
            self.db.execute("INSERT INTO requests VALUES(?,?,NULL)", (rid, digest))
        ok = True
        for i, step in enumerate(steps):
            tick = time.perf_counter()
            op = step["op"]
            try:
                if op == "navigate":
                    response = await self.page.goto(
                        step["url"], wait_until="domcontentloaded"
                    )
                    value = {
                        "url": self.page.url,
                        "http_status": response.status if response else None,
                    }
                elif op == "status":
                    value = {
                        "url": self.page.url,
                        "frame": self.frame,
                        "last_error": self.last_error,
                    }
                elif op == "snapshot":
                    value = await self.capture()
                elif op == "read":
                    body = await self.page.locator(
                        step.get("selector", "body")
                    ).inner_text()
                    value = {
                        "text": body[:16000],
                        "truncated": len(body) > 16000,
                        "url": self.page.url,
                    }
                else:
                    locator = self.page.locator(step["selector"])
                    if op == "click":
                        await locator.click()
                    elif op == "fill":
                        await locator.fill(step["value"])
                    elif op == "press":
                        await locator.press(step["key"])
                    elif op == "wait_for":
                        await locator.wait_for(state=step.get("state", "visible"))
                    value = {"completed": True}
                results.append(
                    {
                        "index": i,
                        "op": op,
                        "ok": True,
                        "result": value,
                        "ms": round((time.perf_counter() - tick) * 1000, 3),
                    }
                )
            except Exception as exc:
                results.append(
                    {
                        "index": i,
                        "op": op,
                        "ok": False,
                        "error": type(exc).__name__ + ": " + str(exc)[:1500],
                    }
                )
                ok = False
                break
        # A fresh post-chain image, in addition to the regular 1 FPS background stream.
        frame = None
        frame_error = None
        try:
            frame = await self.capture()
        except Exception as exc:
            frame_error = type(exc).__name__ + ": " + str(exc)[:300]
        out = {
            "id": rid,
            "ok": ok,
            "results": results,
            "executed": sum(r["ok"] for r in results),
            "partial_actions": not ok and any(r["ok"] for r in results),
            "frame": frame,
            "frame_error": frame_error,
            "ms": round((time.perf_counter() - start) * 1000, 3),
        }
        with self.db:
            self.db.execute(
                "UPDATE requests SET result=? WHERE id=?",
                (json.dumps(out, ensure_ascii=False), rid),
            )
        return out

    async def close(self):
        if self.capture_task:
            self.capture_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.capture_task
        if self.browser:
            await self.browser.close()
        if self.playwright:
            await self.playwright.stop()
        if self.started:
            self.health(False)
        self.db.close()
        if self.worker_lock:
            self.worker_lock.close()


async def serve():
    worker = BrowserWorker(ROOT / "browser-cache")
    try:
        await worker.start()
        print(
            json.dumps(
                {
                    "ready": True,
                    "target_fps": 1,
                    "worker": worker.identity,
                    "frame": worker.frame,
                }
            ),
            flush=True,
        )
        print("BROWSER_READY", flush=True)
        while True:
            line = await asyncio.to_thread(sys.stdin.readline)
            if not line:
                break
            req = None
            try:
                if len(line.encode("utf-8")) > 512000:
                    raise ValueError("Request too large")
                req = json.loads(line)
                out = await worker.execute(req)
            except Exception as exc:
                out = {
                    "id": req.get("id") if isinstance(req, dict) else None,
                    "ok": False,
                    "error": str(exc)[:1500],
                }
            print(
                json.dumps(out, ensure_ascii=False, separators=(",", ":")), flush=True
            )
            print("BROWSER_DONE:" + str(out.get("id")), flush=True)
    finally:
        await worker.close()


async def selftest():
    html = b"""<!doctype html><title>Owned Browser Test</title>
    <label>Name <input id="name"></label><button id="send">Apply</button>
    <p id="out">Ready</p><p id="count">0</p>
    <script>let count=0;document.querySelector('#send').onclick=()=>{
      document.querySelector('#out').textContent='Hello '+document.querySelector('#name').value;
      document.querySelector('#count').textContent=String(++count);};</script>"""

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(html)

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with tempfile.TemporaryDirectory() as tmp:
            worker = BrowserWorker(Path(tmp))
            try:
                await worker.start()
                request = {
                    "id": "test-chain",
                    "steps": [
                        {
                            "op": "navigate",
                            "url": "http://127.0.0.1:" + str(server.server_port),
                        },
                        {"op": "fill", "selector": "#name", "value": "Nam"},
                        {"op": "click", "selector": "#send"},
                        {"op": "wait_for", "selector": "#out"},
                        {"op": "read", "selector": "#out"},
                        {"op": "press", "selector": "#name", "key": "Tab"},
                        {"op": "read", "selector": "#count"},
                    ],
                }
                result = await worker.execute(request)
                assert result["ok"], result
                assert result["results"][4]["result"]["text"] == "Hello Nam"
                assert result["results"][6]["result"]["text"] == "1"
                retry = await worker.execute(request)
                assert retry["cached_result"]
                assert await worker.page.locator("#count").inner_text() == "1"
                invalid = {
                    "id": "invalid",
                    "steps": [{"op": "navigate", "url": "file:///private"}],
                }
                try:
                    await worker.execute(invalid)
                    raise AssertionError("file URL accepted")
                except ValueError:
                    pass
                fail = await worker.execute(
                    {
                        "id": "fail-chain",
                        "steps": [
                            {"op": "read", "selector": "button"},
                            {"op": "fill", "selector": "button", "value": "invalid"},
                            {"op": "click", "selector": "#send"},
                        ],
                    }
                )
                assert (
                    not fail["ok"]
                    and fail["executed"] == 1
                    and len(fail["results"]) == 2
                )
                assert await worker.page.locator("#count").inner_text() == "1"
                initial = worker.sequence
                await asyncio.sleep(3.2)
                assert worker.sequence >= initial + 3
                metadata = json.loads(
                    (worker.cache / "latest.json").read_text(encoding="utf-8")
                )
                image_path = worker.cache / Path(metadata["file"]).name
                content = image_path.read_bytes()
                assert (
                    content[:2] == b"\xff\xd8"
                    and hashlib.sha256(content).hexdigest() == metadata["sha256"]
                )
                assert len(list(worker.cache.glob("frame-*.jpg"))) <= 8
                return {
                    "ok": True,
                    "checks": [
                        "7 browser steps",
                        "observed DOM result",
                        "retry does not repeat click",
                        "file URL rejected",
                        "stop at failed action",
                        "real 1 FPS screenshots",
                        "image hash",
                        "bounded frame ring",
                    ],
                    "chain_ms": result["ms"],
                    "last_capture_ms": metadata["capture_ms"],
                    "background_intervals_ms": [
                        round(t * 1000, 2) for t in worker.intervals
                    ],
                    "frame_bytes": len(content),
                }
            finally:
                await worker.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


if __name__ == "__main__":
    room.configure_output()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--selftest", action="store_true")
    args = parser.parse_args()
    if args.selftest:
        print(json.dumps(asyncio.run(selftest()), ensure_ascii=False), flush=True)
        print("BROWSER_TESTS_OK", flush=True)
    else:
        asyncio.run(serve())
