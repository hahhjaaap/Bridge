"""Single-entrypoint MCP regressions: routing, permissions, and side effects."""
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import time
import unittest

from udb_companion import Runtime, TOOLS
from udb_tool_catalog import HIDDEN_ALIASES, public_catalog
from udb_unified import UnifiedProxy


def core_tool(name, properties, required):
    return dict(name=name, description=name, inputSchema=dict(type='object', properties=properties, required=required))


CORE = [
    core_tool('read_file', dict(project={'type':'string'}, path={'type':'string'}), ['project','path']),
    core_tool('read_files', dict(project={'type':'string'}, paths={'type':'array','items':{'type':'string'}}), ['project','paths']),
    core_tool('propose_file_write', {k:{'type':'string'} for k in ('project','path','content','expected_sha256')}, ['project','path','content','expected_sha256']),
    core_tool('propose_text_edit', {**{k:{'type':'string'} for k in ('project','path','old_text','new_text','expected_sha256')}, 'occurrences':{'type':'integer'}}, ['project','path','old_text','new_text','expected_sha256']),
    core_tool('propose_sandbox_job', {}, []),
    core_tool('propose_media_download', {}, []),
    core_tool('computer_desktop', {}, []),
    core_tool('host_browser', {}, []),
]


class Backend:
    def __init__(self, tools, runtime=None):
        self.name='fixture';self.tools=tools;self.calls=[];self.enabled=True;self.runtime=runtime
    def request(self, method, params=None, **kwargs):
        if method=='tools/list':return {'result':{'tools':copy.deepcopy(self.tools)}}
        if method=='udb/close-sessions':return {'result':{}}
        name=params['name'];args=params.get('arguments',{})
        if name=='computer_status':return {'result':{'structuredContent':{'enabled':self.enabled}}}
        if name=='host_status':return {'result':{'structuredContent':{'resources':{'at':time.time(),'light_operations_ready':True,'admission_ready':True}}}}
        self.calls.append(copy.deepcopy(params))
        if self.runtime:return {'result':self.runtime.tool(name,args)}
        return {'result':{'content':[{'type':'text','text':'PASS | fixture'}],'structuredContent':dict(name=name,arguments=args)}}


