from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import os
from pathlib import Path
import re
import shutil
import signal
import sqlite3
import socket
import subprocess
import sys
import tempfile
import tarfile
import time
from urllib.parse import urlparse
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.darimiru_deploy_safety import (
    BOT_FOREIGN_KEY_ALLOWLIST, BOT_FOREIGN_KEY_PARENT_KEYS, SafetyError, require, readonly, inspect_database, coordinated_backup, financial_snapshot, file_sha256,
    daily_snapshot, schema_is_additive, backup_code, clear_code, install_archive, validate_archive,
    lock_inventory, verify_locks, assert_locks_idle, verify_preserved,
)
from scripts.smoke_darimiru_financial import run_readonly_smoke, run_smoke, preflight_qa


def execute(arguments, **kwargs):
    result = subprocess.run(arguments, capture_output=True, text=True, timeout=kwargs.pop('timeout', 30), **kwargs)
    require(result.returncode == 0, 'Command failed: ' + Path(arguments[0]).name)
    return result.stdout


def proc_record(pid, proc=Path('/proc')):
    directory = proc / str(pid)
    text = (directory / 'stat').read_text()
    fields = text[text.rindex(')') + 2:].split()
    return {'pid': pid, 'ppid': int(fields[1]), 'start': fields[19], 'cwd': str((directory / 'cwd').resolve(strict=True))}


def process_tree(roots, proc=Path('/proc')):
    records = {}
    for directory in proc.iterdir():
        if directory.name.isdigit():
            try:
                row = proc_record(int(directory.name), proc)
                records[row['pid']] = row
            except (FileNotFoundError, ProcessLookupError, PermissionError):
                continue
    if roots is None:
        return records
    selected = set(roots)
    while True:
        children = {pid for pid, row in records.items() if row['ppid'] in selected}
        if children <= selected:
            break
        selected |= children
    return {pid: records[pid] for pid in selected if pid in records}


def sqlite_path(value, root, url=False):
    if url:
        from sqlalchemy.engine import make_url
        parsed = make_url(value)
        require(parsed.drivername in ('sqlite', 'sqlite+aiosqlite') and not parsed.host and not parsed.query, 'Only local SQLite databases are supported')
        value = parsed.database
    require(value and value != ':memory:', 'Persistent SQLite database path is required')
    path = Path(value)
    if not path.is_absolute():
        path = Path(root) / path
    require(path.is_file(), 'Configured production SQLite database does not exist')
    return str(path.absolute()), str(path.resolve(strict=True))


def probe_config(kind, root):
    os.chdir(root)
    sys.path.insert(0, str(root))
    if kind == 'bot':
        from bot.config import settings
        lexical, actual = sqlite_path(settings.database_url, root, url=True)
        return {'path': actual, 'lexical_path': lexical, 'secret': settings.webstore_bridge_secret, 'port': settings.app_port, 'prefix': settings.webhook_path_prefix, 'environment_files': [str(Path(root) / '.env')]}
    from webstore.config import settings, _ENV_FILE
    lexical, actual = sqlite_path(settings.database_path, root)
    return {'path': actual, 'lexical_path': lexical, 'secret': settings.bridge_shared_secret, 'port': settings.port, 'bridge_url': settings.referral_bridge_url, 'environment_files': [str(Path(root) / '.env'), str(_ENV_FILE.absolute()), str(_ENV_FILE.resolve())]}


def migrate(kind, root):
    os.chdir(root)
    sys.path.insert(0, str(root))
    import asyncio
    if kind == 'bot':
        from bot.database import init_db, engine
    else:
        from webstore.database import init_db, engine
    async def run():
        try:
            await init_db()
        finally:
            await engine.dispose()
    asyncio.run(run())


