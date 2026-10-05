"""Independent subprocess CLI gates over disposable synthetic storage only."""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from archive.tests.test_codex_collector_workflow import export_fixture
from archive.v3.util import canonical, sha256_hex
from archive.v3.wire import encode_frame, make_request


def invoke(command, *args, input=None):
    return subprocess.run([sys.executable, '-m', 'archive.v3', command, *map(str, args)],
                          input=input, capture_output=True, timeout=35)


def ok(result):
    assert result.returncode == 0, result.stderr.decode()
    return json.loads(result.stdout)


def private(path, value):
    path.write_text(canonical(value))
    path.chmod(0o600)
    return path


@pytest.fixture
def cli_archive(tmp_path):
    root = tmp_path / 'master'; root.mkdir(mode=0o700)
    scope = private(tmp_path / 'scope.json', {'account': {'talker': {'capture': True, 'query': True}}})
    raw = private(tmp_path / 'auth-input.json', {'roles': {
        'collector': {'token': 'synthetic-collector'}, 'query': {'token': 'synthetic-query'}}})
    token = private(tmp_path / 'token.json', {'token': 'synthetic-collector'})
    common = ['--root', root, '--archive-id', 'collector-fixture', '--allow-synthetic-guard']
    ok(invoke('init', *common, '--scope', scope, '--auth-file', raw))
    network = common + ['--auth-file', root / 'auth.json', '--token-file', token,
                        '--work-dir', tmp_path / 'client-work']
    return root, common, network


def test_production_default_and_missing_master_never_initialize(tmp_path):
    root = tmp_path / 'master'; root.mkdir()
    scope = private(tmp_path / 'scope', {'account': {'talker': {'capture': True}}})
    result = invoke('init', '--root', root, '--archive-id', 'default-deny', '--scope', scope)
    assert result.returncode != 0
    assert json.loads(result.stderr)['error']['code'] == 'guard_config_required'
    assert list(root.iterdir()) == []
    result = invoke('status', '--root', root, '--archive-id', 'default-deny', '--allow-synthetic-guard')
    assert result.returncode != 0
    assert list(root.iterdir()) == []


def test_collect_project_refresh_query_and_encrypted_restore(cli_archive, tmp_path):
    root, common, network = cli_archive
    export = export_fixture(tmp_path / 'export', 'CLI独立合法正文')
    ok(invoke('collect', *network, '--export', export))
    status = ok(invoke('status', *common))
    assert status['messages'] == 1
    policy = private(tmp_path / 'policy.json', {'policy_version': 1,
        'rules': {'account': {'talker': {'query': True}}}})
    first = ok(invoke('project', *common, '--policy', policy))
    second = ok(invoke('project', *common, '--policy', policy))
    assert first['generation'] != second['generation'], 'CLI cannot refresh a published projection'
    query_args = ['--root', root / 'projection', '--archive-id', 'collector-fixture',
                  '--allow-synthetic-guard', '--account', 'account', '--expected-epoch', 1,
                  '--talker', 'talker', '--match', 'CLI独立']
    found = ok(invoke('query', *query_args))
    assert found['count'] == 1
    assert 'raw_record_json' not in canonical(found)
    denied = invoke('query', '--root', root, *query_args[2:])
    assert denied.returncode != 0
    key = private(tmp_path / 'backup-key.json', {'key': 'synthetic-backup-passphrase'})
    backup = tmp_path / 'encrypted'
    ok(invoke('backup', *common, '--target', backup, '--key-file', key))
    for p in backup.rglob('*'):
        if p.is_file():
            assert 'CLI独立合法正文'.encode() not in p.read_bytes()
            assert not p.read_bytes().startswith(b'SQLite format 3')
    scratch = tmp_path / 'restore'
    restored = ok(invoke('restore-drill', *common, '--backup', backup,
                         '--scratch', scratch, '--key-file', key))
    assert restored['ok'] and restored['epoch'] > 1
    new = ok(invoke('status', '--root', scratch, '--archive-id', 'collector-fixture',
                    '--allow-synthetic-guard'))
    assert new['messages'] == 1 and new['epoch'] == restored['epoch']


def test_forced_role_rejects_other_valid_role_and_exit_code_is_preserved(cli_archive, tmp_path):
    root, common, network = cli_archive
    ok(invoke('collect', *network, '--export', export_fixture(tmp_path / 'export')))
    policy = private(tmp_path / 'policy', {'policy_version': 1,
                     'rules': {'account': {'talker': {'query': True}}}})
    ok(invoke('project', *common, '--policy', policy))
    auth = private(tmp_path / 'query-auth', json.loads((root / 'auth.json').read_text()))
    for role in ['collector', 'query']:
        other = 'query' if role == 'collector' else 'collector'
        frame = encode_frame(make_request(1, 'hello', role=other,
                             token='synthetic-' + other, archive_id='collector-fixture'))
        serve_root = root if role == 'collector' else root / 'projection'
        result = invoke('serve', '--root', serve_root, '--archive-id', 'collector-fixture',
                        '--allow-synthetic-guard', '--role', role, '--auth-file', auth,
                        '--account', 'account', '--expected-epoch', 1, input=frame)
        reply = json.loads(result.stdout)
        assert reply['ok'] is False, f'forced {role} admitted a valid {other} token'
    from archive.v3.wire import MAX_FRAME_BYTES
    result = invoke('serve', *common, '--role', 'collector', input=b'x' * (MAX_FRAME_BYTES + 2))
    assert result.returncode == 2, 'CLI discarded the server fatal/framing return code'
    assert json.loads(result.stdout)['error']['code'] == 'frame_too_large'