class Consolidation(unittest.TestCase):
    def setUp(self):
        self.primary=Backend(CORE);self.companion=Backend(TOOLS)
        self.proxy=UnifiedProxy(self.primary,self.companion)
        self.entrypoints=self.proxy.tools_list()['tools']
        self.catalog=copy.deepcopy(self.proxy.tool_cache)

    def call(self, name, **args):
        return self.proxy.call_tool(dict(name='udb',arguments=dict(action='call',tool=name,arguments=args)))

    def entry(self, **args):
        return self.proxy.call_tool(dict(name='udb',arguments=args))

    def test_exactly_one_public_mcp_tool_and_internal_catalog_is_complete(self):
        self.assertEqual([tool['name'] for tool in self.entrypoints], ['udb'])
        original={t['name'] for t in CORE+TOOLS};public={t['name'] for t in self.catalog}
        self.assertEqual(public,(original-HIDDEN_ALIASES)|{'file_transfer'})
        self.assertEqual(len(self.proxy.tool_routes),len(original))
        for name in ('propose_sandbox_job','propose_media_download','computer_desktop','host_browser',
                     'file_operation','file_read_chunk','file_upload','search_start','system_process_terminate'):
            self.assertIn(name,public)

    def test_list_action_returns_schemas_without_exposing_many_mcp_tools(self):
        result=self.entry(action='list')
        self.assertEqual(result['structuredContent']['count'],len(self.catalog))
        self.assertEqual({t['name'] for t in result['structuredContent']['tools']},
                         {t['name'] for t in self.catalog})
        self.assertEqual([t['name'] for t in self.proxy.tools_list()['tools']],['udb'])

    def test_direct_tool_calls_are_rejected(self):
        with self.assertRaisesRegex(ValueError,"Only the unified 'udb' MCP tool is public"):
            self.proxy.call_tool(dict(name='read_file',arguments=dict(project='p',path='a')))
        self.assertFalse(self.primary.calls)
        self.assertFalse(self.companion.calls)

    def test_registered_project_batch_read_stays_on_primary(self):
        self.call('read_file',project='example',paths=['a.py','b.py'])
        self.assertEqual(self.primary.calls[-1],dict(name='read_files',arguments=dict(project='example',paths=['a.py','b.py'])))
        self.assertFalse(self.companion.calls)
        self.call('read_file',project='example',path='a.py')
        self.assertEqual(self.primary.calls[-1]['name'],'read_file')

    def test_source_edit_keeps_original_review_backend(self):
        self.call('propose_file_write',project='example',path='a.py',operation='replace',old_text='a',new_text='b',expected_sha256='old',occurrences=2)
        self.assertEqual(self.primary.calls[-1]['name'],'propose_text_edit')
        self.assertNotIn('operation',self.primary.calls[-1]['arguments'])
        self.assertEqual(self.primary.calls[-1]['arguments']['expected_sha256'],'old')
        self.assertFalse(self.companion.calls)

    def test_terminal_send_does_not_read_or_consume_output(self):
        self.call('terminal_interact',session_id='owned',text='hello\n',send_only=True)
        self.assertEqual(self.companion.calls[-1],dict(name='terminal_send',arguments=dict(session_id='owned',text='hello\n')))
        self.call('terminal_read',session_id='owned',offset=12,wait_for_exit=True,wait_ms=1000)
        self.assertEqual(self.companion.calls[-1],dict(name='terminal_wait',arguments=dict(session_id='owned',offset=12,timeout_ms=1000)))

    def test_ambiguous_calls_never_reach_backend(self):
        bad=[('read_file',dict(project='x',path='a',paths=['b'])),
             ('propose_file_write',dict(project='x',path='a',content='a',old_text='a',new_text='b',expected_sha256='x')),
             ('propose_file_write',dict(project='x',path='a',operation='replace',content='a',old_text='a',new_text='b',expected_sha256='x')),
             ('terminal_interact',dict(session_id='x',send_only=True,text='x',expected_prompt='>')),
             ('terminal_read',dict(session_id='x',wait_for_exit='true')),
             ('file_search',dict(context=1)),('file_transfer',dict(action='delete',source='a',destination='b'))]
        for name,args in bad:
            with self.subTest(name=name,args=args):
                with self.assertRaises(ValueError):self.call(name,**args)
        self.assertFalse(self.primary.calls);self.assertFalse(self.companion.calls)

    def test_legacy_aliases_are_not_public_operations(self):
        args={'read_files':dict(project='p',paths=['a']),
              'propose_text_edit':dict(project='p',path='a',old_text='a',new_text='b',expected_sha256='x'),
              'terminal_send':dict(session_id='s',text='x'), 'terminal_wait':dict(session_id='s'),
              'file_search_context':dict(text='x'),'file_copy':dict(source='a',destination='b'),
              'file_move':dict(source='a',destination='b')}
        for name,a in args.items():
            with self.subTest(name=name):
                with self.assertRaisesRegex(ValueError,'Unknown UDB operation'):
                    self.call(name,**a)
        self.assertFalse(self.primary.calls)
        self.assertFalse(self.companion.calls)

    def test_grant_denial_blocks_companion_operations(self):
        self.primary.enabled=False
        with self.assertRaises(RuntimeError):
            self.call('file_transfer',action='copy',source='a',destination='b')
        with self.assertRaisesRegex(ValueError,'Unknown UDB operation'):
            self.call('file_copy',source='a',destination='b')
        self.assertFalse(self.companion.calls)
        # Core registered-project reads keep their separate authorization path.
        self.call('read_file',project='p',paths=['a'])
        self.assertEqual(self.primary.calls[-1]['name'],'read_files')

    def test_discovery_rejects_cross_backend_consolidation(self):
        routes={t['name']:self.companion for t in CORE+TOOLS}
        routes['read_file']=self.primary
        with self.assertRaises(RuntimeError):public_catalog(CORE+TOOLS,routes)

    def test_catalog_and_arguments_are_not_mutated(self):
        original=copy.deepcopy(CORE+TOOLS)
        self.catalog[0]['inputSchema']['properties'].clear()
        self.assertTrue(self.proxy.tool_cache[0]['inputSchema']['properties'])
        self.assertEqual([t['name'] for t in self.proxy.tools_list()['tools']],['udb'])
        params=dict(name='udb',arguments=dict(action='call',tool='propose_file_write',
                    arguments=dict(project='p',path='a',operation='replace',old_text='a',
                                   new_text='b',expected_sha256='x')))
        before=copy.deepcopy(params);self.proxy.call_tool(params)
        self.assertEqual(params,before);self.assertEqual(CORE+TOOLS,original)

    def test_real_file_transfer_preserves_hash_scope_and_no_overwrite(self):
        with tempfile.TemporaryDirectory(prefix='udb-merged-files-') as root:
            rt=Runtime(root)
            self.companion.runtime=rt
            try:
                data='Nghiệm thu tiếng Việt 🚀\n';src=Path(root)/'source.txt';src.write_text(data,encoding='utf-8')
                sha=hashlib.sha256(src.read_bytes()).hexdigest()
                copied=self.call('file_transfer',action='copy',source='source.txt',destination='copy.txt',expected_sha256=sha)
                self.assertFalse(copied.get('isError'))
                self.assertEqual((Path(root)/'copy.txt').read_text(encoding='utf-8'),data)
                for a in [dict(source='source.txt',destination='copy.txt',expected_sha256=sha),
                          dict(source='source.txt',destination='wrong.txt',expected_sha256='0'*64),
                          dict(source='../outside.txt',destination='escape.txt')]:
                    self.assertTrue(self.call('file_transfer',action='copy',**a).get('isError'))
                self.assertEqual(sorted(x.name for x in Path(root).iterdir()),['copy.txt','source.txt'])
                context=self.call('file_search',text='tiếng Việt',pattern='*.txt',context=1)
                self.assertFalse(context.get('isError'));self.assertIn('source.txt:1',json.dumps(context,ensure_ascii=False))
            finally:rt.close()


if __name__=='__main__':unittest.main(verbosity=2)
