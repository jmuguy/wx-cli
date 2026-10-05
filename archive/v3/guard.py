"""External disk guard — production tier (fail closed) and synthetic tier.

Production tier proves volume identity from real Linux mount topology and
RE-PROVES it continuously: `/proc/self/mountinfo` (mount point, device
major:minor, fs type, in-device root), block-device resolution via
`/dev/block/<maj>:<min>` and UUID via `/dev/disk/by-uuid` (the unlock
mapping: an expected UUID that is not currently unlocked/mounted here simply
fails), capacity floor, and the volume identity marker. Every configured
expectation is enforced — UUID and device path are both mandatory, optional
fields are enforced whenever present; anything unknown is rejected. On
platforms without mountinfo (macOS) the production tier reports
`platform_unverified` and denies — it never pretends to pass.

Path binding (TOCTOU defence): the archive root is opened once and the
directory fd held; every subpath is resolved component-by-component via
openat with O_NOFOLLOW against that fd, so a swapped symlink or re-mount is
rejected, never redirected. SQLite binding is proven the same way:
the path→inode proof is re-run before EVERY transaction using stat-only
resolution — opening the db leaf file is FORBIDDEN, because POSIX fcntl
locks are process-level and close(2) of any descriptor on the inode releases
SQLite's locks. The connection opens read-write-no-create; on Linux it goes
through a pinned VFS anchored to the held root fd. There is no fallback path
and no window where an unproven file is transacted against.

The marker file (`.v3_archive_identity.json`) is mandatory for BOTH tiers and
records tier/archive_id/epoch (+uuid for production, +st_dev for synthetic),
integrity-fingerprinted; it is written only by the explicit init command,
never auto-created, never overwritten. Rewriting it (e.g. epoch bump) kills
every binding that verified the old marker.

The synthetic tier (`--allow-synthetic-guard`) is an explicit TEST adapter:
st_dev + marker binding, capacity floor, cross-device rejection, no
auto-init. It is NOT a production guard and never claims to be.
"""
from __future__ import annotations

import errno
import hashlib
import json
import os
import stat
import sys
from pathlib import Path

from .errors import GuardError
from .util import canonical as _canonical, unique_temp, write_all

PROC_MOUNTINFO = '/proc/self/mountinfo'
BLOCK_ROOT = '/dev/block'
BY_UUID_ROOT = '/dev/disk/by-uuid'
MARKER_NAME = '.v3_archive_identity.json'


# ══════════════════════════════════════════════════════════════════
# mountinfo parsing (fixture-testable; field split BEFORE unescape)
# ══════════════════════════════════════════════════════════════════

def _unescape_field(text):
    """mountinfo escapes space/tab/newline as \\040 \\011 \\012 inside fields;
    the raw line is therefore safely space-split FIRST, fields unescaped after."""
    out, i = [], 0
    while i < len(text):
        if text[i] == '\\' and i + 3 < len(text):
            try:
                out.append(chr(int(text[i + 1:i + 4], 8)))
                i += 4
                continue
            except ValueError:
                pass
        out.append(text[i])
        i += 1
    return ''.join(out)


class MountEntry:
    __slots__ = ('mount_id', 'parent_id', 'major', 'minor', 'root', 'mount_point',
                 'mount_opts', 'fs_type', 'source', 'super_opts')

    def __init__(self, mount_id, parent_id, major, minor, root, mount_point,
                 mount_opts, fs_type, source, super_opts):
        self.mount_id = mount_id
        self.parent_id = parent_id
        self.major = major
        self.minor = minor
        self.root = root
        self.mount_point = mount_point
        self.mount_opts = mount_opts
        self.fs_type = fs_type
        self.source = source
        self.super_opts = super_opts

    @property
    def dev_id(self):
        return (self.major, self.minor)

    def __repr__(self):
        return (f'MountEntry({self.mount_point} dev={self.major}:{self.minor} '
                f'fs={self.fs_type} root={self.root})')


