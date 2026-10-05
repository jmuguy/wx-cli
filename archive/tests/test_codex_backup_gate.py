"""Independent encrypted-backup restore tests over a captured synthetic asset."""
import json
import shutil
from pathlib import Path

import pytest

from archive.v3.collector import CaptureClient, Collector
from archive.v3.endpoint import Endpoint
from archive.v3.guard import SyntheticGuard
from archive.v3.master import MasterStore
from archive.v3.util import canonical, sha256_hex


@pytest.fixture
def captured(tmp_path):
    root = tmp_path / 'master'
    root.mkdir(mode=0o700)
    SyntheticGuard.write_marker(root, 'restore-fixture', 1, 0)
    binding = SyntheticGuard('restore-fixture').verify(root)
    store = MasterStore.initialize(binding, 'restore-fixture', {'account': {'talker': {'capture': True}}})
    ep = Endpoint(binding, 'restore-fixture'); ep.store = store
    ep.auth = {'roles': {'collector': {'token_sha256': sha256_hex(b'synthetic-token')}}}
    class Transport:
        def request(self, frame):
            if frame['op'] == 'commit_batch':
                store._codex_last_frame = json.loads(json.dumps(frame))
            return ep.handle_frame(frame)
    collector = Collector(CaptureClient(Transport(), 'restore-fixture', 'synthetic-token'), tmp_path / 'work')
    asset = b'synthetic independent attachment bytes'
    sha = sha256_hex(asset)
    export = tmp_path / 'export'; export.mkdir()
    (export / 'media').mkdir(); (export / 'media' / 'asset').write_bytes(asset)
    record = {'identity': {'shard': 'message_0.db', 'table': 'Msg_synthetic', 'local_rowid': 1, 'server_id': '101'},
        'create_time': 100, 'sort_seq': 1, 'local_type': 1, 'status': 0,
        'message_content': 'baseline source body', 'raw': {'message_content': 'baseline source body'},
        'media_refs': [{'ref_key': 'file:synthetic', 'kind': 'file', 'present': True,
                        'filename': 'media/asset', 'sha256': sha, 'length': len(asset)}]}
    record['record_sha256'] = sha256_hex(canonical(record).encode())
    manifest = {'contract': 'wx-archive.external-archive-records', 'version': 2, 'kind': 'session_export',
        'archive_id': 'restore-fixture', 'account': 'account', 'talker': 'talker',
        'snapshot': {'generation': 'fixture', 'created_at': 200,
                    'databases': [{'file': 'message_0.db', 'sha256': sha, 'bytes': 4096}]},
        'session': {'shards': [{'database': 'message_0.db', 'table': 'Msg_synthetic', 'row_count': 1}]},
        'enumeration': {'complete': True}, 'records': {'file': 'records.jsonl', 'count': 1}}
    (export / 'export.json').write_text(canonical(manifest)); (export / 'records.jsonl').write_text(canonical(record) + '\n')
    collector.collect_session(export)
    store._codex_collector = collector
    store._codex_export = export
    store._codex_auth = ep.auth
    try:
        yield store, sha, asset
    finally:
        store.close(); binding.close()


def test_baseline_restores_usable_master_assets_and_new_epoch(tmp_path, captured):
    from archive.v3.backup import create_backup, restore_drill
    store, sha, asset = captured
    key = bytes(range(32)); backup = tmp_path / 'backup'; scratch = tmp_path / 'restored'
    create_backup(store, backup, key)
    result = restore_drill(backup, key, scratch_dir=scratch)
    assert result['ok']
    assert result['epoch'] > store.epoch(), 'restored archive reused source epoch and old ACK authority'
    binding = SyntheticGuard('restore-fixture').verify(scratch)
    restored = MasterStore(binding, 'restore-fixture', readonly=True)
    try:
        assert restored.db.execute('SELECT content_text FROM messages').fetchone()[0] == 'baseline source body'
        path = scratch / 'objects' / sha[:2] / sha
        assert path.read_bytes() == asset, 'restore verified assets without restoring their bytes'
        assert path.stat().st_mode & 0o777 == 0o600
    finally:
        restored.close(); binding.close()


def test_captured_master_fts_matches_its_external_content(captured):
    store, _, _ = captured
    rows = store.db.execute("SELECT * FROM messages_fts WHERE messages_fts MATCH 'baseline'").fetchall()
    assert len(rows) == 1
    assert 'baseline source body' in tuple(rows[0])
    store.db.execute("INSERT INTO messages_fts(messages_fts, rank) VALUES('integrity-check', 1)")


