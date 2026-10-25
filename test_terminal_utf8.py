"""Regression for byte paging across UTF-8 code points, not only aligned logs."""
import base64
import collections
import threading
from types import SimpleNamespace
import unittest
from udb_companion import TerminalSession


def reader(data,done=True):
    session=TerminalSession.__new__(TerminalSession)
    session.id='utf8-fixture';session.lock=threading.RLock();session.arrival=threading.Event()
    session.chunks=collections.deque([(0,data)]);session.position=len(data);session.read_cursor=0
    session.mode='pipe';session.done=done;session.reason='';session.process=SimpleNamespace(poll=lambda:0 if done else None)
    return session


class UTF8Paging(unittest.TestCase):
    def test_unaligned_long_text_preserves_characters_and_bytes(self):
        for prefix in range(9):
            with self.subTest(prefix=prefix):
                original='X'*prefix+'Dữ liệu tiếng Việt 🚀 日本語\n'*1600
                data=original.encode('utf-8');session=reader(data);texts=[];raw=[];offset=0
                for _ in range(30):
                    row=session.read(offset,0);texts.append(row['output']);raw.append(base64.b64decode(row['output_base64']))
                    self.assertGreater(row['next_offset'],offset);offset=row['next_offset']
                    if not row['has_more']:break
                self.assertEqual(''.join(texts),original)
                self.assertEqual(b''.join(raw),data);self.assertEqual(offset,len(data))

    def test_live_partial_codepoint_waits_for_remaining_bytes(self):
        data='🚀'.encode('utf-8');session=reader(data[:2],False)
        row=session.read(None,0)
        self.assertEqual(row['output'],'');self.assertEqual(row['next_offset'],0);self.assertTrue(row['has_more'])
        session.chunks=collections.deque([(0,data)]);session.position=len(data);session.done=True;session.process=SimpleNamespace(poll=lambda:0)
        row=session.read(None,0)
        self.assertEqual(row['output'],'🚀');self.assertEqual(row['next_offset'],4)

    def test_invalid_prefix_does_not_consume_a_valid_partial_suffix(self):
        emoji='🚀'.encode('utf-8');session=reader(b'\xff'+emoji[:2],False)
        row=session.read(None,0)
        self.assertEqual(row['next_offset'],1);self.assertEqual(base64.b64decode(row['output_base64']),b'\xff')
        full=b'\xff'+emoji;session.chunks=collections.deque([(0,full)]);session.position=len(full);session.done=True;session.process=SimpleNamespace(poll=lambda:0)
        row=session.read(None,0)
        self.assertEqual(row['output'],'🚀');self.assertEqual(row['next_offset'],len(full))

    def test_genuinely_invalid_utf8_is_still_available_as_exact_bytes(self):
        data=b'raw:\xff\x80';session=reader(data)
        row=session.read(None,0)
        self.assertEqual(base64.b64decode(row['output_base64']),data)
        self.assertEqual(row['next_offset'],len(data));self.assertFalse(row['has_more'])

if __name__=='__main__':unittest.main(verbosity=2)
