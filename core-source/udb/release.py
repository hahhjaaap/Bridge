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

ROOT_FILES = ('bridge.py','setup_local.py','requirements.txt',
    'requirements-build.txt','requirements-gui.txt','Install.ps1','Install-Tunnel.ps1',
    'Enable-Sandbox.ps1','Build-Release.ps1','Build-Release.py','Make-Package.py','Verify-Package.py',
    'UniversalDesktopBridge.spec','README.md','README_VI.md','VERIFICATION.md',
    'THIRD_PARTY.md','LICENSE','CHANGELOG.md','.gitignore','Launch-UDB.ps1','Rollback.ps1','Prepare-Vendor.py','pytest.ini',
    'Install-UDB-OneCommand.ps1')
WORKERS = ('desktop_guest.py','desktop_uia.ps1','desktop_overlay.ps1','desktop_accessibility.ps1')
PREFIX = 'universal-desktop-bridge/'
REQUIRED_VENDOR_FILES = (
    'vendor/python-runtime/python.exe',
    'vendor/python-runtime/python3.dll',
    'vendor/python-runtime/python311.dll',
    'vendor/python-runtime/vcruntime140.dll',
    'vendor/python-runtime/vcruntime140_1.dll',
    'vendor/python-runtime/LICENSE.txt',
    'vendor/python-runtime/python311._pth',
    'vendor/python-runtime/DLLs/_ssl.pyd',
    'vendor/python-runtime/Lib/encodings/__init__.py',
    'vendor/python-runtime/Lib/os.py',
    'vendor/tunnel-client.exe',
    'vendor/tunnel-LICENSES.txt',
    'vendor/DEPENDENCIES.json',
)

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

def validate_vendor_payload(data):
    """Fail closed unless the offline runtime and its dependency inventory agree."""
    vendor_names = {name for name in data if name.startswith('vendor/')}
    if not vendor_names:
        raise ValueError('Offline vendor runtime missing; run Prepare-Vendor.py before packaging')
    missing = set(REQUIRED_VENDOR_FILES) - vendor_names
    if missing:
        raise ValueError('Offline vendor runtime incomplete: ' + ', '.join(sorted(missing)))
    if not any(name.startswith('vendor/python-runtime/DLLs/') and Path(name).suffix.lower() in ('.dll', '.pyd')
               for name in vendor_names):
        raise ValueError('Offline Python runtime has no native DLL/PYD files')
    if not any(name.startswith('vendor/python-runtime/Lib/') and name.endswith('.py') for name in vendor_names):
        raise ValueError('Offline Python runtime has no unpacked standard library')
    if not data['vendor/tunnel-LICENSES.txt'].strip():
        raise ValueError('Tunnel client license notice is empty')
    try:
        pth = data['vendor/python-runtime/python311._pth'].decode('ascii').splitlines()
        dependencies = json.loads(data['vendor/DEPENDENCIES.json'].decode('utf-8'))
    except (UnicodeError, ValueError, TypeError):
        raise ValueError('Invalid offline runtime configuration or dependency manifest') from None
    if pth != ['Lib', '.', 'DLLs']:
        raise ValueError('Portable Python path file must use the isolated Lib/DLLs layout')
    if not isinstance(dependencies, dict) or not str(dependencies.get('python_version', '')).startswith('Python 3.11.'):
        raise ValueError('Offline runtime must be Python 3.11')
    records = dependencies.get('files')
    actual = vendor_names - {'vendor/DEPENDENCIES.json'}
    if not isinstance(records, dict) or set(records) != actual:
        raise ValueError('Offline runtime dependency inventory mismatch')
    for name, record in records.items():
        if not isinstance(record, dict) or record.get('sha256') != sha256(data[name]) or record.get('bytes') != len(data[name]):
            raise ValueError('Offline runtime dependency checksum mismatch: ' + name)
    return {'files': len(vendor_names), 'python_version': dependencies['python_version']}

def validate_vendor_directory(base):
    """Validate staged runtime files before a potentially long build."""
    base = Path(base)
    vendor = base / 'vendor'
    if not vendor.is_dir() or vendor.is_symlink():
        raise ValueError('Offline vendor directory is not staged')
    data = {}
    for path in [vendor, *vendor.rglob('*')]:
        if path.is_symlink() or getattr(path.lstat(), 'st_file_attributes', 0) & 0x400:
            raise ValueError('Linked vendor component refused')
        if path.is_file():
            regular_file(path)
            data[path.relative_to(base).as_posix()] = path.read_bytes()
    return validate_vendor_payload(data)

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
    validate_vendor_payload(data)
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
        if not isinstance(inventory, dict) or inventory.get('version') != __version__:
            raise ValueError('Release version mismatch')
        expected = {PREFIX+n for n in inventory['files']} | {PREFIX+'RELEASE-MANIFEST.json'}
        if set(names) != expected:
            raise ValueError('Release file set mismatch')
        required = set(ROOT_FILES) | {'UniversalDesktopBridge.exe', 'udb/build_info.json'} | {'udb/'+w for w in WORKERS}
        if not required <= set(inventory['files']):
            raise ValueError('Required release component missing')
        payloads = {}
        for name, record in inventory['files'].items():
            b = archive.read(PREFIX+name)
            payloads[name] = b
            assert_safe_payload(b)
            if len(b) != record['size_bytes'] or sha256(b) != record['sha256']:
                raise ValueError('Release checksum mismatch: '+name)
        validate_vendor_payload(payloads)
        try:
            build_info = json.loads(payloads['udb/build_info.json'])
        except (ValueError, TypeError):
            raise ValueError('Invalid embedded build provenance') from None
        if not isinstance(build_info, dict) or build_info.get('version') != inventory.get('version'):
            raise ValueError('Release manifest/build version mismatch')
    return {'verified':True,'version':inventory['version'],'files':len(inventory['files']),
            'publisher_authenticated':False}

def binary_build_info(exe):
    result = subprocess.run([str(Path(exe).resolve()),'--build-info'],check=True,
                            capture_output=True,text=True,timeout=30)
    return json.loads(result.stdout)
