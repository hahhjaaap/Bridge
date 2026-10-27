"""Failure injection with disposable files; no real grants or network requests."""

import io
import json
import subprocess
import sys
from datetime import timedelta
from types import SimpleNamespace

import pytest

from udb import components, media, resources
from udb.companion import background, documents, processes, search
from udb.companion.files import ScopedFiles
from udb.launcher import child_environment, worker_command


def test_tunnel_run_sends_the_standard_initialized_notification(tmp_path, monkeypatch):
    from udb import desktop

    controller = desktop.DesktopController(tmp_path / "bridge", monitor=False)
    helper = tmp_path / "tunnel-client.exe"
    helper.write_bytes(b"synthetic helper")
    calls = []
    monkeypatch.setattr(
        controller, "config", lambda: {"tunnel_id": "tunnel_" + "a" * 32}
    )
    monkeypatch.setattr(desktop, "load_key", lambda: "synthetic-not-a-real-key")
    monkeypatch.setattr(components, "component_path", lambda *a: helper)
    monkeypatch.setattr(
        desktop.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=0)
    )
    monkeypatch.setattr(
        controller, "start_process", lambda name, argv, env=None: calls.append(argv)
    )
    try:
        controller.start_tunnel()
        assert len(calls) == 1
        assert "--mcp.stdio-send-initialized-notification" in calls[0]
    finally:
        controller.close()


@pytest.mark.parametrize("extension", [".xlsx", ".docx"])
def test_corrupt_office_archive_is_a_normal_validation_error(tmp_path, extension):
    (tmp_path / ("broken" + extension)).write_bytes(b"not an office archive")
    with pytest.raises(ValueError, match="archive|ZIP|Office"):
        documents.checked_input(
            ScopedFiles(str(tmp_path)), "broken" + extension, extension
        )


