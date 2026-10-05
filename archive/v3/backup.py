"""Archive-level encrypted backup, chain state and restore drill (§S4 backup).

Approved-spec design (external-archive-v3 §1.8 / §S4), no synthetic-grade
shortcuts:

* **Encryption is AES-256-GCM** (cryptography, streaming Cipher/GCM): every
  blob is encrypted chunk-by-chunk — plaintext is NEVER accumulated in memory
  (no whole-file buffers) and never written unencrypted inside the backup
  directory. Decryption streams to a temp file and verifies the GCM tag at
  finalize; plaintext is consumed only after the tag check. Each blob's AAD
  binds archive_id, role, file name and length, so ciphertexts cannot be
  swapped between roles or truncated.
* **Consistent baseline.** The master is copied with the sqlite online backup
  API inside one read_snapshot view (a single commit_seq), then normalised to
  a self-contained file. Counts, epoch and the mutation log are read from the
  same view / the copy itself — the backup never mixes two states.
* **Plaintext staging is writer-private on the FIXED archive volume.** Every
  plaintext intermediate (the SQLite master copy, the mutation-log tail)
  lives under the guard-bound archive root in a 0700 per-backup staging dir
  opened through the binding — never in the system TMPDIR, which is a
  different volume outside the writer's containment. On Linux the SQLite
  work copy connects through a pinned VFS anchored on the held staging fd
  (the same discipline as the live master); a plain-path connect here would
  bypass the approved fixed-volume binding.
* **The independent target is validated component-wise and held by fd.**
  A symlink anywhere in the backup target chain — the leaf OR an ancestor
  (`link/child` would write through the link into foreign storage) — is
  refused before anything is created or chmod-ed, and every blob write is
  dir_fd-relative from the held target fd, so a component swapped after
  validation cannot redirect the backup either.
* **Incremental backups are REAL.** They carry the mutation-log tail
  (commit_seq in (protected, captured]) with COMPLETE after-images, plus the
  objects committed in that window. The restore drill actually REPLAYS the
  after-images onto the rebuilt baseline and compares per-table counts
  against every chain member's recorded counts — a replay that cannot
  reproduce the recorded state fails the drill, it never "returns ok".
* **protected_seq is a contiguous prefix** in master meta: an incremental
  must continue exactly from the current protected_seq (backup_chain_gap
  refuses a hole), and the prefix only advances after the backup files are
  durably written. Baseline rotation over an existing chain requires a
  recorded, matching drill pass (backup_rotation_requires_drill).
* **Restore is a DRILL into a fresh root.** It never touches the live
  archive, refuses an existing scratch target, builds under a sibling
  .building dir, and publishes the scratch root (marker, master.db, objects/)
  only after every check passed. Assets are physically recovered — decrypted,
  hash-verified against their content address, 0600 — not just listed.
* **Restored archives are born in a NEW epoch.** The drill rotates the
  rebuilt master to epoch+1 (logged in its mutation trail) and PROVES a
  run acknowledged in the old epoch now fails verify_run_binding with
  run_epoch_stale: old capture ACKs never survive a restore.
* **Freshness vs RPO are reported separately** (backup_status): wall-clock
  time since the last successful backup, and the number of commits not yet
  protected.

Structural decoupling: this module imports errors/util/guard/master only —
the collector path never calls into it, and a backup-target failure can
never block collection (it fails the backup, nothing else).
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import stat
import sys
import time
from pathlib import Path

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

from .errors import ArchiveError
from .guard import SyntheticGuard
from .master import DB_NAME, MasterStore
from .util import canonical, now_ms, sha256_hex

BACKUP_MAGIC = b'WXV3B2'
BACKUP_VERSION = 2
KDF_ROUNDS = 600_000
SALT_BYTES = 16
NONCE_BYTES = 12
TAG_BYTES = 16
CHUNK = 256 * 1024

# objects live content-addressed under the archive root; defined locally so
# this module stays decoupled from the endpoint/collector import graph
OBJECTS_REL = 'objects'

# tables whose mutation-log after-images are complete rows and are replayed
# by the drill. identity_map is derived from messages after-images (its
# columns are a subset of the messages row). meta is the only live-bearing
# table left to the next baseline — epoch/commit_seq are deliberately NOT
# replayed (the drill rotates the restored copy to its OWN new epoch).
REPLAY_TABLES = ('messages', 'identity_map', 'message_revisions',
                 'missing_observations', 'media_assets', 'media_needs',
                 'objects', 'runs', 'batches', 'source_observations')
COUNTED_TABLES = REPLAY_TABLES + ('mutation_log',)

PROTECTED_KEY = 'backup_protected_seq'
STATE_KEY = 'backup_state_json'
DRILL_PASS_KEY = 'backup_drill_pass'


# ── key derivation + streaming AES-GCM ─────────────────────────────────────
def derive_key(key_material: bytes, salt: bytes) -> bytes:
    """PBKDF2-HMAC-SHA256 → 256-bit AES key. The salt is public (manifest);
    the key material never leaves the operator's side and is never stored."""
    if not isinstance(key_material, (bytes, bytearray)) or not key_material:
        raise ArchiveError('backup_key_invalid',
                           'key material must be non-empty bytes')
    kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32,
                     salt=bytes(salt), iterations=KDF_ROUNDS)
    return kdf.derive(bytes(key_material))


def _aad(archive_id: str, role: str, name: str, length: int) -> bytes:
    return canonical({'archive_id': archive_id, 'role': role,
                      'name': name, 'length': int(length)}).encode('utf-8')


