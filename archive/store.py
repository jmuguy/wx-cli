"""Local SQLite archive store for wx-cli conversation exports.

Owns message identity (server/local namespaces), atomic validated imports,
revisions, reconciliation marking, FTS5 trigram search, privacy policy
enforcement, readonly access and migration from the prototype schema.
No network calls.
"""
import hashlib
import json
import os
import shutil
import re
import sqlite3
import tempfile
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path, PurePosixPath

DEFAULT_ROOT = Path.home() / 'Library/Application Support/howie-wechat-archive'
SCHEMA_VERSION = 2
SERVER = 'server'
LOCAL = 'local'
MAX_SERVER_ID = 2**63 - 1
_OLD_TABLES = ('messages', 'revisions', 'imports', 'checkpoints')

_SCHEMA = [
    'CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)',
    '''CREATE TABLE IF NOT EXISTS messages (
      account TEXT NOT NULL, talker TEXT NOT NULL,
      id_kind TEXT NOT NULL, message_id TEXT NOT NULL,
      server_id INTEGER, local_id INTEGER, source_shard TEXT,
      create_time INTEGER NOT NULL, sort_seq INTEGER NOT NULL,
      sender TEXT NOT NULL, sender_display_name TEXT NOT NULL DEFAULT '',
      snippet TEXT NOT NULL DEFAULT '', original_text TEXT NOT NULL DEFAULT '',
      payload TEXT NOT NULL, source TEXT NOT NULL,
      media_files TEXT NOT NULL DEFAULT '[]',
      missing_in_source INTEGER NOT NULL DEFAULT 0,
      PRIMARY KEY(account,talker,id_kind,message_id))''',
    '''CREATE TABLE IF NOT EXISTS revisions (
      account TEXT NOT NULL, talker TEXT NOT NULL, id_kind TEXT NOT NULL,
      message_id TEXT NOT NULL, payload_hash TEXT NOT NULL, payload TEXT NOT NULL,
      source TEXT, revised_at INTEGER,
      PRIMARY KEY(account,talker,id_kind,message_id,payload_hash))''',
    '''CREATE TABLE IF NOT EXISTS imports (
      account TEXT NOT NULL, source TEXT NOT NULL, imported_at INTEGER,
      talker TEXT, count INTEGER, status TEXT, PRIMARY KEY(account,source))''',
    '''CREATE TABLE IF NOT EXISTS checkpoints (
      account TEXT NOT NULL, talker TEXT NOT NULL, until_ts INTEGER NOT NULL,
      source TEXT, PRIMARY KEY(account,talker))''',
    '''CREATE TABLE IF NOT EXISTS conversations (
      account TEXT NOT NULL, talker TEXT NOT NULL, name TEXT NOT NULL DEFAULT '',
      PRIMARY KEY(account,talker))''',
    'CREATE INDEX IF NOT EXISTS message_time ON messages(account,talker,create_time,sort_seq)',
    "CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(content, tokenize='trigram')",
]


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def _extract_text(node):
    """Best-effort plain text from the structured message content."""
    if isinstance(node, str):
        return node
    if isinstance(node, dict):
        parts = [_extract_text(node[key]) for key in sorted(node)]
        return '\n'.join(part for part in parts if part)
    if isinstance(node, list):
        parts = [_extract_text(item) for item in node]
        return '\n'.join(part for part in parts if part)
    return ''


def _fts_content(snippet, text):
    if snippet and text:
        return snippet + '\n' + text
    return snippet or text


def _sha256_text(value):
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


