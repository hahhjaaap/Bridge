"""Guest-only demo app-local input bridge acceptance and isolation regression."""
import json
from pathlib import Path
import pytest
from udb import desktop_guest as g

def test_only_exact_guest_pad_is_accepted(tmp_path,monkeypatch):
    d=g.GuestDesktop.__new__(g.GuestDesktop)
    d.session_id="a"*32
    d.u=type("NoPad",(),{"FindWindowW":lambda self,cls,title: 0})()
    monkeypatch.setattr(g,"OUTPUT",tmp_path)
    with pytest.raises(RuntimeError,match="not found"):
        d.pad_inprocess("type",text="TEST")
    assert not list(tmp_path.iterdir())

def test_rejects_any_non_pad_operation(tmp_path,monkeypatch):
    d=g.GuestDesktop.__new__(g.GuestDesktop)
    d.session_id="b"*32
    monkeypatch.setattr(g,"OUTPUT",tmp_path)
    with pytest.raises(ValueError):
        d.pad_inprocess("run_shell",text="cmd")

def test_pad_bridge_requires_matching_session_and_ack(tmp_path,monkeypatch):
    d=g.GuestDesktop.__new__(g.GuestDesktop)
    d.session_id="a"*32
    d.u=type("Pad",(),{"FindWindowW":lambda self,cls,title: 42 if title=="UDB Sandbox Pad - guest only" else 0})()
    monkeypatch.setattr(g,"OUTPUT",tmp_path)
    def respond(path,payload):
        assert payload["mode"]=="type" and payload["text"]=="Hello from AI"
        assert payload["session_id"]=="a"*32
        response={"id":payload["id"],"session_id":payload["session_id"],"ok":True,"result":"PAD_TEXT_SET"}
        Path(tmp_path/("padack-"+payload["id"]+".json")).write_text(json.dumps(response),encoding="utf8")
    monkeypatch.setattr(g,"write_json",respond)
    assert d.pad_inprocess("type",text="Hello from AI")=="PAD_TEXT_SET"
    assert not list(tmp_path.iterdir())

def test_guest_automation_script_restricts_to_demo():
    source=g.DEMO
    assert "UDB Sandbox Pad - guest only" in source
    assert "$box.Text=[string]$request.text" in source
    assert "$button.PerformClick()" in source
    assert "RectangleToScreen" in source
    assert "Unsupported Pad operation" in source
    assert "$request.session_id -ne $padSession" in source
    assert "powershell.exe" not in source.lower()
    assert "SendInput" not in source

def test_legacy_guest_only_boundary_remains():
    import inspect
    src=inspect.getsource(g.GuestDesktop.__init__)
    assert src.index("assert_guest(config)") < src.index("ctypes.WinDLL('user32'")

def test_demo_automation_avoids_guest_sendinput_and_setcursorpos(tmp_path,monkeypatch):
    d=g.GuestDesktop.__new__(g.GuestDesktop)
    d.session_id="a"*32
    d.u=type("Pad",(),{"FindWindowW":lambda self,cls,title:42 if title=="UDB Sandbox Pad - guest only" else 0})()
    def blocked(*args,**kw):raise AssertionError("OS pointer/keyboard injection must not be called for Pad")
    d.move=blocked
    d.send=blocked
    d.ai_cursor=lambda action:None
    actions=[]
    d.pad_inprocess=lambda mode,**kw:actions.append((mode,kw)) or "PAD_ACCEPTED"
    monkeypatch.setattr(g,"OUTPUT",tmp_path)
    d.type_text("HELLO AI")
    d.click(500,463,"left",1)
    assert actions==[("type",{"text":"HELLO AI"}),("click",{"x":500,"y":463})]
    cursor=json.loads((tmp_path/"cursor.json").read_text("utf8"))
    assert cursor=={"x":500,"y":463,"action":"click"}
