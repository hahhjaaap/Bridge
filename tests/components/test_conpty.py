"""Real Windows console mode, VT input, resize, stop and Job cleanup."""

import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
from udb_companion import Runtime

SCRIPT = r"""
import ctypes,msvcrt,time
from ctypes import wintypes as w
k=ctypes.WinDLL('kernel32');k.GetStdHandle.argtypes=[w.DWORD];k.GetStdHandle.restype=w.HANDLE
k.GetConsoleMode.argtypes=[w.HANDLE,ctypes.POINTER(w.DWORD)]
class Coord(ctypes.Structure):_fields_=[('X',ctypes.c_short),('Y',ctypes.c_short)]
class Rect(ctypes.Structure):_fields_=[(n,ctypes.c_short) for n in ['L','T','R','B']]
class Info(ctypes.Structure):_fields_=[('Size',Coord),('Cursor',Coord),('Attributes',w.WORD),('Window',Rect),('Maximum',Coord)]
k.GetConsoleScreenBufferInfo.argtypes=[w.HANDLE,ctypes.POINTER(Info)]
h=k.GetStdHandle(-11&0xffffffff);mode=w.DWORD();print('NATIVE_CONSOLE='+str(bool(k.GetConsoleMode(h,ctypes.byref(mode)))),flush=True)
i=Info();k.GetConsoleScreenBufferInfo(h,ctypes.byref(i));print('INITIAL='+str(i.Size.X)+','+str(i.Size.Y),flush=True)
a=msvcrt.getwch()
if a in ('\x00','\xe0'):a+=msvcrt.getwch()
print('KEY_CODES='+str([ord(c) for c in a]),flush=True)
k.GetConsoleScreenBufferInfo(h,ctypes.byref(i));print('RESIZED='+str(i.Size.X)+','+str(i.Size.Y),flush=True)
print('UNICODE=Tiếng Việt 🚀',flush=True)
"""


@unittest.skipUnless(os.name == "nt", "Windows ConPTY required")
class Native(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.rt = Runtime(self.temp.name, True)

    def tearDown(self):
        self.rt.close()
        self.temp.cleanup()

    def output(self, session, until):
        deadline = time.monotonic() + 12
        raw = ""
        while time.monotonic() < deadline:
            raw = self.rt.sessions[session].read(0, 100)["output"]
            if until in raw:
                return raw
            time.sleep(0.05)
        self.fail("Missing " + until + " in " + repr(raw))

    def test_console_vt_arrow_unicode_and_resize(self):
        row = self.rt.start(
            sys.executable,
            ["-u", "-c", SCRIPT],
            mode="conpty",
            columns=90,
            rows=25,
            wait_ms=0,
        )
        sid = row["session_id"]
        self.assertEqual(row["terminal_mode"], "conpty")
        self.assertIn("NATIVE_CONSOLE=True", self.output(sid, "INITIAL=90,25"))
        self.rt.sessions[sid].process.resize(132, 40)
        self.rt.sessions[sid].send("\x1b[A")
        raw = self.output(sid, "UNICODE=Tiếng Việt 🚀")
        self.assertIn("RESIZED=132,40", raw)
        self.assertIn("KEY_CODES=[224, 72]", raw)
        for _ in range(50):
            if self.rt.sessions[sid].read(0, 0)["state"] == "PASS":
                break
            time.sleep(0.05)
        self.assertEqual(self.rt.sessions[sid].read(0, 0)["state"], "PASS")

    def test_stop_and_invalid_geometry(self):
        with self.assertRaises(ValueError):
            self.rt.start(sys.executable, ["-c", "pass"], mode="conpty", columns=0)
        row = self.rt.start(
            sys.executable,
            ["-c", "import time;time.sleep(60)"],
            mode="conpty",
            wait_ms=0,
        )
        s = self.rt.sessions[row["session_id"]]
        s.stop()
        self.assertIsNotNone(s.process.poll())
        self.assertIsNone(s.process.hpc)

    def test_ctrl_c_reaches_console_application(self):
        script = "import time\nprint('WAIT_CTRL_C',flush=True)\ntry:time.sleep(90)\nexcept KeyboardInterrupt:print('CTRL_C_RECEIVED',flush=True)"
        row = self.rt.start(
            sys.executable, ["-u", "-c", script], mode="conpty", wait_ms=0
        )
        sid = row["session_id"]
        self.output(sid, "WAIT_CTRL_C")
        self.rt.sessions[sid].send("\x03")
        self.output(sid, "CTRL_C_RECEIVED")

    def test_abrupt_owner_exit_kills_conpty_client(self):
        script = "import sys,time;sys.path.insert(0,sys.argv[1]);from udb_companion import Runtime;r=Runtime(sys.argv[2],True);v=r.start(sys.executable,['-c','import time;time.sleep(90)'],mode='conpty',wait_ms=0);print(r.sessions[v['session_id']].process.pid,flush=True);time.sleep(90)"
        p = subprocess.Popen(
            [
                sys.executable,
                "-u",
                "-c",
                script,
                str(Path(__file__).resolve().parents[2] / "src"),
                self.temp.name,
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        import ctypes
        from ctypes import wintypes as w

        k = ctypes.WinDLL("kernel32")
        k.OpenProcess.argtypes = [w.DWORD, w.BOOL, w.DWORD]
        k.OpenProcess.restype = w.HANDLE
        k.WaitForSingleObject.argtypes = [w.HANDLE, w.DWORD]
        k.CloseHandle.argtypes = [w.HANDLE]
        h = None
        try:
            pid = int(p.stdout.readline())
            h = k.OpenProcess(0x100000, False, pid)
            self.assertTrue(h)
            p.kill()
            p.wait(timeout=5)
            self.assertEqual(k.WaitForSingleObject(h, 5000), 0)
        finally:
            if p.poll() is None:
                p.kill()
                p.wait(timeout=5)
            if h:
                k.CloseHandle(h)
            p.stdout.close()
            p.stderr.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