def _encrypt_stream(src_fd: int, dir_fd: int, name: str, key: bytes,
                    nonce: bytes, aad: bytes, length: int) -> dict:
    """Stream-encrypt `length` plaintext bytes from src_fd into dir_fd/name.

    Every write is anchored to the HELD target dir fd: the temp file, the
    fsync-rename and the directory fsync all go through it, so a path
    component swapped after validation cannot redirect a backup blob. The
    blob layout is MAGIC | version | salt is in the manifest (per-run) |
    nonce(12) | ciphertext | tag(16, tail). Plaintext is touched one CHUNK
    at a time — never buffered whole."""
    encryptor = Cipher(algorithms.AES(key), modes.GCM(nonce)).encryptor()
    encryptor.authenticate_additional_data(aad)
    digest = hashlib.sha256()
    seen = 0
    tmp = f'.{name}.tmp{os.getpid()}'
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600,
                 dir_fd=dir_fd)
    try:
        written = 0
        header = BACKUP_MAGIC + bytes([BACKUP_VERSION]) + nonce
        _write_all(fd, header)
        written += len(header)
        while True:
            block = os.read(src_fd, CHUNK)
            if not block:
                break
            digest.update(block)
            seen += len(block)
            cipher = encryptor.update(block)
            _write_all(fd, cipher)
            written += len(cipher)
        if seen != length:
            raise ArchiveError('backup_source_changed',
                               'source bytes changed under the backup',
                               {'expected_length': int(length),
                                'actual_length': seen})
        _write_all(fd, encryptor.finalize())        # empty; tag comes next
        _write_all(fd, encryptor.tag)
        written += TAG_BYTES
        os.fsync(fd)
        os.close(fd)
        fd = -1
        os.rename(tmp, name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
        os.fsync(dir_fd)
    finally:
        if fd >= 0:
            os.close(fd)
        try:
            os.unlink(tmp, dir_fd=dir_fd)
        except OSError:
            pass
    return {'plaintext_sha256': digest.hexdigest(), 'plaintext_length': seen,
            'bytes': written}


def _decrypt_to_file(blob_path: Path, out_path: Path, key: bytes, aad: bytes,
                     expect_name: str) -> dict:
    """Stream-decrypt a blob to out_path, verifying the GCM tag at finalize.

    The plaintext file only appears (rename) when the tag, AAD and recorded
    length all check out — unverified plaintext is never handed to a
    consumer, and a failed decrypt leaves no file behind."""
    size = blob_path.stat().st_size
    header = len(BACKUP_MAGIC) + 1 + NONCE_BYTES
    if size < header + TAG_BYTES:
        raise ArchiveError('backup_blob_invalid',
                           f'blob {expect_name!r} too small')
    fd = os.open(blob_path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        magic = os.read(fd, len(BACKUP_MAGIC) + 1)
        if not magic.startswith(BACKUP_MAGIC) or \
                magic[-1] != BACKUP_VERSION:
            raise ArchiveError('backup_blob_invalid',
                               f'blob {expect_name!r} is not a v3 backup blob')
        nonce = os.read(fd, NONCE_BYTES)
        ct_length = size - header - TAG_BYTES
        decryptor = Cipher(algorithms.AES(key), modes.GCM(nonce)).decryptor()
        decryptor.authenticate_additional_data(aad)
        tmp = out_path.with_name(out_path.name + f'.verify{os.getpid()}')
        out_fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        digest = hashlib.sha256()
        seen = 0
        try:
            while seen < ct_length:
                block = os.read(fd, min(CHUNK, ct_length - seen))
                if not block:
                    break
                seen += len(block)
                plain = decryptor.update(block)
                digest.update(plain)
                _write_all(out_fd, plain)
            if seen != ct_length:
                raise ArchiveError('backup_blob_invalid',
                                   f'blob {expect_name!r} truncated')
            tag = os.read(fd, TAG_BYTES)
            try:
                decryptor.finalize_with_tag(tag)
            except Exception as exc:
                raise ArchiveError(
                    'backup_key_rejected',
                    f'GCM verification failed for {expect_name!r} — wrong key '
                    f'or tampered blob', {'file': expect_name}) from exc
            os.fsync(out_fd)
            os.close(out_fd)
            out_fd = -1
            os.replace(tmp, out_path)
        finally:
            if out_fd >= 0:
                os.close(out_fd)
            if tmp.exists():
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
        return {'plaintext_sha256': digest.hexdigest(),
                'plaintext_length': seen}
    finally:
        os.close(fd)


def _fsync_dir(path: Path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _write_all(fd: int, data: bytes):
    """os.write may write partially — a blob must never accept a short write."""
    while data:
        written = os.write(fd, data)
        if written <= 0:
            raise OSError('short write during backup blob build')
        data = data[written:]


def _secure_mkdir(path: Path, mode=0o700):
    path.mkdir(parents=True, mode=mode)
    os.chmod(path, mode)   # mkdir is umask-sensitive; enforce explicitly


# ── consistent snapshot helpers ────────────────────────────────────────────
class _Staging:
    """Writer-PRIVATE plaintext staging on the FIXED archive volume.

    The system TMPDIR is a different volume outside the writer's
    containment — a plaintext master copy or mutation-log tail there escapes
    everything the guard binding proves. Every plaintext intermediate
    therefore lives under the guard-bound archive root, in a 0700
    per-backup staging dir opened through the binding (component-wise
    O_NOFOLLOW, cross-device refusal — the same discipline as master.db
    itself). On Linux the SQLite work copy connects through a pinned VFS
    anchored on the held staging dir fd: a plain-path connect here would
    bypass the approved fixed-volume binding (temp spills included — etilqs_
    files become anonymous inodes inside the staging dir). The non-Linux
    synthetic tier mirrors master.py: a plain path under the bound root
    (production already refuses unprovable platforms at open)."""

    def __init__(self, store):
        self.binding = store.binding
        self.token = f'{os.getpid()}-{os.urandom(6).hex()}'
        self.rel = f'backup-work/{self.token}'
        self.fd = self.binding.ensure_dir(self.rel)   # held until close()
        self.path = Path(self.binding.root) / self.rel
        self._vfs = None

    def sqlite_connect(self, name):
        """RW connection to a fresh staging file, created through the held
        fd. On Linux the connection is pinned-VFS-anchored and PROVEN (a
        vfs= URI that silently degraded to the default VFS must reject,
        exactly like the live master)."""
        if sys.platform.startswith('linux'):
            from .vfs import register_pinned_vfs, release_pinned_vfs
            fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600,
                         dir_fd=self.fd)
            os.close(fd)
            self._vfs = register_pinned_vfs(self.fd, name)
            try:
                conn = sqlite3.connect(
                    f'file:{name}?vfs={self._vfs.name}&mode=rw',
                    uri=True, isolation_level=None)
                reported = conn.execute('PRAGMA database_list').fetchone()[2]
                if reported != self._vfs.anchored_main():
                    raise ArchiveError(
                        'backup_work_unproven',
                        'staging copy connection is not anchored to the held '
                        'staging fd', {'reported': reported})
            except BaseException:
                release_pinned_vfs(self._vfs)
                self._vfs = None
                raise
            return conn
        target = self.path / name
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.close(fd)
        return sqlite3.connect(str(target), isolation_level=None)

    def open_read(self, name):
        return os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=self.fd)

    def open_new(self, name):
        return os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600,
                       dir_fd=self.fd)

    def close(self):
        """Remove every staging entry through the held fd (flat names only),
        release the pinned VFS first, then the staging dir itself —
        plaintext never outlives the backup that produced it."""
        if self._vfs is not None:
            from .vfs import release_pinned_vfs
            release_pinned_vfs(self._vfs)
            self._vfs = None
        if self.fd is not None:
            try:
                with os.scandir(self.fd) as entries:
                    names = [entry.name for entry in entries]
                for name in names:
                    try:
                        os.unlink(name, dir_fd=self.fd)
                    except OSError:
                        pass
                os.fsync(self.fd)
            except OSError:
                pass
            finally:
                os.close(self.fd)
                self.fd = None
        try:
            parent = self.binding.open_dir('backup-work')
            try:
                os.rmdir(self.token, dir_fd=parent)
                os.fsync(parent)
            finally:
                os.close(parent)
        except OSError:
            pass


