"""Independent capture/restart behavior against the real in-process endpoint."""
import json

import pytest

from archive.v3.collector import CaptureClient, Collector
from archive.v3.endpoint import Endpoint
from archive.v3.errors import ProtocolError
from archive.v3.guard import SyntheticGuard
from archive.v3.master import MasterStore
from archive.v3.util import canonical, sha256_hex


def export_fixture(root, body='synthetic content', generation='generation-1'):
    root.mkdir(exist_ok=True)
    record = {'identity': {'server_id': '123', 'shard': 'message_0.db',
                          'table': 'Msg_synthetic', 'local_rowid': 1},
              'create_time': 100, 'sort_seq': 1, 'local_type': 1, 'status': 0,
              'message_content': body, 'media_refs': [],
              'raw': {'server_id': 123, 'message_content': body,
                      'create_time': 100, 'sort_seq': 1, 'local_type': 1, 'status': 0}}
    record['record_sha256'] = sha256_hex(canonical(record).encode())
    manifest = {'contract': 'wx-archive.external-archive-records', 'version': 2,
                'kind': 'session_export', 'archive_id': 'collector-fixture',
                'account': 'account', 'talker': 'talker',
                'snapshot': {'generation': generation, 'created_at': 200,
                             'databases': [{'file': 'message_0.db',
                                            'sha256': sha256_hex(body.encode()), 'bytes': 4096}]},
                'session': {'shards': [{'database': 'message_0.db',
                                       'table': 'Msg_synthetic', 'row_count': 1}]},
                'enumeration': {'complete': True},
                'records': {'file': 'records.jsonl', 'count': 1}}
    (root / 'export.json').write_text(canonical(manifest))
    (root / 'records.jsonl').write_text(canonical(record) + '\n')
    return root


@pytest.fixture
def endpoint_fixture(tmp_path):
    root = tmp_path / 'nas'
    root.mkdir(mode=0o700)
    SyntheticGuard.write_marker(root, 'collector-fixture', 1, 0)
    binding = SyntheticGuard('collector-fixture').verify(root)
    store = MasterStore.initialize(binding, 'collector-fixture',
                                   {'account': {'talker': {'capture': True}}})
    endpoint = Endpoint(binding, 'collector-fixture')
    endpoint.store = store
    endpoint.auth = {'roles': {'collector': {'token_sha256': sha256_hex(b'synthetic-token')}}}
    class Transport:
        drop_ack = False
        block_upload = False
        def request(self, frame):
            if self.block_upload and frame['op'] == 'begin_upload':
                raise ProtocolError('transport_closed', 'synthetic disconnect before upload')
            reply = endpoint.handle_frame(frame)
            if self.drop_ack and frame['op'] == 'commit_batch' and reply.get('ok'):
                self.drop_ack = False
                raise ProtocolError('transport_closed', 'synthetic lost ACK after commit')
            return reply
    transport = Transport()
    def collector():
        client = CaptureClient(transport, 'collector-fixture', 'synthetic-token')
        return Collector(client, tmp_path / 'mac-work')
    try:
        yield store, transport, collector
    finally:
        store.close()
        binding.close()


def test_ack_loss_recovers_same_commit_without_new_mutation(tmp_path, endpoint_fixture):
    store, transport, collector = endpoint_fixture
    export = export_fixture(tmp_path / 'export')
    transport.drop_ack = True
    with pytest.raises(ProtocolError):
        collector().collect_session(export)
    committed_seq = store.commit_seq()
    assert store.db.execute('SELECT count(*) FROM messages').fetchone()[0] == 1
    reply = collector().collect_session(export)
    assert reply.get('replay') is True, 'lost ACK caused another capture instead of receipt recovery'
    assert store.commit_seq() == committed_seq, 'receipt recovery mutated the archive'


def test_same_generation_label_different_snapshot_is_not_replayed(tmp_path, endpoint_fixture):
    store, _, collector = endpoint_fixture
    collector().collect_session(export_fixture(tmp_path / 'first', 'first snapshot'))
    capture = collector()
    second = tmp_path / 'second'
    capture.claim_source_snapshot(second, 'account', 'talker',
        source_runner=lambda claim: export_fixture(second, 'second snapshot'))
    capture.collect_session(second)
    assert store.db.execute('SELECT content_text FROM messages').fetchone()[0] == 'second snapshot'


