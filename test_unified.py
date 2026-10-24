"""Smoke test original UDB + Companion through the unified MCP proxy."""
import json
import hashlib
import itertools
import os
from pathlib import Path
import queue
import re
import subprocess
import sys
import threading
import tempfile
import time

ROOT = Path(__file__).resolve().parent
manifest = json.loads((ROOT / "mcp.json").read_text(encoding="utf-8"))
config = manifest["mcpServers"]["universal-desktop-bridge"]
argv = [part.replace("${PLUGIN_ROOT}", str(ROOT)) for part in config["args"]]
exe = config["command"].replace("${PLUGIN_ROOT}", str(ROOT))
p = subprocess.Popen([exe] + argv, cwd=str(ROOT), stdin=subprocess.PIPE,
                     stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0)
messages = queue.Queue()

def read_thread():
    while True:
        data = p.stdout.readline()
        if not data:
            break
        try:
            messages.put(json.loads(data))
        except (ValueError, UnicodeError):
            messages.put({"malformed": True})

threading.Thread(target=read_thread, daemon=True).start()

def rpc(identifier, method, params=None, timeout=30):
    if method == "tools/call" and isinstance(params, dict) and params.get("name") != "udb":
        params = {"name": "udb", "arguments": {"action": "call", "tool": params.get("name"), "arguments": params.get("arguments", {})}}
    p.stdin.write((json.dumps({"jsonrpc": "2.0", "id": identifier, "method": method,
                              "params": params or {}}, ensure_ascii=False) + "\n").encode("utf-8"))
    p.stdin.flush()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            data = messages.get(timeout=max(.1, deadline - time.monotonic()))
        except queue.Empty:
            break
        assert not data.get("malformed"), "Malformed JSON-RPC response"
        if data.get("id") != identifier:
            continue
        assert "error" not in data, data
        return data["result"]
    raise TimeoutError(f"Response timeout for {method}")

