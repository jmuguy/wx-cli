"""v3 master store — the single-writer archive database on the NAS.

Everything durable lives here: epoch/commit_seq, messages with visibility and
provenance, revisions, media ledger (assets + needs), missing observations,
the row-level mutation log (replayable for backup), batch/receipt bookkeeping,
source observation summaries, run records, and scope grants.

Durability discipline:
- exactly one writer process at a time (advisory flock on writer.lock, opened
  through the guard binding);
- every mutation batch runs in one FULL-synchronous IMMEDIATE transaction that
  also stamps commit_seq and writes data + receipt + row-level mutation log
  atomically;
- the guard binding is re-validated (check_alive + check_db_path) before every
  transaction — checks are continuous, not startup-only;
- open refuses to auto-create: `initialize()` is a separate explicit path
  (production never auto-initialises an empty archive).
"""
from __future__ import annotations

import fcntl
import os
import re
import secrets
import sqlite3
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import quote

from . import SCHEMA_VERSION
from .errors import AuthError, GuardError, MasterError
from .util import canonical, now_ms
from .vfs import register_pinned_vfs, release_pinned_vfs

DB_NAME = 'master.db'
WRITER_LOCK = 'writer.lock'

BATCH_PENDING = 'pending'
BATCH_COMMITTED = 'committed'
BATCH_ABORTED = 'aborted'
BATCH_REJECTED = 'rejected'

_TOKEN_RE = re.compile(rb'v3-[0-9a-f]{32}')


def _scan_tokens_from_fd(fd):
    """Stream-scan a descriptor for token-shaped byte strings (read-only).
    Overlap keeps a token split across block boundaries findable."""
    found = set()
    offset = 0
    tail = b''
    while True:
        block = os.pread(fd, 1 << 20, offset)
        if not block:
            return found
        found.update(m.group(0) for m in _TOKEN_RE.finditer(tail + block))
        tail = (tail + block)[-40:]
        offset += len(block)

