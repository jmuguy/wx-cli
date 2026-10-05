"""Implementer-side gates for server-authoritative run binding, C0, the
media double-manifest cross-check, and contract-v2 identity/raw records.

These complement the immutable Codex gates with the cases they do not cover:
  * run_epoch_stale / kind mismatch / duplicate run_id;
  * rejection happens BEFORE any batch row exists (not just before apply);
  * unchanged re-observation writes a full after-image 'presence' mutation;
  * revisions carry COMPLETE previous+new records, not fingerprints;
  * the two media lists (frame vs run manifest) are cross-verified —
    one-sided entries, length/kind conflicts and duplicates all reject;
  * a staged object that is missing on disk, or whose content no longer
    hashes to its address, can never be ACKed;
  * local identity is (database, table, rowid): the same Msg table + rowid
    in two different message_N.db shards is TWO rows, never a collision;
  * `raw` (complete original columns) is stored separately from the text
    view and round-trips through the mutation log.
"""
import json
import unittest

from archive.v3.endpoint import Endpoint, StagingManager
from archive.v3.errors import ArchiveError
from archive.v3.guard import SyntheticGuard
from archive.v3.master import MasterStore
from archive.v3.util import canonical, sha256_hex


def _code(exc):
    return getattr(exc, 'code', None)


class FixtureBase(unittest.TestCase):
    ARCHIVE = 'binding-fixture'

    def setUp(self):
        import tempfile
        from pathlib import Path
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        SyntheticGuard.write_marker(self.root, self.ARCHIVE, 1, 0)
        self.binding = SyntheticGuard(self.ARCHIVE).verify(self.root)
        self.store = MasterStore.initialize(
            self.binding, self.ARCHIVE,
            {'acct': {name: {'capture': True}
                      for name in ('talker', 'other')}})
        self.endpoint = Endpoint(self.binding, self.ARCHIVE)
        self.endpoint.store = self.store
        self.staging = StagingManager(self.binding)

    def tearDown(self):
        self.store.close()

    # ── helpers ──────────────────────────────────────────────────────
    def begin(self, run_id, talker='talker', kind='capture', claim=False):
        return self.store.begin_run(run_id, 'acct', talker, kind, claim=claim)

    def run_payload(self, run_id, c0, rows, talker='talker', kind='capture',
                    uploads=None):
        run = {'run_id': run_id, 'account': 'acct', 'talker': talker,
               'kind': kind, 'c0': c0, 'rows': rows,
               'media': {'uploads': uploads or [], 'needs': []}}
        return run

    def upsert(self, server_id, shard, table, rowid, body, raw=None,
               sha=None):
        record = {'message_content': body,
                  'record_sha256': sha or sha256_hex(body.encode())}
        if raw is not None:
            record['raw'] = raw
        return {'op': 'upsert',
                'identity': {'server_id': server_id, 'shard': shard,
                             'table': table, 'local_rowid': rowid},
                'record': record}

    def commit(self, batch_id, run, media_uploads=None):
        if media_uploads is None:
            media_uploads = (run.get('media') or {}).get('uploads') or []
        return self.endpoint.op_commit_batch({
            'batch_id': batch_id, 'run': run,
            'content_sha256': sha256_hex(canonical(run).encode()),
            'media_uploads': media_uploads})

    def stage(self, data, batch_id='batch-media', kind='asset'):
        sha = sha256_hex(data)
        upload_id = 'up-' + sha[:16]
        self.staging.begin_upload(upload_id, kind, sha, len(data), batch_id)
        self.staging.write_chunk(upload_id, 0, data, sha)
        self.staging.finish_upload(upload_id, 1)
        return {'sha256': sha, 'length': len(data), 'kind': kind}

    def messages(self):
        return self.store.db.execute(
            'SELECT * FROM messages ORDER BY msg_uid').fetchall()

    def assert_rejected_before_state(self, batch_id):
        """A binding/C0/manifest rejection must leave no batch row at all."""
        self.assertIsNone(self.store.get_batch(batch_id))
        self.assertEqual(len(self.messages()), 0)
        self.assertEqual(self.store.db.execute(
            'SELECT count(*) FROM objects').fetchone()[0], 0)