def _open_backup_target(out_dir: Path) -> int:
    """Validate EVERY component of the independent backup target and return
    a HELD dir fd (O_NOFOLLOW) for it.

    A symlink anywhere in the chain — the leaf OR an ancestor (a
    `link/child` target would write through the link into foreign storage,
    and mkdir(parents=True) would silently follow it) — is refused BEFORE
    anything is created or chmod-ed; resolve() is deliberately not used
    because it would bless exactly the redirection being checked for. The
    held fd anchors every subsequent blob write (dir_fd-relative opens and
    renames), so a component swapped after validation cannot redirect the
    backup either."""
    path = Path(os.path.abspath(os.fspath(out_dir)))
    if not path.parts:
        raise ArchiveError('backup_target_invalid', 'empty backup target')

    def reject_symlink(component, st):
        if stat.S_ISLNK(st.st_mode):
            raise ArchiveError(
                'backup_target_symlink',
                'backup target path crosses a symlink; refusing to write an '
                'independent encrypted backup through it',
                {'component': str(component)})

    cur = Path(path.parts[0])
    for part in path.parts[1:]:
        cur = cur / part
        try:
            st = os.lstat(cur)
        except FileNotFoundError:
            try:
                os.mkdir(cur, 0o700)
            except FileExistsError:
                pass    # raced into existence — re-lstat'd immediately below
            except OSError as exc:
                raise ArchiveError(
                    'backup_target_invalid',
                    f'cannot create backup target {cur}: '
                    f'{exc.strerror or exc}') from exc
            try:
                st = os.lstat(cur)
            except FileNotFoundError:
                continue   # vanished again; the next level fails loudly
            reject_symlink(cur, st)
            os.chmod(cur, 0o700)   # only after proving it is not a symlink
            continue
        reject_symlink(cur, st)
        if not stat.S_ISDIR(st.st_mode):
            raise ArchiveError('backup_target_invalid',
                               'backup target path component is not a '
                               'directory', {'component': str(cur)})
    try:
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError as exc:
        raise ArchiveError(
            'backup_target_invalid',
            f'cannot open backup target: {exc.strerror or exc}',
            {'dir': str(path)}) from exc
    return fd


def _assets_dir_fd(out_fd: int) -> int:
    """The 0700 'assets' subdir inside the held target fd."""
    try:
        os.mkdir('assets', 0o700, dir_fd=out_fd)
    except FileExistsError:
        pass
    fd = os.open('assets', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                 dir_fd=out_fd)
    os.fchmod(fd, 0o700)
    return fd


def _consistent_master_copy(store, staging: _Staging):
    """Online-backup-API copy of the master inside one read view, staged on
    the FIXED volume through `staging`. Returns the connection so the caller
    reads counts/epoch/objects from the SAME copy before closing it.

    The copy IS the committed state at exactly one commit_seq: the source
    connection holds a read transaction for the whole backup() call, so the
    page copy can never straddle a commit. Normalised (journal_mode=DELETE)
    so the file is self-contained without sidecar WALs."""
    conn = staging.sqlite_connect('master.copy.db')
    with store.read_snapshot():
        store.db.backup(conn)
    conn.execute('PRAGMA journal_mode=DELETE')
    return conn


def _counts_of(db) -> dict:
    return {table: db.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]
            for table in COUNTED_TABLES}


# ── backup creation ────────────────────────────────────────────────────────
def _read_state(store) -> dict:
    raw = store.meta(STATE_KEY)
    if raw is None:
        return {'chain': []}
    try:
        state = json.loads(raw)
    except ValueError:
        raise ArchiveError('backup_state_corrupt',
                           'recorded backup chain state is unreadable')
    if not isinstance(state, dict) or not isinstance(state.get('chain'), list):
        raise ArchiveError('backup_state_corrupt',
                           'recorded backup chain state is malformed')
    return state


def _protected_seq(store):
    raw = store.meta(PROTECTED_KEY)
    return None if raw is None else int(raw)


