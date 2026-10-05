"""v2 → v3 migration tests (consistent snapshot, namespace preservation,
sources/assets/revisions/missing travel, source_upgrade on re-migration)."""
import json
import sqlite3

import pytest

from archive import store as v2_module
from archive.v3.collector import CaptureClient, Collector
from archive.v3.endpoint import Endpoint
from archive.v3.errors import ArchiveError
from archive.v3.guard import SyntheticGuard
from archive.v3.master import MasterStore
from archive.v3.migrate_v2 import migrate_v2
from archive.v3.util import canonical, sha256_hex

ARCHIVE = 'migration-fixture'


class V2Archive:
    """A real-schema v2 archive with hand-inserted rows (the migration only
    reads the v2 side, so direct rows are a faithful fixture)."""

    def __init__(self, root):
        self.root = root
        self.store = v2_module.Archive(root)
        self.n = 0

    def add_server_row(self, account, talker, server_id, text, *, payload=None,
                       media=(), missing=False, revised=True, source=True):
        self.n += 1
        message_id = str(server_id)
        payload_json = canonical(payload or {
            'server_id': server_id, 'talker': talker,
            'content': {'type': 1, 'text': text},
            'sender': 'v2-sender', 'create_time': 1000 + self.n,
            'sort_seq': self.n, 'type': 1})
        digest = sha256_hex(f'v2src-{account}-{server_id}'.encode())
        media_paths = []
        for name, blob in media:
            sha = sha256_hex(blob)
            asset = self.root / 'sources' / 'assets' / sha / 'asset'
            asset.parent.mkdir(parents=True, exist_ok=True)
            if not asset.exists():
                asset.write_bytes(blob)
            media_paths.append(f'sources/assets/{sha}/asset')
        if source:
            src_dir = self.root / 'sources' / digest
            src_dir.mkdir(parents=True, exist_ok=True)
            (src_dir / 'export.json').write_text('{}', encoding='utf-8')
        self.store.db.execute(
            'INSERT INTO messages (account,talker,id_kind,message_id,server_id,'
            'local_id,source_shard,create_time,sort_seq,sender,'
            'sender_display_name,snippet,original_text,payload,source,'
            'media_files,missing_in_source) VALUES '
            '(?,?,?,?,?,NULL,NULL,?,?,?,?,?,?,?,?,?,?)',
            (account, talker, 'server', message_id, server_id,
             1000 + self.n, self.n, 'v2-sender', 'V2 Sender', text[:20], text,
             payload_json, digest, canonical(media_paths),
             1 if missing else 0))
        if revised:
            self.store.db.execute(
                'INSERT INTO revisions VALUES(?,?,?,?,?,?,?,?)',
                (account, talker, 'server', message_id,
                 sha256_hex(payload_json.encode()), payload_json, digest,
                 1700000000 + self.n))
        return message_id

    def add_local_row(self, account, talker, shard, local_id, text, *,
                      missing=False):
        message_id = f'{shard}/{local_id}'
        payload_json = canonical({'local_id': local_id, 'source_shard': shard,
                                  'talker': talker, 'type': 1,
                                  'content': {'type': 1, 'text': text},
                                  'sender': 'v2-sender'})
        digest = sha256_hex(f'v2src-local-{account}-{message_id}'.encode())
        src_dir = self.root / 'sources' / digest
        src_dir.mkdir(parents=True, exist_ok=True)
        (src_dir / 'export.json').write_text('{}', encoding='utf-8')
        self.store.db.execute(
            'INSERT INTO messages (account,talker,id_kind,message_id,server_id,'
            'local_id,source_shard,create_time,sort_seq,sender,'
            'sender_display_name,snippet,original_text,payload,source,'
            'media_files,missing_in_source) VALUES '
            '(:account,:talker,:kind,:message_id,NULL,:local_id,:shard,'
            ':create_time,:sort_seq,:sender,:display,:snippet,:text,:payload,'
            ':source,:media,:missing)',
            {'account': account, 'talker': talker, 'kind': 'local',
             'message_id': message_id, 'local_id': local_id, 'shard': shard,
             'create_time': 2000 + local_id, 'sort_seq': local_id,
             'sender': 'v2-sender', 'display': 'V2 Local',
             'snippet': text[:20], 'text': text, 'payload': payload_json,
             'source': digest, 'media': '[]', 'missing': 1 if missing else 0})
        return message_id

    def close(self):
        self.store.db.close()