class Host:
    def __init__(self, root, python, pm2_name, service, proc=Path("/proc")):
        self.root, self.python = Path(root).resolve(strict=True), str(python)
        require(self.root.is_absolute() and len(self.root.parts) >= 4, 'Unsafe application root')
        require(Path(python).is_file() and os.access(python, os.X_OK), 'Deployment Python executable is missing')
        require(self.root not in Path(python).absolute().parents, 'Deployment virtualenv must be outside the code tree')
        require(re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*', pm2_name) and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.@-]*\.service', service), 'Unsafe process or service selector')
        self.proc = Path(proc)
        self.pm2_name, self.service = pm2_name, service
        self.helper = str(Path(__file__).resolve())
        self.contexts = {}
        self.old_processes = {}
        self.database_paths = []

    def processes(self):
        data = json.loads(execute(['pm2', 'jlist']))
        require(isinstance(data, list), 'Invalid PM2 process list')
        return [row for row in data if row.get('name') == self.pm2_name]

    def service_state(self):
        text = execute(['systemctl', 'show', self.service, '--property=MainPID,ActiveState,WorkingDirectory,KillMode,ControlGroup,TriggeredBy', '--no-pager'])
        return dict(line.split('=', 1) for line in text.splitlines() if '=' in line)

    def call_helper(self, mode, *args, env=None, root=None, timeout=120):
        result = execute([self.python, self.helper, mode, *map(str, args)], env=env, cwd=root or self.root, timeout=timeout)
        return result

    def configuration(self, kind, record):
        pid = record['pid']
        raw = (self.proc / str(pid) / 'environ').read_bytes()
        env = dict(part.decode().split('=', 1) for part in raw.split(b'\0') if b'=' in part)
        env['PYTHONPATH'] = str(self.root)
        env['PYTHONDONTWRITEBYTECODE'] = '1'
        require(Path(record['cwd']) == self.root, 'Runtime process has an unexpected working directory')
        config = json.loads(self.call_helper('_config', kind, self.root, env=env))
        visible_root = self.proc / str(pid) / 'root'
        database = Path(config['path'])
        require(database.stat().st_nlink == 1, 'Hard-linked SQLite databases are not supported')
        shared = [database, database.parent]
        lock_directory = database.parent / '.webstore-profile-locks'
        if kind == 'web' and lock_directory.exists():
            shared.append(lock_directory)
        for path in shared:
            visible = visible_root / str(path).lstrip('/')
            actual_stat, worker_stat = path.stat(), visible.stat()
            require((actual_stat.st_dev, actual_stat.st_ino) == (worker_stat.st_dev, worker_stat.st_ino), 'Workers do not share the same SQLite and flock filesystem')
        status = dict(line.split(':', 1) for line in (self.proc / str(pid) / 'status').read_text().splitlines() if ':' in line)
        return {'env': env, 'config': config, 'uid': (self.proc / str(pid)).stat().st_uid, 'gid': int(status['Gid'].split()[1]), 'groups': sorted(map(int, status['Groups'].split()))}

    def discover(self):
        pm2 = self.processes()
        require(pm2 and all(row.get('pid', 0) > 0 and (row.get('pm2_env') or {}).get('status') == 'online' for row in pm2), 'Every target PM2 worker must be online for discovery')
        require(all(not (row.get('pm2_env') or {}).get('watch') and not (row.get('pm2_env') or {}).get('cron_restart') for row in pm2), 'PM2 watch or cron restart must be disabled before this financial deployment')
        service = self.service_state()
        require(service.get('ActiveState') == 'active' and int(service.get('MainPID', '0')) > 0, 'Webstore must be active for configuration discovery')
        require(Path(service.get('WorkingDirectory', '')).resolve() == self.root, 'Webstore service working directory differs from target')
        require(service.get('KillMode') in ('control-group', 'mixed'), 'Webstore unit must stop its entire process group')
        require(not service.get('TriggeredBy'), 'Webstore has external activation triggers requiring a separate shutdown plan')
        roots = [row['pid'] for row in pm2] + [int(service['MainPID'])]
        self.old_processes = process_tree(roots, self.proc)
        allowed = set(self.old_processes)
        require(set(roots) <= allowed, 'A runtime process disappeared during discovery')
        self.assert_no_unmanaged(allowed)
        for kind, owners in [('bot', [row['pid'] for row in pm2]), ('web', [int(service['MainPID'])])]:
            contexts = []
            for owner in owners:
                group = process_tree([owner], self.proc)
                python_workers = []
                for pid, record in group.items():
                    argv = (self.proc / str(pid) / 'cmdline').read_bytes().split(b'\0')
                    if argv and b'python' in Path(os.fsdecode(argv[0])).name.encode():
                        python_workers.append(record)
                require(python_workers, 'No Python worker found for runtime configuration')
                contexts.extend(self.configuration(kind, record) for record in python_workers)
            configs = [row['config'] for row in contexts]
            require(all(config == configs[0] for config in configs), 'Workers disagree about runtime configuration')
            require(len({(row['uid'], row['gid'], tuple(row['groups'])) for row in contexts}) == 1, 'Workers use different Unix identities')
            self.contexts[kind] = contexts[0]
        require(self.contexts['bot']['config']['secret'] and self.contexts['bot']['config']['secret'] == self.contexts['web']['config']['secret'], 'Bridge secrets are missing or inconsistent')
        bot, web = self.contexts['bot']['config'], self.contexts['web']['config']
        bridge = urlparse(web['bridge_url'])
        addresses = {'127.0.0.1', '::1'}
        addresses |= {row[4][0] for row in socket.getaddrinfo(socket.gethostname(), None)}
        resolved = {row[4][0] for row in socket.getaddrinfo(bridge.hostname, None)}
        if not resolved <= addresses:
            interfaces = json.loads(execute(['ip', '-j', 'address', 'show']))
            addresses |= {address['local'] for interface in interfaces for address in interface.get('addr_info', []) if address.get('local')}
        require(bridge.scheme == 'http' and bridge.port == bot['port'] and bridge.path.rstrip('/') == bot['prefix'].rstrip('/') and resolved and resolved <= addresses and not bridge.username and not bridge.password and not bridge.query, 'Webstore bridge must directly address this host and bot port')
        self.database_paths = [Path(context['config']['path']) for context in self.contexts.values()]
        return {kind: context['config']['path'] for kind, context in self.contexts.items()}

    def assert_no_unmanaged(self, allowed):
        for pid, row in process_tree(None, self.proc).items():
            if row['cwd'] != str(self.root) or pid in allowed or pid == os.getpid():
                continue
            require(False, 'Unmanaged runtime process detected')

    def assert_databases_closed(self):
        identities = set()
        for path in self.database_paths:
            for name in (str(path), str(path) + '-wal', str(path) + '-shm', str(path) + '-journal'):
                if Path(name).exists():
                    stat = Path(name).stat()
                    identities.add((stat.st_dev, stat.st_ino))
        for directory in self.proc.iterdir():
            if not directory.name.isdigit():
                continue
            try:
                for descriptor in (directory / 'fd').iterdir():
                    try:
                        stat = descriptor.stat()
                        require((stat.st_dev, stat.st_ino) not in identities, 'A process still has a production SQLite file open')
                    except FileNotFoundError:
                        pass
            except FileNotFoundError:
                pass

    def quiescent(self):
        self.assert_no_unmanaged(set())
        self.assert_databases_closed()
        require(bool(self.processes()) and all(not row.get('pid') and (row.get('pm2_env') or {}).get('status') == 'stopped' and not (row.get('pm2_env') or {}).get('watch') and not (row.get('pm2_env') or {}).get('cron_restart') for row in self.processes()), 'PM2 workers are not fully stopped')
        state = self.service_state()
        require(state.get('ActiveState') == 'inactive' and state.get('MainPID') == '0', 'Webstore scheduler is still running')
        for pid, record in self.old_processes.items():
            try:
                require(proc_record(pid, self.proc)['start'] != record['start'], 'An old worker or descendant is still alive')
            except (FileNotFoundError, ProcessLookupError):
                pass
        group = state.get('ControlGroup')
        if group:
            cgroup = Path('/sys/fs/cgroup') / group.lstrip('/')
            if cgroup.exists():
                require(not any(path.read_text().strip() for path in cgroup.rglob('cgroup.procs')), 'Webstore cgroup still contains processes')

    def stop(self):
        current = [row['pid'] for row in self.processes() if row.get('pid', 0) > 0]
        service_pid = int(self.service_state().get('MainPID', '0'))
        if service_pid:
            current.append(service_pid)
        self.old_processes.update(process_tree(current, self.proc))
        errors = []
        for command in [['systemctl', 'stop', self.service], ['pm2', 'stop', self.pm2_name, '--watch']]:
            try:
                execute(command, timeout=60)
            except Exception:
                errors.append(command[0])
        deadline = time.monotonic() + 30
        while True:
            try:
                self.quiescent()
                require(not errors, 'Stop command failed; verify all workers manually')
                return
            except SafetyError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.5)

    def verify_configuration(self):
        for kind, context in self.contexts.items():
            current = json.loads(self.call_helper('_config', kind, self.root, env=context['env']))
            require(current == context['config'], 'Candidate changes production database or bridge configuration')

    def lock_probe(self, probe, uid):
        require(uid == os.geteuid() or os.geteuid() == 0, 'Cannot verify Webstore Unix flock permissions')
        def identity():
            if uid != os.geteuid():
                os.setgroups(self.contexts['web']['groups'])
                os.setgid(self.contexts['web']['gid'])
                os.setuid(uid)
        code = "import fcntl,sys\nf=open(sys.argv[1],'r+b')\ntry:\n fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB)\nexcept BlockingIOError:\n sys.exit(3)\n"
        return subprocess.run([self.python, '-c', code, str(probe)], capture_output=True, preexec_fn=identity)

    def migrate(self, root=None, copies=None):
        for kind, context in self.contexts.items():
            env = dict(context['env'])
            if copies:
                env.update(DATABASE_URL='sqlite+aiosqlite:///' + str(copies['bot']), WEBSTORE_DB=str(copies['web']), WEBSTORE_ENV_FILE=str(Path(root) / '.deploy-empty-env'))
            self.call_helper('_migrate', kind, root or self.root, env=env, root=root, timeout=180)

    def start_bot(self):
        execute(['pm2', 'start', self.pm2_name, '--update-env'], timeout=60)

    def start_web(self):
        execute(['systemctl', 'start', self.service], timeout=60)

    def assert_listener(self, kind):
        roots = [row['pid'] for row in self.processes() if row.get('pid', 0) > 0] if kind == 'bot' else [int(self.service_state().get('MainPID', '0'))]
        require(roots and all(roots), 'No new worker owns the service')
        workers = process_tree(roots, self.proc)
        require(set(roots) <= set(workers), 'New worker disappeared')
        port = self.contexts[kind]['config']['port']
        listeners = set()
        for protocol in ('tcp', 'tcp6'):
            for line in (self.proc / 'net' / protocol).read_text().splitlines()[1:]:
                fields = line.split()
                if fields[3] == '0A' and int(fields[1].rsplit(':', 1)[1], 16) == port:
                    listeners.add(fields[9])
        owned = set()
        for pid in workers:
            for descriptor in (self.proc / str(pid) / 'fd').iterdir():
                try:
                    match = re.fullmatch(r'socket:\[(\d+)\]', os.readlink(descriptor))
                    if match:
                        owned.add(match.group(1))
                except FileNotFoundError:
                    pass
        require(listeners and listeners <= owned, 'Health port is not owned by the new target workers')

    def health(self, kind, require_status=True):
        self.assert_listener(kind)
        roots = [row['pid'] for row in self.processes() if row.get('pid', 0) > 0] if kind == 'bot' else [int(self.service_state().get('MainPID', '0'))]
        observed = []
        for pid, record in process_tree(roots, self.proc).items():
            command = (self.proc / str(pid) / 'cmdline').read_bytes().split(b'\0')[0]
            if b'python' in command:
                observed.append(self.configuration(kind, record))
        require(observed and all(row['config'] == self.contexts[kind]['config'] and row['uid'] == self.contexts[kind]['uid'] for row in observed), 'New workers changed database, bridge, or Unix identity')
        if kind == 'bot':
            rows = self.processes()
            require(rows and all((row.get('pm2_env') or {}).get('status') == 'online' and row.get('pid', 0) > 0 for row in rows), 'Not all new PM2 workers are online')
            config = self.contexts['bot']['config']
            base = f"http://127.0.0.1:{config['port']}" + config['prefix'].rstrip('/')
            headers = {'X-Internal-Secret': config['secret']}
            suffixes = ['/internal/balance-config']
            if require_status:
                suffixes.append('/internal/balance-credit-status?source_id=deploy_health_' + os.urandom(12).hex())
            for suffix in suffixes:
                with urlopen(Request(base + suffix, headers=headers), timeout=5) as response:
                    data = json.load(response)
                    require(response.status == 200, 'Bridge health HTTP failure')
                if 'credit-status' in suffix:
                    require(data == {'ok': True, 'found': False, 'completed': False, 'ambiguous': False}, 'Bridge status contract failed')
                else:
                    require(float(data.get('daily_charge_rub', 0)) > 0, 'Bridge configuration health failed')
        else:
            require(self.service_state().get('ActiveState') == 'active', 'Webstore service did not start')
            port = self.contexts['web']['config']['port']
            with urlopen(f'http://127.0.0.1:{port}/api/store/auth/me', timeout=5) as response:
                require(json.load(response).get('authenticated') is False, 'Webstore authentication health failed')

    def wait_health(self, kind, require_status=True):
        deadline = time.monotonic() + 30
        while True:
            try:
                self.health(kind, require_status=require_status)
                return
            except Exception:
                if time.monotonic() >= deadline:
                    raise SafetyError(kind + ': health checks did not pass') from None
                time.sleep(1)

    def smoke(self, paths, qa):
        web = self.contexts['web']['config']
        base = f"http://127.0.0.1:{web['port']}"
        if qa.get('contact') and qa.get('password') and qa.get('telegram_id'):
            return run_smoke(base, web['secret'], paths, qa)
        return run_readonly_smoke(base, web['secret'])


