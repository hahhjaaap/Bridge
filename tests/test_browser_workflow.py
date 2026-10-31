"""Browser transfers and locally controlled login/persistence boundaries."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from udb import browser_files
from udb.browser_settings import remember_login, set_remember_login_local
from udb.core import Agent, PolicyError


def test_saved_login_is_local_opt_in_and_forget_removes_only_owned_profile(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(
        "udb.browser_settings.status",
        lambda agent: {"worker_ready": True, "sessions": []},
    )
    agent = Agent(tmp_path)
    assert not remember_login(agent)
    set_remember_login_local(agent, True)
    assert remember_login(agent)
    profile = agent.state / "host/browser-profile"
    profile.mkdir(parents=True)
    (profile / "CookieFixture").write_text("synthetic")
    set_remember_login_local(agent, False)
    assert not profile.exists() and not remember_login(agent)


def test_login_preference_cannot_change_while_browser_is_running(tmp_path, monkeypatch):
    agent = Agent(tmp_path)
    monkeypatch.setattr(
        "udb.browser_settings.status", lambda agent: {"sessions": [{"kind": "browser"}]}
    )
    with pytest.raises(PolicyError, match="Close"):
        set_remember_login_local(agent, True)
    assert not remember_login(agent)


class Browser:
    def __init__(self, folder):
        self.folder = folder
        self.calls = []
        self.url = "https://example.org"
        self.nodes = [1]
        self.node = {"nodeName": "INPUT", "attributes": ["type", "file"]}

    def evaluate(self, tab, expression, timeout=10):
        return self.url if expression == "location.origin" else {"ok": True}

    def sid(self, tab):
        if tab != "owned":
            raise PolicyError("Unknown tab")
        return "session"

    def call(self, method, params=None, session=None, timeout=10):
        self.calls.append((method, params))
        if method == "DOM.getDocument":
            return {"root": {"nodeId": 1}}
        if method == "Runtime.evaluate" and params.get("returnByValue") is False:
            attributes = self.node.get("attributes", [])
            valid = (
                len(self.nodes) == 1
                and self.node.get("nodeName") == "INPUT"
                and dict(zip(attributes[::2], attributes[1::2], strict=True)).get("type") == "file"
                and "disabled" not in attributes[::2]
            )
            return (
                {"result": {"subtype": "node", "objectId": "remote-file-input"}}
                if valid
                else {"result": {"subtype": "null"}}
            )
        if method == "DOM.requestNode":
            return {"nodeId": 1}
        if method == "DOM.describeNode":
            return {"node": self.node}
        return {}


def test_upload_stages_exact_bytes_until_browser_closes_without_submission(tmp_path):
    browser = Browser(tmp_path)
    source = Path("invoice.xlsx")
    result = browser_files.upload(
        browser, "owned", "input[type=file]", source, b"fixture", browser.url
    )
    staged_call = next(
        params
        for method, params in browser.calls
        if method == "DOM.setFileInputFiles"
    )
    staged = Path(staged_call["files"][0])
    assert staged.read_bytes() == b"fixture" and staged.name == "invoice.xlsx"
    assert result["selected"] and result["submitted"] is False
    assert [method for method, _ in browser.calls].count("DOM.setFileInputFiles") == 1


@pytest.mark.parametrize(
    "nodes,node",
    [
        ([], {}),
        ([1, 2], {}),
        ([1], {"nodeName": "INPUT", "attributes": ["type", "text"]}),
        ([1], {"nodeName": "INPUT", "attributes": ["type", "file", "disabled", ""]}),
    ],
)
def test_upload_rejects_ambiguous_or_non_file_input_before_selecting(
    tmp_path, nodes, node
):
    browser = Browser(tmp_path)
    browser.nodes, browser.node = nodes, node
    with pytest.raises(PolicyError):
        browser_files.upload(
            browser, "owned", "input", Path("a.txt"), b"x", browser.url
        )
    assert not any(method == "DOM.setFileInputFiles" for method, _ in browser.calls)


def test_transfer_checks_page_origin_before_any_network_or_file_selection(tmp_path):
    browser = Browser(tmp_path)
    with pytest.raises(PolicyError, match="origin changed"):
        browser_files.upload(
            browser, "owned", "input", Path("a.txt"), b"x", "https://other.org"
        )
    assert browser.calls == []
    with pytest.raises(PolicyError, match="currently observed origin"):
        browser_files.download(browser, "owned", "https://other.org/a", browser.url)
    assert browser.calls == []


def test_remote_browser_tool_cannot_change_handoff_or_login_preferences(tmp_path):
    import asyncio

    from udb.server import make_server

    async def check():
        server = make_server(Agent(tmp_path))
        _, result = await server.call_tool(
            "host_browser", {"action": "browser_presentation"}
        )
        return result

    # FastMCP raises rather than exposing the local manager operation.
    with pytest.raises(Exception, match="Unsupported background browser action"):
        asyncio.run(check())


def host_engine(tmp_path):
    import time

    from udb.host_client import atomic_json
    from udb.host_service import HostService
    from udb.resources import Governor

    host = HostService.__new__(HostService)
    host.agent = Agent(tmp_path / "home")
    atomic_json(host.agent.state / "host-settings.json", {"enabled": True})
    host.folder = host.agent.state / "host"
    host.folder.mkdir()
    host.resource = Governor().update(
        dict(
            at=time.time(),
            cpu_percent=10,
            ram_percent=30,
            available_mib=8000,
            commit_percent=30,
        )
    )
    host.sessions = {
        "a" * 32: dict(
            kind="browser",
            expires=time.time() + 600,
            object=SimpleNamespace(visible=False),
        )
    }
    return host


def test_human_browser_handoff_blocks_remote_mutations_and_open(tmp_path):
    host = host_engine(tmp_path)
    host.sessions["a" * 32]["object"].visible = True
    for action in ("click", "upload", "download", "open"):
        with pytest.raises(PolicyError, match="owner has browser control"):
            host.dispatch(
                "browser",
                dict(action=action, session_id="a" * 32, url="https://example.org/"),
            )


def test_browser_transfer_needs_separate_computer_grant(tmp_path, monkeypatch):
    host = host_engine(tmp_path)

    def denied(agent):
        raise PolicyError("Computer access is disabled")

    monkeypatch.setattr("udb.computer_access.require", denied)
    with pytest.raises(PolicyError, match="Computer access"):
        host.dispatch(
            "browser", dict(action="upload", session_id="a" * 32, path="anything")
        )


def test_changed_upload_and_existing_download_destination_do_not_transfer(
    tmp_path, monkeypatch
):
    import hashlib

    host = host_engine(tmp_path)
    monkeypatch.setattr("udb.computer_access.require", lambda agent: "grant")
    source = tmp_path / "invoice.txt"
    source.write_bytes(b"changed")
    monkeypatch.setattr(
        browser_files, "upload", lambda *a: pytest.fail("must not upload")
    )
    with pytest.raises(PolicyError, match="file changed"):
        host.dispatch(
            "browser",
            dict(
                action="upload",
                session_id="a" * 32,
                path=str(source),
                expected_sha256=hashlib.sha256(b"old").hexdigest(),
            ),
        )
    monkeypatch.setattr(
        browser_files, "download", lambda *a: pytest.fail("must not fetch")
    )
    with pytest.raises(PolicyError, match="already exists"):
        host.dispatch(
            "browser",
            dict(action="download", session_id="a" * 32, destination=str(source)),
        )
    assert source.read_bytes() == b"changed"
