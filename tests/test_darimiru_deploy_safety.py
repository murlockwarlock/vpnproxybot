from __future__ import annotations

import contextlib
from datetime import datetime, timedelta
import fcntl
import hashlib
import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tarfile
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from bot.models import Base as BotBase, User, BalanceTopUp
from webstore.models import Base as WebBase, WebAccount, WebBalanceAccount
from webstore.routes import _hash_password
from scripts import darimiru_deploy_safety as safety
from scripts import deploy_guard
from scripts.darimiru_deploy_runner import Deployment, Host, sqlite_path
from scripts.smoke_darimiru_financial import preflight_qa

SHA = '0123456789abcdef0123456789abcdef01234567'
QA = {'contact': 'qa@example.test', 'password': 'qa-test-password', 'telegram_id': 123}


def add_indexes(path, kind):
    with sqlite3.connect(path) as connection:
        for name, (table, columns, predicate) in safety.INDEXES[kind].items():
            connection.execute(f'CREATE UNIQUE INDEX IF NOT EXISTS "{name}" ON "{table}" ({",".join(columns)}) WHERE {predicate}')


@pytest.fixture
def databases(tmp_path):
    paths = {'bot': tmp_path / 'bot.sqlite3', 'web': tmp_path / 'web.sqlite3'}
    for kind, base in [('bot', BotBase), ('web', WebBase)]:
        engine = create_engine('sqlite:///' + str(paths[kind]))
        base.metadata.create_all(engine)
        with Session(engine) as session:
            if kind == 'bot':
                session.add_all([
                    User(id=1, telegram_id=123, balance_rub=0, referral_balance=0),
                    User(id=2, telegram_id=456, balance_rub=100, balance_mode_enabled=True, balance_autodebit_enabled=True, next_daily_charge_at=datetime.utcnow() + timedelta(days=1)),
                ])
            else:
                session.add_all([
                    WebAccount(contact=QA['contact'], profile_token='qa-profile', password_hash=_hash_password(QA['password'])),
                    WebBalanceAccount(profile_token='qa-profile', ref_code='qa-ref', balance_rub=0),
                    WebBalanceAccount(profile_token='active-profile', ref_code='active-ref', balance_rub=80, balance_mode_enabled=1, balance_autodebit_enabled=1, next_daily_charge_at=datetime.utcnow() + timedelta(days=1)),
                ])
            session.commit()
        engine.dispose()
        add_indexes(paths[kind], kind)
    return paths



def test_backup_api_preserves_wal_and_financial_state(databases, tmp_path):
    writers = [sqlite3.connect(path) for path in databases.values()]
    try:
        for connection in writers:
            connection.execute('PRAGMA journal_mode=WAL')
        writers[0].execute('UPDATE users SET balance_rub=127 WHERE id=2')
        writers[0].commit()
        writers[1].execute("UPDATE web_balance_accounts SET balance_rub=93 WHERE profile_token='active-profile'")
        writers[1].commit()
        result = safety.coordinated_backup(databases, tmp_path / 'backup')
        for kind, snapshot in result.items():
            assert Path(snapshot['backup']).is_file()
            assert hashlib.sha256(Path(snapshot['backup']).read_bytes()).hexdigest() == snapshot['sha256']
            assert safety.financial_snapshot(snapshot['backup']) == snapshot['financial']
            assert safety.daily_snapshot(snapshot['backup'], kind) == snapshot['daily']
        safety.verify_preserved(databases, result)
    finally:
        for connection in writers:
            connection.close()


def test_missing_or_same_database_aborts_without_creating_empty_db(databases, tmp_path):
    missing = tmp_path / 'missing.sqlite3'
    with pytest.raises(FileNotFoundError):
        safety.coordinated_backup({'bot': missing, 'web': databases['web']}, tmp_path / 'backup')
    assert not missing.exists()
    with pytest.raises(safety.SafetyError):
        safety.coordinated_backup({'bot': databases['bot'], 'web': databases['bot']}, tmp_path / 'same')
    assert not (tmp_path / 'same').exists()