class RunBindingGate(FixtureBase):
    """C0 and run identity are SERVER state; client claims are only checked
    against it, never consumed."""

    def test_forged_c0_rejected_and_history_preserved(self):
        old = self.begin('old')
        new = self.begin('new')
        self.commit('batch-new', self.run_payload(
            'new', new, [self.upsert('s1', 'message_0.db', 'Msg_a', 1,
                                     'newer content')]))
        with self.assertRaises(ArchiveError) as cm:
            self.commit('batch-old', self.run_payload(
                'old', self.store.commit_seq() + 100,
                [self.upsert('s1', 'message_0.db', 'Msg_a', 1,
                             'stale content')]))
        self.assertEqual(_code(cm.exception), 'run_c0_mismatch')
        self.assertEqual(self.messages()[0]['content_text'], 'newer content')
        self.assertIsNone(self.store.get_batch('batch-old'))

    def test_missing_c0_rejected(self):
        c0 = self.begin('r')
        run = self.run_payload('r', c0, [])
        del run['c0']
        with self.assertRaises(ArchiveError) as cm:
            self.commit('batch-c', run)
        self.assertEqual(_code(cm.exception), 'run_c0_missing')

    def test_missing_run_rejected(self):
        with self.assertRaises(ArchiveError) as cm:
            self.commit('batch-c', self.run_payload('ghost', 0, []))
        self.assertEqual(_code(cm.exception), 'run_unknown')
        self.assert_rejected_before_state('batch-c')

    def test_wrong_talker_rejected(self):
        self.begin('r', talker='other')
        with self.assertRaises(ArchiveError) as cm:
            self.commit('batch-c', self.run_payload(
                'r', self.store.get_run('r')['run_start_seq'], [],
                talker='talker'))
        self.assertEqual(_code(cm.exception), 'run_binding_mismatch')
        self.assert_rejected_before_state('batch-c')

    def test_wrong_kind_rejected(self):
        c0 = self.begin('r', kind='capture')
        with self.assertRaises(ArchiveError) as cm:
            self.commit('batch-c', self.run_payload('r', c0, [], kind='collect'))
        self.assertEqual(_code(cm.exception), 'run_binding_mismatch')
        self.assert_rejected_before_state('batch-c')

    def test_closed_run_rejected(self):
        c0 = self.begin('r')
        self.store.finish_run('r', 'aborted')
        with self.assertRaises(ArchiveError) as cm:
            self.commit('batch-c', self.run_payload(
                'r', c0, [self.upsert('s1', 'message_0.db', 'Msg_a', 1, 'x')]))
        self.assertEqual(_code(cm.exception), 'run_not_open')
        self.assert_rejected_before_state('batch-c')

    def test_epoch_stale_run_rejected(self):
        c0 = self.begin('r')
        with self.store.full_transaction() as tx:
            tx.meta_set('epoch', '2')
        with self.assertRaises(ArchiveError) as cm:
            self.commit('batch-c', self.run_payload(
                'r', c0, [self.upsert('s1', 'message_0.db', 'Msg_a', 1, 'x')]))
        self.assertEqual(_code(cm.exception), 'run_epoch_stale')
        self.assert_rejected_before_state('batch-c')

    def test_duplicate_run_id_rejected(self):
        self.begin('r')
        with self.assertRaises(ArchiveError) as cm:
            self.begin('r')
        self.assertEqual(_code(cm.exception), 'run_exists')

    def test_committed_batch_replays_after_run_closed(self):
        """Replay idempotency must survive the run having been finished by
        the commit itself (replay check precedes the open-run check)."""
        c0 = self.begin('r')
        ack = self.commit('batch-c', self.run_payload(
            'r', c0, [self.upsert('s1', 'message_0.db', 'Msg_a', 1, 'body')]))
        run = self.run_payload(
            'r', c0, [self.upsert('s1', 'message_0.db', 'Msg_a', 1, 'body')])
        replay = self.endpoint.op_commit_batch({
            'batch_id': 'batch-c', 'run': run,
            'content_sha256': sha256_hex(canonical(run).encode()),
            'media_uploads': []})
        self.assertTrue(replay.get('replay'))
        self.assertEqual(replay['commit_seq'], ack['commit_seq'])
        self.assertEqual(len(self.messages()), 1)


