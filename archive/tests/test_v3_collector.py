"""Implementer-side gates for the v3 collector and its bounded capture flow.

Complements the immutable Codex gates with the controller's coordination
directives:
  * the loader accepts the REAL Rust v2 export shapes (raw original bytes vs
    decoded text) and only enforces equality where the producer guarantees it;
  * the capture run travels as content-addressed PAGES with machine-readable
    limits — never one giant frame, never all records/media in memory;
  * restart/disconnect resume is real: persisted intent, idempotent uploads,
    ACK-loss receipt recovery, no duplicate application;
  * missing is a SECOND-opinion decision: reported by collect, published only
    from a fresher complete recheck with matching topology (真实缺失 / 迁片 /
    后下载旧媒体 fixtures);
  * media errors are classified: length/hash/permission = hard failures,
    proven source absence = open need, mid-flight vanish = vanished gap,
    undecodable source bytes = error gap.
"""
import json
import os
import shutil
import subprocess
import unittest
from pathlib import Path
from unittest import mock

from archive.v3 import endpoint as endpoint_module
from archive.v3.collector import (CaptureClient, Collector, PageWriter,
                                  SessionExport, _gap_need,
                                  identity_from_key, load_session_export)
from archive.v3.endpoint import Endpoint, StagingManager
from archive.v3.errors import ArchiveError, ExportError
from archive.v3.guard import SyntheticGuard
from archive.v3.master import MasterStore
from archive.v3.util import b64e, canonical, sha256_hex
from archive.v3.wire import decode_frame, encode_frame

ARCHIVE = 'collector-impl-fixture'
ACCOUNT = 'account'
TALKER = 'talker'

# the exact synthetic zstd frame embedded in the Rust gate fixture
# (codex_archive_inspect_gate.rs) — decodes to a known unicode string
ZSTD_FRAME = bytes([
    40, 181, 47, 253, 4, 88, 33, 1, 0, 229, 144, 136, 230, 136, 144, 229, 142,
    139, 231, 188, 169, 230, 173, 163, 230, 150, 135, 32, 115, 121, 110, 116,
    104, 101, 116, 105, 99, 32, 109, 101, 115, 115, 97, 103, 101, 214, 55,
    40, 184])
ZSTD_DECODED = '合成压缩正文 synthetic message'


def _code(exc):
    return getattr(exc, 'code', None)


def _resign(record):
    """Recompute record_sha256 after a test mutates a built record."""
    body = {k: v for k, v in record.items() if k != 'record_sha256'}
    record['record_sha256'] = sha256_hex(canonical(body).encode('utf-8'))
    return record


# ── synthetic export builder (mirrors the Rust producer's shapes) ─────────
def make_record(*, shard='message_0.db', table='Msg_synthetic', rowid=1,
                server_id=123, create_time=100, sort_seq=1, local_type=1,
                status=0, content='body', raw_content=None, raw_is_blob=False,
                decode_status='ok', wcdb_ct=None, packed=None,
                media_refs=None, extra_raw=None):
    """Build a contract-v2 record the way archive_inspect.rs does.

    raw_content: the ORIGINAL message_content bytes (text str or bytes);
    content: the DECODED text the top level carries. When raw is bytes it is
    b64-wrapped in `raw` and preserved in message_content_raw_b64 — exactly
    the compressed/blob rows that must NOT be rejected for differing."""
    if raw_content is None:
        raw_content = content
    raw = {
        'sort_seq': sort_seq, 'server_id': server_id, 'local_type': local_type,
        'create_time': create_time, 'status': status,
        'message_content': raw_content if isinstance(raw_content, str)
        else {'b64': b64e(raw_content)},
        'packed_info_data': None if packed is None else {'b64': b64e(packed)},
    }
    if wcdb_ct is not None:
        raw['WCDB_CT_message_content'] = wcdb_ct
    if extra_raw:
        raw.update(extra_raw)
    needs_preservation = isinstance(raw_content, (bytes, bytearray)) or \
        raw_content != content
    record = {
        'identity': {
            'shard': shard, 'table': table, 'database': shard,
            'local_rowid': rowid,
            'server_id': None if not server_id else str(server_id),
        },
        'create_time': create_time, 'sort_seq': sort_seq,
        'local_type': local_type, 'sub_type': 0, 'status': status,
        'message_content': content,
        'message_content_raw_b64': b64e(raw_content) if needs_preservation
        and isinstance(raw_content, (bytes, bytearray)) else
        (raw_content if needs_preservation else None),
        'message_content_raw_type': 'blob' if raw_is_blob else 'text',
        'message_content_decode_status': decode_status,
        'packed_info_data': None if packed is None else b64e(packed),
        'packed_info_sha256': None if packed is None else sha256_hex(packed),
        'media_refs': media_refs or [],
        'raw': raw,
    }
    if wcdb_ct is not None:
        record['wcdb_ct'] = wcdb_ct
    record['record_sha256'] = sha256_hex(
        canonical(record).encode('utf-8'))
    return record


def build_export(root, records, *, generation='gen-1', created_at=1000,
                 account=ACCOUNT, talker=TALKER, media_files=None,
                 enumeration_complete=True, archive_id=ARCHIVE):
    """Write a contract-v2 session export directory (manifest + records +
    optional media files)."""
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    ordered = sorted(records, key=lambda r: (r['identity']['shard'],
                                             r['identity']['table'],
                                             r['identity']['local_rowid']))
    shard_rows = {}
    for record in ordered:
        ident = record['identity']
        key = (ident['shard'], ident['table'])
        shard_rows.setdefault(key, 0)
        shard_rows[key] += 1
    manifest = {
        'contract': 'wx-archive.external-archive-records', 'version': 2,
        'kind': 'session_export', 'archive_id': archive_id,
        'account': account, 'talker': talker,
        'snapshot': {
            'generation': generation, 'created_at': created_at,
            'databases': [{'file': 'message_0.db',
                           'sha256': sha256_hex(generation.encode()),
                           'bytes': 4096}],
        },
        'session': {
            'talker_name': talker, 'name_hash': 'synthetic',
            'shards': [{'database': db, 'table': table, 'row_count': count}
                       for (db, table), count in sorted(shard_rows.items())],
        },
        'enumeration': {'complete': enumeration_complete,
                        'unknown_tables': [], 'unknown_columns': {}},
        'records': {'file': 'records.jsonl', 'count': len(ordered)},
        'media_dir': 'media',
        'ordering': '(shard,local_rowid) ascending',
        'media': {'dir': 'media', 'complete': True,
                  'attach_root_provided': bool(media_files),
                  'staged_count': len(media_files or {}),
                  'unavailable_count': 0, 'decode_failures': 0,
                  'kinds_without_local_bytes': {}, 'notes': [],
                  'sample_unavailable': []},
    }
    (root / 'export.json').write_text(canonical(manifest), encoding='utf-8')
    with open(root / 'records.jsonl', 'w', encoding='utf-8') as fh:
        for record in ordered:
            fh.write(canonical(record) + '\n')
    if media_files:
        media_dir = root / 'media'
        media_dir.mkdir(exist_ok=True)
        for name, data in media_files.items():
            (media_dir / name).write_bytes(data)
    return root