@pytest.mark.parametrize('kind', ['bot', 'web'])
def test_critical_index_definition_and_missing_index_fail_closed(databases, kind):
    name, (table, columns, _) = next(iter(safety.INDEXES[kind].items()))
    with sqlite3.connect(databases[kind]) as connection:
        connection.execute(f'DROP INDEX "{name}"')
    assert safety.inspect_database(databases[kind], kind)['missing_indexes'] == [name]
    with pytest.raises(safety.SafetyError, match='missing critical'):
        safety.inspect_database(databases[kind], kind, require_indexes=True)
    with sqlite3.connect(databases[kind]) as connection:
        connection.execute(f'CREATE UNIQUE INDEX "{name}" ON "{table}" ({columns[0]}) WHERE 1=1')
    with pytest.raises(safety.SafetyError, match='predicate'):
        safety.inspect_database(databases[kind], kind)


def test_historical_fk_allowlist_is_exact_and_fail_closed():
    baseline = list(safety.BOT_FOREIGN_KEY_ALLOWLIST)
    assert safety.validate_foreign_key_violations('bot', baseline, baseline) == sorted(baseline)
    with pytest.raises(safety.SafetyError, match='historical baseline'):
        safety.validate_foreign_key_violations('bot', baseline[:-1], baseline)
    with pytest.raises(safety.SafetyError, match='historical baseline'):
        safety.validate_foreign_key_violations('bot', baseline + [('users', 999, 'tariffs', 0)], baseline)
    with pytest.raises(safety.SafetyError, match='historical baseline'):
        safety.validate_foreign_key_violations('bot', baseline[:-1] + [('payments', 57, 'users', 0)], baseline)
    actual_keys = dict(safety.BOT_FOREIGN_KEY_PARENT_KEYS)
    actual_keys[('payments', 57, 'subscriptions', 0)] = (999,)
    with pytest.raises(safety.SafetyError, match='parent key changed'):
        safety.validate_foreign_key_violations(
            'bot',
            baseline,
            baseline,
            actual_parent_keys=actual_keys,
            expected_parent_keys=safety.BOT_FOREIGN_KEY_PARENT_KEYS,
        )


def test_identical_legacy_duplicates_block_preflight(databases):
    engine = create_engine('sqlite:///' + str(databases['bot']))
    with Session(engine) as session:
        for _ in range(2):
            session.add(BalanceTopUp(user_id=1, telegram_id=123, amount_rub=10, provider='web_balance_migration', provider_payment_id='web_migrate_duplicate', status='completed'))
        session.commit()
    engine.dispose()
    with pytest.raises(safety.SafetyError, match='duplicates'):
        safety.inspect_database(databases['bot'], 'bot')


@pytest.mark.parametrize("change", ["flag", "schedule", "balance"])
def test_daily_settings_or_money_mutation_is_detected(databases, tmp_path, change):
    snapshots = safety.coordinated_backup(databases, tmp_path / 'backup')
    with sqlite3.connect(databases['web']) as connection:
        column = {"flag": "balance_autodebit_enabled=0", "schedule": "next_daily_charge_at=NULL", "balance": "balance_rub=999"}[change]
        connection.execute("UPDATE web_balance_accounts SET " + column + " WHERE profile_token='active-profile'")
    with pytest.raises(safety.SafetyError, match='financial state|daily settings'):
        safety.verify_preserved(databases, snapshots)


def test_schema_rollback_accepts_additive_and_rejects_destructive_change(databases):
    before = safety.schema_snapshot(databases['web'])
    with sqlite3.connect(databases['web']) as connection:
        connection.execute('ALTER TABLE web_accounts ADD COLUMN nullable_extra TEXT')
    assert safety.schema_is_additive(before, databases['web'])
    with sqlite3.connect(databases['web']) as connection:
        connection.execute('ALTER TABLE web_accounts RENAME COLUMN password_hash TO lost_password')
    assert not safety.schema_is_additive(before, databases['web'])


def make_archive(path, members, revision=SHA):
    with tarfile.open(path, 'w:gz', format=tarfile.PAX_FORMAT, pax_headers={'comment': revision}) as archive:
        for name, content in members.items():
            data = content.encode()
            member = tarfile.TarInfo(name)
            member.size = len(data)
            archive.addfile(member, io.BytesIO(data))
    return path