def test_corrupt_local_intent_is_preserved_and_rejected(tmp_path, endpoint_fixture):
    _, transport, collector = endpoint_fixture
    export = export_fixture(tmp_path / 'export')
    transport.block_upload = True
    with pytest.raises(ProtocolError):
        collector().collect_session(export)
    intents = list((tmp_path / 'mac-work').rglob('intent.json'))
    assert len(intents) == 1
    intents[0].write_bytes(b'corrupt intent evidence')
    transport.block_upload = False
    with pytest.raises(Exception):
        collector().collect_session(export)
    assert intents[0].read_bytes() == b'corrupt intent evidence'


def test_delayed_older_snapshot_cannot_replace_newer_capture(tmp_path, endpoint_fixture):
    store, _, collector = endpoint_fixture
    older = export_fixture(tmp_path / 'older', 'older captured view', 'old-generation')
    manifest_path = older / 'export.json'
    manifest = json.loads(manifest_path.read_text())
    manifest['snapshot']['created_at'] = 100
    manifest_path.write_text(canonical(manifest))
    newer = export_fixture(tmp_path / 'newer', 'newer captured view', 'new-generation')
    collector().collect_session(newer)
    try:
        collector().collect_session(older)
    except Exception:
        pass  # Rejecting the stale capture is acceptable.
    assert store.db.execute('SELECT content_text FROM messages').fetchone()[0] == 'newer captured view', \
        'begin_run after an old export gave it a new C0 and overwrote newer source state'


@pytest.mark.parametrize('incoming_clock', [200, 300])
def test_unclaimed_view_clock_cannot_authorize_overwrite(tmp_path, endpoint_fixture, incoming_clock):
    store, _, collector = endpoint_fixture
    collector().collect_session(export_fixture(tmp_path / 'accepted', 'accepted source state'))
    unclaimed = export_fixture(tmp_path / 'unclaimed', 'unproven source state', 'other-generation')
    path = unclaimed / 'export.json'
    manifest = json.loads(path.read_text())
    manifest['snapshot']['created_at'] = incoming_clock
    path.write_text(canonical(manifest))
    try:
        collector().collect_session(unclaimed)
    except Exception:
        pass
    assert store.db.execute('SELECT content_text FROM messages').fetchone()[0] == 'accepted source state', \
        'unclaimed source clock (same second or clock rollback) was treated as causal authority'


def test_claim_cannot_retroactively_bless_existing_export(tmp_path, endpoint_fixture):
    store, _, collector = endpoint_fixture
    collector().collect_session(export_fixture(tmp_path / 'accepted', 'accepted state'))
    old_export = export_fixture(tmp_path / 'already-read', 'stale state', 'old-view')
    capture = collector()
    try:
        capture.claim_source_snapshot(old_export, 'account', 'talker')
        capture.collect_session(old_export)
    except Exception:
        pass
    assert store.db.execute('SELECT content_text FROM messages').fetchone()[0] == 'accepted state', \
        'claim issued AFTER an export existed retroactively authorized old view overwrite'


@pytest.mark.parametrize('field,value', [('account', 'other-account'), ('epoch', 2), ('c0', 999)])
def test_resumed_claim_is_verified_before_source_runner(tmp_path, endpoint_fixture, field, value):
    _, _, collector = endpoint_fixture
    capture = collector()
    dest = tmp_path / 'claimed'
    capture.claim_source_snapshot(dest, 'account', 'talker')
    path = dest / 'snapshot-claim.json'
    claim = json.loads(path.read_text())
    claim[field] = value
    path.write_text(canonical(claim))
    called = []
    try:
        capture.claim_source_snapshot(dest, 'account', 'talker', source_runner=lambda claim: called.append(claim))
    except Exception:
        pass
    assert not called, 'invalid durable claim started source capture before verifying authority'