class PresenceAndAfterImageGate(FixtureBase):
    def test_unchanged_reobservation_advances_presence_with_full_image(self):
        c0 = self.begin('r1')
        self.commit('b1', self.run_payload(
            'r1', c0, [self.upsert('s1', 'message_0.db', 'Msg_a', 1, 'stable',
                                   raw={'col_x': 1})]))
        c1 = self.begin('r2')
        self.commit('b2', self.run_payload(
            'r2', c1, [self.upsert('s1', 'message_0.db', 'Msg_a', 1, 'stable',
                                   raw={'col_x': 1})]))
        row = self.messages()[0]
        self.assertGreater(row['last_content_or_presence_commit_seq'], c0)
        mut = self.store.db.execute(
            "SELECT after_json FROM mutation_log WHERE table_name='messages' "
            "AND op='presence' ORDER BY seq DESC LIMIT 1").fetchone()
        after = json.loads(mut[0])
        # replayable after-image: full text + raw columns, not a fingerprint
        self.assertEqual(after['content_text'], 'stable')
        self.assertEqual(json.loads(after['raw_record_json']), {'col_x': 1})
        # and an older missing run can no longer mark the row missing
        old_missing = self.run_payload(
            'r1', c0, [{'op': 'missing_candidate',
                        'identity': {'server_id': 's1',
                                     'shard': 'message_0.db', 'table': 'Msg_a',
                                     'local_rowid': 1}}])
        old_missing['recheck'] = {'complete': True, 'identities': {}}
        with self.assertRaises(Exception):
            self.commit('b3', old_missing)
        self.assertEqual(self.store.db.execute(
            'SELECT count(*) FROM missing_observations').fetchone()[0], 0)

    def test_revision_payload_carries_complete_records(self):
        c0 = self.begin('r1')
        self.commit('b1', self.run_payload(
            'r1', c0, [self.upsert('s1', 'message_0.db', 'Msg_a', 1, 'v1 text',
                                   raw={'stage': 'first'})]))
        c1 = self.begin('r2', claim=True)  # updates require a causal run
        self.commit('b2', self.run_payload(
            'r2', c1, [self.upsert('s1', 'message_0.db', 'Msg_a', 1, 'v2 text',
                                   raw={'stage': 'second'})]))
        payload = json.loads(self.store.db.execute(
            'SELECT payload_json FROM message_revisions').fetchone()[0])
        self.assertEqual(payload['previous']['content_text'], 'v1 text')
        self.assertEqual(json.loads(payload['previous']['raw_record_json']),
                         {'stage': 'first'})
        self.assertEqual(payload['new']['content_text'], 'v2 text')
        self.assertEqual(json.loads(payload['new']['raw_record_json']),
                         {'stage': 'second'})


class MediaManifestGate(FixtureBase):
    def _run_with_upload(self, upload):
        c0 = self.begin('r')
        rows = [self.upsert('s1', 'message_0.db', 'Msg_a', 1, 'media row')]
        return c0, self.run_payload('r', c0, rows, uploads=[upload])

    def test_frame_only_entry_rejected(self):
        upload = self.stage(b'media bytes')
        c0, run = self._run_with_upload({})
        frame_list = [upload]
        with self.assertRaises(ArchiveError) as cm:
            self.commit('batch-m', run, media_uploads=frame_list)
        self.assertEqual(_code(cm.exception), 'media_manifest_mismatch')
        self.assert_rejected_before_state('batch-m')

    def test_run_only_entry_rejected(self):
        upload = self.stage(b'media bytes')
        c0, run = self._run_with_upload(upload)
        with self.assertRaises(ArchiveError) as cm:
            self.commit('batch-m', run, media_uploads=[])
        self.assertEqual(_code(cm.exception), 'media_manifest_mismatch')
        self.assert_rejected_before_state('batch-m')

    def test_length_conflict_between_lists_rejected(self):
        upload = self.stage(b'media bytes')
        c0, run = self._run_with_upload(dict(upload, length=upload['length'] + 1))
        with self.assertRaises(ArchiveError) as cm:
            self.commit('batch-m', run, media_uploads=[upload])
        self.assertEqual(_code(cm.exception), 'media_manifest_mismatch')

    def test_duplicate_in_frame_list_rejected(self):
        upload = self.stage(b'media bytes')
        c0, run = self._run_with_upload(upload)
        with self.assertRaises(ArchiveError) as cm:
            self.commit('batch-m', run, media_uploads=[upload, dict(upload)])
        self.assertEqual(_code(cm.exception), 'media_manifest_mismatch')

    def test_missing_staged_object_rejected_without_ack(self):
        phantom = {'sha256': sha256_hex(b'never staged'), 'length': 11}
        c0, run = self._run_with_upload(phantom)
        with self.assertRaises(ArchiveError) as cm:
            self.commit('batch-m', run, media_uploads=[phantom])
        self.assertEqual(_code(cm.exception), 'staging_object_missing')
        self.assert_rejected_before_state('batch-m')

    def test_tampered_staged_object_rejected_without_ack(self):
        upload = self.stage(b'original media bytes')
        # rewrite the staged object in place: length matches, content does
        # not hash to its address any more
        self.binding.write_file_atomic(
            f'staging/objects/{upload["sha256"]}',
            b'tampered media bytes')
        c0, run = self._run_with_upload(upload)
        with self.assertRaises(ArchiveError) as cm:
            self.commit('batch-m', run, media_uploads=[upload])
        self.assertEqual(_code(cm.exception), 'staging_hash_mismatch')
        self.assert_rejected_before_state('batch-m')
        self.assertEqual(self.store.db.execute(
            'SELECT count(*) FROM media_assets').fetchone()[0], 0)

    def test_verified_upload_commits_asset_and_ack(self):
        upload = self.stage(b'real media bytes')
        c0, run = self._run_with_upload(upload)
        ack = self.commit('batch-m', run, media_uploads=[upload])
        self.assertGreaterEqual(ack['counts']['media_assets_stored'], 1)
        self.assertEqual(self.store.db.execute(
            'SELECT count(*) FROM objects').fetchone()[0], 1)
        self.assertEqual(ack['counts']['rows_inserted'], 1)