@pytest.mark.parametrize('member', ['../outside', '/outside', '.env.webstore', '.webstore-profile-locks/token', 'bot/.webstore-profile-locks/token', 'data/users.sqlite3'])
def test_archive_cannot_overwrite_runtime_or_escape_root(tmp_path, member):
    root = tmp_path / 'app'
    root.mkdir()
    archive = make_archive(tmp_path / 'bad.tar.gz', {member: 'bad'})
    with pytest.raises(safety.SafetyError):
        safety.install_archive(archive, root)
    assert list(root.iterdir()) == []


def test_code_cleanup_and_install_preserve_flock_inodes_and_custom_db(tmp_path):
    root = tmp_path / 'app'
    root.mkdir()
    custom = root / 'nonstandard-money'
    custom.write_bytes(b'production-database')
    sidecars = [Path(str(custom) + suffix) for suffix in ['-wal', '-shm', '-journal']]
    for sidecar in sidecars:
        sidecar.write_bytes(b'preserved-sidecar')
    env = root / '.env.webstore'
    env.write_text('private')
    locks = [root / '.webstore-profile-locks' / 'one', root / 'bot/.webstore-profile-locks/two']
    for lock in locks:
        lock.parent.mkdir(parents=True)
        lock.touch()
    (root / 'obsolete.py').write_text('old')
    before = safety.lock_inventory(root)
    backup = tmp_path / 'code-backup.tar.gz'
    safety.backup_code(root, backup, [custom])
    safety.clear_code(root, [custom])
    archive = make_archive(tmp_path / 'candidate.tar.gz', {'bot/__init__.py': '', '.env.example': 'example', 'ops/nl-stack/data/.gitkeep': ''})
    safety.install_archive(archive, root, [custom])
    safety.verify_locks(before)
    assert custom.read_bytes() == b'production-database'
    assert all(sidecar.read_bytes() == b'preserved-sidecar' for sidecar in sidecars)
    assert env.read_text() == 'private'
    assert not (root / 'obsolete.py').exists()
    with tarfile.open(backup) as stored:
        assert stored.getnames() == ['obsolete.py']


def test_held_flock_in_independent_process_blocks_preflight(tmp_path):
    directory = tmp_path / '.webstore-profile-locks'
    directory.mkdir()
    lock = directory / 'profile'
    lock.touch()
    helper = "import fcntl,sys,time; f=open(sys.argv[1],'r+b'); fcntl.flock(f,fcntl.LOCK_EX); print('locked',flush=True); time.sleep(30)"
    process = subprocess.Popen([sys.executable, '-c', helper, str(lock)], stdout=subprocess.PIPE)
    try:
        assert process.stdout.readline() == b'locked\n'
        with pytest.raises(safety.SafetyError, match='still held'):
            safety.assert_locks_idle(directory)
    finally:
        process.kill()
        process.wait()
    safety.assert_locks_idle(directory)


def test_qa_requires_actual_dedicated_identity_and_correct_password(databases):
    preflight_qa(databases, QA)
    with pytest.raises(safety.SafetyError, match='credentials'):
        preflight_qa(databases, {**QA, 'password': 'wrong'})
    with pytest.raises(safety.SafetyError, match='missing or has an active'):
        preflight_qa(databases, {**QA, 'telegram_id': 999})
    with pytest.raises(safety.SafetyError, match='missing or has an active'):
        preflight_qa(databases, {**QA, 'telegram_id': 456})


class FakeHost:
    def __init__(self, root, paths, failure=None, mutate=False):
        self.root = root
        self.python = sys.executable
        self.helper = str(Path(__file__).resolve().parents[1] / 'scripts/darimiru_deploy_runner.py')
        self.events = []
        self.paths = paths
        self.failure = failure
        self.mutate = mutate
        self.running = True
        self.contexts = {kind: {'config': {'path': str(path), 'lexical_path': str(path)}, 'uid': os.geteuid()} for kind, path in paths.items()}

    def event(self, value):
        self.events.append(value)
        if self.failure == value:
            self.failure = None
            raise safety.SafetyError('Injected failure')

    def discover(self):
        self.event('discover')
        return self.paths

    def stop(self):
        self.event('stop')
        self.running = False

    def quiescent(self):
        self.event('quiescent')
        safety.require(not self.running, 'Old writers still alive')

    def lock_probe(self, probe, uid):
        return subprocess.run([self.python, self.helper, '_lock_probe', str(probe)], capture_output=True)

    def verify_configuration(self):
        self.event('configuration')

    def migrate(self, root=None, copies=None):
        self.event('rehearse' if copies else 'migrate')
        for kind, path in (copies or self.paths).items():
            add_indexes(path, kind)

    def start_bot(self):
        self.event('start_bot')
        self.running = True
        if self.mutate:
            with sqlite3.connect(self.paths['bot']) as connection:
                connection.execute('UPDATE users SET balance_rub=balance_rub+5 WHERE id=2')

    def start_web(self):
        self.event('start_web')

    def wait_health(self, kind, **kwargs):
        self.event('health_' + kind)

    def smoke(self, paths, qa):
        self.event('smoke')


