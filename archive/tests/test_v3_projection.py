"""S4c projection gates — synthetic data only, no real NAS/device/keys.

Covers the five mandatory requirements for the query projection slice:
1. hidden sentinels (raw_record_json, revision payloads, quotes=none quote
   text, hidden-sender rows, revoked-talker rows, raw-XML bodies, quotes
   whose OWN sender is outside the allowlist, media source paths) are
   un-findable in FTS, absent from every projection column, and absent
   from the db FILE BYTES;
2. sender allowlist / tag / nested-quote visibility filtering with coverage
   gaps reported (key names only — unknown-key VALUES must also never leak);
3. revocation blocks already-open readers on their NEXT request — same
   process AND cross-process — with the revocations-first ordering proven
   (manifest bytes unchanged by revoke() alone);
4. policy missing / epoch stale / manifest missing or corrupt / revocations
   missing / master-root-passed-to-query all fail closed;
5. build failures never publish half-built output; mode layout matches the
   contract; query-side Python-level opens never touch the master subtree
   (audit hook); on Linux a real different OS user (nobody) can query the
   projection but cannot read master.db (permission gate, runs only in the
   authorized colima VM; skips when sudo is unavailable).

Endpoint factory shape is asserted by calling the service exactly the way
endpoint.py's op_query_* handlers do.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from archive.v3.errors import GuardError, ProjectionError
from archive.v3.guard import SyntheticGuard
from archive.v3.master import MasterStore
from archive.v3.projection import (ProjectionBuilder, ProjectionService,
                                   opaque_media_id)

REPO_ROOT = Path(__file__).resolve().parents[2]

ARCHIVE_ID = 'arch-proj'
PROJECTION_ID = f'{ARCHIVE_ID}:projection'
ACCOUNT = 'acct-main'

RAW_SENT = 'SENTINEL-RAW-9a3f7b'
REV_SENT = 'SENTINEL-REV-8b2e5c'
QUOTE_HIDDEN_SENT = 'SENTINEL-QUOTE-7c1d4e'
QUOTE_VISIBLE_KEEP = 'QUOTE-KEEP-visible-33aa'
HIDDEN_SENDER_SENT = 'SENTINEL-SENDER-6f0a9b'
REVOKED_SENT = 'SENTINEL-REVOKED-51c2d8'
UNKNOWN_KEY_VALUE_SENT = 'SENTINEL-UNKNOWN-KEY-3d7e1f'
NESTED_QUOTE_SENT = 'SENTINEL-NESTED-99b3c2'
VISIBLE_KEEP = 'VISIBLE-KEEP-alpha-42'

_IS_LINUX = sys.platform.startswith('linux')
VM_BASE = Path('/tmp/wx-v3-projection')

_CHILD_READER = r'''
import os, sys, time
from archive.v3.errors import ProjectionError
from archive.v3.projection import ProjectionService

proj_root, ready_path, go_path = sys.argv[1], sys.argv[2], sys.argv[3]

def open_svc():
    return ProjectionService.open(proj_root, projection_id='arch-proj:projection',
                                  account='acct-main', expected_epoch=1,
                                  allow_synthetic=True)

svc = open_svc()
session = svc.open_session()
res = session.search('talk-ok', 'VISIBLE-KEEP-alpha')
assert res['count'] >= 1, res
print('PHASE1-OK', flush=True)
with open(ready_path, 'w') as fh:
    fh.write('1')
deadline = time.time() + 60
while not os.path.exists(go_path):
    if time.time() > deadline:
        print('PHASE2-TIMEOUT', flush=True)
        sys.exit(3)
    time.sleep(0.05)
try:
    session.search('talk-ok', 'VISIBLE-KEEP-alpha')
    print('PHASE2-FAIL-STILL-SEARCHABLE', flush=True)
    sys.exit(4)
except ProjectionError as exc:
    assert exc.code == 'session_stale', exc.code
    print('PHASE2-OK session_stale', flush=True)
try:
    fresh = open_svc().open_session()
    fresh.search('talk-rev', 'anything-long-enough-x')
    print('PHASE3-FAIL-REVOKED-QUERYABLE', flush=True)
    sys.exit(5)
except ProjectionError as exc:
    assert exc.code == 'talker_revoked', exc.code
    print('PHASE3-OK talker_revoked', flush=True)
print('CHILD-DONE', flush=True)
'''

_NOBODY_QUERY = r'''
import os, sys
sys.path.insert(0, sys.argv[3])  # sudo strips PYTHONPATH; pass via argv
from archive.v3.errors import ProjectionError
from archive.v3.projection import ProjectionService

proj_root, archive_root = sys.argv[1], sys.argv[2]

# master is physically unreadable for this OS user
try:
    with open(os.path.join(archive_root, 'master.db'), 'rb') as fh:
        fh.read(1)
    print('MASTER-READABLE-FAIL')
    sys.exit(10)
except PermissionError:
    print('MASTER-UNREADABLE-OK', flush=True)
try:
    os.listdir(archive_root)
    print('ROOT-LISTABLE-FAIL')
    sys.exit(11)
except PermissionError:
    print('ROOT-UNLISTABLE-OK', flush=True)

svc = ProjectionService.open(proj_root, projection_id='arch-proj:projection',
                             account='acct-main', expected_epoch=1,
                             allow_synthetic=True)
res = svc.search('talk-ok', 'VISIBLE-KEEP-alpha')
assert res['count'] >= 1, res
msgs = svc.messages('talk-ok', limit=5)
assert msgs['count'] >= 1, msgs
print('NOBODY-QUERY-OK', flush=True)
'''


def _policy():
    return {
        'policy_version': 3,
        'rules': {ACCOUNT: {
            'talk-ok': {'query': True, 'quotes': 'text_only',
                        'tags': {'mode': 'all'}},
            'talk-rev': {'query': True},
            'talk-tags': {'query': True,
                          'tags': {'mode': 'allowlist', 'allow': ['客户']}},
            'talk-qnone': {'query': True, 'quotes': 'none'},
            'talk-senders': {'query': True,
                             'senders': {'mode': 'allowlist',
                                         'allow': ['wxid_alice']}},
            'talk-qsend': {'query': True, 'quotes': 'text_only',
                           'senders': {'mode': 'allowlist',
                                       'allow': ['wxid_alice',
                                                 'wxid_quote_visible']}},
        }},
    }


class _ProjectionFixture(unittest.TestCase):
    """archive root + master store + initialised projection builder."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name) / 'archive'
        self.root.mkdir(parents=True)
        self.proj_root = self.root / 'projection'
        SyntheticGuard.write_marker(self.root, ARCHIVE_ID, 1, 0)
        self.binding = SyntheticGuard(ARCHIVE_ID).verify(self.root)
        self.addCleanup(self.binding.close)
        scope = {ACCOUNT: {t: {'capture': 1, 'query': 1} for t in
                           ('talk-ok', 'talk-rev', 'talk-tags', 'talk-qnone',
                            'talk-senders', 'talk-qsend')}}
        self.store = MasterStore.initialize(self.binding, ARCHIVE_ID, scope)
        self.addCleanup(self.store.close)
        self.store.acquire_writer_lock()
        self.addCleanup(self.store.release_writer_lock)
        self.builder = ProjectionBuilder.init(self.store, _policy())
        self._uid = 0

    def _insert(self, talker, text, *, sender='wxid_alice',
                sender_display='Alice', content_json='{}', media='[]',
                visibility='visible', deleted_at=None, has_revisions=0,
                raw='{}', create_time=None, direction='in'):
        self._uid += 1
        uid = self._uid
        with self.store.full_transaction():
            self.store.db.execute(
                'INSERT INTO messages(msg_uid,account,talker,server_id,shard,'
                'shard_table,local_rowid,create_time,sort_seq,msg_type,'
                'sub_type,status,direction,sender_id,sender_display_name,'
                'content_text,content_json,packed_info_sha256,record_sha256,'
                'raw_record_json,media_refs_json,provenance,visibility,'
                'has_revisions,first_commit_seq,'
                'last_content_or_presence_commit_seq,deleted_at) '
                'VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                (uid, ACCOUNT, talker, f'srv-{uid}', 'db0', 'Msg_x', uid,
                 create_time if create_time is not None else 1700000000 + uid,
                 uid, 1, 0, 0, direction, sender, sender_display, text,
                 content_json, 'pi-sha', 'rec-sha', raw, media, 'collector',
                 visibility, has_revisions, 1, 1, deleted_at))
        return uid

    def _insert_revision(self, msg_uid, payload):
        with self.store.full_transaction():
            self.store.db.execute(
                'INSERT INTO message_revisions(revision_id,msg_uid,seq,'
                'change_kind,observed_at,payload_json,commit_seq) '
                'VALUES(?,?,?,?,?,?,?)',
                (msg_uid, msg_uid, 1, 'edit', 1700000100,
                 json.dumps({'content_text': payload}), 1))

    def _build(self):
        return self.builder.build_and_publish()

    def _service(self, expected_epoch=1, **kwargs):
        return ProjectionService.open(
            self.proj_root, projection_id=PROJECTION_ID, account=ACCOUNT,
            expected_epoch=expected_epoch, allow_synthetic=True, **kwargs)

    def _db_path(self, manifest):
        return self.proj_root / manifest['db']

    def _proj_files(self):
        return sorted(p.name for p in self.proj_root.iterdir())


