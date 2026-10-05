"""Independent consumer-level durability regressions for the v3 binding."""
import errno
import os
import stat

import pytest

from archive.v3.guard import SyntheticGuard, parse_mountinfo


def test_forced_command_stops_after_volume_guard_failure(tmp_path):
    import io
    from archive.v3.endpoint import Endpoint, serve_endpoint
    from archive.v3.errors import GuardError
    from archive.v3.wire import encode_frame, make_request
    binding = bound_fixture(tmp_path)
    endpoint = Endpoint(binding, 'independent-fixture')
    endpoint.hello_done = True
    endpoint.role = 'collector'
    calls = []
    def fail_first(frame):
        calls.append(frame['id'])
        if len(calls) == 1:
            raise GuardError('identity_changed', 'synthetic volume identity fault')
        return {'alive': True}
    endpoint.op_ping = fail_first
    request = io.BytesIO(encode_frame(make_request(1, 'ping')) + encode_frame(make_request(2, 'ping')))
    response = io.BytesIO()
    try:
        assert serve_endpoint(endpoint, request, response) == 3, 'guard fault did not terminate forced command'
        assert calls == [1], 'endpoint served another operation after guard failure'
        assert len(response.getvalue().splitlines()) == 1
    finally:
        binding.close()


def bound_fixture(root):
    SyntheticGuard.write_marker(root, 'independent-fixture', 1, 0)
    return SyntheticGuard('independent-fixture').verify(root)


def test_atomic_write_propagates_directory_fsync_failure(tmp_path, monkeypatch):
    binding = bound_fixture(tmp_path)
    real_fsync = os.fsync

    def fail_directory(fd):
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError(errno.EIO, 'injected directory persistence failure')
        return real_fsync(fd)

    monkeypatch.setattr(os, 'fsync', fail_directory)
    try:
        with pytest.raises(Exception):
            binding.write_file_atomic('intent.json', b'{"intent":"fixture"}')
    finally:
        binding.close()