class LocalIdentityV2Gate(FixtureBase):
    """Contract v2: local identity = (database shard, table, rowid)."""

    def _local_upsert(self, shard, table, rowid, body):
        return self.upsert(None, shard, table, rowid, body)

    def test_same_table_same_rowid_across_databases_is_two_rows(self):
        c0 = self.begin('r1')
        self.commit('b1', self.run_payload('r1', c0, [
            self._local_upsert('message_0.db', 'Msg_same', 7, 'from db0'),
            self._local_upsert('message_1.db', 'Msg_same', 7, 'from db1'),
        ]))
        rows = self.messages()
        self.assertEqual(len(rows), 2)
        self.assertEqual({r['content_text'] for r in rows},
                         {'from db0', 'from db1'})
        self.assertEqual({r['shard'] for r in rows},
                         {'message_0.db', 'message_1.db'})

    def test_same_database_same_table_rowid_reuse_binds_identity(self):
        c0 = self.begin('r1')
        self.commit('b1', self.run_payload('r1', c0, [
            self._local_upsert('message_0.db', 'Msg_a', 9, 'first life')]))
        c1 = self.begin('r2')
        # rowid 9 reused for a different logical row in the same slot
        self.commit('b2', self.run_payload('r2', c1, [
            self.upsert('srv-new', 'message_0.db', 'Msg_a', 9,
                        'second life')]))
        rows = self.messages()
        self.assertEqual(len(rows), 2)
        self.assertEqual({r['content_text'] for r in rows},
                         {'first life', 'second life'})
        binds = self.store.db.execute(
            "SELECT count(*) FROM mutation_log WHERE table_name='identity_map' "
            "AND op='identity_bind'").fetchone()[0]
        self.assertEqual(binds, 1)

    def test_local_identity_without_table_rejected(self):
        c0 = self.begin('r')
        op = self.upsert(None, 'message_0.db', 'Msg_a', 1, 'no table')
        del op['identity']['table']
        with self.assertRaises(ArchiveError) as cm:
            self.commit('batch-c', self.run_payload('r', c0, [op]))
        self.assertEqual(_code(cm.exception), 'run_invalid')

    def test_raw_columns_stored_separately_from_text(self):
        raw = {'packed_info_data': 'base64==', 'bytesCnt': 128,
               'sort_seq': 55, 'extra_source_column': 'kept verbatim'}
        c0 = self.begin('r')
        self.commit('b', self.run_payload('r', c0, [
            self.upsert('s9', 'message_0.db', 'Msg_a', 3, 'text view',
                        raw=raw)]))
        row = self.messages()[0]
        self.assertEqual(row['content_text'], 'text view')
        self.assertEqual(json.loads(row['raw_record_json']), raw)
        after = json.loads(self.store.db.execute(
            "SELECT after_json FROM mutation_log WHERE table_name='messages' "
            "AND op='insert'").fetchone()[0])
        self.assertEqual(json.loads(after['raw_record_json']), raw)
        self.assertEqual(after['content_text'], 'text view')


if __name__ == '__main__':
    unittest.main(verbosity=2)