class Archive:
    """Archive store. All writes are atomic; policy applies to writes and queries."""

    def __init__(self, root=DEFAULT_ROOT, readonly=False,
                 allowed_account=None, allowed_talkers=None):
        self.readonly = bool(readonly)
        self.allowed_account = allowed_account
        self.allowed_talkers = None if allowed_talkers is None else list(allowed_talkers)
        self.root = Path(root).expanduser().resolve()
        self._savepoint_counter = 0
        try:
            if self.readonly:
                self._open_readonly()
            else:
                self._open_writable()
        except BaseException:
            if hasattr(self, 'db'):
                self.db.close()
            raise

    # ── open / schema ────────────────────────────────────────────────

    def _open_readonly(self):
        db_path = self.root / 'archive.sqlite3'
        if not db_path.is_file():
            raise FileNotFoundError(f'no archive database at {db_path}; readonly refuses to create')
        self.db = sqlite3.connect(db_path.as_uri() + '?mode=ro', uri=True, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA busy_timeout=5000')
        self.db.execute('PRAGMA query_only=ON')
        tables = self._tables()
        if 'meta' not in tables and tables & set(_OLD_TABLES):
            raise ValueError('prototype archive needs a writable open for migration')

    def _open_writable(self):
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.root, 0o700)
        db_path = self.root / 'archive.sqlite3'
        self.db = sqlite3.connect(db_path, isolation_level=None)
        os.chmod(db_path, 0o600)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA busy_timeout=5000')
        tables = self._tables()
        if 'meta' in tables:
            row = self.db.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
            version = int(row[0]) if row else None
            if version != SCHEMA_VERSION:
                raise ValueError(f'unsupported archive schema version: {version}')
            if 'messages_fts' not in tables:  # defensive: rebuild index
                with self._transaction():
                    self.db.execute(_SCHEMA[-1])
                    self._rebuild_fts()
        elif tables & set(_OLD_TABLES):
            self._migrate_old(tables)
        elif not tables:
            with self._transaction():
                for stmt in _SCHEMA:
                    self.db.execute(stmt)
                self.db.execute("INSERT OR REPLACE INTO meta VALUES('schema_version',?)",
                                (str(SCHEMA_VERSION),))
        else:
            raise ValueError('unrecognized archive database; refusing to touch it')

    def _tables(self):
        return {row[0] for row in self.db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}

    def _create_schema(self):
        for stmt in _SCHEMA:
            self.db.execute(stmt)

    def _rebuild_fts(self):
        self.db.execute('DELETE FROM messages_fts')
        rows = self.db.execute('SELECT rowid, snippet, original_text FROM messages').fetchall()
        for row in rows:
            self.db.execute('INSERT INTO messages_fts(rowid, content) VALUES(?,?)',
                            (row['rowid'], _fts_content(row['snippet'], row['original_text'])))

    def _migrate_old(self, tables):
        old = [name for name in _OLD_TABLES if name in tables]
        with self._transaction():
            for name in old:
                self.db.execute(f'ALTER TABLE {name} RENAME TO _migrated_{name}')
            self.db.execute('DROP INDEX IF EXISTS message_time')
            self._create_schema()
            if 'messages' in old:
                for row in self.db.execute('SELECT * FROM _migrated_messages').fetchall():
                    self._migrate_message(row)
            if 'revisions' in old:
                for row in self.db.execute('SELECT * FROM _migrated_revisions').fetchall():
                    self.db.execute(
                        'INSERT OR IGNORE INTO revisions VALUES(?,?,?,?,?,?,?,?)',
                        (row['account'], row['talker'], SERVER, str(row['server_id']),
                         row['payload_hash'], row['payload'], row['source'], None))
            if 'imports' in old:
                for row in self.db.execute('SELECT * FROM _migrated_imports').fetchall():
                    self.db.execute('INSERT OR IGNORE INTO imports VALUES(?,?,?,?,?,?)',
                                    (row['account'], row['source'], row['imported_at'],
                                     None, row['count'], None))
            if 'checkpoints' in old:
                for row in self.db.execute('SELECT * FROM _migrated_checkpoints').fetchall():
                    self.db.execute('INSERT OR IGNORE INTO checkpoints VALUES(?,?,?,?)',
                                    (row['account'], row['talker'], row['until_ts'], row['source']))
            for name in old:
                self.db.execute(f'DROP TABLE _migrated_{name}')
            self.db.execute("INSERT OR REPLACE INTO meta VALUES('schema_version',?)",
                            (str(SCHEMA_VERSION),))

    def _migrate_message(self, row):
        display, text, local_id = '', '', None
        media_paths = []
        try:
            item = json.loads(row['payload'])
            if isinstance(item, dict):
                display = item.get('sender_display_name') or ''
                text = _extract_text(item.get('content'))
                local_id = item.get('local_id')
                for rel in item.get('media_files', []):
                    pure = PurePosixPath(rel)
                    if not pure.is_absolute() and '..' not in pure.parts:
                        media_paths.append(str(Path('sources') / row['source'] / rel))
        except (ValueError, TypeError):
            pass
        cur = self.db.execute(
            'INSERT INTO messages (account,talker,id_kind,message_id,server_id,local_id,'
            'source_shard,create_time,sort_seq,sender,sender_display_name,snippet,'
            'original_text,payload,source,media_files,missing_in_source) '
            "VALUES (?,?,?,?,?,?,NULL,?,?,?,?,?,?,?,?,?,0)",
            (row['account'], row['talker'], SERVER, str(row['server_id']), row['server_id'],
             local_id, row['create_time'], row['sort_seq'], row['sender'], display,
             row['snippet'] or '', text, row['payload'], row['source'], canonical(media_paths)))
        self.db.execute('INSERT INTO messages_fts(rowid, content) VALUES(?,?)',
                        (cur.lastrowid, _fts_content(row['snippet'] or '', text)))

    @contextmanager
    def _transaction(self):
        self._savepoint_counter += 1
        name = f'archive_store_{self._savepoint_counter}'
        self.db.execute(f'SAVEPOINT {name}')
        try:
            yield
        except BaseException:
            self.db.execute(f'ROLLBACK TO SAVEPOINT {name}')
            self.db.execute(f'RELEASE SAVEPOINT {name}')
            raise
        else:
            self.db.execute(f'RELEASE SAVEPOINT {name}')

    # ── policy ───────────────────────────────────────────────────────

    def _check_account(self, account):
        if self.allowed_account is not None and account != self.allowed_account:
            raise ValueError('account not allowed by archive policy')

    def _check_talker(self, talker):
        if self.allowed_talkers is not None and talker not in self.allowed_talkers:
            raise ValueError('talker not allowed by archive policy')

    def _check_write(self, account, talker):
        if self.readonly:
            raise ValueError('readonly archive cannot be written')
        if not isinstance(account, str) or not account:
            raise ValueError('account required')
        if not isinstance(talker, str) or not talker:
            raise ValueError('talker required')
        self._check_account(account)
        self._check_talker(talker)

    def _policy_conditions(self, prefix=''):
        conditions, args = [], []
        if self.allowed_account is not None:
            conditions.append(f'{prefix}account=?')
            args.append(self.allowed_account)
        if self.allowed_talkers is not None:
            if not self.allowed_talkers:
                conditions.append('0')
            else:
                marks = ','.join('?' * len(set(self.allowed_talkers)))
                conditions.append(f'{prefix}talker IN ({marks})')
                args.extend(sorted(set(self.allowed_talkers)))
        return conditions, args

    # ── import ───────────────────────────────────────────────────────

    def import_export(self, path, account, expected_talker=None, bounds=None, *,
                      advance_checkpoint=True, reconcile=False, require_media_contract=False,
                      expected_media_mode=None):
        """Atomically import a validated export.

        Manual imports may use legacy exports. Live callers require the v1
        media contract so unavailable assets cannot be confused with errors
        or an older producer that never checked availability.
        """
        if self.readonly:
            raise ValueError('readonly archive cannot be written')
        path = Path(path).expanduser().resolve()
        if bounds is not None:
            bounds = (int(bounds[0]), int(bounds[1]))
            if bounds[0] > bounds[1]:
                raise ValueError('invalid bounds')
        if reconcile and bounds is None:
            raise ValueError('reconcile import requires explicit bounds')
        raw = path.read_bytes()
        data = json.loads(raw)
        if expected_media_mode not in (None, 'enabled', 'metadata_only'):
            raise ValueError('invalid requested media mode')
        talker, parsed, media = self._validate_export(
            data, expected_talker, bounds, path.parent, require_media_contract, expected_media_mode)
        self._check_write(account, talker)
        digest = hashlib.sha256(raw).hexdigest()
        assets = self._store_assets(path.parent, media)
        self._store_source(raw, digest)
        identities = [ident for ident, _item, _rels in parsed]
        with self._transaction():
            added = changed = missing_media = 0
            for ident, item, rels in parsed:
                paths = [assets[rel] for rel in rels]
                a, c, unavailable = self._apply_item(account, talker, ident, item, paths, digest)
                added += a
                changed += c
                missing_media += unavailable
            self.db.execute(
                'INSERT INTO imports VALUES(?,?,?,?,?,?) '
                'ON CONFLICT(account,source) DO UPDATE SET imported_at=excluded.imported_at,'
                'talker=excluded.talker,count=excluded.count,status=excluded.status',
                (account, digest, int(datetime.now().timestamp()), talker, len(parsed),
                 'imported' if parsed else 'empty'))
            display = data.get('conversation', {}).get('display_name') or ''
            if display:
                self.db.execute(
                    'INSERT INTO conversations VALUES(?,?,?) ON CONFLICT(account,talker) DO '
                    "UPDATE SET name=CASE WHEN excluded.name!='' THEN excluded.name "
                    'ELSE conversations.name END', (account, talker, display))
            missing = 0
            if reconcile:
                missing = self._mark_missing(account, talker, bounds, identities)
            if bounds is not None and advance_checkpoint:
                self._write_checkpoint(account, talker, bounds[1], digest)
        return {'imported': len(parsed), 'added': added, 'changed': changed,
                'missing': missing, 'media_files': len(media), 'missing_media': missing_media,
                'source_missing_media': data.get('media', {}).get('missing', 0),
                'media_status': self._source_media_counts(data),
                'source_sha256': digest, 'status': 'imported' if parsed else 'empty'}

    def import_empty(self, account, talker, bounds, *, advance_checkpoint=True, reconcile=False):
        """Record a trusted empty-window observation atomically."""
        if self.readonly:
            raise ValueError('readonly archive cannot be written')
        bounds = (int(bounds[0]), int(bounds[1]))
        if bounds[0] > bounds[1]:
            raise ValueError('invalid bounds')
        self._check_write(account, talker)
        with self._transaction():
            missing = 0
            if reconcile:
                missing = self._mark_missing(account, talker, bounds, None)
            if advance_checkpoint:
                self._write_checkpoint(account, talker, bounds[1], None)
        return {'imported': 0, 'added': 0, 'changed': 0, 'missing': missing,
                'media_files': 0, 'missing_media': 0, 'source_missing_media': 0,
                'media_status': self._source_media_counts({}),
                'source_sha256': None, 'status': 'empty'}

    def _validate_export(self, data, expected_talker, bounds, base, require_media_contract,
                         expected_media_mode):
        if not isinstance(data, dict):
            raise ValueError('invalid export payload')
        items = data.get('items')
        conversation = data.get('conversation')
        stats = data.get('stats')
        paging = data.get('paging')
        if not isinstance(items, list) or not isinstance(conversation, dict):
            raise ValueError('account/talker/items required')
        talker = conversation.get('talker')
        if not isinstance(talker, str) or not talker:
            raise ValueError('account/talker/items required')
        if expected_talker is not None and talker != expected_talker:
            raise ValueError('unexpected talker; use exact chatroom ID')
        if not isinstance(stats, dict) or not isinstance(paging, dict):
            raise ValueError('export lacks stats/paging')
        if type(stats.get('skipped')) is not int or stats['skipped'] != 0:
            raise ValueError('source has skipped messages or invalid skipped count')
        if stats.get('shard_warnings', []) != []:
            raise ValueError('source has shard warnings')
        if paging.get('has_more') is not False or type(paging.get('offset')) is not int \
                or paging['offset'] != 0 or type(paging.get('returned')) is not int \
                or paging['returned'] != len(items):
            raise ValueError('source is incomplete or paginated')
        if type(conversation.get('message_count')) is not int \
                or conversation['message_count'] != len(items):
            raise ValueError('message_count mismatch')
        self._validate_media_contract(data, require_media_contract, expected_media_mode)
        parsed, media = [], set()
        seen = set()
        for item in items:
            ident, rels = self._validate_item(item, talker, bounds, base)
            if require_media_contract and ident[0] == LOCAL and not ident[4]:
                raise ValueError('live local identity requires stable source_shard')
            key = (ident[0], ident[1])
            if key in seen:
                raise ValueError('duplicate identity in source; reconcile before import')
            seen.add(key)
            media.update(rels)
            parsed.append((ident, item, rels))
        return talker, parsed, sorted(media)

    @staticmethod
    def _source_media_counts(data):
        marker = data.get('media', {})
        return {'available': marker.get('available', 0), 'missing': marker.get('missing', 0),
                'metadata_only': marker.get('not_attempted', 0)}

    @staticmethod
    def _validate_media_contract(data, required, expected_mode):
        if 'media' not in data:
            if required or expected_mode is not None or any(
                    'media_status' in item for item in data['items'] if isinstance(item, dict)):
                raise ValueError('export lacks required media contract')
            return
        marker = data['media']
        counters = ('expected', 'available', 'missing', 'errors', 'not_attempted')
        if not isinstance(marker, dict) or set(marker) != {'version', 'mode', *counters} \
                or type(marker.get('version')) is not int or marker['version'] != 1:
            raise ValueError('invalid or unsupported media contract')
        if marker['mode'] not in ('enabled', 'metadata_only') \
                or any(type(marker[key]) is not int or marker[key] < 0 for key in counters):
            raise ValueError('invalid media mode or counts')
        if expected_mode is not None and marker['mode'] != expected_mode:
            raise ValueError('source media mode differs from requested mode')
        if marker['errors'] != 0:
            raise ValueError('source has media errors')
        counts = dict.fromkeys(counters, 0)
        for item in data['items']:
            if not isinstance(item, dict):
                raise ValueError('invalid media message')
            content = item.get('content')
            eligible = content == 'Voice' or (
                isinstance(content, dict) and bool({'Image', 'Video', 'File'} & content.keys()))
            files = item.get('media_files', [])
            if not isinstance(files, list) or any(not isinstance(rel, str) for rel in files):
                raise ValueError('invalid media_files')
            if not eligible:
                if 'media_status' in item or files:
                    raise ValueError('media metadata on ineligible message')
                continue
            counts['expected'] += 1
            status = item.get('media_status')
            if not isinstance(status, dict) or 'state' not in status \
                    or not set(status) <= {'state', 'reason'}:
                raise ValueError('missing or malformed media_status')
            state = status['state']
            if not isinstance(state, str) \
                    or state not in ('available', 'missing', 'error', 'metadata_only'):
                raise ValueError('invalid media state')
            if 'reason' in status and (not isinstance(status['reason'], str)
                                      or not re.fullmatch(r'[a-z][a-z0-9]*(?:_[a-z0-9]+)*',
                                                          status['reason'])):
                raise ValueError('invalid media reason code')
            if state == 'missing' and 'reason' in status and status['reason'] not in (
                    'missing_local_media', 'missing_reference'):
                raise ValueError('unproven missing media')
            if state == 'error':
                raise ValueError('source has media errors')
            if bool(files) != (state == 'available'):
                raise ValueError('media state/files mismatch')
            if (marker['mode'] == 'metadata_only') != (state == 'metadata_only'):
                raise ValueError('media state/mode mismatch')
            counts['not_attempted' if state == 'metadata_only' else state] += 1
        if any(marker[key] != counts[key] for key in counters):
            raise ValueError('media count mismatch')
        if marker['expected'] != sum(marker[key] for key in counters[1:]) \
                or (marker['mode'] == 'enabled' and marker['not_attempted'] != 0):
            raise ValueError('inconsistent media contract')

    def _validate_item(self, item, talker, bounds, base):
        if not isinstance(item, dict):
            raise ValueError('message identity or structured content invalid')
        sid = item.get('server_id')
        if sid is not None and (type(sid) is not int or sid > MAX_SERVER_ID):
            raise ValueError('invalid server_id')
        local_id = item.get('local_id')
        shard = item.get('source_shard', '')
        if not isinstance(shard, str) or '/' in shard or '\\' in shard \
                or shard in ('.', '..'):
            raise ValueError('invalid source_shard; stable basename only')
        if type(sid) is int and 0 < sid <= MAX_SERVER_ID:
            ident = (SERVER, str(sid), sid,
                     local_id if type(local_id) is int else None, None)
        elif type(local_id) is int and local_id >= 0:
            message_id = f'{shard}/{local_id}' if shard else str(local_id)
            ident = (LOCAL, message_id, sid, local_id, shard)
        else:
            raise ValueError(
                'message lacks server_id/local_id identity; import stopped, no checkpoint advanced')
        if item.get('talker') != talker or 'content' not in item \
                or not isinstance(item.get('sender'), str):
            raise ValueError('message identity or structured content invalid')
        create_time, sort_seq = item.get('create_time'), item.get('sort_seq')
        if type(create_time) is not int or type(sort_seq) is not int:
            raise ValueError('invalid message timestamp/sequence')
        if bounds is not None and not bounds[0] <= create_time <= bounds[1]:
            raise ValueError('message outside requested window')
        media_files = item.get('media_files', [])
        if not isinstance(media_files, list) \
                or any(not isinstance(entry, str) for entry in media_files):
            raise ValueError('invalid media_files')
        for rel in media_files:
            self._safe_media(base, rel)
        return ident, media_files

    @staticmethod
    def _safe_media(base, rel):
        pure = PurePosixPath(rel)
        if not rel or pure.is_absolute() or '..' in pure.parts or '.' in pure.parts:
            raise ValueError('unsafe media path')
        candidate = base.joinpath(*pure.parts)
        current = candidate
        while current != base:
            if current.is_symlink():
                raise ValueError('unsafe media path')
            current = current.parent
        if not candidate.is_file():
            raise ValueError('referenced media missing')
        return candidate

    def _private_dir(self, directory):
        current = self.root
        for part in directory.relative_to(self.root).parts:
            current = current / part
            if current.is_symlink():
                raise ValueError('unsafe archive storage path')
            current.mkdir(exist_ok=True, mode=0o700)
            os.chmod(current, 0o700)

    def _store_source(self, raw, digest):
        dest = self.root / 'sources' / digest
        self._private_dir(dest)
        target = dest / 'export.json'
        if target.is_symlink():
            raise ValueError('unsafe archive source path')
        if not target.exists():
            with tempfile.NamedTemporaryFile(dir=dest, delete=False) as handle:
                pending = Path(handle.name)
                try:
                    handle.write(raw)
                    handle.flush()
                    os.chmod(pending, 0o600)
                    os.replace(pending, target)
                finally:
                    pending.unlink(missing_ok=True)

    def _store_assets(self, base, media):
        """Store each unique attachment once, independent of its exported filename."""
        paths = {}
        for rel in media:
            source = self._safe_media(base, rel)
            with source.open('rb') as handle:
                asset_digest = hashlib.file_digest(handle, 'sha256').hexdigest()
            relative = Path('sources') / 'assets' / asset_digest / 'asset'
            stored = self.root / relative
            self._private_dir(stored.parent)
            if stored.is_symlink():
                raise ValueError('unsafe archive asset path')
            if not stored.exists():
                with tempfile.NamedTemporaryFile(dir=stored.parent, delete=False) as handle:
                    pending = Path(handle.name)
                try:
                    shutil.copyfile(source, pending)
                    os.chmod(pending, 0o600)
                    os.replace(pending, stored)
                finally:
                    pending.unlink(missing_ok=True)
            paths[rel] = str(relative)
        return paths

    def _apply_item(self, account, talker, ident, item, media_paths, digest):
        kind, message_id, server_id, local_id, shard = ident
        payload_text = canonical(item)
        row = self.db.execute(
            'SELECT rowid AS rid, payload, media_files FROM messages WHERE account=? AND '
            'talker=? AND id_kind=? AND message_id=?',
            (account, talker, kind, message_id)).fetchone()
        text = _extract_text(item.get('content'))
        snippet = item.get('snippet') or ''
        display = item.get('sender_display_name') or ''
        new_paths = set(media_paths)
        media_json = canonical(sorted(new_paths))
        source_missing = item.get('media_status', {}).get('state') == 'missing'
        if row is None:
            cur = self.db.execute(
                'INSERT INTO messages (account,talker,id_kind,message_id,server_id,local_id,'
                'source_shard,create_time,sort_seq,sender,sender_display_name,snippet,'
                'original_text,payload,source,media_files,missing_in_source) '
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0)",
                (account, talker, kind, message_id, server_id, local_id, shard,
                 item['create_time'], item['sort_seq'], item['sender'], display, snippet,
                 text, payload_text, digest, media_json))
            self.db.execute('INSERT INTO messages_fts(rowid, content) VALUES(?,?)',
                            (cur.lastrowid, _fts_content(snippet, text)))
            self.db.execute('INSERT OR IGNORE INTO revisions VALUES(?,?,?,?,?,?,?,?)',
                            (account, talker, kind, message_id, _sha256_text(payload_text),
                             payload_text, digest, int(datetime.now().timestamp())))
            return 1, 0, int(source_missing and not new_paths)
        existing = set(json.loads(row['media_files'] or '[]'))
        media_union = canonical(sorted(existing | new_paths))
        old_payload = json.loads(row['payload'])
        metadata = {'media_files', 'media_status', 'local_id', 'source_shard',
                    'sender_display_name', 'direction', 'snippet'}
        old_substance = {key: value for key, value in old_payload.items() if key not in metadata}
        substance = {key: value for key, value in item.items() if key not in metadata}
        changed = old_substance != substance
        self.db.execute(
            'UPDATE messages SET server_id=?,local_id=?,source_shard=?,create_time=?,sort_seq=?,'
            'sender=?,sender_display_name=?,snippet=?,original_text=?,payload=?,source=?,'
            'media_files=?,missing_in_source=0 WHERE rowid=?',
            (server_id, local_id, shard, item['create_time'], item['sort_seq'],
             item['sender'], display, snippet, text, payload_text, digest, media_union, row['rid']))
        if changed or (old_payload.get('snippet') or '') != snippet:
            self.db.execute('DELETE FROM messages_fts WHERE rowid=?', (row['rid'],))
            self.db.execute('INSERT INTO messages_fts(rowid, content) VALUES(?,?)',
                            (row['rid'], _fts_content(snippet, text)))
        if changed:
            self.db.execute('INSERT OR IGNORE INTO revisions VALUES(?,?,?,?,?,?,?,?)',
                            (account, talker, kind, message_id, _sha256_text(payload_text),
                             payload_text, digest, int(datetime.now().timestamp())))
        return 0, int(changed), int(source_missing and not (existing | new_paths))

    def _write_checkpoint(self, account, talker, until_ts, source):
        self.db.execute(
            'INSERT INTO checkpoints VALUES(?,?,?,?) ON CONFLICT(account,talker) DO UPDATE SET '
            'until_ts=MAX(checkpoints.until_ts,excluded.until_ts),'
            'source=CASE WHEN excluded.until_ts>=checkpoints.until_ts THEN excluded.source '
            'ELSE checkpoints.source END',
            (account, talker, until_ts, source))

    def _mark_missing(self, account, talker, bounds, identities):
        if identities is None:
            cur = self.db.execute(
                'UPDATE messages SET missing_in_source=1 WHERE account=? AND talker=? AND '
                'missing_in_source=0 AND create_time>=? AND create_time<=?',
                (account, talker, bounds[0], bounds[1]))
            return cur.rowcount
        self.db.execute('CREATE TEMP TABLE IF NOT EXISTS supplied_ids '
                        '(id_kind TEXT NOT NULL, message_id TEXT NOT NULL, '
                        'PRIMARY KEY(id_kind,message_id))')
        self.db.execute('DELETE FROM supplied_ids')
        self.db.executemany('INSERT INTO supplied_ids VALUES(?,?)',
                            [(kind, message_id) for kind, message_id, *_ in identities])
        cur = self.db.execute(
            'UPDATE messages SET missing_in_source=1 WHERE account=? AND talker=? AND '
            'missing_in_source=0 AND create_time>=? AND create_time<=? AND NOT EXISTS '
            '(SELECT 1 FROM supplied_ids s WHERE s.id_kind=messages.id_kind '
            'AND s.message_id=messages.message_id)',
            (account, talker, bounds[0], bounds[1]))
        return cur.rowcount

    # ── queries ──────────────────────────────────────────────────────

    def checkpoint(self, account, talker):
        self._check_account(account)
        self._check_talker(talker)
        row = self.db.execute('SELECT until_ts FROM checkpoints WHERE account=? AND talker=?',
                              (account, talker)).fetchone()
        return row[0] if row else None

    def search(self, query, account, talker=None, limit=20, since=None, until=None):
        if not isinstance(query, str) or not query or not account:
            raise ValueError('query and account required')
        self._check_account(account)
        if talker is not None:
            self._check_talker(talker)
        limit = max(1, min(int(limit), 100))
        conditions, args = ['m.account=?'], [account]
        if talker is not None:
            conditions.append('m.talker=?')
            args.append(talker)
        elif self.allowed_talkers is not None:
            if not self.allowed_talkers:
                return []
            marks = ','.join('?' * len(set(self.allowed_talkers)))
            conditions.append(f'm.talker IN ({marks})')
            args.extend(sorted(set(self.allowed_talkers)))
        if since is not None:
            conditions.append('m.create_time>=?')
            args.append(int(since))
        if until is not None:
            conditions.append('m.create_time<=?')
            args.append(int(until))
        order = (' ORDER BY m.create_time DESC, m.sort_seq DESC, COALESCE(m.server_id,-1) DESC,'
                 " COALESCE(m.local_id,0) DESC, COALESCE(m.source_shard,'') DESC,"
                 ' m.id_kind DESC, m.message_id DESC LIMIT ?')
        where = ' AND '.join(conditions)
        if len(query) >= 3:
            phrase = '"' + query.replace('"', '""') + '"'
            sql = (f'SELECT m.* FROM messages m JOIN messages_fts ON messages_fts.rowid=m.rowid '
                   f'WHERE messages_fts MATCH ? AND {where}{order}')
            rows = self.db.execute(sql, [phrase] + args + [limit]).fetchall()
            return [self._record(row) for row in rows]
        literal = query.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')
        pattern = '%' + literal + '%'
        sql = (f"SELECT m.* FROM messages m WHERE (m.snippet LIKE ? ESCAPE '\\' OR "
               f"m.original_text LIKE ? ESCAPE '\\') AND {where}{order}")
        rows = self.db.execute(sql, [pattern, pattern] + args + [limit]).fetchall()
        return [self._record(row) for row in rows]

    def context(self, account, talker, server_id=None, radius=10, *,
                message_id=None, id_kind=SERVER):
        self._check_account(account)
        self._check_talker(talker)
        if message_id is None:
            if server_id is None:
                raise ValueError('server_id or message_id required')
            row = self.db.execute(
                'SELECT * FROM messages WHERE account=? AND talker=? AND id_kind=? AND server_id=?',
                (account, talker, SERVER, int(server_id))).fetchone()
        else:
            if id_kind not in (SERVER, LOCAL):
                raise ValueError('invalid id_kind')
            row = self.db.execute(
                'SELECT * FROM messages WHERE account=? AND talker=? AND id_kind=? AND message_id=?',
                (account, talker, id_kind, str(message_id))).fetchone()
        if row is None:
            raise ValueError('message not found')
        radius = max(0, min(int(radius), 50))
        anchor = (row['create_time'], row['sort_seq'],
                  row['server_id'] if row['server_id'] is not None else -1,
                  row['local_id'] if row['local_id'] is not None else 0,
                  row['source_shard'] or '', row['id_kind'], row['message_id'])
        ordering = ('create_time {dir}, sort_seq {dir}, COALESCE(server_id,-1) {dir}, '
                    "COALESCE(local_id,0) {dir}, COALESCE(source_shard,'') {dir}, "
                    'id_kind {dir}, message_id {dir}')
        keys = ("create_time,sort_seq,COALESCE(server_id,-1),COALESCE(local_id,0),"
                "COALESCE(source_shard,''),id_kind,message_id")
        before = self.db.execute(
            f'SELECT * FROM messages WHERE account=? AND talker=? AND ({keys}) '
            f'< (?,?,?,?,?,?,?) ORDER BY {ordering.format(dir="DESC")} LIMIT ?',
            (account, talker, *anchor, radius)).fetchall()
        after = self.db.execute(
            f'SELECT * FROM messages WHERE account=? AND talker=? AND ({keys}) '
            f'> (?,?,?,?,?,?,?) ORDER BY {ordering.format(dir="ASC")} LIMIT ?',
            (account, talker, *anchor, radius)).fetchall()
        return [self._record(r) for r in (*reversed(before), row, *after)]

    def list_conversations(self, account=None):
        if account is not None:
            self._check_account(account)
            accounts = [account]
        elif self.allowed_account is not None:
            accounts = [self.allowed_account]
        else:
            accounts = [row[0] for row in self.db.execute(
                'SELECT account FROM messages UNION SELECT account FROM checkpoints '
                'UNION SELECT account FROM conversations ORDER BY account')]
        result = []
        for acct in accounts:
            if self.allowed_talkers is None:
                talkers = [row[0] for row in self.db.execute(
                    'SELECT talker FROM messages WHERE account=? UNION SELECT talker FROM '
                    'checkpoints WHERE account=? UNION SELECT talker FROM conversations WHERE '
                    'account=? ORDER BY talker', (acct, acct, acct))]
            else:
                talkers = sorted(set(self.allowed_talkers))
            for talker in talkers:
                name = self.db.execute(
                    'SELECT name FROM conversations WHERE account=? AND talker=?',
                    (acct, talker)).fetchone()
                count, last_time, missing = self.db.execute(
                    'SELECT count(*), max(create_time), sum(missing_in_source) FROM messages '
                    'WHERE account=? AND talker=?', (acct, talker)).fetchone()
                checkpoint_row = self.db.execute(
                    'SELECT until_ts FROM checkpoints WHERE account=? AND talker=?',
                    (acct, talker)).fetchone()
                result.append({
                    'account': acct, 'talker': talker,
                    'conversation_name': name[0] if name and name[0] else talker,
                    'message_count': count, 'last_message_time': last_time,
                    'checkpoint': checkpoint_row[0] if checkpoint_row else None,
                    'missing': missing or 0})
        return result

    def status(self):
        conditions, args = self._policy_conditions('m.')
        where = ' AND '.join(conditions) or '1'
        messages = self.db.execute(
            f'SELECT count(*) FROM messages m WHERE {where}', args).fetchone()[0]
        missing = self.db.execute(
            f'SELECT count(*) FROM messages m WHERE {where} AND m.missing_in_source=1',
            args).fetchone()[0]
        media_status = {'available': 0, 'missing': 0, 'metadata_only': 0}
        missing_media = 0
        rows = self.db.execute(
            f'SELECT payload, media_files FROM messages m WHERE {where}', args)
        for row in rows:
            try:
                payload = json.loads(row['payload'])
            except (ValueError, TypeError):
                continue  # Prototype migrations can contain non-JSON payloads.
            if not isinstance(payload, dict):
                continue
            source_status = payload.get('media_status')
            state = source_status.get('state') if isinstance(source_status, dict) else None
            if isinstance(state, str) and state in media_status:
                media_status[state] += 1
                if state == 'missing' and not json.loads(row['media_files'] or '[]'):
                    missing_media += 1
        revision_conditions, revision_args = self._policy_conditions('r.')
        revisions = self.db.execute(
            'SELECT count(*) FROM revisions r WHERE '
            + (' AND '.join(revision_conditions) or '1'), revision_args).fetchone()[0]
        checkpoint_conditions, checkpoint_args = self._policy_conditions('c.')
        checkpoints = [dict(row) for row in self.db.execute(
            'SELECT c.* FROM checkpoints c WHERE '
            + (' AND '.join(checkpoint_conditions) or '1')
            + ' ORDER BY c.account, c.talker', checkpoint_args)]
        validation = self.db.execute(
            "SELECT value FROM meta WHERE key='real_wechat_validation'").fetchone()
        schema = self.db.execute(
            "SELECT value FROM meta WHERE key='schema_version'").fetchone()
        return {'messages': messages, 'revisions': revisions, 'missing': missing,
                'media_status': media_status, 'missing_media': missing_media,
                'source_missing_media': media_status['missing'],
                'checkpoints': checkpoints, 'schema_version': int(schema[0]) if schema else None,
                'real_wechat_validation': validation[0] if validation else 'UNVERIFIED'}

    # ── citation records ─────────────────────────────────────────────

    def _record(self, row):
        name = self.db.execute(
            'SELECT name FROM conversations WHERE account=? AND talker=?',
            (row['account'], row['talker'])).fetchone()
        revisions = self.db.execute(
            'SELECT count(*) FROM revisions WHERE account=? AND talker=? AND id_kind=? '
            'AND message_id=?',
            (row['account'], row['talker'], row['id_kind'], row['message_id'])).fetchone()[0]
        digest = row['source']
        stored = json.loads(row['media_files'] or '[]')
        payload = json.loads(row['payload'])
        return {
            'account': row['account'], 'talker': row['talker'],
            'conversation_name': name[0] if name and name[0] else row['talker'],
            'sender': row['sender'], 'sender_display_name': row['sender_display_name'],
            'time': datetime.fromtimestamp(row['create_time']).astimezone().isoformat(),
            'create_time': row['create_time'], 'server_id': row['server_id'],
            'local_id': row['local_id'], 'id_kind': row['id_kind'],
            'message_id': row['message_id'], 'original_text': row['original_text'],
            'message': payload,
            'media_status': payload.get('media_status') if isinstance(payload, dict) else None,
            'has_revisions': revisions > 1, 'missing_in_source': bool(row['missing_in_source']),
            'source': str(self.root / 'sources' / digest / 'export.json') if digest else None,
            'media_files': [str(self.root / path) for path in stored]}