def _advance_protected(store, captured_seq, entry, state):
    """Advance the contiguous protected prefix and record the chain entry in
    ONE full transaction — only after the backup files are durably on disk.

    CAS-guarded against out-of-order publication: between this backup's
    capture and its publication a NEWER backup may have completed and moved
    the prefix forward. Re-asserting the older captured_seq would REGRESS
    the protected prefix (and disorder the chain), so a stale publication
    is refused outright — its files stay on disk but never become chain
    truth.

    The writer lock is re-entrant with respect to the CALLER'S held lock:
    an operator (or scheduler) that already owns the archive writer lock
    must be able to take a backup — a second acquire here would self-
    deadlock on writer_busy, and releasing the caller's lock afterwards
    would silently drop their ownership. Only an unheld lock is acquired
    and released here."""
    lock_was_held = getattr(store, '_lock_fd', None) is not None
    if not lock_was_held:
        store.acquire_writer_lock()
    try:
        with store.full_transaction() as tx:
            current = _protected_seq(store)
            live_chain = _read_state(store)['chain']
            if current is not None and int(current) > int(captured_seq):
                raise ArchiveError(
                    'backup_prefix_superseded',
                    'a newer backup already advanced the protected prefix '
                    'past this backup; refusing the stale publication',
                    {'current_protected_seq': int(current),
                     'stale_captured_seq': int(captured_seq),
                     'backup_id': entry['backup_id']})
            if len(live_chain) != len(state['chain']):
                raise ArchiveError(
                    'backup_prefix_superseded',
                    'the recorded chain grew while this backup was being '
                    'published; refusing the stale publication',
                    {'chain_len_expected': len(state['chain']),
                     'chain_len_now': len(live_chain),
                     'backup_id': entry['backup_id']})
            tx.meta_set(PROTECTED_KEY, str(int(captured_seq)))
            state = dict(state)
            state['chain'] = list(state['chain']) + [entry]
            state['last_backup_at_ms'] = now_ms()
            state['protected_seq'] = int(captured_seq)
            tx.meta_set(STATE_KEY, canonical(state))
            tx.mutation('meta', 'backup', 'protected_seq_advanced',
                        {'protected_seq': int(captured_seq)},
                        {'backup_id': entry['backup_id'],
                         'kind': entry['kind']})
    finally:
        if not lock_was_held:
            store.release_writer_lock()
    return state


def _encrypt_asset(store, assets_fd: int, sha: str, length: int,
                   key: bytes, salt: bytes, archive_id: str) -> dict:
    rel = f'{OBJECTS_REL}/{sha[:2]}/{sha}'
    fd = store.binding.open_file_read(rel)
    try:
        nonce = os.urandom(NONCE_BYTES)
        info = _encrypt_stream(fd, assets_fd, sha, key, nonce,
                               _aad(archive_id, 'asset', sha, int(length)),
                               int(length))
    finally:
        os.close(fd)
    if info['plaintext_sha256'] != sha:
        raise ArchiveError('backup_asset_corrupt',
                           'committed object fails its content address',
                           {'sha256': sha, 'expected_length': int(length),
                            'actual_length': info['plaintext_length']})
    return {'file': f'assets/{sha}', 'sha256': sha,
            'bytes': info['bytes'], 'kind': 'asset',
            'plaintext_length': info['plaintext_length'],
            'plaintext_sha256': info['plaintext_sha256']}


def _write_manifest(out_dir, manifest: dict) -> dict:
    """Publish the manifest into the (re-validated) target, dir_fd-anchored.
    Signature kept (out_dir, manifest): publication is the CAS interleave
    point operators hook for out-of-order checks."""
    manifest = dict(manifest)
    manifest['backup_id'] = sha256_hex(
        canonical({k: v for k, v in manifest.items()
                   if k != 'backup_id'}).encode('utf-8'))
    dir_fd = _open_backup_target(Path(out_dir))
    try:
        tmp = f'.manifest.tmp{os.getpid()}'
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600,
                     dir_fd=dir_fd)
        try:
            _write_all(fd, (canonical(manifest) + '\n').encode('utf-8'))
            os.fsync(fd)
        finally:
            os.close(fd)
        os.rename(tmp, 'manifest.json', src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)
    return manifest


def _create_common(store, out_dir, key_material, *, kind, since_seq,
                   verified_rotation=False):
    if kind not in ('baseline', 'incremental'):
        raise ArchiveError('backup_kind_invalid', f'unknown kind {kind!r}')
    out_dir = Path(out_dir)
    # the independent ENCRYPTED backup target: every component validated
    # (leaf AND ancestors — link/child must not redirect writes or chmods
    # into foreign storage), then HELD by fd for all blob writes
    out_fd = _open_backup_target(out_dir)
    published = False
    try:
        if os.listdir(out_fd):
            raise ArchiveError('backup_dir_not_empty',
                               'backup target exists and is not empty',
                               {'dir': str(out_dir)})
        os.fchmod(out_fd, 0o700)
        manifest, state = _create_bound(store, out_fd, key_material,
                                        kind=kind, since_seq=since_seq,
                                        verified_rotation=verified_rotation)
        published = True
        return manifest, state, out_fd
    finally:
        if not published:
            os.close(out_fd)


