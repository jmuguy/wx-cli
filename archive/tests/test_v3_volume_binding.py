"""Implementer-side Linux gates for the volume-binding task.

Independently written (not a copy of test_codex_linux_binding.py); focuses on
things the functional bar requires that exception-swallowing tests cannot
prove alone:
  * temp spill must actually WORK (12 x 1MB through temp_store=FILE), land
    only under the archive root, and leave TMPDIR untouched;
  * temp files are ANONYMOUS (O_TMPFILE in the held root): a foreign file
    planted under an etilqs_* name can no longer be opened or deleted —
    the name-based race surface is gone entirely, and live temp resources
    stay bounded by concurrently-open temp files, not request count;
  * registration failure propagates on Linux (no fallback, synthetic too);
  * ABA swap that stays swapped at postcheck time still cannot serve the
    replacement, and the replacement's bytes are untouched;
  * marker epoch bump kills the binding at the next operation;
  * a cross-device / symlinked intermediate directory is rejected by the
    binding chain;
  * guard revalidation during an ACTIVE write transaction keeps the POSIX
    locks (subprocess must see the lock) and the commit still succeeds.
"""
import gc
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from archive.v3 import master as master_mod
from archive.v3.errors import GuardError, VfsError
from archive.v3.guard import SyntheticGuard
from archive.v3.master import MasterStore


def _ctypes_buffer_count():
    gc.collect()
    return sum(1 for o in gc.get_objects()
               if type(o).__name__.startswith('c_char_Array'))