def test_backup_tamper_fails_without_published_restored_root(tmp_path, captured):
    from archive.v3.backup import create_backup, restore_drill
    store, _, _ = captured
    key = bytes(range(32)); backup = tmp_path / 'backup'; scratch = tmp_path / 'restored'
    manifest = create_backup(store, backup, key)
    name = manifest['entries']['master']['file']
    encrypted = backup / name
    data = bytearray(encrypted.read_bytes()); data[-1] ^= 1; encrypted.write_bytes(data)
    with pytest.raises(Exception):
        restore_drill(backup, key, scratch_dir=scratch)
    assert not (scratch / '.v3_archive_identity.json').exists(), 'failed restore published active archive identity'


def test_backup_does_not_spill_plaintext_to_system_tmp(tmp_path, captured, monkeypatch):
    import tempfile
    from archive.v3 import backup as module
    store, _, _ = captured
    external_tmp = tmp_path / 'other-volume-tmp'
    external_tmp.mkdir()
    monkeypatch.setattr(tempfile, 'tempdir', str(external_tmp))
    encrypt = module._encrypt_stream
    def observe(*args, **kwargs):
        for path in external_tmp.rglob('*'):
            if path.is_file():
                data = path.read_bytes()
                assert not data.startswith(b'SQLite format 3'), 'plaintext master spilled outside the bound writer volume'
                assert b'baseline source body' not in data, 'plaintext mutation log spilled outside the bound writer volume'
        return encrypt(*args, **kwargs)
    monkeypatch.setattr(module, '_encrypt_stream', observe)
    module.create_backup(store, tmp_path / 'backup', bytes(range(32)))


def test_backup_preserves_existing_writer_lock(tmp_path, captured):
    from archive.v3.backup import create_backup
    store, _, _ = captured
    store.acquire_writer_lock()
    try:
        create_backup(store, tmp_path / 'backup', bytes(range(32)))
        with store.full_transaction():
            assert store.db.execute('SELECT count(*) FROM messages').fetchone()[0] == 1
    finally:
        store.release_writer_lock()


@pytest.mark.parametrize('replace_asset_and_reuse_rowid', [False, True])
def test_incremental_replay_preserves_revision_observation_receipt_and_log(tmp_path, captured, replace_asset_and_reuse_rowid):
    from archive.v3.backup import create_backup, restore_drill
    store, _, _ = captured
    key = bytes(range(32))
    create_backup(store, tmp_path / 'baseline', key)
    dest = tmp_path / 'new-view'
    def runner(claim):
        shutil.copytree(store._codex_export, dest, dirs_exist_ok=True)
        path = dest / 'records.jsonl'
        row = json.loads(path.read_text())
        row['message_content'] = 'updated source body'
        row['raw']['message_content'] = row['message_content']
        if replace_asset_and_reuse_rowid:
            replacement = b'replacement synthetic attachment in incremental'
            (dest / 'media' / 'asset').write_bytes(replacement)
            row['media_refs'][0].update(sha256=sha256_hex(replacement), length=len(replacement))
        row.pop('record_sha256')
        row['record_sha256'] = sha256_hex(canonical(row).encode())
        lines = [row]
        if replace_asset_and_reuse_rowid:
            reused = json.loads(canonical(row))
            reused['identity']['server_id'] = '102'
            reused['sort_seq'] = 2
            reused['message_content'] = 'another identity reuses the original local rowid'
            reused['raw']['message_content'] = reused['message_content']
            reused.pop('record_sha256')
            reused['record_sha256'] = sha256_hex(canonical(reused).encode())
            lines = [reused]
        path.write_text(''.join(canonical(r) + '\n' for r in lines))
        manifest = json.loads((dest / 'export.json').read_text())
        manifest['snapshot']['generation'] = 'updated'
        manifest['records']['count'] = len(lines)
        manifest['session']['shards'][0]['row_count'] = len(lines)
        (dest / 'export.json').write_text(canonical(manifest))
    collector = store._codex_collector
    collector.claim_source_snapshot(dest, 'account', 'talker', source_runner=runner)
    collector.collect_session(dest)
    tables = ('messages', 'message_revisions', 'source_observations', 'identity_map', 'runs', 'batches',
              'media_assets', 'media_needs', 'missing_observations', 'objects', 'scope_grants')
    expected = {t: [dict(r) for r in store.db.execute('SELECT * FROM ' + t)] for t in tables}
    old_log = [dict(r) for r in store.db.execute('SELECT * FROM mutation_log')]
    create_backup(store, tmp_path / 'incremental', key)
    scratch = tmp_path / 'restored'
    restore_drill(tmp_path / 'incremental', key, scratch_dir=scratch)
    binding = SyntheticGuard('restore-fixture').verify(scratch)
    restored = MasterStore(binding, 'restore-fixture', readonly=True)
    try:
        for table in tables:
            actual = [dict(r) for r in restored.db.execute('SELECT * FROM ' + table)]
            if table == 'runs':
                actual = [r for r in actual if not r['run_id'].startswith('drill-probe-')]
            assert actual == expected[table], 'incremental restore changed or omitted ' + table
        actual_log = [dict(r) for r in restored.db.execute('SELECT * FROM mutation_log')]
        assert actual_log[:len(old_log)] == old_log, 'incremental history log did not survive restore'
        for row in store.db.execute('SELECT sha256 FROM objects'):
            rel = Path('objects') / row[0][:2] / row[0]
            assert (scratch / rel).read_bytes() == (store.binding.root / rel).read_bytes()
    finally:
        restored.close(); binding.close()


