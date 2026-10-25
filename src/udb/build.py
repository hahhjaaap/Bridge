"""Source/frozen release build from the canonical package; never bundles state."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from ._version import VERSION

def runtime_manifest(package=None):
    package=Path(package) if package else Path(__file__).parent
    result={}
    for p in sorted(package.rglob('*')):
        if not p.is_file() or p.suffix not in ('.py','.ps1'):continue
        if p.is_symlink() or getattr(p.lstat(),'st_file_attributes',0)&0x400:raise ValueError('Linked source component refused')
        result['udb/'+p.relative_to(package).as_posix()]=hashlib.sha256(p.read_bytes()).hexdigest()
    return result

def build_info():
    embedded=Path(__file__).with_name('build_info.json')
    if getattr(sys,'frozen',False) and embedded.is_file():return json.loads(embedded.read_text('utf-8'))
    return dict(version=VERSION,kind='source',code_signed=False,source_files=runtime_manifest())

def source_root():
    import tomllib
    for candidate in (Path.cwd(),Path(__file__).resolve().parents[2]):
        project=candidate/'pyproject.toml'
        if not project.is_file():continue
        data=tomllib.loads(project.read_text('utf-8'))
        if data.get('project',{}).get('name')=='universal-desktop-bridge' and (candidate/'src/udb/_version.py').is_file():return candidate
    raise ValueError('Build requires a source checkout')

def run(exe=False,output=None):
    base=source_root()
    destination=Path(output).absolute() if output else base/'dist'
    if exe:
        if os.name!='nt':raise ValueError('A Windows executable must be built on Windows')
        info=base/'src/udb/build_info.json';info.write_text(json.dumps(dict(version=VERSION,source_files=runtime_manifest(base/'src/udb'),kind='frozen',release_provenance=True,code_signed=False),sort_keys=True,indent=2)+'\n')
        try:
            env=os.environ.copy();env['PYTHONPATH']=str(base/'src')+os.pathsep+env.get('PYTHONPATH','')
            subprocess.run([sys.executable,'-m','PyInstaller','--noconfirm','--clean','--distpath',str(destination),str(base/'tools/udb.spec')],cwd=base,env=env,check=True)
        finally:info.unlink(missing_ok=True)
    else:subprocess.run([sys.executable,'-m','build','--outdir',str(destination)],cwd=base,check=True)
    destination.mkdir(parents=True,exist_ok=True)
    from .metadata import manifest
    (destination/'plugin.json').write_text(json.dumps(manifest(),indent=2)+'\n')
    for template in ('udb.example.toml','MCP_CONFIGURATION.example.json'):
        (destination/template).write_bytes((base/template).read_bytes())
    files=[p for p in destination.iterdir() if p.is_file() and p.suffix in ('.exe','.whl','.gz','.toml','.json')]
    (destination/'SHA256SUMS').write_text(''.join(hashlib.sha256(p.read_bytes()).hexdigest()+'  '+p.name+'\n' for p in sorted(files)))
    return 0