_SCHEMA = [
    'CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)',
    '''CREATE TABLE IF NOT EXISTS identity_map (
      account TEXT NOT NULL, talker TEXT NOT NULL,
      server_id TEXT, shard TEXT, shard_table TEXT, local_rowid INTEGER,
      msg_uid INTEGER NOT NULL,
      UNIQUE(msg_uid))''',
    'CREATE UNIQUE INDEX IF NOT EXISTS identity_server ON identity_map'
    '(account,talker,server_id) WHERE server_id IS NOT NULL',
    # local identity = database shard + table + rowid (contract v2): the same
    # Msg_<namehash> table exists in multiple message_N.db shards, so a local
    # (table,rowid) pair alone is NOT unique across the source
    'CREATE UNIQUE INDEX IF NOT EXISTS identity_local ON identity_map'
    '(account,talker,shard,shard_table,local_rowid) WHERE server_id IS NULL',
    '''CREATE TABLE IF NOT EXISTS messages (
      msg_uid INTEGER PRIMARY KEY,
      account TEXT NOT NULL, talker TEXT NOT NULL,
      server_id TEXT, shard TEXT, shard_table TEXT, local_rowid INTEGER,
      create_time INTEGER NOT NULL, sort_seq INTEGER NOT NULL,
      msg_type INTEGER NOT NULL, sub_type INTEGER NOT NULL DEFAULT 0,
      status INTEGER NOT NULL DEFAULT 0, direction TEXT,
      sender_id TEXT, sender_display_name TEXT,
      content_text TEXT NOT NULL DEFAULT '',
      content_json TEXT NOT NULL DEFAULT '{}',
      packed_info_sha256 TEXT,
      record_sha256 TEXT,
      raw_record_json TEXT NOT NULL DEFAULT '{}',
      media_refs_json TEXT NOT NULL DEFAULT '[]',
      provenance TEXT NOT NULL,
      visibility TEXT NOT NULL DEFAULT 'visible',
      has_revisions INTEGER NOT NULL DEFAULT 0,
      first_commit_seq INTEGER NOT NULL,
      last_content_or_presence_commit_seq INTEGER NOT NULL,
      deleted_at INTEGER)''',
    'CREATE INDEX IF NOT EXISTS messages_window ON messages'
    '(account,talker,create_time,sort_seq)',
    '''CREATE TABLE IF NOT EXISTS message_revisions (
      revision_id INTEGER PRIMARY KEY,
      msg_uid INTEGER NOT NULL,
      seq INTEGER NOT NULL,
      change_kind TEXT NOT NULL,
      observed_at INTEGER NOT NULL,
      payload_json TEXT NOT NULL,
      commit_seq INTEGER NOT NULL,
      UNIQUE(msg_uid, seq))''',
    '''CREATE TABLE IF NOT EXISTS media_assets (
      sha256 TEXT PRIMARY KEY, asset_id TEXT NOT NULL, kind TEXT NOT NULL,
      length INTEGER NOT NULL, state TEXT NOT NULL,
      first_batch TEXT, created_commit_seq INTEGER)''',
    '''CREATE TABLE IF NOT EXISTS media_needs (
      need_id INTEGER PRIMARY KEY,
      msg_uid INTEGER NOT NULL, ref_key TEXT NOT NULL, ref_kind TEXT NOT NULL,
      state TEXT NOT NULL,
      first_seen_run TEXT, last_error TEXT,
      opened_commit_seq INTEGER, resolved_commit_seq INTEGER,
      UNIQUE(msg_uid, ref_key))''',
    '''CREATE TABLE IF NOT EXISTS missing_observations (
      obs_id INTEGER PRIMARY KEY,
      msg_uid INTEGER, account TEXT NOT NULL, talker TEXT NOT NULL,
      identity_json TEXT NOT NULL,
      kind TEXT NOT NULL, soft INTEGER NOT NULL,
      run_id TEXT NOT NULL,
      first_observed_commit_seq INTEGER NOT NULL,
      resolved_commit_seq INTEGER,
      detail_json TEXT)''',
    '''CREATE TABLE IF NOT EXISTS mutation_log (
      seq INTEGER PRIMARY KEY AUTOINCREMENT,
      commit_seq INTEGER NOT NULL, ts INTEGER NOT NULL,
      table_name TEXT NOT NULL, row_key TEXT NOT NULL, op TEXT NOT NULL,
      after_json TEXT, detail_json TEXT)''',
    '''CREATE TABLE IF NOT EXISTS batches (
      batch_id TEXT PRIMARY KEY,
      content_sha256 TEXT NOT NULL,
      account TEXT NOT NULL, talker TEXT NOT NULL, run_id TEXT NOT NULL,
      state TEXT NOT NULL,
      created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL,
      committed_at INTEGER, commit_seq INTEGER,
      receipt_json TEXT, error_code TEXT, error_detail_json TEXT)''',
    '''CREATE TABLE IF NOT EXISTS runs (
      run_id TEXT PRIMARY KEY,
      batch_id TEXT, account TEXT, talker TEXT,
      kind TEXT NOT NULL, started_at INTEGER NOT NULL, finished_at INTEGER,
      status TEXT NOT NULL, run_start_seq INTEGER NOT NULL,
      epoch INTEGER NOT NULL,
      claimed INTEGER NOT NULL DEFAULT 0,
      stats_json TEXT)''',
    '''CREATE TABLE IF NOT EXISTS source_observations (
      account TEXT NOT NULL, talker TEXT NOT NULL,
      run_id TEXT NOT NULL, observed_at INTEGER NOT NULL,
      commit_seq INTEGER NOT NULL, observation_json TEXT NOT NULL,
      PRIMARY KEY(account, talker))''',
    '''CREATE TABLE IF NOT EXISTS scope_grants (
      account TEXT NOT NULL, talker TEXT NOT NULL,
      capture_allowed INTEGER NOT NULL, query_allowed INTEGER NOT NULL,
      granted_at INTEGER NOT NULL, revoked_at INTEGER,
      revoke_effective_seq INTEGER,
      PRIMARY KEY(account, talker))''',
    '''CREATE TABLE IF NOT EXISTS objects (
      sha256 TEXT PRIMARY KEY, kind TEXT NOT NULL, length INTEGER NOT NULL,
      state TEXT NOT NULL, first_batch TEXT,
      created_at INTEGER, committed_at INTEGER)''',
    "CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5("
    "content_text, content='messages', content_rowid='msg_uid', "
    "tokenize='trigram')",
    # keep FTS in lockstep with messages inside every FULL transaction.
    # The fts column MUST be named exactly like the external content
    # table's column (content_text): fts5 resolves MATCH reads through
    # messages.<column> — a mismatched name makes every query fail with
    # 'no such column: T.content'.
    '''CREATE TRIGGER IF NOT EXISTS messages_fts_ins AFTER INSERT ON messages BEGIN
      INSERT INTO messages_fts(rowid, content_text) VALUES (new.msg_uid, new.content_text);
    END''',
    '''CREATE TRIGGER IF NOT EXISTS messages_fts_del AFTER DELETE ON messages BEGIN
      INSERT INTO messages_fts(messages_fts, rowid, content_text)
      VALUES ('delete', old.msg_uid, old.content_text);
    END''',
    '''CREATE TRIGGER IF NOT EXISTS messages_fts_upd AFTER UPDATE OF content_text ON messages BEGIN
      INSERT INTO messages_fts(messages_fts, rowid, content_text)
      VALUES ('delete', old.msg_uid, old.content_text);
      INSERT INTO messages_fts(rowid, content_text) VALUES (new.msg_uid, new.content_text);
    END''',
]


