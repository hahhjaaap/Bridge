"""Cleanup controls remain usable while heavy work is paused by the governor."""

import time

import pytest

from udb.proxy import UnifiedProxy


class Primary:
    def __init__(self):
        self.calls = []

    def request(self, method, params, **_kwargs):
        assert method == "tools/call"
        name = params["name"]
        self.calls.append(name)
        if name == "computer_status":
            return {
                "result": {
                    "structuredContent": {
                        "enabled": True,
                        "capabilities": ["files", "execute"],
                    }
                }
            }
        if name == "host_status":
            return {
                "result": {
                    "structuredContent": {
                        "resources": {
                            "at": time.time(),
                            "admission_ready": False,
                            "light_operations_ready": False,
                        }
                    }
                }
            }
        raise AssertionError("Unexpected primary tool: " + name)


class Companion:
    def __init__(self):
        self.calls = []

    def request(self, method, params, **_kwargs):
        assert method == "tools/call"
        self.calls.append(params)
        return {"result": {"content": [{"type": "text", "text": "PASS | fixture"}]}}


@pytest.fixture
def proxy():
    original = Primary()
    companion = Companion()
    unified = UnifiedProxy(original, companion)
    unified.tool_routes = {"file_upload": companion}
    unified.tool_cache = [{"name": "file_upload"}]
    return unified, original, companion


@pytest.mark.parametrize("action", ["status", "abort"])
def test_upload_cleanup_survives_resource_pause(proxy, action):
    unified, original, companion = proxy
    result = unified.call_tool({
        "name": "file_upload",
        "arguments": {"action": action, "upload_id": "synthetic"},
    })
    assert result.get("isError") is not True
    assert original.calls == ["computer_status"]
    assert len(companion.calls) == 1
    assert companion.calls[0]["arguments"]["action"] == action


@pytest.mark.parametrize("action", ["begin", "write", "commit"])
def test_upload_mutations_still_require_resource_capacity(proxy, action):
    unified, original, companion = proxy
    with pytest.raises(RuntimeError, match="Machine busy"):
        unified.call_tool({
            "name": "file_upload",
            "arguments": {"action": action, "upload_id": "synthetic"},
        })
    assert original.calls == ["computer_status", "host_status"]
    assert companion.calls == []
