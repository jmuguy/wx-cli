"""Backup chain behaviour tests (AES-GCM, real incremental replay,
protected_seq, rotation gate, perms, freshness/RPO) — own tests, distinct
from the Codex restore gate."""
import json
import os
import stat
from pathlib import Path

import pytest

from archive.v3.backup import (backup_status, create_backup, create_baseline,
                               create_incremental, record_drill_pass,
                               restore_drill)
from archive.v3.collector import CaptureClient, Collector
from archive.v3.endpoint import Endpoint
from archive.v3.errors import ArchiveError
from archive.v3.guard import SyntheticGuard
from archive.v3.master import MasterStore
from archive.v3.util import canonical, sha256_hex

ARCHIVE = 'backup-fixture'
KEY = bytes(range(32))


class Harness:
    """One archive + collector over an in-process endpoint."""

    def __init__(self, tmp_path):
        self.tmp = tmp_path
        self.root = tmp_path / 'master'
        self.root.mkdir(mode=0o700)
        SyntheticGuard.write_marker(self.root, ARCHIVE, 1, 0)
        self.binding = SyntheticGuard(ARCHIVE).verify(self.root)
        self.store = MasterStore.initialize(
            self.binding, ARCHIVE,
            {'account': {'talker': {'capture': True, 'query': True}}})
        ep = Endpoint(self.binding, ARCHIVE)
        ep.store = self.store
        ep.auth = {'roles': {'collector': {
            'token_sha256': sha256_hex(b'synthetic-token')}}}

        class Transport:
            def request(_, frame):
                return ep.handle_frame(frame)

        self.collector = Collector(
            CaptureClient(Transport(), ARCHIVE, 'synthetic-token'),
            tmp_path / 'work')
        self.n = 0

    def export(self, records, *, manifest_extra=None, assets=()):
        """Write an export dir (unclaimed; insert-only by construction)."""
        self.n += 1
        export = self.tmp / f'export{self.n}'
        export.mkdir()
        for name, blob in assets:
            (export / 'media').mkdir(exist_ok=True)
            (export / 'media' / name).write_bytes(blob)
        rows = []
        for record in records:
            record = dict(record)
            record['record_sha256'] = sha256_hex(
                canonical(record).encode('utf-8'))
            rows.append(record)
        manifest = {
            'contract': 'wx-archive.external-archive-records', 'version': 2,
            'kind': 'session_export', 'archive_id': ARCHIVE,
            'account': 'account', 'talker': 'talker',
            'snapshot': {'generation': f'g{self.n}', 'created_at': 200 + self.n,
                         'databases': [{'file': 'message_0.db',
                                        'sha256': '0' * 64, 'bytes': 4096}]},
            'session': {'shards': [{'database': 'message_0.db',
                                    'table': 'Msg_synthetic',
                                    'row_count': len(rows)}]},
            'enumeration': {'complete': True},
            'records': {'file': 'records.jsonl', 'count': len(rows)},
        }
        manifest.update(manifest_extra or {})
        (export / 'export.json').write_text(canonical(manifest),
                                            encoding='utf-8')
        with open(export / 'records.jsonl', 'w', encoding='utf-8') as fh:
            for row in rows:
                fh.write(canonical(row) + '\n')
        return export

    def export_with_claim(self, records, *, assets=()):
        """Claim-first export: the run is opened BEFORE the view is written,
        so updates and missing marks carry causal authority."""
        self.n += 1
        export = self.tmp / f'export{self.n}'
        export.mkdir()

        def write_view(_claim):
            rows = []
            for record in records:
                record = dict(record)
                record['record_sha256'] = sha256_hex(
                    canonical(record).encode('utf-8'))
                rows.append(record)
            manifest = {
                'contract': 'wx-archive.external-archive-records',
                'version': 2, 'kind': 'session_export', 'archive_id': ARCHIVE,
                'account': 'account', 'talker': 'talker',
                'snapshot': {'generation': f'g{self.n}',
                             'created_at': 300 + self.n,
                             'databases': [{'file': 'message_0.db',
                                            'sha256': '0' * 64,
                                            'bytes': 4096}]},
                'session': {'shards': [{'database': 'message_0.db',
                                        'table': 'Msg_synthetic',
                                        'row_count': len(rows)}]},
                'enumeration': {'complete': True},
                'records': {'file': 'records.jsonl', 'count': len(rows)},
            }
            (export / 'export.json').write_text(canonical(manifest),
                                                encoding='utf-8')
            with open(export / 'records.jsonl', 'w',
                      encoding='utf-8') as fh:
                for row in rows:
                    fh.write(canonical(row) + '\n')
            for name, blob in assets:
                target = export / 'media'
                target.mkdir(exist_ok=True)
                (target / name).write_bytes(blob)

        self.collector.claim_source_snapshot(export, 'account', 'talker',
                                             source_runner=write_view)
        return export

    @staticmethod
    def record(rowid, server_id, text, *, refs=(), raw_extra=None):
        raw = {'message_content': text}
        raw.update(raw_extra or {})
        return {'identity': {'shard': 'message_0.db', 'table': 'Msg_synthetic',
                             'local_rowid': rowid, 'server_id': server_id},
                'create_time': 100 + rowid, 'sort_seq': rowid,
                'local_type': 1, 'status': 0, 'message_content': text,
                'raw': raw, 'media_refs': list(refs)}

    def close(self):
        self.store.close()
        self.binding.close()