def test_restored_endpoint_rejects_old_committed_ack_replay(tmp_path, captured):
    from archive.v3.backup import create_backup, restore_drill
    from archive.v3.wire import make_request
    store, _, _ = captured
    key = bytes(range(32)); backup = tmp_path / 'baseline'; scratch = tmp_path / 'restored'
    create_backup(store, backup, key)
    restore_drill(backup, key, scratch_dir=scratch)
    binding = SyntheticGuard('restore-fixture').verify(scratch)
    restored = MasterStore(binding, 'restore-fixture')
    ep = Endpoint(binding, 'restore-fixture'); ep.store = restored; ep.auth = store._codex_auth
    try:
        hello = ep.handle_frame(make_request(1, 'hello', role='collector', token='synthetic-token', archive_id='restore-fixture'))
        assert hello['ok']
        reply = ep.handle_frame(store._codex_last_frame)
        assert not reply['ok'], 'restored endpoint reissued old-epoch committed capture ACK'
    finally:
        restored.close(); binding.close()


def test_plaintext_never_lands_on_independent_backup_target(tmp_path, captured, monkeypatch):
    from archive.v3 import backup as module
    store, _, _ = captured
    destination = tmp_path / 'backup'
    encrypt = module._encrypt_stream
    seen = []
    def inspect_target(*args, **kwargs):
        for path in destination.rglob('*'):
            if path.is_file():
                data = path.read_bytes()
                if data.startswith(b'SQLite format 3') or b'baseline source body' in data:
                    seen.append(path.name)
        return encrypt(*args, **kwargs)
    monkeypatch.setattr(module, '_encrypt_stream', inspect_target)
    module.create_backup(store, destination, bytes(range(32)))
    assert not seen, 'independent encrypted backup target held plaintext during creation: ' + repr(seen)


@pytest.mark.parametrize('ancestor', [False, True])
def test_backup_target_symlink_cannot_redirect_writes(tmp_path, captured, ancestor):
    from archive.v3.backup import create_backup
    store, _, _ = captured
    outside = tmp_path / 'foreign'; outside.mkdir(mode=0o755)
    before = outside.stat().st_mode
    link = tmp_path / 'backup-link'; link.symlink_to(outside, target_is_directory=True)
    with pytest.raises(Exception):
        create_backup(store, link / 'child' if ancestor else link, bytes(range(32)))
    assert list(outside.iterdir()) == [], 'backup wrote through foreign target symlink'
    assert outside.stat().st_mode == before, 'backup chmod followed target symlink'


def test_out_of_order_backup_publish_cannot_regress_protected_prefix(tmp_path, captured, monkeypatch):
    from archive.v3 import backup as module
    store, _, _ = captured
    real_write = module._write_manifest
    newer_prefix = []
    outer = tmp_path / 'older-backup'
    interleaved = False
    def interleave(out_dir, manifest):
        nonlocal interleaved
        result = real_write(out_dir, manifest)
        if not interleaved:
            interleaved = True
            with store.full_transaction() as tx:
                tx.meta_set('synthetic_after_capture', 'later committed state')
                tx.mutation('meta', 'synthetic_after_capture', 'put', {'key': 'synthetic_after_capture', 'value': 'later committed state'})
            module.create_backup(store, tmp_path / 'newer-backup', bytes(range(32)))
            newer_prefix.append(module.backup_status(store)['protected_seq'])
        return result
    monkeypatch.setattr(module, '_write_manifest', interleave)
    try:
        module.create_backup(store, outer, bytes(range(32)))
    except Exception:
        pass  # safe refusal of the stale publication is allowed
    assert newer_prefix, 'interleave did not create newer independently durable backup'
    assert module.backup_status(store)['protected_seq'] >= newer_prefix[0], 'late old backup publication regressed protected_seq'
