"""Crash fixtures and reload durable records without replaying unknown writes."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parent))
from udb_companion import Runtime
from udb_work_journal import Journal,WorkRecovery
from udb_background import version
MODULES=str(Path(__file__).resolve().parent)

class Recovery(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='udb-work-');self.root=Path(self.temp.name)
    def tearDown(self):self.temp.cleanup()
    def crashed(self,descriptor=None,kind='file_copy',side_effect=False):
        script="""import sys,time,os,json
from pathlib import Path
sys.path.insert(0,sys.argv[1])
from udb_companion import Runtime
rt=Runtime(sys.argv[2]);cfg=json.loads(sys.argv[3])
def work(task):
 if cfg['effect']:(Path(sys.argv[2])/'effect').write_text('once',encoding='utf-8')
 time.sleep(90)
row=rt.features.background.new(cfg['kind'],work,recovery=cfg['descriptor'])
print(row['job_id'],flush=True)
time.sleep(.1)
os._exit(12)
"""
        proc=subprocess.run([sys.executable,'-u','-c',script,MODULES,str(self.root),json.dumps(dict(descriptor=descriptor,kind=kind,effect=side_effect))],capture_output=True,text=True,timeout=15)
        self.assertEqual(proc.returncode,12,proc.stderr)
        return proc.stdout.strip()
    def wait(self,rt,row):
        deadline=time.monotonic()+10
        while row['state']=='running' and time.monotonic()<deadline:
            time.sleep(.02);row=rt.features.background.task(row['job_id'])
        self.assertEqual(row['state'],'completed',row);return row
    def test_copy_recovers_after_actual_backend_exit_and_preserves_idempotency(self):
        src=self.root/'source.txt';src.write_text('Tiếng Việt',encoding='utf-8')
        desc=dict(tool='file_operation',arguments=dict(action='copy',source='source.txt',destination='dest.txt'),source_version=version(src))
        jid=self.crashed(desc);rt=Runtime(str(self.root));recovery=WorkRecovery(rt)
        try:
            self.assertEqual(recovery.call('inspect','job',jid)['state'],'interrupted')
            row=self.wait(rt,recovery.call('resume','job',jid))
            self.assertEqual((self.root/'dest.txt').read_text(encoding='utf-8'),'Tiếng Việt')
            second=recovery.call('resume','job',jid)
            self.assertTrue(second['already_resumed']);self.assertEqual(second['job_id'],row['job_id'])
        finally:rt.close()
    def test_changed_copy_source_refuses_resume(self):
        src=self.root/'source.txt';src.write_text('before',encoding='utf-8')
        jid=self.crashed(dict(tool='file_operation',arguments=dict(action='copy',source='source.txt',destination='dest.txt'),source_version=version(src)))
        src.write_text('changed',encoding='utf-8');rt=Runtime(str(self.root))
        try:
            with self.assertRaisesRegex(ValueError,'Source changed'):WorkRecovery(rt).call('resume','job',jid)
            self.assertFalse((self.root/'dest.txt').exists())
        finally:rt.close()
    def test_uncertain_mutation_is_never_replayed(self):
        jid=self.crashed(None,'document_write',True);rt=Runtime(str(self.root))
        try:
            with self.assertRaisesRegex(ValueError,'unknown'):WorkRecovery(rt).call('resume','job',jid)
            self.assertEqual((self.root/'effect').read_text(encoding='utf-8'),'once')
        finally:rt.close()
    def test_search_can_restart_but_old_results_are_retained(self):
        (self.root/'a.txt').write_text('needle',encoding='utf-8')
        jid=self.crashed(dict(tool='search_start',arguments=dict(path='.',text='needle')),'search');rt=Runtime(str(self.root))
        try:
            row=self.wait(rt,WorkRecovery(rt).call('resume','job',jid));self.assertEqual(row['result']['matches'],1)
        finally:rt.close()
    def test_terminal_transcript_survives_runtime_close(self):
        rt=Runtime(str(self.root),True)
        row=rt.start(sys.executable,['-u','-c',"print('saved-transcript')"],wait_ms=200);sid=row['session_id']
        for _ in range(30):
            if rt.sessions[sid].done:break
            time.sleep(.05)
        rt.close();next_rt=Runtime(str(self.root))
        try:
            row=WorkRecovery(next_rt).call('inspect','terminal',sid)
            import base64
            self.assertIn(b'saved-transcript',base64.b64decode(row['output_base64']))
            with self.assertRaisesRegex(ValueError,'dead process'):WorkRecovery(next_rt).call('resume','terminal',sid)
        finally:next_rt.close()
    def test_journal_scope_and_cross_instance_lease(self):
        journal=Journal(self.root)
        with self.assertRaises(ValueError):journal.load('../outside')
        lease=journal.lease('job-'+'a'*32)
        try:
            with self.assertRaises(RuntimeError):Journal(self.root).lease('job-'+'a'*32)
        finally:lease.close()
        rt=Runtime(str(self.root))
        try:
            with self.assertRaises((ValueError,PermissionError)):rt.files.path('.udb-work-state')
        finally:rt.close()

if __name__=='__main__':unittest.main(verbosity=2)
