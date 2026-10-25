"""One public runtime configuration. Secrets/grants remain in owner storage."""
from dataclasses import dataclass
import os
from pathlib import Path
import tomllib

@dataclass(frozen=True)
class Settings:
    home:Path
    project_root:Path
    enable_terminal:bool=False
    core_backend:str='source'
    core_exe:Path|None=None

def default_home():return Path(os.environ.get('UDB_HOME',str(Path.home()/'Downloads/Universal-Desktop-Bridge'))).expanduser().absolute()

def checked(path):
    path=Path(path).expanduser().absolute()
    for item in (path,*path.parents):
        if item.is_symlink() or (item.exists() and getattr(item.lstat(),'st_file_attributes',0)&0x400):raise ValueError('Linked configuration path refused')
    return path.resolve(strict=False)

def load(home=None,config=None,root=None,enable_terminal=None,core_exe=None,allow_missing_config=False):
    initial=checked(home) if home else checked(default_home())
    selected=checked(config) if config else checked(initial/'udb.toml')
    if config and not selected.exists() and not allow_missing_config:raise ValueError('Explicit configuration file does not exist')
    data={}
    if selected.exists():
        if not selected.is_file() or selected.stat().st_nlink!=1 or selected.stat().st_size>65536:raise ValueError('Invalid configuration file')
        with selected.open('rb') as stream:
            raw=stream.read(65537)
        if len(raw)>65536:raise ValueError('Configuration exceeds 64 KiB')
        try:data=tomllib.loads(raw.decode('utf-8'))
        except (ValueError,UnicodeError):raise ValueError('Invalid UDB TOML configuration') from None
    if set(data)-{'bridge'} or not isinstance(data.get('bridge',{}),dict):raise ValueError('Unknown configuration section')
    values=data.get('bridge',{})
    if set(values)-{'home','project_root','enable_terminal','core_backend','core_exe'}:raise ValueError('Unknown bridge configuration field; credentials belong in local owner storage')
    def path_value(value):
        if not isinstance(value,str) or not value or '\0' in value:raise ValueError('Invalid configured path')
        path=Path(value).expanduser()
        return checked(path if path.is_absolute() else selected.parent/path)
    actual=checked(home) if home else path_value(values['home']) if 'home' in values else initial
    project=checked(root) if root else path_value(values['project_root']) if 'project_root' in values else actual/'projects/demo'
    terminal=enable_terminal if enable_terminal is not None else values.get('enable_terminal',False)
    if type(terminal) is not bool:raise ValueError('enable_terminal must be boolean')
    backend='installed' if core_exe else values.get('core_backend','source')
    if backend not in ('source','installed'):raise ValueError('core_backend must be source or installed')
    exe=checked(core_exe) if core_exe else path_value(values['core_exe']) if 'core_exe' in values else actual/'UniversalDesktopBridge.exe' if backend=='installed' else None
    if backend=='source' and exe is not None:raise ValueError('core_exe requires installed backend')
    return Settings(actual,project,terminal,backend,exe)

# Effective settings are passed to the manager via its own bounded arguments,
# never persisted as grants or inserted into the user's connection manifest.
_active=None

def activate(settings):
    global _active
    previous=_active;_active=settings;return previous

def effective(home):
    return _active if _active is not None and _active.home==Path(home).absolute() else load(home=home)

def manager_options(home):
    settings=effective(home)
    args=['--runtime-root',str(settings.project_root),'--runtime-terminal','on' if settings.enable_terminal else 'off']
    if settings.core_exe:args+=['--runtime-core-exe',str(settings.core_exe)]
    return args

def serve_options(home):
    settings=effective(home)
    args=['serve','--home',str(settings.home),'--root',str(settings.project_root), '--enable-terminal' if settings.enable_terminal else '--disable-terminal']
    if settings.core_exe:args+=['--core-exe',str(settings.core_exe)]
    return args
