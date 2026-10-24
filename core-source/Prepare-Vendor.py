"""Prepare offline dependencies from explicit trusted local installations; no downloads."""
import argparse,json,shutil,subprocess,sys
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


def main():
    p=argparse.ArgumentParser();p.add_argument('--python-home',type=Path,required=True)
    p.add_argument('--tunnel',type=Path,required=True);p.add_argument('--tunnel-licenses',type=Path,required=True)
    a=p.parse_args();root=Path(__file__).resolve().parent;vendor=root/'vendor'
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
    subprocess.run([str(runtime/'python.exe'),'-I','-c',
        'import ctypes,json,sqlite3,ssl,subprocess,hashlib,encodings,xml.etree.ElementTree;print("PORTABLE_RUNTIME_OK")'],check=True)
    records={p.relative_to(root).as_posix():{'sha256':sha256(p.read_bytes()),'bytes':p.stat().st_size}
             for p in vendor.rglob('*') if p.is_file()}
    (vendor/'DEPENDENCIES.json').write_text(json.dumps({'python_version':subprocess.check_output([str(runtime/'python.exe'),'--version'],text=True).strip(),
        'origin':'explicit existing local CPython installation and tunnel client; no network download',
        'files':records},indent=2),encoding='utf8')
    validate_vendor_directory(root)
    print('VENDOR_FILES',len(records))
if __name__=='__main__':main()
