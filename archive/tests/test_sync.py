"""Deterministic sync-layer tests against a real store.Archive and a scripted
fake wx binary. Covers chunk windows, trusted-empty handling, warning marker
rules, checkpoint/overlap incrementals, whitelist discovery paging, reconcile
atomicity, lock serialization, and log privacy."""
import fcntl
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import time
import unittest

ARCHIVE_DIR = Path(__file__).resolve().parents[1]
if str(ARCHIVE_DIR) not in sys.path:
    sys.path.insert(0, str(ARCHIVE_DIR))

import sync  # noqa: E402
from store import Archive  # noqa: E402

ACCT = 'howiefire'
T = 'group@chatroom'

# Scripted stand-in for the wx binary. Responses are matched deterministically
# by request (talker+window for export, offset for sessions), never by call
# order; unmatched requests exit 3 so test bugs cannot pass silently.
FAKE_WX_BODY = r'''
import json, os, sys
from pathlib import Path

def fail(msg):
    sys.stderr.write('fake-wx: ' + msg + '\n')
    raise SystemExit(3)

control = json.loads(Path(os.environ['WX_FAKE_CONTROL']).read_text(encoding='utf-8'))
argv = sys.argv[1:]

def value(flag):
    if flag in argv:
        i = argv.index(flag)
        if i + 1 < len(argv):
            return argv[i + 1]
    return None

def record(call):
    with open(os.environ['WX_FAKE_CALLS'], 'a', encoding='utf-8') as fh:
        fh.write(json.dumps(call, ensure_ascii=False) + '\n')

sub = argv[0] if argv else fail('missing subcommand')
if sub == 'export':
    talker = argv[1]
    since, until, out = int(value('--since')), int(value('--until')), value('-o')
    record({'subcommand': 'export', 'talker': talker, 'since': since,
            'until': until, 'no_media': '--no-media' in argv,
            'account': value('--account')})
    entry = None
    for candidate in control.get('exports', []):
        if candidate.get('talker', talker) != talker:
            continue
        if candidate.get('since') == since and candidate.get('until') == until:
            entry = candidate
            break
    if entry is None:
        fail('no scripted export for talker=%s window=[%d,%d]' % (talker, since, until))
    sys.stderr.write(entry.get('stderr', ''))
    if entry.get('rc'):
        raise SystemExit(entry['rc'])
    if not entry.get('empty'):
        for relative, content in entry.get('assets', {}).items():
            asset = Path(out) / relative
            asset.parent.mkdir(parents=True, exist_ok=True)
            asset.write_bytes(content.encode('utf-8'))
        for n in range(entry.get('json_count', 1)):
            (Path(out) / ('export_%d.json' % n)).write_text(
                json.dumps(entry.get('envelope'), ensure_ascii=False, indent=1),
                encoding='utf-8')
    raise SystemExit(0)
if sub == 'sessions':
    offset = int(value('--offset') or 0)
    record({'subcommand': 'sessions', 'offset': offset,
            'no_server': '--no-server' in argv, 'format': value('--format'),
            'all': '--all' in argv, 'account': value('--account')})
    entry = next((c for c in control.get('sessions', [])
                  if c.get('offset', 0) == offset), None)
    if entry is None:
        fail('no scripted sessions page for offset=%d' % offset)
    sys.stderr.write(entry.get('stderr', ''))
    if entry.get('rc'):
        raise SystemExit(entry['rc'])
    sys.stdout.write(json.dumps(entry.get('envelope'), ensure_ascii=False))
    raise SystemExit(0)
fail('unknown subcommand ' + sub)
'''

OPEN_NOTE = 'Direct encrypted open (SQLCipher) for messages; decrypt cache for media resolution\n'
EMPTY_STDERR = OPEN_NOTE + 'No messages found for export.\n'


def message(sid, ts, text, sender='alice'):
    return {'server_id': sid, 'talker': T, 'sender': sender,
            'sender_display_name': sender.title(), 'create_time': ts,
            'sort_seq': sid * 10, 'msg_type': 1,
            'content': {'Text': text}, 'snippet': text}


