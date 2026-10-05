"""Subprocess behavior checks for the explicit v3 operator CLI."""
import json
import os
import stat
import subprocess
import sys
from pathlib import Path


REPO = Path(__file__).resolve().parents[2]


def _private_json(path, value):
    path.write_text(json.dumps(value), encoding='utf-8')
    path.chmod(0o600)
    return path


def _run(*args, cwd=REPO):
    return subprocess.run([sys.executable, '-m', 'archive.v3', *map(str, args)],
                          cwd=cwd, capture_output=True, text=True)


def test_cli_init_project_refresh_query_and_status(tmp_path):
    root = tmp_path / 'master'
    root.mkdir(mode=0o700)
    scope = _private_json(tmp_path / 'scope.json', {
        'account': {'talker': {'capture': True, 'query': True}}})
    auth = _private_json(tmp_path / 'auth-input.json', {'roles': {
        'collector': {'token': 'collector-secret'},
        'query': {'token': 'query-secret'}}})
    common = ('--root', root, '--archive-id', 'cli-fixture',
              '--allow-synthetic-guard')

    denied = _run('init', '--root', root, '--archive-id', 'cli-default-deny',
                  '--scope', scope)
    assert denied.returncode == 2
    assert not (root / '.v3_archive_identity.json').exists()

    initialized = _run('init', *common, '--scope', scope, '--auth-file', auth)
    assert initialized.returncode == 0, initialized.stderr
    saved_auth = json.loads((root / 'auth.json').read_text())
    assert saved_auth['roles']['collector']['token_sha256']
    assert 'collector-secret' not in (root / 'auth.json').read_text()
    assert stat.S_IMODE((root / 'auth.json').stat().st_mode) == 0o600

    status = _run('status', *common)
    assert status.returncode == 0, status.stderr
    assert json.loads(status.stdout)['commit_seq'] >= 0

    # The forced-command entry authenticates only its selected role. The
    # collector token cannot start a query-role session, and the query token
    # cannot authenticate to the collector endpoint.
    collector_frames = [
        {'v': 1, 'id': 1, 'op': 'hello', 'role': 'collector',
         'token': 'collector-secret', 'archive_id': 'cli-fixture'},
        {'v': 1, 'id': 2, 'op': 'query_manifest'},
    ]
    # _run is intentionally not used for the interactive session because
    # communicate(input=...) must preserve JSONL framing.
    server = subprocess.run(
        [sys.executable, '-m', 'archive.v3', 'serve', '--root', str(root),
         '--archive-id', 'cli-fixture', '--allow-synthetic-guard',
         '--role', 'collector'], cwd=REPO,
        input=''.join(json.dumps(f) + '\n' for f in collector_frames),
        capture_output=True, text=True)
    assert server.returncode == 0, server.stderr
    replies = [json.loads(line) for line in server.stdout.splitlines()]
    assert replies[0]['ok'] and replies[0]['result']['role'] == 'collector'
    assert not replies[1]['ok']
    denied_query = subprocess.run(
        [sys.executable, '-m', 'archive.v3', 'serve', '--root', str(root),
         '--archive-id', 'cli-fixture', '--allow-synthetic-guard',
         '--role', 'collector'], cwd=REPO,
        input=json.dumps({'v': 1, 'id': 1, 'op': 'hello',
                          'role': 'query', 'token': 'query-secret',
                          'archive_id': 'cli-fixture'}) + '\n',
        capture_output=True, text=True)
    assert denied_query.returncode == 0
    assert not json.loads(denied_query.stdout)['ok']

    policy = _private_json(tmp_path / 'policy.json', {
        'policy_version': 1,
        'rules': {'account': {'talker': {'query': True}}}})
    project = _run('project', *common, '--policy', policy)
    assert project.returncode == 0, project.stderr
    first = json.loads(project.stdout)
    assert first['archive_id'] == 'cli-fixture'
    # A second invocation is an explicit rebuild of the existing projection.
    refresh = _run('project', *common, '--policy', policy)
    assert refresh.returncode == 0, refresh.stderr
    second = json.loads(refresh.stdout)
    assert second['generation'] != first['generation']

    projection_root = root / 'projection'
    queried = _run('query', '--root', projection_root, '--archive-id',
                   'cli-fixture', '--allow-synthetic-guard', '--account',
                   'account', '--expected-epoch', '1')
    assert queried.returncode == 0, queried.stderr
    assert json.loads(queried.stdout)['sessions'] == []


def test_cli_query_rejects_master_root(tmp_path):
    result = _run('query', '--root', tmp_path / 'master', '--archive-id',
                  'a', '--allow-synthetic-guard', '--account', 'x',
                  '--expected-epoch', '1', '--master-root', str(tmp_path / 'master'))
    assert result.returncode == 2
    assert 'master_root_rejected' in result.stderr
