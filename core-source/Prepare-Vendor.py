"""Prepare offline dependencies from explicit trusted local installations; no downloads."""
import argparse,json,re,shutil,subprocess,sys
from pathlib import Path
from udb.release import regular_file,sha256,validate_vendor_directory
def stage_stdlib(python_home, runtime):
    """Stage an unpacked stdlib; a release must not contain nested ZIPs."""
    source = Path(python_home) / 'Lib'
    destination = Path(runtime) / 'Lib'
    destination.mkdir(parents=True)
    excluded = {'site-packages', '__pycache__', 'test', 'tests',
                'idlelib', 'tkinter', 'ensurepip'}
    for item in sorted(source.rglob('*.py')):
        relative = item.relative_to(source)
        if any(part in excluded for part in relative.parts):
            continue
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(regular_file(item), target)


def read_tunnel_source_manifest(path, tunnel, licenses):
    source = json.loads(regular_file(path).read_text(encoding='utf-8-sig'))
    if not isinstance(source, dict):
        raise ValueError('Invalid tunnel source manifest')
    tag = source.get('tag_name', '')
    if (source.get('repository') != 'openai/tunnel-client' or
            not re.fullmatch(r'v[0-9]+[.][0-9]+[.][0-9]+', tag)):
        raise ValueError('Tunnel source manifest must identify an official tagged OpenAI release')
    if source.get('asset_name') != f'tunnel-client-{tag}-windows-amd64.zip':
        raise ValueError('Tunnel source manifest asset name mismatch')
    if source.get('license_asset_name') != f'tunnel-client-{tag}-windows-amd64-licenses.txt':
        raise ValueError('Tunnel license asset name mismatch')
    for field in ('asset_sha256', 'license_asset_sha256'):
        if not isinstance(source.get(field), str) or not re.fullmatch(r'sha256:[0-9a-f]{64}', source[field]):
            raise ValueError('Tunnel source manifest has an invalid published digest')
    if source.get('tunnel_executable_sha256') != sha256(regular_file(tunnel).read_bytes()):
        raise ValueError('Tunnel executable does not match source manifest')
    if source.get('license_file_sha256') != sha256(regular_file(licenses).read_bytes()):
        raise ValueError('Tunnel license file does not match source manifest')
    return source


def main():
    p=argparse.ArgumentParser();p.add_argument('--python-home',type=Path,required=True)
    p.add_argument('--tunnel',type=Path,required=True);p.add_argument('--tunnel-licenses',type=Path,required=True)
    p.add_argument('--tunnel-source-manifest',type=Path,required=True)
    a=p.parse_args();root=Path(__file__).resolve().parent;vendor=root/'vendor'
    source_manifest = read_tunnel_source_manifest(a.tunnel_source_manifest, a.tunnel, a.tunnel_licenses)
    if vendor.exists():raise ValueError('Use a clean vendor directory')
    runtime=vendor/'python-runtime';runtime.mkdir(parents=True)
    for name in ('python.exe','python3.dll','python311.dll','vcruntime140.dll','vcruntime140_1.dll','LICENSE.txt'):
        shutil.copy2(regular_file(a.python_home/name),runtime/name)
    (runtime/'DLLs').mkdir()
    for item in sorted((a.python_home/'DLLs').iterdir()):
        if item.suffix in ('.pyd','.dll'):shutil.copy2(regular_file(item),runtime/'DLLs'/item.name)
    stage_stdlib(a.python_home, runtime)
    (runtime/'python311._pth').write_text('Lib\n.\nDLLs\n',encoding='ascii')
    shutil.copy2(regular_file(a.tunnel),vendor/'tunnel-client.exe')
    shutil.copy2(regular_file(a.tunnel_licenses),vendor/'tunnel-LICENSES.txt')
    (vendor/'tunnel-client-source.json').write_text(
        json.dumps(source_manifest, sort_keys=True, indent=2) + '\n', encoding='utf-8')
    subprocess.run([str(runtime/'python.exe'),'-I','-B','-c',
        'import ctypes,json,sqlite3,ssl,subprocess,hashlib,encodings,xml.etree.ElementTree;print("PORTABLE_RUNTIME_OK")'],check=True)
    # Keep the distributable runtime source-only even if a smoke test or toolchain
    # accidentally produced bytecode caches.
    for cache in runtime.rglob('__pycache__'):
        if cache.is_symlink(): raise ValueError('Linked bytecode cache refused')
        shutil.rmtree(cache)
    for bytecode in runtime.rglob('*.pyc'):
        if bytecode.is_symlink(): raise ValueError('Linked bytecode file refused')
        bytecode.unlink()
    records={p.relative_to(root).as_posix():{'sha256':sha256(p.read_bytes()),'bytes':p.stat().st_size}
             for p in vendor.rglob('*') if p.is_file()}
    (vendor/'DEPENDENCIES.json').write_text(json.dumps({'python_version':subprocess.check_output([str(runtime/'python.exe'),'-B','--version'],text=True).strip(),
        'tunnel_client_release':source_manifest['tag_name'],
        'origin':'explicit CPython installation plus digest-verified official OpenAI tunnel-client release',
        'files':records},indent=2),encoding='utf8')
    validate_vendor_directory(root)
    print('VENDOR_FILES',len(records))
if __name__=='__main__':main()