@pytest.fixture
def deployment(tmp_path, databases):
    root = tmp_path / 'runtime' / 'app'
    root.mkdir(parents=True)
    (root / 'requirements.txt').write_text('')
    (root / 'REVISION').write_text('9008232\n')
    (root / 'old.py').write_text('original')
    locks = root / '.webstore-profile-locks'
    locks.mkdir()
    (locks / 'old-profile').touch()
    archive = make_archive(tmp_path / 'candidate.tar.gz', {'requirements.txt': '', 'bot/__init__.py': '', 'webstore/__init__.py': '', 'new.py': 'candidate'})
    host = FakeHost(root, databases)
    return Deployment(host, archive, SHA, QA), host


def test_deployment_sequence_and_active_settings_survive(deployment):
    deploy, host = deployment
    before = {kind: safety.daily_snapshot(path, kind) for kind, path in host.paths.items()}
    locks = safety.lock_inventory(host.root)
    result = deploy.run()
    assert result['complete'] and result['revision'] == SHA
    events = host.events
    assert events.index('stop') < events.index('rehearse') < events.index('migrate') < events.index('start_bot') < events.index('health_bot') < events.index('start_web') < events.index('health_web') < events.index('smoke')
    assert (host.root / 'REVISION').read_text().strip() == SHA
    assert not (host.root / 'old.py').exists()
    safety.verify_locks(locks)
    for kind, path in host.paths.items():
        assert safety.daily_snapshot(path, kind) == before[kind]
        safety.inspect_database(path, kind, require_indexes=True)
    manifest = json.loads((Path(result['backup']) / 'manifest.json').read_text())
    assert manifest['phase'] == 'complete'
    for db in manifest['databases'].values():
        assert Path(db['backup']).exists()


@pytest.mark.parametrize('phase', ['health_bot', 'health_web', 'smoke'])
def test_failure_after_start_halts_without_restoring_live_money(deployment, phase):
    deploy, host = deployment
    host.failure, host.mutate = phase, True
    with pytest.raises(safety.SafetyError):
        deploy.run()
    assert host.events[-1] == 'stop'
    assert not host.running
    with sqlite3.connect(host.paths['bot']) as connection:
        assert connection.execute('SELECT balance_rub FROM users WHERE id=2').fetchone()[0] == 105
    assert (host.root / 'new.py').exists() and not (host.root / 'old.py').exists()
    assert json.loads((deploy.backup / 'manifest.json').read_text())['phase'] == 'manual_recovery_required_no_database_restore'
    with pytest.raises(safety.SafetyError, match='Manual recovery'):
        deploy.rollback_before_start()


def test_prestart_failure_rolls_back_only_code_with_compatible_schema(deployment):
    deploy, host = deployment
    host.failure = 'configuration'
    with pytest.raises(safety.SafetyError):
        deploy.run()
    assert (host.root / 'old.py').read_text() == 'original'
    assert not (host.root / 'new.py').exists()
    assert (host.root / 'REVISION').read_text().strip() == '9008232'
    assert host.running
    assert json.loads((deploy.backup / 'manifest.json').read_text())['phase'] == 'rolled_back_code_without_restoring_databases'


def test_stop_failure_never_switches_code(deployment):
    deploy, host = deployment
    host.failure = 'stop'
    with pytest.raises(safety.SafetyError):
        deploy.run()
    assert (host.root / 'old.py').exists()
    assert 'start_bot' not in host.events and 'migrate' not in host.events