class Harness:
    def __init__(self, tmp_path):
        self.tmp = tmp_path
        self.root = tmp_path / 'master'
        self.root.mkdir(mode=0o700)
        SyntheticGuard.write_marker(self.root, ARCHIVE, 1, 0)
        self.binding = SyntheticGuard(ARCHIVE).verify(self.root)
        self.store = MasterStore.initialize(
            self.binding, ARCHIVE,
            {'acct': {'talker': {'capture': True, 'query': True}}})
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


@pytest.fixture
def v2(tmp_path):
    v = V2Archive(tmp_path / 'v2')
    try:
        yield v
    finally:
        v.close()


def test_migration_carries_everything_and_preserves_namespaces(tmp_path,
                                                               harness, v2):
    blob = b'v2-attachment-bytes'
    v2.add_server_row('acct', 'talker', 4242, 'server row body',
                      media=[('att.bin', blob)])
    v2.add_local_row('acct', 'talker', 'message_0', 77, 'local row body')
    v2.add_local_row('acct', 'talker', 'message_0', 78, 'gone from source',
                     missing=True)

    report = migrate_v2(tmp_path / 'v2', harness.collector, tmp_path / 'work')
    assert not report['dry_run']
    assert report['archive_id'] == ARCHIVE
    entry = report['sessions'][0]
    counts = entry['counts']
    assert counts['rows_inserted'] == 3
    assert counts['legacy_rows'] == 3
    assert counts['legacy_revisions'] == 1      # server row's v2 revision
    assert counts['legacy_missing'] == 1        # missing_in_source mark
    # the v2 asset travelled, PLUS the sources tree as run-level objects
    # (the three source views share '{}' bytes → one dedup'd view object)
    assert counts['media_assets_stored'] == 2

    # namespace preservation: local row keeps (source_shard, local_id)
    local = harness.store.db.execute(
        "SELECT * FROM messages WHERE server_id IS NULL").fetchone()
    assert local['shard'] == 'message_0.db'
    assert local['shard_table'] == 'Msg_v2legacy'
    assert local['local_rowid'] == 77
    # server row keyed by server_id; provenance labeled legacy
    server = harness.store.db.execute(
        "SELECT * FROM messages WHERE server_id='4242'").fetchone()
    assert server['provenance'] == 'legacy_visibility_projected'
    assert server['content_text'] == 'server row body'
    # raw keeps the complete verbatim v2 row
    raw = json.loads(server['raw_record_json'])
    assert raw['v2']['message_id'] == '4242'
    assert raw['v2']['payload']['content']['text'] == 'server row body'
    # v2 revision became a legacy_revision; v2 missing became soft mark
    rev = harness.store.db.execute(
        "SELECT change_kind FROM message_revisions WHERE change_kind="
        "'legacy_revision'").fetchall()
    assert len(rev) == 1
    miss = harness.store.db.execute(
        "SELECT kind, soft FROM missing_observations WHERE kind="
        "'legacy_v2_missing'").fetchone()
    assert miss['soft'] == 1
    # the asset physically landed in the v3 objects store
    sha = sha256_hex(blob)
    asset = harness.root / 'objects' / sha[:2] / sha
    assert asset.read_bytes() == blob
    stored = harness.store.db.execute(
        'SELECT kind FROM media_assets WHERE sha256=?', (sha,)).fetchone()
    assert stored is not None


def test_re_migration_restores_fuller_payload(tmp_path, harness, v2):
    """v2 gained a fuller payload after the first migration → re-migration
    upgrades the SAME v3 identity (source_upgrade), never duplicates."""
    v2.add_server_row('acct', 'talker', 4242, 'first body')
    migrate_v2(tmp_path / 'v2', harness.collector, tmp_path / 'work')
    uid = harness.store.db.execute(
        "SELECT msg_uid FROM messages WHERE server_id='4242'").fetchone()['msg_uid']
    v2.store.db.execute(
        "UPDATE messages SET original_text='fuller body', snippet='fuller' "
        "WHERE message_id='4242'")
    v2.store.db.commit()
    report = migrate_v2(tmp_path / 'v2', harness.collector, tmp_path / 'work2')
    counts = report['sessions'][0]['counts']
    assert counts['source_upgrade'] == 1
    row = harness.store.db.execute(
        "SELECT msg_uid, content_text FROM messages WHERE server_id='4242'"
    ).fetchone()
    assert row['content_text'] == 'fuller body'
    assert row['msg_uid'] == uid


