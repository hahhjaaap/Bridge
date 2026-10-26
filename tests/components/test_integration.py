import json
import time
from types import SimpleNamespace

from udb_unified import UnifiedProxy


class Backend:
    def __init__(self, enabled=True):
        self.enabled = enabled
        self.calls = []

    def request(self, method, params=None, **kw):
        self.calls.append((method, params))
        if method == "udb/close-sessions":
            return {"result": {}}
        if params["name"] == "computer_status":
            return {
                "result": {
                    "content": [
                        {"type": "text", "text": json.dumps({"enabled": self.enabled})}
                    ]
                }
            }
        if params["name"] == "host_status":
            return {
                "result": {
                    "content": [
                        {
                            "type": "text",
                            "text": json.dumps(
                                {
                                    "resources": {
                                        "at": time.time(),
                                        "admission_ready": False,
                                        "light_operations_ready": True,
                                    }
                                }
                            ),
                        }
                    ]
                }
            }
        return {"result": {"content": [{"type": "text", "text": "PASS | fixture"}]}}


def main():
    original, companion = Backend(False), Backend()
    proxy = UnifiedProxy(original, companion)
    proxy.tool_routes = {"terminal_start": companion, "bridge_status": original}
    try:
        proxy.call_tool({"name": "terminal_start", "arguments": {}})
    except RuntimeError:
        pass
    else:
        raise AssertionError("Revoked access reached Companion")
    assert companion.calls == [("udb/close-sessions", None)]
    assert proxy.call_tool({"name": "bridge_status", "arguments": {}})["content"][0][
        "text"
    ].startswith("PASS")
    original.enabled = True
    proxy.tool_routes.update(file_read=companion, terminal_start=companion)
    assert proxy.call_tool({"name": "file_read", "arguments": {"path": "fixture"}})[
        "content"
    ][0]["text"].startswith("PASS")
    before = len(companion.calls)
    try:
        proxy.call_tool({"name": "terminal_start", "arguments": {}})
    except RuntimeError:
        pass
    else:
        raise AssertionError("Heavy terminal launch ignored paused governor")
    assert len(companion.calls) == before
    print(
        "PASS | grants retained; light file works during CPU pause; heavy launch refused"
    )


if __name__ == "__main__":
    main()
