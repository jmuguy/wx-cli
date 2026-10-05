"""Pinned SQLite VFS — anchors every database path to the held archive root fd.

Why this exists (independent Linux verification, resume2-linux-binding-probe.log,
plus controller's extended-error-code evidence SQLITE_READONLY_DBMOVED=1032):
  1. SQLite's default unix VFS canonicalizes ``/proc/self/fd/<fd>/...`` names
     back to absolute paths (unixFullPathname readlink-normalizes each path
     component), so a procfd-anchored URI does NOT stay pinned.
  2. Scanning /proc/self/fd for ANY descriptor on the pinned inode proves
     nothing about which file the *connection* opened — an unrelated open of
     the original file spoofs it while sqlite holds the replacement.
  3. A write-through probe writes into the wrong (replacement) database before
     detecting it and breaks read-only observation semantics.
  4. unixFile stores the xOpen zName POINTER (no copy — see fillInUnixFile in
     sqlite3.c) and uses it later (hasMoved/unixFileHasMoved → SQLITE_READONLY_
     DBMOVED). A ctypes-converted temporary bytes buffer dangles after the
     delegate call returns, so xOpen must pass ORIGINAL pointers through.

Design (all real I/O, POSIX locking, WAL and shm handling stay in the base
unix VFS — no hand-rolled lock or sync protocol):
  * xFullPathname — OURS, no normalization: whitelisted names expand to
    ``/proc/self/fd/<rootfd>/<name>`` written into SQLite's own zOut buffer
    (double-NUL terminated per the sqlite3_filename contract). This is the
    ONLY place a target path is written.
  * xOpen — validates the name, then passes SQLite's ORIGINAL name pointer
    to the base VFS (the string SQLite hands us is SQLite-owned and stable
    for the file's lifetime; base stores that pointer in unixFile.zPath).
    The only exceptions: xOpen(NULL) temp files (minted as O_TMPFILE
    anonymous inodes inside the held root — never names, because base
    unixOpen's read-only retry branch opens AND DELETEONCLOSE-unlinks a
    colliding pre-existing name; anonymous inodes have no name to race on)
    and defensively bare names (capped instance registry, never a temporary
    buffer). Invalid names → pMethods=NULL + SQLITE_CANTOPEN.
  * xAccess / xDelete — validate and delegate; a transient buffer is safe
    here because these methods do not retain the pointer.
  * every other method pointer is copied from the base VFS.

Consumers verify anchoring at runtime with ``PRAGMA database_list``
(master.py): a vfs= URI that silently failed (e.g. a second copy of
libsqlite3) would degrade to the default VFS and report a normalized path.

Linux-only (requires procfs); registration fails closed anywhere else.
"""
from __future__ import annotations

import ctypes
import os
import secrets
import sqlite3 as _stdlib_sqlite3
import sys

from .errors import VfsError

# Result codes VERBATIM from the public sqlite3.h (libsqlite3-sys-0.37.0,
# struct/define block). These were once written from memory and caused
# SILENT DATA LOSS: 101 is SQLITE_DONE, not SQLITE_NOTFOUND — an xFileControl
# reporting "done" for "unknown op" and a mis-numbered SHORT_READ (real 522)
# collided with a bogus IOERR_READ made every fresh-page read look valid.
# Do not renumber; test_v3 cross-checks NOTFOUND/SHORT_READ against the LIVE
# base VFS behavior.
SQLITE_OK = 0
SQLITE_IOERR = 10
SQLITE_NOTFOUND = 12
SQLITE_CANTOPEN = 14
SQLITE_DONE = 101
SQLITE_IOERR_READ = SQLITE_IOERR | (1 << 8)       # 266
SQLITE_IOERR_SHORT_READ = SQLITE_IOERR | (2 << 8)  # 522
SQLITE_IOERR_WRITE = SQLITE_IOERR | (3 << 8)      # 778
SQLITE_IOERR_FSYNC = SQLITE_IOERR | (4 << 8)      # 1034
SQLITE_IOERR_TRUNCATE = SQLITE_IOERR | (6 << 8)   # 1546
SQLITE_IOERR_FSTAT = SQLITE_IOERR | (7 << 8)      # 1802
SQLITE_IOERR_CLOSE = SQLITE_IOERR | (16 << 8)     # 4106

