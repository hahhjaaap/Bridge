"""Policy-enforced Windows agent. No host shell execution is exposed."""
from __future__ import annotations
import ast
import difflib
import hashlib
import json
import os
import re
import sqlite3
import stat
import threading
import time
import uuid
from pathlib import Path, PureWindowsPath

MAX_TEXT = 512 * 1024
SOURCE_EXT = {'.py', '.ps1', '.json', '.toml', '.ini', '.cfg', '.md', '.txt',
              '.yaml', '.yml', '.js', '.ts', '.tsx', '.jsx', '.html', '.css',
              '.c', '.cpp', '.h', '.cs', '.sql', '.xml', '.gml', '.gitignore'}
BLOCK_DIRS = {'checkpoint', 'checkpoints', 'data', 'dataset', 'datasets',
              'training_data', 'training-data', 'weights', 'models', 'backups',
              'aiviet_backups', '.git', '.venv', 'venv', 'node_modules',
              '__pycache__', 'state', '.ssh', '.aws', '.azure', '.config'}
BLOCK_EXT = {'.pt', '.pth', '.ckpt', '.safetensors', '.bin', '.gguf', '.onnx',
             '.parquet', '.arrow', '.jsonl', '.csv', '.tsv', '.npy', '.npz',
             '.zip', '.7z', '.tar', '.gz', '.db', '.sqlite', '.pem', '.key', '.pfx'}
RESERVED = {'CON', 'PRN', 'AUX', 'NUL', 'CLOCK$', *(f'COM{i}' for i in range(10)),
            *(f'LPT{i}' for i in range(10))}

class PolicyError(ValueError):
    pass

class ScopedConnection(sqlite3.Connection):
    """Commit/rollback normally, then release the Windows database handle."""
    def __exit__(self, exc_type, exc_value, traceback):
        try:
            return super().__exit__(exc_type, exc_value, traceback)
        finally:
            self.close()

def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()

def canonical(value) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'))