def parse_mountinfo(text):
    """Parse mountinfo content. The RAW line splits on spaces (escapes make
    fields unambiguous), then each field is unescaped. Malformed lines are
    fatal — a guard that skips unreadable mounts is not a guard."""
    entries = []
    for lineno, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line:
            continue
        parts = line.split(' ')
        if len(parts) < 7:
            raise GuardError('mountinfo_parse_error',
                             f'mountinfo line {lineno} has too few fields')
        try:
            mount_id = int(parts[0])
            parent_id = int(parts[1])
            major_s, minor_s = parts[2].split(':')
            major, minor = int(major_s), int(minor_s)
        except ValueError:
            raise GuardError('mountinfo_parse_error',
                             f'mountinfo line {lineno} numeric fields malformed')
        root = _unescape_field(parts[3])
        mount_point = _unescape_field(parts[4])
        mount_opts = _unescape_field(parts[5])
        try:
            sep = parts.index('-', 6)
        except ValueError:
            raise GuardError('mountinfo_parse_error',
                             f'mountinfo line {lineno} missing option separator')
        if len(parts) < sep + 3:
            raise GuardError('mountinfo_parse_error',
                             f'mountinfo line {lineno} missing fstype/source')
        fs_type = _unescape_field(parts[sep + 1])
        source = _unescape_field(parts[sep + 2])
        super_opts = _unescape_field(' '.join(parts[sep + 3:]))
        entries.append(MountEntry(mount_id, parent_id, major, minor, root,
                                  mount_point, mount_opts, fs_type, source, super_opts))
    if not entries:
        raise GuardError('mountinfo_empty', 'no mounts parsed from mountinfo')
    return entries


# ══════════════════════════════════════════════════════════════════
# expectations and volume identity resolution
# ══════════════════════════════════════════════════════════════════

class VolumeExpectation:
    """Expected volume identity. UUID AND device path are both mandatory —
    optional-only expectations cannot silently skip physical identity checks.
    fs_type / mount_root are enforced whenever present."""

    def __init__(self, uuid, device_path, archive_id, fs_type=None, mount_root=None,
                 min_free_bytes=0):
        for name, value in (('uuid', uuid), ('device_path', device_path),
                            ('archive_id', archive_id)):
            if not value or not isinstance(value, str):
                raise GuardError('expectation_invalid',
                                 f'expected volume {name} is mandatory')
        self.uuid = uuid
        self.device_path = device_path
        self.archive_id = archive_id
        self.fs_type = fs_type
        self.mount_root = mount_root
        self.min_free_bytes = int(min_free_bytes)

    @classmethod
    def from_config(cls, cfg):
        if not isinstance(cfg, dict):
            raise GuardError('expectation_invalid', 'volume expectation must be an object')
        return cls(cfg.get('uuid'), cfg.get('device_path'), cfg.get('archive_id'),
                   cfg.get('fs_type'), cfg.get('mount_root'),
                   cfg.get('min_free_bytes', 0))


def _resolve_block_device(major, minor, dev_root=BLOCK_ROOT):
    link = os.path.join(dev_root, f'{major}:{minor}')
    try:
        target = os.readlink(link)
    except OSError as exc:
        raise GuardError('block_device_unresolved',
                         f'cannot resolve {link}: {exc.strerror or exc}')
    path = target if target.startswith('/') else os.path.normpath(
        os.path.join(dev_root, target))
    if not os.path.exists(path):
        raise GuardError('block_device_unresolved',
                         f'resolved device path {path} does not exist')
    return path


def _uuid_for_device(device_path, by_uuid_root=BY_UUID_ROOT):
    try:
        names = os.listdir(by_uuid_root)
    except OSError as exc:
        raise GuardError('uuid_unresolved',
                         f'cannot enumerate {by_uuid_root}: {exc.strerror or exc}')
    for name in names:
        try:
            target = os.readlink(os.path.join(by_uuid_root, name))
        except OSError:
            continue
        resolved = target if target.startswith('/') else os.path.normpath(
            os.path.join(by_uuid_root, target))
        if resolved == device_path:
            return name
    raise GuardError('uuid_unresolved', f'no by-uuid mapping points at {device_path}')


# ══════════════════════════════════════════════════════════════════
# identity marker (mandatory both tiers, explicit init only)
# ══════════════════════════════════════════════════════════════════

def _marker_fingerprint(marker):
    payload = {k: v for k, v in marker.items() if k != 'fingerprint'}
    return hashlib.sha256(_canonical(payload).encode('utf-8')).hexdigest()