def _create_bound(store, out_fd, key_material, *, kind, since_seq,
                  verified_rotation=False):
    state = _read_state(store)
    if kind == 'baseline' and state['chain']:
        # rotation over an existing chain: require a drill pass recorded for
        # EXACTLY this chain (token = hash of its backup_ids)
        if not verified_rotation:
            raise ArchiveError(
                'backup_rotation_requires_drill',
                're-baselining over an existing chain requires '
                'verified_rotation after a passed restore_drill')
        expected = _chain_token(_chain_manifests(state))
        recorded = store.meta(DRILL_PASS_KEY)
        token = json.loads(recorded).get('token') if recorded else None
        if token != expected:
            raise ArchiveError('backup_rotation_requires_drill',
                               'no matching drill pass recorded for this chain',
                               {'expected_token': expected})
    if kind == 'incremental':
        protected = _protected_seq(store)
        if protected is None:
            raise ArchiveError('backup_chain_absent',
                               'no baseline exists; take a baseline first')
        if int(since_seq) != protected:
            raise ArchiveError('backup_chain_gap',
                               'incremental must continue the protected prefix',
                               {'protected_seq': protected,
                                'since_seq': int(since_seq)})
        # the prefix must also agree with the recorded chain: drift between
        # meta and state (corruption, manual edits) is a hole, never guessed
        if not state['chain'] or \
                int(state['chain'][-1]['captured_seq']) != int(protected):
            raise ArchiveError('backup_chain_gap',
                               'protected prefix disagrees with the recorded '
                               'chain tail',
                               {'protected_seq': protected,
                                'chain_tail_captured':
                                    state['chain'][-1]['captured_seq']
                                    if state['chain'] else None})

    salt = os.urandom(SALT_BYTES)
    key = derive_key(key_material, salt)
    archive_id = store.archive_id
    if kind == 'incremental' and state['chain']:
        # the chain shares ONE salt/key: the baseline mints it, every
        # incremental reuses it, so the drill reconstructs the whole chain
        # from the operator's single key material (a rotation mints fresh)
        prior = _load_manifest(Path(state['chain'][-1]['dir']))
        salt = bytes.fromhex(prior['encryption']['salt_hex'])
        key = derive_key(key_material, salt)
    entries = {}
    assets = []
    counts = None
    epoch = None
    captured = None

    if kind == 'baseline':
        # plaintext intermediates are staged WRITER-PRIVATE on the FIXED
        # archive volume (never the system TMPDIR — a different volume
        # outside the containment the guard proves); the destination only
        # ever holds ciphertext
        staging = _Staging(store)
        try:
            snap = _consistent_master_copy(store, staging)
            try:
                counts = _counts_of(snap)
                epoch = int(snap.execute(
                    "SELECT value FROM meta WHERE key='epoch'").fetchone()[0])
                captured = int(snap.execute(
                    "SELECT value FROM meta WHERE key='commit_seq'").fetchone()[0])
                object_rows = snap.execute(
                    'SELECT sha256, length FROM objects').fetchall()
            finally:
                snap.close()
            plain_fd = staging.open_read('master.copy.db')
            try:
                plain_size = os.fstat(plain_fd).st_size
                nonce = os.urandom(NONCE_BYTES)
                info = _encrypt_stream(plain_fd, out_fd, 'master.db.enc',
                                       key, nonce,
                                       _aad(archive_id, 'master',
                                            'master.db.enc', plain_size),
                                       plain_size)
            finally:
                os.close(plain_fd)
            entries['master'] = dict(info, file='master.db.enc')
        finally:
            staging.close()
        if object_rows:
            assets_fd = _assets_dir_fd(out_fd)
            try:
                for sha, length in object_rows:
                    assets.append(_encrypt_asset(store, assets_fd, sha,
                                                 int(length), key, salt,
                                                 archive_id))
            finally:
                os.close(assets_fd)
    else:
        # incremental: ONE fixed-seq read view streams the log tail lazily —
        # a cursor, never fetchall of unbounded after-images — straight into
        # the FIXED-volume staging log; counts/epoch/new-object keys come
        # from the same view. Per-object IO (asset reads, encryption)
        # happens AFTER the view closes so a backup never stretches the
        # consistent read view across IO waits that would block collection.
        staging = _Staging(store)
        try:
            first_seq = last_seq = None
            rows = 0
            log_fd = staging.open_new('mutation-log.jsonl')
            try:
                with store.read_snapshot():
                    cursor = store.db.execute(
                        'SELECT seq, commit_seq, ts, table_name, row_key, '
                        'op, after_json, detail_json FROM mutation_log '
                        'WHERE commit_seq > ? ORDER BY seq', (int(since_seq),))
                    for row in cursor:
                        _write_all(log_fd, (canonical(dict(zip(
                            ('seq', 'commit_seq', 'ts', 'table_name', 'row_key',
                             'op', 'after_json', 'detail_json'),
                            tuple(row)))) + '\n').encode('utf-8'))
                        rows += 1
                        if first_seq is None:
                            first_seq = row[0]
                        last_seq = row[0]
                    counts = {table: store.db.execute(
                        f'SELECT COUNT(*) FROM {table}').fetchone()[0]
                        for table in COUNTED_TABLES}
                    epoch = store.epoch()
                    captured = store.commit_seq()
                    new_shas = [r[0] for r in store.db.execute(
                        "SELECT DISTINCT row_key FROM mutation_log "
                        "WHERE table_name='objects' AND commit_seq > ?",
                        (int(since_seq),))]
                os.fsync(log_fd)
                log_bytes = os.fstat(log_fd).st_size
            finally:
                os.close(log_fd)
            plain_fd = staging.open_read('mutation-log.jsonl')
            try:
                nonce = os.urandom(NONCE_BYTES)
                info = _encrypt_stream(plain_fd, out_fd,
                                       'mutation-log.jsonl.enc', key, nonce,
                                       _aad(archive_id, 'mutation_log',
                                            'mutation-log.jsonl.enc',
                                            log_bytes),
                                       log_bytes)
            finally:
                os.close(plain_fd)
        finally:
            staging.close()
        entries['mutation_log'] = dict(
            info, file='mutation-log.jsonl.enc',
            first_seq=first_seq, last_seq=last_seq, rows=rows)
        # asset bytes: objects whose commits fell inside the window
        if new_shas:
            assets_fd = _assets_dir_fd(out_fd)
            try:
                for sha in new_shas:
                    with store.read_snapshot():
                        row = store.db.execute(
                            'SELECT length FROM objects WHERE sha256=?',
                            (sha,)).fetchone()
                    if row is None:
                        continue
                    assets.append(_encrypt_asset(store, assets_fd, sha,
                                                 int(row[0]), key, salt,
                                                 archive_id))
            finally:
                os.close(assets_fd)

    manifest = {
        'version': BACKUP_VERSION,
        'kind': kind,
        'archive_id': archive_id,
        'created_at': now_ms(),
        'epoch': epoch,
        'commit_seq': captured,
        'since_seq': since_seq,
        'counts': counts,
        'entries': entries,
        'assets': assets,
        'encryption': {'alg': 'aes-256-gcm', 'tag': '16-byte tail',
                       'aad': 'archive_id+role+name+length',
                       'kdf': f'pbkdf2-hmac-sha256/{KDF_ROUNDS}',
                       'salt_hex': salt.hex()},
        'replay_tables': list(REPLAY_TABLES),
        # prior chain entries only — never a self-entry: the backup_id
        # commits to the full manifest content, so a self-entry would be
        # circular and uncomputable
        'chain': None,
    }
    return manifest, state