def test_archive_revision_mismatch_stops_before_services(deployment):
    deploy, host = deployment
    deploy.revision = 'a' * 40
    with pytest.raises(safety.SafetyError, match='exact deployment commit'):
        deploy.run()
    assert 'stop' not in host.events


def test_database_url_resolution_does_not_guess_or_create(tmp_path):
    path = tmp_path / 'custom-finances'
    path.touch()
    assert sqlite_path('sqlite+aiosqlite:///custom-finances', tmp_path, url=True)[1] == str(path)
    for value in ['sqlite+aiosqlite:///:memory:', 'postgresql://localhost/database']:
        with pytest.raises(safety.SafetyError):
            sqlite_path(value, tmp_path, url=True)
    with pytest.raises(safety.SafetyError):
        sqlite_path('sqlite+aiosqlite:///not-found', tmp_path, url=True)


def fake_proc(directory, pid, root, environment, ppid=1):
    process = directory / str(pid)
    process.mkdir(parents=True)
    (process / 'cwd').symlink_to(root, target_is_directory=True)
    (process / 'root').symlink_to('/', target_is_directory=True)
    fields = ['S', str(ppid)] + ['0'] * 17 + [str(pid * 100)]
    (process / 'stat').write_text(f'{pid} (python) ' + ' '.join(fields))
    (process / 'cmdline').write_bytes(b'python\0-m\0bot\0')
    (process / 'environ').write_bytes(b'\0'.join((key + '=' + value).encode() for key, value in environment.items()) + b'\0')
    (process / 'status').write_text('Gid:\t' + ' '.join([str(os.getegid())] * 4) + '\nGroups:\t' + ' '.join(map(str, os.getgroups())) + '\n')
    (process / 'fd').mkdir()


@pytest.fixture
def real_host_adapter(tmp_path, databases, monkeypatch):
    root = tmp_path / 'running' / 'app'
    root.mkdir(parents=True)
    for module in ['bot', 'webstore']:
        (root / module).mkdir()
        (root / module / '__init__.py').touch()
    (root / 'bot/config.py').write_text("import os\nfrom types import SimpleNamespace\nsettings=SimpleNamespace(database_url=os.environ['DATABASE_URL'],webstore_bridge_secret='test-secret',app_port=8080,webhook_path_prefix='/vpnbot')\n")
    (root / 'webstore/config.py').write_text("import os\nfrom pathlib import Path\nfrom types import SimpleNamespace\n_ENV_FILE=Path('config.private')\nsettings=SimpleNamespace(database_path=os.environ['WEBSTORE_DB'],bridge_shared_secret='test-secret',port=8900,referral_bridge_url='http://127.0.0.1:8080/vpnbot')\n")
    proc = tmp_path / 'proc'
    environment = {'DATABASE_URL': 'sqlite+aiosqlite:///' + str(databases['bot']), 'WEBSTORE_DB': str(databases['web'])}
    for pid in [10, 11, 20]:
        fake_proc(proc, pid, root, environment)
    host = Host(root, sys.executable, 'darimiru_bot', 'webstore-darimiru.service', proc=proc)
    monkeypatch.setattr(host, 'processes', lambda: [{'name': 'darimiru_bot', 'pid': pid, 'pm2_env': {'status': 'online'}} for pid in [10, 11]])
    monkeypatch.setattr(host, 'service_state', lambda: {'ActiveState': 'active', 'MainPID': '20', 'WorkingDirectory': str(root), 'KillMode': 'control-group'})
    monkeypatch.setattr('scripts.darimiru_deploy_runner.socket.getaddrinfo', lambda *args: [(None, None, None, None, ('127.0.0.1', 0))])
    return host, proc, environment


def test_paths_resolve_from_every_worker_effective_environment(real_host_adapter, databases):
    host, _, _ = real_host_adapter
    assert host.discover() == {kind: str(path) for kind, path in databases.items()}
    assert len(host.old_processes) == 3
    assert str(host.root / 'config.private') in host.contexts['web']['config']['environment_files']


