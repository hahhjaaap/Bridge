"""Offline release inventory. SHA256 detects corruption, not publisher identity."""
from __future__ import annotations
import hashlib
import json
import os
import subprocess
import tempfile
import zipfile
from pathlib import Path
from . import __version__
from .release_policy import allowed_release_member, assert_safe_payload

ROOT_FILES = ('plugin.json','mcp.json','bridge.py','setup_local.py','requirements.txt',
    'requirements-build.txt','requirements-gui.txt','Install.ps1','Install-Tunnel.ps1',
    'Enable-Sandbox.ps1','Build-Release.ps1','Build-Release.py','Make-Package.py','Verify-Package.py',
    'UniversalDesktopBridge.spec','README.md','README_VI.md','VERIFICATION.md',
    'THIRD_PARTY.md','LICENSE','CHANGELOG.md','.gitignore','Launch-UDB.ps1','Rollback.ps1','Prepare-Vendor.py','pytest.ini',
    'Install-UDB-OneCommand.ps1')
WORKERS = ('desktop_guest.py','desktop_uia.ps1','desktop_overlay.ps1','desktop_accessibility.ps1')
PREFIX = 'universal-desktop-bridge/'

def sha256(data):
    return hashlib.sha256(data).hexdigest()

def regular_file(path):
    path = Path(path)
    for item in (path, *path.parents):
        if item.is_symlink() or getattr(item.lstat(),'st_file_attributes',0) & 0x400:
            raise ValueError('Linked release component: '+path.name)
    if not path.is_file():
        raise ValueError('Missing release component: '+path.name)
    return path

def runtime_manifest(base):
    base = Path(base)
    names = ['bridge.py','UniversalDesktopBridge.spec','requirements.txt','requirements-build.txt','requirements-gui.txt']
    names += [p.relative_to(base).as_posix() for p in (base/'udb').iterdir() if p.suffix in ('.py','.ps1')]
    if not {'udb/'+w for w in WORKERS} <= set(names):
        raise ValueError('Missing desktop worker')
    return {n:sha256(regular_file(base/n).read_bytes()) for n in sorted(names)}

def write_build_info(base, revision):
    base = Path(base)
    info = {'version':__version__, 'kind':'frozen-candidate', 'release_provenance':True,
            'source_revision':revision, 'source_files':runtime_manifest(base), 'code_signed':False}
    (base/'udb'/'build_info.json').write_text(json.dumps(info,sort_keys=True,indent=2)+'\n',encoding='utf-8')
    return info

def package_inputs(base, exe):
    base = Path(base)
    files = {n:regular_file(base/n) for n in ROOT_FILES}
    files['UniversalDesktopBridge.exe'] = regular_file(exe)
    for folder, extensions in [('udb',('.py','.ps1')),('tests',('.py','.ps1')),('docs',('.md','.json','.png')),
                               ('command',('.py','.ps1','.json','.md'))]:
        if (base/folder).is_dir():
            for p in sorted((base/folder).iterdir()):
                if p.suffix in extensions:
                    files[p.relative_to(base).as_posix()] = regular_file(p)
    files['udb/build_info.json'] = regular_file(base/'udb'/'build_info.json')
    for worker in WORKERS:
        if 'udb/'+worker not in files:
            raise ValueError('Missing desktop worker')
    vendor=base/'vendor'
    if vendor.is_dir():
        for p in sorted(vendor.rglob('*')):
            if p.is_file():
                name = p.relative_to(base).as_posix()
                if not allowed_release_member(name, ROOT_FILES):
                    raise ValueError('Disallowed vendor release component')
                files[name] = regular_file(p)
    site = base/'.venv'/'Lib'/'site-packages'
    for metadata in sorted(site.glob('*.dist-info')):
        for p in sorted(metadata.rglob('*')):
            if p.is_file() and any(w in p.name.lower() for w in ('license','copying','notice')):
                files['THIRD_PARTY_NOTICES/'+metadata.name+'/'+p.relative_to(metadata).as_posix()] = regular_file(p)
    return files