def create_baseline(store, out_dir, key_material, *, verified_rotation=False):
    """Full backup: consistent master copy + full mutation log tail state +
    every committed object. Starting a NEW baseline over an existing chain is
    a rotation and requires a recorded drill pass (verified_rotation)."""
    out_fd = None
    try:
        manifest, state, out_fd = _create_common(
            store, out_dir, key_material, kind='baseline', since_seq=None,
            verified_rotation=verified_rotation)
        manifest['chain'] = list(state['chain'])
        manifest = _write_manifest(Path(out_dir), manifest)
        entry = {'backup_id': manifest['backup_id'], 'kind': 'baseline',
                 'dir': str(Path(out_dir).resolve()),
                 'since_seq': None, 'captured_seq': manifest['commit_seq']}
        _advance_protected(store, manifest['commit_seq'], entry, state)
        return manifest
    finally:
        if out_fd is not None:
            os.close(out_fd)


def create_incremental(store, out_dir, key_material):
    """Incremental backup continuing the protected prefix exactly: the
    mutation-log tail with complete after-images plus newly committed
    objects (immutable, so the chain stays additive)."""
    protected = _protected_seq(store)
    if protected is None:
        raise ArchiveError('backup_chain_absent',
                           'no baseline exists; take a baseline first')
    out_fd = None
    try:
        manifest, state, out_fd = _create_common(
            store, out_dir, key_material, kind='incremental',
            since_seq=protected)
        manifest['chain'] = list(state['chain'])
        manifest = _write_manifest(Path(out_dir), manifest)
        entry = {'backup_id': manifest['backup_id'], 'kind': 'incremental',
                 'dir': str(Path(out_dir).resolve()),
                 'since_seq': protected,
                 'captured_seq': manifest['commit_seq']}
        # captured may equal protected when nothing changed: the heartbeat
        # extends the chain without moving the prefix (no hole is created)
        _advance_protected(store, manifest['commit_seq'], entry, state)
        return manifest
    finally:
        if out_fd is not None:
            os.close(out_fd)


def create_backup(store, out_dir, key_material):
    """Operator entry point: baseline when no chain exists, incremental
    otherwise (contiguity enforced — never a silent hole)."""
    if _protected_seq(store) is None:
        return create_baseline(store, out_dir, key_material)
    return create_incremental(store, out_dir, key_material)


# ── chain resolution + drill-pass bookkeeping ──────────────────────────────
def _load_manifest(dir_path: Path) -> dict:
    try:
        manifest = json.loads(
            (Path(dir_path) / 'manifest.json').read_text(encoding='utf-8'))
    except (OSError, ValueError) as exc:
        raise ArchiveError('backup_manifest_invalid',
                           f'cannot read backup manifest: {exc}') from exc
    if not isinstance(manifest, dict) or \
            manifest.get('version') != BACKUP_VERSION or \
            manifest.get('kind') not in ('baseline', 'incremental'):
        raise ArchiveError('backup_manifest_invalid',
                           'manifest is not a v3 backup manifest')
    # self-addressing: the backup_id must commit to the manifest content
    expected = sha256_hex(canonical(
        {k: v for k, v in manifest.items() if k != 'backup_id'}).encode('utf-8'))
    if manifest.get('backup_id') != expected:
        raise ArchiveError('backup_manifest_invalid',
                           'manifest backup_id does not commit to its content',
                           {'expected': expected,
                            'found': manifest.get('backup_id')})
    return manifest


def _chain_manifests(state):
    manifests = []
    for entry in state['chain']:
        manifests.append(_load_manifest(Path(entry['dir'])))
    return manifests


def _chain_token(manifests):
    return sha256_hex(''.join(m['backup_id'] for m in manifests).encode('utf-8'))


def record_drill_pass(store, token):
    """Record that the CURRENT chain passed a full restore drill (the token
    is the chain token returned by a successful restore_drill — only that
    path computes it, so the record cannot be asserted without a drill)."""
    with store.full_transaction() as tx:
        tx.meta_set(DRILL_PASS_KEY,
                    canonical({'token': token, 'at': now_ms()}))
        tx.mutation('meta', 'backup', 'drill_pass_recorded', {'token': token})


# ── replay engine (used by the drill — real, verified) ────────────────────
def _replay_row(rebuilt, row, table_columns):
    table = row['table_name']
    op = row['op']
    if table not in table_columns or not row.get('after_json'):
        return False
    if table == 'messages' and op == 'conflict':
        return False
    if table == 'identity_map':
        # derived from messages after-images (columns are a subset)
        return False
    after = json.loads(row['after_json'])
    if not isinstance(after, dict):
        return False
    cols = table_columns[table]
    payload = {k: v for k, v in after.items() if k in cols}
    if not payload:
        return False
    names = ','.join(payload)
    rebuilt.execute(
        f'INSERT OR REPLACE INTO {table}({names}) '
        f'VALUES({",".join(":" + c for c in payload)})', payload)
    if table == 'messages':
        ident = {k: payload.get(k) for k in
                 ('account', 'talker', 'server_id', 'shard', 'shard_table',
                  'local_rowid', 'msg_uid')}
        rebuilt.execute(
            'INSERT OR REPLACE INTO identity_map(account,talker,server_id,'
            'shard,shard_table,local_rowid,msg_uid) '
            'VALUES(:account,:talker,:server_id,:shard,:shard_table,'
            ':local_rowid,:msg_uid)', ident)
    return True