def _write_marker(root_path, marker):
    """Explicit-init-only marker write: O_EXCL, refuses overwrite, durable."""
    fd = os.open(root_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        if _marker_exists_fd(fd):
            raise GuardError('marker_exists',
                             'identity marker already present; refusing to overwrite')
        data = (_canonical(marker) + '\n').encode('utf-8')
        tmp = unique_temp(MARKER_NAME)
        try:
            tfd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                          0o600, dir_fd=fd)
            try:
                write_all(tfd, data)
                os.fsync(tfd)
            finally:
                os.close(tfd)
            os.rename(tmp, MARKER_NAME, src_dir_fd=fd, dst_dir_fd=fd)
        except BaseException:
            try:
                os.unlink(tmp, dir_fd=fd)
            except OSError:
                pass
            raise
        os.fsync(fd)
    finally:
        os.close(fd)


def _marker_exists_fd(root_fd):
    try:
        fd = os.open(MARKER_NAME, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=root_fd)
    except OSError:
        return False
    os.close(fd)
    return True


def read_marker_fd(root_fd):
    """Read + integrity-validate the identity marker through a dir fd."""
    try:
        fd = os.open(MARKER_NAME, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=root_fd)
    except OSError as exc:
        if exc.errno in (errno.ENOENT, errno.ELOOP):
            raise GuardError('guard_marker_missing',
                             'identity marker absent; explicit init required')
        raise GuardError('marker_unreadable', str(exc))
    try:
        with os.fdopen(fd, 'r', encoding='utf-8', closefd=False) as fh:
            marker = json.load(fh)
    except (ValueError, OSError) as exc:
        raise GuardError('marker_corrupt', f'marker not valid JSON: {exc}')
    finally:
        os.close(fd)
    if not isinstance(marker, dict) or marker.get('tier') not in ('production', 'synthetic'):
        raise GuardError('marker_invalid', 'marker tier missing or unknown')
    fingerprint = marker.pop('fingerprint', None)
    if fingerprint != _marker_fingerprint(marker):
        raise GuardError('marker_tampered', 'marker fingerprint mismatch')
    if not marker.get('archive_id') or not isinstance(marker.get('epoch'), int):
        raise GuardError('marker_invalid', 'marker missing archive_id/epoch')
    return marker


# ══════════════════════════════════════════════════════════════════
# binding: held root fd, provable subpaths, durable writes
# ══════════════════════════════════════════════════════════════════

def _parts_of(relpath):
    if not isinstance(relpath, (str, os.PathLike)):
        raise GuardError('path_invalid', f'bad relpath type: {type(relpath)!r}')
    text = os.fspath(relpath)
    if text.startswith('/'):
        raise GuardError('path_invalid', f'absolute path rejected under root: {text}')
    parts = [p for p in text.split('/') if p not in ('', '.')]
    if any(p == '..' for p in parts):
        raise GuardError('path_escape_rejected', f'relpath escapes root: {text}')
    return parts


def _component_is_symlink(part, dir_fd):
    """True when an intermediate component that failed to open is actually a
    symlink. Opening a symlink with O_NOFOLLOW|O_DIRECTORY yields ELOOP on
    some platforms but ENOTDIR on Linux — errno alone cannot distinguish an
    attack-shaped symlink from a regular file sitting in the middle of a
    path, so LOOK at the component (fstatat semantics, no fd opened)."""
    try:
        st = os.stat(part, dir_fd=dir_fd, follow_symlinks=False)
    except OSError:
        return False
    return stat.S_ISLNK(st.st_mode)