class Deployment:
    def __init__(self, host, archive, revision, qa, foreign_key_allowlist=None):
        self.host, self.root = host, host.root
        self.archive, self.revision, self.qa = Path(archive), revision, qa
        self.foreign_key_allowlist = foreign_key_allowlist
        self.backup = None
        self.paths = {}
        self.manifest = {}
        self.started = False
        self.switched = False

    def journal(self, phase):
        self.manifest['phase'] = phase
        self.manifest['candidate_started'] = self.started
        if self.backup:
            target = self.backup / 'manifest.json'
            temporary = self.backup / ('manifest-' + os.urandom(6).hex() + '.new')
            with temporary.open('x') as handle:
                os.chmod(temporary, 0o600)
                json.dump(self.manifest, handle, sort_keys=True)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, target)
            descriptor = os.open(self.backup, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)

    def rollback_before_start(self):
        require(not self.started and self.backup is not None, 'Manual recovery required after candidate workers may have processed operations')
        self.host.quiescent()
        require(file_sha256(self.backup / 'code.tar.gz') == self.manifest['code_sha256'], 'Rollback code backup checksum failed')
        verify_preserved(self.paths, self.manifest['databases'])
        for kind, path in self.paths.items():
            inspect_database(
                path,
                kind,
                foreign_key_allowlist=self.foreign_key_allowlist if kind == 'bot' else None,
                foreign_key_parent_keys=BOT_FOREIGN_KEY_PARENT_KEYS if kind == 'bot' and self.foreign_key_allowlist else None,
            )
        require(all(schema_is_additive(self.manifest['databases'][kind]['schema'], path) for kind, path in self.paths.items()), 'Rollback SQLite schema is not backward compatible')
        clear_code(self.root, self.manifest['protected_paths'])
        install_archive(self.backup / 'code.tar.gz', self.root, self.manifest['protected_paths'])
        verify_locks(self.manifest['locks'])
        self.host.verify_configuration()
        self.host.start_bot()
        self.host.wait_health('bot', require_status=False)
        self.host.start_web()
        self.host.wait_health('web')
        self.journal('rolled_back_code_without_restoring_databases')

    def preflight(self):
        require(re.fullmatch('[0-9a-f]{40}', self.revision), 'Full deployment SHA is required')
        revision_file = self.root / 'REVISION'
        require(revision_file.is_file() and re.fullmatch('[0-9a-f]{7,40}', revision_file.read_text().strip()), 'Current runtime revision is missing or invalid')
        backup_root = self.root / 'backups'
        require(not backup_root.is_symlink(), 'Backup root must not be a symlink')
        if backup_root.exists():
            stat = backup_root.stat()
            require(stat.st_uid == os.geteuid() and not stat.st_mode & 0o022, 'Backup root has unsafe ownership or write permissions')
        require(not (backup_root / '.financial-deploy.lock').is_symlink(), 'Deployment lock must not be a symlink')
        with tarfile.open(self.archive, 'r:gz') as archive:
            require(archive.pax_headers.get('comment') == self.revision, 'Archive does not identify the exact deployment commit')
        paths = self.host.discover()
        qa_fields = [self.qa.get('contact'), self.qa.get('password'), self.qa.get('telegram_id')]
        qa_ready = bool(all(qa_fields) and isinstance(self.qa.get('telegram_id'), int) and self.qa['telegram_id'] > 0)
        require(not any(qa_fields) or qa_ready, 'Dedicated QA configuration is incomplete')
        if qa_ready:
            preflight_qa(paths, self.qa)
        protected_paths = list(paths.values()) + [context['config']['lexical_path'] for context in self.host.contexts.values()]
        protected_paths += [name for context in self.host.contexts.values() for name in context['config'].get('environment_files', [])]
        require(not os.path.samefile(paths['bot'], paths['web']), 'Production databases must be distinct')
        validate_archive(self.archive, self.root, protected_paths)
        checks = {
            kind: inspect_database(
                path,
                kind,
                foreign_key_allowlist=self.foreign_key_allowlist if kind == 'bot' else None,
                foreign_key_parent_keys=BOT_FOREIGN_KEY_PARENT_KEYS if kind == 'bot' and self.foreign_key_allowlist else None,
            )
            for kind, path in paths.items()
        }
        directory = Path(paths['web']).parent / '.webstore-profile-locks'
        require(not directory.is_symlink(), 'Profile flock directory must not be a symlink')
        if directory.exists():
            stat = directory.stat()
            require(
                directory.is_dir()
                and stat.st_uid == self.host.contexts['web']['uid']
                and stat.st_mode & 0o700 == 0o700
                and not stat.st_mode & 0o022,
                'Webstore Unix user cannot safely share the flock directory',
            )
            for lock_file in directory.iterdir():
                lock_stat = lock_file.stat()
                require(
                    lock_file.is_file()
                    and not lock_file.is_symlink()
                    and lock_stat.st_uid == self.host.contexts['web']['uid']
                    and lock_stat.st_mode & 0o200
                    and not lock_stat.st_mode & 0o022,
                    'Profile flock file has unsafe ownership or permissions',
                )
        return {'revision': self.revision, 'databases': paths, 'indexes': checks, 'flock_directory': str(directory), 'qa_ready': qa_ready, 'smoke_mode': 'qa' if qa_ready else 'readonly', 'ready_for_approval': True}

    def run(self):
        require(self.root.is_absolute() and len(self.root.parts) >= 4, 'Unsafe application root')
        require(re.fullmatch('[0-9a-f]{40}', self.revision), 'Full deployment SHA is required')
        require(self.preflight()['ready_for_approval'], 'Dedicated smoke credentials are required before stopping production')
        backup_root = self.root / 'backups'
        backup_root.mkdir(mode=0o700, exist_ok=True)
        with (backup_root / '.financial-deploy.lock').open('a+b') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise SafetyError('Another deployment is running') from None
            self.paths = self.host.discover()
            if self.qa.get('contact') and self.qa.get('password') and self.qa.get('telegram_id'):
                preflight_qa(self.paths, self.qa)
            protected_paths = self.paths.values()
            lexical = [context['config']['lexical_path'] for context in self.host.contexts.values()]
            lexical += [name for context in self.host.contexts.values() for name in context['config'].get('environment_files', [])]
            protected_paths = sorted(set(map(str, protected_paths)) | set(lexical))
            require(not os.path.samefile(self.paths['bot'], self.paths['web']), 'Production database paths refer to the same file')
            for kind, path in self.paths.items():
                inspect_database(
                    path,
                    kind,
                    foreign_key_allowlist=self.foreign_key_allowlist if kind == 'bot' else None,
                    foreign_key_parent_keys=BOT_FOREIGN_KEY_PARENT_KEYS if kind == 'bot' and self.foreign_key_allowlist else None,
                )
            stage = Path(tempfile.mkdtemp(prefix='darimiru-stage-')).resolve()
            try:
                validate_archive(self.archive, self.root, protected_paths)
                install_archive(self.archive, stage)
                require((stage / 'bot').is_dir() and (stage / 'webstore').is_dir(), 'Candidate archive is incomplete')
                require((self.root / 'requirements.txt').read_bytes() == (stage / 'requirements.txt').read_bytes(), 'Requirements changed; prepare a separate validated virtualenv')
                self.host.stop()
                self.host.quiescent()
                self.backup = backup_root / ('financial-' + self.revision + '-' + os.urandom(6).hex())
                snapshots = coordinated_backup(self.paths, self.backup)
                lock_dir = Path(self.paths['web']).parent / '.webstore-profile-locks'
                require(not lock_dir.is_symlink(), 'Profile flock directory must not be a symlink')
                created = not lock_dir.exists()
                lock_dir.mkdir(mode=0o700, exist_ok=True)
                uid = self.host.contexts['web']['uid']
                if created and uid != os.geteuid():
                    require(os.geteuid() == 0, 'Cannot create flock directory for Webstore Unix identity')
                    os.chown(lock_dir, uid, -1)
                assert_locks_idle(lock_dir)
                probe = lock_dir / '.deployment-probe'
                new_probe = not probe.exists()
                with probe.open('a+b') as held:
                    if new_probe and uid != os.geteuid():
                        os.chown(probe, uid, -1)
                    fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    result = self.host.lock_probe(probe, uid)
                    require(result.returncode == 3, 'Cross-process flock exclusion failed')
                self.manifest = {'revision': self.revision, 'databases': snapshots, 'protected_paths': protected_paths, 'locks': {**lock_inventory(self.root), **lock_inventory(lock_dir)}, 'previous_revision': (self.root / 'REVISION').read_text().strip()}
                recovery = self.backup / 'recovery' / 'scripts'
                recovery.mkdir(parents=True, mode=0o700)
                for name in ['darimiru_deploy_runner.py', 'darimiru_deploy_safety.py', 'smoke_darimiru_financial.py']:
                    shutil.copy2(Path(__file__).parent / name, recovery / name)
                backup_code(self.root, self.backup / 'code.tar.gz', protected_paths)
                self.manifest['code_sha256'] = file_sha256(self.backup / 'code.tar.gz')
                self.journal('backed_up_and_stopped')
                copies = {}
                rehearsal = stage / '.deploy-rehearsal'
                rehearsal.mkdir(mode=0o700)
                for kind, snapshot in snapshots.items():
                    copies[kind] = rehearsal / (kind + '.sqlite3')
                    with contextlib.closing(readonly(snapshot['backup'])) as source, contextlib.closing(sqlite3.connect(copies[kind])) as target:
                        source.backup(target)
                (stage / '.deploy-empty-env').touch(mode=0o600)
                self.host.migrate(stage, copies)
                for kind, path in copies.items():
                    inspect_database(
                        path,
                        kind,
                        require_indexes=True,
                        foreign_key_allowlist=self.foreign_key_allowlist if kind == 'bot' else None,
                        foreign_key_parent_keys=BOT_FOREIGN_KEY_PARENT_KEYS if kind == 'bot' and self.foreign_key_allowlist else None,
                    )
                verify_preserved(copies, snapshots)
                require(all(schema_is_additive(snapshots[kind]['schema'], path) for kind, path in copies.items()), 'Rehearsal found an incompatible SQLite migration')
                self.host.quiescent()
                self.switched = True
                self.journal('switching_code')
                clear_code(self.root, protected_paths)
                install_archive(self.archive, self.root, protected_paths)
                (self.root / 'REVISION').write_text(self.revision + '\n')
                verify_locks(self.manifest['locks'])
                self.host.verify_configuration()
                execute([self.host.python, '-m', 'compileall', '-q', 'bot', 'webstore'], cwd=self.root)
                self.host.migrate()
                for kind, path in self.paths.items():
                    inspect_database(
                        path,
                        kind,
                        require_indexes=True,
                        foreign_key_allowlist=self.foreign_key_allowlist if kind == 'bot' else None,
                        foreign_key_parent_keys=BOT_FOREIGN_KEY_PARENT_KEYS if kind == 'bot' and self.foreign_key_allowlist else None,
                    )
                    require(schema_is_additive(snapshots[kind]['schema'], path), 'Live SQLite migration is not backward compatible')
                verify_preserved(self.paths, snapshots)
                self.host.quiescent()
                self.journal('migrated_daily_settings_preserved')
                self.started = True
                self.journal('starting_bot')
                self.host.start_bot()
                self.host.wait_health('bot')
                inspect_database(
                    self.paths['bot'],
                    'bot',
                    require_indexes=True,
                    foreign_key_allowlist=self.foreign_key_allowlist,
                    foreign_key_parent_keys=BOT_FOREIGN_KEY_PARENT_KEYS if self.foreign_key_allowlist else None,
                )
                self.journal('starting_webstore')
                self.host.start_web()
                self.host.wait_health('web')
                inspect_database(self.paths['web'], 'web', require_indexes=True)
                for kind, path in self.paths.items():
                    require(daily_snapshot(path, kind, active_only=True, configuration_only=True) == snapshots[kind]['active_daily'], 'Existing active daily settings changed at service startup')
                self.host.smoke(self.paths, self.qa)
                for kind, path in self.paths.items():
                    require(daily_snapshot(path, kind, active_only=True, configuration_only=True) == snapshots[kind]['active_daily'], 'Existing active daily settings changed during smoke')
                verify_locks(self.manifest['locks'])
                require((self.root / 'REVISION').read_text().strip() == self.revision, 'Runtime revision changed')
                self.journal('complete')
                return {'revision': self.revision, 'backup': str(self.backup), 'complete': True}
            except BaseException:
                if self.started:
                    try:
                        self.host.stop()
                    finally:
                        self.journal('manual_recovery_required_no_database_restore')
                elif self.switched or self.manifest.get('code_sha256'):
                    try:
                        self.rollback_before_start()
                    except BaseException:
                        try:
                            self.host.stop()
                        finally:
                            self.journal('manual_recovery_required_no_database_restore')
                elif self.backup:
                    self.journal('stopped_before_switch')
                print(json.dumps({'backup': str(self.backup) if self.backup else None, 'phase': self.manifest.get('phase', 'preflight_or_stop_failed'), 'database_restore_performed': False}), file=sys.stderr)
                raise
            finally:
                shutil.rmtree(stage)