@pytest.mark.parametrize(
    "format,extension", [("spreadsheet", ".xlsx"), ("word_document", ".docx")]
)
def test_companion_survives_a_corrupt_document_request(tmp_path, format, extension):
    (tmp_path / ("broken" + extension)).write_bytes(b"not an office archive")
    frames = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {
                "name": format,
                "arguments": {"action": "inspect", "path": "broken" + extension},
            },
        },
        {"jsonrpc": "2.0", "id": 3, "method": "ping", "params": {}},
    ]
    result = subprocess.run(
        worker_command("companion", ["--root", str(tmp_path)]),
        input="".join(json.dumps(frame) + "\n" for frame in frames),
        cwd=tmp_path,
        env=child_environment(),
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr[-2000:]
    replies = {row["id"]: row for row in map(json.loads, result.stdout.splitlines())}
    assert replies[2]["result"]["isError"]
    assert replies[3]["result"] == {}


@pytest.fixture
def workbook(tmp_path):
    import openpyxl

    path = tmp_path / "duration.xlsx"
    book = openpyxl.Workbook()
    book.active["A1"] = timedelta(hours=27, minutes=4, seconds=5)
    book.active["A1"].number_format = "[h]:mm:ss"
    book.save(path)
    book.close()
    return ScopedFiles(str(tmp_path))


def test_excel_elapsed_time_can_be_returned_as_json(workbook):
    row = documents.spreadsheet(
        workbook, {"action": "read", "path": "duration.xlsx", "range": "A1:A1"}, None
    )
    assert json.loads(json.dumps(row))["rows"] == [["1 day, 3:04:05"]]


@pytest.mark.parametrize("range", ["XFE1:XFE1", "A1048577:A1048577"])
def test_excel_read_rejects_cells_outside_the_file_format(workbook, range):
    with pytest.raises(ValueError, match="range|XLSX|limit"):
        documents.spreadsheet(
            workbook, {"action": "read", "path": "duration.xlsx", "range": range}, None
        )


class ShortJSONInput:
    def __init__(self, requests):
        self.wire = bytearray()
        self.closed = False
        self.requests = requests

    def write(self, data):
        count = min(7, len(data))
        self.wire.extend(data[:count])
        return count

    def close(self):
        if not self.closed:
            self.closed = True
            self.requests.append(json.loads(bytes(self.wire)))


@pytest.mark.parametrize("operation", ["document", "search"])
def test_worker_jobs_send_complete_requests_when_a_pipe_writes_partially(
    tmp_path, monkeypatch, operation
):
    import openpyxl

    path = tmp_path / "input.xlsx"
    book = openpyxl.Workbook()
    book.active["A1"] = "fixture"
    book.save(path)
    book.close()
    requests = []
    response = {"format": "spreadsheet", "rows": [["fixture"]]}
    if operation == "search":
        response = {
            "type": "done",
            "value": {"files_scanned": 1, "matches": 0, "truncated": False},
        }

    class Worker:
        def __init__(self, *args, **kwargs):
            self.stdin = ShortJSONInput(requests)
            self.stdout = io.BytesIO((json.dumps(response) + "\n").encode())

        def poll(self):
            return 0

        def wait(self, timeout=None):
            return 0

        def terminate(self):
            pass

        def close(self):
            pass

    monkeypatch.setattr(processes, "ManagedProcess", Worker)
    monkeypatch.setattr(search, "ManagedProcess", Worker)
    bg = background.Background(ScopedFiles(str(tmp_path)))
    try:
        if operation == "document":
            row = documents.Documents(bg).start(
                "spreadsheet", action="read", path="input.xlsx"
            )
        else:
            row = bg.search(text="fixture", search_type="files")
        task = bg.tasks[row["job_id"]]
        task.thread.join(5)
        result = task.read()
        assert result["state"] == "completed", result
        assert len(requests) == 1 and requests[0]["root"] == str(tmp_path)
    finally:
        bg.close()


@pytest.fixture
def media_fixture(tmp_path, monkeypatch):
    downloads = tmp_path / "Downloads"
    downloads.mkdir()
    agent = SimpleNamespace(home=tmp_path / "app", audit=lambda *a: None)
    monkeypatch.setattr(media.Path, "home", staticmethod(lambda: tmp_path))
    monkeypatch.setattr(media, "_hosts", lambda _: {"media.example.test"})
    monkeypatch.setattr(media, "_manifest", lambda *a: (10.0, 1))
    monkeypatch.setattr(resources, "guard_new_heavy_task", lambda *a: None)
    monkeypatch.setattr(components, "component_path", lambda name, home=None: name)
    payload = dict(
        url="https://media.example.test/video.m3u8",
        filename="result.mp4",
        root=str(downloads.resolve()),
        host="media.example.test",
    )
    calls = []

    def configure(duration="10.03", reported_size=None):
        data = b"synthetic media bytes"

        def run(argv, **kwargs):
            calls.append(kwargs)
            if argv[0] == "ffmpeg":
                from pathlib import Path

                Path(argv[-1]).write_bytes(data)
                return SimpleNamespace(returncode=0)
            value = {
                "format": {
                    "duration": duration,
                    "size": str(len(data) if reported_size is None else reported_size),
                },
                "streams": [{"codec_type": "video"}],
            }
            return SimpleNamespace(returncode=0, stdout=json.dumps(value).encode())

        monkeypatch.setattr(media.subprocess, "run", run)

    return agent, downloads, payload, calls, configure


@pytest.mark.parametrize("duration", ["nan", "inf", "30.0"])
def test_media_never_publishes_an_invalid_or_unexpected_duration(
    media_fixture, duration
):
    agent, downloads, payload, _, configure = media_fixture
    configure(duration=duration)
    with pytest.raises(ValueError, match="duration|bounds"):
        media.run_download(agent, "fixture", payload)
    assert not (downloads / "result.mp4").exists()
    assert not list(downloads.glob(".udb-*.mp4"))


def test_media_checks_probe_size_against_the_actual_file(media_fixture):
    agent, downloads, payload, _, configure = media_fixture
    configure(reported_size=100)
    with pytest.raises(ValueError, match="size|bounds"):
        media.run_download(agent, "fixture", payload)
    assert not (downloads / "result.mp4").exists()


def test_media_helpers_do_not_inherit_tunnel_authentication(media_fixture, monkeypatch):
    agent, _, payload, calls, configure = media_fixture
    monkeypatch.setenv("CONTROL_PLANE_API_KEY", "synthetic-not-a-real-key")
    monkeypatch.setenv("TUNNEL_CLIENT_PROFILE_DIR", "synthetic-profile")
    configure()
    media.run_download(agent, "fixture", payload)
    assert len(calls) == 2
    for kwargs in calls:
        assert isinstance(kwargs.get("env"), dict)
        assert "CONTROL_PLANE_API_KEY" not in kwargs["env"]
        assert "TUNNEL_CLIENT_PROFILE_DIR" not in kwargs["env"]