# ── ctypes prototypes ────────────────────────────────────────────────────
# zName params are c_void_p (raw pointer), NOT c_char_p: c_char_p converts
# to a Python bytes copy and the raw address is lost — but base unixOpen
# stores the zName pointer itself (unixFile.zPath). Raw pass-through keeps
# SQLite's filename metadata/lifetime contract intact.
OPEN_T = ctypes.CFUNCTYPE(
    ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p,
    ctypes.c_void_p, ctypes.c_int, ctypes.POINTER(ctypes.c_int))
DELETE_T = ctypes.CFUNCTYPE(
    ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int)
ACCESS_T = ctypes.CFUNCTYPE(
    ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int,
    ctypes.POINTER(ctypes.c_int))
FULLPATH_T = ctypes.CFUNCTYPE(
    ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int,
    ctypes.c_void_p)


class Sqlite3Vfs(ctypes.Structure):
    """Mirror of the C sqlite3_vfs layout (iVersion 3). Delegate fields are
    c_void_p so raw pointers copied from the base VFS fit without friction."""
    _fields_ = [
        ('iVersion', ctypes.c_int),
        ('szOsFile', ctypes.c_int),
        ('mxPathname', ctypes.c_int),
        ('pNext', ctypes.c_void_p),
        ('zName', ctypes.c_char_p),
        ('pAppData', ctypes.c_void_p),
        ('xOpen', OPEN_T),
        ('xDelete', DELETE_T),
        ('xAccess', ACCESS_T),
        ('xFullPathname', FULLPATH_T),
        ('xDlOpen', ctypes.c_void_p),
        ('xDlError', ctypes.c_void_p),
        ('xDlSym', ctypes.c_void_p),
        ('xDlClose', ctypes.c_void_p),
        ('xRandomness', ctypes.c_void_p),
        ('xSleep', ctypes.c_void_p),
        ('xCurrentTime', ctypes.c_void_p),
        ('xGetLastError', ctypes.c_void_p),
        ('xCurrentTimeInt64', ctypes.c_void_p),
        ('xSetSystemCall', ctypes.c_void_p),
        ('xGetSystemCall', ctypes.c_void_p),
        ('xNextSystemCall', ctypes.c_void_p),
    ]


_DELEGATE_V1 = ['xDlOpen', 'xDlError', 'xDlSym', 'xDlClose', 'xRandomness',
                'xSleep', 'xCurrentTime', 'xGetLastError']
_DELEGATE_V2 = ['xCurrentTimeInt64']
_DELEGATE_V3 = ['xSetSystemCall', 'xGetSystemCall', 'xNextSystemCall']

# Names handed to base xOpen must outlive the file (unixFile.zPath keeps the
# pointer) — that contract is unchanged for the DATABASE files. ANONYMOUS
# temp opens (xOpen with zName=NULL) no longer use names at all: see the
# O_TMPFILE section below.


# ── anonymous temp files via O_TMPFILE ────────────────────────────────────
# Why not named temp files, in any form: every name-based delegation to the
# base VFS can open and unlink a PRE-EXISTING foreign file. Independently
# verified (binding-retry-temp-race.strace): with O_RDWR|O_CREAT|O_EXCL the
# open fails EEXIST on a colliding name, and unixOpen's read-only retry
# branch (sqlite3.c ~46876, condition `errno!=EISDIR && isReadWrite` — EEXIST
# passes straight through) re-opens with O_RDONLY|O_EXCL, SUCCEEDS on the
# foreign file, and DELETEONCLOSE unlinks it. Forcing/stripping
# SQLITE_OPEN_EXCLUSIVE cannot close that branch, and stat-probes are
# check-then-open, not safety. So anonymous temp opens never touch the
# namespace: O_TMPFILE mints an unlinked inode inside the HELD archive root
# dir_fd — there is no name to race on, nothing to collide with, nothing to
# delete. Unsupported O_TMPFILE fails closed (SQLITE_CANTOPEN); there is NO
# /tmp or internal-disk fallback.
#
# The temp file's I/O methods are ours (pread/pwrite/ftruncate/fsync/fstat,
# no-op locking) — exactly the contract the base VFS itself gives temp files
# through unixIoMethodsNoLock (temp/journal/transient files take the no-lock
# methods in fillInUnixFile). Main db/-wal/-shm and ALL database locking
# stay in the base unix VFS; no hand-rolled database locks anywhere.