@pytest.fixture
def harness(tmp_path):
    h = Harness(tmp_path)
    try:
        yield h
    finally:
        h.close()


def _asset(ref_key, blob):
    sha = sha256_hex(blob)
    return {'ref_key': ref_key, 'kind': 'file', 'present': True,
            'filename': f'media/{ref_key}', 'sha256': sha,
            'length': len(blob)}, sha, blob


def test_incremental_replay_reproduces_live_state(tmp_path, harness):
    h = harness
    blob1 = b'attachment-one-bytes'
    ref1, sha1, _ = _asset('file:one', blob1)
    first = h.export([h.record(1, '101', 'baseline body', refs=[ref1])],
                     assets=[('file:one', blob1)])
    r1 = h.collector.collect_session(first)
    assert r1['receipt']['counts']['rows_inserted'] == 1

    backup1 = tmp_path / 'backup1'
    manifest1 = create_backup(h.store, backup1, KEY)
    assert manifest1['kind'] == 'baseline'

    # second capture: an EDIT of row 101 (claimed) + a NEW row with an asset
    blob2 = b'attachment-two-bytes'
    ref2, sha2, _ = _asset('file:two', blob2)
    second = h.export_with_claim(
        [h.record(1, '101', 'edited body v2'),
         h.record(2, '102', 'second row', refs=[ref2])],
        assets=[('file:two', blob2)])
    r2 = h.collector.collect_session(second)
    assert r2['receipt']['counts']['rows_updated'] == 1
    assert r2['receipt']['counts']['rows_inserted'] == 1

    status = backup_status(h.store)
    assert status['protected_seq'] == manifest1['commit_seq']
    assert status['unprotected_commits'] > 0

    backup2 = tmp_path / 'backup2'
    manifest2 = create_backup(h.store, backup2, KEY)
    assert manifest2['kind'] == 'incremental'
    assert manifest2['since_seq'] == manifest1['commit_seq']
    assert manifest2['commit_seq'] > manifest2['since_seq']
    assert manifest2['entries']['mutation_log']['rows'] > 0
    assert {a['sha256'] for a in manifest2['assets']} == {sha2}
    # the protection-advance transaction itself commits AFTER the captured
    # point, so exactly one bookkeeping commit stays unprotected by design
    assert backup_status(h.store)['unprotected_commits'] == 1

    # the drill must resolve the CHAIN from the incremental dir alone
    scratch = tmp_path / 'restored'
    result = restore_drill(backup2, KEY, scratch_dir=scratch)
    assert result['ok'] and result['chain_depth'] == 2
    assert result['assets_recovered'] == 2   # union over the chain

    # REAL replay: content-bearing state equals the live master
    live = h.store.db.execute(
        'SELECT content_text, record_sha256 FROM messages '
        'WHERE server_id=?', ('101',)).fetchone()
    restored = MasterStore(
        SyntheticGuard(ARCHIVE).verify(scratch), ARCHIVE, readonly=True)
    try:
        row = restored.db.execute(
            'SELECT content_text, record_sha256 FROM messages '
            'WHERE server_id=?', ('101',)).fetchone()
        assert row['content_text'] == live['content_text'] == 'edited body v2'
        assert row['record_sha256'] == live['record_sha256']
        assert restored.db.execute(
            'SELECT COUNT(*) FROM messages').fetchone()[0] == \
            h.store.db.execute(
                'SELECT COUNT(*) FROM messages').fetchone()[0] == 2
        assert restored.db.execute(
            'SELECT COUNT(*) FROM message_revisions').fetchone()[0] == \
            h.store.db.execute(
                'SELECT COUNT(*) FROM message_revisions').fetchone()[0] == 1
        # new epoch, and the old epoch's ACK authority is dead
        assert result['epoch'] == h.store.epoch() + 1
        assert restored.epoch() == result['epoch']
        # assets physically recovered, content-addressed, 0600
        for sha, blob in ((sha1, blob1), (sha2, blob2)):
            path = scratch / 'objects' / sha[:2] / sha
            assert path.read_bytes() == blob
            assert path.stat().st_mode & 0o777 == 0o600
    finally:
        restored.close()


