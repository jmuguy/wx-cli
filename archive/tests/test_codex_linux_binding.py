"""Independent Linux behavior gates for the isolated volume-binding task."""
import hashlib
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from archive.v3.guard import SyntheticGuard
from archive.v3.master import MasterStore
from archive.v3 import master


@unittest.skipUnless(sys.platform.startswith('linux'), 'requires actual Linux VFS')
class LinuxBindingGate(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.root = self.base / 'archive'
        self.root.mkdir(mode=0o700)
        SyntheticGuard.write_marker(self.root, 'binding-fixture', 1, 0)
        store = MasterStore.initialize(self.binding(), 'binding-fixture', {})
        with store.full_transaction() as tx:
            tx.meta_set('sentinel', 'original')
        store.close(); store.binding.close()

    def tearDown(self):
        self.tmp.cleanup()

    def binding(self):
        return SyntheticGuard('binding-fixture').verify(self.root)

    def open(self, readonly=False):
        return MasterStore(self.binding(), 'binding-fixture', readonly=readonly)

    def test_commit_rollback_reopen_and_integrity(self):
        st = self.open()
        try:
            self.assertEqual(st.db.execute('PRAGMA journal_mode').fetchone()[0], 'wal')
            with st.full_transaction() as tx:
                tx.meta_set('committed', 'yes')
            with self.assertRaises(RuntimeError):
                with st.full_transaction() as tx:
                    tx.meta_set('rolled_back', 'no')
                    raise RuntimeError('injected')
        finally:
            st.close(); st.binding.close()
        st = self.open()
        try:
            self.assertEqual(st.meta('committed'), 'yes')
            self.assertIsNone(st.meta('rolled_back'))
            self.assertEqual(st.db.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
        finally:
            st.close(); st.binding.close()

    def test_aba_with_unrelated_fd_cannot_open_replacement(self):
        alt = self.base / 'replacement'
        alt.mkdir(mode=0o700)
        SyntheticGuard.write_marker(alt, 'binding-fixture', 1, 0)
        st = MasterStore.initialize(SyntheticGuard('binding-fixture').verify(alt),
                                    'binding-fixture', {})
        with st.full_transaction() as tx:
            tx.meta_set('sentinel', 'replacement')
        st.close(); st.binding.close()
        before = hashlib.sha256((alt / 'master.db').read_bytes()).digest()
        binding = self.binding()
        connect = master.sqlite3.connect
        extra = []

        def swap(*a, **kw):
            self.root.rename(self.base / 'detached')
            alt.rename(self.root)
            extra.append(os.open(self.base / 'detached/master.db', os.O_RDONLY))
            try:
                return connect(*a, **kw)
            finally:
                self.root.rename(alt)
                (self.base / 'detached').rename(self.root)

        st = None
        try:
            with patch.object(master.sqlite3, 'connect', swap):
                try:
                    st = MasterStore(binding, 'binding-fixture')
                except Exception:
                    pass
            if st:
                self.assertEqual(st.meta('sentinel'), 'original')
        finally:
            if st:
                st.close(); st.binding.close()
            else:
                binding.close()
            for fd in extra:
                os.close(fd)
        self.assertEqual(hashlib.sha256((alt / 'master.db').read_bytes()).digest(), before)

    def test_main_symlink_swap_at_sqlite_open_cannot_write_elsewhere(self):
        outside = self.base / 'outside.db'
        outside.write_bytes((self.root / 'master.db').read_bytes())
        before = hashlib.sha256(outside.read_bytes()).digest()
        binding = self.binding()
        connect = master.sqlite3.connect
        path = self.root / 'master.db'
        saved = self.root / 'saved.db'

        def swap(*a, **kw):
            path.rename(saved)
            path.symlink_to(outside)
            try:
                return connect(*a, **kw)
            finally:
                path.unlink()
                saved.rename(path)

        st = None
        try:
            with patch.object(master.sqlite3, 'connect', swap):
                try:
                    st = MasterStore(binding, 'binding-fixture')
                except Exception:
                    pass
            if st:
                with st.full_transaction() as tx:
                    tx.meta_set('outside_probe', 'must stay in original')
        finally:
            if st:
                st.close(); st.binding.close()
            else:
                binding.close()
        self.assertEqual(hashlib.sha256(outside.read_bytes()).digest(), before)
        self.assertFalse(Path(str(outside) + '-wal').exists())

    def test_sidecar_symlinks_never_modify_external_file(self):
        for suffix in ['-wal', '-shm', '-journal']:
            with self.subTest(suffix=suffix):
                target = self.base / ('external' + suffix)
                payload = b'external-file-must-not-change' * 300
                target.write_bytes(payload)
                side = self.root / ('master.db' + suffix)
                side.unlink(missing_ok=True)
                side.symlink_to(target)
                st = None
                try:
                    try:
                        st = self.open()
                        with st.full_transaction() as tx:
                            tx.meta_set('sidecar_probe', suffix)
                        st.db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
                    except Exception:
                        pass
                finally:
                    if st:
                        st.close(); st.binding.close()
                self.assertEqual(target.read_bytes(), payload)
                side.unlink(missing_ok=True)

    def test_root_loss_rejects_next_transaction(self):
        st = self.open()
        self.root.rename(self.base / 'detached')
        self.root.mkdir()
        try:
            with self.assertRaises(Exception):
                with st.full_transaction() as tx:
                    tx.meta_set('must_not_commit', 'no')
            self.assertEqual(list(self.root.iterdir()), [])
        finally:
            st.close(); st.binding.close()

    def test_repeated_open_close_does_not_leak_fds(self):
        start = len(os.listdir('/proc/self/fd'))
        for _ in range(100):
            st = self.open()
            self.assertEqual(st.meta('sentinel'), 'original')
            st.close(); st.binding.close()
        self.assertLessEqual(len(os.listdir('/proc/self/fd')), start + 2)

    def test_sigkill_uncommitted_write_recovers(self):
        script = '''import os,sys,signal
from archive.v3.guard import SyntheticGuard
from archive.v3.master import MasterStore
s=MasterStore(SyntheticGuard('binding-fixture').verify(sys.argv[1]),'binding-fixture')
with s.full_transaction() as tx:
 tx.meta_set('crash_only','uncommitted')
 os.kill(os.getpid(),signal.SIGKILL)
'''
        r = subprocess.run([sys.executable, '-c', script, str(self.root)], capture_output=True)
        self.assertEqual(r.returncode, -9, r.stderr.decode())
        st = self.open()
        try:
            self.assertIsNone(st.meta('crash_only'))
            self.assertEqual(st.meta('sentinel'), 'original')
            self.assertEqual(st.db.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
        finally:
            st.close(); st.binding.close()

    def test_wal_reader_snapshot_and_checkpoint(self):
        reader = self.open(readonly=True)
        writer = self.open()
        try:
            with reader.read_snapshot():
                self.assertEqual(reader.meta('sentinel'), 'original')
                with writer.full_transaction() as tx:
                    tx.meta_set('sentinel', 'new committed value')
                self.assertEqual(reader.meta('sentinel'), 'original')
                writer.db.execute('PRAGMA busy_timeout=50')
                self.assertEqual(writer.db.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()[0], 1)
            self.assertEqual(reader.meta('sentinel'), 'new committed value')
            self.assertEqual(writer.db.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()[0], 0)
        finally:
            reader.close(); reader.binding.close()
            writer.close(); writer.binding.close()

    def test_temp_spill_does_not_fall_back_outside_archive(self):
        st = self.open()
        before = set(os.listdir('/proc/self/fd'))
        try:
            st.db.execute('PRAGMA temp_store=FILE')
            st.db.execute('PRAGMA temp.cache_size=8')
            st.db.execute('CREATE TEMP TABLE volume_gate(payload BLOB)')
            for _ in range(12):
                st.db.execute('INSERT INTO volume_gate VALUES(zeroblob(1048576))')
            self.assertEqual(st.db.execute('SELECT count(*) FROM volume_gate').fetchone()[0], 12)
            for fd in set(os.listdir('/proc/self/fd')) - before:
                try:
                    target = os.readlink('/proc/self/fd/' + fd)
                except FileNotFoundError:
                    continue
                if target.startswith('/') and ('etilqs' in target or '(deleted)' in target):
                    self.assertTrue(target.startswith(str(self.root) + '/'), target)
            self.assertEqual(st.meta('sentinel'), 'original')
        finally:
            st.close(); st.binding.close()

    def test_guard_revalidation_preserves_sqlite_posix_locks(self):
        st = self.open()
        script = """import os,sys,fcntl,errno
fd=os.open(sys.argv[1],os.O_RDWR)
try:
 try:fcntl.lockf(fd,fcntl.LOCK_EX|fcntl.LOCK_NB,510,0x40000002,os.SEEK_SET)
 except OSError as e:
  assert e.errno in (errno.EACCES,errno.EAGAIN),repr(e)
  print('sqlite_lock_preserved')
 else:raise AssertionError('SQLite main-file shared lock was silently dropped')
finally:os.close(fd)
"""
        try:
            with st.read_snapshot():
                self.assertEqual(st.meta('sentinel'), 'original')
                st.binding.prove_db('master.db')
                r = subprocess.run([sys.executable, '-c', script, str(self.root / 'master.db')],
                                   capture_output=True, text=True, timeout=10)
                self.assertEqual(r.returncode, 0, r.stderr)
                self.assertIn('sqlite_lock_preserved', r.stdout)
        finally:
            st.close(); st.binding.close()

    def test_linux_vfs_registration_failure_never_falls_back(self):
        from archive.v3.errors import VfsError
        binding = self.binding()
        opened = None
        try:
            with patch.object(master, 'register_pinned_vfs',
                              side_effect=VfsError('injected', 'registration failed')):
                with self.assertRaises(Exception):
                    opened = MasterStore(binding, 'binding-fixture')
        finally:
            if opened:
                opened.close()
            binding.close()

    def test_close_with_outstanding_cursor_and_blob_is_safe(self):
        script = """import sys,gc
from archive.v3.guard import SyntheticGuard
from archive.v3.master import MasterStore
s=MasterStore(SyntheticGuard('binding-fixture').verify(sys.argv[1]),'binding-fixture')
s.db.execute('CREATE TABLE lifecycle_gate(id INTEGER PRIMARY KEY,data BLOB)')
s.db.execute('INSERT INTO lifecycle_gate VALUES(1,zeroblob(64))')
s.db.execute('PRAGMA temp_store=FILE')
s.db.execute('PRAGMA cache_size=8')
s.db.execute('CREATE TABLE close_spill(data TEXT)')
with s.full_transaction():
 s.db.executemany('INSERT INTO close_spill VALUES(?)', [('z'*2048,) for _ in range(2048)])
cur=s.db.execute('SELECT data FROM close_spill ORDER BY random()')
cur.fetchone()
blob=s.db.blobopen('lifecycle_gate','data',1)
s.close();s.binding.close();gc.collect()
for resource in (cur,blob):
 try:resource.close()
 except Exception:pass
print('closed_safely')
"""
        r = subprocess.run([sys.executable, '-c', script, str(self.root)],
                           capture_output=True, text=True, timeout=10)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('closed_safely', r.stdout)

    def test_live_connection_repeated_spills_do_not_retain_per_request_memory(self):
        import gc
        import tracemalloc

        st = self.open()
        try:
            st.db.execute('PRAGMA temp_store=FILE')
            st.db.execute('PRAGMA cache_size=8')
            st.db.execute('PRAGMA threads=0')
            st.db.execute('CREATE TABLE spill_lifetime(n INTEGER, payload TEXT)')
            with st.full_transaction():
                st.db.executemany('INSERT INTO spill_lifetime VALUES(?,?)',
                                  [(i, format(i, '08d') + 'z' * 2040) for i in range(2048)])
            tracemalloc.start(8)

            def sort_once():
                count = 0
                for row in st.db.execute('SELECT n,payload FROM spill_lifetime ORDER BY random()'):
                    self.assertEqual(len(row[1]), 2048)
                    count += 1
                self.assertEqual(count, 2048)

            for _ in range(10):
                sort_once()
            gc.collect()
            before = tracemalloc.take_snapshot()
            for _ in range(100):
                sort_once()
            gc.collect()
            after = tracemalloc.take_snapshot()
            retained = sum(item.size_diff for item in after.compare_to(before, 'traceback')
                           if any('/archive/v3/' in frame.filename
                                  for frame in item.traceback))
            self.assertLess(retained, 16384, 'VFS retains allocations after each completed spill')
        finally:
            tracemalloc.stop()
            st.close(); st.binding.close()

    def test_anonymous_temp_inode_stays_on_bound_volume(self):
        import fcntl
        st = self.open()
        try:
            st.db.execute('PRAGMA temp_store=FILE')
            st.db.execute('PRAGMA temp.cache_size=8')
            st.db.execute('CREATE TEMP TABLE anonymous_gate(data BLOB)')
            for _ in range(12):
                st.db.execute('INSERT INTO anonymous_gate VALUES(zeroblob(1048576))')
            anonymous = []
            for name in os.listdir('/proc/self/fd'):
                try:
                    fd = int(name)
                    flags = fcntl.fcntl(fd, fcntl.F_GETFL)
                    info = os.fstat(fd)
                    if flags & os.O_TMPFILE == os.O_TMPFILE:
                        self.assertEqual(info.st_dev, self.root.stat().st_dev)
                        self.assertEqual(info.st_nlink, 0)
                        self.assertTrue(os.readlink('/proc/self/fd/' + name).startswith(str(self.root) + '/'))
                        anonymous.append(fd)
                except OSError:
                    continue
            self.assertTrue(anonymous, 'no anonymous inode backing the live temp table')
            self.assertEqual(st.db.execute('SELECT count(*) FROM anonymous_gate').fetchone()[0], 12)
        finally:
            st.close(); st.binding.close()

    def test_unsupported_anonymous_temp_does_not_fallback(self):
        import errno
        st = self.open()
        calls = []
        original_open = os.open

        def unsupported(path, flags, *args, **kwargs):
            if flags & os.O_TMPFILE == os.O_TMPFILE:
                calls.append(path)
                raise OSError(errno.EOPNOTSUPP, 'injected unsupported anonymous temp')
            return original_open(path, flags, *args, **kwargs)

        try:
            st.db.execute('PRAGMA temp_store=FILE')
            st.db.execute('PRAGMA temp.cache_size=8')
            st.db.execute('CREATE TEMP TABLE unsupported_gate(data BLOB)')
            with patch('os.open', unsupported):
                with self.assertRaises(Exception):
                    for _ in range(12):
                        st.db.execute('INSERT INTO unsupported_gate VALUES(zeroblob(1048576))')
            self.assertTrue(calls, 'O_TMPFILE failure was not exercised')
            self.assertEqual(st.meta('sentinel'), 'original')
            self.assertFalse(list(self.root.glob('etilqs*')))
        finally:
            st.close(); st.binding.close()

    def test_anonymous_temp_handles_partial_reads_and_writes(self):
        import fcntl
        st = self.open()
        calls = {'read': 0, 'write': 0}
        pread, pwrite = os.pread, os.pwrite

        def short_read(fd, count, offset):
            if fcntl.fcntl(fd, fcntl.F_GETFL) & os.O_TMPFILE == os.O_TMPFILE:
                calls['read'] += 1
                count = max(1, count // 3)
            return pread(fd, count, offset)

        def short_write(fd, data, offset):
            if fcntl.fcntl(fd, fcntl.F_GETFL) & os.O_TMPFILE == os.O_TMPFILE:
                calls['write'] += 1
                data = data[:max(1, len(data) // 3)]
            return pwrite(fd, data, offset)

        try:
            st.db.execute('PRAGMA temp_store=FILE')
            st.db.execute('PRAGMA temp.cache_size=8')
            payload = bytes([165]) * 262144
            with patch('os.pread', short_read), patch('os.pwrite', short_write):
                st.db.execute('CREATE TEMP TABLE partial_gate(data BLOB)')
                for _ in range(12):
                    st.db.execute('INSERT INTO partial_gate VALUES(?)', (payload,))
                count = 0
                for row in st.db.execute('SELECT data FROM partial_gate'):
                    self.assertTrue(row[0] == payload, 'partial IO corrupted temp payload')
                    count += 1
                self.assertEqual(count, 12)
            self.assertGreater(calls['read'], 0)
            self.assertGreater(calls['write'], 0)
        finally:
            st.close(); st.binding.close()

    def test_anonymous_temp_disk_full_is_visible_and_master_survives(self):
        import errno
        import fcntl
        st = self.open()
        pwrite = os.pwrite
        faults = []

        def no_space(fd, data, offset):
            if fcntl.fcntl(fd, fcntl.F_GETFL) & os.O_TMPFILE == os.O_TMPFILE:
                faults.append(fd)
                raise OSError(errno.ENOSPC, 'injected full temp volume')
            return pwrite(fd, data, offset)

        try:
            st.db.execute('PRAGMA temp_store=FILE')
            st.db.execute('PRAGMA temp.cache_size=8')
            st.db.execute('CREATE TEMP TABLE full_gate(data BLOB)')
            with patch('os.pwrite', no_space):
                with self.assertRaises(Exception):
                    for _ in range(12):
                        st.db.execute('INSERT INTO full_gate VALUES(zeroblob(1048576))')
            self.assertTrue(faults, 'disk-full injection was not reached')
        finally:
            st.close(); st.binding.close()
        st = self.open()
        try:
            self.assertEqual(st.meta('sentinel'), 'original')
            self.assertEqual(st.db.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
        finally:
            st.close(); st.binding.close()

    def test_writer_lock_excludes_other_process(self):
        st = self.open()
        st.acquire_writer_lock()
        script = '''import sys
from archive.v3.guard import SyntheticGuard
from archive.v3.master import MasterStore
s=MasterStore(SyntheticGuard('binding-fixture').verify(sys.argv[1]),'binding-fixture')
try:
 try:s.acquire_writer_lock(timeout=0.1)
 except Exception as e:
  assert getattr(e,'code',None)=='writer_busy', repr(e)
  print('writer_busy')
 else:raise AssertionError('second writer entered')
finally:s.close()
'''
        try:
            r = subprocess.run([sys.executable, '-c', script, str(self.root)],
                               capture_output=True, text=True, timeout=10)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn('writer_busy', r.stdout)
        finally:
            st.close(); st.binding.close()


if __name__ == '__main__':
    unittest.main(verbosity=2)