def media_message(sid, ts, text, state='missing', reason='missing_local_media',
                  content=None, media_files=None):
    item = message(sid, ts, text)
    item['content'] = {'Image': {'md5': 'shared-reference'}} if content is None else content
    item['media_status'] = {'state': state}
    if reason is not None:
        item['media_status']['reason'] = reason
    if media_files is not None:
        item['media_files'] = media_files
    return item


def envelope(items, talker=T, media_mode='enabled'):
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
    return {'export_info': {'version': '1',
                            'exported_at': '2026-10-02T00:00:00+08:00',
                            'generator': 'fake'},
            'conversation': {'talker': talker, 'display_name': '群',
                             'type': 'group', 'message_count': len(items)},
            'items': items,
            'media': dict(version=1, mode=media_mode, **counts),
            'paging': {'limit': 0, 'offset': 0, 'returned': len(items),
                       'has_more': False, 'total': len(items)},
            'stats': {'scanned': len(items), 'skipped': 0}}


def session_item(username, ts):
    return {'username': username, 'sort_timestamp': ts, 'summary': 'x',
            'display_name': username, 'last_msg_type': 1}


def sessions_envelope(items, offset=0, has_more=False):
    return {'items': items,
            'paging': {'limit': 20000, 'offset': offset,
                       'returned': len(items), 'has_more': has_more,
                       'total': len(items)},
            'stats': {'scanned': len(items), 'skipped': 0}}


def export_entry(since, until, items, stderr=OPEN_NOTE, talker=T,
                 media_mode='enabled', **extra):
    items = [dict(item, talker=talker) for item in items]
    entry = {'talker': talker, 'since': since, 'until': until,
             'stderr': stderr, 'envelope': envelope(items, talker, media_mode)}
    entry.update(extra)
    return entry


def empty_entry(since, until, stderr=EMPTY_STDERR, talker=T, **extra):
    entry = {'talker': talker, 'since': since, 'until': until,
             'stderr': stderr, 'empty': True}
    entry.update(extra)
    return entry


class SyncTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.archive = Archive(self.root / 'store')
        self.control = self.root / 'control.json'
        self.calls = self.root / 'calls.jsonl'
        self.binary = self.root / 'fake-wx'
        self.binary.write_text(
            '#!/usr/bin/env ' + sys.executable + '\n' + FAKE_WX_BODY,
            encoding='utf-8')
        self.binary.chmod(0o755)
        os.environ['WX_FAKE_CONTROL'] = str(self.control)
        os.environ['WX_FAKE_CALLS'] = str(self.calls)

    def tearDown(self):
        self.archive.db.close()
        os.environ.pop('WX_FAKE_CONTROL', None)
        os.environ.pop('WX_FAKE_CALLS', None)
        self.tmp.cleanup()

    def script(self, exports=(), sessions=()):
        self.control.write_text(
            json.dumps({'exports': list(exports), 'sessions': list(sessions)},
                       ensure_ascii=False), encoding='utf-8')

    def calls_read(self):
        if not self.calls.exists():
            return []
        return [json.loads(line) for line in
                self.calls.read_text(encoding='utf-8').splitlines() if line.strip()]

    def exports_called(self):
        return [c for c in self.calls_read() if c['subcommand'] == 'export']

    def record(self, text):
        hits = self.archive.search(text, ACCT, talker=T)
        self.assertEqual(len(hits), 1, f'expected exactly one record for {text!r}')
        return hits[0]

    # ── chunk windows ────────────────────────────────────────────────────

    def test_chunk_windows_bounded_inclusive_and_checkpoint(self):
        since = 1_000_000
        until = since + 3 * 86400 - 1  # exactly three max-size chunks
        windows = [(since, since + 86399), (since + 86400, since + 172799),
                   (since + 172800, until)]
        items = [[message(1, windows[0][0], 'm1')],
                 [message(2, windows[1][0], 'm2'),
                  message(3, windows[1][0] + 5, 'm3')],
                 [message(4, until, 'm4')]]
        self.script(exports=[export_entry(a, b, msgs)
                             for (a, b), msgs in zip(windows, items)])
        summary = sync.sync(self.archive, self.binary, ACCT, T, since, until)
        self.assertEqual([(c['since'], c['until']) for c in self.exports_called()],
                         windows)
        self.assertEqual(summary['chunks'], 3)
        self.assertEqual(summary['status'], 'imported')
        self.assertEqual(summary['added'], 4)
        self.assertEqual(summary['changed'], 0)
        self.assertEqual(summary['checkpoint'], until)
        self.assertEqual(self.archive.checkpoint(ACCT, T), until)
        self.assertEqual(self.archive.status()['messages'], 4)

    def test_same_second_group_never_split_across_chunk_boundary(self):
        since = 100_000
        boundary = since + 86399  # last second of chunk 1; +1s starts chunk 2
        five = [message(n, boundary, f'same{n}') for n in range(1, 6)]
        next_group = [message(9, boundary + 1, 'next')]
        self.script(exports=[export_entry(since, boundary, five),
                              export_entry(boundary + 1, boundary + 100, next_group)])
        summary = sync.sync(self.archive, self.binary, ACCT, T, since, boundary + 100)
        self.assertEqual(summary['chunks'], 2)
        self.assertEqual([(c['since'], c['until']) for c in self.exports_called()],
                         [(since, boundary), (boundary + 1, boundary + 100)])
        self.assertEqual(self.archive.status()['messages'], 6)
        for n in range(1, 6):
            self.assertEqual(self.record(f'same{n}')['message']['server_id'], n)

    def test_failed_middle_chunk_keeps_checkpoint_at_last_success(self):
        since = 200_000
        w1, w2, w3 = (since, since + 999), (since + 1000, since + 1999), \
                     (since + 2000, since + 2999)
        self.script(exports=[export_entry(*w1, [message(1, since + 5, 'ok1')]),
                             {'talker': T, 'since': w2[0], 'until': w2[1],
                              'stderr': 'error: database is locked\n', 'rc': 1}])
        with self.assertRaises(ValueError):
            sync.sync(self.archive, self.binary, ACCT, T, since, w3[1],
                      chunk_seconds=1000)
        self.assertEqual(self.archive.checkpoint(ACCT, T), w1[1])
        self.assertEqual(self.archive.status()['messages'], 1)
        self.record('ok1')  # first chunk's data survived
        self.assertEqual(len(self.exports_called()), 2)

    # ── warning/ambiguity marker rules ───────────────────────────────────

    def test_warning_markers_vs_failed_filename_and_ambiguity(self):
        w_failname = (300_000, 300_100)
        # Four ambiguous/failing sources, each its own window: a real warning
        # line, no JSON without the empty marker, two JSON files, and the
        # privacy-filtered "no visible messages" variant (not trusted empty).
        self.script(exports=[
            {'talker': T, 'since': 300_101, 'until': 300_200,
             'stderr': 'warning: shard msg_0.db: decode error\n', 'rc': 0,
             'empty': True},
            {'talker': T, 'since': 300_201, 'until': 300_300,
             'stderr': OPEN_NOTE, 'rc': 0, 'empty': True},
            {'talker': T, 'since': 300_301, 'until': 300_400,
             'stderr': OPEN_NOTE, 'rc': 0,
             'envelope': envelope([message(6, 300_350, 'ambig')]),
             'json_count': 2},
            {'talker': T, 'since': 300_401, 'until': 300_500,
             'stderr': OPEN_NOTE + 'No visible messages found for export '
                                   '(all filtered by sender hiding).\n',
             'rc': 0, 'empty': True},
        ])
        for since, until in ((300_101, 300_200), (300_201, 300_300),
                             (300_301, 300_400), (300_401, 300_500)):
            with self.assertRaises(ValueError):
                sync.sync(self.archive, self.binary, ACCT, T, since, until)
        self.assertIsNone(self.archive.checkpoint(ACCT, T))
        self.assertEqual(self.archive.status()['messages'], 0)
        # A file *named* "failed..." is not a warning marker: import proceeds.
        self.script(exports=[export_entry(
            *w_failname, [message(5, 300_050, 'despite')],
            stderr=OPEN_NOTE + 'skipped transcode media/failed_audio.dat\n')])
        summary = sync.sync(self.archive, self.binary, ACCT, T, *w_failname)
        self.assertEqual(summary['status'], 'imported')
        self.assertEqual(self.archive.checkpoint(ACCT, T), w_failname[1])

    def test_trusted_empty_marker_advances_checkpoint(self):
        window = (400_000, 400_100)
        self.script(exports=[empty_entry(*window)])
        summary = sync.sync(self.archive, self.binary, ACCT, T, *window)
        self.assertEqual(summary['status'], 'empty')
        self.assertEqual(summary['added'], 0)
        self.assertEqual(self.archive.checkpoint(ACCT, T), window[1])
        self.assertEqual(self.archive.status()['messages'], 0)

    # ── incremental overlap / not-due ────────────────────────────────────

    def test_incremental_overlap_dedupe_and_not_due(self):
        self.script(exports=[export_entry(500_000, 500_999,
                                          [message(1, 500_100, 'over1'),
                                           message(2, 500_950, 'over2')])])
        sync.sync(self.archive, self.binary, ACCT, T, 500_000, 500_999)
        self.assertEqual(self.archive.checkpoint(ACCT, T), 500_999)
        # Window [checkpoint-300, now-100] = [500_699, 501_499]; the overlapped
        # message (ts 500_950) reappears and must dedupe instead of re-adding.
        self.script(exports=[export_entry(500_699, 501_499,
                                          [message(2, 500_950, 'over2'),
                                           message(3, 501_000, 'fresh3')])])
        summary = sync.sync_incremental(self.archive, self.binary, ACCT, T,
                                        initial_since=500_000,
                                        overlap_seconds=300, settle_seconds=100,
                                        now=501_599)
        self.assertEqual(summary['status'], 'imported')
        self.assertEqual(summary['since'], 500_699)
        self.assertEqual(summary['until'], 501_499)
        self.assertEqual(summary['added'], 1)   # overlapped message deduped
        self.assertEqual(summary['changed'], 0)
        self.assertEqual(summary['checkpoint_before'], 500_999)
        self.assertEqual(self.archive.checkpoint(ACCT, T), 501_499)
        before = len(self.calls_read())
        summary = sync.sync_incremental(self.archive, self.binary, ACCT, T,
                                        initial_since=500_000,
                                        overlap_seconds=300, settle_seconds=100,
                                        now=500_799)
        self.assertEqual(summary['status'], 'not_due')
        self.assertEqual(len(self.calls_read()), before)  # no export ran
        self.assertEqual(self.archive.checkpoint(ACCT, T), 501_499)

    # ── discovery ────────────────────────────────────────────────────────

    def test_discover_whitelist_filter_and_session_paging(self):
        peer = 'peer@wxid'
        self.script(
            exports=[export_entry(600_000, 600_899, [message(1, 600_100, 'd1')]),
                     export_entry(600_000, 600_899, [message(7, 600_100, 'd7')],
                                  talker=peer)],
            sessions=[
                {'offset': 0, 'stderr': '', 'rc': 0,
                 'envelope': sessions_envelope(
                     [session_item(T, 600_500), session_item('stranger@x', 600_900),
                      session_item(peer, 600_001)], offset=0, has_more=True)},
                {'offset': 3, 'stderr': '', 'rc': 0,
                 'envelope': sessions_envelope([session_item(peer, 600_400)],
                                               offset=3, has_more=False)},
            ])
        result = sync.discover(self.archive, self.binary, ACCT, [T, peer], 600_050,
                               initial_since=600_000, settle_seconds=100,
                               now=600_999)
        self.assertEqual(result['changed'], [T, peer])
        self.assertEqual(sorted(result['results']), sorted([T, peer]))
        self.assertEqual(result['results'][T]['added'], 1)
        sessions_calls = [c for c in self.calls_read()
                          if c['subcommand'] == 'sessions']
        self.assertEqual([c['offset'] for c in sessions_calls], [0, 3])
        self.assertTrue(all(c['no_server'] and c['all'] and c['format'] == 'json'
                            for c in sessions_calls))
        exported = [c['talker'] for c in self.exports_called()]
        self.assertEqual(sorted(exported), sorted([T, peer]))  # stranger skipped

    def test_discover_sessions_failures(self):
        self.script(sessions=[{'offset': 0, 'stderr': 'error: no key\n', 'rc': 2}])
        with self.assertRaises(ValueError):
            sync.discover(self.archive, self.binary, ACCT, [T], 1,
                          initial_since=1, settle_seconds=0, now=100)
        self.assertEqual(self.exports_called(), [])
        self.script(sessions=[{'offset': 0, 'stderr': '', 'rc': 0,
                               'envelope': {'items': [],
                                            'paging': {'offset': 0, 'returned': 0,
                                                       'has_more': False},
                                            'stats': {'skipped': 3}}}])
        with self.assertRaises(ValueError):
            sync.discover(self.archive, self.binary, ACCT, [T], 1,
                          initial_since=1, settle_seconds=0, now=100)

    # ── reconciliation ───────────────────────────────────────────────────

    def _seed_reconcile_fixture(self):
        self.script(exports=[export_entry(700_000, 700_999,
                                          [message(1, 700_100, 'orig1'),
                                           message(2, 700_200, 'keep2'),
                                           message(3, 700_300, 'keep3')])])
        sync.sync(self.archive, self.binary, ACCT, T, 700_000, 700_999)
        # Old backfill outside any future reconcile bounds; it must stay
        # untouched by reconciliation and not move the checkpoint.
        self.script(exports=[export_entry(10_000, 10_999,
                                          [message(8, 10_500, 'ancient8')])])
        sync.sync(self.archive, self.binary, ACCT, T, 10_000, 10_999,
                  advance_checkpoint=False)
        self.assertEqual(self.archive.checkpoint(ACCT, T), 700_999)

    def test_reconcile_reports_missing_without_deleting_or_advancing(self):
        self._seed_reconcile_fixture()
        # days=1 ending now=700_999 → range [614_599, 700_999], two chunks.
        w1, w2 = (614_599, 700_998), (700_999, 700_999)
        self.script(exports=[
            export_entry(*w1, [message(1, 700_100, 'edited1'),
                               message(3, 700_300, 'keep3'),
                               message(9, 700_150, 'new9')]),
            empty_entry(*w2),
        ])
        summary = sync.reconcile(self.archive, self.binary, ACCT, T, days=1,
                                 now=700_999)
        self.assertTrue(summary['reconcile'])
        self.assertEqual(summary['added'], 1)
        self.assertEqual(summary['changed'], 1)
        self.assertEqual(summary['missing'], 1)
        self.assertEqual(summary['checkpoint'], 700_999)  # unchanged by reconcile
        self.assertEqual(self.archive.checkpoint(ACCT, T), 700_999)
        self.record('keep2')  # still present: reconciliation never deletes
        self.assertTrue(self.record('keep2')['missing_in_source'])
        self.assertTrue(self.record('edited1')['has_revisions'])
        self.assertFalse(self.record('new9')['missing_in_source'])
        self.assertFalse(self.record('ancient8')['missing_in_source'])  # out of bounds

    def test_reconcile_gather_failure_is_atomic(self):
        self._seed_reconcile_fixture()
        w1, w2 = (614_599, 700_998), (700_999, 700_999)
        self.script(exports=[
            export_entry(*w1, [message(1, 700_100, 'edited1'),
                               message(9, 700_150, 'new9')]),
            {'talker': T, 'since': w2[0], 'until': w2[1],
             'stderr': 'error: shard unreadable\n', 'rc': 1},
        ])
        with self.assertRaises(ValueError):
            sync.reconcile(self.archive, self.binary, ACCT, T, days=1, now=700_999)
        # Nothing from the successfully gathered first chunk was applied.
        self.assertEqual(self.archive.status()['messages'], 4)
        self.assertFalse(self.record('keep2')['missing_in_source'])
        self.assertFalse(self.record('orig1')['has_revisions'])
        self.assertEqual(self.archive.search('new9', ACCT), [])
        self.assertEqual(self.archive.search('edited1', ACCT), [])
        self.assertEqual(self.archive.checkpoint(ACCT, T), 700_999)

    def test_reconcile_late_import_failure_rolls_back_all_chunks(self):
        self._seed_reconcile_fixture()
        w1, w2 = (614_599, 700_998), (700_999, 700_999)
        original = [self.record(text) for text in
                    ('orig1', 'keep2', 'keep3', 'ancient8')]
        checkpoint = self.archive.checkpoint(ACCT, T)
        invalid_identity = message(10, 700_999, 'late10')
        invalid_identity.pop('server_id')
        missing_asset = media_message(10, 700_999, 'late10', state='available',
                                      reason=None, media_files=['media/not-exported.dat'])
        for invalid_item in (invalid_identity, missing_asset):
            with self.subTest(invalid_item=invalid_item):
                self.script(exports=[
                    export_entry(*w1, [message(1, 700_100, 'edited1'),
                                       message(9, 700_150, 'new9')]),
                    export_entry(*w2, [invalid_item]),
                ])
                # Both exports succeed. The first import adds a message,
                # revises orig1, and marks keep2/keep3 missing; only the late
                # import then fails identity or asset validation.
                with self.assertRaises(ValueError):
                    sync.reconcile(self.archive, self.binary, ACCT, T,
                                   days=1, now=700_999)
                self.assertEqual(
                    [self.record(text) for text in
                     ('orig1', 'keep2', 'keep3', 'ancient8')], original)
                self.assertEqual(self.archive.status()['messages'], 4)
                self.assertEqual(self.archive.search('new9', ACCT), [])
                self.assertEqual(self.archive.search('edited1', ACCT), [])
                self.assertEqual(self.archive.search('late10', ACCT), [])
                self.assertEqual(self.archive.checkpoint(ACCT, T), checkpoint)

    def test_missing_media_archives_all_text_and_advances_live_checkpoint(self):
        items = [message(1, 700_100, 'ordinary'),
                 media_message(2, 700_200, 'missing-image'),
                 media_message(3, 700_300, 'duplicate-image'),
                 media_message(4, 700_400, 'missing-video',
                               content={'Video': {'md5': None}}, reason='missing_reference'),
                 media_message(5, 700_500, 'missing-file',
                               content={'File': {'name': 'gone.pdf'}}, reason='missing_reference'),
                 media_message(6, 700_600, 'missing-voice',
                               content='Voice', reason='missing_reference')]
        self.script(exports=[export_entry(
            700_000, 700_999, items,
            stderr=OPEN_NOTE + 'media unavailable [image]: missing local media\n')])
        summary = sync.sync(self.archive, self.binary, ACCT, T, 700_000, 700_999)
        self.assertEqual((summary['added'], summary['missing_media'],
                          summary['source_missing_media']), (6, 5, 5))
        self.assertEqual(summary['media_status'],
                         {'available': 0, 'missing': 5, 'metadata_only': 0})
        self.assertEqual(summary['checkpoint'], 700_999)
        for item in items[1:]:
            record = self.record(item['snippet'])
            self.assertEqual(record['media_status'], item['media_status'])
            self.assertEqual(record['media_files'], [])
        self.assertEqual(self.archive.status()['missing_media'], 5)

    def test_live_cached_attachment_survives_later_missing_export(self):
        available = media_message(1, 700_100, 'retained-image', state='available',
                                  reason=None, media_files=['media/asset.dat'])
        self.script(exports=[export_entry(
            700_000, 700_999, [available], assets={'media/asset.dat': 'preserved attachment'})])
        sync.sync(self.archive, self.binary, ACCT, T, 700_000, 700_999)
        before = self.record('retained-image')
        missing = media_message(1, 700_100, 'retained-image')
        self.script(exports=[export_entry(700_000, 701_000, [missing])])
        summary = sync.sync(self.archive, self.binary, ACCT, T, 700_000, 701_000)
        after = self.record('retained-image')
        self.assertEqual((summary['changed'], summary['missing_media'],
                          summary['source_missing_media']), (0, 0, 1))
        self.assertEqual(after['media_files'], before['media_files'])
        self.assertEqual(Path(after['media_files'][0]).read_bytes(), b'preserved attachment')
        self.assertEqual(after['media_status'], missing['media_status'])
        self.assertFalse(after['has_revisions'])
        self.assertEqual(self.archive.status()['missing_media'], 0)
        self.assertEqual(summary['checkpoint'], 701_000)

    def test_live_metadata_only_contract_reports_unattempted_media(self):
        item = media_message(1, 700_100, 'metadata-image',
                             state='metadata_only', reason=None)
        self.script(exports=[export_entry(700_000, 700_999, [item],
                                           media_mode='metadata_only')])
        summary = sync.sync(self.archive, self.binary, ACCT, T, 700_000, 700_999,
                            no_media=True)
        self.assertEqual(summary['media_status'],
                         {'available': 0, 'missing': 0, 'metadata_only': 1})
        self.assertEqual(summary['missing_media'], 0)
        self.assertEqual(summary['checkpoint'], 700_999)
        self.assertEqual(self.record('metadata-image')['media_status'],
                         {'state': 'metadata_only'})

    def test_live_source_cannot_silently_drop_requested_media(self):
        item = media_message(1, 700_100, 'unattempted-image',
                             state='metadata_only', reason=None)
        self.script(exports=[export_entry(700_000, 700_999, [item],
                                           media_mode='metadata_only')])
        with self.assertRaises(ValueError):
            sync.sync(self.archive, self.binary, ACCT, T, 700_000, 700_999)
        self.assertIsNone(self.archive.checkpoint(ACCT, T))
        self.assertEqual(self.archive.status()['messages'], 0)

    def test_live_media_errors_malformed_contracts_and_old_binary_stop_checkpoint(self):
        self.script(exports=[export_entry(700_000, 700_099,
                                           [message(1, 700_050, 'seed')])])
        sync.sync(self.archive, self.binary, ACCT, T, 700_000, 700_099)
        before = self.archive.status()
        mutations = [
            lambda d: d.pop('media'),
            lambda d: d['media'].update(version=2),
            lambda d: d['media'].update(errors=1),
            lambda d: d['media'].update(missing=0),
            lambda d: d['media'].update(expected=True),
            lambda d: d['media'].update(mode='metadata_only'),
            lambda d: d['items'][0].pop('media_status'),
            lambda d: d['items'][0]['media_status'].update(state='error',
                                                        reason='source_read_failed'),
        ]
        for mutate in mutations:
            entry = export_entry(700_100, 700_999,
                                  [media_message(2, 700_200, 'must-not-archive')])
            mutate(entry['envelope'])
            self.script(exports=[entry])
            with self.subTest(mutation=mutate):
                with self.assertRaises(ValueError):
                    sync.sync(self.archive, self.binary, ACCT, T, 700_100, 700_999)
                self.assertEqual(self.archive.status(), before)
                self.assertEqual(self.archive.search('must-not-archive', ACCT), [])
        entry = export_entry(700_100, 700_999, [message(2, 700_200, 'old-text-only')])
        entry['envelope'].pop('media')
        self.script(exports=[entry])
        with self.assertRaises(ValueError):
            sync.sync(self.archive, self.binary, ACCT, T, 700_100, 700_999)
        self.assertEqual(self.archive.status(), before)

    def test_expected_absence_io_diagnostics_do_not_block_text_checkpoint(self):
        self.script(exports=[export_entry(
            700_000, 700_999, [media_message(1, 700_100, 'missing-image')],
            stderr=OPEN_NOTE + 'media unavailable [image]: synthetic-reference: '
                   'I/O error: No such file or directory (os error 2)\n'
                   'Exported messages to my error group.json\n')])
        result = sync.sync(self.archive, self.binary, ACCT, T, 700_000, 700_999)
        self.assertEqual(result['missing_media'], 1)
        self.assertEqual(self.archive.checkpoint(ACCT, T), 700_999)
        self.assertEqual(self.record('missing-image')['media_status']['state'], 'missing')

    def test_missing_media_never_softens_unknown_stderr_warnings(self):
        self.script(exports=[export_entry(
            700_000, 700_999, [media_message(1, 700_100, 'missing-image')],
            stderr=OPEN_NOTE + 'warning: media cache permission denied\n')])
        with self.assertRaises(ValueError):
            sync.sync(self.archive, self.binary, ACCT, T, 700_000, 700_999)
        self.assertEqual(self.archive.status()['messages'], 0)
        self.assertIsNone(self.archive.checkpoint(ACCT, T))

    def test_timestamped_colored_error_stops_checkpoint(self):
        self.script(exports=[export_entry(
            700_000, 700_999, [message(1, 700_100, 'not-imported')],
            stderr=OPEN_NOTE + '2026-10-03T00:00:00Z \x1b[31mERROR\x1b[0m '
                   'wx_cli: source failure\n')])
        with self.assertRaises(ValueError):
            sync.sync(self.archive, self.binary, ACCT, T, 700_000, 700_999)
        self.assertIsNone(self.archive.checkpoint(ACCT, T))
        self.assertEqual(self.archive.status()['messages'], 0)

    def test_reconcile_late_media_contract_failure_rolls_back_text_and_missing_flags(self):
        self._seed_reconcile_fixture()
        before = self.archive.status()
        original = [self.record(text) for text in ('orig1', 'keep2', 'keep3', 'ancient8')]
        for failure in ('legacy', 'error', 'counts', 'omitted-status'):
            late = export_entry(700_999, 700_999,
                                 [media_message(10, 700_999, 'late-media')])
            if failure == 'legacy':
                late['envelope'].pop('media')
                late['envelope']['items'][0].pop('media_status')
            elif failure == 'error':
                late['envelope']['media'].update(missing=0, errors=1)
                late['envelope']['items'][0]['media_status'] = {
                    'state': 'error', 'reason': 'source_read_failed'}
            elif failure == 'counts':
                late['envelope']['media']['missing'] = 0
            else:
                late['envelope']['items'][0].pop('media_status')
            self.script(exports=[
                export_entry(614_599, 700_998,
                             [message(1, 700_100, 'edited1'),
                              media_message(9, 700_150, 'new-missing-media')]),
                late,
            ])
            with self.subTest(failure=failure):
                with self.assertRaises(ValueError):
                    sync.reconcile(self.archive, self.binary, ACCT, T, days=1,
                                   now=700_999)
                self.assertEqual(self.archive.status(), before)
                self.assertEqual([self.record(text) for text in
                                  ('orig1', 'keep2', 'keep3', 'ancient8')], original)
                self.assertEqual(self.archive.search('new-missing-media', ACCT), [])
                self.assertEqual(self.archive.search('edited1', ACCT), [])

    # ── locking, logs, validation ────────────────────────────────────────

    def test_sync_lock_reentrant_and_excludes_other_handles(self):
        lock_path = self.archive.root / 'sync.lock'
        with sync._sync_lock(self.archive):
            with sync._sync_lock(self.archive):  # nested: no self-deadlock
                pass
            fd = os.open(lock_path, os.O_RDWR)
            try:
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            finally:
                os.close(fd)
        fd = os.open(lock_path, os.O_RDWR)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)  # free after release
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)
        self.assertEqual(stat.S_IMODE(lock_path.stat().st_mode), 0o600)

    def test_logs_private_and_key_material_redacted(self):
        key = 'a1b2c3d4' * 8
        self.script(exports=[export_entry(
            800_000, 800_999, [message(1, 800_100, 'logged')],
            stderr=OPEN_NOTE + f'debug key={key}\n')])
        sync.sync(self.archive, self.binary, ACCT, T, 800_000, 800_999)
        log = self.archive.root / 'last-sync.log'
        self.assertTrue(log.is_file())
        self.assertEqual(stat.S_IMODE(log.stat().st_mode), 0o600)
        body = log.read_bytes()
        self.assertNotIn(key.encode(), body)
        self.assertIn(b'<redacted>', body)

    def test_invalid_windows_and_arguments(self):
        with self.assertRaises(ValueError):
            sync.sync(self.archive, self.binary, ACCT, T, 900_100, 900_000)
        with self.assertRaises(ValueError):
            sync.sync(self.archive, self.binary, ACCT, T, 900_000,
                      int(time.time()) + 100)
        with self.assertRaises(ValueError):
            sync.sync(self.archive, self.binary, ACCT, T, 900_000, 900_100,
                      chunk_seconds=0)
        with self.assertRaises(ValueError):
            sync.discover(self.archive, self.binary, ACCT, [], 1,
                          initial_since=1)
        with self.assertRaises(TypeError):
            sync.sync(object(), self.binary, ACCT, T, 1, 2)


if __name__ == '__main__':
    unittest.main()