class NormalQueryFlow(_ProjectionFixture):

    def test_manifest_messages_search_sessions_context(self):
        uid1 = self._insert('talk-ok', f'first {VISIBLE_KEEP} message')
        self._insert('talk-ok', 'plain second message')
        self._insert('talk-tags', 'tagged message',
                     content_json=json.dumps({'tags': ['客户', '内部']}))
        manifest = self._build()
        for key, value in (('archive_id', ARCHIVE_ID),
                           ('projection_id', PROJECTION_ID),
                           ('epoch', 1), ('policy_version', 3),
                           ('revocations_version', 0)):
            self.assertEqual(manifest[key], value)
        self.assertEqual(manifest['message_count'], 3)
        self.assertEqual(sorted(manifest['talkers']),
                         ['talk-ok', 'talk-tags'])
        svc = self._service()
        self.addCleanup(svc.close)
        # endpoint.py op_query_* call shapes, verbatim
        self.assertEqual(svc.manifest()['generation'], manifest['generation'])
        msgs = svc.messages('talk-ok', limit=50)
        self.assertEqual(msgs['count'], 2)
        self.assertEqual(msgs['messages'][0]['seq'], uid1)
        self.assertEqual(msgs['messages'][0]['text'],
                         f'first {VISIBLE_KEEP} message')
        res = svc.search('talk-ok', VISIBLE_KEEP, limit=20)
        self.assertEqual(res['count'], 1)
        self.assertIn(f'[{VISIBLE_KEEP}]', res['results'][0]['snippet'])
        sessions = svc.sessions()['sessions']
        self.assertEqual({s['talker'] for s in sessions},
                         {'talk-ok', 'talk-tags'})
        ctx = svc.context('talk-ok', uid1, before=1, after=1)
        self.assertEqual(ctx['after'][0]['seq'], uid1 + 1)
        cov = svc.coverage()
        self.assertEqual(cov['counts']['included'], 3)
        self.assertEqual(cov['counts']['master_visible_rows'], 3)
        self.assertEqual(svc.epoch(), 1)
        self.assertEqual(svc.policy_version(), 3)

    def test_stateless_ops_work_alongside_open_session(self):
        self._insert('talk-ok', f'{VISIBLE_KEEP} body')
        self._build()
        svc = self._service()
        self.addCleanup(svc.close)
        session = svc.open_session()
        self.addCleanup(session.close)
        self.assertEqual(session.search('talk-ok', VISIBLE_KEEP)['count'], 1)
        # stateless op alongside an open session
        self.assertEqual(svc.search('talk-ok', VISIBLE_KEEP)['count'], 1)
        self.assertEqual(session.messages('talk-ok')['count'], 1)

    def test_quote_text_only_is_visible_and_searchable(self):
        cj = json.dumps({'quote': {'text': QUOTE_VISIBLE_KEEP}})
        uid = self._insert('talk-ok', 'body line', content_json=cj)
        self._build()
        svc = self._service()
        self.addCleanup(svc.close)
        msg = svc.messages('talk-ok')['messages'][0]
        self.assertEqual(msg['quote_text'], QUOTE_VISIBLE_KEEP)
        self.assertEqual(msg['seq'], uid)
        self.assertEqual(svc.search('talk-ok', QUOTE_VISIBLE_KEEP)['count'], 1)

    def test_tag_allowlist(self):
        cj = json.dumps({'tags': ['客户', '内部']})
        self._insert('talk-tags', 'tagged', content_json=cj)
        self._build()
        svc = self._service()
        self.addCleanup(svc.close)
        msg = svc.messages('talk-tags')['messages'][0]
        self.assertEqual(msg['tags'], ['客户'])

    def test_media_ids_are_opaque(self):
        media = json.dumps([
            {'ref_key': 'md5_abc', 'kind': 'image', 'sha256': 'LEAK-SHA',
             'length': 123, 'path': '/leak/path.jpg'},
            {'kind': 'image'},
            'garbage-entry'])
        self._insert('talk-ok', 'with media', media=media)
        manifest = self._build()
        svc = self._service()
        self.addCleanup(svc.close)
        msg = svc.messages('talk-ok')['messages'][0]
        # the id is the IRREVERSIBLE hash — the ref value itself never
        # appears anywhere in the projection
        self.assertEqual(msg['media'],
                         [{'id': opaque_media_id('md5_abc'), 'kind': 'image'}])
        self.assertNotIn('md5_abc', json.dumps(msg))
        raw = self._db_path(manifest).read_bytes()
        self.assertNotIn(b'LEAK-SHA', raw)
        self.assertNotIn(b'/leak/path.jpg', raw)
        self.assertNotIn(b'md5_abc', raw)
        gaps = {g['kind']: g for g in svc.coverage()['coverage_gaps']}
        self.assertEqual(gaps['media_ref_shape']['count'], 2)

    def test_limit_and_match_validation(self):
        self._insert('talk-ok', 'body')
        self._build()
        svc = self._service()
        self.addCleanup(svc.close)
        for bad in ('ab', ''):
            with self.assertRaises(ProjectionError) as ctx:
                svc.search('talk-ok', bad)
            self.assertEqual(ctx.exception.code, 'match_too_short')
        for bad in (0, 201, 'x', True):
            with self.assertRaises(ProjectionError) as ctx:
                svc.messages('talk-ok', limit=bad)
            self.assertEqual(ctx.exception.code, 'limit_invalid')
        with self.assertRaises(ProjectionError) as ctx:
            svc.context('talk-ok', 999999)
        self.assertEqual(ctx.exception.code, 'anchor_unknown')
        with self.assertRaises(ProjectionError) as ctx:
            svc.messages('talk-no-rule')
        self.assertEqual(ctx.exception.code, 'talker_not_queryable')