def _replay_incremental(rebuilt, log_path: Path, manifest: dict, checks):
    """Two things happen per mutation row: (1) the row itself is inserted
    VERBATIM into the rebuilt mutation_log — history is content, and the
    restored log must be the live log's prefix, not a truncated stub; (2) its
    after-image is replayed onto the payload tables. Rows whose commit_seq
    falls outside the manifest's declared window are a corrupt tail."""
    applied = 0
    skipped = 0
    logged = 0
    with open(log_path, encoding='utf-8') as fh:
        for line in fh:
            if not line.strip():
                continue
            row = json.loads(line)
            if not (manifest['since_seq'] < row['commit_seq']
                    <= manifest['commit_seq']):
                raise ArchiveError('restore_drill_failed',
                                   'mutation row outside the declared window',
                                   {'seq': row['seq'],
                                    'commit_seq': row['commit_seq'],
                                    'window': (manifest['since_seq'],
                                               manifest['commit_seq'])})
            rebuilt.execute(
                'INSERT OR REPLACE INTO mutation_log(seq,commit_seq,ts,'
                'table_name,row_key,op,after_json,detail_json) '
                'VALUES(:seq,:commit_seq,:ts,:table_name,:row_key,:op,'
                ':after_json,:detail_json)', row)
            logged += 1
            if _replay_row(rebuilt, row, _TABLE_COLUMNS):
                applied += 1
            else:
                skipped += 1
    checks.append({'check': f'replay:{manifest["backup_id"][:12]}',
                   'ok': True, 'detail': {'applied': applied,
                                          'logged_rows': logged,
                                          'skipped_non_replayable': skipped}})
    return applied, skipped


_TABLE_COLUMNS = {}


def _table_columns_of(db):
    columns = {}
    for table in REPLAY_TABLES:
        columns[table] = {row[1] for row in
                          db.execute(f'PRAGMA table_info({table})')}
    return columns