def simple_rows(count, *, shard='message_0.db', table='Msg_synthetic',
                create_time=100, body_prefix='m'):
    return [make_record(shard=shard, table=table, rowid=i + 1,
                        server_id=10_000_000_000_000 + i,
                        create_time=create_time, sort_seq=i,
                        content=f'{body_prefix}{i}')
            for i in range(count)]


def media_ref(ref_key, data=None, *, kind='image', reason=None):
    if data is None:
        return {'kind': kind, 'ref_key': ref_key, 'present': False,
                'reason': reason or 'no local .dat found '
                                   '(attach layout and hardlink.db exhausted)'}
    return {'kind': kind, 'ref_key': ref_key, 'present': True,
            'length': len(data), 'sha256': sha256_hex(data),
            'filename': f'media/{ref_key}'}


# ── in-process endpoint harness with a wire-bridging transport ────────────
class BridgeTransport:
    """Round-trips every request through the REAL wire encode/decode against
    a live endpoint — frame-size limits and op dispatch apply for real. The
    op log lets tests assert which protocol paths a collect actually used."""

    def __init__(self, endpoint):
        self.endpoint = endpoint
        self.ops = []
        self.fail_on = None      # (op_name) -> raise after delivering?

    def request(self, frame):
        encoded = encode_frame(frame)
        self.ops.append(frame['op'])
        response = self.endpoint.handle_frame(decode_frame(encoded))
        encode_frame(response)  # response frame bound too
        return response


class Harness:
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        SyntheticGuard.write_marker(self.root, ARCHIVE, 1, 0)
        self.binding = SyntheticGuard(ARCHIVE).verify(self.root)
        self.store = MasterStore.initialize(
            self.binding, ARCHIVE, {ACCOUNT: {TALKER: {'capture': True}}})
        self.endpoint = self._make_endpoint()

    def _make_endpoint(self):
        endpoint = Endpoint(self.binding, ARCHIVE)
        endpoint.store = self.store
        endpoint.auth = {'roles': {
            'collector': {'token_sha256': sha256_hex(b'impl-token')}}}
        return endpoint

    def collector(self, work_dir):
        transport = BridgeTransport(self.endpoint)
        client = CaptureClient(transport, ARCHIVE, 'impl-token')
        return Collector(client, work_dir), transport

    def restart(self):
        """Simulate an endpoint process restart: close everything, reopen the
        store through a fresh binding, new endpoint + transport."""
        self.store.close()
        self.binding.close()
        self.binding = SyntheticGuard(ARCHIVE).verify(self.root)
        self.store = MasterStore(self.binding, ARCHIVE)
        self.store.acquire_writer_lock()
        self.endpoint = self._make_endpoint()

    def close(self):
        try:
            self.store.close()
        finally:
            self.binding.close()

    def q(self, sql, *params):
        return self.store.db.execute(sql, params).fetchone()