try:
    initial = rpc(1,"initialize",{"protocolVersion":"2024-11-05","capabilities":{},
                                   "clientInfo":{"name":"udb-unified-smoke","version":"1.0"}})
    assert initial["serverInfo"]["name"] == "UDB Unified"
    print("PASS | Unified MCP initialization")
    # Do not print credentials or raw status values.
    p.stdin.write(b'{"jsonrpc":"2.0","method":"notifications/initialized","params":{}}\n')
    p.stdin.flush()
    tools = rpc(2,"tools/list")["tools"]
    assert [tool["name"] for tool in tools] == ["udb"], tools
    catalog_result = rpc(3,"tools/call",{"name":"udb","arguments":{"action":"list"}})
    catalog = catalog_result["structuredContent"]["tools"]
    names = {tool["name"] for tool in catalog}
    assert len(names) == 60, f"Expected 60 internal operations, found {len(names)}"
    from udb_tool_catalog import HIDDEN_ALIASES
    assert not names.intersection(HIDDEN_ALIASES)
    assert {'file_transfer','file_operation','host_workspace','propose_media_download',
            'propose_sandbox_job','propose_desktop_session','desktop_action',
            'check_python_syntax','system_process_terminate'}.issubset(names)
    assert {"bridge_status", "computer_execute", "file_list", "terminal_start", "terminal_read"}.issubset(names)
    print("PASS | Unified discovery: one public MCP tool, 60 internal operations (66 backend routes retained)")
    status = rpc(4,"tools/call",{"name":"bridge_status","arguments":{}})
    assert status["content"][0]["text"].startswith("PASS |")
    assert len(status["content"]) >= 2
    print("PASS | Original UDB forwarding and compact status")
    files = rpc(5,"tools/call",{"name":"file_list","arguments":{"limit":2}})
    assert files["content"][0]["text"].startswith("PASS |")
    print("PASS | Companion file tool")
    blocked = rpc(6,"tools/call",{"name":"file_read","arguments":{"path":"../denied.txt"}})
    assert blocked["isError"]
    print("PASS | Companion project scope restriction")
    cmd = rpc(7,"tools/call",{"name":"terminal_start", "arguments":{
        "program":sys.executable,"arguments":["-u","-c","print('unified-terminal-done')"],
        "wait_ms":200}})
    raw = cmd["content"][0]["text"]
    session = re.search(r"Phiên: ([a-f0-9]{12})",raw)
    assert session, raw
    logs = raw
    final = raw
    for identifier in range(8,26):
        if "PASS | Lệnh đã hoàn tất" in final and "unified-terminal-done" in logs:
            break
        update = rpc(identifier,"tools/call",{"name":"terminal_read", "arguments":{
            "session_id":session.group(1),"wait_ms":200}})
        final=update["content"][0]["text"]
        logs+=final
        if final.startswith("PASS |") and "unified-terminal-done" in logs:
            break
    assert "unified-terminal-done" in logs
    assert final.startswith("PASS |"), final
    print("PASS | Terminal process completion")
    counter=itertools.count(100)
    def tool(name, **arguments):
        return rpc(next(counter),'tools/call',dict(name=name,arguments=arguments))
    def ok(name, **arguments):
        result=tool(name,**arguments)
        assert not result.get('isError'), result
        return result
    project_root=Path(argv[argv.index('--root')+1])
    with tempfile.TemporaryDirectory(prefix='udb-catalog-smoke-',dir=project_root) as fixture:
        prefix=Path(fixture).name
        source=prefix+'/source.py';copied=prefix+'/copied.py';moved=prefix+'/moved.py'
        text='# Nghiệm thu tiếng Việt 🚀\nprint(6)\n'
        (Path(fixture)/'source.py').write_text(text,encoding='utf-8')
        sha=hashlib.sha256((Path(fixture)/'source.py').read_bytes()).hexdigest()
        single=ok('read_file',project='demo',path=source)
        batch=ok('read_file',project='demo',paths=[source])
        assert 'Nghiệm thu' in json.dumps(single,ensure_ascii=False)
        assert 'Nghiệm thu' in json.dumps(batch,ensure_ascii=False)
        assert tool('read_files',project='demo',paths=[source]).get('isError')  # legacy alias is not public
        ok('propose_file_write',project='demo',path=prefix+'/new.py',content='# new\n',expected_sha256='NEW')
        assert (Path(fixture)/'new.py').read_text(encoding='utf-8')=='# new\n'
        ok('propose_file_write',project='demo',path=source,operation='replace',old_text='print(6)',new_text='print(7)',expected_sha256=sha)
        assert (Path(fixture)/'source.py').read_text(encoding='utf-8')==text.replace('print(6)','print(7)')
        sha=hashlib.sha256((Path(fixture)/'source.py').read_bytes()).hexdigest()
        refused=tool('propose_file_write',project='demo',path=source,operation='replace',old_text='print(7)',new_text='print(8)',expected_sha256='0'*64)
        assert refused.get('isError')
        ok('file_transfer',action='copy',source=source,destination=copied,expected_sha256=sha)
        ok('file_transfer',action='move',source=copied,destination=moved,expected_sha256=sha)
        assert not (Path(fixture)/'copied.py').exists()
        assert hashlib.sha256((Path(fixture)/'moved.py').read_bytes()).hexdigest()==sha
        assert tool('file_transfer',action='copy',source=source,destination=moved).get('isError')
        context=ok('file_search',path=prefix,pattern='*.py',text='Nghiệm thu',context=1)
        assert 'source.py:1' in json.dumps(context,ensure_ascii=False)
        waiting=ok('terminal_start',program=sys.executable,arguments=['-u','-c',"print(input(),flush=True)"],cwd=prefix,wait_ms=0)
        sid=re.search(r'Phiên: ([a-f0-9]{12})',waiting['content'][0]['text']).group(1)
        ok('terminal_interact',session_id=sid,text='Gộp terminal 🚀\n',send_only=True)
        logs=''
        for _ in range(10):
            row=tool('terminal_read',session_id=sid,wait_for_exit=True,wait_ms=1500)
            logs+=json.dumps(row,ensure_ascii=False)
            if 'Gộp terminal 🚀' in logs:break
        assert 'Gộp terminal 🚀' in logs
        ok('terminal_stop',session_id=sid)
    print('PASS | Single entrypoint routes to core/Companion backends; hashes, scope, permissions and Unicode preserved; fixture cleaned')
    print("PASS | UDB Unified end-to-end smoke test")
finally:
    p.stdin.close()
    try:
        p.wait(timeout=15)
    except subprocess.TimeoutExpired:
        p.terminate()
        p.wait(timeout=5)