def recovery_status(backup):
    backup = Path(backup).resolve(strict=True)
    require(backup.parent.name == 'backups', 'Recovery directory must be an application backup')
    manifest = json.loads((backup / 'manifest.json').read_text())
    require(file_sha256(backup / 'code.tar.gz') == manifest['code_sha256'], 'Recovery code checksum failed')
    paths = {kind: item['path'] for kind, item in manifest['databases'].items()}
    for item in manifest['databases'].values():
        saved = Path(item['backup']).resolve(strict=True)
        require(saved.parent == backup, 'Database backup escaped the recovery directory')
        require(file_sha256(saved) == item['sha256'], 'Database backup checksum failed')
    unchanged = True
    try:
        verify_preserved(paths, manifest['databases'])
        verify_locks(manifest['locks'])
    except SafetyError:
        unchanged = False
    additive = all(schema_is_additive(manifest['databases'][kind]['schema'], path) for kind, path in paths.items())
    return {
        'revision': manifest['revision'], 'previous_revision': manifest['previous_revision'],
        'candidate_may_have_processed_operations': manifest['candidate_started'],
        'financial_state_unchanged': unchanged, 'schema_backward_compatible': additive,
        'code_only_rollback_eligible': not manifest['candidate_started'] and unchanged and additive,
        'automatic_database_restore_allowed': False,
    }