def test_incremental_tamper_fails_without_scratch(tmp_path, harness):
    h = harness
    h.collector.collect_session(h.export([h.record(1, '101', 'body')]))
    create_backup(h.store, tmp_path / 'b1', KEY)
    h.collector.collect_session(h.export_with_claim(
        [h.record(1, '101', 'edited')]))
    create_backup(h.store, tmp_path / 'b2', KEY)
    blob = tmp_path / 'b2' / 'mutation-log.jsonl.enc'
    data = bytearray(blob.read_bytes())
    data[len(data) // 2] ^= 1
    blob.write_bytes(data)
    with pytest.raises(ArchiveError):
        restore_drill(tmp_path / 'b2', KEY, scratch_dir=tmp_path / 'scratch')
    assert not (tmp_path / 'scratch').exists()
    assert not (tmp_path / 'scratch'
                / '.v3_archive_identity.json').exists()


def test_wrong_key_rejected_before_publish(tmp_path, harness):
    h = harness
    h.collector.collect_session(h.export([h.record(1, '101', 'body')]))
    create_backup(h.store, tmp_path / 'b1', KEY)
    with pytest.raises(ArchiveError):
        restore_drill(tmp_path / 'b1', bytes(32), scratch_dir=tmp_path / 's')
    assert not (tmp_path / 's').exists()


def test_scratch_refused_when_existing(tmp_path, harness):
    h = harness
    h.collector.collect_session(h.export([h.record(1, '101', 'body')]))
    create_backup(h.store, tmp_path / 'b1', KEY)
    target = tmp_path / 's'
    target.mkdir()
    with pytest.raises(ArchiveError) as exc:
        restore_drill(tmp_path / 'b1', KEY, scratch_dir=target)
    assert exc.value.code == 'backup_scratch_exists'


def test_no_plaintext_bytes_in_backup_dir(tmp_path, harness):
    h = harness
    secret = b'very-recognisable-plaintext-body'
    h.collector.collect_session(
        h.export([h.record(1, '101', secret.decode())]))
    backup = tmp_path / 'b1'
    create_backup(h.store, backup, KEY)
    for path in backup.rglob('*'):
        if path.is_file():
            assert secret not in path.read_bytes(), \
                f'plaintext leaked into {path.name}'


def test_backup_directory_permissions(tmp_path, harness):
    h = harness
    h.collector.collect_session(h.export([h.record(1, '101', 'body')]))
    backup = tmp_path / 'b1'
    create_backup(h.store, backup, KEY)
    assert backup.stat().st_mode & 0o777 == 0o700
    for path in backup.rglob('*'):
        if path.is_file():
            assert path.stat().st_mode & 0o777 == 0o600, path.name


def test_chain_gap_refused(tmp_path, harness):
    h = harness
    h.collector.collect_session(h.export([h.record(1, '101', 'body')]))
    create_backup(h.store, tmp_path / 'b1', KEY)
    # simulate drift of the protected prefix (corruption / manual edit):
    # the incremental must refuse to continue from a wrong hole point
    with h.store.full_transaction() as tx:
        tx.meta_set('backup_protected_seq',
                    str(int(h.store.meta('backup_protected_seq')) - 1))
    with pytest.raises(ArchiveError) as exc:
        create_incremental(h.store, tmp_path / 'b2', KEY)
    assert exc.value.code == 'backup_chain_gap'


def test_rotation_requires_matching_drill_pass(tmp_path, harness):
    h = harness
    h.collector.collect_session(h.export([h.record(1, '101', 'body')]))
    create_backup(h.store, tmp_path / 'b1', KEY)
    with pytest.raises(ArchiveError) as exc:
        create_baseline(h.store, tmp_path / 'b2', KEY, verified_rotation=True)
    assert exc.value.code == 'backup_rotation_requires_drill'
    # only a REAL drill of the existing chain unlocks the rotation
    result = restore_drill(tmp_path / 'b1', KEY, scratch_dir=tmp_path / 's1')
    record_drill_pass(h.store, result['chain_token'])
    manifest = create_baseline(h.store, tmp_path / 'b2', KEY,
                               verified_rotation=True)
    assert manifest['kind'] == 'baseline'
    assert len(manifest['chain']) == 1        # fresh chain
    # and the next incremental continues the NEW chain
    h.collector.collect_session(h.export_with_claim(
        [h.record(1, '101', 'edited')]))
    inc = create_incremental(h.store, tmp_path / 'b3', KEY)
    assert inc['since_seq'] == manifest['commit_seq']


def test_heartbeat_incremental_when_unchanged(tmp_path, harness):
    h = harness
    h.collector.collect_session(h.export([h.record(1, '101', 'body')]))
    create_backup(h.store, tmp_path / 'b1', KEY)
    # every protection advance itself commits, so each no-capture
    # incremental carries exactly the PREVIOUS advance's mutation row —
    # that is the honest heartbeat invariant, never a fabricated zero
    inc1 = create_incremental(h.store, tmp_path / 'b2', KEY)
    assert inc1['entries']['mutation_log']['rows'] == 1
    inc2 = create_incremental(h.store, tmp_path / 'b3', KEY)
    assert inc2['commit_seq'] == inc1['commit_seq'] + 1
    assert inc2['entries']['mutation_log']['rows'] == 1
    assert inc2['assets'] == []
    assert inc2['counts']['messages'] == inc1['counts']['messages']
    result = restore_drill(tmp_path / 'b3', KEY, scratch_dir=tmp_path / 's')
    assert result['ok'] and result['chain_depth'] == 3


def test_replay_verification_catches_missing_tail(tmp_path, harness):
    """A mutation row silently dropped from an incremental (simulated
    truncation of the LOG, keeping the GCM blob intact is impossible — so
    instead: a manifest whose counts disagree with its log) must FAIL the
    drill, never return ok."""
    h = harness
    h.collector.collect_session(h.export([h.record(1, '101', 'body')]))
    create_backup(h.store, tmp_path / 'b1', KEY)
    r = h.collector.collect_session(h.export_with_claim(
        [h.record(1, '101', 'edited body')]))
    assert r['receipt']['counts']['rows_updated'] == 1
    backup2 = tmp_path / 'b2'
    create_incremental(h.store, backup2, KEY)
    # forge the recorded counts upward: replay cannot reach them → failure
    manifest = json.loads((backup2 / 'manifest.json').read_text())
    manifest['counts']['messages'] += 5
    keep = manifest['backup_id']
    body = {k: v for k, v in manifest.items() if k != 'backup_id'}
    manifest = dict(body, backup_id=sha256_hex(canonical(body).encode()))
    assert manifest['backup_id'] != keep
    (backup2 / 'manifest.json').write_text(canonical(manifest), encoding='utf-8')
    with pytest.raises(ArchiveError) as exc:
        restore_drill(backup2, KEY, scratch_dir=tmp_path / 's')
    assert exc.value.code == 'restore_drill_failed'
    assert not (tmp_path / 's').exists()


def test_status_freshness_and_rpo_split(tmp_path, harness):
    h = harness
    empty = backup_status(h.store)
    assert empty['protected_seq'] is None
    assert empty['unprotected_commits'] == h.store.commit_seq()
    assert empty['freshness_ms'] is None
    h.collector.collect_session(h.export([h.record(1, '101', 'body')]))
    create_backup(h.store, tmp_path / 'b1', KEY)
    after = backup_status(h.store)
    assert after['protected_seq'] == after['master_commit_seq'] - 1
    assert after['unprotected_commits'] == 1   # the advance commit itself
    assert after['chain_depth'] == 1
    assert isinstance(after['freshness_ms'], int)
    # new commits after the backup: FRESH timestamp, but RPO exposure > 0 —
    # the two facts are reported independently
    h.collector.collect_session(h.export([h.record(2, '102', 'new row')]))
    exposed = backup_status(h.store)
    assert exposed['freshness_ms'] is not None
    assert exposed['unprotected_commits'] > 0
