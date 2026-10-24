"""Regression for the eight text-only MCP extension tools. No user files touched."""
import hashlib
import sys
import tempfile
from pathlib import Path

from udb_companion import Runtime, TOOLS

def check(rt, name, expect_error=False, **kwargs):
    result = rt.tool(name, kwargs)
    body = result["content"][0]["text"]
    assert result.get("isError", False) == expect_error, (name, body)
    assert body.startswith("ERROR |" if expect_error else "PASS |"), (name, body)
    return body

def main():
    assert len(TOOLS) == 29, f"Expected 29 Companion tools, found {len(TOOLS)}"
    with tempfile.TemporaryDirectory(prefix="udb-regression-") as root:
        rt = Runtime(root, enable_terminal=True)
        check(rt, "file_mkdir", path="work")
        original = Path(root) / "work" / "a.txt"
        original.write_text("first\nmatch here\nlast\n", encoding="utf-8")
        digest = hashlib.sha256(original.read_bytes()).hexdigest()
        assert digest in check(rt, "file_stat", path="work/a.txt")
        assert "match here" in check(rt, "file_read_lines", path="work/a.txt", offset=1, length=1)
        assert "match here" in check(rt, "file_search_context", path="work", text="match", context=1)
        check(rt, "file_copy", source="work/a.txt", destination="work/b.txt", expected_sha256=digest)
        check(rt, "file_copy", expect_error=True, source="work/a.txt", destination="work/b.txt")
        check(rt, "file_copy", expect_error=True, source="work/a.txt", destination="work/c.txt",
              expected_sha256="0" * 64)
        assert not (Path(root) / "work" / "c.txt").exists()
        check(rt, "file_move", source="work/b.txt", destination="work/c.txt")
        assert not (Path(root) / "work" / "b.txt").exists()
        check(rt, "file_move", expect_error=True, source="work/a.txt", destination="work/c.txt")
        check(rt, "file_read_lines", expect_error=True, path="../outside.txt")
        job = rt.start(sys.executable, ["-u", "-c", "print('extension-terminal-ok')"], ".", 0)
        sid = job["session_id"]
        assert sid in check(rt, "terminal_list")
        result = rt.tool("terminal_wait", {"session_id": sid, "timeout_ms": 1000})
        for _ in range(10):
            if result["content"][0]["text"].startswith("PASS |"):
                break
            result = rt.tool("terminal_wait", {"session_id": sid, "timeout_ms": 300})
        assert result["content"][0]["text"].startswith("PASS |"), result
    print("PASS | 8 extension tools tested (file/terminal/security)")

if __name__ == "__main__":
    main()