class HiddenSentinelGates(_ProjectionFixture):

    def test_every_hidden_sentinel_unfindable_and_absent_from_db_bytes(self):
        # raw_record_json never selected
        self._insert('talk-ok', 'clean body', raw=json.dumps(
            {'anything': RAW_SENT}))
        # revision payloads: table never read by the builder
        rev_uid = self._insert('talk-ok', 'has revisions', has_revisions=1)
        self._insert_revision(rev_uid, REV_SENT)
        # quote text under quotes=none
        self._insert('talk-qnone', 'no-quote body', content_json=json.dumps(
            {'quote': {'text': QUOTE_HIDDEN_SENT}}))
        # hidden sender (allowlist excludes wxid_bob)
        self._insert('talk-senders', f'hidden sender {HIDDEN_SENDER_SENT}',
                     sender='wxid_bob', sender_display='Bob')
        # revoked talker row
        self._insert('talk-rev', f'revoked {REVOKED_SENT}')
        self.store.revoke_talker(ACCOUNT, 'talk-rev')
        # unknown content_json key: value must never leak anywhere
        self._insert('talk-ok', 'unknown key body', content_json=json.dumps(
            {'appmsg_xml': UNKNOWN_KEY_VALUE_SENT}))
        # nested quote under text_only
        self._insert('talk-ok', 'nested body', content_json=json.dumps(
            {'quote': {'text': 'outer quote', 'quote':
                       {'text': NESTED_QUOTE_SENT}}}))
        # non-visible / deleted rows excluded entirely
        self._insert('talk-ok', 'redacted row', visibility='redacted')
        self._insert('talk-ok', 'deleted row', deleted_at=1700000123)
        self._insert('talk-ok', f'visible anchor {VISIBLE_KEEP}')

        manifest = self._build()
        svc = self._service()
        self.addCleanup(svc.close)
        sentinels = (RAW_SENT, REV_SENT, QUOTE_HIDDEN_SENT, HIDDEN_SENDER_SENT,
                     REVOKED_SENT, UNKNOWN_KEY_VALUE_SENT, NESTED_QUOTE_SENT)
        for talker in ('talk-ok', 'talk-qnone', 'talk-senders'):
            for sentinel in sentinels:
                self.assertEqual(
                    svc.search(talker, sentinel)['count'], 0,
                    f'{sentinel} leaked via FTS in {talker}')
        msgs = svc.messages('talk-ok', limit=200)['messages']
        joined = json.dumps(msgs, ensure_ascii=False)
        for sentinel in sentinels:
            self.assertNotIn(sentinel, joined)
        # the strongest check: sentinel bytes absent from the whole db file
        raw = self._db_path(manifest).read_bytes()
        for sentinel in sentinels:
            self.assertNotIn(sentinel.encode('utf-8'), raw,
                             f'{sentinel} bytes present in projection db')
        # ... and from every text column via SQL
        import sqlite3
        conn = sqlite3.connect(f'file:{self._db_path(manifest)}?mode=ro',
                               uri=True)
        try:
            for table, cols in (('msgs', ('vt', 'text_body', 'quote_text',
                                          'tags_json', 'media_ids_json',
                                          'sender_display')),
                                ('msgs_fts', ('vt',))):
                for col in cols:
                    for sentinel in sentinels:
                        n = conn.execute(
                            f'SELECT COUNT(*) FROM {table} '
                            f'WHERE {col} LIKE ?', (f'%{sentinel}%',)).fetchone()[0]
                        self.assertEqual(n, 0, f'{sentinel} in {table}.{col}')
        finally:
            conn.close()
        # exclusions accounted: 6 included of 8 visible rows
        cov = svc.coverage()['counts']
        self.assertEqual(cov['included'], 6)
        self.assertEqual(cov['master_visible_rows'], 8)
        self.assertEqual(cov['revoked_talker'], 1)
        self.assertEqual(cov['hidden_sender'], 1)
        self.assertEqual(cov['non_visible'], 1)
        self.assertEqual(cov['deleted'], 1)
        gaps = {g['kind']: g for g in svc.coverage()['coverage_gaps']}
        self.assertEqual(gaps['unknown_content_key']['count'], 2)
        self.assertIn('appmsg_xml', gaps['unknown_content_key']['sample_keys'])
        self.assertIn('quote.quote', gaps['unknown_content_key']['sample_keys'])
        self.assertEqual(gaps['nested_quote']['count'], 1)

    def test_sender_allowlist_filters_rows(self):
        self._insert('talk-senders', 'alice says hello', sender='wxid_alice',
                     sender_display='Alice')
        self._insert('talk-senders', f'bob says {HIDDEN_SENDER_SENT}',
                     sender='wxid_bob', sender_display='Bob')
        self._build()
        svc = self._service()
        self.addCleanup(svc.close)
        msgs = svc.messages('talk-senders')['messages']
        self.assertEqual([m['sender_display'] for m in msgs], ['Alice'])
        self.assertEqual(
            svc.search('talk-senders', 'hello')['count'], 1)
        self.assertEqual(
            svc.search('talk-senders', HIDDEN_SENDER_SENT)['count'], 0)

    def test_raw_xml_body_quote_sender_and_path_refs_never_leak(self):
        """Regression for the three Codex round-1 findings: verbatim XML body
        is whitelist-decoded (title only), a quote from a sender outside the
        allowlist is omitted even when the OUTER sender is visible, and media
        refs are irreversible hashes — no source path ever appears."""
        xml = ('<msg><appmsg><title>白名单标题可见</title><refermsg>'
               '<fromusr>hidden</fromusr><content>' + QUOTE_HIDDEN_SENT +
               '</content></refermsg></appmsg></msg>')
        self._insert('talk-ok', xml)  # content_text IS the raw XML
        # outer sender wxid_alice visible; quote sender wxid_bob is not
        cj = json.dumps({'text': '合法正文甲', 'quote': {
            'sender_id': 'wxid_bob', 'text': QUOTE_HIDDEN_SENT}})
        self._insert('talk-qsend', '合法正文甲', content_json=cj)
        # quote sender inside the allowlist passes the gate
        cj2 = json.dumps({'quote': {'sender_id': 'wxid_quote_visible',
                                    'text': QUOTE_VISIBLE_KEEP}})
        self._insert('talk-qsend', '正文乙', content_json=cj2)
        # media ref carrying a source path
        media = json.dumps([{'ref_key': 'file:/private/writer/'
                            + HIDDEN_SENDER_SENT + '.pdf', 'kind': 'file'}])
        self._insert('talk-ok', '带媒体正文', media=media)
        manifest = self._build()
        svc = self._service()
        self.addCleanup(svc.close)
        # the hidden content is unfindable in FTS, absent from payloads and
        # from the db file bytes — in BOTH talkers
        for talker in ('talk-ok', 'talk-qsend'):
            self.assertEqual(svc.search(talker, QUOTE_HIDDEN_SENT)['count'], 0)
            self.assertEqual(
                svc.search(talker, HIDDEN_SENDER_SENT)['count'], 0)
        payload = json.dumps(svc.messages('talk-ok', limit=50)['messages']
                             + svc.messages('talk-qsend', limit=50)['messages'],
                             ensure_ascii=False)
        self.assertNotIn(QUOTE_HIDDEN_SENT, payload)
        self.assertNotIn(HIDDEN_SENDER_SENT, payload)
        self.assertNotIn('file:/private/', payload)
        raw = self._db_path(manifest).read_bytes()
        for sentinel in (QUOTE_HIDDEN_SENT, HIDDEN_SENDER_SENT,
                         b'file:/private/'):
            self.assertNotIn(sentinel.encode('utf-8')
                             if isinstance(sentinel, str) else sentinel, raw)
        # whitelist decode keeps usability: title + plain body + allowed
        # quote text all stay searchable
        self.assertEqual(svc.search('talk-ok', '白名单标题可见')['count'], 1)
        self.assertEqual(svc.search('talk-qsend', '合法正文甲')['count'], 1)
        self.assertEqual(
            svc.search('talk-qsend', QUOTE_VISIBLE_KEEP)['count'], 1)
        bodies = {m['text'] for m in svc.messages('talk-ok')['messages']}
        self.assertEqual(bodies, {'白名单标题可见', '带媒体正文'})
        msgs = [m for m in svc.messages('talk-ok')['messages']
                if m['text'] == '带媒体正文']
        self.assertEqual(msgs[0]['media'],
                         [{'id': opaque_media_id(
                             'file:/private/writer/' + HIDDEN_SENDER_SENT
                             + '.pdf'), 'kind': 'file'}])
        gaps = {g['kind']: g for g in svc.coverage()['coverage_gaps']}
        self.assertEqual(gaps['quote_sender_filtered']['count'], 1)

    def test_xml_title_ancestry_and_prefixed_xml(self):
        """A <title> nested inside <refermsg> is hidden quoted content —
        never extracted even when it appears FIRST; only a title whose
        ancestry is msg/appmsg is decoded. A plain-text prefix before the
        XML document ('sender:\\n<msg>…') still triggers XML mode."""
        # refermsg title first, appmsg title second: must decode the SECOND
        xml1 = ('<msg><appmsg><refermsg><title>'
                + QUOTE_HIDDEN_SENT + '</title></refermsg>'
                '<title>合法标题甲</title></appmsg></msg>')
        self._insert('talk-ok', xml1)
        # sender-prefixed XML: prefix must not smuggle the XML past the gate
        xml2 = ('wxid_alice:\n<msg><appmsg><title>合法标题乙</title>'
                '<refermsg><content>' + QUOTE_HIDDEN_SENT +
                '</content></refermsg></appmsg></msg>')
        self._insert('talk-ok', xml2)
        # XML with ONLY an inadmissible title: conservative omission + gap
        xml3 = ('<msg><appmsg><refermsg><title>'
                + QUOTE_HIDDEN_SENT + '</title></refermsg></appmsg></msg>')
        self._insert('talk-ok', xml3)
        manifest = self._build()
        svc = self._service()
        self.addCleanup(svc.close)
        self.assertEqual(svc.search('talk-ok', QUOTE_HIDDEN_SENT)['count'], 0)
        raw = self._db_path(manifest).read_bytes()
        self.assertNotIn(QUOTE_HIDDEN_SENT.encode('utf-8'), raw)
        bodies = [m['text'] for m in svc.messages('talk-ok')['messages']]
        self.assertEqual(bodies, ['合法标题甲', '合法标题乙', ''])
        self.assertEqual(svc.search('talk-ok', '合法标题甲')['count'], 1)
        self.assertEqual(svc.search('talk-ok', '合法标题乙')['count'], 1)
        gaps = {g['kind']: g for g in svc.coverage()['coverage_gaps']}
        self.assertEqual(gaps['raw_xml_body_omitted']['count'], 1)

    def test_title_inner_subtree_dropped_and_unbalanced_close_aborts(self):
        """Round-2 structural holes: (a) a nested refermsg INSIDE an
        admissible title must lose its whole subtree text (never
        tag-stripped into the body) while the direct text stays; (b) an
        unbalanced close must abort extraction entirely — it may never pop
        the stack through refermsg ancestry and launder a title."""
        # (a) direct text kept, hidden subtree dropped
        xml_a = ('<msg><appmsg><title>合法正文'
                 '<refermsg><content>' + QUOTE_HIDDEN_SENT +
                 '</content></refermsg></title></appmsg></msg>')
        self._insert('talk-ok', xml_a)
        # (b) unbalanced </appmsg> inside refermsg>foo: strict parse aborts
        xml_b = ('<msg><appmsg><refermsg><foo></appmsg><title>'
                 + QUOTE_HIDDEN_SENT + '</title></foo></refermsg>'
                 '</appmsg></msg>')
        self._insert('talk-ok', xml_b)
        manifest = self._build()
        svc = self._service()
        self.addCleanup(svc.close)
        self.assertEqual(svc.search('talk-ok', QUOTE_HIDDEN_SENT)['count'], 0)
        raw = self._db_path(manifest).read_bytes()
        self.assertNotIn(QUOTE_HIDDEN_SENT.encode('utf-8'), raw)
        bodies = [m['text'] for m in svc.messages('talk-ok')['messages']]
        self.assertEqual(bodies, ['合法正文', ''])
        self.assertEqual(svc.search('talk-ok', '合法正文')['count'], 1)
        gaps = {g['kind']: g for g in svc.coverage()['coverage_gaps']}
        self.assertEqual(gaps['raw_xml_body_omitted']['count'], 1)