CLOSE_T = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_void_p)
RW_T = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p,
                        ctypes.c_int, ctypes.c_int64)
TRUNCATE_T = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_int64)
SYNC_T = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_int)
FILESIZE_T = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_void_p,
                              ctypes.POINTER(ctypes.c_int64))
LOCK_T = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_int)
CHECKRES_T = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_void_p,
                              ctypes.POINTER(ctypes.c_int))
FILECONTROL_T = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_int,
                                 ctypes.c_void_p)
SHMMAP_T = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_int,
                            ctypes.c_int, ctypes.c_int,
                            ctypes.POINTER(ctypes.c_void_p))
SHMLOCK_T = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_int,
                             ctypes.c_int, ctypes.c_int)
SHMBARRIER_T = ctypes.CFUNCTYPE(None, ctypes.c_void_p)
SHMUNMAP_T = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_int)
FETCH_T = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_int64,
                           ctypes.c_int, ctypes.POINTER(ctypes.c_void_p))
UNFETCH_T = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_int64,
                             ctypes.c_void_p)


class TempIoMethods(ctypes.Structure):
    """sqlite3_io_methods with iVersion=1. Field ORDER is ABI and mirrors the
    public header exactly (sqlite3.h struct sqlite3_io_methods): xFileControl
    sits BETWEEN xCheckReservedLock and xSectorSize — a swap silently makes
    sqlite call the wrong callback signature and read SQLITE_NOTFOUND as
    device-characteristics bits. The v2/v3 fields are still laid out (C code
    NULL-checks pMethods->xFileControl regardless of iVersion — a truncated
    allocation would make that read past our memory) but stay NULL, and
    sqlite iVersion-gates every xShm*/xFetch call site."""
    _fields_ = [
        ('iVersion', ctypes.c_int),
        ('xClose', CLOSE_T),
        ('xRead', RW_T),
        ('xWrite', RW_T),
        ('xTruncate', TRUNCATE_T),
        ('xSync', SYNC_T),
        ('xFileSize', FILESIZE_T),
        ('xLock', LOCK_T),
        ('xUnlock', LOCK_T),
        ('xCheckReservedLock', CHECKRES_T),
        ('xFileControl', FILECONTROL_T),
        ('xSectorSize', CLOSE_T),
        ('xDeviceCharacteristics', CLOSE_T),
        ('xShmMap', SHMMAP_T),
        ('xShmLock', SHMLOCK_T),
        ('xShmBarrier', SHMBARRIER_T),
        ('xShmUnmap', SHMUNMAP_T),
        ('xFetch', FETCH_T),
        ('xUnfetch', UNFETCH_T),
    ]


class _AnonTemp:
    """One live anonymous temp file (O_TMPFILE fd). The registry entry dies
    with close(), so the population is bounded by currently-OPEN temp files —
    repeated spills on a long-lived connection accumulate nothing."""
    __slots__ = ('fd',)

    def __init__(self, fd):
        self.fd = fd

    def close(self):
        fd, self.fd = self.fd, None
        if fd is not None:
            _TEMP_FILES.pop(id(self), None)
            os.close(fd)

    def __del__(self):
        if getattr(self, 'fd', None) is not None:
            try:
                self.close()
            except Exception:
                pass


class _TempHandle(ctypes.Structure):
    """What gets planted at p_file: pMethods pointer + a py_object backref.
    The registry keeps this struct alive for the file's lifetime (the raw
    memmove into sqlite's file object does not manage Python refcounts)."""
    _fields_ = [('pMethods', ctypes.c_void_p), ('obj', ctypes.py_object)]


_TEMP_FILES = {}    # id(_AnonTemp) → (handle, obj); drained by xClose

_OBJ_OFFSET = ctypes.sizeof(ctypes.c_void_p)
_HANDLE_SIZE = ctypes.sizeof(_TempHandle)


def _temp_from(p_file):
    return ctypes.cast(p_file + _OBJ_OFFSET,
                       ctypes.POINTER(ctypes.py_object)).contents.value


def _temp_close(p_file):
    try:
        _temp_from(p_file).close()
        return SQLITE_OK
    except Exception:
        return SQLITE_IOERR_CLOSE