class MasterStore:
    """All access goes through the guard binding; path binding is re-checked
    before every transaction (continuous verification)."""

    def __init__(self, binding, archive_id, readonly=False, create=False):
        self.binding = binding
        self.archive_id = archive_id
        self.readonly = readonly
        self._lock_fd = None
        self._schema_creating = False
        binding.check_alive()
        if create:
            # WE create the empty file through the binding (never sqlite):
            # explicit init only, identity pinned for prove_db immediately.
            try:
                fd = binding._open_chain(DB_NAME, os.O_WRONLY | os.O_CREAT | os.O_EXCL
                                         | os.O_NOFOLLOW, 0o600)
            except GuardError as exc:
                if exc.code != 'path_exists':
                    raise
                raise MasterError('master_exists',
                                  'master.db already exists; refusing to re-init')
            else:
                os.fsync(fd)
                os.close(fd)
        else:
            try:
                binding.prove_db(DB_NAME)
            except GuardError as exc:
                if exc.code != 'path_missing':
                    raise
                raise MasterError(
                    'master_missing',
                    'master.db not present under archive root; '
                    'explicit init required (no auto-initialisation)') from exc
        if create:
            # pin the freshly created file before sqlite ever touches it
            binding.prove_db(DB_NAME)
        # HANDLE ANCHORING — see the discipline block below the __init__.
        # Linux: pinned VFS so every sqlite path (main/-wal/-shm/-journal)
        # resolves through the HELD root fd. Registration failure PROPAGATES —
        # there is NO fallback on Linux (spec: an unprovable handle must
        # reject, synthetic tier included; a try/except → None here would let
        # an unbound connection through — that exact hole failed the Codex
        # gate test_linux_vfs_registration_failure_never_falls_back).
        # Other platforms: production rejects; the synthetic tier falls back
        # to a read-only init_token identity proof.
        self.vfs = None
        if sys.platform.startswith('linux'):
            self.vfs = register_pinned_vfs(binding.root_fd, DB_NAME)
        elif binding.tier == 'production':
            raise MasterError(
                'master_handle_unproven',
                'production requires the pinned sqlite VFS (Linux '
                'procfs); this platform cannot anchor the database '
                'handle — refusing rather than falling back')
        # Synthetic tier (no pinned VFS): read the archive bytes and extract
        # the init_token BEFORE sqlite connects. This is the only safe moment
        # to touch the db file — POSIX fcntl locks are process-level, so an
        # open+close after connect would strip the connection's locks.
        disk_tokens = None
        if self.vfs is None and not create:
            disk_tokens = self._tokens_from_disk(binding)
            if not disk_tokens:
                raise MasterError(
                    'master_handle_unproven',
                    'init_token absent from archive file bytes; handle '
                    'identity cannot be proven on this platform')
        self.db = None
        try:
            if self.vfs is not None:
                db_uri = f'file:{DB_NAME}?vfs={self.vfs.name}&mode=rw'
                self.db = sqlite3.connect(db_uri, uri=True,
                                          isolation_level=None, timeout=10.0)
                self.db.row_factory = sqlite3.Row
                # prove the pinned VFS is actually in use before any read
                self._prove_anchored()
            else:
                db_path = str(Path(binding.root) / DB_NAME)
                db_uri = 'file:' + quote(db_path) + '?mode=rw'
                self.db = sqlite3.connect(db_uri, uri=True,
                                          isolation_level=None, timeout=10.0)
                self.db.row_factory = sqlite3.Row
            if create:
                self._create_schema()
            else:
                if self.vfs is None:
                    self._verify_token_proof(disk_tokens)
                self._verify_meta()
            self.db.execute('PRAGMA journal_mode=WAL')
            self.db.execute('PRAGMA synchronous=FULL')
            self.db.execute('PRAGMA busy_timeout=10000')
            self.db.execute('PRAGMA foreign_keys=ON')
            if readonly:
                self.db.execute('PRAGMA query_only=ON')
        except BaseException:
            if self.db is not None:
                try:
                    self.db.close()
                except sqlite3.Error:
                    pass
            release_pinned_vfs(self.vfs)
            self.vfs = None
            raise

    # ── sqlite handle anchoring ──────────────────────────────────────
    # Two earlier proof schemes were WITHDRAWN after independent Linux
    # verification (resume2-linux-binding-probe.log):
    #   * scanning /proc/self/fd for any descriptor on the pinned inode is
    #     spoofable by an unrelated open of the original file — the scan
    #     proves nothing about which file the CONNECTION opened;
    #   * a write-through probe writes into the wrong (replacement)
    #     database before detecting it and breaks read-only observation
    #     semantics.
    # What ships:
    #   * Linux: the pinned VFS (vfs.py). xFullPathname is ours, so the
    #     default VFS's canonicalization (which resolves /proc/self/fd
    #     paths back to swappable absolute paths) never runs. Every name —
    #     main db, -wal, -shm, -journal, temp spills — resolves through the
    #     HELD root fd, so a path swap cannot redirect the connection.
    #     Re-proved per transaction via PRAGMA database_list.
    #   * other platforms: production REJECTS (unprovable); the synthetic
    #     test tier uses the READ-ONLY init_token identity proof — bytes are
    #     read through the binding BEFORE connect (never after: touching the
    #     leaf post-connect would drop the connection's fcntl locks) and the
    #     connection's token must match (residual: a byte-identical clone
    #     passes — synthetic tier only).
    def _prove_anchored(self):
        """The connection must report the anchored name. This is the runtime
        proof that the pinned VFS is in use: a vfs= URI that silently failed
        (e.g. registered in a second copy of libsqlite3) would degrade to the
        default VFS, whose database_list reports a normalized real path."""
        row = self.db.execute('PRAGMA database_list').fetchone()
        reported = row[2] if row else None
        if self.vfs is None or reported != self.vfs.anchored_main():
            raise MasterError(
                'master_handle_unproven',
                'sqlite connection is not anchored to the held root fd',
                {'reported': reported,
                 'expected': self.vfs.anchored_main() if self.vfs else None})

    def _tokens_from_disk(self, binding):
        """Pre-connect read of the archive file through the binding chain.
        MUST run before sqlite3.connect: afterwards, an open+close of the db
        leaf would drop the connection's process-level POSIX fcntl locks."""
        fd = binding.open_file_read(DB_NAME)
        try:
            return _scan_tokens_from_fd(fd)
        finally:
            os.close(fd)

    def _verify_token_proof(self, disk_tokens):
        """Post-connect half of the synthetic-tier proof: the init_token the
        connection reports must be one of the tokens extracted from the
        archive bytes read through the binding. A connect-time path swap
        (ABA) lands the connection on a different file whose token differs —
        rejected before any read or write is served."""
        try:
            row = self.db.execute(
                "SELECT value FROM meta WHERE key='init_token'").fetchone()
        except sqlite3.Error as exc:
            raise MasterError('master_handle_unproven',
                              f'init_token unreadable through connection: {exc}')
        if row is None:
            raise MasterError('master_handle_unproven',
                              'archive lacks init_token')
        token = row[0].encode('utf-8')
        if token not in (disk_tokens or ()):
            raise MasterError(
                'master_handle_unproven',
                'init_token through the connection is not the token in the '
                'archive bytes: the connection is not the archive database')

    # ── lifecycle ────────────────────────────────────────────────────
    @classmethod
    def initialize(cls, binding, archive_id, scope, anomaly=None, epoch=1):
        """Explicit init path — never invoked by open()."""
        binding.check_alive()
        try:
            binding.stat_path(DB_NAME)
        except GuardError as exc:
            if exc.code != 'path_missing':
                raise
        else:
            raise MasterError('master_exists',
                              'master.db already exists; refusing to re-init')
        store = cls(binding, archive_id, create=True)
        try:
            with store.full_transaction() as tx:
                tx.meta_set('schema_version', str(SCHEMA_VERSION))
                tx.meta_set('archive_id', archive_id)
                tx.meta_set('epoch', str(epoch))
                tx.meta_set('commit_seq', '0')
                tx.meta_set('created_at', str(int(time.time())))
                for account, talkers in scope.items():
                    for talker, grants in talkers.items():
                        # Missing policy is DENY for both purposes: a
                        # capture-only grant must never implicitly grant AI
                        # query access; the receiving side re-checks anyway.
                        store.db.execute(
                            'INSERT INTO scope_grants VALUES(?,?,?,?,?,NULL,NULL)',
                            (account, talker, int(grants.get('capture', 0)),
                             int(grants.get('query', 0)), int(time.time())))
                anomaly_cfg = anomaly or {}
                tx.meta_set('anomaly_missing_abs_min',
                            str(anomaly_cfg.get('missing_abs_min', 20)))
                tx.meta_set('anomaly_missing_pct',
                            str(anomaly_cfg.get('missing_pct', 10)))
                tx.meta_set('anomaly_missing_hard',
                            str(anomaly_cfg.get('missing_hard', 500)))
                # durable random identity token for the read-only handle
                # proof on platforms without procfs (never rotates)
                tx.meta_set('init_token', 'v3-' + secrets.token_hex(16))
        except BaseException:
            store.close()
            raise
        # checkpoint so init_token lives in the MAIN file, not just the WAL
        store.db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        return store

    def _create_schema(self):
        self._schema_creating = True
        try:
            with self.full_transaction():
                for stmt in _SCHEMA:
                    self.db.execute(stmt)
        finally:
            self._schema_creating = False

    def _verify_meta(self):
        row = self.db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='meta'"
        ).fetchone()
        if not row:
            raise MasterError('master_invalid', 'master.db has no meta table')
        version = self._meta_get('schema_version')
        if version != str(SCHEMA_VERSION):
            raise MasterError('schema_version_mismatch',
                              f'expected {SCHEMA_VERSION}, found {version!r}')
        found = self._meta_get('archive_id')
        if found != self.archive_id:
            raise MasterError('archive_id_mismatch',
                              f'master archive_id {found!r} != expected {self.archive_id!r}')

    def _meta_get(self, key):
        row = self.db.execute('SELECT value FROM meta WHERE key=?', (key,)).fetchone()
        return row[0] if row else None

    # ── single-writer lock ───────────────────────────────────────────
    def acquire_writer_lock(self, timeout=5.0):
        if self.readonly:
            raise MasterError('readonly_store', 'readonly store cannot take writer lock')
        # opened through the binding chain (dirfd-relative, no symlink
        # following) — never a raw absolute path
        lfd = self.binding._open_chain(WRITER_LOCK,
                                       os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW,
                                       0o600)
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(lfd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                self._lock_fd = lfd
                return
            except OSError:
                if time.monotonic() >= deadline:
                    os.close(lfd)
                    raise MasterError('writer_busy',
                                      'another writer holds the archive writer lock')
                time.sleep(0.05)

    def release_writer_lock(self):
        if self._lock_fd is not None:
            try:
                fcntl.flock(self._lock_fd, fcntl.LOCK_UN)
            finally:
                os.close(self._lock_fd)
                self._lock_fd = None

    # ── transactions ─────────────────────────────────────────────────
    @contextmanager
    def full_transaction(self):
        """One FULL transaction: guard re-check → BEGIN IMMEDIATE → work →
        COMMIT. commit_seq is allocated inside and exposed via `tx.commit_seq`."""
        self.binding.check_alive()
        self.binding.prove_db(DB_NAME, allow_missing=self._schema_creating)
        # per-transaction anchoring proof (Linux VFS path): the connection
        # must still report the anchored name
        if self.vfs is not None:
            self._prove_anchored()
        if self.readonly:
            raise MasterError('readonly_store', 'readonly store cannot transact')
        tx = _Tx(self)
        try:
            self.db.execute('BEGIN IMMEDIATE')
            if self._schema_creating:
                tx.commit_seq = 0  # schema tx precedes meta bookkeeping
            else:
                tx.commit_seq = self._next_commit_seq()
            yield tx
            if not self._schema_creating:
                self.db.execute(
                    'UPDATE meta SET value=? WHERE key=?',
                    (str(tx.commit_seq), 'commit_seq'))
            self.db.execute('COMMIT')
        except BaseException:
            try:
                self.db.execute('ROLLBACK')
            except sqlite3.Error:
                pass
            raise

    def _next_commit_seq(self):
        current = int(self._meta_get('commit_seq') or 0)
        return current + 1

    @contextmanager
    def read_snapshot(self):
        """Consistent read view (for projection/backup/status)."""
        self.binding.check_alive()
        self.binding.prove_db(DB_NAME)
        if self.vfs is not None:
            self._prove_anchored()
        self.db.execute('BEGIN DEFERRED')
        self.db.execute('SELECT COUNT(*) FROM meta')  # actually establish the view
        try:
            yield self.db
        finally:
            self.db.execute('COMMIT')

    # ── basic accessors ──────────────────────────────────────────────
    def epoch(self):
        return int(self._meta_get('epoch') or 0)

    def commit_seq(self):
        return int(self._meta_get('commit_seq') or 0)

    def anomaly_config(self):
        return {
            'missing_abs_min': int(self._meta_get('anomaly_missing_abs_min') or 20),
            'missing_pct': int(self._meta_get('anomaly_missing_pct') or 10),
            'missing_hard': int(self._meta_get('anomaly_missing_hard') or 500),
        }

    def meta(self, key):
        return self._meta_get(key)

    # ── scope / authorization (re-verified on the NAS, independently) ─
    def check_scope(self, account, talker, purpose):
        row = self.db.execute(
            'SELECT * FROM scope_grants WHERE account=? AND talker=?',
            (account, talker)).fetchone()
        if row is None:
            raise AuthError('scope_denied',
                            f'no grant for {account}/{talker}',
                            {'account': account, 'talker': talker, 'purpose': purpose})
        column = 'capture_allowed' if purpose == 'capture' else 'query_allowed'
        if not row[column]:
            raise AuthError('scope_denied',
                            f'{purpose} not granted for {talker}',
                            {'account': account, 'talker': talker, 'purpose': purpose})
        if row['revoked_at'] is not None:
            raise AuthError('talker_revoked',
                            f'{talker} was revoked',
                            {'account': account, 'talker': talker, 'purpose': purpose,
                             'revoke_effective_seq': row['revoke_effective_seq']})
        return row

    def revoke_talker(self, account, talker):
        with self.full_transaction() as tx:
            cur = self.db.execute(
                'UPDATE scope_grants SET revoked_at=?, revoke_effective_seq=?, '
                'query_allowed=0 WHERE account=? AND talker=? AND revoked_at IS NULL',
                (int(time.time()), tx.commit_seq, account, talker))
            if cur.rowcount == 0:
                row = self.db.execute(
                    'SELECT revoked_at FROM scope_grants WHERE account=? AND talker=?',
                    (account, talker)).fetchone()
                if row is None:
                    raise MasterError('scope_unknown', f'no grant for {talker}')
                return None  # already revoked
            tx.mutation('scope_grants', f'{account}/{talker}', 'revoke',
                        {'revoke_effective_seq': tx.commit_seq})
            return tx.commit_seq

    # ── batches ──────────────────────────────────────────────────────
    def get_batch(self, batch_id):
        return self.db.execute('SELECT * FROM batches WHERE batch_id=?',
                               (batch_id,)).fetchone()

    def insert_batch_pending(self, batch_id, content_sha256, account, talker, run_id):
        now = now_ms()
        self.db.execute(
            'INSERT INTO batches(batch_id,content_sha256,account,talker,run_id,state,'
            'created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)',
            (batch_id, content_sha256, account, talker, run_id, BATCH_PENDING, now, now))

    def cas_batch_state(self, batch_id, from_state, to_state, *, error_code=None,
                        error_detail=None, receipt=None, commit_seq=None):
        """Serialised CAS — only pending batches may commit/abort/reject."""
        assignments = 'state=?, updated_at=?'
        params = [to_state, now_ms()]
        if receipt is not None:
            assignments += ', receipt_json=?, committed_at=?, commit_seq=?'
            params += [canonical(receipt), now_ms(), commit_seq]
        if error_code is not None:
            assignments += ', error_code=?, error_detail_json=?'
            params += [error_code, canonical(error_detail) if error_detail else None]
        params += [batch_id, from_state]
        cur = self.db.execute(
            f'UPDATE batches SET {assignments} WHERE batch_id=? AND state=?', params)
        return cur.rowcount == 1

    # ── runs ─────────────────────────────────────────────────────────
    def begin_run(self, run_id, account, talker, kind, batch_id=None,
                  claim=False):
        """Server-side allocation of the run and its C0 (concurrency floor).

        c0 is read from the PERSISTED commit_seq BEFORE this run's own
        transaction — the value stored in `runs.run_start_seq` is the only
        authoritative C0; anything a client claims in its commit payload is
        verified against this row and never trusted directly.

        claim=True marks a CAUSAL run: the collector took this run BEFORE
        reading the source view (claim_source_snapshot), so its C0 orders
        the view against everything else. Only claimed runs may overwrite
        existing history or publish missing; an unclaimed run's C0 was
        minted after its view was read and carries no causal authority."""
        c0 = self.commit_seq()
        try:
            with self.full_transaction() as tx:
                self.db.execute(
                    'INSERT INTO runs(run_id,batch_id,account,talker,kind,started_at,'
                    'status,run_start_seq,epoch,claimed) '
                    'VALUES(?,?,?,?,?,?,?,?,?,?)',
                    (run_id, batch_id, account, talker, kind, now_ms(), 'open', c0,
                     self.epoch(), 1 if claim else 0))
                tx.mutation('runs', run_id, 'begin_run',
                            dict(self.db.execute(
                                'SELECT * FROM runs WHERE run_id=?',
                                (run_id,)).fetchone()),
                            {'c0': c0})
        except sqlite3.IntegrityError as exc:
            raise MasterError('run_exists',
                              f'run_id {run_id!r} already exists',
                              {'run_id': run_id}) from exc
        return c0

    def verify_run_binding(self, run_id, account, talker, kind_declared=None):
        """Authoritative run binding check for a commit payload.

        The commit's run must be a run THIS SERVER opened (missing runs are
        unacceptable, not assumed), bound to the same account/talker (and
        kind, when the payload declares one), still open, and begun in the
        current archive epoch. Returns the persisted runs row — the caller
        takes C0 from it, never from the client payload."""
        row = self.get_run(run_id)
        if row is None:
            raise MasterError('run_unknown',
                              f'no run {run_id!r} was begun on this server',
                              {'run_id': run_id})
        if row['account'] != account or row['talker'] != talker:
            raise MasterError(
                'run_binding_mismatch',
                'run is bound to a different account/talker',
                {'run_id': run_id, 'run_account': row['account'],
                 'run_talker': row['talker'], 'claimed_account': account,
                 'claimed_talker': talker})
        if kind_declared is not None and row['kind'] != kind_declared:
            raise MasterError(
                'run_binding_mismatch', 'run kind differs from the payload',
                {'run_id': run_id, 'run_kind': row['kind'],
                 'claimed_kind': kind_declared})
        if row['status'] != 'open':
            raise MasterError('run_not_open',
                              f"run is {row['status']!r}, not open",
                              {'run_id': run_id, 'status': row['status']})
        if int(row['epoch']) != self.epoch():
            raise MasterError('run_epoch_stale',
                              'run was begun in a previous archive epoch',
                              {'run_id': run_id, 'run_epoch': row['epoch'],
                               'current_epoch': self.epoch()})
        return row

    def finish_run(self, run_id, status, stats=None):
        with self.full_transaction() as tx:
            self.db.execute(
                'UPDATE runs SET finished_at=?, status=?, stats_json=? WHERE run_id=?',
                (now_ms(), status, canonical(stats) if stats else None, run_id))
            tx.mutation('runs', run_id, f'finish_{status}',
                        dict(self.db.execute(
                            'SELECT * FROM runs WHERE run_id=?',
                            (run_id,)).fetchone()))

    def get_run(self, run_id):
        return self.db.execute('SELECT * FROM runs WHERE run_id=?', (run_id,)).fetchone()

    # ── source observations ──────────────────────────────────────────
    def get_last_observation(self, account, talker):
        return self.db.execute(
            'SELECT * FROM source_observations WHERE account=? AND talker=?',
            (account, talker)).fetchone()

    def put_observation(self, account, talker, run_id, observation, commit_seq):
        self.db.execute(
            'INSERT OR REPLACE INTO source_observations VALUES(?,?,?,?,?,?)',
            (account, talker, run_id, now_ms(), commit_seq, canonical(observation)))

    # ── identity resolution ──────────────────────────────────────────
    def resolve_identity(self, account, talker, server_id, shard, shard_table,
                         local_rowid):
        """Resolve per contract v2 rules. Returns (msg_uid, bound_server_id,
        bound_local) where exactly one binding matched, or (None, None, None).

        A row with server_id binds by server identity; the local coordinates
        (database shard, table, rowid) are recorded for diagnostics but only
        ever *win* for rows without server_id — so rowid reuse never
        overwrites history."""
        if server_id is not None:
            row = self.db.execute(
                'SELECT * FROM identity_map WHERE account=? AND talker=? AND server_id=?',
                (account, talker, str(server_id))).fetchone()
            if row is not None:
                return row['msg_uid'], row['server_id'], \
                    (row['shard'], row['shard_table'], row['local_rowid'])
            return None, None, None
        row = self.db.execute(
            'SELECT * FROM identity_map WHERE account=? AND talker=? AND shard=? '
            'AND shard_table IS ? AND local_rowid=? AND server_id IS NULL',
            (account, talker, shard, shard_table, local_rowid)).fetchone()
        if row is not None:
            return row['msg_uid'], None, \
                (row['shard'], row['shard_table'], row['local_rowid'])
        return None, None, None

    def local_identity_conflict(self, account, talker, shard, shard_table,
                                local_rowid):
        """Rows already bound to the local coordinates (shard,table,rowid) —
        non-empty means rowid reuse: the existing binding must be preserved
        untouched and the new row enters under an independent identity."""
        rows = self.db.execute(
            'SELECT msg_uid, server_id FROM identity_map WHERE account=? AND talker=? '
            'AND shard=? AND shard_table IS ? AND local_rowid=?',
            (account, talker, shard, shard_table, local_rowid)).fetchall()
        return [dict(r) for r in rows] if rows else []

    def next_msg_uid(self):
        row = self.db.execute('SELECT MAX(msg_uid) FROM identity_map').fetchone()
        return (row[0] or 0) + 1

    def close(self):
        self.release_writer_lock()
        if self.db is not None:
            try:
                self.db.close()
            except sqlite3.Error:
                pass
            self.db = None
        release_pinned_vfs(self.vfs)
        self.vfs = None


class _Tx:
    """Transaction handle: exposes commit_seq and the mutation-log writer used
    by every row-level change, so receipts and logs are written in the same
    FULL transaction as the data."""

    def __init__(self, store):
        self.store = store
        self.commit_seq = None

    def meta_set(self, key, value):
        self.store.db.execute(
            'INSERT INTO meta(key,value) VALUES(?,?) '
            'ON CONFLICT(key) DO UPDATE SET value=excluded.value', (key, value))

    def mutation(self, table_name, row_key, op, after=None, detail=None):
        self.store.db.execute(
            'INSERT INTO mutation_log(commit_seq,ts,table_name,row_key,op,after_json,'
            'detail_json) VALUES(?,?,?,?,?,?,?)',
            (self.commit_seq, now_ms(), table_name, str(row_key), op,
             canonical(after) if after is not None else None,
             canonical(detail) if detail is not None else None))