class _Binding:
    """Holds the verified root directory fd. All subpath access goes through
    openat with O_NOFOLLOW so a swapped symlink is rejected, not followed."""

    tier = None

    def __init__(self, root, root_fd, st_dev, st_ino, marker_epoch):
        self.root = root
        self.root_fd = root_fd
        self.st_dev = st_dev
        self.st_ino = st_ino
        self.marker_epoch = marker_epoch
        self._db_identity = None
        self.closed = False

    # ── identity revalidation (subclass extends with volume re-proof) ──
    def check_alive(self):
        if self.closed:
            raise GuardError('binding_closed', 'root binding already released')
        try:
            st = os.fstat(self.root_fd)
        except OSError as exc:
            raise GuardError('root_binding_lost', f'root fd fstat failed: {exc}')
        if not stat.S_ISDIR(st.st_mode) or st.st_dev != self.st_dev or st.st_ino != self.st_ino:
            raise GuardError('root_rebound',
                             'held root fd no longer matches verified identity')
        try:
            lst = os.lstat(self.root)
        except OSError as exc:
            raise GuardError('root_path_lost',
                             f'archive root path no longer reachable: {exc.strerror or exc}')
        if stat.S_ISLNK(lst.st_mode):
            raise GuardError('path_symlink_rejected', 'archive root path became a symlink')
        if (lst.st_dev, lst.st_ino) != (self.st_dev, self.st_ino):
            raise GuardError('root_rebound',
                             'archive root path now resolves to a different directory')
        marker = read_marker_fd(self.root_fd)
        if marker['epoch'] != self.marker_epoch:
            raise GuardError('epoch_changed',
                             f"marker epoch moved {self.marker_epoch}→{marker['epoch']}; "
                             'binding is stale (new epoch invalidates old bindings)')

    # ── dirfd-relative resolution ────────────────────────────────────
    def _open_chain(self, relpath, final_flags, final_mode=0o600):
        if self.closed:
            raise GuardError('binding_closed', 'root binding already released')
        parts = _parts_of(relpath)
        if not parts:
            raise GuardError('path_invalid', 'empty relpath')
        dir_fd = self.root_fd
        opened = []
        try:
            for part in parts[:-1]:
                try:
                    nxt = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                  dir_fd=dir_fd)
                except OSError as exc:
                    # Linux returns ENOTDIR (not ELOOP) for O_NOFOLLOW|
                    # O_DIRECTORY on a symlink: inspect the component so the
                    # consumer sees 'symlink', not a generic open failure.
                    if exc.errno in (errno.ELOOP, errno.ENOTDIR) \
                            and _component_is_symlink(part, dir_fd):
                        raise GuardError('path_symlink_rejected',
                                         f'symlink in path rejected: {relpath}')
                    raise
                opened.append(nxt)
                st = os.fstat(nxt)
                if st.st_dev != self.st_dev:
                    raise GuardError('cross_device_rejected',
                                     f'{part} resolves to another device')
                dir_fd = nxt
            fd = os.open(parts[-1], final_flags, final_mode, dir_fd=dir_fd)
            st = os.fstat(fd)
            if st.st_dev != self.st_dev:
                os.close(fd)
                raise GuardError('cross_device_rejected',
                                 f'{parts[-1]} resolves to another device')
            return fd
        except OSError as exc:
            if exc.errno == errno.ELOOP:
                raise GuardError('path_symlink_rejected',
                                 f'symlink in path rejected: {relpath}')
            if exc.errno == errno.ENOENT:
                raise GuardError('path_missing', f'not found under root: {relpath}')
            if exc.errno == errno.EEXIST:
                raise GuardError('path_exists', f'already exists: {relpath}')
            raise GuardError('path_open_failed',
                             f'cannot open {relpath}: {exc.strerror or exc}')
        finally:
            for fd in opened:
                os.close(fd)

    def open_file_read(self, relpath):
        return self._open_chain(relpath, os.O_RDONLY | os.O_NOFOLLOW)

    def open_dir(self, relpath='.'):
        if relpath in ('.', ''):
            return os.dup(self.root_fd)
        return self._open_chain(relpath, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)

    def stat_path(self, relpath):
        """FD-FREE stat through the held chain (only intermediate dirs are
        opened): stat(2) with AT_SYMLINK_NOFOLLOW on the leaf. Opening the
        leaf file — even briefly — would drop the process's POSIX fcntl locks
        on that inode (SQLite's locks on master.db/-wal/-shm)."""
        return self._stat_chain(relpath)

    def _stat_chain(self, relpath):
        if self.closed:
            raise GuardError('binding_closed', 'root binding already released')
        parts = _parts_of(relpath)
        if not parts:
            raise GuardError('path_invalid', 'empty relpath')
        dir_fd = self.root_fd
        opened = []
        try:
            for part in parts[:-1]:
                try:
                    nxt = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                  dir_fd=dir_fd)
                except OSError as exc:
                    if exc.errno in (errno.ELOOP, errno.ENOTDIR) \
                            and _component_is_symlink(part, dir_fd):
                        raise GuardError('path_symlink_rejected',
                                         f'symlink in path rejected: {relpath}')
                    raise
                opened.append(nxt)
                st = os.fstat(nxt)
                if st.st_dev != self.st_dev:
                    raise GuardError('cross_device_rejected',
                                     f'{part} resolves to another device')
                dir_fd = nxt
            try:
                st = os.stat(parts[-1], dir_fd=dir_fd, follow_symlinks=False)
            except FileNotFoundError:
                raise GuardError('path_missing', f'not found under root: {relpath}')
            except OSError as exc:
                raise GuardError('path_open_failed',
                                 f'cannot stat {relpath}: {exc.strerror or exc}')
            if stat.S_ISLNK(st.st_mode):
                raise GuardError('path_symlink_rejected',
                                 f'leaf is a symlink, rejected: {relpath}')
            if st.st_dev != self.st_dev:
                raise GuardError('cross_device_rejected',
                                 f'{parts[-1]} resolves to another device')
            return st
        except OSError as exc:
            if exc.errno == errno.ELOOP:
                raise GuardError('path_symlink_rejected',
                                 f'symlink in path rejected: {relpath}')
            raise GuardError('path_open_failed',
                             f'cannot open parent of {relpath}: {exc.strerror or exc}')
        finally:
            for fd in opened:
                os.close(fd)

    def read_bytes(self, relpath):
        fd = self.open_file_read(relpath)
        try:
            with os.fdopen(fd, 'rb', closefd=False) as fh:
                return fh.read()
        finally:
            os.close(fd)

    def read_json(self, relpath):
        return json.loads(self.read_bytes(relpath).decode('utf-8'))

    def ensure_dir(self, relpath):
        """Create relpath (with parents) through the binding: each level via
        mkdir(dir_fd) 0700; existing levels must be real same-device dirs."""
        if self.closed:
            raise GuardError('binding_closed', 'root binding already released')
        parts = _parts_of(relpath)
        if not parts:
            raise GuardError('path_invalid', 'empty relpath')
        dir_fd = os.dup(self.root_fd)
        try:
            for part in parts:
                try:
                    os.mkdir(part, 0o700, dir_fd=dir_fd)
                except FileExistsError:
                    pass
                except OSError as exc:
                    raise GuardError('mkdir_failed',
                                     f'cannot create {part}: {exc.strerror or exc}')
                os.fsync(dir_fd)  # durable directory entry, failures propagate
                try:
                    nxt = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                  dir_fd=dir_fd)
                except OSError as exc:
                    if exc.errno in (errno.ELOOP, errno.ENOTDIR) \
                            and _component_is_symlink(part, dir_fd):
                        raise GuardError('path_symlink_rejected',
                                         f'symlink in path: {relpath}')
                    raise
                os.close(dir_fd)
                st = os.fstat(nxt)
                if st.st_dev != self.st_dev:
                    os.close(nxt)
                    raise GuardError('cross_device_rejected',
                                     f'{part} resolves to another device')
                dir_fd = nxt
            return dir_fd  # caller closes
        except GuardError:
            os.close(dir_fd)
            raise
        except OSError as exc:
            os.close(dir_fd)
            if exc.errno == errno.ELOOP:
                raise GuardError('path_symlink_rejected', f'symlink in path: {relpath}')
            raise GuardError('path_open_failed', f'cannot create {relpath}: {exc}')

    def _ensure_or_open(self, parent):
        if not parent:
            return os.dup(self.root_fd)
        try:
            return self.open_dir(parent)
        except GuardError as exc:
            if exc.code != 'path_missing':
                raise
            return self.ensure_dir(parent)

    def write_file_atomic(self, relpath, data: bytes, mode=0o600):
        """Durable write through the binding: unique temp → full write → fsync
        file → rename → fsync dir. Every failure propagates; temp cleaned up."""
        parts = _parts_of(relpath)
        if not parts:
            raise GuardError('path_invalid', 'empty relpath')
        parent = '/'.join(parts[:-1])
        name = parts[-1]
        dir_fd = self._ensure_or_open(parent)
        tmp = unique_temp(name)
        try:
            try:
                os.unlink(tmp, dir_fd=dir_fd)
            except FileNotFoundError:
                pass
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                         mode, dir_fd=dir_fd)
            try:
                write_all(fd, data)
                os.fsync(fd)
            finally:
                os.close(fd)
            os.rename(tmp, name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
            os.fsync(dir_fd)
        except BaseException:
            try:
                os.unlink(tmp, dir_fd=dir_fd)
            except OSError:
                pass
            raise
        finally:
            os.close(dir_fd)

    def write_json_atomic(self, relpath, obj, mode=0o600):
        self.write_file_atomic(relpath, _canonical(obj).encode('utf-8') + b'\n', mode)

    def write_stream(self, relpath, chunk_iter, mode=0o600):
        """Stream an iterable of byte chunks into relpath atomically: unique
        temp file, write_all per chunk, fsync, rename, fsync dir. Memory stays
        bounded by the chunk size no matter how large the object is. Failures
        propagate and clean the temp file."""
        parts = _parts_of(relpath)
        if not parts:
            raise GuardError('path_invalid', 'empty relpath')
        parent = '/'.join(parts[:-1])
        name = parts[-1]
        dir_fd = self._ensure_or_open(parent)
        tmp = unique_temp(name)
        try:
            try:
                os.unlink(tmp, dir_fd=dir_fd)
            except FileNotFoundError:
                pass
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                         mode, dir_fd=dir_fd)
            try:
                for chunk in chunk_iter:
                    if not isinstance(chunk, (bytes, bytearray, memoryview)):
                        raise GuardError('write_invalid',
                                         'write_stream chunks must be bytes')
                    write_all(fd, bytes(chunk))
                os.fsync(fd)
            finally:
                os.close(fd)
            os.rename(tmp, name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
            os.fsync(dir_fd)
        except BaseException:
            try:
                os.unlink(tmp, dir_fd=dir_fd)
            except OSError:
                pass
            raise
        finally:
            os.close(dir_fd)

    def rename_under(self, src_parent, src_name, dst_parent, dst_name):
        """Cross-directory rename between two relpaths under the binding, with
        dir fsync on both sides; used for staging→objects promotion."""
        src_fd = self._ensure_or_open(src_parent)
        try:
            dst_fd = self._ensure_or_open(dst_parent)
            try:
                os.rename(src_name, dst_name, src_dir_fd=src_fd, dst_dir_fd=dst_fd)
                os.fsync(src_fd)
                os.fsync(dst_fd)
            finally:
                os.close(dst_fd)
        finally:
            os.close(src_fd)

    # ── SQLite binding proof ─────────────────────────────────────────
    def prove_db(self, relpath, allow_missing=False):
        """Prove the db path resolves — through the held fd chain — to the
        inode recorded at first proof. NEVER opens the leaf file: POSIX fcntl
        locks are per-process and close(2) of ANY descriptor on an inode
        releases every lock the process holds on it — an open+close here would
        silently strip the live SQLite connection of its locks mid-transaction.
        So the leaf is verified purely by stat (AT_SYMLINK_NOFOLLOW semantics)
        through dir fds, and every re-proof compares (st_dev, st_ino) plus
        same-device + not-a-symlink. A swapped file/symlink/rename is rejected
        BEFORE any transaction; SQLite itself opens read-write-no-create only
        (or through the pinned VFS on Linux), so it can neither materialise a
        new database nor redirect past the proof."""
        self.check_alive()
        try:
            st = self._stat_chain(relpath)
        except GuardError as exc:
            if allow_missing and exc.code == 'path_missing':
                return None
            raise
        if not stat.S_ISREG(st.st_mode):
            raise GuardError('path_not_regular',
                             f'database path is not a regular file: {relpath}')
        ident = (st.st_dev, st.st_ino)
        if self._db_identity is None:
            self._db_identity = ident
            return ident
        if ident != self._db_identity:
            raise GuardError('path_rebound',
                             'database path now resolves to a different inode')
        return ident

    def db_identity(self):
        return self._db_identity

    def close(self):
        if not self.closed:
            self.closed = True
            try:
                os.close(self.root_fd)
            except OSError:
                pass


class ProductionBinding(_Binding):
    tier = 'production'

    def __init__(self, root, root_fd, st_dev, st_ino, marker_epoch, guard):
        super().__init__(root, root_fd, st_dev, st_ino, marker_epoch)
        self.guard = guard

    def check_alive(self):
        super().check_alive()
        # Continuous guard: the FULL volume proof re-runs on every use —
        # mount topology, UUID/unlock mapping, capacity, marker. A startup
        # precheck never substitutes for per-transaction verification.
        self.guard.reverify(self)


class SyntheticBinding(_Binding):
    tier = 'synthetic'


# ══════════════════════════════════════════════════════════════════
# guards
# ══════════════════════════════════════════════════════════════════

class ProductionGuard:
    """Default tier. Denies unless every expectation matches the real mount,
    and RE-PROVES that on every binding use."""

    tier = 'production'

    def __init__(self, expectation, mountinfo_path=PROC_MOUNTINFO,
                 block_root=BLOCK_ROOT, by_uuid_root=BY_UUID_ROOT):
        self.expectation = (expectation if isinstance(expectation, VolumeExpectation)
                            else VolumeExpectation.from_config(expectation))
        self.mountinfo_path = mountinfo_path
        self.block_root = block_root
        self.by_uuid_root = by_uuid_root

    # ── explicit init ────────────────────────────────────────────────
    def write_marker(self, root, epoch, created_at):
        marker = {
            'tier': 'production',
            'archive_id': self.expectation.archive_id,
            'epoch': int(epoch),
            'created_at': int(created_at),
            'uuid': self.expectation.uuid,
        }
        marker['fingerprint'] = _marker_fingerprint(marker)
        _write_marker(os.path.abspath(str(root)), marker)

    # ── full topology proof ──────────────────────────────────────────
    def _prove_topology(self, root_abs):
        if not sys.platform.startswith('linux'):
            raise GuardError('platform_unverified',
                             f'production guard requires Linux mountinfo; platform '
                             f'{sys.platform} cannot prove volume identity')
        try:
            with open(self.mountinfo_path, 'r', encoding='utf-8') as fh:
                entries = parse_mountinfo(fh.read())
        except FileNotFoundError:
            raise GuardError('mountinfo_unavailable',
                             f'{self.mountinfo_path} not present; refusing')
        try:
            root_stat = os.stat(root_abs)
        except OSError as exc:
            raise GuardError('volume_not_mounted',
                             f'archive root not reachable: {exc.strerror or exc}')
        best = None
        for entry in entries:
            mp = entry.mount_point
            if root_abs == mp or root_abs.startswith(mp.rstrip('/') + '/'):
                if best is None or len(mp) > len(best.mount_point):
                    best = entry
        if best is None:
            raise GuardError('volume_not_mounted', f'no mount covers {root_abs}')
        if (best.major, best.minor) != (os.major(root_stat.st_dev),
                                        os.minor(root_stat.st_dev)):
            raise GuardError('device_mismatch',
                             'covering mount device does not match root st_dev')
        exp = self.expectation
        if exp.fs_type and best.fs_type != exp.fs_type:
            raise GuardError('fs_type_mismatch',
                             f'expected {exp.fs_type}, mount is {best.fs_type}')
        if exp.mount_root and best.root != exp.mount_root:
            raise GuardError('mount_root_mismatch',
                             f'expected in-device root {exp.mount_root}, '
                             f'mount reports {best.root}')
        device = _resolve_block_device(best.major, best.minor, self.block_root)
        if device != exp.device_path:
            raise GuardError('device_path_mismatch',
                             f'expected device {exp.device_path}, resolved {device}')
        uuid = _uuid_for_device(device, self.by_uuid_root)
        if uuid != exp.uuid:
            raise GuardError('uuid_mismatch',
                             f'expected volume UUID {exp.uuid}, mount maps {uuid}')
        return root_stat

    def _check_capacity(self, root_abs):
        floor = self.expectation.min_free_bytes
        if not floor:
            return
        try:
            usage = os.statvfs(root_abs)
        except OSError as exc:
            raise GuardError('capacity_check_failed', str(exc))
        free = usage.f_bavail * usage.f_frsize
        if free < floor:
            raise GuardError('capacity_below_floor',
                             f'free {free} < floor {floor}',
                             {'free': free, 'floor': floor})

    def verify(self, root):
        root_abs = os.path.abspath(str(root))
        root_stat = self._prove_topology(root_abs)
        self._check_capacity(root_abs)
        root_fd = os.open(root_abs, os.O_RDONLY | os.O_DIRECTORY)
        try:
            marker = read_marker_fd(root_fd)
            if marker['tier'] != 'production':
                raise GuardError('marker_tier_mismatch',
                                 f"expected production marker, found {marker['tier']}")
            if marker['archive_id'] != self.expectation.archive_id:
                raise GuardError('archive_id_mismatch',
                                 f"marker archive_id {marker['archive_id']!r} != "
                                 f"{self.expectation.archive_id!r}")
            if marker.get('uuid') != self.expectation.uuid:
                raise GuardError('uuid_mismatch',
                                 'marker uuid does not match expected volume uuid')
            st = os.fstat(root_fd)
            if st.st_dev != root_stat.st_dev or st.st_ino != root_stat.st_ino:
                raise GuardError('root_rebound', 'root fd identity differs from stat')
            return ProductionBinding(Path(root_abs), root_fd, st.st_dev, st.st_ino,
                                     marker['epoch'], self)
        except BaseException:
            os.close(root_fd)
            raise

    def reverify(self, binding):
        """Continuous per-transaction proof: identical checks to verify(),
        against the binding's already-held root fd."""
        root_abs = str(binding.root)
        self._prove_topology(root_abs)
        self._check_capacity(root_abs)
        st = os.fstat(binding.root_fd)
        if st.st_dev != binding.st_dev or st.st_ino != binding.st_ino:
            raise GuardError('root_rebound', 'held root fd no longer matches mount')
        marker = read_marker_fd(binding.root_fd)
        if marker['tier'] != 'production' or marker['archive_id'] != self.expectation.archive_id \
                or marker.get('uuid') != self.expectation.uuid:
            raise GuardError('marker_invalid', 'marker no longer matches expectations')


class SyntheticGuard:
    """Explicit TEST-ONLY adapter. st_dev + marker binding; not a production
    guard. Never auto-initialises: a missing marker is an error, not a create."""

    tier = 'synthetic'

    def __init__(self, archive_id, min_free_bytes=0):
        if not archive_id:
            raise GuardError('expectation_invalid', 'synthetic guard needs archive_id')
        self.archive_id = archive_id
        self.min_free_bytes = int(min_free_bytes)

    @staticmethod
    def write_marker(root, archive_id, epoch, created_at):
        """Only invoked by the explicit `init` CLI, never by verify()."""
        marker = {
            'tier': 'synthetic',
            'archive_id': archive_id,
            'epoch': int(epoch),
            'created_at': int(created_at),
            'st_dev': os.stat(os.path.abspath(str(root))).st_dev,
        }
        marker['fingerprint'] = _marker_fingerprint(marker)
        _write_marker(os.path.abspath(str(root)), marker)

    def verify(self, root):
        root_path = Path(os.path.abspath(str(root)))
        try:
            root_lstat = os.lstat(root_path)
        except OSError as exc:
            raise GuardError('root_missing', f'archive root missing: {exc.strerror or exc}')
        if stat.S_ISLNK(root_lstat.st_mode):
            raise GuardError('path_symlink_rejected', 'archive root itself is a symlink')
        root_stat = os.stat(root_path)
        root_fd = os.open(root_path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            marker = read_marker_fd(root_fd)
            if marker['tier'] != 'synthetic':
                raise GuardError('marker_tier_mismatch',
                                 f"expected synthetic marker, found {marker['tier']}")
            if marker['archive_id'] != self.archive_id:
                raise GuardError('archive_id_mismatch',
                                 f"marker archive_id {marker['archive_id']!r} != "
                                 f"{self.archive_id!r}")
            if int(marker.get('st_dev', -1)) != root_stat.st_dev:
                raise GuardError('synthetic_device_changed',
                                 'archive root moved to a different device')
            if self.min_free_bytes:
                usage = os.statvfs(root_path)
                free = usage.f_bavail * usage.f_frsize
                if free < self.min_free_bytes:
                    raise GuardError('capacity_below_floor',
                                     f'free {free} < floor {self.min_free_bytes}',
                                     {'free': free, 'floor': self.min_free_bytes})
            st = os.fstat(root_fd)
            if st.st_dev != root_stat.st_dev or st.st_ino != root_stat.st_ino:
                raise GuardError('root_rebound', 'root fd identity differs from stat')
            return SyntheticBinding(root_path, root_fd, st.st_dev, st.st_ino,
                                    marker['epoch'])
        except BaseException:
            os.close(root_fd)
            raise


def make_guard(root, config, allow_synthetic=False):
    """Guard factory used by CLI/endpoint. Production tier is the default and
    fail-closed; synthetic tier only with explicit opt-in flag."""
    if not isinstance(config, dict):
        raise GuardError('guard_config_invalid', 'guard config must be an object')
    tier = config.get('tier', 'production')
    if tier == 'production':
        return ProductionGuard(VolumeExpectation.from_config(config))
    if tier == 'synthetic':
        if not allow_synthetic:
            raise GuardError('synthetic_not_allowed',
                             'synthetic guard requires explicit --allow-synthetic-guard')
        return SyntheticGuard(config.get('archive_id'),
                              config.get('min_free_bytes', 0))
    raise GuardError('guard_tier_unknown', f'unknown guard tier {tier!r}')