def _temp_read(p_file, z, amt, ofst):
    try:
        fd = _temp_from(p_file).fd
        done = 0
        while done < amt:
            # A regular-file pread CAN return short without being at EOF;
            # only a 0-byte read proves the real end of file. Treating a
            # merely short transfer as EOF would zero-fill live data —
            # silent corruption — so keep reading until satisfied.
            data = os.pread(fd, amt - done, ofst + done)
            if not data:
                break
            ctypes.memmove(z + done, data, len(data))
            done += len(data)
        if done < amt:
            ctypes.memset(z + done, 0, amt - done)  # zero-fill per unixRead
            return SQLITE_IOERR_SHORT_READ
        return SQLITE_OK
    except Exception:
        return SQLITE_IOERR_READ


def _temp_write(p_file, z, amt, ofst):
    try:
        view = memoryview((ctypes.c_char * amt).from_address(z))
        done = 0
        while done < amt:
            # partial writes are legal; loop until the request is complete.
            # A raised OSError (e.g. ENOSPC) must propagate as an IOERR
            # return code, never be swallowed into success.
            n = os.pwrite(_temp_from(p_file).fd, view[done:], ofst + done)
            if not n:
                raise OSError('pwrite made no progress')
            done += n
        return SQLITE_OK
    except Exception:
        return SQLITE_IOERR_WRITE


def _temp_truncate(p_file, size):
    try:
        os.ftruncate(_temp_from(p_file).fd, size)
        return SQLITE_OK
    except Exception:
        return SQLITE_IOERR_TRUNCATE


def _temp_sync(p_file, flags):
    try:
        os.fsync(_temp_from(p_file).fd)
        return SQLITE_OK
    except Exception:
        return SQLITE_IOERR_FSYNC


def _temp_file_size(p_file, p_size):
    try:
        p_size[0] = os.fstat(_temp_from(p_file).fd).st_size
        return SQLITE_OK
    except Exception:
        return SQLITE_IOERR_FSTAT


def _temp_noop_lock(p_file, level):
    return SQLITE_OK


def _temp_check_reserved(p_file, p_res):
    p_res[0] = 0
    return SQLITE_OK


def _temp_sector_size(p_file):
    return 0


def _temp_device_characteristics(p_file):
    return 0


def _temp_file_control(p_file, op, p_arg):
    return SQLITE_NOTFOUND


_TEMP_METHODS = TempIoMethods()
_TEMP_METHODS.iVersion = 1
_TEMP_METHODS.xClose = CLOSE_T(_temp_close)
_TEMP_METHODS.xRead = RW_T(_temp_read)
_TEMP_METHODS.xWrite = RW_T(_temp_write)
_TEMP_METHODS.xTruncate = TRUNCATE_T(_temp_truncate)
_TEMP_METHODS.xSync = SYNC_T(_temp_sync)
_TEMP_METHODS.xFileSize = FILESIZE_T(_temp_file_size)
_TEMP_METHODS.xLock = LOCK_T(_temp_noop_lock)
_TEMP_METHODS.xUnlock = LOCK_T(_temp_noop_lock)
_TEMP_METHODS.xCheckReservedLock = CHECKRES_T(_temp_check_reserved)
_TEMP_METHODS.xSectorSize = CLOSE_T(_temp_sector_size)
_TEMP_METHODS.xDeviceCharacteristics = CLOSE_T(_temp_device_characteristics)
_TEMP_METHODS.xFileControl = FILECONTROL_T(_temp_file_control)


def open_anonymous_temp(root_fd):
    """O_TMPFILE inside the HELD archive root: an unlinked inode — no name,
    no namespace race, same volume as the archive. Returns an fd or None;
    callers MUST fail closed on None (there is never a /tmp fallback)."""
    o_tmpfile = getattr(os, 'O_TMPFILE', None)
    if o_tmpfile is None:
        return None
    try:
        return os.open('.', o_tmpfile | os.O_RDWR, 0o600, dir_fd=root_fd)
    except OSError:
        return None


def _c_str(ptr) -> bytes:
    return ctypes.string_at(ptr) if ptr else b''