def test_re_migration_of_unchanged_rows_is_presence_not_upgrade(tmp_path,
                                                                harness, v2):
    """An UNCHANGED re-observation is a presence bump (no_change) on the
    same identity — never a duplicate row, never a fabricated upgrade."""
    v2.add_server_row('acct', 'talker', 4242, 'stable body')
    migrate_v2(tmp_path / 'v2', harness.collector, tmp_path / 'work')
    first = harness.store.db.execute(
        "SELECT msg_uid, provenance FROM messages WHERE server_id='4242'"
    ).fetchone()
    report = migrate_v2(tmp_path / 'v2', harness.collector, tmp_path / 'work2')
    counts = report['sessions'][0]['counts']
    assert counts['no_change'] == 1
    assert counts['rows_updated'] == 0
    again = harness.store.db.execute(
        "SELECT msg_uid FROM messages WHERE server_id='4242'").fetchone()
    assert again['msg_uid'] == first['msg_uid']   # same identity, no dup
    assert harness.store.db.execute(
        'SELECT COUNT(*) FROM messages').fetchone()[0] == 1


def test_missing_source_view_audited_not_dropped(tmp_path, harness, v2):
    v2.add_server_row('acct', 'talker', 4242, 'body', source=False)
    report = migrate_v2(tmp_path / 'v2', harness.collector, tmp_path / 'work')
    counts = report['sessions'][0]['counts']
    assert counts['legacy_audit_unknown'] == 1
    row = harness.store.db.execute(
        "SELECT raw_record_json FROM messages WHERE server_id='4242'"
    ).fetchone()
    assert json.loads(row['raw_record_json'])['v2'][
        'source_view_present'] is False


def test_corrupt_v2_asset_refuses_migration(tmp_path, harness, v2):
    blob = b'v2-attachment-bytes'
    v2.add_server_row('acct', 'talker', 4242, 'body',
                      media=[('att.bin', blob)])
    sha = sha256_hex(blob)
    asset = tmp_path / 'v2' / 'sources' / 'assets' / sha / 'asset'
    asset.write_bytes(b'tampered-different-bytes')
    with pytest.raises(ArchiveError) as exc:
        migrate_v2(tmp_path / 'v2', harness.collector, tmp_path / 'work')
    assert exc.value.code == 'v2_asset_corrupt'
    assert harness.store.db.execute(
        'SELECT COUNT(*) FROM messages').fetchone()[0] == 0


def test_dry_run_renders_without_touching_v3(tmp_path, harness, v2):
    v2.add_server_row('acct', 'talker', 4242, 'body')
    report = migrate_v2(tmp_path / 'v2', harness.collector, tmp_path / 'work',
                        dry_run=True)
    assert report['dry_run']
    assert report['sessions'][0]['dry_run'] is True
    assert harness.store.db.execute(
        'SELECT COUNT(*) FROM messages').fetchone()[0] == 0
    # the rendered export is a valid contract view
    from pathlib import Path
    export = Path(report['sessions'][0]['export'])
    manifest = json.loads((export / 'export.json').read_text())
    assert manifest['archive_id'] == ARCHIVE
    assert manifest['legacy']['provenance_override'] == \
        'legacy_visibility_projected'
    records = [json.loads(line) for line in
               (export / 'records.jsonl').read_text().splitlines() if line]
    assert len(records) == 1
    assert records[0]['identity']['server_id'] == '4242'


def test_snapshot_is_a_consistent_copy(tmp_path, harness, v2):
    from archive.v3.migrate_v2 import v2_snapshot
    v2.add_server_row('acct', 'talker', 4242, 'body')
    snapshot = v2_snapshot(tmp_path / 'v2', tmp_path / 'work')
    db = sqlite3.connect(f'file:{snapshot}?mode=ro', uri=True)
    try:
        assert db.execute('SELECT COUNT(*) FROM messages').fetchone()[0] == 1
    finally:
        db.close()
    # mutating the live v2 afterwards does not leak into the snapshot
    v2.add_server_row('acct', 'talker', 4243, 'another')
    db = sqlite3.connect(f'file:{snapshot}?mode=ro', uri=True)
    try:
        assert db.execute('SELECT COUNT(*) FROM messages').fetchone()[0] == 1
    finally:
        db.close()