# ══════════════════════════════════════════════════════════════════════════
# 1. loader vs the REAL Rust v2 shapes (directive #1)
# ══════════════════════════════════════════════════════════════════════════
class LoaderRustShapesTest(unittest.TestCase):
    def _rows(self, records, **kwargs):
        """Build + load + CONSUME inside the tempdir: `records` is a lazy
        stream, so it must be exhausted while the export still exists."""
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            root = build_export(Path(tmp) / 'export', records, **kwargs)
            return list(load_session_export(root).records)

    def test_rust_plain_text_row_loads(self):
        record = make_record(server_id=9007199254740993, rowid=1,
                             content='synthetic body')
        rows = self._rows([record])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['message_content'], 'synthetic body')
        self.assertEqual(rows[0]['identity']['server_id'], '9007199254740993')

    def test_rust_compressed_row_loads_with_raw_bytes_preserved(self):
        # the Rust gate fixture exactly: raw is a b64-wrapped zstd BLOB, the
        # top level is the DECODED text — the loader must not demand equality
        record = make_record(content=ZSTD_DECODED, raw_content=ZSTD_FRAME,
                             raw_is_blob=True, wcdb_ct=4)
        rows = self._rows([record])
        self.assertEqual(rows[0]['message_content'], ZSTD_DECODED)
        self.assertEqual(rows[0]['raw']['WCDB_CT_message_content'], 4)
        self.assertEqual(rows[0]['raw']['message_content']['b64'],
                         b64e(ZSTD_FRAME))
        self.assertEqual(rows[0]['message_content_raw_b64'], b64e(ZSTD_FRAME))

    def test_compressed_row_with_any_decoded_text_is_not_rejected(self):
        # positive control: decoded text is NOT compared to compressed bytes
        record = make_record(content='anything the decoder produced',
                             raw_content=ZSTD_FRAME, raw_is_blob=True,
                             wcdb_ct=4)
        self.assertEqual(len(self._rows([record])), 1)

    def test_invalid_utf8_text_row_loads_with_null_content(self):
        invalid = bytes([0xff, 0xfe, 0xfd])
        record = make_record(content=None, raw_content=invalid,
                             decode_status='utf8_invalid')
        record['raw']['message_content'] = {'b64': b64e(invalid),
                                            'utf8': False}
        _resign(record)
        rows = self._rows([record])
        self.assertIsNone(rows[0]['message_content'])
        self.assertEqual(rows[0]['message_content_decode_status'],
                         'utf8_invalid')

    def test_null_server_id_row_uses_local_identity(self):
        record = make_record(server_id=0, content='local only')
        rows = self._rows([record])
        self.assertIsNone(rows[0]['identity']['server_id'])
        self.assertEqual(rows[0]['raw']['server_id'], 0)

    def test_packed_info_blob_roundtrip_loads(self):
        packed = b'\x00\x01packed-info-bytes'
        record = make_record(packed=packed)
        rows = self._rows([record])
        self.assertEqual(rows[0]['packed_info_data'], b64e(packed))

    def test_tampered_fingerprint_rejected(self):
        record = make_record()
        record['record_sha256'] = '0' * 64
        with self.assertRaises(ExportError) as cm:
            self._rows([record])
        self.assertEqual(_code(cm.exception), 'export_record_hash_mismatch')

    def test_int_promotion_violation_rejected(self):
        # raw holds an INTEGER but the top level promotes null — impossible
        # from int_or_null; the exporter is lying
        record = make_record()
        record['create_time'] = None
        record['record_sha256'] = sha256_hex(
            canonical({k: v for k, v in record.items()
                       if k != 'record_sha256'}).encode())
        with self.assertRaises(ExportError) as cm:
            self._rows([record])
        self.assertEqual(_code(cm.exception), 'export_raw_inconsistent')

    def test_real_number_promotes_to_null_and_loads(self):
        # a REAL raw column legitimately promotes to null (int_or_null)
        record = make_record()
        record['create_time'] = None
        record['raw']['create_time'] = 1.5
        record['record_sha256'] = sha256_hex(
            canonical({k: v for k, v in record.items()
                       if k != 'record_sha256'}).encode())
        self.assertEqual(len(self._rows([record])), 1)

    def test_guaranteed_text_equality_enforced(self):
        # uncompressed TEXT storage, clean decode, nothing preserved → the
        # top level MUST equal raw
        record = make_record(content='body')
        record['message_content'] = 'different from raw'
        record['record_sha256'] = sha256_hex(
            canonical({k: v for k, v in record.items()
                       if k != 'record_sha256'}).encode())
        with self.assertRaises(ExportError) as cm:
            self._rows([record])
        self.assertEqual(_code(cm.exception), 'export_raw_inconsistent')

    def test_server_id_raw_mismatch_rejected(self):
        record = make_record(server_id=123)
        record['raw']['server_id'] = 456
        record['record_sha256'] = sha256_hex(
            canonical({k: v for k, v in record.items()
                       if k != 'record_sha256'}).encode())
        with self.assertRaises(ExportError) as cm:
            self._rows([record])
        self.assertEqual(_code(cm.exception), 'export_raw_inconsistent')

    def test_order_and_count_violations_rejected(self):
        a = make_record(rowid=1, server_id=1)
        b = make_record(rowid=1, server_id=1)  # duplicate (shard,table,rowid)
        with self.assertRaises(ExportError) as cm:
            self._rows([a, b])
        self.assertEqual(_code(cm.exception), 'export_order_violation')
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            root = build_export(Path(tmp) / 'e', [make_record()])
            manifest = json.loads((root / 'export.json').read_text())
            manifest['records']['count'] = 5
            (root / 'export.json').write_text(canonical(manifest))
            with self.assertRaises(ExportError) as cm:
                list(load_session_export(root).records)
            self.assertEqual(_code(cm.exception), 'export_count_mismatch')

    def test_packed_info_bytes_mismatch_rejected(self):
        record = make_record(packed=b'packed-bytes')
        record['packed_info_data'] = b64e(b'other bytes')  # same length class
        record['record_sha256'] = sha256_hex(
            canonical({k: v for k, v in record.items()
                       if k != 'record_sha256'}).encode())
        with self.assertRaises(ExportError) as cm:
            self._rows([record])
        self.assertEqual(_code(cm.exception), 'export_raw_inconsistent')