def create_archive(base, exe, output, binary_info, notices_site=None):
    base, output = Path(base), Path(output)
    if output.exists():
        raise ValueError('Refusing to overwrite an existing release')
    if binary_info.get('version') != __version__ or binary_info.get('source_files') != runtime_manifest(base):
        raise ValueError('Candidate binary and source provenance mismatch')
    plugin = json.loads((base/'plugin.json').read_text('utf-8'))
    if plugin['name'] != 'universal-desktop-bridge' or plugin['version'] != __version__:
        raise ValueError('Plugin identity/version mismatch')
    if len(plugin['extensions']['com.openai']['interface']['shortDescription']) > 30:
        raise ValueError('Plugin shortDescription is too long')
    if not json.loads((base/'mcp.json').read_text('utf-8'))['mcpServers']:
        raise ValueError('Missing MCP server configuration')
    inputs = package_inputs(base, exe)
    if notices_site:
        for metadata in sorted(Path(notices_site).glob('*.dist-info')):
            for p in sorted(metadata.rglob('*')):
                if p.is_file() and any(w in p.name.lower() for w in ('license','copying','notice')):
                    inputs['THIRD_PARTY_NOTICES/'+metadata.name+'/'+p.relative_to(metadata).as_posix()] = regular_file(p)
    data = {name:p.read_bytes() for name,p in inputs.items()}
    for name, content in data.items():
        if not allowed_release_member(name, ROOT_FILES):
            raise ValueError('Disallowed release component')
        assert_safe_payload(content)
    if json.loads(data['udb/build_info.json']) != binary_info:
        raise ValueError('Embedded build provenance mismatch')
    inventory = {'version':__version__, 'code_signed':False, 'integrity_only':True, 'files':{
        n:{'sha256':sha256(b),'size_bytes':len(b)} for n,b in sorted(data.items())}}
    data['RELEASE-MANIFEST.json'] = json.dumps(inventory,sort_keys=True,indent=2).encode()
    assert_safe_payload(data['RELEASE-MANIFEST.json'])
    output.parent.mkdir(parents=True,exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='udb-release-',suffix='.zip',dir=output.parent)
    os.close(fd)
    try:
        with zipfile.ZipFile(temporary,'w') as archive:
            for name,content in sorted(data.items()):
                entry = zipfile.ZipInfo(PREFIX+name,date_time=(1980,1,1,0,0,0))
                entry.compress_type = zipfile.ZIP_DEFLATED
                entry.create_system = 3
                entry.external_attr = 0o100644 << 16
                archive.writestr(entry,content)
        verify_archive(temporary)
        with output.open('xb') as dest, open(temporary,'rb') as source:
            import shutil
            shutil.copyfileobj(source,dest)
    finally:
        Path(temporary).unlink(missing_ok=True)
    result = {'path':str(output),'size_bytes':output.stat().st_size,'sha256':sha256(output.read_bytes()),
              'files':len(inventory['files']),'version':__version__,'code_signed':False}
    output.with_suffix(output.suffix+'.sha256').write_text(result['sha256']+'  '+output.name+'\n',encoding='ascii')
    return result

def verify_archive(path, expected_sha256=None):
    path = Path(path)
    if expected_sha256 and sha256(path.read_bytes()) != expected_sha256.lower():
        raise ValueError('Archive SHA256 mismatch')
    with zipfile.ZipFile(path) as archive:
        entries = archive.infolist()
        names = [e.filename for e in entries]
        if len(names) != len(set(names)) or len({n.casefold() for n in names}) != len(names):
            raise ValueError('Duplicate archive entry')
        if len(names) > 5000 or sum(e.file_size for e in entries) > 1024**3:
            raise ValueError('Archive exceeds release limits')
        for entry in entries:
            name = entry.filename
            parts = name.split('/')
            if (not name.startswith(PREFIX) or any(x in ('','.', '..') for x in parts) or '\\' in name or ':' in name
                    or entry.is_dir() or (entry.external_attr >> 16) & 0o170000 == 0o120000
                    or not allowed_release_member(name[len(PREFIX):], ROOT_FILES)):
                raise ValueError('Unsafe release entry')
        manifest_payload = archive.read(PREFIX+'RELEASE-MANIFEST.json')
        assert_safe_payload(manifest_payload)
        inventory = json.loads(manifest_payload)
        expected = {PREFIX+n for n in inventory['files']} | {PREFIX+'RELEASE-MANIFEST.json'}
        if set(names) != expected:
            raise ValueError('Release file set mismatch')
        required = {'UniversalDesktopBridge.exe','plugin.json','mcp.json','THIRD_PARTY.md',
                    'UniversalDesktopBridge.spec','udb/build_info.json'} | {'udb/'+w for w in WORKERS}
        if not required <= set(inventory['files']):
            raise ValueError('Required release component missing')
        for name, record in inventory['files'].items():
            b = archive.read(PREFIX+name)
            assert_safe_payload(b)
            if len(b) != record['size_bytes'] or sha256(b) != record['sha256']:
                raise ValueError('Release checksum mismatch: '+name)
    return {'verified':True,'version':inventory['version'],'files':len(inventory['files']),
            'publisher_authenticated':False}

def binary_build_info(exe):
    result = subprocess.run([str(Path(exe).resolve()),'--build-info'],check=True,
                            capture_output=True,text=True,timeout=30)
    return json.loads(result.stdout)