def test_disagreeing_pm2_database_paths_abort(real_host_adapter, tmp_path):
    host, proc, env = real_host_adapter
    other = tmp_path / 'other.sqlite3'
    other.touch()
    (proc / '11/environ').write_bytes(b'\0'.join((key + '=' + value).encode() for key, value in {**env, 'DATABASE_URL': 'sqlite+aiosqlite:///' + str(other)}.items()))
    with pytest.raises(safety.SafetyError, match='disagree'):
        host.discover()


def test_unmanaged_python_writer_and_surviving_worker_abort(real_host_adapter, monkeypatch):
    host, proc, env = real_host_adapter
    fake_proc(proc, 99, host.root, env)
    with pytest.raises(safety.SafetyError, match='Unmanaged'):
        host.discover()
    import shutil
    shutil.rmtree(proc / '99')
    host.discover()
    monkeypatch.setattr(host, 'processes', lambda: [{'pid': 0, 'pm2_env': {'status': 'stopped'}}])
    monkeypatch.setattr(host, 'service_state', lambda: {'ActiveState': 'inactive', 'MainPID': '0'})
    with pytest.raises(safety.SafetyError):
        host.quiescent()
    for pid in [10, 11, 20]:
        shutil.rmtree(proc / str(pid))
    host.quiescent()


def test_health_port_cannot_belong_to_foreign_process(real_host_adapter):
    host, proc, _ = real_host_adapter
    host.discover()
    (proc / 'net').mkdir()
    (proc / 'net/tcp').write_text('header\n 0: 0100007F:1F90 00000000:0000 0A 0 0 0 0 0 444\n')
    (proc / 'net/tcp6').write_text('header\n')
    with pytest.raises(safety.SafetyError, match='not owned'):
        host.assert_listener('bot')
    (proc / '10/fd/5').symlink_to('socket:[444]')
    host.assert_listener('bot')


def test_recovery_check_forbids_automatic_database_restore(deployment):
    from scripts.darimiru_deploy_runner import recovery_status
    deploy, host = deployment
    deploy.run()
    result = recovery_status(deploy.backup)
    assert result['candidate_may_have_processed_operations']
    assert not result['code_only_rollback_eligible']
    assert result['financial_state_unchanged']
    assert not result['automatic_database_restore_allowed']


def test_existing_mode_disabled_at_startup_aborts_without_reset(deployment, monkeypatch):
    deploy, host = deployment
    original = host.start_bot
    def change_mode():
        original()
        with sqlite3.connect(host.paths['bot']) as connection:
            connection.execute('UPDATE users SET balance_autodebit_enabled=0 WHERE id=2')
    monkeypatch.setattr(host, 'start_bot', change_mode)
    with pytest.raises(safety.SafetyError, match='at service startup'):
        deploy.run()
    assert not host.running
    with sqlite3.connect(host.paths['bot']) as connection:
        assert connection.execute('SELECT balance_autodebit_enabled FROM users WHERE id=2').fetchone()[0] == 0
    assert json.loads((deploy.backup / 'manifest.json').read_text())['phase'] == 'manual_recovery_required_no_database_restore'


def test_corrupt_database_aborts_without_changes(databases):
    databases['web'].write_bytes(b'not a SQLite database')
    before = databases['web'].read_bytes()
    with pytest.raises(sqlite3.DatabaseError):
        safety.inspect_database(databases['web'], 'web')
    assert databases['web'].read_bytes() == before


def test_both_source_databases_are_write_locked_during_each_backup(databases, tmp_path, monkeypatch):
    original = safety.readonly
    probes = []
    class Source:
        def __init__(self, path):
            self.connection = original(path)
        def __getattr__(self, name):
            return getattr(self.connection, name)
        def backup(self, target):
            for kind, path in databases.items():
                with contextlib.closing(sqlite3.connect(path, timeout=0.01)) as contender:
                    with pytest.raises(sqlite3.OperationalError, match='locked'):
                        contender.execute('BEGIN IMMEDIATE')
                probes.append(kind)
            self.connection.backup(target)
    monkeypatch.setattr(safety, 'readonly', Source)
    safety.coordinated_backup(databases, tmp_path / 'backup')
    assert probes == ['bot', 'web', 'bot', 'web']


