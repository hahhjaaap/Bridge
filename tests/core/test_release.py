"""Release regressions: missing workers, leaked state and mismatched EXE/source."""
import json
import zipfile
from pathlib import Path
import pytest
from udb import __version__
from udb.release import ROOT_FILES,WORKERS,PREFIX,create_archive,write_build_info,verify_archive

@pytest.fixture
def release(tmp_path):
    base=tmp_path/'source';base.mkdir();(base/'udb').mkdir()
    for name in ROOT_FILES:
        (base/name).write_text('synthetic fixture\n',encoding='utf-8')
    (base/'plugin.json').write_text(json.dumps({'name':'universal-desktop-bridge','version':__version__,
        'extensions':{'com.openai':{'interface':{'shortDescription':'Controlled Windows projects'}}}}))
    (base/'mcp.json').write_text('{"mcpServers":{"synthetic":{}}}')
    for name in WORKERS:
        (base/'udb'/name).write_text('# synthetic worker\n')
    (base/'udb'/'__init__.py').write_text('# synthetic module\n')
    exe=tmp_path/'candidate.exe';exe.write_bytes(b'SYNTHETIC-NOT-EXECUTABLE')
    return base,exe,tmp_path/'candidate.zip',write_build_info(base,'fixture-revision')

def test_package_preserves_workers_and_excludes_private_data(release):
    base,exe,out,info=release
    for directory in ('state','projects','.git','.venv','tools'):
        (base/directory).mkdir();(base/directory/'DO-NOT-EXPORT.txt').write_text('private-sentinel')
    result=create_archive(base,exe,out,info)
    assert verify_archive(out,result['sha256'])['verified']
    with zipfile.ZipFile(out) as z:
        assert {PREFIX+'udb/'+w for w in WORKERS} <= set(z.namelist())
        assert PREFIX+'THIRD_PARTY.md' in z.namelist()
        assert not any('DO-NOT-EXPORT' in n for n in z.namelist())
    assert out.with_suffix('.zip.sha256').is_file()

def test_modified_source_cannot_be_packaged_with_stale_binary(release):
    base,exe,out,info=release
    (base/'udb'/'desktop_guest.py').write_text('changed after build\n')
    with pytest.raises(ValueError,match='provenance'):
        create_archive(base,exe,out,info)
    assert not out.exists()

def test_missing_worker_fails_before_release(release):
    base,exe,out,info=release
    (base/'udb'/'desktop_accessibility.ps1').unlink()
    with pytest.raises(ValueError,match='worker'):
        create_archive(base,exe,out,info)

def test_archive_is_deterministic_and_old_release_cannot_be_overwritten(release):
    base,exe,out,info=release
    first=create_archive(base,exe,out,info)
    second=create_archive(base,exe,out.with_name('second.zip'),info)
    assert first['sha256']==second['sha256']
    with pytest.raises(ValueError,match='overwrite'):
        create_archive(base,exe,out,info)

def test_tampered_file_detected_without_running_binary(release):
    base,exe,out,info=release
    create_archive(base,exe,out,info)
    with zipfile.ZipFile(out) as z:
        contents={n:z.read(n) for n in z.namelist()}
    contents[PREFIX+'bridge.py']=b'tampered'
    with zipfile.ZipFile(out,'w') as z:
        for n,b in contents.items():z.writestr(n,b)
    with pytest.raises(ValueError,match='checksum'):
        verify_archive(out)

@pytest.mark.parametrize('entry',['../outside.txt',PREFIX+'../escape.txt',PREFIX+'state/token',PREFIX+'unexpected.py',PREFIX+'udb/desktop_guest.py'])
@pytest.mark.filterwarnings('ignore:Duplicate name:UserWarning')
def test_unsafe_unlisted_or_duplicate_entries_rejected(release,entry):
    base,exe,out,info=release
    create_archive(base,exe,out,info)
    with zipfile.ZipFile(out,'a') as z:
        z.writestr(entry,b'bad')
    with pytest.raises(ValueError):
        verify_archive(out)

def test_wrong_archive_hash_rejected(release):
    base,exe,out,info=release
    create_archive(base,exe,out,info)
    with pytest.raises(ValueError,match='SHA256'):
        verify_archive(out,'0'*64)