def is_under(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False

def reject_links(path: Path):
    """Reject every symlink/junction/reparse point, including ancestors."""
    for part in [path, *path.parents]:
        if not part.exists() and not part.is_symlink():
            continue
        s = part.lstat()
        if stat.S_ISLNK(s.st_mode) or getattr(s, 'st_file_attributes', 0) & 0x400:
            raise PolicyError('Symlink/junction/reparse points are not allowed')
    if path.is_file() and path.stat().st_nlink != 1:
        raise PolicyError('Hardlinked files are not allowed')

class Agent:
    def __init__(self, home: Path):
        self.home = home.absolute()
        self.state = self.home / 'state'
        reject_links(self.home)
        reject_links(self.state)
        self.state.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.db = self.state / 'agent.sqlite3'
        with self.connect() as c:
            c.executescript('''
            CREATE TABLE IF NOT EXISTS projects(id TEXT PRIMARY KEY, root TEXT UNIQUE, writable INTEGER);
            CREATE TABLE IF NOT EXISTS protected(root TEXT PRIMARY KEY);
            CREATE TABLE IF NOT EXISTS requests(id TEXT PRIMARY KEY, kind TEXT, payload TEXT,
              hash TEXT, created REAL, status TEXT, result TEXT);
            CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY, status TEXT, info TEXT);
            CREATE TABLE IF NOT EXISTS sandbox_grants(project TEXT PRIMARY KEY, root TEXT, token TEXT, created REAL);
            CREATE TABLE IF NOT EXISTS source_grants(project TEXT PRIMARY KEY, root TEXT, token TEXT, created REAL);
            CREATE TABLE IF NOT EXISTS media_grants(scope TEXT PRIMARY KEY, token TEXT, created REAL);
            CREATE TABLE IF NOT EXISTS events(seq INTEGER PRIMARY KEY AUTOINCREMENT,
              at REAL, event TEXT, details TEXT);
            ''')
            # Protect the *entire* existing AI tree, not just known suffixes.
            if os.name == 'nt':
                for name in ('AIVIET', 'AIVIET_Backups'):
                    root = Path.home() / 'Downloads' / name
                    c.execute('INSERT OR IGNORE INTO protected VALUES (?)', (str(root.resolve()),))

    def connect(self):
        reject_links(self.db)
        c = sqlite3.connect(self.db, timeout=10, factory=ScopedConnection)
        c.row_factory = sqlite3.Row
        return c

    def audit(self, event, details):
        # Never store source text, tokens, or environment variables in audit.
        with self.connect() as c:
            c.execute('INSERT INTO events(at,event,details) VALUES (?,?,?)',
                      (time.time(), event, canonical(details)))

    def protect_local(self, root: str):
        p = Path(root).absolute().resolve()
        if not p.is_dir():
            raise PolicyError('Protected folder must exist')
        with self.connect() as c:
            c.execute('INSERT OR IGNORE INTO protected VALUES (?)', (str(p),))
        self.audit('protect', {'root': str(p)})

    def projects(self):
        with self.connect() as c:
            return [dict(r) for r in c.execute('SELECT * FROM projects ORDER BY id')]

    def project(self, project: str):
        with self.connect() as c:
            r = c.execute('SELECT * FROM projects WHERE id=?', (project,)).fetchone()
        if not r:
            raise PolicyError('Unknown project; register it locally in the EXE manager')
        p = Path(r['root'])
        reject_links(p)
        if not p.is_dir() or p.resolve() != p:
            raise PolicyError('Project root changed')
        self.check_protected(p)
        return r, p

    def check_protected(self, p: Path):
        # Internal agent files must never be exposed as a registered project.
        if is_under(p.resolve(), self.state.resolve()):
            raise PolicyError('Agent state is private')
        if is_under(p.resolve(), self.home.resolve()) and not is_under(p.resolve(), (self.home/'projects').resolve()):
            raise PolicyError('Bridge installation is private; use the projects subfolder')
        with self.connect() as c:
            roots = [Path(r[0]) for r in c.execute('SELECT root FROM protected')]
        if any(is_under(p.resolve(), q) for q in roots):
            raise PolicyError('Protected root: access denied')

    def register_local(self, project: str, root: str, writable=False):
        if not re.fullmatch(r'[a-zA-Z0-9_-]{1,48}', project):
            raise PolicyError('Project ID: 1–48 letters, digits, hyphen or underscore')
        p = Path(root).absolute()
        reject_links(p)
        p = p.resolve()
        if not p.is_dir() or p == p.parent:
            raise PolicyError('Choose a project folder, never a drive root')
        # Home, Downloads, install tree and their ancestors are too broad.
        for private in (self.home.resolve(), Path.home().resolve()):
            if is_under(private, p):
                raise PolicyError('Root too broad or contains bridge installation')
        self.check_protected(p)
        if p.name.lower() in BLOCK_DIRS:
            raise PolicyError('Data/backup folders cannot be projects')
        with self.connect() as c:
            c.execute('INSERT INTO projects VALUES (?,?,?) ON CONFLICT(id) DO UPDATE SET root=excluded.root,writable=excluded.writable',
                      (project, str(p), int(writable)))
        self.audit('register', {'project': project, 'root': str(p), 'writable': bool(writable)})

    def path(self, project: str, relative: str = '.', write=False):
        r, root = self.project(project)
        if write and not r['writable']:
            raise PolicyError('Project is read-only')
        if not isinstance(relative, str) or len(relative) > 240 or '\x00' in relative:
            raise PolicyError('Invalid relative path')
        w = PureWindowsPath(relative)
        if w.is_absolute() or w.drive or w.root or ':' in relative or relative.startswith(('/', '\\')):
            raise PolicyError('Only project-relative paths are allowed')
        parts = relative.replace('\\', '/').split('/')
        for part in parts:
            if part in ('', '.'):
                continue
            if part == '..' or part.endswith((' ', '.')) or any(ch in part for ch in '<>"|?*'):
                raise PolicyError('Unsafe path segment')
            lower = part.lower()
            if (part.split('.')[0].upper() in RESERVED or lower in BLOCK_DIRS
                    or 'checkpoint' in lower or 'training' in lower
                    or lower.startswith(('.env', 'credentials', 'secrets', 'id_rsa'))
                    or Path(part).suffix.lower() in BLOCK_EXT):
                raise PolicyError('Protected data, credential, or reserved path')
        p = root.joinpath(*(q for q in parts if q not in ('', '.')))
        reject_links(p)
        if not is_under(p.resolve(), root):
            raise PolicyError('Path escaped project')
        self.check_protected(p)
        return p

    def read_bytes(self, project, relative):
        p = self.path(project, relative)
        if not p.is_file() or p.suffix.lower() not in SOURCE_EXT or p.stat().st_size > MAX_TEXT:
            raise PolicyError('Only small source/text files may be read')
        data = p.read_bytes()
        if len(data) > MAX_TEXT or b'\0' in data:
            raise PolicyError('Binary/oversized file blocked')
        data.decode('utf-8-sig')
        return data

    def read(self, project, relative):
        data = self.read_bytes(project, relative)
        self.audit('read', {'project': project, 'path': relative, 'sha256': digest(data)})
        return {'path': relative, 'sha256': digest(data), 'text': data.decode('utf-8-sig')}

    def listing(self, project, relative='.', offset=0, limit=100):
        p = self.path(project, relative)
        offset = max(0, min(int(offset), 100000))
        limit = max(1, min(int(limit), 200))
        _, root = self.project(project)
        # Bound traversal even for unusually large directories.
        visible = []
        for i, f in enumerate(p.iterdir()):
            if i >= 10000:
                break
            rel = f.relative_to(root).as_posix()
            try:
                self.path(project, rel)
                if f.is_file() and f.suffix.lower() not in SOURCE_EXT:
                    continue
                visible.append({'path': rel, 'directory': f.is_dir()})
            except PolicyError:
                continue
        visible.sort(key=lambda x: x['path'].lower())
        return {'items': visible[offset:offset+limit], 'next_offset': offset+limit if len(visible)>offset+limit else None,
                'protected_items_hidden': True, 'scan_cap': 10000}

    def propose(self, kind, payload):
        data = canonical(payload)
        rid = uuid.uuid4().hex
        h = digest(data.encode())
        automatic = (kind in ('desktop','job') and self.sandbox_authorization(payload['project'],payload['root'])) or (kind=='media' and self.media_autonomy())
        initial = ('queued_sandbox' if kind in ('desktop','job') else 'queued_media') if automatic else 'pending'
        with self.connect() as c:
            if c.execute("SELECT count(*) FROM requests WHERE status IN ('pending','queued_sandbox','queued_media') AND created>?", (time.time()-600,)).fetchone()[0] >= 30:
                raise PolicyError('Too many pending approvals')
            c.execute('INSERT INTO requests VALUES (?,?,?,?,?,?,?)',
                      (rid, kind, data, h, time.time(), initial, canonical({'authorization':automatic}) if automatic else '{}'))
        self.audit('propose', {'id': rid, 'kind': kind, 'sha256': h})
        return {'request_id': rid, 'sha256': h, 'status': initial if automatic else 'pending_local_approval',
                'expires_in_seconds': 600,
                'next': ('Existing owner grant: keep Bridge open and poll get_request_status; no per-action approval.' if automatic else 'Enable the corresponding autonomy switch in Bridge, or review this exceptional request locally. AI cannot create grants.')}

    def propose_write(self, project, relative, text, expected_sha256):
        p = self.path(project, relative, write=True)
        if p == self.project(project)[1] or p.suffix.lower() not in SOURCE_EXT:
            raise PolicyError('Write only source/text file extensions')
        if not p.parent.is_dir():
            raise PolicyError('Parent directory must exist')
        if len(text.encode()) > MAX_TEXT or '\0' in text:
            raise PolicyError('Text too large or binary')
        before = self.read_bytes(project, relative) if p.exists() else None
        actual = digest(before) if before is not None else 'NEW'
        if expected_sha256 != actual:
            raise PolicyError('Stale file: read current SHA256; use NEW only for new files')
        diff = ''.join(difflib.unified_diff((before or b'').decode('utf-8-sig').splitlines(True), text.splitlines(True), fromfile=relative, tofile=relative))
        payload = {'project': project, 'path': relative, 'text': text,
                   'expected': actual, 'root': str(self.project(project)[1]), 'diff': diff}
        if self.source_autonomy_authorization(project, payload['root']):
            return self.auto_apply_write(payload)
        response = self.propose('write', payload)
        response['diff'] = diff[:16000]
        return response

    def snapshot(self, project):
        _, root = self.project(project)
        manifest = {}
        total = 0
        visited = 0
        for parent, dirs, files in os.walk(root, followlinks=False):
            visited += 1
            if visited > 2000:
                raise PolicyError('Source tree too large; register a narrower folder')
            safe_dirs = []
            for d in dirs:
                try:
                    self.path(project, (Path(parent)/d).relative_to(root).as_posix())
                    safe_dirs.append(d)
                except PolicyError:
                    pass
            dirs[:] = safe_dirs
            for name in files:
                if Path(name).suffix.lower() not in SOURCE_EXT:
                    continue
                rel = (Path(parent)/name).relative_to(root).as_posix()
                try:
                    data = self.read_bytes(project, rel)
                except (PolicyError, UnicodeError):
                    continue
                total += len(data)
                if total > 16*1024*1024 or len(manifest) >= 2000:
                    raise PolicyError('Source snapshot limit: 16 MiB / 2000 files')
                manifest[rel] = digest(data)
        return manifest

    def propose_job(self, project, language, script, timeout=120):
        from .sandbox import available
        if not available():
            raise PolicyError('Windows Sandbox unavailable. Host execution is disabled; see Enable-Sandbox.ps1')
        if language not in ('python', 'powershell') or not 1 <= timeout <= 600:
            raise PolicyError('Invalid language or timeout (1–600 seconds)')
        if len(script.encode()) > 64000 or '\0' in script:
            raise PolicyError('Script too large or invalid')
        # Running tests also executes arbitrary code, so uses identical isolation.
        return self.propose('job', {'project': project, 'root': str(self.project(project)[1]),
                            'language': language, 'script': script, 'timeout': timeout,
                            'manifest': self.snapshot(project)})

    def propose_desktop(self,project,task,duration_seconds=600):
        from .desktop_vm import propose
        return propose(self,project,task,duration_seconds)

    # Owner-controlled persistent grants. No MCP tool exports the setters.
    # Changes to dependency manifests, launch scripts and CI remain manual.
    AUTO_PROTECTED_NAMES = frozenset((
        'setup.py','pyproject.toml','package.json','package-lock.json',
        'pnpm-lock.yaml','yarn.lock','requirements.txt','pipfile',
        'dockerfile','compose.yaml','docker-compose.yml',
        'install.ps1','uninstall.ps1','start.ps1','run.ps1',
        'build.ps1','postinstall.js','vite.config.js','webpack.config.js',
        'tox.ini','.gitignore','web.config','settings.json',
    ))

    def source_autonomy_authorization(self, project, root, token=None):
        with self.connect() as c:
            row=c.execute('SELECT * FROM source_grants WHERE project=? AND root=?',
                          (project,root)).fetchone()
        if not row or (token is not None and row['token'] != token):
            return None
        try:
            record, actual=self.project(project)
            if str(actual)!=root or not record['writable']:return None
        except PolicyError:return None
        return row['token']

    def source_autonomy_projects(self):
        with self.connect() as c:
            rows=[dict(r) for r in c.execute('SELECT * FROM source_grants ORDER BY project')]
        return [r['project'] for r in rows
                if self.source_autonomy_authorization(r['project'],r['root'],r['token'])]

    def set_source_autonomy_local(self, project, enabled):
        record, root=self.project(project)
        if enabled and not record['writable']:
            raise PolicyError('Enable project source writes locally before autonomy')
        with self.lock, self.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            c.execute('DELETE FROM source_grants WHERE project=?',(project,))
            if enabled:
                c.execute('INSERT INTO source_grants VALUES (?,?,?,?)',
                          (project,str(root),uuid.uuid4().hex,time.time()))
        self.audit('source_autonomy',{'project':project,'enabled':bool(enabled),
                                    'scope':'registered project source files only'})

    def auto_apply_write(self, payload):
        project, root, relative=payload['project'],payload['root'],payload['path']
        parts=[x.lower() for x in relative.replace('\\','/').split('/')]
        if any(x in self.AUTO_PROTECTED_NAMES for x in parts) or any(
                x.startswith(('.github','.vscode','.idea')) for x in parts):
            raise PolicyError('Important configuration or build file: automatic modification blocked')
        with self.lock:
            if not self.source_autonomy_authorization(project,root):
                raise PolicyError('Owner auto-write grant revoked or project root changed')
            rid=uuid.uuid4().hex
            h=digest(canonical(payload).encode())
            with self.connect() as c:
                c.execute('INSERT INTO requests VALUES (?,?,?,?,?,?,?)',
                          (rid,'write',canonical(payload),h,time.time(),'executing','{}'))
            try:
                result=self.apply_write(rid,payload)
                state='applied'
            except Exception as e:
                result,state={'error':str(e)},'failed'
            with self.connect() as c:
                c.execute('UPDATE requests SET status=?,result=? WHERE id=?',
                          (state,canonical(result),rid))
            self.audit('auto_write',{'id':rid,'project':project,'path':relative,'status':state})
            if state=='failed':
                raise PolicyError(result['error'])
            return {'request_id':rid,'status':'applied_auto','sha256':h,
                    'file_sha256':result['sha256'],'backup_id':result['backup_id'],
                    'scope':'owner_granted_project_only'}

    def media_autonomy(self):
        with self.connect() as c:
            r=c.execute("SELECT token FROM media_grants WHERE scope='approved_https_hls'").fetchone()
        return bool(r)

    def set_media_autonomy_local(self, enabled):
        with self.lock,self.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            c.execute("DELETE FROM media_grants WHERE scope='approved_https_hls'")
            if enabled:
                c.execute('INSERT INTO media_grants VALUES (?,?,?)',
                          ('approved_https_hls',uuid.uuid4().hex,time.time()))
        self.audit('media_autonomy',{'enabled':bool(enabled),
                   'scope':'allowlisted HTTPS HLS to Downloads, no overwrite'})

    def pump_media_queue_local(self):
        # This function is only called by the Windows-owned DesktopController.
        # Downloads run asynchronously; no script or arbitrary command is accepted.
        t=getattr(self,'_media_thread',None)
        if t is not None and t.is_alive():return
        with self.lock,self.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            c.execute("UPDATE requests SET status='expired' WHERE status='queued_media' AND created<?",
                      (time.time()-600,))
            row=c.execute("SELECT * FROM requests WHERE status='queued_media' ORDER BY created LIMIT 1").fetchone()
            if row is None:return
            if not self.media_autonomy() or row['kind']!='media' or digest(row['payload'].encode())!=row['hash']:
                c.execute("UPDATE requests SET status='rejected',result=? WHERE id=?",
                          (canonical({'error':'Media grant missing, revoked or request modified'}),row['id']))
                return
            c.execute("UPDATE requests SET status='executing' WHERE id=? AND status='queued_media'",
                      (row['id'],))
            rid=row['id'];payload=json.loads(row['payload'])
        def worker():
            try:
                if not self.media_autonomy():
                    raise PolicyError('Media autonomy revoked')
                from .media import run_download
                result=run_download(self,rid,payload)
                state='applied'
            except Exception as e:
                result,state={'error':str(e)},'failed'
            with self.connect() as c:
                c.execute('UPDATE requests SET status=?,result=? WHERE id=?',
                          (state,canonical(result),rid))
            self.audit('auto_media',{'id':rid,'status':state})
        self._media_thread=threading.Thread(target=worker,name='UDBAutoMedia',daemon=True)
        self._media_thread.start()

    def sandbox_authorization(self, project, root, token=None):
        """Read-only scope check. No MCP tool can create or widen this grant."""
        with self.connect() as c:
            row = c.execute('SELECT * FROM sandbox_grants WHERE project=? AND root=?', (project,root)).fetchone()
        if not row or (token is not None and row['token'] != token):return None
        try:
            if str(self.project(project)[1]) != root:return None
        except PolicyError:return None
        return row['token']

    def sandbox_autonomy_projects(self):
        with self.connect() as c:rows=[dict(r) for r in c.execute('SELECT * FROM sandbox_grants ORDER BY project')]
        return [r['project'] for r in rows if self.sandbox_authorization(r['project'],r['root'],r['token'])]

    def set_sandbox_autonomy_local(self, project, enabled):
        """Owner-only local setting, never exported by MCP. Does not grant host writes."""
        _,root=self.project(project)
        with self.lock, self.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            c.execute('DELETE FROM sandbox_grants WHERE project=?',(project,))
            if enabled:
                token=uuid.uuid4().hex
                c.execute('INSERT INTO sandbox_grants VALUES (?,?,?,?)',(project,str(root),token,time.time()))
                # Existing unexpired guest proposals share the same owner-granted scope.
                for row in c.execute("SELECT * FROM requests WHERE kind IN ('desktop','job') AND status='pending' AND created>?",(time.time()-600,)).fetchall():
                    p=json.loads(row['payload'])
                    if p.get('project')==project and p.get('root')==str(root):
                        c.execute("UPDATE requests SET status='queued_sandbox',result=? WHERE id=?",(canonical({'authorization':token}),row['id']))
            else:
                for row in c.execute("SELECT * FROM requests WHERE status='queued_sandbox'").fetchall():
                    if json.loads(row['payload']).get('project')==project:
                        c.execute("UPDATE requests SET status='rejected',result=? WHERE id=?",(canonical({'error':'Sandbox autonomy revoked locally'}),row['id']))
                for row in c.execute("SELECT * FROM jobs WHERE status IN ('running','reviewing')").fetchall():
                    info=json.loads(row['info'])
                    if info.get('project')==project and info.get('authorization'):
                        c.execute("UPDATE jobs SET status='stopping' WHERE id=?",(row['id'],))
        self.audit('sandbox_autonomy',{'project':project,'enabled':bool(enabled),'scope':'guest only'})

    def pump_sandbox_queue_local(self):
        """GUI-owned executor for previously granted scopes; never calls decide_local."""
        with self.lock, self.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            c.execute("UPDATE requests SET status='expired' WHERE status='queued_sandbox' AND created<?",(time.time()-600,))
            if c.execute("SELECT count(*) FROM jobs WHERE status IN ('running','reviewing','stopping')").fetchone()[0]:return
            row=c.execute("SELECT * FROM requests WHERE status='queued_sandbox' ORDER BY created LIMIT 1").fetchone()
            if not row:return
            p=json.loads(row['payload']);authorization=json.loads(row['result']).get('authorization')
            grant=c.execute('SELECT token FROM sandbox_grants WHERE project=? AND root=?',(p.get('project'),p.get('root'))).fetchone()
            if row['kind'] not in ('desktop','job') or not grant or grant['token']!=authorization or digest(canonical(p).encode())!=row['hash']:
                c.execute("UPDATE requests SET status='failed',result=? WHERE id=?",(canonical({'error':'Sandbox authorization invalid or revoked'}),row['id']));return
            c.execute("UPDATE requests SET status='executing' WHERE id=? AND status='queued_sandbox'",(row['id'],))
        try:
            if not self.sandbox_authorization(p['project'],p['root'],authorization):raise PolicyError('Sandbox grant revoked or project root changed')
            p['authorization']=authorization
            if row['kind']=='job':result=self.start_job(row['id'],p)
            else:
                from .desktop_vm import launch
                result=launch(self,row['id'],p)
            state='started'
        except Exception as e:result,state={'error':str(e)},'failed'
        with self.connect() as c:c.execute('UPDATE requests SET status=?,result=? WHERE id=?',(state,canonical(result),row['id']))
        self.audit('sandbox_grant_dispatch',{'id':row['id'],'status':state})
        return result

    def pending_local(self):
        with self.connect() as c:
            c.execute("UPDATE requests SET status='expired' WHERE status IN ('pending','queued_sandbox','queued_media') AND created<?", (time.time()-600,))
            return [dict(r) for r in c.execute("SELECT * FROM requests WHERE status='pending' ORDER BY created")]

    def request_status(self, rid):
        with self.connect() as c:
            row = c.execute('SELECT id,kind,hash,created,status,result FROM requests WHERE id=?', (rid,)).fetchone()
        if not row:
            raise PolicyError('Unknown request ID')
        r = dict(row)
        r['result'] = json.loads(r['result'])
        return r

    def decide_local(self, rid, approve: bool):
        """Intentionally not exported as an MCP tool or HTTP endpoint."""
        with self.lock, self.connect() as c:
            row = c.execute("SELECT * FROM requests WHERE id=? AND status='pending'", (rid,)).fetchone()
            if not row or row['created'] < time.time()-600:
                raise PolicyError('Request expired or already consumed')
            if not approve:
                c.execute("UPDATE requests SET status='rejected' WHERE id=?", (rid,))
                return
            p = json.loads(row['payload'])
            if digest(canonical(p).encode()) != row['hash']:
                raise PolicyError('Approval integrity check failed')
            claimed = c.execute("UPDATE requests SET status='executing' WHERE id=? AND status='pending'", (rid,))
            if claimed.rowcount != 1:
                raise PolicyError('Request already consumed by another local manager')
        try:
            if row['kind'] != 'media' and str(self.project(p['project'])[1]) != p['root']:
                raise PolicyError('Project root changed since proposal')
            if row['kind']=='write':result=self.apply_write(rid,p)
            elif row['kind']=='job':result=self.start_job(rid,p)
            elif row['kind']=='desktop':
                from .desktop_vm import launch
                result=launch(self,rid,p)
            elif row['kind']=='media':
                from .media import run_download
                result=run_download(self,rid,p)
            else:raise PolicyError('Unknown request kind')
            status = 'applied' if row['kind'] in ('write','media') else 'started'
        except Exception as e:
            result, status = {'error': str(e)}, 'failed'
        with self.connect() as c:
            c.execute('UPDATE requests SET status=?,result=? WHERE id=?', (status, canonical(result), rid))
        self.audit('decision', {'id': rid, 'status': status})
        return result

    def apply_write(self, rid, payload):
        project, rel = payload['project'], payload['path']
        p = self.path(project, rel, write=True)
        old = self.read_bytes(project, rel) if p.exists() else None
        if (digest(old) if old is not None else 'NEW') != payload['expected']:
            raise PolicyError('File changed after approval was requested; submit a new proposal')
        backup = None
        if old is not None:
            folder = self.state / 'backups'
            folder.mkdir(exist_ok=True)
            backup = folder / (rid + '.bak')
            with backup.open('xb') as f:
                f.write(old)
        # Exclusive temporary file + atomic rename. Never follow an existing link.
        temp = p.with_name('.udb-' + rid + '.tmp')
        try:
            with temp.open('xb') as f:
                f.write(payload['text'].encode('utf-8'))
                f.flush()
                os.fsync(f.fileno())
            self.path(project, rel, write=True)
            current = p.read_bytes() if p.exists() else None
            if (digest(current) if current is not None else 'NEW') != payload['expected']:
                raise PolicyError('Concurrent modification detected')
            os.replace(temp, p)
        finally:
            temp.unlink(missing_ok=True)
        return {'path': rel, 'sha256': digest(payload['text'].encode()),
                'backup_id': rid if backup else None}

    def start_job(self, rid, payload):
        from .sandbox import launch
        return launch(self, rid, payload)

    def syntax_check(self, project, relative):
        if not relative.lower().endswith('.py'):
            raise PolicyError('Python source only')
        data = self.read_bytes(project, relative)
        try:
            ast.parse(data.decode('utf-8-sig'), filename=relative)
            return {'valid': True, 'executes_code': False}
        except SyntaxError as e:
            return {'valid': False, 'line': e.lineno, 'column': e.offset, 'message': e.msg, 'executes_code': False}

    def job_status(self, jid, offset=0, limit=8000):
        if not re.fullmatch('[a-f0-9]{32}', jid):
            raise PolicyError('Invalid job ID')
        with self.connect() as c:
            row = c.execute('SELECT * FROM jobs WHERE id=?', (jid,)).fetchone()
        if not row:
            raise PolicyError('Unknown job')
        r = dict(row)
        r['info'] = json.loads(r['info'])
        folder = self.state/'jobs'/jid/'output'
        log = folder/'output.log'
        reject_links(log)
        offset, limit = max(0, int(offset)), max(1, min(int(limit), 32000))
        if log.exists():
            with log.open('rb') as f:
                f.seek(offset)
                data = f.read(limit)
            r.update(output=data.decode('utf-8', errors='replace'), next_offset=offset+len(data))
        return r

    def diagnostics(self):
        from .sandbox import available
        from . import __version__
        from .host_client import status as host_status
        hs=host_status(self)
        from .headless import manager_status
        from .computer_access import summary as computer_summary
        computer=computer_summary(self)
        return {'name': 'Universal Desktop Bridge', 'version': __version__,
                'manager': manager_status(self.state),
                'computer_access': computer,
                'host_background': {'enabled':hs['enabled'],'worker_ready':hs['worker_ready'],'scope':'owned browser and native text workspace only','user_mouse_keyboard':False},
                'resources':hs['resources'],
                'platform': os.name, 'sandbox_available': available(),
                'host_execution': computer['enabled'], 'auto_write_projects': self.source_autonomy_projects(), 'media_auto': self.media_autonomy(), 'media_download': 'owner-approved, HTTPS allowlist, FFmpeg only', 'approvals': 'Existing project/Sandbox grants retained. Optional computer access requires attended local owner opt-in; no MCP setter.',
                'sandbox_autonomy_projects': self.sandbox_autonomy_projects(),
                'protected_data': 'Scoped APIs enforce protected paths; optional computer commands are current-user and unsandboxed',
                'projects': len(self.projects()), 'rdc_dependency': False}