def test_stop_attempts_both_service_groups_even_if_one_command_fails(real_host_adapter, monkeypatch):
    host, _, _ = real_host_adapter
    commands = []
    def command(args, **kwargs):
        commands.append(args)
        if args[0] == 'systemctl':
            raise safety.SafetyError('stop failed')
        return ''
    monkeypatch.setattr('scripts.darimiru_deploy_runner.execute', command)
    monkeypatch.setattr(host, 'quiescent', lambda: None)
    ticks = iter([0, 31])
    monkeypatch.setattr('scripts.darimiru_deploy_runner.time.monotonic', lambda: next(ticks))
    with pytest.raises(safety.SafetyError, match='Stop command failed'):
        host.stop()
    assert [args[:2] for args in commands] == [['systemctl', 'stop'], ['pm2', 'stop']]


def test_real_git_archive_for_requested_sha_passes_runtime_validation(tmp_path):
    root = Path(__file__).resolve().parents[1]
    real_sha = subprocess.run(
        ['git', 'rev-parse', 'HEAD'], cwd=root, check=True, capture_output=True, text=True
    ).stdout.strip()
    archive = tmp_path / 'requested.tar.gz'
    subprocess.run(['git', 'archive', '--format=tar.gz', '--output=' + str(archive), real_sha], cwd=root, check=True)
    with tarfile.open(archive, 'r:gz') as opened:
        assert opened.pax_headers['comment'] == real_sha
    safety.validate_archive(archive, tmp_path / 'runtime')


def test_candidate_safety_contract_fails_closed(monkeypatch):
    payload = {
        path: '\n'.join(markers)
        for path, markers in deploy_guard.CANDIDATE_SAFETY_CONTRACT.items()
    }

    def fake_git(root, *args, check=True):
        path = args[1].split(':', 1)[1]
        return subprocess.CompletedProcess(
            args=['git', *args], returncode=0, stdout=payload[path], stderr=''
        )

    monkeypatch.setattr(deploy_guard, '_git', fake_git)
    assert deploy_guard.ensure_candidate_safety_contract('/repo', SHA) == SHA

    payload['bot/database.py'] = ''
    with pytest.raises(SystemExit) as error:
        deploy_guard.ensure_candidate_safety_contract('/repo', SHA)
    assert error.value.code == 2


def test_deploy_shell_refuses_execution_without_exact_production_confirmation(tmp_path):
    root = tmp_path / 'checkout'
    (root / 'scripts').mkdir(parents=True)
    source = Path(__file__).resolve().parents[1] / 'deploy_darimiru_prod.sh'
    (root / source.name).write_text(source.read_text())
    (root / 'scripts/deploy_guard.py').write_text(
        "def ensure_clean_git(root):\n"
        "    return '" + SHA + "'\n"
        "def ensure_candidate_safety_contract(root, revision):\n"
        "    return revision\n"
    )
    binaries = tmp_path / 'bin'
    binaries.mkdir()
    (binaries / 'git').write_text('#!/bin/sh\ncase "$1" in rev-parse) echo ' + SHA + ';; *) exit 2;; esac\n')
    marker = tmp_path / 'ssh-called'
    (binaries / 'ssh').write_text('#!/bin/sh\ntouch "' + str(marker) + '"\nexit 1\n')
    for file in binaries.iterdir():
        file.chmod(0o700)
    env = {**os.environ, 'PATH': str(binaries) + os.pathsep + os.environ['PATH']}
    result = subprocess.run(['bash', str(root / source.name), '--revision', SHA, '--expected-sha', SHA], env=env, capture_output=True, text=True)
    assert result.returncode != 0
    assert 'fresh --confirm-production' in result.stderr
    assert not marker.exists()


def test_shell_syntax_and_help_do_not_contact_production():
    script = Path(__file__).resolve().parents[1] / 'deploy_darimiru_prod.sh'
    subprocess.run(['bash', '-n', str(script)], check=True)
    result = subprocess.run(['bash', str(script), '--help'], capture_output=True, text=True, check=True)
    assert '--preflight-only' in result.stdout and '--confirm-production' in result.stdout


