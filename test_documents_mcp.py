"""End-to-end three document tools through an installed MCP; own files only."""
import json
from pathlib import Path
import tempfile
import time
from test_uia_provider import Client,ROOT,object_data


def main():
    config=json.loads((ROOT/'mcp.json').read_text())['mcpServers']['universal-desktop-bridge']['args']
    project=Path(config[config.index('--root')+1])
    with tempfile.TemporaryDirectory(prefix='udb-office-mcp-',dir=project) as folder:
        prefix=Path(folder).relative_to(project).as_posix()
        client=Client()
        def tool(name,**args):return client.rpc('tools/call',dict(name=name,arguments=args))
        def job(name,**args):
            row=object_data(tool(name,**args));deadline=time.monotonic()+35
            while row['state']=='running' and time.monotonic()<deadline:
                time.sleep(.08)
                row=object_data(tool('file_operation',action='read',job_id=row['job_id']))
            assert row['state']=='completed',row
            return row['result']
        try:
            assert len(client.rpc('tools/list',{})['tools'])==60
            name=prefix+'/data.xlsx'
            created=job('spreadsheet',action='write',path=name,sheets={'Data':[['Tiếng Việt',3]]})
            assert created['committed']
            assert job('spreadsheet',action='read',path=name,range='A1:B1')['rows']==[['Tiếng Việt',3]]
            job('spreadsheet',action='edit_cells',path=name,cells={'B1':7},expected_sha256=created['sha256'])
            assert job('spreadsheet',action='read',path=name,range='B1:B1')['rows']==[[7]]
            assert tool('spreadsheet',action='write',path=name,sheets={'x':[[1]]}).get('isError')
            name=prefix+'/text.docx'
            created=job('word_document',action='write',path=name,text='Nghiệm thu Word')
            job('word_document',action='replace_text',path=name,old='Word',new='MCP',expected_sha256=created['sha256'])
            assert job('word_document',action='read',path=name)['paragraphs']==['Nghiệm thu MCP']
            name=prefix+'/text.pdf'
            created=job('pdf_document',action='write',path=name,text='Nghiệm thu tiếng Việt')
            assert 'Nghiệm thu tiếng Việt' in job('pdf_document',action='read',path=name)['pages'][0]['text']
            assert tool('pdf_document',action='read',path='../denied.pdf').get('isError')
            print('PASS | Real MCP XLSX write/read/hash edit/no-overwrite, DOCX replacement, Unicode PDF and path scope; fixtures removed')
        finally:client.close()


if __name__=='__main__':main()