# ── locate the live sqlite3 shared library ───────────────────────────────
def _candidate_paths():
    if sys.platform.startswith('linux'):
        try:
            with open('/proc/self/maps', 'r', encoding='utf-8', errors='replace') as fh:
                for line in fh:
                    parts = line.rstrip('\n').split(None, 5)
                    if len(parts) == 6 and 'libsqlite3' in parts[5]:
                        yield parts[5].strip()
        except OSError:
            pass
    elif sys.platform == 'darwin':
        # enumerate already-loaded images through libSystem; no subprocess
        try:
            libc = ctypes.CDLL('/usr/lib/libSystem.dylib')
            libc._dyld_get_image_count.restype = ctypes.c_uint32
            libc._dyld_get_image_name.restype = ctypes.c_char_p
            libc._dyld_get_image_name.argtypes = [ctypes.c_uint32]
            for idx in range(libc._dyld_get_image_count()):
                name = libc._dyld_get_image_name(idx)
                if name and b'libsqlite3' in name.split(b'/')[-1]:
                    yield name.decode('utf-8', 'replace')
        except OSError:
            pass


def load_live_sqlite3():
    """Return a CDLL for the libsqlite3 the stdlib sqlite3 module actually
    uses. Same SONAME already loaded ⇒ dlopen returns the same handle, so
    sqlite3_vfs_register affects the stdlib connection. A version mismatch
    (a second copy) is rejected — and even a missed mismatch cannot silently
    pass: master.py proves anchoring via PRAGMA database_list after connect."""
    tried = []
    for path in _candidate_paths():
        tried.append(path)
        try:
            lib = ctypes.CDLL(path)
        except OSError:
            continue
        try:
            lib.sqlite3_libversion.restype = ctypes.c_char_p
        except AttributeError:
            continue
        version = (lib.sqlite3_libversion() or b'').decode('ascii', 'replace')
        if version == _stdlib_sqlite3.sqlite_version:
            return lib
    detail = f'tried={tried}; stdlib={_stdlib_sqlite3.sqlite_version}'
    raise VfsError('sqlite3_library_unavailable',
                   'cannot obtain the live libsqlite3 handle', {'detail': detail})


# ── name policy ──────────────────────────────────────────────────────────
class _NamePolicy:
    def __init__(self, root_fd, base_name):
        self.root_fd = int(root_fd)
        self.base = base_name.encode('ascii') if isinstance(base_name, str) \
            else bytes(base_name)
        self.allowed = {
            self.base,
            self.base + b'-wal',
            self.base + b'-shm',
            self.base + b'-journal',
        }
        self.anchor = f'/proc/self/fd/{self.root_fd}'.encode('ascii')
        self.poison = b'__v3pin_rejected__:'

    def fullpath(self, name: bytes) -> bytes:
        """Whitelisted name → anchored absolute path; anything else → poison
        (xOpen/xAccess/xDelete reject poisoned names). This result is only
        ever written into SQLite's own zOut buffer (xFullPathname) — it is
        never handed to base.xOpen as a Python-converted temporary."""
        if name.startswith(self.anchor + b'/'):
            tail = name[len(self.anchor) + 1:]
            if tail in self.allowed or self._is_temp(tail):
                return name
            return self.poison + tail[-96:]
        if name in self.allowed:
            return self.anchor + b'/' + name
        # sqlite temp spill files (<tmpdir>/etilqs_…): anchor the basename so
        # spills stay on the held volume, never TMPDIR/the internal disk
        basename = name.rsplit(b'/', 1)[-1]
        if self._is_temp(basename):
            return self.anchor + b'/' + basename
        return self.poison + name[-96:]

    def is_openable(self, full: bytes) -> bool:
        return full.startswith(self.anchor + b'/') and \
            not full.startswith(self.poison) and \
            b'/' not in full[len(self.anchor) + 1:]

    @staticmethod
    def _is_temp(basename: bytes) -> bool:
        return basename.startswith(b'etilqs_') and b'/' not in basename