def test_partial_code_cleanup_failure_restores_old_code_before_restart(deployment, monkeypatch):
    deploy, host = deployment
    original = safety.clear_code
    calls = 0
    def fail_once(root, protected):
        nonlocal calls
        calls += 1
        if calls == 1:
            (root / 'old.py').unlink()
            raise OSError('cleanup interrupted')
        original(root, protected)
    monkeypatch.setattr('scripts.darimiru_deploy_runner.clear_code', fail_once)
    with pytest.raises(OSError):
        deploy.run()
    assert (host.root / 'old.py').read_text() == 'original'
    assert (host.root / 'REVISION').read_text().strip() == '9008232'
    assert host.running
    assert 'migrate' not in host.events


def test_failed_rehearsal_restarts_only_unchanged_old_version(deployment):
    deploy, host = deployment
    host.failure = 'rehearse'
    with pytest.raises(safety.SafetyError):
        deploy.run()
    assert (host.root / 'old.py').exists() and not (host.root / 'new.py').exists()
    assert host.running
    assert json.loads((deploy.backup / 'manifest.json').read_text())['phase'] == 'rolled_back_code_without_restoring_databases'


@pytest.mark.parametrize('setting', ['watch', 'cron_restart'])
def test_pm2_auto_restart_configuration_blocks_preflight(real_host_adapter, monkeypatch, setting):
    host, _, _ = real_host_adapter
    monkeypatch.setattr(host, 'processes', lambda: [{'pid': 10, 'pm2_env': {'status': 'online', setting: True}}])
    with pytest.raises(safety.SafetyError, match='watch or cron'):
        host.discover()


def test_external_service_activation_blocks_preflight(real_host_adapter, monkeypatch):
    host, _, _ = real_host_adapter
    monkeypatch.setattr(host, 'service_state', lambda: {'MainPID': '20', 'ActiveState': 'active', 'WorkingDirectory': str(host.root), 'KillMode': 'control-group', 'TriggeredBy': 'webstore.socket'})
    with pytest.raises(safety.SafetyError, match='activation triggers'):
        host.discover()


def test_custom_env_inside_code_directory_is_preserved_without_blocking_code(tmp_path):
    root = tmp_path / 'app'
    (root / 'webstore').mkdir(parents=True)
    private = root / 'webstore' / 'private-settings'
    private.write_text('private runtime configuration')
    (root / 'webstore/old.py').write_text('old')
    archive = make_archive(tmp_path / 'code.tar.gz', {'webstore/new.py': 'new'})
    safety.validate_archive(archive, root, [private])
    safety.clear_code(root, [private])
    safety.install_archive(archive, root, [private])
    assert private.read_text() == 'private runtime configuration'
    assert (root / 'webstore/new.py').read_text() == 'new'
    assert not (root / 'webstore/old.py').exists()


def test_readonly_preflight_allows_deploy_without_production_qa_mutations(deployment):
    deploy, host = deployment
    deploy.qa = {}
    result = deploy.preflight()
    assert result['databases'] == host.paths
    assert not result['qa_ready'] and result['ready_for_approval'] and result['smoke_mode'] == 'readonly'
    assert not (host.root / 'backups').exists()
    assert deploy.run()['complete']
    assert 'stop' in host.events


def test_payment_amount_mutation_is_not_ignored(databases):
    from bot.models import Payment, PaymentMethod
    engine = create_engine('sqlite:///' + str(databases['bot']))
    with Session(engine) as session:
        session.add(Payment(user_id=2, amount=250, method=PaymentMethod.STARS))
        session.commit()
    engine.dispose()
    before = safety.financial_snapshot(databases['bot'])
    with sqlite3.connect(databases['bot']) as connection:
        connection.execute('UPDATE payments SET amount=999')
    assert safety.financial_snapshot(databases['bot'], before) != before


@pytest.mark.parametrize('literal', ['PENDING', 'pen ding'])
def test_critical_index_literal_values_are_not_case_or_whitespace_folded(databases, literal):
    with sqlite3.connect(databases['web']) as connection:
        connection.execute('DROP INDEX uq_web_balance_transfers_profile_pending')
        connection.execute("CREATE UNIQUE INDEX uq_web_balance_transfers_profile_pending ON web_balance_transfers(profile_token) WHERE status='" + literal + "'")
    with pytest.raises(safety.SafetyError, match='predicate'):
        safety.inspect_database(databases['web'], 'web')