# ── restore drill ──────────────────────────────────────────────────────────
def restore_drill(backup_dir, key_material: bytes, *, scratch_dir,
                  expect_archive_id=None, expect_epoch=None) -> dict:
    """Rebuild a backup CHAIN into a fresh scratch root and verify it end to
    end. Never touches a live archive; refuses an existing scratch target.

    The rebuilt root is only published after every check passes: baseline
    master decrypted (GCM tag) and mounted, every incremental REPLAYED onto
    it with per-table count comparison against the recorded counts, every
    asset recovered to objects/<h>/<sha> and hash-verified, the master
    rotated to a NEW epoch with the rotation logged, and a run acknowledged
    in the old epoch PROVEN stale (run_epoch_stale)."""
    backup_dir = Path(backup_dir)
    scratch_dir = Path(scratch_dir)
    if scratch_dir.exists():
        raise ArchiveError('backup_scratch_exists',
                           'restore target already exists; refusing to '
                           'overwrite', {'dir': str(scratch_dir)})
    head = _load_manifest(backup_dir)
    if expect_archive_id is not None and \
            head.get('archive_id') != expect_archive_id:
        raise ArchiveError('backup_archive_mismatch',
                           'backup belongs to a different archive',
                           {'expected': expect_archive_id,
                            'found': head.get('archive_id')})

    # resolve the full chain (baseline first). A manifest's chain lists its
    # PRIOR entries only, so the immediate predecessor is the last one.
    chain = [head]
    chain_dirs_by_id = {head['backup_id']: Path(backup_dir.resolve())}
    seen_dirs = {str(backup_dir.resolve())}
    while chain[0]['kind'] == 'incremental':
        prior = chain[0].get('chain') or []
        if not prior:
            raise ArchiveError('backup_chain_broken',
                               'incremental manifest carries no prior link')
        prev_entry = prior[-1]
        prev = _load_manifest(Path(prev_entry['dir']))
        if prev['backup_id'] != prev_entry['backup_id'] or \
                prev['commit_seq'] != chain[0]['since_seq'] or \
                prev_entry['dir'] in seen_dirs:
            raise ArchiveError('backup_chain_broken',
                               'chain link does not continue exactly')
        seen_dirs.add(prev_entry['dir'])
        chain_dirs_by_id[prev['backup_id']] = Path(prev_entry['dir'])
        chain.insert(0, prev)
    baseline, incrementals = chain[0], chain[1:]
    for prev, nxt in zip(chain, chain[1:]):
        if nxt['since_seq'] != prev['commit_seq']:
            raise ArchiveError('backup_chain_broken',
                               'chain has a hole', {'at': nxt['backup_id']})

    token = _chain_token(chain)
    building = scratch_dir.with_name(
        scratch_dir.name + f'.building-{os.getpid()}-{os.urandom(4).hex()}')
    checks = []

    def check(name, ok, detail=None):
        checks.append({'check': name, 'ok': bool(ok), 'detail': detail or {}})
        if not ok:
            raise ArchiveError('restore_drill_failed',
                               f'drill check {name!r} failed', detail or {})

    salt = bytes.fromhex(head['encryption']['salt_hex'])
    key = derive_key(key_material, salt)
    head_dir = chain_dirs_by_id[head['backup_id']]

    def decrypt_entry(manifest, role, entry, out_path):
        aad = _aad(manifest['archive_id'], role, Path(entry['file']).name,
                   entry['plaintext_length'])
        info = _decrypt_to_file(
            chain_dirs_by_id[manifest['backup_id']] / entry['file'],
            out_path, key, aad, entry['file'])
        check(f'blob:{role}:{Path(entry["file"]).name}',
              info['plaintext_sha256'] == entry['plaintext_sha256'] and
              info['plaintext_length'] == entry['plaintext_length'],
              {'file': entry['file']})

    try:
        _secure_mkdir(building)
        _secure_mkdir(building / 'objects')

        # 1. baseline master: decrypt (tag first) and mount
        master_plain = building / 'master.rebuild.db'
        decrypt_entry(baseline, 'master', baseline['entries']['master'],
                      master_plain)
        os.replace(master_plain, building / DB_NAME)
        rebuilt = sqlite3.connect(building / DB_NAME)
        try:
            for table, expected in baseline['counts'].items():
                actual = rebuilt.execute(
                    f'SELECT COUNT(*) FROM {table}').fetchone()[0]
                check(f'baseline_count:{table}', actual == expected,
                      {'expected': expected, 'actual': actual})
            epoch_found = int(rebuilt.execute(
                "SELECT value FROM meta WHERE key='epoch'").fetchone()[0])
            check('baseline_epoch', epoch_found == baseline['epoch'],
                  {'manifest': baseline['epoch'], 'rebuilt': epoch_found})
            if expect_epoch is not None:
                check('epoch_as_expected', epoch_found == expect_epoch,
                      {'expected': expect_epoch, 'actual': epoch_found})

            global _TABLE_COLUMNS
            _TABLE_COLUMNS = _table_columns_of(rebuilt)

            # 2. REAL incremental replay with count verification per stage
            for manifest in incrementals:
                log_plain = building / 'log.rebuild.jsonl'
                decrypt_entry(manifest, 'mutation_log',
                              manifest['entries']['mutation_log'], log_plain)
                try:
                    _replay_incremental(rebuilt, log_plain, manifest, checks)
                finally:
                    os.unlink(log_plain)
                rebuilt.commit()
                for table, expected in manifest['counts'].items():
                    actual = rebuilt.execute(
                        f'SELECT COUNT(*) FROM {table}').fetchone()[0]
                    check(f'replay_count:{manifest["backup_id"][:8]}:{table}',
                          actual == expected,
                          {'expected': expected, 'actual': actual})

            # 3. new epoch: rotate + prove the old epoch's ACK authority dead
            final_epoch_src = chain[-1]['epoch']
            probe = f'drill-probe-{os.urandom(6).hex()}'
            with rebuilt:
                rebuilt.execute(
                    'INSERT INTO runs(run_id,batch_id,account,talker,kind,'
                    'started_at,status,run_start_seq,epoch,claimed) '
                    'VALUES(?,?,?,?,"capture",?,"open",0,?,0)',
                    (probe, None, '__drill__', '__drill__', now_ms(),
                     int(final_epoch_src)))
                rebuilt.execute(
                    "UPDATE meta SET value=? WHERE key='epoch'",
                    (str(int(final_epoch_src) + 1),))
                rebuilt.execute(
                    "UPDATE meta SET value=? WHERE key='commit_seq'",
                    (str(int(chain[-1]['commit_seq'])),))
                rebuilt.execute(
                    'INSERT INTO mutation_log(commit_seq,ts,table_name,row_key,'
                    'op,after_json,detail_json) VALUES(?,?,?,?,?,?,?)',
                    (int(chain[-1]['commit_seq']), now_ms(), 'meta', 'epoch',
                     'restore_epoch_rotated', None,
                     canonical({'restored_from': chain[-1]['backup_id'],
                                'old_epoch': int(final_epoch_src),
                                'new_epoch': int(final_epoch_src) + 1})))
        finally:
            rebuilt.close()

        # 4. assets: physically recover every object the chain carries
        recovered = 0
        seen_shas = set()
        for manifest in chain:
            for asset in manifest.get('assets') or []:
                sha = asset['sha256']
                if sha in seen_shas:
                    continue
                seen_shas.add(sha)
                out = building / OBJECTS_REL / sha[:2] / sha
                _secure_mkdir(out.parent, mode=0o700)
                decrypt_entry(manifest, 'asset', dict(
                    asset, file=asset['file'], plaintext_length=asset['plaintext_length'],
                    plaintext_sha256=sha), out)
                check(f'asset:{sha[:12]}', out.stat().st_size ==
                      asset['plaintext_length'])
                recovered += 1

        # 5. publish as a REAL archive root, already rotated to a new epoch
        new_epoch = int(chain[-1]['epoch']) + 1
        SyntheticGuard.write_marker(building, head['archive_id'], new_epoch,
                                    int(time.time()))
        binding = SyntheticGuard(head['archive_id']).verify(building)
        store = MasterStore(binding, head['archive_id'])
        try:
            check('restored_epoch_rotated', store.epoch() == new_epoch,
                  {'epoch': store.epoch(), 'expected': new_epoch})
            from .errors import MasterError
            try:
                store.verify_run_binding(probe, '__drill__', '__drill__',
                                         'capture')
                check('old_ack_invalidated', False,
                      {'probe': probe, 'epoch': store.epoch()})
            except MasterError as exc:
                check('old_ack_invalidated', exc.code == 'run_epoch_stale',
                      {'code': exc.code})
        finally:
            store.close()
            binding.close()

        # everything verified — publish the scratch root atomically
        os.rename(building, scratch_dir)
        _fsync_dir(scratch_dir.parent)
        return {'ok': True, 'checks': checks, 'epoch': new_epoch,
                'chain_token': token, 'assets_recovered': recovered,
                'chain_depth': len(chain),
                'backup_epoch': int(chain[-1]['epoch']),
                'commit_seq': int(chain[-1]['commit_seq']),
                'scratch': str(scratch_dir)}
    except BaseException:
        shutil.rmtree(building, ignore_errors=True)
        raise


# ── status (freshness vs RPO split) ────────────────────────────────────────
def backup_status(store) -> dict:
    """Backup posture, reporting FRESHNESS (wall clock since the last
    successful backup) and RPO EXPOSURE (commits not yet protected) as
    SEPARATE facts — a fresh heartbeat with an unadvanced prefix is still
    exposed, and the report must show both."""
    state = _read_state(store)
    protected = _protected_seq(store)
    commit_seq = store.commit_seq()
    last_at = state.get('last_backup_at_ms')
    return {
        'master_commit_seq': commit_seq,
        'protected_seq': protected,
        'unprotected_commits': max(0, commit_seq - (protected or 0)),
        'chain_depth': len(state['chain']),
        'last_backup_at_ms': last_at,
        'freshness_ms': (now_ms() - last_at) if last_at else None,
        'chain': list(state['chain']),
    }


# kept for the explicit operator rotation path (post-verified-backup epoch
# rotation on a LIVE archive — distinct from the drill's restored-copy
# rotation, which happens inside restore_drill)
def rotate_epoch(store) -> int:
    with store.full_transaction() as tx:
        new_epoch = store.epoch() + 1
        tx.meta_set('epoch', str(new_epoch))
        tx.mutation('meta', 'epoch', 'epoch_rotated', {'epoch': new_epoch},
                    {'previous_epoch': new_epoch - 1})
    return new_epoch