class RevocationGates(_ProjectionFixture):

    def test_revoke_invalidates_before_publish_same_process(self):
        self._insert('talk-ok', f'{VISIBLE_KEEP} one')
        self._insert('talk-rev', f'revoked content {REVOKED_SENT}')
        self._build()
        svc = self._service()
        self.addCleanup(svc.close)
        session = svc.open_session()
        self.addCleanup(session.close)
        self.assertEqual(session.search('talk-ok', VISIBLE_KEEP)['count'], 1)
        manifest_before = (self.proj_root / 'manifest.json').read_bytes()

        # revocation step ONLY — no rebuild yet
        info = self.builder.revoke(ACCOUNT, 'talk-rev')
        self.assertEqual(info['revocations_version'], 1)
        # ordering proof: the published manifest is untouched by revoke()
        self.assertEqual((self.proj_root / 'manifest.json').read_bytes(),
                         manifest_before)
        # already-open reader dies on its NEXT request
        with self.assertRaises(ProjectionError) as ctx:
            session.search('talk-ok', VISIBLE_KEEP)
        self.assertEqual(ctx.exception.code, 'session_stale')
        # fresh session for the revoked talker is denied even though the old
        # generation file still contains its rows
        fresh = svc.open_session()
        self.addCleanup(fresh.close)
        with self.assertRaises(ProjectionError) as ctx:
            fresh.search('talk-rev', REVOKED_SENT)
        self.assertEqual(ctx.exception.code, 'talker_revoked')
        # other talkers keep working on the old generation
        ok = svc.open_session()
        self.addCleanup(ok.close)
        self.assertEqual(ok.search('talk-ok', VISIBLE_KEEP)['count'], 1)

        # now the clean rebuild
        manifest = self.builder.build_and_publish()
        self.assertNotIn('talk-rev', manifest['talkers'])
        self.builder.prune()
        # old generation files beyond retention are gone; queries still fine
        svc2 = self._service()
        self.addCleanup(svc2.close)
        self.assertEqual(
            svc2.search('talk-ok', VISIBLE_KEEP)['count'], 1)
        with self.assertRaises(ProjectionError) as ctx:
            svc2.messages('talk-rev')
        self.assertEqual(ctx.exception.code, 'talker_revoked')

    def test_revoke_blocks_open_reader_cross_process(self):
        self._insert('talk-ok', f'{VISIBLE_KEEP} one')
        self._insert('talk-rev', f'revoked content {REVOKED_SENT}')
        self._build()
        ready = Path(self._tmp.name) / 'ready.flag'
        go = Path(self._tmp.name) / 'go.flag'
        env = dict(os.environ)
        env['PYTHONPATH'] = str(REPO_ROOT)
        child = subprocess.Popen(
            [sys.executable, '-c', _CHILD_READER, str(self.proj_root),
             str(ready), str(go)],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            env=env, cwd=str(REPO_ROOT))
        try:
            deadline = time.time() + 30
            while not ready.exists():
                if time.time() > deadline or child.poll() is not None:
                    self.fail(f'child never became ready: '
                              f'{child.stdout.read() if child.stdout else ""}')
                time.sleep(0.05)
            self.builder.revoke_and_rebuild(ACCOUNT, 'talk-rev')
            go.write_text('1')
            out, _ = child.communicate(timeout=60)
        finally:
            if child.poll() is None:
                child.kill()
        self.assertIn('PHASE1-OK', out)
        self.assertIn('PHASE2-OK session_stale', out)
        self.assertIn('PHASE3-OK talker_revoked', out)
        self.assertIn('CHILD-DONE', out)
        self.assertEqual(child.returncode, 0, out)

    def test_new_generation_stales_old_sessions(self):
        self._insert('talk-ok', f'{VISIBLE_KEEP} one')
        self._build()
        svc = self._service()
        self.addCleanup(svc.close)
        session = svc.open_session()
        self.assertEqual(session.search('talk-ok', VISIBLE_KEEP)['count'], 1)
        self._insert('talk-ok', 'new message')
        self._build()  # normal refresh, no revocation
        with self.assertRaises(ProjectionError) as ctx:
            session.search('talk-ok', VISIBLE_KEEP)
        self.assertEqual(ctx.exception.code, 'session_stale')
        fresh = svc.open_session()
        self.addCleanup(fresh.close)
        self.assertEqual(fresh.messages('talk-ok')['count'], 2)