@unittest.skipUnless(sys.platform.startswith('linux'), 'requires actual Linux VFS')
class VolumeBindingImplGate(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.root = self.base / 'archive'
        self.root.mkdir(mode=0o700)
        SyntheticGuard.write_marker(self.root, 'impl-fixture', 1, 0)
        store = MasterStore.initialize(self.binding(), 'impl-fixture', {})
        with store.full_transaction() as tx:
            tx.meta_set('sentinel', 'original')
        store.close()
        store.binding.close()

    def tearDown(self):
        self.tmp.cleanup()

    def binding(self):
        return SyntheticGuard('impl-fixture').verify(self.root)

    def open(self, readonly=False):
        return MasterStore(self.binding(), 'impl-fixture', readonly=readonly)

    # ── temp spill: FUNCTIONAL, not just fail-closed ──────────────────
    def test_temp_spill_works_and_stays_under_archive_root(self):
        canary = self.base / 'tmp-canary'
        canary.mkdir()
        os.environ['TMPDIR'] = str(canary)   # any TMPDIR leak lands here
        st = self.open()
        try:
            st.db.execute('PRAGMA temp_store=FILE')
            st.db.execute('PRAGMA temp.cache_size=8')
            st.db.execute('CREATE TEMP TABLE spill_gate(payload BLOB)')
            for _ in range(12):
                st.db.execute('INSERT INTO spill_gate VALUES(zeroblob(1048576))')
            # the inserts themselves must succeed — no swallowed CANTOPEN
            rows, total = st.db.execute(
                'SELECT count(*), sum(length(payload)) FROM spill_gate').fetchone()
            self.assertEqual(rows, 12)
            self.assertEqual(total, 12 * 1048576)
            self.assertEqual(st.meta('sentinel'), 'original')
        finally:
            st.close()
            st.binding.close()
        self.assertEqual(os.listdir(canary), [],
                         'TMPDIR canary must stay empty — spill left the archive')
        for entry in os.listdir(self.root):
            self.assertFalse(entry.startswith('etilqs'),
                             f'spill residue leaked into archive root: {entry}')

    def test_temp_name_buffers_are_vfs_instance_scoped(self):
        """Store open/spill/close cycles must not grow the buffer population
        — the name cache is VFS-instance-scoped and released on close."""
        baseline = _ctypes_buffer_count()
        for _ in range(10):
            st = self.open()
            try:
                st.db.execute('PRAGMA temp_store=FILE')
                st.db.execute('PRAGMA temp.cache_size=8')
                st.db.execute('CREATE TEMP TABLE t(payload BLOB)')
                st.db.execute('INSERT INTO t VALUES(zeroblob(1048576))')
            finally:
                st.close()
                st.binding.close()
        self.assertEqual(_ctypes_buffer_count(), baseline,
                         'generated temp-name buffers accumulated across cycles')

    def test_long_lived_connection_repeated_spill_stays_bounded(self):
        """Repeated spills on ONE long-lived connection (the archive endpoint
        scenario) must not accumulate temp resources: sorter temp files go
        through xOpen(NULL) (vdbeSorterOpenTempFile → zName=0), now served as
        anonymous O_TMPFILE inodes that drain via xClose. The live-temp
        population has to be CONSTANT across spills, not merely capped."""
        canary = self.base / 'tmp-canary'
        canary.mkdir()
        os.environ['TMPDIR'] = str(canary)   # any TMPDIR leak lands here
        st = self.open()
        try:
            st.db.execute('PRAGMA temp_store=FILE')
            st.db.execute('PRAGMA cache_size=8')
            counts = set()
            for _ in range(40):
                st.db.execute(
                    'WITH RECURSIVE c(n) AS (SELECT 1 UNION ALL '
                    'SELECT n+1 FROM c WHERE n<4000) '
                    'SELECT count(*) FROM (SELECT randomblob(1024) AS b '
                    'FROM c ORDER BY b)').fetchone()
                counts.add(st.vfs.name_buffer_count())
            self.assertLessEqual(max(counts), 64,
                                 f'temp population exceeded its bound: {counts}')
            self.assertEqual(len(counts), 1,
                             f'live-temp population drifted across spills: '
                             f'{sorted(counts)}')
            self.assertEqual(st.meta('sentinel'), 'original')
        finally:
            st.close()
            st.binding.close()
        self.assertEqual(os.listdir(canary), [],
                         'TMPDIR canary must stay empty — spill left the archive')
        self.assertFalse([e for e in os.listdir(self.root)
                          if e.startswith('etilqs')],
                         'spill residue leaked into the archive root')

    # ── P1 regression: planted same-named file can no longer be touched ──
    def test_planted_collision_cannot_be_opened_or_deleted(self):
        """Regression for the controller's fault injection (binding-retry-
        temp-race.py): the OLD name-pool design let a foreign file planted
        between the absence probe and base xOpen be opened read-only and
        DELETEONCLOSE-unlinked (base unixOpen's read-only retry does not
        exclude EEXIST). Anonymous O_TMPFILE temps have no name, so the
        injection point itself must be GONE: we plant the attacker file,
        watch every os.stat touching an etilqs_* name (the fault script's
        trigger), and require that spills still succeed, no etilqs name is
        ever even looked at, and the planted bytes stay untouched."""
        payload = b'planted file must remain unchanged'
        planted = self.root / 'etilqs_v3slot_0_attacker'
        planted.write_bytes(payload)
        original_stat = os.stat
        seen = []

        def watch_etilqs_stat(path, *args, **kw):
            if isinstance(path, bytes) and path.startswith(b'etilqs_'):
                seen.append(path)
            return original_stat(path, *args, **kw)

        canary = self.base / 'tmp-canary'
        canary.mkdir()
        os.environ['TMPDIR'] = str(canary)
        st = self.open()
        try:
            st.db.execute('PRAGMA temp_store=FILE')
            st.db.execute('PRAGMA temp.cache_size=8')
            st.db.execute('CREATE TEMP TABLE race_probe(a)')
            with patch('os.stat', watch_etilqs_stat):
                for _ in range(12):
                    st.db.execute(
                        'INSERT INTO race_probe VALUES(zeroblob(1048576))')
            rows, total = st.db.execute(
                'SELECT count(*), sum(length(a)) FROM race_probe').fetchone()
            self.assertEqual((rows, total), (12, 12 * 1048576))
        finally:
            st.close()
            st.binding.close()
        self.assertEqual(
            seen, [],
            'temp open touched an etilqs NAME — the race surface is back')
        self.assertTrue(planted.exists(), 'planted file was deleted')
        self.assertEqual(planted.read_bytes(), payload,
                         'planted file was modified')
        self.assertEqual(os.listdir(canary), [], 'TMPDIR was used')

    # ── registration failure propagates (Linux never falls back) ──────
    def test_registration_failure_propagates_synthetic_too(self):
        binding = self.binding()
        opened = None
        try:
            with patch.object(master_mod, 'register_pinned_vfs',
                              side_effect=VfsError('injected', 'boom')):
                with self.assertRaises(VfsError):
                    opened = MasterStore(binding, 'impl-fixture')
                self.assertIsNone(opened)
                self.assertTrue(binding.root_fd >= 0)
        finally:
            if opened:
                opened.close()
            binding.close()

    # ── ABA with the swap still in place at postcheck ─────────────────
    def test_aba_swap_left_in_place_cannot_serve_replacement(self):
        alt = self.base / 'replacement'
        alt.mkdir(mode=0o700)
        SyntheticGuard.write_marker(alt, 'impl-fixture', 1, 0)
        st = MasterStore.initialize(SyntheticGuard('impl-fixture').verify(alt),
                                    'impl-fixture', {})
        with st.full_transaction() as tx:
            tx.meta_set('sentinel', 'replacement')
        st.close()
        st.binding.close()
        before = hashlib.sha256((alt / 'master.db').read_bytes()).digest()

        binding = self.binding()   # verified against the ORIGINAL root dir
        self.root.rename(self.base / 'detached')   # original dir moves away
        alt.rename(self.root)                      # replacement takes the path
        try:
            # the held fd follows the detached original; check_alive compares
            # the PATH's inode against it → root_rebound, fail closed
            with self.assertRaises(GuardError):
                MasterStore(binding, 'impl-fixture')
        finally:
            self.root.rename(alt)
            (self.base / 'detached').rename(self.root)
            binding.close()
        self.assertEqual(hashlib.sha256((alt / 'master.db').read_bytes()).digest(),
                         before, 'replacement bytes were modified')

    # ── epoch bump kills the binding ──────────────────────────────────
    def test_epoch_bump_rejects_next_operation(self):
        st = self.open()   # binding verified under the CURRENT epoch
        marker = self.root / '.v3_archive_identity.json'
        data = json.loads(marker.read_text())
        data['epoch'] = int(data['epoch']) + 1
        from archive.v3.guard import _marker_fingerprint
        data['fingerprint'] = _marker_fingerprint(data)
        marker.write_text(json.dumps(data, sort_keys=True,
                                     separators=(',', ':')) + '\n')
        try:
            with self.assertRaises(GuardError):
                with st.full_transaction() as tx:
                    tx.meta_set('post_bump', 'no')
        finally:
            st.close()
            st.binding.close()

    # ── symlinked intermediate directory rejected by the chain ────────
    def test_symlinked_intermediate_dir_rejected(self):
        outside = self.base / 'outside-dir'
        outside.mkdir()
        (outside / 'x.txt').write_text('x')
        (self.root / 'linkdir').symlink_to(outside)
        (self.root / 'plainfile').write_text('not a directory')
        binding = self.binding()
        try:
            # a symlink component: Linux gives ENOTDIR for O_NOFOLLOW|
            # O_DIRECTORY here — the classification must still say 'symlink'
            with self.assertRaises(GuardError) as cm:
                binding.open_file_read('linkdir/x.txt')
            self.assertEqual(cm.exception.code, 'path_symlink_rejected')
            # a plain regular file mid-path is NOT a symlink: the generic
            # open-failure code must survive (no blanket ENOTDIR remapping)
            with self.assertRaises(GuardError) as cm:
                binding.open_file_read('plainfile/x.txt')
            self.assertEqual(cm.exception.code, 'path_open_failed')
        finally:
            binding.close()

    # ── guard revalidation keeps locks during an ACTIVE write tx ──────
    def test_revalidation_preserves_locks_during_active_write_tx(self):
        st = self.open()
        script = """import errno,fcntl,os,sys
fd=os.open(sys.argv[1],os.O_RDWR)
try:
 try:fcntl.lockf(fd,fcntl.LOCK_EX|fcntl.LOCK_NB,510,0x40000002,os.SEEK_SET)
 except OSError as e:
  assert e.errno in (errno.EACCES,errno.EAGAIN),repr(e)
  print('lock_preserved')
 else:raise AssertionError('write-tx lock was dropped')
finally:os.close(fd)
"""
        try:
            with st.full_transaction() as tx:
                tx.meta_set('during_tx', 'value')
                # revalidation mid-transaction must not drop sqlite's locks
                st.binding.check_alive()
                st.binding.prove_db('master.db')
                r = subprocess.run(
                    [sys.executable, '-c', script, str(self.root / 'master.db')],
                    capture_output=True, text=True, timeout=10)
                self.assertEqual(r.returncode, 0, r.stderr)
                self.assertIn('lock_preserved', r.stdout)
            # and the commit itself must have succeeded after all of that
            self.assertEqual(st.meta('during_tx'), 'value')
        finally:
            if st.db is not None:
                st.close()
            if st.binding and not st.binding.closed:
                st.binding.close()


@unittest.skipUnless(sys.platform.startswith('linux'), 'requires O_TMPFILE on Linux')
class AnonymousTempSemantics(unittest.TestCase):
    """Unit proof that temp files are truly anonymous, independent of
    sqlite: minted INSIDE the held root, absent from the namespace
    ('(deleted)' fd target, no directory entry), fully functional through
    OUR io_methods (write/read/size/truncate/short-read/close), and drained
    from the registry on close."""

    def test_anonymous_temp_is_unlinked_and_functional(self):
        import ctypes as ct
        from archive.v3.vfs import (
            _AnonTemp, _HANDLE_SIZE, _TEMP_FILES, _TempHandle,
            SQLITE_IOERR_SHORT_READ, SQLITE_OK, _temp_close, _temp_file_size,
            _temp_read, _temp_truncate, _temp_write, open_anonymous_temp)
        with tempfile.TemporaryDirectory() as td:
            dir_fd = os.open(td, os.O_RDONLY | os.O_DIRECTORY)
            try:
                tfd = open_anonymous_temp(dir_fd)
                self.assertIsNotNone(tfd, 'O_TMPFILE failed on a real Linux '
                                          'dir — must fail closed, never /tmp')
                link = os.readlink(f'/proc/self/fd/{tfd}')
                self.assertIn('(deleted)', link)
                self.assertTrue(link.startswith(td), link)
                self.assertEqual(os.listdir(td), [],
                                 'anonymous temp created a directory entry')
                # plant the handle exactly like xOpen does, then drive the
                # private io_methods by address — no sqlite involved
                temp = _AnonTemp(tfd)
                handle = _TempHandle()
                handle.pMethods = None      # never dereferenced by callbacks
                handle.obj = temp
                _TEMP_FILES[id(temp)] = (handle, temp)
                buf = ct.create_string_buffer(256)     # ≥ szOsFile room
                p_file = ct.addressof(buf)
                ct.memmove(p_file, ct.byref(handle), _HANDLE_SIZE)

                payload = b'anonymous-io-roundtrip\x00' * 40
                src = ct.create_string_buffer(payload, len(payload))
                self.assertEqual(
                    _temp_write(p_file, ct.addressof(src), len(payload), 0),
                    SQLITE_OK)
                size = ct.c_int64(-1)
                self.assertEqual(
                    _temp_file_size(p_file, ct.pointer(size)), SQLITE_OK)
                self.assertEqual(size.value, len(payload))
                dst = ct.create_string_buffer(len(payload))
                self.assertEqual(
                    _temp_read(p_file, ct.addressof(dst), len(payload), 0),
                    SQLITE_OK)
                self.assertEqual(dst.raw[:len(payload)], payload)
                # short read past EOF: zero-filled tail + SHORT_READ rc
                far = ct.create_string_buffer(len(payload) + 16)
                self.assertEqual(
                    _temp_read(p_file, ct.addressof(far), len(payload) + 16,
                               len(payload)),
                    SQLITE_IOERR_SHORT_READ)
                self.assertEqual(far.raw, b'\x00' * (len(payload) + 16))
                # truncate + size again
                self.assertEqual(_temp_truncate(p_file, 8), SQLITE_OK)
                self.assertEqual(
                    _temp_file_size(p_file, ct.pointer(size)), SQLITE_OK)
                self.assertEqual(size.value, 8)
                # close drains the registry and the descriptor
                self.assertEqual(_temp_close(p_file), SQLITE_OK)
                self.assertNotIn(id(temp), _TEMP_FILES)
                with self.assertRaises(OSError):
                    os.fstat(tfd)
            finally:
                os.close(dir_fd)

    def test_open_anonymous_temp_fails_closed_on_bad_dir(self):
        from archive.v3.vfs import open_anonymous_temp
        readfd = os.open(__file__, os.O_RDONLY)   # NOT a directory
        try:
            self.assertIsNone(open_anonymous_temp(readfd))
        finally:
            os.close(readfd)

    def test_io_methods_abi_matches_public_header(self):
        """The 2026-10-04 controller audit caught xFileControl placed AFTER
        xSectorSize/xDeviceCharacteristics — sqlite would call the wrong
        callback signature and read SQLITE_NOTFOUND=101 as device bits.
        Field order and size are ABI: mirror sqlite3.h exactly (int + pad +
        18 pointers on 64-bit)."""
        import ctypes as ct
        from archive.v3 import vfs
        expected = ['iVersion', 'xClose', 'xRead', 'xWrite', 'xTruncate',
                    'xSync', 'xFileSize', 'xLock', 'xUnlock',
                    'xCheckReservedLock', 'xFileControl', 'xSectorSize',
                    'xDeviceCharacteristics', 'xShmMap', 'xShmLock',
                    'xShmBarrier', 'xShmUnmap', 'xFetch', 'xUnfetch']
        self.assertEqual([f[0] for f in vfs.TempIoMethods._fields_], expected)
        self.assertEqual(
            ct.sizeof(vfs.TempIoMethods), ct.sizeof(ct.c_void_p) * 19,
            'v1..v3 io_methods must be int+pad+18 pointers on 64-bit')
        # the singleton actually wired up: right methods, NOTFOUND file ctl
        self.assertEqual(vfs._TEMP_METHODS.iVersion, 1)
        self.assertEqual(vfs._TEMP_METHODS.xFileControl(0, 999, None),
                         vfs.SQLITE_NOTFOUND)
        self.assertEqual(vfs._TEMP_METHODS.xDeviceCharacteristics(0), 0)
        # ctypes surfaces a NULL callback slot as an opaque callable, not
        # None — read the raw slot through a void* cast instead
        def _slot(obj, fieldname):
            off = getattr(type(obj), fieldname).offset
            return ct.cast(ct.byref(obj, off),
                           ct.POINTER(ct.c_void_p)).contents.value
        self.assertIsNone(_slot(vfs._TEMP_METHODS, 'xShmMap'))
        self.assertIsNone(_slot(vfs._TEMP_METHODS, 'xFetch'))
        self.assertIsNotNone(_slot(vfs._TEMP_METHODS, 'xFileControl'))

    def test_result_codes_match_public_header(self):
        """The constants are ABI. 2026-10-04 incident: they were written from
        memory (NOTFOUND=101 is actually SQLITE_DONE; every IOERR op number
        was off) and temp tables silently lost rows. Values below are
        verbatim from sqlite3.h's define block; the LIVE-library behavioral
        check lives in test_result_codes_match_live_library_behavior."""
        from archive.v3 import vfs
        self.assertEqual(vfs.SQLITE_NOTFOUND, 12)
        self.assertEqual(vfs.SQLITE_DONE, 101)
        self.assertNotEqual(vfs.SQLITE_NOTFOUND, vfs.SQLITE_DONE)
        self.assertEqual(
            (vfs.SQLITE_IOERR_READ, vfs.SQLITE_IOERR_SHORT_READ,
             vfs.SQLITE_IOERR_WRITE, vfs.SQLITE_IOERR_FSYNC,
             vfs.SQLITE_IOERR_TRUNCATE, vfs.SQLITE_IOERR_FSTAT,
             vfs.SQLITE_IOERR_CLOSE),
            (266, 522, 778, 1034, 1546, 1802, 4106))

    @unittest.skipUnless(sys.platform.startswith('linux'), 'needs base unix VFS')
    def test_result_codes_match_live_library_behavior(self):
        """Machine-authoritative: drive the LIVE base unix VFS directly and
        require its behavior to match our constants — an unknown xFileControl
        op returns exactly SQLITE_NOTFOUND, a read past EOF returns exactly
        SQLITE_IOERR_SHORT_READ with a zero-filled buffer."""
        import ctypes as ct
        import tempfile
        from archive.v3 import vfs
        lib = vfs.load_live_sqlite3()
        lib.sqlite3_vfs_find.restype = ct.c_void_p
        lib.sqlite3_vfs_find.argtypes = [ct.c_char_p]
        base_addr = lib.sqlite3_vfs_find(b'unix-excl') \
            or lib.sqlite3_vfs_find(b'unix')
        self.assertTrue(base_addr)
        base = vfs.Sqlite3Vfs.from_address(base_addr)
        with tempfile.TemporaryDirectory() as td:
            # unixFile keeps the zName POINTER — the buffer must outlive use
            name_buf = ct.create_string_buffer(
                os.path.join(td, 'probe.db').encode('ascii') + b'\x00')
            fbuf = ct.create_string_buffer(max(int(base.szOsFile), 256))
            p_file = ct.addressof(fbuf)
            out_flags = ct.c_int(0)
            rc = base.xOpen(base_addr, ct.addressof(name_buf), p_file,
                            0x00000100 | 0x02 | 0x04,   # MAIN_DB|RW|CREATE
                            ct.pointer(out_flags))
            self.assertEqual(rc, vfs.SQLITE_OK, 'base xOpen failed')
            try:
                pmethods = ct.cast(p_file, ct.POINTER(ct.c_void_p))[0]
                self.assertTrue(pmethods)
                # TempIoMethods mirrors sqlite3_io_methods, so it can read
                # the BASE vtable laid out by the live library
                iom = vfs.TempIoMethods.from_address(pmethods)
                self.assertEqual(
                    iom.xFileControl(p_file, 999999, None),
                    vfs.SQLITE_NOTFOUND,
                    'base VFS unknown-op code != our SQLITE_NOTFOUND')
                dst = ct.create_string_buffer(16)
                self.assertEqual(
                    iom.xRead(p_file, ct.addressof(dst), 16, 1 << 20),
                    vfs.SQLITE_IOERR_SHORT_READ,
                    'base VFS past-EOF code != our SQLITE_IOERR_SHORT_READ')
                self.assertEqual(dst.raw, b'\x00' * 16,
                                 'base did not zero-fill the short read')
            finally:
                iom.xClose(p_file)

    def _planted_handle(self, dir_fd):
        """Mint an anonymous temp in dir_fd and return (p_file, temp, buf).
        `buf` MUST stay referenced by the caller for as long as p_file is
        used — it IS the sqlite3_file whose address we hand around."""
        import ctypes as ct
        from archive.v3 import vfs
        tfd = vfs.open_anonymous_temp(dir_fd)
        self.assertIsNotNone(tfd)
        temp = vfs._AnonTemp(tfd)
        handle = vfs._TempHandle()
        handle.pMethods = None
        handle.obj = temp
        vfs._TEMP_FILES[id(temp)] = (handle, temp)
        buf = ct.create_string_buffer(256)
        p_file = ct.addressof(buf)
        ct.memmove(p_file, ct.byref(handle), vfs._HANDLE_SIZE)
        return p_file, temp, buf

    def test_partial_reads_and_writes_loop_until_complete(self):
        """os.pread/os.pwrite may legally transfer less than requested on a
        regular file; a single-shot call would misread live data as EOF and
        zero-fill it. Shrink every transfer to 1/3 (like Codex's injected
        wrappers) and require byte-exact round-trips through OUR methods."""
        import ctypes as ct
        import fcntl
        from archive.v3 import vfs
        pread, pwrite = os.pread, os.pwrite

        def short_read(fd, count, offset):
            if fcntl.fcntl(fd, fcntl.F_GETFL) & os.O_TMPFILE == os.O_TMPFILE:
                count = max(1, count // 3)
            return pread(fd, count, offset)

        def short_write(fd, data, offset):
            if fcntl.fcntl(fd, fcntl.F_GETFL) & os.O_TMPFILE == os.O_TMPFILE:
                data = data[:max(1, len(data) // 3)]
            return pwrite(fd, data, offset)

        with tempfile.TemporaryDirectory() as td:
            dir_fd = os.open(td, os.O_RDONLY | os.O_DIRECTORY)
            try:
                p_file, temp, _keep = self._planted_handle(dir_fd)
                try:
                    payload = bytes([165]) * 4096
                    src = ct.create_string_buffer(payload, len(payload))
                    dst = ct.create_string_buffer(len(payload))
                    with patch('os.pread', short_read), \
                            patch('os.pwrite', short_write):
                        rc_w = vfs._temp_write(p_file, ct.addressof(src),
                                               len(payload), 0)
                        rc_r = vfs._temp_read(p_file, ct.addressof(dst),
                                              len(payload), 0)
                    self.assertEqual(rc_w, vfs.SQLITE_OK)
                    self.assertEqual(rc_r, vfs.SQLITE_OK)
                    self.assertEqual(dst.raw[:len(payload)], payload,
                                     'partial IO corrupted the payload')
                    size = ct.c_int64(-1)
                    self.assertEqual(
                        vfs._temp_file_size(p_file, ct.pointer(size)),
                        vfs.SQLITE_OK)
                    self.assertEqual(size.value, len(payload))
                finally:
                    temp.close()
            finally:
                os.close(dir_fd)

    def test_disk_full_write_propagates_not_success(self):
        """An ENOSPC raised by pwrite must surface as SQLITE_IOERR_WRITE —
        never swallowed into SQLITE_OK — and leave prior bytes intact."""
        import ctypes as ct
        import errno
        from archive.v3 import vfs
        pwrite = os.pwrite
        victim_fd = []

        def no_space(fd, data, offset):
            if fd in victim_fd:
                raise OSError(errno.ENOSPC, 'injected full temp volume')
            return pwrite(fd, data, offset)

        with tempfile.TemporaryDirectory() as td:
            dir_fd = os.open(td, os.O_RDONLY | os.O_DIRECTORY)
            try:
                p_file, temp, _keep = self._planted_handle(dir_fd)
                victim_fd.append(temp.fd)
                try:
                    payload = b'first-write-stays' * 32
                    src = ct.create_string_buffer(payload, len(payload))
                    self.assertEqual(
                        vfs._temp_write(p_file, ct.addressof(src),
                                        len(payload), 0), vfs.SQLITE_OK)
                    with patch('os.pwrite', no_space):
                        self.assertEqual(
                            vfs._temp_write(p_file, ct.addressof(src),
                                            len(payload), 4096),
                            vfs.SQLITE_IOERR_WRITE,
                            'ENOSPC was swallowed into success')
                    size = ct.c_int64(-1)
                    self.assertEqual(
                        vfs._temp_file_size(p_file, ct.pointer(size)),
                        vfs.SQLITE_OK)
                    self.assertEqual(size.value, len(payload),
                                     'failed write partially extended the file')
                finally:
                    temp.close()
            finally:
                os.close(dir_fd)


if __name__ == '__main__':
    unittest.main(verbosity=2)