# ── pinned VFS instance ──────────────────────────────────────────────────
class PinnedVfs:
    def __init__(self, name, struct, policy, callbacks, lib, base_addr,
                 bare_names):
        self.name = name
        self.struct = struct
        self.policy = policy
        self.anchor_name = policy.anchor.decode('ascii') + '/' \
            + policy.base.decode('ascii')
        self._callbacks = callbacks
        self._lib = lib
        self._base_addr = base_addr
        self._bare_names = bare_names
        self._unregistered = False

    def anchored_main(self):
        """The canonical main-db name sqlite must report via database_list."""
        return self.anchor_name

    def name_buffer_count(self):
        """Live generated temp resources (observability for leak checks).
        Anonymous temps self-drain via xClose, so this is the count of
        currently-OPEN temp files — bounded by live files, independent of
        how many spills or open/close cycles happened."""
        return len(_TEMP_FILES) + len(self._bare_names)

    def unregister(self):
        if self._unregistered:
            return
        self._lib.sqlite3_vfs_unregister(ctypes.addressof(self.struct))
        # Safe ONLY because our lifecycle closes every connection using this
        # VFS BEFORE releasing it (master.py: db.close() → release_pinned_vfs)
        # — no live unixFile.zPath can point into these buffers afterwards.
        # Bounded lifetime instead of a process-global never-freed set. The
        # temp registry is NOT cleared here: it drains through xClose during
        # db.close() and must stay intact for any still-live temp file.
        del self._bare_names[:]
        self._unregistered = True
        # NOTE: struct/callback references stay alive on this object — sqlite
        # must never see freed pointers. After a successful unregister sqlite
        # no longer holds the pointer; on failure the references keep it valid
        # for process lifetime (unique per-instance names).

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.unregister()
        return False


_LIVE = {}          # name → PinnedVfs (keeps C structs/callbacks alive)
_LIVE_LIB = None    # cached shared library handle


