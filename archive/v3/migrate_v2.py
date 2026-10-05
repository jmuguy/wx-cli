"""One-shot migration from the local v2 archive into a v3 NAS master.

Approved-spec design (external-archive-v3 §1.9), replacing the rejected
per-session export-replay approach:

* **Consistent FULL-DB snapshot.** The v2 archive is copied once with the
  sqlite online backup API from a read-only connection (work/v2-snapshot.db);
  every session is rendered from THAT copy — one consistent state, never a
  mixture of per-session reads against a moving archive.
* **Nothing is dropped.** Every referenced source view and every referenced
  asset travels: v2 `sources/<digest>/export.json` existence is audited, and
  v2 `sources/assets/<sha256>/asset` bytes ARE present in a v2 archive —
  they are copied into the migration export hash-verified (the copy must
  re-hash to its content address or the migration fails; no silent skip).
* **Direct v3 runs, claim-first.** Each session renders as a contract-v2
  export from the snapshot and rides the NORMAL collector path with a causal
  claim taken BEFORE the view is written — so a re-migration that restores a
  fuller payload onto an already-migrated row is a legitimate source_upgrade
  under the claim's C0, while a v3 row with real causal history still wins.
* **Namespaces are preserved.** Local v2 rows keep their ORIGINAL
  (source_shard, local_id) coordinates as the v3 local identity (contract
  suffix rule: shard names end in .db; the verbatim v2 value also rides in
  raw.v2). Server rows are keyed by server_id — their true identity; their
  v3 stream coordinates are sequential inside the dedicated
  '__v2_server__.db' namespace with the v2 originals kept in raw.v2. No
  global rowid renumbering ever replaces a v2 local namespace.
* **History and honesty travel.** v2 revisions become legacy_revision rows,
  v2 missing_in_source marks become soft legacy_v2_missing observations,
  rows whose v2 source view is no longer on disk are counted in the audit
  (unknown), and the run carries provenance_override=
  legacy_visibility_projected — the reconciler counts the restored payloads
  as source_upgrade separately from ordinary source edits.
* **archive_id is the real target.** Taken from the collector's client, so
  the manifest matches the archive the runs land in — never a hardcoded
  migration label.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
from pathlib import Path

from . import RECORD_CONTRACT, RECORD_CONTRACT_VERSION
from .errors import ArchiveError
from .reconcile import LEGACY_MAX_MISSING, LEGACY_MAX_REVISIONS
from .util import canonical, sha256_hex

LEGACY_TABLE = 'Msg_v2legacy'
SERVER_SHARD = '__v2_server__.db'
_CHUNK = 256 * 1024


# ── v2 opening + consistent full snapshot ──────────────────────────────────
def _v2_connect(v2_root):
    """Open a v2 archive read-only. v2_root is the archive root directory
    (archive.sqlite3 inside) or the db file itself."""
    root = Path(v2_root)
    db_path = root / 'archive.sqlite3' if root.is_dir() else root
    if not db_path.exists():
        raise ArchiveError('v2_archive_missing',
                           f'v2 archive not found at {db_path}')
    db = sqlite3.connect(f'file:{db_path}?mode=ro', uri=True)
    db.row_factory = sqlite3.Row
    try:
        version = db.execute('PRAGMA user_version').fetchone()[0]
        tables = {row[0] for row in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        if 'messages' not in tables:
            raise ArchiveError('v2_schema_unrecognized',
                               'no messages table — not a v2 archive')
        if version and int(version) > 2:
            raise ArchiveError('v2_schema_unrecognized',
                               f'user_version {version} is newer than v2')
    except sqlite3.Error as exc:
        raise ArchiveError('v2_schema_unrecognized',
                           f'cannot inspect v2 archive: {exc}') from exc
    return db


def v2_snapshot(v2_root, work_dir) -> Path:
    """Consistent full-DB copy of the v2 archive (backup API, read-only
    source). Every session renders from this one copy."""
    work_dir = Path(work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)
    snapshot = work_dir / 'v2-snapshot.db'
    if snapshot.exists():
        os.unlink(snapshot)
    source = _v2_connect(v2_root)
    try:
        dst = sqlite3.connect(snapshot)
        try:
            source.backup(dst)
            dst.execute('PRAGMA journal_mode=DELETE')
            dst.commit()
        finally:
            dst.close()
    finally:
        source.close()
    os.chmod(snapshot, 0o600)
    return snapshot


def v2_talkers(snapshot_or_root):
    """(account, talker, rows) for every session — from the snapshot when a
    path ending .db is given, else from the live archive."""
    path = Path(snapshot_or_root)
    db = sqlite3.connect(
        f'file:{path}?mode=ro', uri=True) if path.suffix == '.db' \
        else _v2_connect(path)
    db.row_factory = sqlite3.Row
    try:
        return [dict(row) for row in db.execute(
            'SELECT account, talker, COUNT(*) AS rows FROM messages '
            'GROUP BY account, talker ORDER BY account, talker')]
    finally:
        db.close()


# ── identity mapping (namespace preservation) ──────────────────────────────
def _shard_of(v2_shard: str) -> str:
    """Contract v2 requires shard to be the DATABASE file (.db suffix). The
    verbatim v2 value always rides in raw.v2.source_shard."""
    shard = (v2_shard or 'message_v2legacy').strip()
    if shard == SERVER_SHARD:
        raise ArchiveError(
            'v2_namespace_collision',
            f"a v2 local row already uses the reserved server namespace "
            f"{SERVER_SHARD!r}")
    return shard if shard.endswith('.db') else shard + '.db'


def _identity_of(row, server_seq):
    """v2 row → v3 contract identity.

    * server rows (id_kind='server'): server_id is the identity; stream
      coordinates are sequential inside '__v2_server__.db' (the v2 values
      stay in raw.v2) — v2 server rows have source_shard NULL and possibly
      colliding local_ids, so their coordinates cannot be a namespace.
    * local rows: the ORIGINAL (source_shard, local_id) IS the v3 local
      identity — verbatim, never renumbered (only the contract .db suffix
      is applied)."""
    if row['id_kind'] == 'server':
        if row['server_id'] is None:
            raise ArchiveError(
                'v2_row_unmappable',
                'server row without server_id cannot be mapped',
                {'message_id': row['message_id']})
        return {'shard': SERVER_SHARD, 'database': SERVER_SHARD,
                'table': LEGACY_TABLE, 'local_rowid': server_seq,
                'server_id': str(int(row['server_id']))}
    if row['local_id'] is None:
        raise ArchiveError(
            'v2_row_unmappable',
            'local row without local_id cannot keep its namespace',
            {'message_id': row['message_id']})
    shard = _shard_of(row['source_shard'])
    return {'shard': shard, 'database': shard, 'table': LEGACY_TABLE,
            'local_rowid': int(row['local_id']), 'server_id': None}


def _v2_columns(row):
    """Every v2 column, lossless (payload/media_files parsed)."""
    out = {}
    for key in row.keys():
        value = row[key]
        if key in ('payload', 'media_files') and isinstance(value, str):
            try:
                value = json.loads(value)
            except ValueError:
                pass
        out[key] = value
    return out


def _media_refs_and_copy(row, v2_root, media_dir, copied):
    """v2 media_files paths → present refs; assets copied hash-verified.

    v2 stores assets content-addressed (sources/assets/<sha256>/asset) —
    the bytes ARE in a v2 archive; each copy must re-hash to its address.
    An asset missing from disk becomes present=False (an honest gap need),
    never a fabricated presence."""
    refs = []
    for rel in json.loads(row['media_files'] or '[]'):
        parts = Path(rel).parts
        if len(parts) >= 4 and parts[0] == 'sources' and parts[-3] == 'assets' \
                and parts[-1] == 'asset':
            sha = parts[-2]
        else:
            # non-content-addressed v2 path: audited as unknown structure
            refs.append({'ref_key': f'v2legacy:{rel}', 'kind': 'file',
                         'present': False,
                         'reason': 'v2_media_path_not_content_addressed'})
            continue
        source = Path(v2_root) / 'sources' / 'assets' / sha / 'asset'
        if not source.exists():
            refs.append({'ref_key': f'v2legacy:{sha}', 'kind': 'file',
                         'present': False,
                         'reason': 'v2_asset_bytes_missing'})
            continue
        target = media_dir / sha
        if sha not in copied:
            digest = hashlib.sha256()
            size = 0
            with open(source, 'rb') as src, open(target, 'wb') as dst:
                while True:
                    block = src.read(_CHUNK)
                    if not block:
                        break
                    digest.update(block)
                    size += len(block)
                    dst.write(block)
            if digest.hexdigest() != sha:
                raise ArchiveError(
                    'v2_asset_corrupt',
                    'v2 asset fails its content address; refusing to '
                    'migrate unverified bytes',
                    {'sha256': sha, 'length': size,
                     'actual_sha256': digest.hexdigest()})
            copied.add(sha)
        refs.append({'ref_key': f'v2legacy:{sha}', 'kind': 'file',
                     'present': True, 'filename': f'media/{sha}',
                     'sha256': sha,
                     'length': int(target.stat().st_size)})
    return refs


def _carry_source_tree(v2_root, media_dir, copied):
    """Carry EVERY file under v2 sources/ into the export as a
    content-addressed object: source views (sources/<digest>/export.json)
    AND asset bytes (sources/assets/<sha256>/asset) — the v2 side keeps both
    as proof of what was observed, so the v3 side keeps them as objects.

    Each file is streamed (bounded), hashed WHILE copied, and staged under
    its TRUE content hash — the object address is always the bytes that
    actually travelled. The v2 originals are only ever read."""
    sources_root = Path(v2_root) / 'sources'
    entries = []
    if not sources_root.is_dir():
        return entries
    for path in sorted(p for p in sources_root.rglob('*') if p.is_file()):
        sha = sha256_hex(path.read_bytes()) if path.stat().st_size < _CHUNK \
            else _hash_file(path)
        rel = path.relative_to(Path(v2_root)).as_posix()
        entries.append({'ref_key': f'v2source:{rel}', 'kind': 'source-view',
                        'sha256': sha, 'length': int(path.stat().st_size),
                        'filename': f'media/{sha}'})
        if sha in copied:
            continue
        target = media_dir / sha
        digest = hashlib.sha256()
        with open(path, 'rb') as src, open(target, 'wb') as dst:
            while True:
                block = src.read(_CHUNK)
                if not block:
                    break
                digest.update(block)
                dst.write(block)
        if digest.hexdigest() != sha:
            raise ArchiveError(
                'v2_asset_corrupt',
                'v2 sources file changed while being copied; refusing '
                'unverified bytes',
                {'path': rel, 'sha256': sha})
        copied.add(sha)
    return entries


def _hash_file(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as fh:
        while True:
            block = fh.read(_CHUNK)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def _record_of(row, ident, refs, source_present):
    """v2 row → contract-v2 record with a lossless raw block."""
    v2 = _v2_columns(row)
    payload = v2.get('payload') if isinstance(v2.get('payload'), dict) else {}
    text = row['original_text'] or row['snippet'] or ''
    local_type = 0
    candidate = payload.get('type') if isinstance(payload, dict) else None
    if isinstance(candidate, int) and not isinstance(candidate, bool):
        local_type = candidate
    raw = {
        # promoted contract views (int-or-null of the equivalent v2 facts)
        'create_time': int(row['create_time']),
        'sort_seq': int(row['sort_seq']),
        'local_type': local_type,
        'status': 0,
        # server_id raw: only SERVER rows carry it (contract: identity.
        # server_id is its decimal-string form); local rows must not gain
        # one — a v2 local row with a stray sid keeps it in raw.v2 only
        'server_id': int(row['server_id']) if row['id_kind'] == 'server'
        else None,
        'message_content': text,
        # the COMPLETE verbatim v2 row, lossless and separate
        'v2': dict(v2, source_view_present=bool(source_present)),
    }
    record = {
        'identity': ident,
        'create_time': int(row['create_time']),
        'sort_seq': int(row['sort_seq']),
        'local_type': local_type, 'sub_type': 0, 'status': 0,
        'message_content': text,
        'message_content_raw_b64': None,
        'message_content_raw_type': 'text',
        'message_content_decode_status': 'ok',
        'packed_info_data': None, 'packed_info_sha256': None,
        'media_refs': refs,
        # legacy sender identity: v2's sender column IS the account id the
        # row was captured for (the resolved-identity equivalent of the Rust
        # contract's real_sender_name) and sender_display_name its explicit
        # contact name — neither is fabricated on the v3 side
        'real_sender_name': row['sender'] or None,
        'real_sender_display_name': row['sender_display_name'] or None,
        'raw': raw,
    }
    body = dict(record)
    record['record_sha256'] = sha256_hex(canonical(body).encode('utf-8'))
    return record


def _legacy_block(snapshot, v2_root, account, talker, idents):
    """v2 revisions + missing marks + honesty audit for one session."""
    revisions = []
    for rev in snapshot.execute(
            'SELECT * FROM revisions WHERE account=? AND talker=? '
            'ORDER BY revised_at, payload_hash', (account, talker)):
        ident = idents.get((rev['id_kind'], rev['message_id']))
        if ident is None:
            continue   # revision without a message row: audited below
        payload = rev['payload']
        try:
            payload = json.loads(payload)
        except (ValueError, TypeError):
            pass
        revisions.append({'identity': ident,
                          'payload': payload,
                          'payload_hash': rev['payload_hash'],
                          'revised_at': int(rev['revised_at'] or 0)})
    missing = []
    for row in snapshot.execute(
            'SELECT id_kind, message_id FROM messages WHERE account=? AND '
            'talker=? AND missing_in_source=1', (account, talker)):
        ident = idents.get((row['id_kind'], row['message_id']))
        if ident is not None:
            missing.append({'identity': ident})
    return revisions, missing


# ── per-session rendering from the snapshot ────────────────────────────────
def export_v2_session(snapshot_db, v2_root, out_dir, account, talker,
                      archive_id, snapshot_created_at) -> Path:
    """Render one (account, talker) session as a contract-v2 export dir,
    directly from the consistent snapshot copy. The claim must already have
    been taken (claim-first) — this writes the view."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    media_dir = out_dir / 'media'
    media_dir.mkdir(exist_ok=True)

    rows = snapshot_db.execute(
        'SELECT * FROM messages WHERE account=? AND talker=? '
        'ORDER BY create_time, sort_seq, id_kind, message_id',
        (account, talker)).fetchall()
    conversation = snapshot_db.execute(
        'SELECT name FROM conversations WHERE account=? AND talker=?',
        (account, talker)).fetchone()

    copied = set()
    records = []
    idents = {}
    local_keys = set()
    server_seq = 0
    unknown_sources = 0
    for row in rows:
        if row['id_kind'] == 'server':
            server_seq += 1
            ident = _identity_of(row, server_seq)
            key = (SERVER_SHARD, LEGACY_TABLE, server_seq)
        else:
            ident = _identity_of(row, 0)
            shard = ident['shard']
            key = (shard, LEGACY_TABLE, int(row['local_id']))
            if key in local_keys:
                raise ArchiveError(
                    'v2_namespace_collision',
                    'two v2 local rows map to the same (shard, local_id) '
                    'namespace slot',
                    {'shard': shard, 'local_id': row['local_id']})
            local_keys.add(key)
        idents[(row['id_kind'], row['message_id'])] = ident
        digest = row['source']
        source_present = digest is not None and \
            (Path(v2_root) / 'sources' / digest / 'export.json').exists()
        if not source_present:
            unknown_sources += 1
        refs = _media_refs_and_copy(row, v2_root, media_dir, copied)
        records.append((key, _record_of(row, ident, refs, source_present)))

    # stream order: strictly ascending (shard, table, rowid)
    records.sort(key=lambda item: item[0])
    ordered = [record for _key, record in records]

    revisions, missing = _legacy_block(snapshot_db, v2_root, account, talker,
                                       idents)
    # the whole v2 sources tree rides as run-level objects (source views
    # AND assets) — history proof, not just message-bound media
    source_tree = _carry_source_tree(v2_root, media_dir, copied)
    if len(revisions) > LEGACY_MAX_REVISIONS:
        raise ArchiveError(
            'legacy_block_too_large',
            f'session carries {len(revisions)} v2 revisions (cap '
            f'{LEGACY_MAX_REVISIONS}); migrate this talker in smaller '
            f'slices')
    if len(missing) > LEGACY_MAX_MISSING:
        raise ArchiveError(
            'legacy_block_too_large',
            f'session carries {len(missing)} v2 missing marks (cap '
            f'{LEGACY_MAX_MISSING}); migrate this talker in smaller slices')

    shard_rows = {}
    for record in ordered:
        ident = record['identity']
        bucket = shard_rows.setdefault(
            (ident['shard'], ident['table']), 0)
        shard_rows[(ident['shard'], ident['table'])] = bucket + 1
    media_entries = len(copied)
    manifest = {
        'contract': RECORD_CONTRACT, 'version': RECORD_CONTRACT_VERSION,
        'kind': 'session_export',
        'archive_id': archive_id,
        'account': account, 'talker': talker,
        'snapshot': {
            'generation': f'v2-migration-{account}-{sha256_hex(talker.encode())[:8]}',
            # the snapshot copy's completion clock — metadata only, never
            # causal (the claim is the causal token)
            'created_at': int(snapshot_created_at),
            'databases': [{'file': shard, 'sha256': sha256_hex(shard.encode()),
                           'bytes': 0}
                          for shard in sorted({k[0] for k in shard_rows})],
        },
        'session': {
            'talker_name': (conversation['name'] if conversation else talker),
            'name_hash': 'v2',
            'shards': [{'database': shard, 'table': table, 'row_count': count}
                       for (shard, table), count in sorted(shard_rows.items())],
        },
        'enumeration': {'complete': True, 'unknown_tables': [],
                        'unknown_columns': {}},
        'records': {'file': 'records.jsonl', 'count': len(ordered)},
        'media_dir': 'media',
        'ordering': '(shard,local_rowid) ascending',
        'media': {'dir': 'media', 'complete': True,
                  'attach_root_provided': False,
                  'staged_count': media_entries,
                  'unavailable_count': 0, 'decode_failures': 0,
                  'kinds_without_local_bytes': {}, 'notes': [
                      'v2 migration: assets are content-addressed '
                      '(sources/assets/<sha256>/asset) and were copied '
                      'hash-verified'],
                  'sample_unavailable': []},
        'migration': {'from': 'v2', 'rows': len(ordered),
                      'revisions': len(revisions),
                      'missing_in_source_rows': len(missing),
                      'source_views_missing': unknown_sources,
                      'assets_copied': media_entries},
        'legacy': {
            'provenance_override': 'legacy_visibility_projected',
            'revisions': revisions,
            'missing': missing,
            'sources': source_tree,
            'audit': {
                'unknown': unknown_sources,
                'notes': [
                    f'v2 source view (sources/<digest>/export.json) no '
                    f'longer on disk for {unknown_sources} row(s); payload '
                    f'migrated, provenance untraceable'] if unknown_sources
                else [],
            },
        },
    }
    (out_dir / 'export.json').write_text(canonical(manifest), encoding='utf-8')
    with open(out_dir / 'records.jsonl', 'w', encoding='utf-8') as fh:
        for record in ordered:
            fh.write(canonical(record) + '\n')
    return out_dir