def test_upload_then_prepared_reconcile_reaches_real_commit(tmp_path, captured):
    from archive.v3.errors import ProtocolError
    store, _, _ = captured
    root = Path(store.binding.root)
    private(root / 'auth.json', store._codex_auth)
    token = private(tmp_path / 'token', {'token': 'synthetic-token'})
    common = ['--root', root, '--archive-id', 'restore-fixture', '--allow-synthetic-guard']
    net = common + ['--auth-file', root / 'auth.json', '--token-file', token,
                    '--work-dir', tmp_path / 'client-work']
    asset = tmp_path / 'standalone-asset'; asset.write_bytes(b'independent upload bytes')
    uploaded = ok(invoke('upload', *net, '--file', asset, '--sha256', sha256_hex(asset.read_bytes()),
                         '--length', asset.stat().st_size, '--batch-id', 'cli-independent-upload'))
    assert uploaded['upload']['sha256'] == sha256_hex(asset.read_bytes())
    collector = store._codex_collector
    next_export = tmp_path / 'next-view'
    def produce(claim):
        import shutil
        shutil.copytree(store._codex_export, next_export, dirs_exist_ok=True)
        p = next_export / 'records.jsonl'; row = json.loads(p.read_text())
        row['message_content'] = row['raw']['message_content'] = 'prepared CLI reconcile body'
        row.pop('record_sha256', None); row['record_sha256'] = sha256_hex(canonical(row).encode())
        p.write_text(canonical(row) + '\n')
    collector.claim_source_snapshot(next_export, 'account', 'talker', source_runner=produce)
    original = collector.client.transport.request
    pending = {}
    def hold_commit(frame):
        if frame['op'] == 'commit_batch':
            pending.update(json.loads(canonical(frame)))
            raise ProtocolError('transport_closed', 'synthetic disconnect before commit')
        return original(frame)
    collector.client.transport.request = hold_commit
    with pytest.raises(ProtocolError):
        collector.collect_session(next_export)
    collector.client.transport.request = original
    run = private(tmp_path / 'prepared-run', pending['run'])
    media = private(tmp_path / 'prepared-media', {'uploads': pending['media_uploads']})
    reply = ok(invoke('reconcile', *net, '--run-file', run, '--media-uploads-file', media,
                      '--batch-id', pending['batch_id']))
    assert reply['commit_seq'] > 0
    assert store.db.execute('SELECT content_text FROM messages').fetchone()[0] == 'prepared CLI reconcile body'


from archive.tests.test_codex_backup_gate import captured


def test_migration_command_preserves_actual_legacy_archive(cli_archive, tmp_path):
    from archive.store import Archive
    import hashlib
    root, common, network = cli_archive
    legacy_root = tmp_path / 'legacy'; legacy = Archive(legacy_root)
    export = private(tmp_path / 'legacy-export.json', {
        'items': [{'server_id': 101, 'talker': 'talker', 'create_time': 100, 'sort_seq': 1,
                   'sender': 'synthetic-sender', 'content': {'Text': 'CLI migration history'},
                   'snippet': 'CLI migration history', 'msg_type': 1, 'status': 0}],
        'media': {'version': 1, 'mode': 'enabled', 'expected': 0, 'available': 0,
                  'missing': 0, 'errors': 0, 'not_attempted': 0},
        'conversation': {'talker': 'talker', 'display_name': 'Synthetic', 'type': 'group', 'message_count': 1},
        'stats': {'skipped': 0}, 'paging': {'has_more': False, 'offset': 0, 'returned': 1}})
    legacy.import_export(export, 'account', expected_talker='talker', bounds=(0, 200))
    legacy.db.close()
    def hashes():
        return {str(p.relative_to(legacy_root)): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in legacy_root.rglob('*') if p.is_file()}
    before = hashes()
    ok(invoke('migrate-v2', *network, '--v2-root', legacy_root, '--account', 'account'))
    assert hashes() == before
    assert ok(invoke('status', *common))['messages'] == 1


def test_query_serve_refuses_master_auth_path(cli_archive, tmp_path):
    root, common, network = cli_archive
    ok(invoke('collect', *network, '--export', export_fixture(tmp_path / 'export')))
    policy = private(tmp_path / 'policy', {'policy_version': 1,
                     'rules': {'account': {'talker': {'query': True}}}})
    ok(invoke('project', *common, '--policy', policy))
    result = invoke('serve', '--root', root / 'projection', '--archive-id', 'collector-fixture',
                    '--allow-synthetic-guard', '--role', 'query', '--auth-file', root / 'auth.json',
                    '--account', 'account', '--expected-epoch', 1, input=b'')
    assert result.returncode != 0
    assert json.loads(result.stderr)['error']['code'] == 'query_auth_isolation'


def test_restore_does_not_silently_select_synthetic_guard(tmp_path):
    root = tmp_path / 'master'; root.mkdir()
    backup = tmp_path / 'backup'; backup.mkdir()
    scratch = tmp_path / 'restored'
    key = private(tmp_path / 'key', {'key': 'synthetic-passphrase'})
    result = invoke('restore-drill', '--root', root, '--archive-id', 'default-deny',
                    '--backup', backup, '--scratch', scratch, '--key-file', key)
    assert result.returncode != 0
    assert json.loads(result.stderr)['error']['code'] == 'production_restore_unverified'
    assert not scratch.exists()