def main():
    mode = sys.argv[1]
    if mode == 'recovery-check':
        require(len(sys.argv) == 3, 'Expected recovery backup directory')
        print(json.dumps(recovery_status(sys.argv[2])))
        return
    if mode == '_config':
        print(json.dumps(probe_config(sys.argv[2], Path(sys.argv[3]))))
        return
    if mode == '_migrate':
        migrate(sys.argv[2], Path(sys.argv[3]))
        return
    if mode == '_lock_probe':
        with Path(sys.argv[2]).open('r+b') as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise SystemExit(3)
        return
    if mode in ('emit-config', 'emit-preflight-config', 'check-config'):
        raw = {
            'contact': os.getenv('DARIMIRU_SMOKE_CONTACT', '').strip(),
            'password': os.getenv('DARIMIRU_SMOKE_PASSWORD', ''),
            'telegram_id': os.getenv('DARIMIRU_SMOKE_TELEGRAM_ID', '').strip(),
        }
        if not any(raw.values()):
            qa = {}
        else:
            require(all(raw.values()), 'Configure all DARIMIRU_SMOKE_CONTACT, DARIMIRU_SMOKE_PASSWORD and DARIMIRU_SMOKE_TELEGRAM_ID or leave all unset')
            try:
                telegram_id = int(raw['telegram_id'])
            except ValueError:
                raise SafetyError('DARIMIRU_SMOKE_TELEGRAM_ID must be a positive integer') from None
            require(telegram_id > 0, 'DARIMIRU_SMOKE_TELEGRAM_ID must be a positive integer')
            qa = {'contact': raw['contact'], 'password': raw['password'], 'telegram_id': telegram_id}
        if mode in ('emit-config', 'emit-preflight-config'):
            require(not sys.stdout.isatty(), 'Refusing to print QA credentials to a terminal')
            print(json.dumps(qa))
        return
    require(mode in ('run', 'preflight'), 'Unsupported deploy operation')
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', required=True)
    parser.add_argument('--python', required=True)
    parser.add_argument('--pm2-name', required=True)
    parser.add_argument('--service', required=True)
    parser.add_argument('--archive', required=True)
    parser.add_argument('--revision', required=True)
    args = parser.parse_args(sys.argv[2:])
    qa = json.load(sys.stdin)
    host = Host(args.root, args.python, args.pm2_name, args.service)
    deployment = Deployment(
        host,
        args.archive,
        args.revision,
        qa,
        foreign_key_allowlist=BOT_FOREIGN_KEY_ALLOWLIST,
    )
    if mode == 'run':
        def interrupted(signum, frame):
            raise SystemExit(128 + signum)
        signal.signal(signal.SIGTERM, interrupted)
        signal.signal(signal.SIGINT, interrupted)
        signal.signal(signal.SIGHUP, interrupted)
    result = deployment.preflight() if mode == 'preflight' else deployment.run()
    print(json.dumps(result))
    if mode == 'preflight' and not result['ready_for_approval']:
        raise SystemExit(2)


if __name__ == '__main__':
    try:
        main()
    except SafetyError as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(1)
    except Exception as error:
        print(f"Deployment aborted: {type(error).__name__}: {error}", file=sys.stderr)
        raise SystemExit(1)