def migrate_v2(v2_root, collector, work_dir, *, account=None,
               dry_run=False, talkers=None):
    """Migrate every (or one account's, or explicit) v2 session into v3 via
    the normal claim-first collector path. Returns an aggregate report;
    dry_run renders from the snapshot and reports what WOULD travel."""
    work_dir = Path(work_dir)
    snapshot_path = v2_snapshot(v2_root, work_dir)
    snapshot_at = int(time.time() * 1000)
    snapshot_db = sqlite3.connect(f'file:{snapshot_path}?mode=ro', uri=True)
    snapshot_db.row_factory = sqlite3.Row
    try:
        sessions = talkers if talkers is not None else v2_talkers(snapshot_path)
        archive_id = collector.client.archive_id
        stamp = time.strftime('%Y%m%d-%H%M%S')
        report = {'sessions': [], 'dry_run': bool(dry_run),
                  'snapshot': str(snapshot_path),
                  'archive_id': archive_id}
        for index, session in enumerate(sessions):
            acct, talker = session['account'], session['talker']
            if account is not None and acct != account:
                continue
            export_dir = work_dir / 'v2-exports' / stamp / f'{index:05d}'
            if dry_run:
                export_v2_session(snapshot_db, v2_root, export_dir, acct,
                                  talker, archive_id, snapshot_at)
                report['sessions'].append({
                    'account': acct, 'talker': talker,
                    'v2_rows': session['rows'], 'dry_run': True,
                    'export': str(export_dir)})
                continue
            # claim FIRST: the run is opened before the view is written, so
            # re-migrations may upgrade already-migrated rows legitimately
            def write_view(_claim, _acct=acct, _talker=talker,
                           _dir=export_dir):
                export_v2_session(snapshot_db, v2_root, _dir, _acct, _talker,
                                  archive_id, snapshot_at)
            collector.claim_source_snapshot(export_dir, acct, talker,
                                            source_runner=write_view)
            result = collector.collect_session(export_dir)
            receipt = result['receipt']
            entry = {'account': acct, 'talker': talker,
                     'v2_rows': session['rows'],
                     'export': str(export_dir),
                     'counts': receipt['counts'],
                     'conflicts': receipt['conflicts'],
                     'warnings': receipt['warnings']}
            report['sessions'].append(entry)
    finally:
        snapshot_db.close()
    totals = {}
    for entry in report['sessions']:
        for key, value in (entry.get('counts') or {}).items():
            totals[key] = totals.get(key, 0) + value
    report['totals'] = totals
    return report
