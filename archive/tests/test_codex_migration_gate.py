"""Migrate an archive produced by the real v2 importer, not a hand-written schema."""
import hashlib
import json

from archive.store import Archive
from archive.v3.collector import CaptureClient, Collector
from archive.v3.endpoint import Endpoint
from archive.v3.guard import SyntheticGuard
from archive.v3.master import MasterStore
from archive.v3.util import canonical, sha256_hex


def test_actual_v2_history_sources_assets_missing_and_namespace_survive(tmp_path):
    from archive.v3.migrate_v2 import migrate_v2
    account, talker = 'account', 'synthetic@chatroom'
    legacy_root = tmp_path / 'legacy'
    legacy = Archive(legacy_root)
    exports = tmp_path / 'exports'; exports.mkdir(); (exports / 'media').mkdir()
    def import_version(text, asset):
        (exports / 'media' / 'file').write_bytes(asset)
        item = {'server_id': 101, 'talker': talker, 'create_time': 100, 'sort_seq': 1,
            'sender': 'wxid_synthetic_sender', 'sender_display_name': 'Synthetic Sender',
            'content': {'File': {'name': text}}, 'snippet': text, 'msg_type': 49, 'sub_type': 0,
            'status': 0, 'direction': 'incoming', 'media_status': {'state': 'available'},
            'media_files': ['media/file']}
        local = {'server_id': 0, 'local_id': 17, 'source_shard': 'message_0.db',
            'talker': talker, 'create_time': 100, 'sort_seq': 2, 'sender': 'wxid_synthetic_sender',
            'content': {'Text': 'local synthetic history'}, 'snippet': 'local synthetic history',
            'msg_type': 1, 'status': 0, 'direction': 'incoming'}
        data = {'items': [item, local], 'media': {'version': 1, 'mode': 'enabled', 'expected': 1,
            'available': 1, 'missing': 0, 'errors': 0, 'not_attempted': 0},
            'conversation': {'talker': talker, 'display_name': 'Synthetic Group', 'type': 'group', 'message_count': 2},
            'stats': {'skipped': 0}, 'paging': {'has_more': False, 'offset': 0, 'returned': 2}}
        path = exports / 'export.json'; path.write_text(canonical(data))
        legacy.import_export(path, account, expected_talker=talker, bounds=(0, 200))
    import_version('old synthetic file name', b'old synthetic attachment')
    import_version('new synthetic file name', b'new synthetic attachment')
    legacy.db.execute("UPDATE messages SET missing_in_source=1 WHERE id_kind='local'")
    revision_count = legacy.db.execute('SELECT count(*) FROM revisions').fetchone()[0]
    assert revision_count > 0
    original_files = {str(p.relative_to(legacy_root)): hashlib.sha256(p.read_bytes()).digest()
                      for p in legacy_root.rglob('*') if p.is_file()}
    referenced = {sha256_hex(p.read_bytes()): p.read_bytes() for p in (legacy_root / 'sources').rglob('*') if p.is_file()}
    root = tmp_path / 'target'; root.mkdir(mode=0o700)
    SyntheticGuard.write_marker(root, 'migration-fixture', 1, 0)
    binding = SyntheticGuard('migration-fixture').verify(root)
    store = MasterStore.initialize(binding, 'migration-fixture', {account: {talker: {'capture': True}}})
    ep = Endpoint(binding, 'migration-fixture'); ep.store = store
    ep.auth = {'roles': {'collector': {'token_sha256': sha256_hex(b'synthetic-token')}}}
    class Transport:
        def request(self, frame): return ep.handle_frame(frame)
    collector = Collector(CaptureClient(Transport(), 'migration-fixture', 'synthetic-token'), tmp_path / 'work')
    try:
        migrate_v2(legacy_root, collector, tmp_path / 'migration-work')
        rows = [dict(r) for r in store.db.execute('SELECT * FROM messages')]
        assert len(rows) == 2, 'migration lost history'
        assert {r['provenance'] for r in rows} == {'legacy_visibility_projected'}
        local = next(r for r in rows if r['server_id'] is None)
        assert (local['shard'], local['local_rowid']) == ('message_0.db', 17), 'migration renumbered local namespace'
        assert store.db.execute('SELECT count(*) FROM message_revisions').fetchone()[0] == revision_count
        assert store.db.execute('SELECT count(*) FROM missing_observations WHERE msg_uid=? AND soft=1', (local['msg_uid'],)).fetchone()[0] == 1
        for sha, data in referenced.items():
            path = root / 'objects' / sha[:2] / sha
            assert path.is_file(), 'migration omitted referenced v2 source/asset object'
            assert path.read_bytes() == data
        after = {str(p.relative_to(legacy_root)): hashlib.sha256(p.read_bytes()).digest()
                 for p in legacy_root.rglob('*') if p.is_file()}
        assert after == original_files, 'migration changed original v2 files'
    finally:
        store.close(); binding.close(); legacy.db.close()