def register_pinned_vfs(root_fd, base_name):
    """Register a uniquely-named VFS anchoring `base_name`(+sidecars) to the
    held root fd. Returns a PinnedVfs; call unregister() when done."""
    global _LIVE_LIB
    if not sys.platform.startswith('linux'):
        raise VfsError('vfs_platform_unsupported',
                       'pinned VFS requires Linux procfs; refusing to pretend')
    if _LIVE_LIB is None:
        _LIVE_LIB = load_live_sqlite3()
    lib = _LIVE_LIB
    lib.sqlite3_vfs_find.restype = ctypes.c_void_p
    lib.sqlite3_vfs_find.argtypes = [ctypes.c_char_p]
    lib.sqlite3_vfs_register.restype = ctypes.c_int
    lib.sqlite3_vfs_register.argtypes = [ctypes.c_void_p, ctypes.c_int]
    lib.sqlite3_vfs_unregister.restype = ctypes.c_int
    lib.sqlite3_vfs_unregister.argtypes = [ctypes.c_void_p]

    base_addr = lib.sqlite3_vfs_find(b'unix')
    if not base_addr:
        raise VfsError('base_vfs_missing', 'sqlite3_vfs_find("unix") returned NULL')
    base = Sqlite3Vfs.from_address(base_addr)
    policy = _NamePolicy(root_fd, base_name)
    name = ('v3pin-' + secrets.token_hex(8)).encode('ascii')
    bare_names = []   # defensive bare-whitelist names (capped below)

    def x_fullpath(p_vfs, z_name, n_out, z_out):
        try:
            full = policy.fullpath(_c_str(z_name))
            buf = full[:max(0, int(n_out) - 2)]
            # double-NUL terminated per the sqlite3_filename contract
            ctypes.memmove(z_out, buf + b'\x00\x00', len(buf) + 2)
            return SQLITE_OK
        except Exception:
            return SQLITE_CANTOPEN

    def _validated(z_name):
        """Return the pointer to hand to base for z_name, or None to reject.
        ORIGINAL pointer pass-through whenever the name is already valid —
        never a Python-converted temporary (unixFile.zPath keeps it)."""
        name = _c_str(z_name)
        full = policy.fullpath(name)
        if not policy.is_openable(full):
            return None
        if full == name:
            return z_name                      # already anchored: SQLite's own memory
        # bare whitelisted name reaching xOpen (defensive; sqlite normally
        # passes the xFullPathname output). Bounded: the cap keeps even a
        # pathological caller from growing the registry without limit —
        # overflow fails closed instead.
        if len(bare_names) >= 256:
            return None
        buf = ctypes.create_string_buffer(full + b'\x00')
        bare_names.append(buf)
        return ctypes.addressof(buf)

    def x_open(p_vfs, z_name, p_file, flags, p_out_flags):
        try:
            if not z_name:
                # Anonymous temp file (TEMP db / sort spill). Never named:
                # names are the race surface (see O_TMPFILE section). The
                # inode is minted inside the HELD root — same volume, no
                # namespace entry, nothing to collide with or delete. Any
                # failure fails closed; there is no TMPDIR fallback and base
                # xOpen is never consulted for temp files.
                fd = open_anonymous_temp(policy.root_fd)
                if fd is None:
                    ctypes.cast(p_file, ctypes.POINTER(ctypes.c_void_p))[0] = None
                    return SQLITE_CANTOPEN
                temp = _AnonTemp(fd)
                handle = _TempHandle()
                handle.pMethods = ctypes.addressof(_TEMP_METHODS)
                handle.obj = temp
                _TEMP_FILES[id(temp)] = (handle, temp)
                ctypes.memmove(p_file, ctypes.byref(handle), _HANDLE_SIZE)
                if p_out_flags:
                    p_out_flags[0] = flags
                return SQLITE_OK
            validated = _validated(z_name)
            if validated is None:
                ctypes.cast(p_file, ctypes.POINTER(ctypes.c_void_p))[0] = None
                return SQLITE_CANTOPEN
            return base.xOpen(base_addr, validated, p_file, flags, p_out_flags)
        except Exception:
            ctypes.cast(p_file, ctypes.POINTER(ctypes.c_void_p))[0] = None
            return SQLITE_CANTOPEN

    def x_delete(p_vfs, z_name, sync_dir):
        try:
            name = _c_str(z_name)
            if not name:
                return SQLITE_CANTOPEN
            full = policy.fullpath(name)
            if not policy.is_openable(full):
                return SQLITE_CANTOPEN
            # xDelete does not retain the pointer: a transient buffer is safe
            return base.xDelete(base_addr, full, sync_dir)
        except Exception:
            return SQLITE_CANTOPEN

    def x_access(p_vfs, z_name, flags, p_res_out):
        try:
            name = _c_str(z_name)
            if not name:
                return SQLITE_CANTOPEN
            full = policy.fullpath(name)
            if not policy.is_openable(full):
                return SQLITE_CANTOPEN  # fail closed, never a false negative
            return base.xAccess(base_addr, full, flags, p_res_out)
        except Exception:
            return SQLITE_CANTOPEN

    struct = Sqlite3Vfs()
    struct.iVersion = min(int(base.iVersion), 3)
    # xOpen may plant a _TempHandle into the sqlite3_file sqlite allocates
    # from szOsFile — never smaller than the handle, whatever base reports
    struct.szOsFile = max(int(base.szOsFile), _HANDLE_SIZE)
    struct.mxPathname = max(int(base.mxPathname), 512)
    struct.pNext = None
    struct.zName = name
    struct.pAppData = None
    callbacks = [OPEN_T(x_open), DELETE_T(x_delete),
                 ACCESS_T(x_access), FULLPATH_T(x_fullpath)]
    struct.xOpen, struct.xDelete = callbacks[0], callbacks[1]
    struct.xAccess, struct.xFullPathname = callbacks[2], callbacks[3]
    delegate = list(_DELEGATE_V1)
    if struct.iVersion >= 2:
        delegate += _DELEGATE_V2
    if struct.iVersion >= 3:
        delegate += _DELEGATE_V3
    for field in delegate:
        setattr(struct, field, getattr(base, field))

    rc = lib.sqlite3_vfs_register(ctypes.addressof(struct), 0)
    if rc != SQLITE_OK:
        raise VfsError('vfs_register_failed',
                       f'sqlite3_vfs_register returned {rc}')
    if not lib.sqlite3_vfs_find(name):
        # registered but not findable: take it back out before raising, or
        # sqlite's list would keep a pointer into a soon-GC'd Python object
        lib.sqlite3_vfs_unregister(ctypes.addressof(struct))
        raise VfsError('vfs_register_failed', 'vfs not findable after register')
    pinned = PinnedVfs(name.decode('ascii'), struct, policy, callbacks, lib,
                       base_addr, bare_names)
    _LIVE[pinned.name] = pinned
    return pinned


def release_pinned_vfs(pinned):
    if pinned is None:
        return
    _LIVE.pop(pinned.name, None)
    pinned.unregister()