# ══════════════════════════════════════════════════════════════════════════
# 2. bounded paged capture end-to-end (directive #2)
# ══════════════════════════════════════════════════════════════════════════
class PagedCollectTest(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.harness = Harness(Path(self.tmp.name) / 'nas')
        self.work = Path(self.tmp.name) / 'mac-work'

    def tearDown(self):
        self.harness.close()
        self.tmp.cleanup()

    def test_many_rows_travel_as_pages_and_apply_atomically(self):
        records = simple_rows(4500)  # 3 pages at PAGE_ROW_LIMIT=2000
        export = build_export(Path(self.tmp.name) / 'export', records)
        collector, transport = self.harness.collector(self.work)
        result = collector.collect_session(export)
        counts = result['receipt']['counts']
        self.assertEqual(counts['rows_inserted'], 4500)
        self.assertEqual(self.harness.q(
            'SELECT count(*) FROM messages')[0], 4500)
        # pages really were used (upload ops for records-page kind)
        self.assertIn('finish_upload', transport.ops)
        # local staging released after the ACK
        self.assertEqual(list((self.work / 'batches').rglob('intent.json')),
                         [])
        # the observation base now carries every identity
        observation = json.loads(self.harness.q(
            'SELECT observation_json FROM source_observations')[0])
        self.assertEqual(len(observation['identities']), 4500)
        # server-side run pages were released from staging
        leftover = self.harness.binding and list(
            (Path(self.tmp.name) / 'nas' / 'staging' / 'objects').glob('*'))
        self.assertEqual(leftover, [])

    def test_second_generation_collects_as_presence_refresh(self):
        first = build_export(Path(self.tmp.name) / 'e1', simple_rows(50))
        collector, _ = self.harness.collector(self.work)
        collector.collect_session(first)
        second = build_export(Path(self.tmp.name) / 'e2', simple_rows(50),
                              generation='gen-2', created_at=2000)
        result = collector.collect_session(second)
        counts = result['receipt']['counts']
        self.assertEqual(counts['rows_inserted'], 0)
        self.assertEqual(counts['no_change'], 50)
        self.assertEqual(counts['conflicts'], 0)

    def test_late_row_marked_when_created_before_previous_observation(self):
        collector, _ = self.harness.collector(self.work)
        collector.collect_session(build_export(
            Path(self.tmp.name) / 'e1', simple_rows(10)))
        late_row = make_record(rowid=99, server_id=999999,
                               create_time=50, content='old arrival')
        second = build_export(Path(self.tmp.name) / 'e2',
                              simple_rows(10) + [late_row],
                              generation='gen-2', created_at=2000)
        result = collector.collect_session(second)
        self.assertEqual(result['receipt']['counts']['late'], 1)
        provenance = self.harness.q(
            'SELECT provenance FROM messages WHERE server_id=?',
            str(late_row['identity']['server_id']))[0]
        self.assertEqual(provenance, 'late')

    def test_oversize_previous_observation_walked_in_chunks(self):
        # ~8k identities push the stored observation past the inline cutoff;
        # the second collect must walk it via observation_ids, not one frame
        collector, _ = self.harness.collector(self.work)
        collector.collect_session(build_export(
            Path(self.tmp.name) / 'e1', simple_rows(10500)))
        collector2, transport2 = self.harness.collector(self.work)
        result = collector2.collect_session(build_export(
            Path(self.tmp.name) / 'e2', simple_rows(10500),
            generation='gen-2', created_at=2000))
        self.assertEqual(result['receipt']['counts']['no_change'], 10500)
        self.assertGreaterEqual(transport2.ops.count('observation_ids'), 1)
        self.assertEqual(transport2.ops.count('commit_batch'), 1)


# ══════════════════════════════════════════════════════════════════════════
# 3. restart / disconnect resume (directive #3)
# ══════════════════════════════════════════════════════════════════════════
class ResumeTest(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.harness = Harness(Path(self.tmp.name) / 'nas')
        self.work = Path(self.tmp.name) / 'mac-work'

    def tearDown(self):
        self.harness.close()
        self.tmp.cleanup()

    def test_crash_mid_upload_resumes_and_completes(self):
        records = simple_rows(4500)
        export = build_export(Path(self.tmp.name) / 'export', records)
        collector, _ = self.harness.collector(self.work)
        original = collector.client.transport.request

        state = {'uploads': 0}

        def crashing_request(frame):
            if frame['op'] == 'finish_upload':
                state['uploads'] += 1
                if state['uploads'] >= 2:
                    raise ExportError('transport_closed', 'synthetic crash')
            return original(frame)

        collector.client.transport.request = crashing_request
        with self.assertRaises(ArchiveError):
            collector.collect_session(export)
        # intent persisted with pages planned and some uploads outstanding
        intents = list(self.work.rglob('intent.json'))
        self.assertEqual(len(intents), 1)
        intent = json.loads(intents[0].read_text())
        self.assertEqual(len(intent['pages']), 3)  # 4500 rows / 2000
        self.assertTrue(any(p.get('uploaded') for p in intent['pages']))
        # endpoint process restart, fresh collector, same work dir
        self.harness.restart()
        collector2, _ = self.harness.collector(self.work)
        result = collector2.collect_session(export)
        self.assertFalse(result['replay'])
        self.assertEqual(result['receipt']['counts']['rows_inserted'], 4500)
        self.assertEqual(self.harness.q('SELECT count(*) FROM messages')[0],
                         4500)
        # no duplicated objects: exactly zero media here, staging released
        leftovers = list((Path(self.tmp.name) / 'nas' / 'staging' / 'objects')
                         .glob('*'))
        self.assertEqual(leftovers, [])

    def test_lost_ack_replays_receipt_after_endpoint_restart(self):
        export = build_export(Path(self.tmp.name) / 'export', simple_rows(30))
        collector, transport = self.harness.collector(self.work)
        original = transport.request

        def drop_commit(frame):
            reply = original(frame)  # the endpoint DOES commit
            if frame['op'] == 'commit_batch' and reply.get('ok') and \
                    getattr(drop_commit, 'armed', True):
                drop_commit.armed = False
                raise ExportError('transport_closed', 'synthetic lost ACK')
            return reply
        transport.request = drop_commit
        with self.assertRaises(ArchiveError):
            collector.collect_session(export)
        self.assertEqual(self.harness.q('SELECT count(*) FROM messages')[0],
                         30)
        committed_seq = self.harness.store.commit_seq()
        self.harness.restart()
        collector2, _ = self.harness.collector(self.work)
        result = collector2.collect_session(export)
        self.assertTrue(result['replay'])
        self.assertEqual(self.harness.store.commit_seq(), committed_seq)
        self.assertEqual(self.harness.q('SELECT count(*) FROM messages')[0],
                         30)

    def test_open_run_after_disconnect_reuses_persisted_c0(self):
        export = build_export(Path(self.tmp.name) / 'export', simple_rows(5))
        collector, transport = self.harness.collector(self.work)
        original = transport.request

        def disconnect_after_begin(frame):
            reply = original(frame)
            if frame['op'] == 'begin_run':
                raise ExportError('transport_closed', 'cut after begin_run')
            return reply
        transport.request = disconnect_after_begin
        with self.assertRaises(ArchiveError):
            collector.collect_session(export)
        # the run exists server-side and stays open with its persisted c0
        runs = self.harness.q('SELECT count(*) FROM runs')[0]
        self.assertEqual(runs, 1)
        self.harness.restart()
        collector2, transport2 = self.harness.collector(self.work)
        result = collector2.collect_session(export)
        self.assertFalse(result['replay'])
        self.assertEqual(result['receipt']['counts']['rows_inserted'], 5)


# ══════════════════════════════════════════════════════════════════════════
# 3b. causal claim authority (controller red-gate mirrors, implementer side)
# ══════════════════════════════════════════════════════════════════════════
class CausalClaimTest(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.harness = Harness(Path(self.tmp.name) / 'nas')
        self.work = Path(self.tmp.name) / 'mac-work'

    def tearDown(self):
        self.harness.close()
        self.tmp.cleanup()

    def test_claimed_run_may_overwrite(self):
        collector, _ = self.harness.collector(self.work)
        collector.collect_session(build_export(
            Path(self.tmp.name) / 'e1',
            [make_record(rowid=1, server_id=101, content='v1')]))
        second = Path(self.tmp.name) / 'e2'
        collector.claim_source_snapshot(
            second, ACCOUNT, TALKER,
            source_runner=lambda claim: build_export(
                second, [make_record(rowid=1, server_id=101, content='v2')],
                generation='gen-2', created_at=2000))
        result = collector.collect_session(second)
        self.assertEqual(result['receipt']['counts']['rows_updated'], 1)
        self.assertEqual(self.harness.q(
            'SELECT content_text FROM messages')[0], 'v2')

    def test_unclaimed_changed_update_refused(self):
        collector, _ = self.harness.collector(self.work)
        collector.collect_session(build_export(
            Path(self.tmp.name) / 'e1',
            [make_record(rowid=1, server_id=101, content='accepted')]))
        result = collector.collect_session(build_export(
            Path(self.tmp.name) / 'e2',
            [make_record(rowid=1, server_id=101, content='unproven')],
            generation='gen-2', created_at=2000))
        conflicts = result['receipt']['conflicts']
        self.assertEqual([c['reason'] for c in conflicts],
                         ['update_requires_claim'])
        self.assertEqual(result['receipt']['counts']['missing_refused_unclaimed'],
                         0)
        self.assertEqual(self.harness.q(
            'SELECT content_text FROM messages')[0], 'accepted')

    def test_claim_after_export_exists_refused(self):
        collector, _ = self.harness.collector(self.work)
        collector.collect_session(build_export(
            Path(self.tmp.name) / 'e1',
            [make_record(rowid=1, server_id=101, content='accepted')]))
        stale = build_export(Path(self.tmp.name) / 'already-read',
                             [make_record(rowid=1, server_id=101,
                                          content='stale')],
                             generation='old-view')
        with self.assertRaises(ArchiveError) as cm:
            collector.claim_source_snapshot(stale, ACCOUNT, TALKER)
        self.assertEqual(_code(cm.exception), 'claim_not_causal')
        # and nothing was overwritten
        self.assertEqual(self.harness.q(
            'SELECT content_text FROM messages')[0], 'accepted')

    def test_tampered_claim_c0_fails_closed(self):
        collector, _ = self.harness.collector(self.work)
        collector.collect_session(build_export(
            Path(self.tmp.name) / 'e1',
            [make_record(rowid=1, server_id=101, content='v1')]))
        second = Path(self.tmp.name) / 'e2'
        claim = collector.claim_source_snapshot(
            second, ACCOUNT, TALKER,
            source_runner=lambda c: build_export(
                second, [make_record(rowid=1, server_id=101, content='v2')],
                generation='gen-2', created_at=2000))
        claim['c0'] = int(claim['c0']) + 5
        (second / 'snapshot-claim.json').write_text(canonical(claim))
        with self.assertRaises(ArchiveError) as cm:
            collector.collect_session(second)
        self.assertEqual(_code(cm.exception), 'claim_binding_mismatch')

    def test_reclaim_reuses_existing_claim_without_new_run(self):
        collector, _ = self.harness.collector(self.work)
        second = Path(self.tmp.name) / 'e2'
        collector.claim_source_snapshot(
            second, ACCOUNT, TALKER,
            source_runner=lambda c: build_export(
                second, simple_rows(1), generation='gen-1'))
        runs_before = self.harness.q('SELECT count(*) FROM runs')[0]
        seen = []
        collector.claim_source_snapshot(
            second, ACCOUNT, TALKER, source_runner=seen.append)
        self.assertEqual(len(seen), 1)
        self.assertEqual(self.harness.q('SELECT count(*) FROM runs')[0],
                         runs_before)


# ══════════════════════════════════════════════════════════════════════════
# 4. missing / 迁片 / 后下载旧媒体 (directive #4)
# ══════════════════════════════════════════════════════════════════════════
class MissingSemanticsTest(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.harness = Harness(Path(self.tmp.name) / 'nas')
        self.work = Path(self.tmp.name) / 'mac-work'

    def tearDown(self):
        self.harness.close()
        self.tmp.cleanup()

    def test_real_missing_published_only_via_second_recheck(self):
        rows = [make_record(rowid=1, server_id=101, content='A'),
                make_record(rowid=2, server_id=102, content='B'),
                make_record(rowid=3, server_id=103, content='C')]
        first = build_export(Path(self.tmp.name) / 'e1', rows,
                             created_at=1000)
        collector, _ = self.harness.collector(self.work)
        first_result = collector.collect_session(first)
        self.assertEqual(first_result['missing_candidates'], [])

        second = Path(self.tmp.name) / 'e2'
        collector.claim_source_snapshot(
            second, ACCOUNT, TALKER,
            source_runner=lambda claim: build_export(
                second, rows[1:], generation='gen-2', created_at=2000))
        second_result = collector.collect_session(second)
        keys = [c['key'] for c in second_result['missing_candidates']]
        self.assertEqual(keys, ['s:101'])
        # collect alone does NOT publish missing — the row is untouched
        self.assertEqual(self.harness.q(
            'SELECT count(*) FROM missing_observations')[0], 0)
        self.assertEqual(self.harness.q('SELECT count(*) FROM messages')[0], 3)

        # second opinion: absence is PROVEN by a THIRD, fresher, complete
        # recheck with its own claim — never by the export that noticed it
        recheck = Path(self.tmp.name) / 'e3'
        collector.claim_source_snapshot(
            recheck, ACCOUNT, TALKER,
            source_runner=lambda claim: build_export(
                recheck, rows[1:], generation='gen-3', created_at=3000))
        submitted = collector.submit_missing(
            recheck, candidates=second_result['missing_candidates'],
            baseline_created_at=first_result['snapshot_created_at'])
        self.assertEqual(submitted['submitted'], 1)
        self.assertEqual(submitted['refused'], [])
        self.assertEqual(self.harness.q(
            'SELECT count(*) FROM missing_observations')[0], 1)
        soft = self.harness.q(
            'SELECT soft, msg_uid FROM missing_observations')
        self.assertEqual(soft[0], 1)
        # soft missing never deletes or hides the archived row
        self.assertEqual(self.harness.q(
            "SELECT visibility FROM messages WHERE server_id='101'")[0],
            'visible')

    def test_stale_recheck_refuses_missing(self):
        rows = [make_record(rowid=1, server_id=101, content='A')]
        collector, _ = self.harness.collector(self.work)
        collector.collect_session(build_export(
            Path(self.tmp.name) / 'e1', rows, created_at=2000))
        recheck = Path(self.tmp.name) / 'e2'
        collector.claim_source_snapshot(
            recheck, ACCOUNT, TALKER,
            source_runner=lambda claim: build_export(
                recheck, [], generation='gen-2', created_at=1000))
        candidates = [{'key': 's:101',
                       'identity': {'server_id': '101'}}]
        with self.assertRaises(ArchiveError) as cm:
            collector.submit_missing(recheck, candidates=candidates,
                                     baseline_created_at=2000)
        self.assertEqual(_code(cm.exception), 'missing_recheck_stale')

    def test_topology_change_refuses_local_only_missing(self):
        # candidate lives in message_0.db; the recheck session only has
        # message_1.db — the row may have MIGRATED, absence is unprovable
        collector, _ = self.harness.collector(self.work)
        collector.collect_session(build_export(
            Path(self.tmp.name) / 'e1', simple_rows(2)))
        recheck = Path(self.tmp.name) / 'e2'
        collector.claim_source_snapshot(
            recheck, ACCOUNT, TALKER,
            source_runner=lambda claim: build_export(
                recheck,
                [make_record(shard='message_1.db', rowid=1, server_id=201,
                             content='elsewhere')],
                generation='gen-2', created_at=2000))
        candidates = [{'key': 'l:message_0.db:Msg_synthetic:1',
                       'identity': {'shard': 'message_0.db',
                                    'table': 'Msg_synthetic',
                                    'local_rowid': 1}}]
        submitted = collector.submit_missing(
            recheck, candidates=candidates, baseline_created_at=1000)
        self.assertEqual(submitted['submitted'], 0)
        self.assertEqual(submitted['refused'][0]['reason'],
                         'missing_topology_changed')

    def test_shard_migration_is_update_not_missing(self):
        # 迁片: same server identity moves message_0.db#1 → message_1.db#9
        collector, _ = self.harness.collector(self.work)
        collector.collect_session(build_export(
            Path(self.tmp.name) / 'e1',
            [make_record(rowid=1, server_id=777, content='migrated row')]))
        moved = make_record(shard='message_1.db', table='Msg_synthetic',
                            rowid=9, server_id=777, content='migrated row')
        second = Path(self.tmp.name) / 'e2'
        collector.claim_source_snapshot(
            second, ACCOUNT, TALKER,
            source_runner=lambda claim: build_export(
                second, [moved], generation='gen-2', created_at=2000))
        result = collector.collect_session(second)
        self.assertEqual(result['missing_candidates'], [])
        self.assertEqual(result['receipt']['counts']['rows_updated'], 1)
        row = self.harness.q(
            'SELECT shard, local_rowid FROM messages WHERE server_id=?',
            '777')
        self.assertEqual((row[0], row[1]), ('message_1.db', 9))
        self.assertEqual(self.harness.q(
            'SELECT count(*) FROM messages')[0], 1)

    def test_incomplete_enumeration_never_reports_missing(self):
        rows = [make_record(rowid=1, server_id=101, content='A'),
                make_record(rowid=2, server_id=102, content='B')]
        collector, _ = self.harness.collector(self.work)
        collector.collect_session(build_export(
            Path(self.tmp.name) / 'e1', rows))
        incomplete = build_export(Path(self.tmp.name) / 'e2', rows[1:],
                                  generation='gen-2', created_at=2000,
                                  enumeration_complete=False)
        result = collector.collect_session(incomplete)
        self.assertEqual(result['missing_candidates'], [])


# ══════════════════════════════════════════════════════════════════════════
# 5. media taxonomy (directive #5)
# ══════════════════════════════════════════════════════════════════════════
class MediaTaxonomyTest(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.harness = Harness(Path(self.tmp.name) / 'nas')
        self.work = Path(self.tmp.name) / 'mac-work'

    def tearDown(self):
        self.harness.close()
        self.tmp.cleanup()

    def _collect_with_refs(self, refs, media_files, name='e'):
        record = make_record(rowid=1, server_id=42, media_refs=refs)
        export = build_export(Path(self.tmp.name) / name, [record],
                              media_files=media_files)
        collector, _ = self.harness.collector(self.work)
        return collector.collect_session(export), export

    def test_healthy_media_upload_commits_asset(self):
        data = b'decoded-image-bytes'
        result, _ = self._collect_with_refs(
            [media_ref('md5_ok', data)], {'md5_ok': data})
        self.assertEqual(result['receipt']['counts']['media_assets_stored'], 1)
        self.assertEqual(self.harness.q(
            'SELECT count(*) FROM media_needs')[0], 0)
        self.assertEqual(self.harness.q(
            "SELECT state FROM objects WHERE kind='image'")[0], 'committed')

    def test_vanished_after_export_is_gap_not_hard_failure(self):
        data = b'video-bytes'
        record = make_record(rowid=1, server_id=42,
                             media_refs=[media_ref('md5_gone', data)])
        export = build_export(Path(self.tmp.name) / 'e', [record],
                              media_files={'md5_gone': data})
        (export / 'media' / 'md5_gone').unlink()
        collector, _ = self.harness.collector(self.work)
        result = collector.collect_session(export)
        need = self.harness.q('SELECT state, last_error FROM media_needs')
        self.assertEqual(need[0], 'vanished')
        self.assertEqual(need[1], 'media_vanished_midflight')
        self.assertEqual(self.harness.q(
            'SELECT count(*) FROM media_assets')[0], 0)

    def test_length_mismatch_is_hard_failure_nothing_committed(self):
        data = b'file-bytes'
        record = make_record(rowid=1, server_id=42,
                             media_refs=[media_ref('md5_len', data)])
        export = build_export(Path(self.tmp.name) / 'e', [record],
                              media_files={'md5_len': data})
        (export / 'media' / 'md5_len').write_bytes(b'short')  # truncated
        collector, _ = self.harness.collector(self.work)
        with self.assertRaises(ExportError) as cm:
            collector.collect_session(export)
        self.assertEqual(_code(cm.exception), 'media_file_mismatch')
        self.assertEqual(self.harness.q('SELECT count(*) FROM messages')[0], 0)
        self.assertEqual(self.harness.q(
            'SELECT count(*) FROM media_assets')[0], 0)

    def test_hash_mismatch_is_hard_failure(self):
        data = b'some-bytes'
        record = make_record(rowid=1, server_id=42,
                             media_refs=[media_ref('md5_hash', data)])
        export = build_export(Path(self.tmp.name) / 'e', [record],
                              media_files={'md5_hash': data})
        (export / 'media' / 'md5_hash').write_bytes(b'other-bytes!!')  # same len
        collector, _ = self.harness.collector(self.work)
        with self.assertRaises(ExportError) as cm:
            collector.collect_session(export)
        self.assertEqual(_code(cm.exception), 'media_file_mismatch')
        self.assertEqual(self.harness.q('SELECT count(*) FROM messages')[0], 0)

    @unittest.skipIf(os.geteuid() == 0, 'root ignores file permissions')
    def test_permission_error_is_hard_failure(self):
        data = b'protected-bytes'
        record = make_record(rowid=1, server_id=42,
                             media_refs=[media_ref('md5_perm', data)])
        export = build_export(Path(self.tmp.name) / 'e', [record],
                              media_files={'md5_perm': data})
        target = export / 'media' / 'md5_perm'
        target.chmod(0o000)
        self.addCleanup(lambda: target.exists() and target.chmod(0o600))
        collector, _ = self.harness.collector(self.work)
        with self.assertRaises(ArchiveError) as cm:
            collector.collect_session(export)
        self.assertEqual(_code(cm.exception), 'media_permission_denied')
        self.assertEqual(self.harness.q('SELECT count(*) FROM messages')[0], 0)

    def test_proven_source_absence_is_open_need(self):
        result, _ = self._collect_with_refs(
            [media_ref('md5_missing', reason='no local .dat found '
                        '(attach layout and hardlink.db exhausted)')], None)
        need = self.harness.q('SELECT state, last_error FROM media_needs')
        self.assertEqual(need[0], 'open')
        self.assertEqual(need[1], 'not_present_in_source')
        self.assertEqual(result['receipt']['counts']['media_needs_open'], 1)

    def test_undecodable_source_is_error_gap_not_open(self):
        result, _ = self._collect_with_refs(
            [media_ref('md5_undecodable',
                       reason='image .dat decode failed '
                              '(keys stay Mac-side): bad format')], None)
        need = self.harness.q('SELECT state, last_error FROM media_needs')
        self.assertEqual(need[0], 'error')
        self.assertEqual(need[1], 'source_decode_failed')

    def test_late_downloaded_old_media_fulfils_open_need(self):
        # 后下载旧媒体: gen-1 lacks the bytes (open need); gen-2 has them
        record1 = make_record(rowid=1, server_id=42,
                              media_refs=[media_ref('md5_late')])
        collector, _ = self.harness.collector(self.work)
        collector.collect_session(build_export(
            Path(self.tmp.name) / 'e1', [record1], created_at=1000))
        self.assertEqual(self.harness.q(
            'SELECT state FROM media_needs')[0], 'open')

        data = b'late-downloaded-bytes'
        record2 = make_record(rowid=1, server_id=42, content='body',
                              media_refs=[media_ref('md5_late', data)])
        result = collector.collect_session(build_export(
            Path(self.tmp.name) / 'e2', [record2], generation='gen-2',
            created_at=2000, media_files={'md5_late': data}))
        self.assertEqual(result['receipt']['counts']['media_assets_stored'], 1)
        self.assertEqual(result['receipt']['counts']['media_needs_resolved'], 1)
        self.assertEqual(self.harness.q(
            'SELECT state FROM media_needs')[0], 'fulfilled')

    def test_media_path_escape_rejected(self):
        data = b'bytes'
        record = make_record(rowid=1, server_id=42,
                             media_refs=[media_ref('md5_escape', data)])
        record['media_refs'][0]['filename'] = 'media/../../escape'
        _resign(record)
        export = build_export(Path(self.tmp.name) / 'e', [record],
                              media_files={'md5_escape': data})
        collector, _ = self.harness.collector(self.work)
        with self.assertRaises(ExportError) as cm:
            collector.collect_session(export)
        self.assertEqual(_code(cm.exception), 'media_path_escape_rejected')


# ══════════════════════════════════════════════════════════════════════════
# 6. endpoint page gates + helpers (directive #2 rejections)
# ══════════════════════════════════════════════════════════════════════════
class EndpointPageGateTest(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        SyntheticGuard.write_marker(self.root, ARCHIVE, 1, 0)
        self.binding = SyntheticGuard(ARCHIVE).verify(self.root)
        self.store = MasterStore.initialize(
            self.binding, ARCHIVE, {ACCOUNT: {TALKER: {'capture': True}}})
        self.endpoint = Endpoint(self.binding, ARCHIVE)
        self.endpoint.store = self.store
        self.endpoint.auth = {'roles': {
            'collector': {'token_sha256': sha256_hex(b'impl-token')}}}
        self.staging = StagingManager(self.binding)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def _page_bytes(self, ops):
        return ''.join(canonical(op) + '\n' for op in ops).encode('utf-8')

    def _stage(self, data, batch_id='batch-pages'):
        sha = sha256_hex(data)
        self.staging.begin_upload('up-' + sha[:16], 'records-page', sha,
                                  len(data), batch_id)
        self.staging.write_chunk('up-' + sha[:16], 0, data, sha)
        self.staging.finish_upload('up-' + sha[:16], 1)
        return {'sha256': sha, 'length': len(data)}

    def _commit(self, run, batch_id, media_uploads=None):
        return self.endpoint.op_commit_batch({
            'batch_id': batch_id, 'run': run,
            'content_sha256': sha256_hex(canonical(run).encode('utf-8')),
            'media_uploads': media_uploads or []})

    def _upsert(self, body='paged row', rowid=1):
        return {'op': 'upsert',
                'identity': {'server_id': str(1000 + rowid),
                             'shard': 'message_0.db',
                             'table': 'Msg_t', 'local_rowid': rowid},
                'record': {'message_content': body,
                           'record_sha256': sha256_hex(body.encode())}}

    def test_paged_commit_applies_all_pages_atomically(self):
        c0 = self.store.begin_run('run-pages', ACCOUNT, TALKER, 'capture')
        data1 = self._page_bytes([self._upsert('one', 1)])
        data2 = self._page_bytes([self._upsert('two', 2)])
        p1, p2 = self._stage(data1), self._stage(data2)
        run = {'run_id': 'run-pages', 'account': ACCOUNT, 'talker': TALKER,
               'kind': 'capture', 'c0': c0, 'rows': [],
               'record_pages': [dict(p1, rows=1), dict(p2, rows=1)],
               'media': {'uploads': [], 'needs': []}}
        receipt = self._commit(run, 'batch-pages')
        self.assertEqual(receipt['counts']['rows_inserted'], 2)
        self.assertEqual(self.store.db.execute(
            'SELECT count(*) FROM messages').fetchone()[0], 2)

    def test_declared_rows_mismatch_rejects_whole_batch(self):
        c0 = self.store.begin_run('run-mismatch', ACCOUNT, TALKER, 'capture')
        data = self._page_bytes([self._upsert('a', 1), self._upsert('b', 2)])
        page = self._stage(data)
        run = {'run_id': 'run-mismatch', 'account': ACCOUNT, 'talker': TALKER,
               'kind': 'capture', 'c0': c0, 'rows': [],
               'record_pages': [dict(page, rows=5)],  # lies: only 2 lines
               'media': {'uploads': [], 'needs': []}}
        with self.assertRaises(ArchiveError) as cm:
            self._commit(run, 'batch-mismatch')
        self.assertEqual(_code(cm.exception), 'page_rows_mismatch')
        self.assertEqual(self.store.db.execute(
            'SELECT count(*) FROM messages').fetchone()[0], 0)

    def test_pages_over_limit_rejected_before_state(self):
        c0 = self.store.begin_run('run-limit', ACCOUNT, TALKER, 'capture')
        data = self._page_bytes([self._upsert()])
        page = self._stage(data)
        run = {'run_id': 'run-limit', 'account': ACCOUNT, 'talker': TALKER,
               'kind': 'capture', 'c0': c0, 'rows': [],
               'record_pages': [dict(page, rows=1), dict(page, rows=1)],
               'media': {'uploads': [], 'needs': []}}
        with mock.patch.object(endpoint_module, 'MAX_RECORD_PAGES', 1):
            with self.assertRaises(ArchiveError) as cm:
                self._commit(run, 'batch-limit')
        self.assertEqual(_code(cm.exception), 'run_pages_exceeded')
        self.assertIsNone(self.store.get_batch('batch-limit'))

    def test_duplicate_page_rejected(self):
        c0 = self.store.begin_run('run-dup', ACCOUNT, TALKER, 'capture')
        data = self._page_bytes([self._upsert()])
        page = dict(self._stage(data), rows=1)
        run = {'run_id': 'run-dup', 'account': ACCOUNT, 'talker': TALKER,
               'kind': 'capture', 'c0': c0, 'rows': [],
               'record_pages': [page, dict(page)],
               'media': {'uploads': [], 'needs': []}}
        with self.assertRaises(ArchiveError) as cm:
            self._commit(run, 'batch-dup')
        self.assertEqual(_code(cm.exception), 'page_duplicate')
        self.assertIsNone(self.store.get_batch('batch-dup'))

    def test_missing_page_object_rejected_before_state(self):
        c0 = self.store.begin_run('run-ghost', ACCOUNT, TALKER, 'capture')
        run = {'run_id': 'run-ghost', 'account': ACCOUNT, 'talker': TALKER,
               'kind': 'capture', 'c0': c0, 'rows': [],
               'record_pages': [{'sha256': 'a' * 64, 'length': 10, 'rows': 1}],
               'media': {'uploads': [], 'needs': []}}
        with self.assertRaises(ArchiveError) as cm:
            self._commit(run, 'batch-ghost')
        self.assertEqual(_code(cm.exception), 'staging_object_missing')
        self.assertIsNone(self.store.get_batch('batch-ghost'))

    def test_hello_announces_machine_readable_limits(self):
        response = self.endpoint.op_hello({
            'role': 'collector',
            'token': 'impl-token', 'archive_id': ARCHIVE})
        limits = response['limits']
        self.assertEqual(limits['max_record_pages'],
                         endpoint_module.MAX_RECORD_PAGES)
        self.assertEqual(limits['max_total_rows'],
                         endpoint_module.MAX_TOTAL_ROWS)
        self.assertEqual(limits['chunk_max_bytes'],
                         endpoint_module.CHUNK_MAX_BYTES)


# ══════════════════════════════════════════════════════════════════════════
# 7. small units: PageWriter bounds, gap taxonomy, identity keys
# ══════════════════════════════════════════════════════════════════════════
class UnitsTest(unittest.TestCase):
    def test_page_writer_splits_on_rows_and_bytes(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            writer = PageWriter(tmp, 'rec', row_limit=3, byte_target=120)
            for i in range(7):
                writer.add({'n': i, 'pad': 'x' * 20})
            pages = writer.close()
            self.assertEqual([p['rows'] for p in pages], [3, 3, 1])
            for page in pages:
                self.assertEqual(page['length'],
                                 (Path(tmp) / page['file']).stat().st_size)
                self.assertEqual(page['sha256'], sha256_hex(
                    (Path(tmp) / page['file']).read_bytes()))

            # byte target splits before the row limit
            writer = PageWriter(tmp, 'big', row_limit=100, byte_target=1024)
            for i in range(5):
                writer.add({'n': i, 'pad': 'y' * 400})
            pages = writer.close()
            self.assertEqual([p['rows'] for p in pages], [2, 2, 1])
            for page in pages:
                self.assertLessEqual(page['length'], 1024)

    def test_gap_need_taxonomy(self):
        ident = {'shard': 'message_0.db', 'table': 'Msg_t', 'local_rowid': 1}
        open_need = _gap_need(ident, {'ref_key': 'r1', 'kind': 'image',
                                      'present': False,
                                      'reason': 'no local .dat found '
                                                '(attach layout and '
                                                'hardlink.db exhausted)'})
        self.assertEqual((open_need['state'], open_need['error']),
                         ('open', 'not_present_in_source'))
        error_need = _gap_need(ident, {'ref_key': 'r2', 'kind': 'image',
                                       'present': False,
                                       'reason': 'image .dat decode failed: x'})
        self.assertEqual((error_need['state'], error_need['error']),
                         ('error', 'source_decode_failed'))
        unknown_need = _gap_need(ident, {'ref_key': 'r3', 'present': False,
                                         'reason': 'something novel'})
        self.assertEqual(unknown_need['state'], 'error')

    def test_identity_from_key_roundtrip(self):
        server = identity_from_key('s:7001234567890123456')
        self.assertEqual(server, {'server_id': '7001234567890123456'})
        local = identity_from_key('l:message_0.db:Msg_abc:42')
        self.assertEqual(local, {'shard': 'message_0.db', 'table': 'Msg_abc',
                                 'local_rowid': 42})


# ══════════════════════════════════════════════════════════════════════════
# 8. real Rust export → Python load (directive #1, via the cargo gate)
# ══════════════════════════════════════════════════════════════════════════
class RustInteropTest(unittest.TestCase):
    def test_real_rust_export_loads_in_python(self):
        """Drive the actual wx-cli binary through its own gate test: it
        synthesises an encrypted snapshot generation, exports a COMPRESSED
        session with the real producer, and loads it with the real
        load_session_export in a python3 subprocess."""
        repo = Path(__file__).resolve().parents[2]
        if not shutil.which('cargo') or \
                not (repo / 'crates' / 'wx-cli').is_dir():
            self.skipTest('cargo / rust workspace not available')
        try:
            result = subprocess.run(
                ['cargo', 'test', '-p', 'wx-cli',
                 '--test', 'codex_archive_inspect_gate',
                 'codex_compressed_source_reaches_python_collector_losslessly',
                 '--', '--exact'],
                cwd=repo, capture_output=True, timeout=900)
        except subprocess.TimeoutExpired:
            self.fail('cargo gate test timed out')
        self.assertEqual(
            result.returncode, 0,
            'real Rust export rejected by the Python loader:\n' +
            result.stderr.decode('utf-8', 'replace')[-2000:])


if __name__ == '__main__':
    unittest.main()