class FailClosedGates(_ProjectionFixture):

    def test_policy_missing_rejected(self):
        self._insert('talk-ok', 'body')
        self._build()
        (self.proj_root / 'policy' / 'policy.json').unlink()
        svc = self._service()
        self.addCleanup(svc.close)
        with self.assertRaises(ProjectionError) as ctx:
            svc.manifest()
        self.assertEqual(ctx.exception.code, 'policy_missing')

    def test_revocations_missing_rejected(self):
        self._insert('talk-ok', 'body')
        self._build()
        (self.proj_root / 'policy' / 'revocations.json').unlink()
        svc = self._service()
        self.addCleanup(svc.close)
        with self.assertRaises(ProjectionError) as ctx:
            svc.search('talk-ok', VISIBLE_KEEP)
        self.assertEqual(ctx.exception.code, 'revocations_missing')

    def test_policy_invalid_rejected(self):
        self._insert('talk-ok', 'body')
        self._build()
        (self.proj_root / 'policy' / 'policy.json').write_text(
            json.dumps({'policy_version': 1, 'rules': {ACCOUNT: {
                'talk-ok': {'query': True, 'quotes': 'full'}}}}))
        svc = self._service()
        self.addCleanup(svc.close)
        with self.assertRaises(ProjectionError) as ctx:
            svc.search('talk-ok', VISIBLE_KEEP)
        self.assertEqual(ctx.exception.code, 'policy_invalid')

    def test_epoch_stale_rejected(self):
        self._insert('talk-ok', 'body')
        self._build()
        svc = self._service(expected_epoch=2)
        self.addCleanup(svc.close)
        with self.assertRaises(ProjectionError) as ctx:
            svc.manifest()
        self.assertEqual(ctx.exception.code, 'epoch_stale')

    def test_manifest_missing_and_corrupt(self):
        self._insert('talk-ok', 'body')
        self._build()
        (self.proj_root / 'manifest.json').unlink()
        svc = self._service()
        self.addCleanup(svc.close)
        with self.assertRaises(ProjectionError) as ctx:
            svc.manifest()
        self.assertEqual(ctx.exception.code, 'projection_unpublished')
        self._build()
        (self.proj_root / 'manifest.json').write_text('{"broken": ')
        with self.assertRaises(ProjectionError) as ctx:
            svc.manifest()
        self.assertEqual(ctx.exception.code, 'projection_manifest_invalid')

    def test_master_root_rejected(self):
        self._insert('talk-ok', 'body')
        self._build()
        # guard rejects the master root for the projection identity...
        with self.assertRaises(GuardError):
            ProjectionService.open(
                self.root, projection_id=PROJECTION_ID, account=ACCOUNT,
                expected_epoch=1, allow_synthetic=True)
        # ...and even with a matching id, the master.db belt refuses it
        with self.assertRaises(ProjectionError) as ctx:
            ProjectionService.open(
                self.root, projection_id=ARCHIVE_ID, account=ACCOUNT,
                expected_epoch=1, allow_synthetic=True)
        self.assertEqual(ctx.exception.code, 'master_root_rejected')

    def test_db_corruption_detected_by_sha(self):
        self._insert('talk-ok', f'{VISIBLE_KEEP} body')
        manifest = self._build()
        svc = self._service()
        self.addCleanup(svc.close)
        self.assertEqual(svc.search('talk-ok', VISIBLE_KEEP)['count'], 1)
        db = self._db_path(manifest)
        data = bytearray(db.read_bytes())
        data[len(data) // 2] ^= 0xFF  # same size, different bytes
        db.write_bytes(bytes(data))
        with self.assertRaises(ProjectionError) as ctx:
            svc.search('talk-ok', VISIBLE_KEEP)
        self.assertEqual(ctx.exception.code, 'projection_db_corrupt')

    def test_query_side_never_opens_master_subtree(self):
        """sys.audit proof at the Python level: during the service's lifetime
        no open() touches master.db / writer.lock / staging / objects. (C-level
        sqlite file opens are not Python-audited; physical isolation is
        separately proven by the Linux nobody gate.)"""
        self._insert('talk-ok', f'{VISIBLE_KEEP} body')
        self._build()
        opened = []
        collecting = []

        def hook(event, args):
            if event == 'open' and collecting:
                try:
                    path = args[0]
                    if isinstance(path, (str, bytes, os.PathLike)):
                        opened.append(os.fspath(path))
                except Exception:
                    pass

        sys.addaudithook(hook)
        svc = self._service()
        try:
            collecting.append(True)
            svc.manifest()
            svc.messages('talk-ok', limit=5)
            svc.search('talk-ok', VISIBLE_KEEP)
            svc.sessions()
            svc.coverage()
            session = svc.open_session()
            session.search('talk-ok', VISIBLE_KEEP)
            session.close()
        finally:
            collecting.clear()
            svc.close()
        forbidden = ('master.db', 'writer.lock', 'master.db-wal',
                     'master.db-shm')
        for path in opened:
            name = os.path.basename(path) if path.startswith('/') else path
            self.assertNotIn(name, forbidden,
                             f'query side opened master subtree: {path}')
            self.assertFalse(path.startswith(('staging', 'objects')),
                             f'query side opened master subtree: {path}')


class BuildIntegrityGates(_ProjectionFixture):

    def test_build_failure_publishes_nothing(self):
        self._insert('talk-ok', f'{VISIBLE_KEEP} one')
        self._build()
        manifest_before = (self.proj_root / 'manifest.json').read_bytes()
        files_before = self._proj_files()
        self._insert('talk-ok', 'second row')

        original = ProjectionBuilder._project_row
        state = {'n': 0}

        def exploding(self, row, *args, **kwargs):
            state['n'] += 1
            if state['n'] > 1:
                raise RuntimeError('injected mid-build failure')
            return original(self, row, *args, **kwargs)

        with mock.patch.object(ProjectionBuilder, '_project_row', exploding):
            with self.assertRaises(ProjectionError) as ctx:
                self.builder.build_and_publish()
        self.assertEqual(ctx.exception.code, 'build_failed')
        # manifest bytes untouched, no new generation, no temp residue
        self.assertEqual((self.proj_root / 'manifest.json').read_bytes(),
                         manifest_before)
        self.assertEqual(self._proj_files(), files_before)
        self.assertFalse([p for p in self._proj_files()
                          if p.startswith('.p-build-')])
        # the archive still serves the previous generation
        svc = self._service()
        self.addCleanup(svc.close)
        self.assertEqual(svc.messages('talk-ok')['count'], 1)

    def test_prune_keeps_current_generation(self):
        for i in range(3):
            self._insert('talk-ok', f'round {i}')
            self._build()
        svc = self._service()
        self.addCleanup(svc.close)
        current_db = svc.manifest()['db']
        files = self._proj_files()
        self.assertEqual(len([f for f in files if f.startswith('p-g')]), 3)
        pruned = self.builder.prune(keep=2)
        self.assertTrue(pruned['removed'])
        files = self._proj_files()
        kept = [f for f in files if f.startswith('p-g')]
        self.assertEqual(len(kept), 2)
        self.assertIn(current_db, kept)
        self.assertEqual(svc.messages('talk-ok', limit=10)['count'], 3)

    def test_mode_layout_matches_contract(self):
        self._insert('talk-ok', 'body')
        self._build()
        mode = lambda p: p.stat().st_mode & 0o777  # noqa: E731
        self.assertEqual(mode(self.proj_root), 0o750)
        self.assertEqual(mode(self.proj_root / 'policy'), 0o750)
        for name in ('manifest.json', 'policy/policy.json',
                     'policy/revocations.json'):
            self.assertEqual(mode(self.proj_root / name), 0o640)
        manifest = json.loads((self.proj_root / 'manifest.json').read_text())
        self.assertEqual(mode(self._db_path(manifest)), 0o640)
        self.assertEqual(mode(self.proj_root / manifest['report']), 0o640)
        self.assertEqual(mode(self.proj_root / '.v3_archive_identity.json'),
                         0o640)

    def test_init_refuses_reinit_and_bad_policy(self):
        with self.assertRaises(ProjectionError) as ctx:
            ProjectionBuilder.init(self.store, _policy())
        self.assertEqual(ctx.exception.code, 'projection_exists')
        bad = {'policy_version': 3, 'rules': {ACCOUNT: {
            'talk-ok': {'query': True, 'surprise': 1}}}}
        with self.assertRaises(ProjectionError) as ctx:
            ProjectionBuilder.init(self.store, bad)
        self.assertEqual(ctx.exception.code, 'policy_invalid')
        with self.assertRaises(ProjectionError) as ctx:
            ProjectionBuilder.init(
                self.store, {'policy_version': 0, 'rules': {}})
        self.assertEqual(ctx.exception.code, 'policy_invalid')

    def test_set_policy_stales_sessions(self):
        self._insert('talk-ok', 'body')
        self._build()
        svc = self._service()
        self.addCleanup(svc.close)
        session = svc.open_session()
        policy = _policy()
        policy['policy_version'] = 4
        self.builder.set_policy(policy)
        with self.assertRaises(ProjectionError) as ctx:
            session.messages('talk-ok')
        self.assertEqual(ctx.exception.code, 'session_stale')
        # a rebuild under the new policy serves with the new version
        self._build()
        self.assertEqual(self._service().policy_version(), 4)

    def test_policy_tightening_blocks_every_reader_until_rebuild(self):
        self._insert('talk-ok', 'VISIBLE-KEEP-before-tightening',
                     sender='wxid_alice')
        hidden_uid = self._insert('talk-ok', 'POLICY-HIDDEN-9f4c2a',
                                  sender='wxid_bob')
        self._build()
        svc = self._service()
        self.addCleanup(svc.close)
        old_session = svc.open_session()
        self.addCleanup(old_session.close)

        tightened = _policy()
        tightened['policy_version'] = 4
        tightened['rules'][ACCOUNT]['talk-ok']['senders'] = {
            'mode': 'allowlist', 'allow': ['wxid_alice']}
        self.builder.set_policy(tightened)

        # Until the new generation publishes, neither existing nor freshly
        # opened readers may consult the old broader generation.
        for query in (lambda: svc.messages('talk-ok'),
                      lambda: svc.search('talk-ok', 'POLICY-HIDDEN'),
                      lambda: svc.context('talk-ok', hidden_uid),
                      lambda: svc.sessions(), lambda: svc.manifest(),
                      lambda: svc.open_session()):
            with self.assertRaises(ProjectionError) as ctx:
                query()
            self.assertEqual(ctx.exception.code, 'session_stale')

        self._build()
        fresh = self._service()
        self.addCleanup(fresh.close)
        result = fresh.messages('talk-ok')
        self.assertEqual(result['count'], 1)
        self.assertNotIn('POLICY-HIDDEN-9f4c2a', json.dumps(result))
        self.assertEqual(fresh.search('talk-ok', 'POLICY-HIDDEN')['count'], 0)

    def test_same_policy_version_cannot_change_policy_content(self):
        original = _policy()
        altered = _policy()
        altered['rules'][ACCOUNT]['talk-ok']['senders'] = {
            'mode': 'allowlist', 'allow': ['wxid_alice']}
        self.assertEqual(original['policy_version'], altered['policy_version'])
        with self.assertRaises(ProjectionError) as ctx:
            self.builder.set_policy(altered)
        self.assertEqual(ctx.exception.code, 'policy_invalid')
        self.assertEqual(self.builder._read_policy(), original)

    def test_identical_policy_same_version_is_idempotent(self):
        result = self.builder.set_policy(_policy())
        self.assertEqual(result['policy_version'], 3)


@unittest.skipUnless(_IS_LINUX, 'Linux-only gate (colima wx-archive-v3-test)')
class LinuxOsUserGate(unittest.TestCase):
    """Real different-OS-user gate: nobody (via sudo, no system changes) can
    query the projection end-to-end but cannot read master.db nor list the
    archive root. Skips honestly when sudo is unavailable."""

    def _sudo_ok(self):
        if not shutil.which('sudo'):
            return False
        return subprocess.run(['sudo', '-n', 'true']).returncode == 0

    def test_nobody_queries_projection_but_not_master(self):
        if not self._sudo_ok():
            self.skipTest('sudo unavailable — OS-user gate not provable here')
        VM_BASE.mkdir(parents=True, exist_ok=True)
        tmp = tempfile.mkdtemp(dir=VM_BASE)
        self.addCleanup(shutil.rmtree, tmp, True)
        os.chmod(tmp, 0o711)  # mkdtemp is 0700; the query user must be able
        #                         to REACH the archive root (ops-equivalent of
        #                         a traversable mount path above the archive)
        root = Path(tmp) / 'archive'
        root.mkdir()
        proj_root = root / 'projection'
        SyntheticGuard.write_marker(root, ARCHIVE_ID, 1, 0)
        binding = SyntheticGuard(ARCHIVE_ID).verify(root)
        scope = {ACCOUNT: {t: {'capture': 1, 'query': 1} for t in
                           ('talk-ok', 'talk-rev')}}
        store = MasterStore.initialize(binding, ARCHIVE_ID, scope)
        try:
            store.acquire_writer_lock()
            with store.full_transaction():
                store.db.execute(
                    'INSERT INTO messages(msg_uid,account,talker,server_id,'
                    'shard,shard_table,local_rowid,create_time,sort_seq,'
                    'msg_type,sub_type,status,direction,sender_id,'
                    'sender_display_name,content_text,content_json,'
                    'packed_info_sha256,record_sha256,raw_record_json,'
                    'media_refs_json,provenance,visibility,has_revisions,'
                    'first_commit_seq,last_content_or_presence_commit_seq,'
                    'deleted_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                    (1, ACCOUNT, 'talk-ok', 'srv-1', 'db0', 'Msg_x', 1,
                     1700000001, 1, 1, 0, 0, 'in', 'wxid_alice', 'Alice',
                     f'visible {VISIBLE_KEEP} body', '{}', 'pi', 'rec', '{}',
                     '[]', 'collector', 'visible', 0, 1, 1, None))
            builder = ProjectionBuilder.init(store, _policy())
            builder.build_and_publish()
        finally:
            store.release_writer_lock()
            store.close()
            binding.close()
        # ops layout: query group = nogroup (nobody's primary group)
        subprocess.run(['sudo', '-n', 'chgrp', '-R', 'nogroup',
                        str(proj_root)], check=True)
        os.chmod(root, 0o711)  # traverse-only for others (ops decision)
        env = dict(os.environ)
        env['PYTHONPATH'] = str(REPO_ROOT)
        result = subprocess.run(
            ['sudo', '-n', '-u', 'nobody', sys.executable, '-c',
             _NOBODY_QUERY, str(proj_root), str(root), str(REPO_ROOT)],
            capture_output=True, text=True, timeout=120)
        self.assertEqual(result.returncode, 0,
                         f'stdout={result.stdout}\nstderr={result.stderr}')
        self.assertIn('MASTER-UNREADABLE-OK', result.stdout)
        self.assertIn('ROOT-UNLISTABLE-OK', result.stdout)
        self.assertIn('NOBODY-QUERY-OK', result.stdout)
        # master stayed owner-only through it all
        mode = (root / 'master.db').stat().st_mode & 0o777
        self.assertEqual(mode, 0o600)


if __name__ == '__main__':
    unittest.main()