def test_atomic_write_handles_short_writes(tmp_path, monkeypatch):
    binding = bound_fixture(tmp_path)
    real_write = os.write

    def short_write(fd, data):
        return real_write(fd, data[:max(1, len(data) // 2)])

    monkeypatch.setattr(os, 'write', short_write)
    payload = b'complete synthetic durable intent payload'
    try:
        binding.write_file_atomic('intent.json', payload)
        assert binding.read_bytes('intent.json') == payload
    finally:
        binding.close()


def test_mountinfo_preserves_escaped_mount_paths():
    entries = parse_mountinfo('42 1 8:1 / /media/External\\040Disk rw - ext4 /dev/sdb1 rw\n')
    assert entries[0].mount_point == '/media/External Disk'


def test_capture_grant_does_not_implicitly_grant_ai_access(tmp_path):
    from archive.v3.master import MasterStore

    binding = bound_fixture(tmp_path)
    store = MasterStore.initialize(binding, 'independent-fixture',
                                   {'fixture-account': {'private-talker': {'capture': True}}})
    try:
        store.check_scope('fixture-account', 'private-talker', 'capture')
        with pytest.raises(Exception):
            store.check_scope('fixture-account', 'private-talker', 'query')
    finally:
        store.close()


@pytest.mark.parametrize('restore_path_before_postcheck', [False, True])
@pytest.mark.parametrize('unrelated_descriptor', [False, True])
def test_master_open_cannot_follow_replaced_archive_path(tmp_path, monkeypatch,
                                                        restore_path_before_postcheck,
                                                        unrelated_descriptor):
    from archive.v3 import master

    root, alternate = tmp_path / 'volume', tmp_path / 'replacement'
    for path, label in [(root, 'original'), (alternate, 'replacement')]:
        path.mkdir(mode=0o700)
        binding = bound_fixture(path)
        store = master.MasterStore.initialize(binding, 'independent-fixture', {})
        with store.full_transaction() as tx:
            tx.meta_set('fixture_identity', label)
        store.close()

    binding = SyntheticGuard('independent-fixture').verify(root)
    connect = master.sqlite3.connect
    swapped = False
    unrelated_fds = []

    def replace_then_open(*args, **kwargs):
        nonlocal swapped
        if not swapped:
            root.rename(tmp_path / 'detached-original')
            alternate.rename(root)
            swapped = True
            if unrelated_descriptor:
                unrelated_fds.append(os.open(tmp_path / 'detached-original' / master.DB_NAME,
                                             os.O_RDONLY))
        connection = connect(*args, **kwargs)
        if restore_path_before_postcheck:
            root.rename(alternate)
            (tmp_path / 'detached-original').rename(root)
        return connection

    monkeypatch.setattr(master.sqlite3, 'connect', replace_then_open)
    store = None
    try:
        try:
            store = master.MasterStore(binding, 'independent-fixture')
        except Exception:
            return  # Failing closed is safe; opening the replacement is not.
        assert store.meta('fixture_identity') == 'original', 'opened replacement volume'
    finally:
        if store is not None:
            store.close()
        else:
            binding.close()
        for fd in unrelated_fds:
            os.close(fd)


def test_corrupt_upload_intent_is_not_silently_reinitialized(tmp_path):
    from archive.v3.endpoint import StagingManager

    binding = bound_fixture(tmp_path)
    staging = StagingManager(binding)
    upload_id = 'fixture-upload-123'
    try:
        staging.begin_upload(upload_id, 'asset', '0' * 64, 0, 'fixture-batch')
        intent = f'staging/uploads/{upload_id}/session.json'
        binding.write_file_atomic(intent, b'corrupt intent')
        with pytest.raises(Exception):
            staging.begin_upload(upload_id, 'asset', '0' * 64, 0, 'fixture-batch')
        assert binding.read_bytes(intent) == b'corrupt intent'
    finally:
        binding.close()


def test_commit_rejects_unverified_client_manifest_hash(tmp_path):
    from archive.v3.endpoint import Endpoint
    from archive.v3.master import MasterStore

    binding = bound_fixture(tmp_path)
    store = MasterStore.initialize(binding, 'independent-fixture',
                                   {'account': {'talker': {'capture': True, 'query': False}}})
    endpoint = Endpoint(binding, 'independent-fixture')
    endpoint.store = store
    try:
        c0 = store.begin_run('run-fixture', 'account', 'talker', 'capture')
        run = {'run_id': 'run-fixture', 'account': 'account', 'talker': 'talker',
               'rows': [], 'c0': c0, 'media': {'uploads': [], 'needs': []}}
        with pytest.raises(Exception):
            endpoint.op_commit_batch({'batch_id': 'fixture-batch', 'run': run,
                                      'content_sha256': '0' * 64, 'media_uploads': []})
        row = store.get_batch('fixture-batch')
        assert row is None or row['state'] != 'committed'
    finally:
        store.close()


def test_client_cannot_forge_c0_to_overwrite_newer_capture(tmp_path):
    from archive.v3.endpoint import Endpoint
    from archive.v3.master import MasterStore
    from archive.v3.util import canonical, sha256_hex

    binding = bound_fixture(tmp_path)
    store = MasterStore.initialize(binding, 'independent-fixture',
                                   {'account': {'talker': {'capture': True}}})
    endpoint = Endpoint(binding, 'independent-fixture')
    endpoint.store = store

    def commit(run_id, c0, body):
        run = {'run_id': run_id, 'account': 'account', 'talker': 'talker',
               'c0': c0, 'media': {'uploads': [], 'needs': []},
               'rows': [{'op': 'upsert',
                         'identity': {'server_id': '123', 'shard': 'message_0.db',
                                      'local_rowid': 1},
                         'record': {'message_content': body,
                                    'record_sha256': sha256_hex(body.encode())}}]}
        return endpoint.op_commit_batch({
            'batch_id': 'batch-' + run_id, 'run': run,
            'content_sha256': sha256_hex(canonical(run).encode()),
            'media_uploads': []})

    try:
        old_c0 = store.begin_run('old', 'account', 'talker', 'capture')
        new_c0 = store.begin_run('new', 'account', 'talker', 'capture')
        commit('new', new_c0, 'newer content')
        assert store.commit_seq() > old_c0
        try:
            commit('old', store.commit_seq() + 100, 'stale content')
        except Exception:
            pass
        value = store.db.execute('SELECT content_text FROM messages').fetchone()[0]
        assert value == 'newer content', 'client-supplied C0 bypassed server capture ordering'
    finally:
        store.close()


@pytest.mark.parametrize('binding_fault', ['missing_run', 'different_talker', 'closed_run'])
def test_commit_requires_matching_open_server_run(tmp_path, binding_fault):
    from archive.v3.endpoint import Endpoint
    from archive.v3.master import MasterStore
    from archive.v3.util import canonical, sha256_hex

    binding = bound_fixture(tmp_path)
    store = MasterStore.initialize(binding, 'independent-fixture',
        {'account': {name: {'capture': True} for name in ('talker', 'other')}})
    endpoint = Endpoint(binding, 'independent-fixture')
    endpoint.store = store
    try:
        c0 = 0
        if binding_fault != 'missing_run':
            c0 = store.begin_run('bound-run', 'account',
                'other' if binding_fault == 'different_talker' else 'talker', 'capture')
        if binding_fault == 'closed_run':
            store.finish_run('bound-run', 'aborted')
        run = {'run_id': 'bound-run', 'account': 'account', 'talker': 'talker',
               'kind': 'capture', 'c0': c0, 'media': {'uploads': [], 'needs': []},
               'rows': [{'op': 'upsert', 'identity': {'server_id': '456',
                        'shard': 'message_0.db', 'local_rowid': 1},
                        'record': {'message_content': 'must not be accepted',
                                   'record_sha256': sha256_hex(b'rejected fixture')}}]}
        with pytest.raises(Exception):
            endpoint.op_commit_batch({'batch_id': 'bound-batch', 'run': run,
                'content_sha256': sha256_hex(canonical(run).encode()), 'media_uploads': []})
        assert store.db.execute('SELECT count(*) FROM messages').fetchone()[0] == 0
    finally:
        store.close()


def test_unchanged_presence_protects_against_older_missing_run(tmp_path):
    from archive.v3.endpoint import Endpoint
    from archive.v3.master import MasterStore
    from archive.v3.util import canonical, sha256_hex, identity_key

    binding = bound_fixture(tmp_path)
    store = MasterStore.initialize(binding, 'independent-fixture',
                                   {'account': {'talker': {'capture': True}}})
    endpoint = Endpoint(binding, 'independent-fixture')
    endpoint.store = store
    ident = {'server_id': '789', 'shard': 'message_0.db', 'local_rowid': 1}
    record = {'message_content': 'unchanged', 'record_sha256': sha256_hex(b'unchanged')}
    def begin(name):
        return store.begin_run(name, 'account', 'talker', 'capture')
    def commit(name, c0, rows, recheck=None):
        run = {'run_id': name, 'account': 'account', 'talker': 'talker',
               'kind': 'capture', 'c0': c0, 'rows': rows,
               'media': {'uploads': [], 'needs': []}}
        if recheck is not None:
            run['recheck'] = recheck
        return endpoint.op_commit_batch({'batch_id': 'batch-' + name, 'run': run,
            'content_sha256': sha256_hex(canonical(run).encode()), 'media_uploads': []})
    upsert = {'op': 'upsert', 'identity': ident, 'record': record}
    try:
        commit('initial', begin('initial'), [upsert])
        old_c0 = begin('old-missing')
        commit('fresh-presence', begin('fresh-presence'), [upsert])
        presence_seq = store.db.execute(
            'SELECT last_content_or_presence_commit_seq FROM messages').fetchone()[0]
        assert presence_seq > old_c0, 'unchanged re-observation failed to advance presence sequence'
        try:
            commit('old-missing', old_c0, [{'op': 'missing_candidate', 'identity': ident}],
                   {'complete': True, 'identities': {}, 'shards': ['message_0.db']})
        except Exception:
            pass
        assert store.db.execute('SELECT count(*) FROM missing_observations').fetchone()[0] == 0
    finally:
        store.close()


def test_manifest_cannot_claim_asset_without_uploaded_bytes(tmp_path):
    from archive.v3.endpoint import Endpoint
    from archive.v3.master import MasterStore
    from archive.v3.util import canonical, sha256_hex
    binding = bound_fixture(tmp_path)
    store = MasterStore.initialize(binding, 'independent-fixture',
                                   {'account': {'talker': {'capture': True}}})
    endpoint = Endpoint(binding, 'independent-fixture')
    endpoint.store = store
    try:
        c0 = store.begin_run('asset-run', 'account', 'talker', 'capture')
        run = {'run_id': 'asset-run', 'account': 'account', 'talker': 'talker',
               'kind': 'capture', 'c0': c0, 'rows': [],
               'media': {'uploads': [{'sha256': sha256_hex(b'absent asset'),
                                     'length': 12, 'kind': 'asset'}], 'needs': []}}
        with pytest.raises(Exception):
            endpoint.op_commit_batch({'batch_id': 'asset-batch', 'run': run,
                'content_sha256': sha256_hex(canonical(run).encode()), 'media_uploads': []})
        assert store.db.execute('SELECT count(*) FROM media_assets').fetchone()[0] == 0
    finally:
        store.close()


def test_unterminated_page_line_is_rejected_before_reading_whole_object(tmp_path, monkeypatch):
    from archive.v3.endpoint import StagingManager, MAX_PAGE_LINE_BYTES
    from archive.v3.util import sha256_hex
    from archive.v3.errors import ProtocolError
    binding = bound_fixture(tmp_path)
    staging = StagingManager(binding)
    payload = b'"' + b'a' * (MAX_PAGE_LINE_BYTES * 2)
    sha = sha256_hex(payload)
    target = tmp_path / 'staging' / 'objects' / sha
    target.parent.mkdir(parents=True, mode=0o700)
    target.write_bytes(payload)
    inode = target.stat().st_ino
    read_bytes = 0
    real_read = os.read
    def counted_read(fd, count):
        nonlocal read_bytes
        data = real_read(fd, count)
        if os.fstat(fd).st_ino == inode:
            read_bytes += len(data)
        return data
    monkeypatch.setattr(os, 'read', counted_read)
    try:
        with pytest.raises(ProtocolError):
            list(staging.iter_page_json(sha))
        assert read_bytes <= MAX_PAGE_LINE_BYTES + 256 * 1024, \
            'oversized unterminated line was accumulated before rejecting'
    finally:
        binding.close()
