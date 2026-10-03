import copy
from datetime import datetime
import hashlib
import importlib.util
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

MODULE = Path(__file__).resolve().parents[1] / 'store.py'
spec = importlib.util.spec_from_file_location('wx_archive_store', MODULE)
a = importlib.util.module_from_spec(spec)
spec.loader.exec_module(a)

ACCOUNT = 'synthetic-account'
TALKER = 'synthetic@chatroom'


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.root = self.base / 'private'
        self.store = a.Archive(self.root)
        self.connections = [self.store.db]

    def tearDown(self):
        for connection in self.connections:
            connection.close()
        self.tmp.cleanup()

    def open(self, **kwargs):
        archive = a.Archive(self.root, **kwargs)
        self.connections.append(archive.db)
        return archive

    def message(self, sid=1, text='收到', ts=1000, seq=1, talker=TALKER, **extra):
        item = dict(server_id=sid, talker=talker, create_time=ts, sort_seq=seq,
                    sender='synthetic-sender', sender_display_name='Synthetic Sender',
                    content={'Text': text}, snippet=text, msg_type=1, sub_type=0,
                    status=0, direction='incoming')
        item.update(extra)
        return item

    def media_message(self, sid=1, content=None, state='missing',
                      reason='missing_local_media', **extra):
        status = {'state': state}
        if reason is not None:
            status['reason'] = reason
        return self.message(sid, content={'Image': {'md5': 'shared-reference'}}
                            if content is None else content, media_status=status, **extra)

    def fixture(self, items, talker=TALKER, media_mode='enabled'):
        counts = dict(expected=0, available=0, missing=0, errors=0, not_attempted=0)
        for item in items:
            content = item.get('content')
            if content == 'Voice' or (isinstance(content, dict)
                                     and {'Image', 'Video', 'File'} & content.keys()):
                counts['expected'] += 1
                state = item.get('media_status', {}).get('state')
                key = {'error': 'errors', 'metadata_only': 'not_attempted'}.get(state, state)
                if key in counts:
                    counts[key] += 1
        return dict(items=items, media=dict(version=1, mode=media_mode, **counts),
                    conversation=dict(talker=talker, display_name='Synthetic Group',
                                      type='group', message_count=len(items)),
                    stats=dict(skipped=0),
                    paging=dict(has_more=False, offset=0, returned=len(items)))

    def load(self, items, account=ACCOUNT, talker=TALKER, archive=None, **kwargs):
        return self.load_data(self.fixture(items, talker), account, archive, **kwargs)

    def load_data(self, data, account=ACCOUNT, archive=None, **kwargs):
        path = self.base / 'export.json'
        path.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')
        return (archive or self.store).import_export(path, account, **kwargs)

    def test_same_second_server_and_local_context_follows_source_row_order(self):
        self.load([
            self.message(900, text='收到更新', local_id=1),
            self.message(0, text='收到更新', local_id=2, source_shard='message_0.db'),
            self.message(7, text='收到更新', local_id=3),
        ])
        expected = ['900', 'message_0.db/2', '7']
        context = self.store.context(ACCOUNT, TALKER, message_id='message_0.db/2',
                                     id_kind='local', radius=1)
        self.assertEqual([row['message_id'] for row in context], expected)
        server_context = self.store.context(ACCOUNT, TALKER, server_id=900, radius=1)
        self.assertEqual([row['message_id'] for row in server_context], expected[:2])
        for query in ('收到更新', '收'):
            found = self.store.search(query, ACCOUNT)
            self.assertEqual([row['message_id'] for row in found], list(reversed(expected)))

    def test_same_second_local_context_uses_numeric_row_order_then_shard(self):
        self.load([
            self.message(0, local_id=10, source_shard='message_1.db'),
            self.message(0, local_id=9, source_shard='message_0.db'),
            self.message(0, local_id=10, source_shard='message_0.db'),
        ])
        expected = [('message_0.db', 9), ('message_0.db', 10), ('message_1.db', 10)]
        context = self.store.context(ACCOUNT, TALKER, message_id='message_0.db/10',
                                     id_kind='local', radius=1)
        self.assertEqual([(row['message']['source_shard'], row['local_id']) for row in context], expected)
        found = self.store.search('收到', ACCOUNT)
        self.assertEqual([(row['message']['source_shard'], row['local_id']) for row in found],
                         list(reversed(expected)))

    def records(self):
        return self.store.search('收到', ACCOUNT, limit=100)

    def test_same_second_five_messages_and_overlap_dedupe(self):
        items = [self.message(sid, seq=sid) for sid in range(1, 6)]
        first = self.load(items, bounds=(900, 1100))
        second = self.load(items, bounds=(1000, 1200))
        self.assertEqual((first['added'], second['added'], second['changed']), (5, 0, 0))
        self.assertEqual([r['server_id'] for r in self.records()], [5, 4, 3, 2, 1])
        self.assertEqual([r['server_id'] for r in self.store.context(ACCOUNT, TALKER, 3)],
                         [1, 2, 3, 4, 5])
        self.assertEqual(self.store.status()['revisions'], 5)
        self.assertEqual(self.store.checkpoint(ACCOUNT, TALKER), 1200)

    def test_server_metadata_refresh_is_not_a_content_revision(self):
        self.load([self.message(1, local_id=11, sender_display_name='Old Name')],
                  bounds=(900, 1100))
        updated = self.message(1, local_id=22, sender_display_name='New Name',
                               snippet='格式更新', direction='outgoing')
        result = self.load([updated], bounds=(1000, 1200))
        self.assertEqual(result['changed'], 0)
        record = self.store.context(ACCOUNT, TALKER, 1, radius=0)[0]
        self.assertFalse(record['has_revisions'])
        self.assertEqual(record['original_text'], '收到')
        self.assertEqual(record['local_id'], 22)
        self.assertEqual(record['message']['local_id'], 22)
        self.assertEqual(record['sender_display_name'], 'New Name')
        self.assertEqual(self.store.search('格式更新', ACCOUNT)[0]['server_id'], 1)

    def test_account_talker_and_server_local_namespaces(self):
        self.load([self.message(7)])
        self.load([self.message(7)], account='other-account')
        self.load([self.message(7, talker='other@chatroom')], talker='other@chatroom')
        self.load([self.message(0, local_id=7)])
        self.assertEqual(self.store.status()['messages'], 4)
        rows = self.records()
        self.assertEqual({r['id_kind'] for r in rows}, {'server', 'local'})
        self.assertEqual(len(rows), 3)
        self.assertEqual(self.store.context(ACCOUNT, TALKER, 7, radius=0)[0]['id_kind'], 'server')
        local = self.store.context(ACCOUNT, TALKER, message_id='7', id_kind='local', radius=0)[0]
        self.assertEqual((local['local_id'], local['id_kind']), (7, 'local'))

    def test_local_cross_shard_identities_and_same_sequence_context(self):
        items = [self.message(0, local_id=8, source_shard=shard, seq=1)
                 for shard in ('message_a.db', 'message_b.db')]
        items.append(self.message(0, local_id=8, seq=1))
        self.load(items)
        rows = self.records()
        identities = {(r['id_kind'], r['message_id']) for r in rows}
        self.assertEqual(len(identities), 3)
        anchor = next(r for r in rows if r['message'].get('source_shard') == 'message_a.db')
        context = self.store.context(ACCOUNT, TALKER, message_id=anchor['message_id'],
                                     id_kind='local', radius=10)
        self.assertEqual({r['message_id'] for r in context}, {r['message_id'] for r in rows})

    def test_invalid_exports_leave_messages_revisions_and_checkpoint_unchanged(self):
        self.load([self.message(1)], bounds=(900, 1100))
        original = self.store.status()
        mutations = [
            lambda d: d['stats'].update(skipped=1),
            lambda d: d['stats'].update(shard_warnings=[{'reason': 'broken shard'}]),
            lambda d: d['stats'].pop('skipped'),
            lambda d: d['paging'].update(has_more=True),
            lambda d: d['paging'].pop('has_more'),
            lambda d: d['paging'].update(offset=1),
            lambda d: d['paging'].update(returned=9),
            lambda d: d['conversation'].update(message_count=9),
            lambda d: d['items'][1].pop('server_id'),
            lambda d: d['items'][1].update(server_id=0),
            lambda d: d['items'][1].update(server_id=True),
            lambda d: d['items'][1].update(create_time=5000),
            lambda d: d['items'][1].update(talker='wrong@chatroom'),
            lambda d: d['items'][1].pop('content'),
            lambda d: d['items'][1].update(media_files=['media/missing.dat']),
            lambda d: d['items'][1].update(media_files=['../outside']),
            lambda d: d['items'][1].update(media_files=['/etc/passwd']),
            lambda d: d['items'][1].update(server_id=0, local_id=3,
                                         source_shard='/private/database.db'),
            lambda d: d['items'][1].update(server_id=1),
        ]
        for mutate in mutations:
            data = self.fixture([self.message(1, '改动后的文本'), self.message(2)])
            mutate(data)
            with self.subTest(mutation=mutate):
                with self.assertRaises(ValueError):
                    self.load_data(data, bounds=(900, 1200), reconcile=True)
                self.assertEqual(self.store.status(), original)
                self.assertEqual(self.store.context(ACCOUNT, TALKER, 1, 0)[0]['original_text'], '收到')

    def test_omitted_optional_fields_and_trusted_empty(self):
        result = self.load([self.message(1)], bounds=(900, 1100))
        self.assertEqual(result['status'], 'imported')
        empty = self.store.import_empty(ACCOUNT, TALKER, (1101, 1200))
        self.assertEqual((empty['status'], empty['imported'], empty['source_sha256']),
                         ('empty', 0, None))
        self.assertEqual(self.store.checkpoint(ACCOUNT, TALKER), 1200)
        json_empty = self.load([], bounds=(1201, 1300))
        self.assertEqual((json_empty['status'], json_empty['imported']), ('empty', 0))
        self.assertIsNotNone(json_empty['source_sha256'])

    def test_backfill_does_not_regress_and_advance_false_does_not_create_checkpoint(self):
        self.assertIsNone(self.store.checkpoint(ACCOUNT, TALKER))
        self.load([self.message(1)], bounds=(900, 1100), advance_checkpoint=False)
        self.assertIsNone(self.store.checkpoint(ACCOUNT, TALKER))
        self.store.import_empty(ACCOUNT, TALKER, (1100, 1200))
        self.load([self.message(2, ts=800)], bounds=(700, 900))
        self.assertEqual(self.store.checkpoint(ACCOUNT, TALKER), 1200)

    def test_content_revision_keeps_initial_payload_and_refreshes_fts(self):
        before = self.message(1, '修改之前的原话')
        after = self.message(1, '修改之后的原话')
        self.load([before])
        result = self.load([after])
        self.assertEqual((result['added'], result['changed']), (0, 1))
        self.assertEqual(self.store.search('修改之前的原话', ACCOUNT), [])
        record = self.store.search('修改之后的原话', ACCOUNT)[0]
        self.assertTrue(record['has_revisions'])
        self.assertEqual(record['message'], after)
        payloads = [json.loads(r[0]) for r in self.store.db.execute('SELECT payload FROM revisions')]
        self.assertCountEqual(payloads, [before, after])
        self.load([before])
        self.assertTrue(self.store.search('修改之前的原话', ACCOUNT)[0]['has_revisions'])

    def test_media_preserved_deduplicated_and_not_a_content_revision(self):
        media = self.base / 'media'
        media.mkdir()
        (media / 'a.dat').write_bytes(b'synthetic attachment')
        (media / 'b.dat').write_bytes(b'synthetic attachment')
        first = self.media_message(1, state='available', reason=None,
                                   media_files=['media/a.dat'])
        self.load([first])
        first_record = self.records()[0]
        asset = Path(first_record['media_files'][0])
        original_stat = asset.stat()
        second = self.media_message(1, state='available', reason=None,
                                    media_files=['media/b.dat'])
        result = self.load([second])
        self.assertEqual(result['changed'], 0)
        self.assertEqual(self.store.status()['revisions'], 1)
        self.assertEqual(self.records()[0]['media_files'], first_record['media_files'])
        self.assertEqual(asset.stat().st_mtime_ns, original_stat.st_mtime_ns)
        (media / 'a.dat').unlink()
        (media / 'b.dat').unlink()
        (self.base / 'export.json').unlink()
        record = self.records()[0]
        self.assertEqual(record['message'], second)
        self.assertEqual(asset.read_bytes(), b'synthetic attachment')
        self.assertEqual(json.loads(Path(record['source']).read_text())['items'], [second])
        self.assertEqual(json.loads(Path(first_record['source']).read_text())['items'], [first])
        for parent in [self.root, self.root / 'sources', self.root / 'sources' / 'assets', asset.parent]:
            self.assertEqual(parent.stat().st_mode & 0o777, 0o700)
        self.assertEqual(asset.stat().st_mode & 0o777, 0o600)
        self.assertEqual(Path(record['source']).stat().st_mode & 0o777, 0o600)

    def test_symlink_media_and_parent_symlink_fail_before_mutation(self):
        outside = self.base / 'outside.dat'
        outside.write_bytes(b'outside')
        media = self.base / 'media'
        media.mkdir()
        (media / 'link.dat').symlink_to(outside)
        (self.base / 'linked-dir').symlink_to(media, target_is_directory=True)
        for rel in ('media/link.dat', 'linked-dir/link.dat'):
            with self.assertRaises(ValueError):
                self.load([self.media_message(1, state='available', reason=None,
                                              media_files=[rel])], bounds=(900, 1100))
        self.assertEqual(self.store.status()['messages'], 0)
        self.assertIsNone(self.store.checkpoint(ACCOUNT, TALKER))

    def test_proven_missing_media_preserves_text_and_counts_each_message(self):
        items = [self.media_message(1), self.media_message(2),
                 self.media_message(3, content={'Video': {'md5': None}},
                                    reason='missing_reference'),
                 self.media_message(4, content={'File': {'name': 'gone.pdf'}},
                                    reason='missing_reference'),
                 self.media_message(5, content='Voice', reason='missing_reference')]
        result = self.load(items, bounds=(900, 1100), require_media_contract=True)
        self.assertEqual((result['added'], result['missing_media'],
                          result['source_missing_media']), (5, 5, 5))
        self.assertEqual(self.store.checkpoint(ACCOUNT, TALKER), 1100)
        self.assertEqual(self.store.status()['media_status'],
                         {'available': 0, 'missing': 5, 'metadata_only': 0})
        records = {r['server_id']: r for r in self.records()}
        for item in items:
            record = records[item['server_id']]
            self.assertEqual(record['media_status'], item['media_status'])
            self.assertEqual(record['media_files'], [])
            self.assertEqual(record['message']['content'], item['content'])

    def test_cached_asset_survives_source_loss_and_status_refresh_without_revision(self):
        (self.base / 'asset.dat').write_bytes(b'preserved')
        available = self.media_message(state='available', reason=None,
                                       media_files=['asset.dat'])
        self.load([available], bounds=(900, 1100), require_media_contract=True)
        before = self.records()[0]
        (self.base / 'asset.dat').unlink()
        missing = self.media_message()
        result = self.load([missing], bounds=(900, 1200), require_media_contract=True)
        after = self.records()[0]
        self.assertEqual((result['changed'], result['missing_media'],
                          result['source_missing_media']), (0, 0, 1))
        self.assertFalse(after['has_revisions'])
        self.assertEqual(after['media_status'], missing['media_status'])
        self.assertEqual(after['media_files'], before['media_files'])
        self.assertEqual(Path(after['media_files'][0]).read_bytes(), b'preserved')
        self.assertNotEqual(after['source'], before['source'])
        self.assertEqual(self.store.status()['missing_media'], 0)
        self.assertEqual(self.store.status()['source_missing_media'], 1)
        missing['media_status']['reason'] = 'missing_reference'
        refreshed = self.load([missing], require_media_contract=True)
        self.assertEqual(refreshed['changed'], 0)
        self.assertEqual(self.records()[0]['media_status']['reason'], 'missing_reference')
        (self.base / 'asset-again.dat').write_bytes(b'preserved')
        available['media_files'] = ['asset-again.dat']
        self.assertEqual(self.load([available], require_media_contract=True)['changed'], 0)
        self.assertEqual(self.store.status()['revisions'], 1)
        self.assertEqual(self.store.status()['source_missing_media'], 0)

    def test_repeated_available_reference_counts_messages_not_assets(self):
        (self.base / 'asset.dat').write_bytes(b'shared')
        items = [self.media_message(sid, state='available', reason=None,
                                    media_files=['asset.dat']) for sid in (1, 2)]
        result = self.load(items, require_media_contract=True)
        self.assertEqual(result['media_status']['available'], 2)
        self.assertEqual(result['media_files'], 1)
        self.assertEqual(self.records()[0]['media_files'], self.records()[1]['media_files'])

    def test_metadata_only_contract_never_claims_available_or_missing_media(self):
        items = [self.media_message(state='metadata_only', reason=None)]
        result = self.load_data(self.fixture(items, media_mode='metadata_only'),
                                bounds=(900, 1100), require_media_contract=True)
        self.assertEqual(result['media_status'],
                         {'available': 0, 'missing': 0, 'metadata_only': 1})
        self.assertEqual((result['missing_media'], result['media_files']), (0, 0))
        self.assertEqual(self.records()[0]['media_status'], {'state': 'metadata_only'})
        self.assertEqual(self.store.checkpoint(ACCOUNT, TALKER), 1100)

    def test_manual_legacy_import_allowed_but_live_marker_required(self):
        data = self.fixture([self.message(1)])
        data.pop('media')
        self.load_data(data, bounds=(900, 1100))
        before = self.store.status()
        data['items'][0]['content'] = {'Text': 'must not replace'}
        with self.assertRaises(ValueError):
            self.load_data(data, bounds=(900, 1200), require_media_contract=True)
        self.assertEqual(self.store.status(), before)
        data['items'][0]['media_status'] = {'state': 'missing',
                                          'reason': 'missing_local_media'}
        with self.assertRaises(ValueError):
            self.load_data(data)
        self.assertEqual(self.store.status(), before)

    def test_live_local_identity_without_shard_stops_before_checkpoint(self):
        data = self.fixture([self.message(0, local_id=7)])
        with self.assertRaises(ValueError):
            self.load_data(data, bounds=(900, 1100), require_media_contract=True)
        self.assertEqual(self.store.status()['messages'], 0)
        self.assertIsNone(self.store.checkpoint(ACCOUNT, TALKER))


    def test_media_errors_and_malformed_contracts_are_atomic(self):
        self.load([self.message(1)], bounds=(900, 1100))
        before = self.store.status()
        (self.base / 'asset.dat').write_bytes(b'asset')
        mutations = [
            lambda d: d.update(media=None),
            lambda d: d['media'].update(version=2),
            lambda d: d['media'].update(version=True),
            lambda d: d['media'].pop('errors'),
            lambda d: d['media'].update(unrecognized=0),
            lambda d: d['media'].update(mode='unknown'),
            lambda d: d['media'].update(expected=True),
            lambda d: d['media'].update(missing=-1),
            lambda d: d['media'].update(available=1.0),
            lambda d: d['media'].update(expected=0),
            lambda d: d['media'].update(missing=0),
            lambda d: d['media'].update(errors=1),
            lambda d: d['media'].update(mode='metadata_only'),
            lambda d: d['items'][1].pop('media_status'),
            lambda d: d['items'][1].update(media_status=None),
            lambda d: d['items'][1]['media_status'].update(state='unknown'),
            lambda d: d['items'][1]['media_status'].update(state='error',
                                                        reason='source_read_failed'),
            lambda d: d['items'][1]['media_status'].update(state='available'),
            lambda d: d['items'][1]['media_status'].update(state='metadata_only'),
            lambda d: d['items'][1]['media_status'].update(reason='permission denied'),
            lambda d: d['items'][1]['media_status'].update(reason='source_read_failed'),
            lambda d: d['items'][1]['media_status'].update(extra=True),
            lambda d: d['items'][1].update(media_files=['asset.dat']),
            lambda d: d['items'][1].update(content={'Text': 'not eligible'}),
            lambda d: d['items'][0].update(media_status={'state': 'available'}),
        ]
        for mutate in mutations:
            data = self.fixture([self.message(1, 'must not replace'),
                                 self.media_message(2)])
            mutate(data)
            with self.subTest(mutation=mutate):
                with self.assertRaises(ValueError):
                    self.load_data(data, bounds=(900, 1200), reconcile=True,
                                   require_media_contract=True)
                self.assertEqual(self.store.status(), before)
                self.assertEqual(self.records()[0]['original_text'], '收到')

    def test_eligible_message_cannot_be_omitted_even_with_zero_expected(self):
        for content in ({'Image': {}}, {'Video': {'md5': None}},
                        {'File': {'name': 'no-reference'}}, 'Voice'):
            with self.subTest(content=content):
                data = self.fixture([self.message(1, content=content)])
                data['media']['expected'] = 0
                with self.assertRaises(ValueError):
                    self.load_data(data, bounds=(900, 1100), require_media_contract=True)
                self.assertEqual(self.store.status()['messages'], 0)
                self.assertIsNone(self.store.checkpoint(ACCOUNT, TALKER))

    def test_available_status_requires_an_actual_safe_exported_file(self):
        self.load([self.message(1)], bounds=(900, 1100))
        before = self.store.status()
        for rel in ('not-exported.dat', '../outside.dat', '/etc/passwd'):
            item = self.media_message(2, state='available', reason=None, media_files=[rel])
            with self.subTest(relative=rel):
                with self.assertRaises(ValueError):
                    self.load([self.message(1, 'must not replace'), item],
                              bounds=(900, 1200), require_media_contract=True)
                self.assertEqual(self.store.status(), before)

    def test_missing_status_reason_is_optional_but_duplicate_eligibility_is_not(self):
        items = [self.media_message(1, reason=None), self.media_message(2, reason=None)]
        result = self.load(items, bounds=(900, 1100), require_media_contract=True)
        self.assertEqual(result['missing_media'], 2)
        self.assertEqual(self.records()[0]['media_status'], {'state': 'missing'})
        before = self.store.status()
        data = self.fixture(items)
        data['items'][1].pop('media_status')
        with self.assertRaises(ValueError):
            self.load_data(data, bounds=(900, 1200), require_media_contract=True)
        self.assertEqual(self.store.status(), before)

    def test_literal_search_original_content_bounds_and_stable_order(self):
        items = [self.message(1, '样本100%', ts=100, seq=1),
                 self.message(2, '样本1000', ts=200, seq=1),
                 self.message(3, '样本_under', ts=200, seq=1),
                 self.message(4, '今天天气不错适合出门', ts=300, snippet='截断'),
                 self.message(5, '他说"明天出门"', ts=400)]
        self.load(items)
        self.assertEqual([r['server_id'] for r in self.store.search('%', ACCOUNT)], [1])
        self.assertEqual([r['server_id'] for r in self.store.search('_', ACCOUNT)], [3])
        self.assertEqual([r['server_id'] for r in self.store.search('天气', ACCOUNT)], [4])
        self.assertEqual([r['server_id'] for r in self.store.search('天气不错', ACCOUNT)], [4])
        self.assertEqual([r['server_id'] for r in self.store.search('截断', ACCOUNT)], [4])
        self.assertEqual([r['server_id'] for r in self.store.search('"明天出门"', ACCOUNT)], [5])
        self.assertEqual(self.store.search('%10', ACCOUNT), [])
        self.assertEqual([r['server_id'] for r in self.store.search('样本', ACCOUNT, since=100, until=200)],
                         [3, 2, 1])
        self.assertEqual([r['server_id'] for r in self.store.search('样本', ACCOUNT, since=101, limit=1)], [3])
        self.assertEqual(self.store.search('样本', ACCOUNT, until=99), [])

    def test_reconcile_missing_reappearance_and_no_partial_false_missing(self):
        items = [self.message(1, ts=900), self.message(2, ts=1000),
                 self.message(3, ts=1100), self.message(4, ts=2000)]
        self.load(items, bounds=(900, 2000))
        result = self.load([items[1]], bounds=(900, 1100), reconcile=True,
                           advance_checkpoint=False)
        self.assertEqual(result['missing'], 2)
        rows = {r['server_id']: r for r in self.records()}
        self.assertEqual({sid for sid, r in rows.items() if r['missing_in_source']}, {1, 3})
        self.assertEqual(self.store.checkpoint(ACCOUNT, TALKER), 2000)
        self.assertEqual(self.store.status()['messages'], 4)
        self.load(items[:3], bounds=(900, 1100), reconcile=True, advance_checkpoint=False)
        self.assertFalse(any(r['missing_in_source'] for r in self.records()))
        self.load([items[1]], bounds=(900, 1100))
        self.assertFalse(any(r['missing_in_source'] for r in self.records()))
        result = self.load([items[2]], bounds=(1001, 1100), reconcile=True,
                           advance_checkpoint=False)
        self.assertEqual(result['missing'], 0)
        empty = self.store.import_empty(ACCOUNT, TALKER, (900, 1000),
                                        reconcile=True, advance_checkpoint=False)
        self.assertEqual(empty['missing'], 2)
        rows = {r['server_id']: r for r in self.records()}
        self.assertFalse(rows[3]['missing_in_source'])
        self.assertFalse(rows[4]['missing_in_source'])
        with self.assertRaises(ValueError):
            self.load([items[1]], reconcile=True)

    def test_outer_savepoint_can_rollback_complete_reconciliation(self):
        self.load([self.message(1), self.message(2)], bounds=(900, 1100))
        before = self.store.status()
        self.store.db.execute('SAVEPOINT consumer_reconcile')
        try:
            self.load([self.message(1, '新的消息内容')], bounds=(900, 1100),
                      reconcile=True, advance_checkpoint=False)
            self.store.import_empty(ACCOUNT, TALKER, (1200, 1300))
            self.assertTrue(self.store.db.in_transaction)
            bad = self.fixture([self.message(3, ts=1200)])
            bad['paging']['has_more'] = True
            with self.assertRaises(ValueError):
                self.load_data(bad, bounds=(1200, 1300), reconcile=True)
        finally:
            self.store.db.execute('ROLLBACK TO consumer_reconcile')
            self.store.db.execute('RELEASE consumer_reconcile')
        self.assertEqual(self.store.status(), before)
        self.assertEqual(self.store.search('新的消息内容', ACCOUNT), [])
        self.assertFalse(any(r['missing_in_source'] for r in self.records()))

    def test_late_sqlite_failure_rolls_back_entire_import(self):
        self.load([self.message(1)], bounds=(900, 1100))
        before = self.store.status()
        self.store.db.execute("CREATE TRIGGER reject_synthetic BEFORE INSERT ON messages "
                              "WHEN new.server_id=3 BEGIN SELECT RAISE(ABORT,'synthetic failure'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.load([self.message(1, 'Changed'), self.message(2), self.message(3)],
                      bounds=(900, 1200), reconcile=True)
        self.assertEqual(self.store.status(), before)
        self.assertEqual(self.store.search('Changed', ACCOUNT), [])
        self.assertEqual(self.store.context(ACCOUNT, TALKER, 1, 0)[0]['original_text'], '收到')

    def test_policy_applies_to_every_query_and_write(self):
        self.load([self.message(1)], bounds=(900, 1100))
        other = 'other@chatroom'
        self.load([self.message(2, talker=other)], talker=other, bounds=(900, 1100))
        self.load([self.message(3)], account='other-account', bounds=(900, 1100))
        policy = self.open(allowed_account=ACCOUNT, allowed_talkers=[TALKER, 'unsynced@chatroom'])
        calls = [lambda: policy.search('收到', 'other-account'),
                 lambda: policy.search('收到', ACCOUNT, talker=other),
                 lambda: policy.context(ACCOUNT, other, 2),
                 lambda: policy.checkpoint(ACCOUNT, other),
                 lambda: policy.list_conversations('other-account'),
                 lambda: policy.import_empty(ACCOUNT, other, (900, 1100)),
                 lambda: self.load([self.message(1)], account='other-account', archive=policy),
                 lambda: self.load([self.message(2, talker=other)], talker=other, archive=policy)]
        for call in calls:
            with self.assertRaises(ValueError):
                call()
        self.assertEqual([r['server_id'] for r in policy.search('收到', ACCOUNT)], [1])
        self.assertEqual(policy.status()['messages'], 1)
        self.assertEqual(len(policy.status()['checkpoints']), 1)
        conversations = {r['talker']: r for r in policy.list_conversations()}
        self.assertEqual(set(conversations), {TALKER, 'unsynced@chatroom'})
        self.assertEqual(conversations[TALKER]['checkpoint'], 1100)
        self.assertEqual(conversations['unsynced@chatroom']['message_count'], 0)
        self.assertIsNone(conversations['unsynced@chatroom']['checkpoint'])
        self.assertEqual(conversations['unsynced@chatroom']['conversation_name'], 'unsynced@chatroom')
        empty_policy = self.open(allowed_account=ACCOUNT, allowed_talkers=[])
        self.assertEqual(empty_policy.search('收到', ACCOUNT), [])
        self.assertEqual(empty_policy.status()['messages'], 0)
        self.assertEqual(empty_policy.list_conversations(), [])

    def test_readonly_never_creates_and_refuses_all_writes(self):
        missing = self.base / 'nonexistent'
        with self.assertRaises(FileNotFoundError):
            a.Archive(missing, readonly=True)
        self.assertFalse(missing.exists())
        self.load([self.message(1)], bounds=(900, 1100))
        readonly = self.open(readonly=True)
        before = readonly.status()
        self.assertEqual(readonly.search('收到', ACCOUNT)[0]['server_id'], 1)
        self.assertEqual(readonly.context(ACCOUNT, TALKER, 1, 0)[0]['server_id'], 1)
        with self.assertRaises(ValueError):
            self.load([self.message(2)], archive=readonly)
        with self.assertRaises(ValueError):
            readonly.import_empty(ACCOUNT, TALKER, (1100, 1200))
        with self.assertRaises(sqlite3.OperationalError):
            readonly.db.execute("INSERT INTO meta VALUES('write-attempt','blocked')")
        self.assertEqual(readonly.status(), before)

    def test_citations_are_faithful_and_status_unverified(self):
        item = self.message(1, '引用完整原话', local_id=12)
        self.load([item])
        record = self.store.search('引用完整原话', ACCOUNT)[0]
        self.assertEqual(record['message'], item)
        self.assertEqual(record['original_text'], '引用完整原话')
        self.assertEqual(record['conversation_name'], 'Synthetic Group')
        self.assertEqual(record['sender_display_name'], 'Synthetic Sender')
        self.assertEqual((record['server_id'], record['local_id'], record['id_kind']), (1, 12, 'server'))
        timestamp = datetime.fromisoformat(record['time'])
        self.assertIsNotNone(timestamp.utcoffset())
        self.assertEqual(int(timestamp.timestamp()), item['create_time'])
        self.assertFalse(record['has_revisions'])
        self.assertFalse(record['missing_in_source'])
        self.assertTrue(Path(record['source']).is_file())
        status = self.store.status()
        self.assertEqual((status['messages'], status['revisions'], status['missing']), (1, 1, 0))
        self.assertEqual(status['schema_version'], a.SCHEMA_VERSION)
        self.assertEqual(status['real_wechat_validation'], 'UNVERIFIED')


class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.old = sqlite3.connect(self.root / 'archive.sqlite3')
        self.old.executescript('''
        CREATE TABLE messages (
          account TEXT, talker TEXT, server_id INTEGER, create_time INTEGER,
          sort_seq INTEGER, sender TEXT, snippet TEXT, payload TEXT, source TEXT,
          PRIMARY KEY(account,talker,server_id));
        CREATE TABLE revisions (
          account TEXT,talker TEXT,server_id INTEGER,payload_hash TEXT,payload TEXT,source TEXT,
          PRIMARY KEY(account,talker,server_id,payload_hash));
        CREATE TABLE imports (
          account TEXT,source TEXT,imported_at INTEGER,count INTEGER,PRIMARY KEY(account,source));
        CREATE TABLE checkpoints (
          account TEXT,talker TEXT,until_ts INTEGER,source TEXT,PRIMARY KEY(account,talker));
        CREATE INDEX message_time ON messages(account,talker,create_time,sort_seq,server_id);
        ''')
        self.before = dict(server_id=1, talker=TALKER, create_time=1000, sort_seq=1,
                           sender='sender', sender_display_name='Sender',
                           snippet='旧版本', content={'Text': '旧版本完整消息'},
                           media_files=['media/attachment.dat'])
        self.after = copy.deepcopy(self.before)
        self.after.update(snippet='新版本', content={'Text': '新版本完整消息'})
        raw = a.canonical({'items': [self.after]}).encode()
        self.source = hashlib.sha256(raw).hexdigest()
        directory = self.root / 'sources' / self.source
        (directory / 'media').mkdir(parents=True)
        (directory / 'export.json').write_bytes(raw)
        (directory / 'media' / 'attachment.dat').write_bytes(b'legacy attachment')
        self.old.execute('INSERT INTO messages VALUES(?,?,?,?,?,?,?,?,?)',
                         (ACCOUNT, TALKER, 1, 1000, 1, 'sender', '新版本',
                          a.canonical(self.after), self.source))
        for item in [self.before, self.after]:
            payload = a.canonical(item)
            self.old.execute('INSERT INTO revisions VALUES(?,?,?,?,?,?)',
                             (ACCOUNT, TALKER, 1, hashlib.sha256(payload.encode()).hexdigest(),
                              payload, self.source))
        self.old.execute('INSERT INTO imports VALUES(?,?,?,?)', (ACCOUNT, self.source, 1234, 1))
        self.old.execute('INSERT INTO checkpoints VALUES(?,?,?,?)', (ACCOUNT, TALKER, 1100, self.source))
        self.old.commit()

    def tearDown(self):
        self.old.close()
        self.tmp.cleanup()

    def test_migration_preserves_payloads_revisions_checkpoints_sources_and_media(self):
        archive = a.Archive(self.root)
        try:
            record = archive.search('新版本完整消息', ACCOUNT)[0]
            self.assertEqual(record['message'], self.after)
            self.assertTrue(record['has_revisions'])
            self.assertEqual(record['id_kind'], 'server')
            self.assertEqual(record['conversation_name'], TALKER)
            self.assertEqual(archive.checkpoint(ACCOUNT, TALKER), 1100)
            self.assertEqual(archive.status()['revisions'], 2)
            self.assertEqual(Path(record['media_files'][0]).read_bytes(), b'legacy attachment')
            self.assertEqual(archive.db.execute('SELECT imported_at,count FROM imports').fetchone()[:],
                             (1234, 1))
            self.assertEqual(archive.status()['schema_version'], a.SCHEMA_VERSION)
        finally:
            archive.db.close()
        reopened = a.Archive(self.root, readonly=True)
        try:
            self.assertEqual(reopened.search('新版本完整消息', ACCOUNT)[0]['message'], self.after)
        finally:
            reopened.db.close()

    def test_migration_failure_rolls_back_schema_and_data(self):
        self.old.execute('DROP TABLE imports')
        self.old.execute('CREATE TABLE imports (account TEXT, source TEXT)')
        self.old.execute('INSERT INTO imports VALUES(?,?)', (ACCOUNT, self.source))
        self.old.commit()
        with self.assertRaises((IndexError, sqlite3.Error)):
            a.Archive(self.root)
        self.assertEqual(self.old.execute('SELECT payload FROM messages').fetchone()[0],
                         a.canonical(self.after))
        self.assertEqual(self.old.execute('SELECT count(*) FROM revisions').fetchone()[0], 2)
        self.assertEqual(self.old.execute('SELECT until_ts FROM checkpoints').fetchone()[0], 1100)
        columns = {row[1] for row in self.old.execute('PRAGMA table_info(messages)')}
        self.assertNotIn('id_kind', columns)


if __name__ == '__main__':
    unittest.main()
