"""Mac-side collector: consumes S3 complete-record session exports
(nas-contract v2) and drives the capture protocol over a transport.

Design invariants (controller-reviewed):

* **Bounded everywhere.** Records stream from records.jsonl line by line; media
  files stream to the wire in ≤1 MiB chunks and are never held whole; the run
  itself travels as content-addressed PAGE objects (`record_pages`,
  `observation_pages`, `recheck.identity_pages`), so no frame ever carries a
  whole large session. The two O(rows) structures that remain by design — the
  previous identity map (diff base) and the current identity summary — hold
  one small entry per archived row, never record bodies.
* **One run, one transaction.** Pages are received and physically verified
  page-by-page, but the endpoint applies ALL of them inside a single FULL
  transaction: completeness is never faked by splitting a reconcile across
  several commits.
* **Restart- and ACK-loss-safe.** The batch intent (ids, page/media manifests,
  upload marks) is persisted durably Mac-side BEFORE anything is offered to
  the wire; uploads are content-addressed and idempotent; `batch_status` /
  `run_status` recover a committed-but-unACKed batch or an open run; local
  staging is released only after the ACK.
* **Raw vs decoded is not a contradiction.** `raw.message_content` carries the
  ORIGINAL source bytes (BLOB/compressed/invalid-UTF-8) while the top-level
  `message_content` is the DECODED text — the consistency check only enforces
  equality where the Rust producer guarantees it (see _check_raw_consistency).
* **Missing is a second opinion, never a guess.** collect_session only
  REPORTS missing candidates; submit_missing publishes them from a NEWER,
  complete recheck export with matching shard topology. A recheck that is
  stale (older snapshot) or topologically unknown REFUSES the submission.
* **Causal capture order.** The REAL flow claims the server run + C0 BEFORE
  the source view is read (`claim_source_snapshot` → source runner →
  collect); a claimed export may only ride its claimed run, so anything
  committed after the claim is protected by the per-row C0 gate. A
  PRE-GENERATED export has no causal token — its snapshot clock is only a
  staleness GUARD (server refuses strictly-older or clock-unknown views),
  never a total order.
* **Honest media taxonomy.** length/hash/permission failures are HARD
  failures (the export is inconsistent); only proven source-side absence
  becomes an open need; a file that vanishes between export and upload is a
  `vanished` gap; undecodable source bytes are an `error` gap.
"""
from __future__ import annotations

import json
import shutil
import time
import uuid
from pathlib import Path

from . import RECORD_CONTRACT, RECORD_CONTRACT_VERSION
from .errors import ArchiveError, ExportError, ProtocolError
from .util import b64e, canonical, sha256_hex, write_json_durable
from .wire import ROLE_COLLECTOR, make_request

CHUNK_BYTES = 1024 * 1024  # wire chunk bound (b64-expanded stays under 8 MiB)
PAGE_ROW_LIMIT = 2000              # row ops per records page
PAGE_BYTE_TARGET = 4 * 1024 * 1024  # jsonl bytes per page file
OBS_PAGE_ENTRIES = 20000           # identity entries per observation page
MAX_RUN_ATTEMPTS = 8               # run-id suffixes before giving up
MAX_BATCH_ATTEMPTS = 8
CLAIM_FILE = 'snapshot-claim.json'  # causal binding of a run to a source view

# server-side defaults mirrored for pre-flight checks; the endpoint's hello
# response carries the authoritative numbers and overrides these
SERVER_DEFAULTS = {
    'chunk_max_bytes': CHUNK_BYTES,
    'max_page_line_bytes': 8 * 1024 * 1024,
    'max_total_rows': 2_000_000,
    'max_record_pages': 8192,
    'max_media_uploads': 20000,
}


# ── contract v2 export loading (streaming) ────────────────────────────────
class SessionExport:
    """A loaded export. `records` is a LAZY validated stream: each iteration
    re-reads records.jsonl, validates every record, and verifies the manifest
    count at exhaustion — loading never materialises the record bodies."""

    def __init__(self, manifest, root, media_root):
        self.manifest = manifest
        self.root = Path(root)
        self.media_root = media_root
        self.account = manifest['account']
        self.talker = manifest['talker']
        self.archive_id = manifest['archive_id']
        self.enumeration_complete = bool(
            manifest.get('enumeration', {}).get('complete'))
        snapshot = manifest.get('snapshot') or {}
        self.generation = snapshot.get('generation')
        self.created_at = snapshot.get('created_at')
        declared = manifest.get('records') or {}
        self.declared_count = declared.get('count')
        self.records_path = self.root / (declared.get('file') or 'records.jsonl')
        # databases actually enumerable from the session section (topology)
        self.session_dbs = sorted({
            shard.get('database') for shard
            in (manifest.get('session', {}).get('shards') or [])
            if shard.get('database')})

    @property
    def records(self):
        """Validated record stream (a fresh generator per access)."""
        return iter_records(self.records_path, self.declared_count)

    def export_key(self):
        """Stable identity of THIS export: sha256 of the manifest bytes —
        binds a persisted batch intent to the exact export it was planned
        from (a re-export of the same generation produces a new key only if
        the manifest content actually changed)."""
        return sha256_hex((self.root / 'export.json').read_bytes())


def iter_records(records_path, declared_count):
    """Stream records.jsonl with full per-record validation: identity shape,
    raw/top consistency (only where the producer guarantees equality),
    record_sha256 recompute, ordering, media_refs shape. The manifest count
    is verified when the stream is exhausted."""
    order = None
    count = 0
    try:
        fh = open(records_path, 'r', encoding='utf-8')
    except FileNotFoundError as exc:
        raise ExportError('export_missing',
                          f'{records_path.name} absent') from exc
    with fh:
        for index, line in enumerate(fh):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except ValueError as exc:
                raise ExportError('export_record_invalid',
                                  f'record #{index} is not JSON: {exc}')
            if not isinstance(record, dict):
                raise ExportError('export_record_invalid',
                                  f'record #{index} is not an object')
            ident = record.get('identity')
            _validate_identity(ident, index)
            _check_raw_consistency(record, index)
            claimed = record.get('record_sha256')
            if not isinstance(claimed, str):
                raise ExportError('export_record_invalid',
                                  f'record #{index} missing record_sha256')
            fingerprint_input = dict(record)
            fingerprint_input.pop('record_sha256')
            actual = sha256_hex(canonical(fingerprint_input).encode('utf-8'))
            if actual != claimed:
                raise ExportError(
                    'export_record_hash_mismatch',
                    f'record #{index} record_sha256 does not match content',
                    {'claimed': claimed, 'actual': actual})
            for i, ref in enumerate(record.get('media_refs') or []):
                if not isinstance(ref, dict) or not ref.get('ref_key'):
                    raise ExportError('export_record_invalid',
                                      f'record #{index} media_refs[{i}] malformed')
                if ref.get('present') and (not isinstance(ref.get('length'), int)
                                           or not ref.get('sha256')):
                    raise ExportError(
                        'export_record_invalid',
                        f'record #{index} media_refs[{i}]: present=true '
                        'requires length+sha256')
            key = (ident['shard'], ident['table'], int(ident['local_rowid']))
            if order is not None and key <= order:
                raise ExportError('export_order_violation',
                                  f'record #{index} out of order or duplicated: {key}')
            order = key
            count += 1
            yield record
    if declared_count is not None and int(declared_count) != count:
        raise ExportError('export_count_mismatch',
                          f'manifest count {declared_count} != {count} records')


def _validate_identity(ident, index):
    if not isinstance(ident, dict):
        raise ExportError('export_record_invalid',
                          f'record #{index} identity is not an object')
    shard = ident.get('shard')
    table = ident.get('table')
    rowid = ident.get('local_rowid')
    if not isinstance(shard, str) or not shard.endswith('.db'):
        raise ExportError(
            'export_record_invalid',
            f'record #{index}: identity.shard must be the DATABASE file '
            f'(message_N.db), got {shard!r} (contract v2)')
    if not isinstance(table, str) or not table:
        raise ExportError('export_record_invalid',
                          f'record #{index}: identity.table missing '
                          '(contract v2: local identity needs the table)')
    if not isinstance(rowid, int) or rowid < 0:
        raise ExportError('export_record_invalid',
                          f'record #{index}: local_rowid must be an int >= 0')
    server_id = ident.get('server_id')
    if server_id is not None and not isinstance(server_id, (str, int)):
        raise ExportError('export_record_invalid',
                          f'record #{index}: server_id must be decimal-string/int/null')


def _raw_scalar(raw_value):
    """The raw column value if it is a plain scalar (null/number/string), or
    None if it is a base64-wrapped blob/invalid-UTF-8 object."""
    if isinstance(raw_value, (str, int, float)) or raw_value is None:
        return raw_value
    return None


def _check_raw_consistency(record, index):
    """Top-level promoted fields vs the lossless `raw` object — enforcing
    equality ONLY where the Rust producer guarantees it.

    Rust (crates/wx-cli/src/cmd/archive_inspect.rs):
      * create_time/sort_seq/local_type/status are `int_or_null` of the raw
        column: an INTEGER raw value must appear verbatim; any non-integer
        raw (null/real/text/blob) promotes to null;
      * `message_content` is the DECODED text while `raw.message_content`
        holds the ORIGINAL bytes — they legitimately differ whenever the row
        is compressed, stored as BLOB, or not valid UTF-8. Equality is only
        guaranteed when storage was TEXT, decoding was clean (status "ok"),
        and nothing needed preserving (message_content_raw_b64 absent);
      * `sub_type` is DERIVED (high 32 bits of local_type), never a source
        column — never compared;
      * packed_info_data is base64 of the raw bytes: it must decode to the
        raw column's bytes, and a null promotion is only legal for a
        null/empty raw value;
      * identity.server_id is the decimal-string form of the raw integer
        (null exactly when the raw value is missing, non-integer, or 0).
    """
    raw = record.get('raw')
    if not isinstance(raw, dict):
        raise ExportError('export_record_invalid',
                          f'record #{index}: raw (complete original row) missing')

    for promoted in ('create_time', 'sort_seq', 'local_type', 'status'):
        if promoted not in record or promoted not in raw:
            continue
        raw_value = _raw_scalar(raw[promoted])
        if raw_value is None:
            continue  # wrapped blob/non-scalar — no equality guaranteed
        if isinstance(raw_value, int) and not isinstance(raw_value, bool):
            expected = raw_value
        else:
            expected = None  # int_or_null promotes non-integers to null
        if record[promoted] != expected:
            raise ExportError(
                'export_raw_inconsistent',
                f'record #{index}: top-level {promoted} disagrees with raw '
                f'({record[promoted]!r} != promoted {expected!r}); raw is authoritative')

    # identity.server_id ↔ raw.server_id (decimal-string vs raw integer)
    if 'server_id' in raw:
        raw_value = _raw_scalar(raw['server_id'])
        if raw_value is not None:
            expected = str(raw_value) if isinstance(raw_value, int) \
                and not isinstance(raw_value, bool) and raw_value != 0 else None
            if record.get('identity', {}).get('server_id') != expected:
                raise ExportError(
                    'export_raw_inconsistent',
                    f'record #{index}: identity.server_id '
                    f'{record.get("identity", {}).get("server_id")!r} does not '
                    f'match raw server_id {raw_value!r}')

    # message_content: equality only in the guaranteed round-trip case
    if 'message_content' in raw:
        raw_value = raw['message_content']
        if isinstance(raw_value, str) and \
                record.get('message_content_raw_b64') is None and \
                record.get('message_content_decode_status') == 'ok' and \
                record.get('message_content_raw_type') == 'text':
            if record.get('message_content') != raw_value:
                raise ExportError(
                    'export_raw_inconsistent',
                    f'record #{index}: uncompressed text message_content '
                    'differs from raw; raw is authoritative')

    # packed_info_data = base64(raw bytes); null promotion only for empty/null
    if 'packed_info_data' in raw:
        raw_value = raw['packed_info_data']
        top = record.get('packed_info_data')
        if isinstance(raw_value, dict) and 'b64' in raw_value:
            raw_bytes = b64_bytes(raw_value)
            if top is not None and b64_bytes({'b64': top}) != raw_bytes:
                raise ExportError(
                    'export_raw_inconsistent',
                    f'record #{index}: packed_info_data does not decode to '
                    'the raw packed_info_data bytes')
        elif isinstance(raw_value, str):
            if top is not None and b64_bytes({'b64': top}) != raw_value.encode('utf-8'):
                raise ExportError(
                    'export_raw_inconsistent',
                    f'record #{index}: packed_info_data does not decode to '
                    'the raw packed_info_data bytes')


def b64_bytes(wrapped):
    import base64
    try:
        return base64.b64decode(wrapped['b64'].encode('ascii'), validate=True)
    except Exception as exc:  # noqa: BLE001 - becomes a rejection below
        raise ExportError('export_record_invalid',
                          f'base64 field is not valid base64: {exc}') from exc


def load_session_export(export_dir):
    """The ONLY entry point for exports (contract §Preamble). Strict; records
    stream lazily (see SessionExport.records)."""
    export_dir = Path(export_dir)
    manifest_path = export_dir / 'export.json'
    try:
        manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    except FileNotFoundError as exc:
        raise ExportError('export_missing', 'export.json absent') from exc
    except (ValueError, UnicodeDecodeError) as exc:
        raise ExportError('export_manifest_invalid',
                          f'export.json unreadable: {exc}') from exc
    if not isinstance(manifest, dict) or \
            manifest.get('contract') != RECORD_CONTRACT:
        raise ExportError('export_contract_version',
                          'unknown contract name', {'contract': manifest.get('contract')})
    version = manifest.get('version')
    if version != RECORD_CONTRACT_VERSION:
        raise ExportError('export_contract_version',
                          f'contract version {version!r} not accepted '
                          f'(expected {RECORD_CONTRACT_VERSION})')
    if manifest.get('kind') != 'session_export':
        raise ExportError('export_manifest_invalid',
                          f"kind {manifest.get('kind')!r} is not a session export")
    for field in ('archive_id', 'account', 'talker'):
        value = manifest.get(field)
        if not isinstance(value, str) or not value:
            raise ExportError('export_manifest_invalid',
                              f'{field} must be a non-empty string')
    snapshot = manifest.get('snapshot')
    if not isinstance(snapshot, dict) or not snapshot.get('generation') or \
            not isinstance(snapshot.get('databases'), list):
        raise ExportError('export_manifest_invalid',
                          'snapshot.generation/databases required')
    if not (export_dir / 'records.jsonl').exists():
        raise ExportError('export_missing', 'records.jsonl absent')
    media_root = None
    media_dir = manifest.get('media_dir')
    if media_dir and (export_dir / media_dir).is_dir():
        media_root = export_dir / media_dir
    return SessionExport(manifest, export_dir, media_root)


# ── capture protocol client (collector role) ─────────────────────────────
class CaptureClient:
    """Drives the collector-role ops over a transport. Responses with ok=false
    raise the endpoint's ArchiveError verbatim (codes are the contract)."""

    def __init__(self, transport, archive_id, token):
        self.transport = transport
        self.archive_id = archive_id
        self.token = token
        self._req_id = 0

    def _call(self, op, **fields):
        self._req_id += 1
        frame = make_request(self._req_id, op, **fields)
        frame['role'] = ROLE_COLLECTOR
        response = self.transport.request(frame)
        if not response.get('ok'):
            raise ArchiveError.from_json(response.get('error') or {})
        return response.get('result') or {}

    def hello(self, epoch=None):
        return self._call('hello', role=ROLE_COLLECTOR, token=self.token,
                          archive_id=self.archive_id, epoch=epoch)

    def begin_run(self, run_id, account, talker, kind='capture', *,
                  claim=False):
        return self._call('begin_run', run_id=run_id, account=account,
                          talker=talker, kind=kind, claim=claim)

    def run_status(self, run_id):
        return self._call('run_status', run_id=run_id)

    def get_observation(self, account, talker):
        return self._call('get_observation', account=account, talker=talker)

    def observation_ids(self, account, talker, after=None, limit=None):
        fields = {'account': account, 'talker': talker}
        if after is not None:
            fields['after'] = after
        if limit is not None:
            fields['limit'] = limit
        return self._call('observation_ids', **fields)

    def upload_file(self, path, expected_sha256, expected_length, batch_id,
                    kind='asset'):
        """Stream a local file to staging in bounded chunks. The client half
        of double-sided verification: length is checked before the first
        chunk, the content hash is computed WHILE streaming and must equal
        the export's claim BEFORE finish_upload — a mismatched file is never
        offered for commit."""
        from .util import sha256_hex as _sha
        import hashlib
        upload_id = 'up-' + expected_sha256[:24]
        self._call('begin_upload', upload_id=upload_id, kind=kind,
                   expected_sha256=expected_sha256,
                   expected_length=int(expected_length),
                   batch_id=batch_id)
        digest = hashlib.sha256()
        length = 0
        chunks = 0
        with open(path, 'rb') as fh:
            while True:
                block = fh.read(CHUNK_BYTES)
                if not block:
                    break
                digest.update(block)
                length += len(block)
                self._call('upload_chunk', upload_id=upload_id, seq=chunks,
                           data_b64=b64e(block), sha256=_sha(block))
                chunks += 1
        if length != int(expected_length) or digest.hexdigest() != expected_sha256:
            raise ExportError(
                'media_file_mismatch',
                f'{path.name} changed under the export claims '
                f'(length {length} != {expected_length} or hash mismatch)',
                {'expected_sha256': expected_sha256,
                 'actual_sha256': digest.hexdigest(),
                 'expected_length': int(expected_length), 'actual_length': length})
        result = self._call('finish_upload', upload_id=upload_id,
                            chunk_count=chunks)
        if result.get('sha256') != expected_sha256 or \
                int(result.get('length', -1)) != int(expected_length):
            raise ProtocolError('upload_verification_failed',
                                'endpoint confirmed different content',
                                {'expected': expected_sha256, 'confirmed': result})
        return {'sha256': expected_sha256, 'length': int(expected_length),
                'kind': kind}

    def commit_batch(self, batch_id, run, media_uploads):
        content_sha256 = sha256_hex(canonical(run).encode('utf-8'))
        return self._call('commit_batch', batch_id=batch_id, run=run,
                          content_sha256=content_sha256,
                          media_uploads=media_uploads)

    def batch_status(self, batch_id):
        return self._call('batch_status', batch_id=batch_id)

    def abort_batch(self, batch_id):
        return self._call('abort_batch', batch_id=batch_id)


# ── bounded page staging (Mac side) ───────────────────────────────────────
class PageWriter:
    """Writes jsonl pages under the batch dir with row/byte bounds; each page
    is fsynced before close and content-addressed in its manifest."""

    def __init__(self, pages_dir, prefix, *, row_limit=PAGE_ROW_LIMIT,
                 byte_target=PAGE_BYTE_TARGET, line_limit=None):
        self.dir = Path(pages_dir)
        self.prefix = prefix
        self.row_limit = row_limit
        self.byte_target = byte_target
        self.line_limit = line_limit or SERVER_DEFAULTS['max_page_line_bytes']
        self.pages = []
        self._index = 0
        self._fh = None
        self._path = None
        self._rows = 0
        self._bytes = 0

    def add(self, obj):
        import hashlib
        line = canonical(obj).encode('utf-8') + b'\n'
        if len(line) > self.line_limit:
            raise ExportError(
                'export_record_too_large',
                f'single {self.prefix} entry of {len(line)} bytes exceeds the '
                f'wire line limit {self.line_limit}; the record cannot travel',
                {'bytes': len(line), 'limit': self.line_limit})
        if self._fh is not None and (self._rows >= self.row_limit or
                                     self._bytes + len(line) > self.byte_target):
            self._close_page()
        if self._fh is None:
            self._index += 1
            self._path = self.dir / f'{self.prefix}-{self._index:06d}.jsonl'
            self._fh = open(self._path, 'wb')
            self._digest = hashlib.sha256()
            self._rows = 0
            self._bytes = 0
        self._fh.write(line)
        self._digest.update(line)
        self._rows += 1
        self._bytes += len(line)

    def _close_page(self):
        if self._fh is None:
            return
        import os
        self._fh.flush()
        os.fsync(self._fh.fileno())
        self._fh.close()
        self.pages.append({'file': self._path.name,
                           'sha256': self._digest.hexdigest(),
                           'length': self._bytes, 'rows': self._rows})
        self._fh = None

    def close(self):
        self._close_page()
        return self.pages


def identity_from_key(key):
    """Reconstruct a minimal identity dict from an identity key (the shape
    missing ops need). s:<id> → server identity; l:<shard>:<table>:<rowid> →
    local identity (shard and table contain no ':' in real sources)."""
    if key.startswith('s:'):
        return {'server_id': key[2:]}
    if key.startswith('l:'):
        rest = key[2:]
        shard, table, rowid = rest.rsplit(':', 2)
        return {'shard': shard, 'table': table, 'local_rowid': int(rowid)}
    raise ExportError('export_record_invalid', f'unparseable identity key {key!r}')


# ── run construction ──────────────────────────────────────────────────────
def _gap_need(ident, ref):
    """Honest taxonomy for a present=false media ref: only PROVEN source-side
    absence becomes an open need; undecodable bytes are an error gap; an
    unclassifiable reason is an error too — never silently open."""
    reason = str(ref.get('reason') or '')
    low = reason.lower()
    if 'decode' in low or 'zero bytes' in low:
        state, error = 'error', 'source_decode_failed'
    elif ('no local' in low or 'not found' in low or 'no media database' in low
          or 'exhausted' in low or 'no attach' in low):
        state, error = 'open', 'not_present_in_source'
    else:
        state, error = 'error', 'unclassified_source_gap'
    return {'identity': ident, 'ref_key': ref['ref_key'],
            'kind': ref.get('kind', 'unknown'), 'state': state,
            'error': error, 'reason': reason}


class Collector:
    """One session export → one capture run. Bounded, diff-driven, resumable,
    and explicit about every gap it leaves behind."""

    def __init__(self, client, work_dir):
        self.client = client
        self.work_dir = Path(work_dir)
        self.limits = dict(SERVER_DEFAULTS)

    # ── public entry points ──────────────────────────────────────────
    def claim_source_snapshot(self, export_dir, account, talker, *,
                              source_runner=None):
        """CAUSAL capture step 1 — take the server run + C0 BEFORE the source
        view exists.

        The run's C0 is the watermark that decides what this observation may
        overwrite: anything committed after the C0 is protected. That is only
        true if the C0 was obtained BEFORE the source was read — a C0 minted
        after an export already exists says nothing about when the view was
        taken and would let a delayed OLD snapshot overwrite a NEWER capture.

        Writes `snapshot-claim.json` durably into export_dir and only THEN
        invokes `source_runner(claim)` (the export producer — injectable so
        tests drive a controlled runner; the real CLI runs the source
        exporter here). The claim binds run_id + c0 + epoch + account/talker;
        collect_session refuses to mint a different C0 for a claimed export.
        """
        export_dir = Path(export_dir)
        export_dir.mkdir(parents=True, exist_ok=True)
        claim_path = export_dir / CLAIM_FILE
        if claim_path.exists():
            # a claim already binds this directory; reusing it keeps the
            # original causal position (idempotent re-invocation) — but the
            # durable JSON is EVIDENCE, not authority: every field is
            # re-verified against the SERVER (epoch, run binding, lifecycle,
            # c0) before the source runner may read anything
            try:
                existing = json.loads(claim_path.read_text(encoding='utf-8'))
            except (ValueError, UnicodeDecodeError, OSError) as exc:
                raise ArchiveError(
                    'claim_corrupt',
                    f'snapshot claim {claim_path} is unreadable; preserved '
                    'as evidence — the source view ordering cannot be '
                    'verified without it',
                    {'path': str(claim_path)}) from exc
            if isinstance(existing, dict) and existing.get('run_id') \
                    and 'c0' in existing:
                self._verify_claim_authority(existing, account, talker)
                if source_runner is not None:
                    source_runner(existing)
                return existing
        # A claim is only CAUSAL if the run precedes the source view. If an
        # export already sits here, the view was read before any claim —
        # minting a C0 now would retroactively bless that stale view and let
        # it overwrite history committed after the view was taken. Refuse
        # (fail closed); the evidence on disk is preserved untouched.
        if (export_dir / 'export.json').exists() or \
                (export_dir / 'records.jsonl').exists():
            raise ArchiveError(
                'claim_not_causal',
                'cannot claim a source view that already exists — '
                'claim_source_snapshot must run BEFORE the export is '
                'produced; a claim minted after the read authorizes nothing '
                '(the accepted history would be overwritten by a view of '
                'unknown age)',
                {'path': str(export_dir)})
        hello = self.client.hello()
        self.limits = dict(SERVER_DEFAULTS)
        self.limits.update(hello.get('limits') or {})
        run_id = 'run-' + sha256_hex(
            f"{self.client.archive_id}:{account}:{talker}:"
            f"{time.time_ns()}:{uuid.uuid4()}".encode('utf-8'))[:16]
        c0 = self.client.begin_run(run_id, account, talker,
                                   claim=True)['c0']
        claim = {
            'version': 1, 'kind': 'capture',
            'run_id': run_id, 'c0': int(c0), 'epoch': hello.get('epoch'),
            'archive_id': self.client.archive_id,
            'account': account, 'talker': talker,
            'claimed_at_ms': int(time.time() * 1000),
        }
        write_json_durable(claim_path, claim)
        if source_runner is not None:
            source_runner(claim)
        return claim

    def _verify_claim_authority(self, claim, account, talker):
        """Server-side verification of a durable claim BEFORE its source view
        may be (re)read. The on-disk JSON proves nothing by itself — a tampered
        account/epoch/c0 must never start a capture. Every field is checked
        against the live server: hello (epoch), run_status (binding, claimed
        lifecycle, persisted run_start_seq). Any disagreement raises and the
        source runner is never invoked."""
        if not isinstance(claim, dict) or not claim.get('run_id') or \
                'c0' not in claim:
            raise ArchiveError(
                'claim_corrupt',
                'snapshot claim is malformed; preserved as evidence',
                {'claim': claim})
        if claim.get('archive_id') != self.client.archive_id or \
                claim.get('account') != account or claim.get('talker') != talker:
            raise ArchiveError(
                'claim_binding_mismatch',
                'snapshot claim is bound to a different archive/account/'
                'talker than requested',
                {'claim': {k: claim.get(k) for k
                           in ('archive_id', 'account', 'talker')},
                 'requested': {'archive_id': self.client.archive_id,
                               'account': account, 'talker': talker}})
        hello = self.client.hello()
        self.limits = dict(SERVER_DEFAULTS)
        self.limits.update(hello.get('limits') or {})
        if claim.get('epoch') is not None and \
                int(claim['epoch']) != int(hello.get('epoch')):
            raise ArchiveError(
                'claim_epoch_stale',
                'the archive epoch moved after this claim was taken; the '
                'claimed run belongs to a previous epoch — re-claim and '
                're-read the source',
                {'claim_epoch': claim['epoch'], 'server_epoch': hello.get('epoch')})
        try:
            status = self.client.run_status(claim['run_id'])
        except ArchiveError as exc:
            raise ArchiveError(
                'claim_run_unusable',
                f'the claimed run cannot be verified on the server '
                f'({exc.code}); the source view ordering is unprovable — '
                're-claim and re-read the source',
                {'run_id': claim['run_id'], 'cause': exc.code}) from exc
        if status.get('status') != 'open' or \
                status.get('account') != account or \
                status.get('talker') != talker or \
                not status.get('claimed') or \
                int(status.get('run_start_seq', -1)) != int(claim['c0']):
            raise ArchiveError(
                'claim_run_unusable',
                'the claimed run is not an open, claimed, matching server run '
                'with the claimed c0; the source view ordering is unprovable '
                '— re-claim and re-read the source',
                {'run_id': claim['run_id'], 'server': status,
                 'claim_c0': claim['c0']})

    def collect_session(self, export_dir, *, run_id=None):
        export = load_session_export(export_dir)
        claim = self._load_claim(export)
        hello = self.client.hello()
        self.limits = dict(SERVER_DEFAULTS)
        self.limits.update(hello.get('limits') or {})

        # run identity: a CLAIMED export is bound to the run taken before its
        # source view existed (claim-first flow) — that run is the only C0
        # this view may ever use. An unclaimed pre-generated export falls
        # back to the export IDENTITY (manifest sha256, not the generation
        # label: re-exported content is a NEW observation, a byte-identical
        # re-export replays idempotently); its stale-view protection is the
        # server-side snapshot_created_at guard, not a causal C0.
        export_key = export.export_key()
        if claim is not None:
            base_run = claim['run_id']
        else:
            base_run = run_id or 'run-' + sha256_hex(
                f"{export.archive_id}:{export.account}:{export.talker}:"
                f"{export_key}".encode('utf-8'))[:16]
        batch_dir = self.work_dir / 'batches' / base_run
        intent = self._load_intent(batch_dir, export)
        if intent is None and batch_dir.exists():
            shutil.rmtree(batch_dir, ignore_errors=True)

        run_id, batch_id, replay, c0, attempt = self._resolve_ids(
            export, base_run, (intent or {}).get('attempt', 1), claim=claim)
        if claim is not None and replay is None and \
                int(c0) != int(claim['c0']):
            raise ArchiveError(
                'claim_binding_mismatch',
                'the server run bound to this snapshot claim carries a '
                'different run_start_seq than the claim; the claimed causal '
                'position is gone — re-claim and re-read the source',
                {'claim_c0': int(claim['c0']), 'server_c0': int(c0),
                 'run_id': run_id})
        if replay is not None:
            # committed but never ACKed: recover the receipt, release staging
            self._release(batch_dir)
            return {'receipt': replay, 'run_id': run_id, 'batch_id': batch_id,
                    'replay': True, 'missing_candidates': [],
                    'snapshot_created_at': export.created_at}

        if intent is None or intent.get('batch_id') != batch_id:
            header = self.client.get_observation(export.account, export.talker)
            prev_ids = self._walk_prev_ids(export, header)
            plan = self._plan(export, batch_dir, prev_ids,
                              header.get('observed_at'))
            intent = {
                'version': 1, 'state': 'planned',
                'base_run_id': base_run, 'run_id': run_id, 'batch_id': batch_id,
                'attempt': attempt, 'export_key': export.export_key(),
                'account': export.account, 'talker': export.talker,
                'archive_id': export.archive_id, 'generation': export.generation,
                'pages': [dict(p, uploaded=False) for p in plan['pages']],
                'obs_pages': [dict(p, uploaded=False) for p in plan['obs_pages']],
                'media': [dict(m, uploaded=False) for m in plan['media']],
            }
            self._write_intent(batch_dir, intent)
        plan = json.loads((batch_dir / 'plan.json').read_text(encoding='utf-8'))

        # ── upload phase: idempotent, resume-aware, bounded memory ──
        media_uploads = []
        needs = list(plan['needs'])
        for entry in intent['media']:
            if not entry.get('uploaded'):
                outcome = self._upload_media(export, batch_id, entry)
                if outcome is None:
                    entry['uploaded'] = True
                    self._write_intent(batch_dir, intent)
                else:
                    # vanished between export and upload: record the gap; a
                    # LATER invocation retries (the file may legitimately
                    # reappear — the export dir is a static snapshot copy)
                    needs.append(outcome)
            if not entry.get('uploaded'):
                continue
            media_uploads.append({'sha256': entry['sha256'],
                                  'length': entry['length'],
                                  'kind': entry['kind']})
        if len(media_uploads) > self.limits['max_media_uploads']:
            raise ArchiveError(
                'media_uploads_exceeded',
                'distinct media objects over the per-run limit; split the '
                'session capture',
                {'limit': self.limits['max_media_uploads'],
                 'actual': len(media_uploads)})
        for entry in intent['pages']:
            if not entry.get('uploaded'):
                self.client.upload_file(batch_dir / 'pages' / entry['file'],
                                        entry['sha256'], entry['length'],
                                        batch_id, kind='records-page')
                entry['uploaded'] = True
                self._write_intent(batch_dir, intent)
        for entry in intent['obs_pages']:
            if not entry.get('uploaded'):
                self.client.upload_file(batch_dir / 'pages' / entry['file'],
                                        entry['sha256'], entry['length'],
                                        batch_id, kind='observation-page')
                entry['uploaded'] = True
                self._write_intent(batch_dir, intent)

        record_pages = [{'sha256': p['sha256'], 'length': p['length'],
                         'rows': p['rows']} for p in intent['pages']]
        obs_pages = [{'sha256': p['sha256'], 'length': p['length'],
                      'rows': p['rows']} for p in intent['obs_pages']]
        run = {
            'run_id': run_id, 'account': export.account,
            'talker': export.talker, 'kind': 'capture', 'c0': c0,
            'snapshot_created_at': export.created_at,
            'rows': [],
            'record_pages': record_pages,
            'observation_header': plan['header'],
            'observation_pages': obs_pages,
            'media': {'uploads': media_uploads, 'needs': needs},
            'recheck': {'complete': export.enumeration_complete,
                        'shards': export.session_dbs,
                        'identity_pages': obs_pages},
        }
        # v2 migration block (provenance override + historical revisions +
        # v2 missing marks + honesty audit) rides the export manifest and is
        # passed through VERBATIM into the run for the reconciler
        if isinstance(export.manifest.get('legacy'), dict):
            run['legacy'] = export.manifest['legacy']
        receipt = self.client.commit_batch(batch_id, run, media_uploads)
        self._release(batch_dir)  # staging freed only after the ACK
        return {'receipt': receipt, 'run_id': run_id, 'batch_id': batch_id,
                'replay': False,
                'missing_candidates': plan['missing_candidates'],
                'snapshot_created_at': export.created_at,
                'counts': plan['counts']}

    def submit_missing(self, recheck_export_dir, *, candidates,
                       baseline_created_at, run_id=None):
        """Publish missing candidates from a SECOND, fresher, complete export.

        Refusal conditions (controller directive): the recheck must be a
        complete enumeration, at least as fresh as the export that produced
        the candidates, and every LOCAL-only candidate's shard database must
        still be part of the session topology — a row that moved shards
        (WeChat 迁片) is not missing, and an unknown topology can never prove
        absence. Server-identity candidates are topology-independent."""
        export = load_session_export(recheck_export_dir)
        claim = self._load_claim(export)
        if claim is None:
            # absence is only provable from a view whose run predates it;
            # claim the recheck BEFORE reading the source (the server
            # double-checks runs.claimed and refuses unclaimed missing ops)
            raise ArchiveError(
                'missing_requires_claim',
                'missing may only be published from a CLAIMED recheck — '
                'claim_source_snapshot must run before the recheck source '
                'view is read; an unclaimed view cannot prove absence')
        if not export.enumeration_complete:
            raise ArchiveError(
                'missing_recheck_incomplete',
                'missing may only be published from a COMPLETE recheck export')
        if baseline_created_at is not None and \
                int(export.created_at or 0) < int(baseline_created_at):
            raise ArchiveError(
                'missing_recheck_stale',
                'recheck export predates the observation that produced the '
                'candidates; a stale view cannot prove absence',
                {'recheck_created_at': export.created_at,
                 'baseline_created_at': baseline_created_at})
        hello = self.client.hello()
        self.limits.update(hello.get('limits') or {})

        if claim is not None:
            base_run = claim['run_id']
        else:
            base_run = run_id or 'run-' + sha256_hex(
                f"{export.archive_id}:{export.account}:{export.talker}:"
                f"{export.export_key()}:missing".encode('utf-8'))[:16]
        batch_dir = self.work_dir / 'batches' / base_run
        shutil.rmtree(batch_dir, ignore_errors=True)
        batch_dir.mkdir(parents=True, exist_ok=True)
        (batch_dir / 'pages').mkdir(parents=True, exist_ok=True)

        # one streaming pass: fresh-view identity map (+ observation pages)
        identities, _shards, _pages, _media, _needs, _rows = \
            self._scan_identities(export, batch_dir, write_rows=False)
        obs_writer = PageWriter(batch_dir / 'pages', 'obs',
                                row_limit=OBS_PAGE_ENTRIES,
                                line_limit=self.limits['max_page_line_bytes'])
        for key in sorted(identities):
            obs_writer.add({'k': key, **identities[key]})
        obs_pages = obs_writer.close()
        unknown = export.manifest.get('enumeration') or {}
        header = {'version': RECORD_CONTRACT_VERSION, 'shards': _shards,
                  'enumeration_complete': True,
                  'snapshot_created_at': export.created_at,
                  'unknown': {'tables': unknown.get('unknown_tables') or [],
                              'columns': unknown.get('unknown_columns') or {}}}

        accepted = []
        refused = []
        for candidate in candidates:
            key = candidate.get('key') or identity_key_of(candidate)
            if key in identities:
                continue  # visible again in the fresh view — not missing
            ident = candidate.get('identity') or identity_from_key(key)
            if ident.get('server_id') is None and \
                    ident.get('shard') not in export.session_dbs:
                refused.append({'key': key,
                                'reason': 'missing_topology_changed'})
                continue
            accepted.append({'op': 'missing_candidate', 'identity': ident})

        run_id, batch_id, replay, c0, _attempt = self._resolve_ids(
            export, base_run, 1, claim=claim)
        if claim is not None and replay is None and \
                int(c0) != int(claim['c0']):
            raise ArchiveError(
                'claim_binding_mismatch',
                'the server run bound to this recheck claim carries a '
                'different run_start_seq than the claim',
                {'claim_c0': int(claim['c0']), 'server_c0': int(c0),
                 'run_id': run_id})
        if replay is not None:
            self._release(batch_dir)
            return {'receipt': replay, 'submitted': 0, 'refused': refused,
                    'replay': True}
        if not accepted:
            return {'submitted': 0, 'refused': refused, 'receipt': None}

        for page in obs_pages:  # pages were written under batch_dir by scan
            self.client.upload_file(batch_dir / 'pages' / page['file'],
                                    page['sha256'], page['length'], batch_id,
                                    kind='observation-page')
        obs_manifests = [{'sha256': p['sha256'], 'length': p['length'],
                          'rows': p['rows']} for p in obs_pages]
        run = {
            'run_id': run_id, 'account': export.account,
            'talker': export.talker, 'kind': 'capture', 'c0': c0,
            'snapshot_created_at': export.created_at,
            'rows': accepted,  # bounded by the server's anomaly gate
            'observation_header': header,
            'observation_pages': obs_manifests,
            'media': {'uploads': [], 'needs': []},
            'recheck': {'complete': True, 'shards': export.session_dbs,
                        'identity_pages': obs_manifests},
        }
        receipt = self.client.commit_batch(batch_id, run, [])
        self._release(batch_dir)
        return {'receipt': receipt, 'submitted': len(accepted),
                'refused': refused, 'replay': False}

    # ── internals ────────────────────────────────────────────────────
    def _load_intent(self, batch_dir, export):
        """Load the persisted batch intent.

        * absent → None (fresh plan);
        * bound to a DIFFERENT export → None (stale attempt of a superseded
          export; safe to discard — every server-side step is idempotent);
        * unreadable/corrupt, or present without its plan → HARD refusal.
          The intent file is evidence of what was in flight; it is preserved
          byte-for-byte and never replanned over — mirroring the server-side
          upload-intent rule (a corrupt durable intent is never silently
          re-initialised)."""
        path = batch_dir / 'intent.json'
        if not path.exists():
            return None
        try:
            intent = json.loads(path.read_text(encoding='utf-8'))
        except (ValueError, UnicodeDecodeError) as exc:
            raise ArchiveError(
                'intent_corrupt',
                f'local batch intent {path} is unreadable; preserved as '
                'evidence — inspect and remove it manually to replan',
                {'path': str(path)}) from exc
        except OSError as exc:
            raise ArchiveError(
                'intent_corrupt',
                f'local batch intent {path} cannot be read: {exc}',
                {'path': str(path)}) from exc
        if not isinstance(intent, dict):
            raise ArchiveError(
                'intent_corrupt',
                f'local batch intent {path} is not an object; preserved as '
                'evidence — inspect and remove it manually to replan',
                {'path': str(path)})
        if not (batch_dir / 'plan.json').exists():
            raise ArchiveError(
                'intent_corrupt',
                f'local batch intent {path} exists without its plan; '
                'preserved as evidence — inspect and remove it manually',
                {'path': str(path)})
        if intent.get('export_key') != export.export_key() or \
                intent.get('account') != export.account or \
                intent.get('talker') != export.talker:
            return None
        return intent

    def _write_intent(self, batch_dir, intent):
        batch_dir.mkdir(parents=True, exist_ok=True)
        write_json_durable(batch_dir / 'intent.json', intent)

    def _release(self, batch_dir):
        """Free local staging — only ever called after the ACK (or a
        recovered commit)."""
        shutil.rmtree(batch_dir, ignore_errors=True)

    def _resolve_ids(self, export, base_run, attempt_hint, claim=None):
        """Pick a (run_id, batch_id) that is safe to (re)use, recovering:
        * committed batch → its receipt (ACK-loss recovery, no new work);
        * open run → its persisted c0 (crash-after-begin recovery);
        * finished/foreign run or aborted/rejected batch → next suffix.
        Every branch re-verifies the server-side binding before reuse.

        A CLAIMED export may only ever ride the claimed run: its C0 is the
        causal position of the source view. If that run has become unusable
        we FAIL CLOSED — minting a fresh C0 for an already-read view is
        exactly the ordering bug the claim exists to prevent."""
        attempt = int(attempt_hint or 1)
        while attempt <= MAX_RUN_ATTEMPTS:
            run_id = base_run if attempt == 1 else f'{base_run}-{attempt}'
            # 1) is this attempt already committed? (lost ACK / plain rerun)
            chosen_batch = None
            for n in range(1, MAX_BATCH_ATTEMPTS + 1):
                batch_id = f'{run_id}-b{n}'
                status = self.client.batch_status(batch_id)
                state = status.get('state')
                if state == 'committed':
                    receipt = status.get('receipt')
                    if receipt is None:
                        raise ArchiveError(
                            'batch_committed_without_receipt',
                            'server reports the batch committed but carries '
                            'no receipt; refuse to claim success',
                            {'batch_id': batch_id})
                    return run_id, batch_id, receipt, None, attempt
                if state in (None, 'unknown', 'pending'):
                    chosen_batch = batch_id
                    break
                # aborted / rejected → try the next batch suffix
            if chosen_batch is None:
                if claim is not None:
                    raise ArchiveError(
                        'claim_run_unusable',
                        'every batch slot of the claimed run is spent; a new '
                        'run would mint a fresh C0 for an already-read '
                        'source view — re-claim and re-read the source',
                        {'run_id': run_id})
                attempt += 1
                continue
            # 2) open (or reuse) the run this batch belongs to
            c0 = None
            try:
                c0 = self.client.begin_run(run_id, export.account,
                                           export.talker, 'capture')['c0']
            except ArchiveError as exc:
                if exc.code != 'run_exists':
                    raise
                status = self.client.run_status(run_id)
                if status.get('status') == 'open' and \
                        status.get('account') == export.account and \
                        status.get('talker') == export.talker and \
                        status.get('kind') == 'capture':
                    c0 = int(status['run_start_seq'])
                elif claim is not None:
                    raise ArchiveError(
                        'claim_run_unusable',
                        'the claimed run is no longer open on the server; a '
                        'new run would mint a fresh C0 for an already-read '
                        'source view — re-claim and re-read the source',
                        {'run_id': run_id, 'status': status.get('status')})
                else:
                    attempt += 1
                    continue
            return run_id, chosen_batch, None, c0, attempt
        raise ArchiveError(
            'collect_ids_exhausted',
            'no usable run/batch id after repeated attempts; investigate '
            'server state before retrying')

    def _load_claim(self, export):
        """Load the snapshot claim binding this export to a pre-read server
        run. Absent → None (unclaimed pre-generated export; stale-view
        protection falls to the server-side clock guard). Unreadable or
        malformed → HARD refusal with the file preserved as evidence;
        foreign binding (another archive/account/talker) → refusal.

        The claim file lives in the CLAIM directory — the real CLI claims an
        empty dir first, then the exporter produces a fresh payload/ subdir
        into it (the Rust exporter refuses non-empty out dirs). A payload dir
        therefore resolves its claim from the PARENT, which is the directory
        that was empty when the claim was written."""
        path = export.root / CLAIM_FILE
        if not path.exists():
            parent_claim = export.root.parent / CLAIM_FILE
            if parent_claim.exists():
                path = parent_claim
            else:
                return None
        try:
            claim = json.loads(path.read_text(encoding='utf-8'))
        except (ValueError, UnicodeDecodeError) as exc:
            raise ArchiveError(
                'claim_corrupt',
                f'snapshot claim {path} is unreadable; preserved as '
                'evidence — the source view ordering cannot be verified '
                'without it',
                {'path': str(path)}) from exc
        except OSError as exc:
            raise ArchiveError(
                'claim_corrupt',
                f'snapshot claim {path} cannot be read: {exc}',
                {'path': str(path)}) from exc
        if not isinstance(claim, dict) or not claim.get('run_id') \
                or 'c0' not in claim:
            raise ArchiveError(
                'claim_corrupt',
                f'snapshot claim {path} is malformed; preserved as '
                'evidence — inspect and remove it manually to re-plan',
                {'path': str(path)})
        if claim.get('archive_id') != export.archive_id or \
                claim.get('account') != export.account or \
                claim.get('talker') != export.talker:
            raise ArchiveError(
                'claim_binding_mismatch',
                'snapshot claim belongs to a different archive/account/'
                'talker than the export manifest',
                {'claim': {k: claim.get(k) for k in
                           ('archive_id', 'account', 'talker')},
                 'export': {'archive_id': export.archive_id,
                            'account': export.account,
                            'talker': export.talker}})
        return claim

    def _walk_prev_ids(self, export, header):
        """Previous observation identities as {key: fp}. Inline when the
        server still returns it whole; otherwise chunked via observation_ids.
        Size is one small entry per archived row — the diff base."""
        inline = header.get('observation')
        if isinstance(inline, dict):
            return {key: (value or {}).get('fp') for key, value
                    in (inline.get('identities') or {}).items()}
        if not header.get('observation_oversize') and \
                not header.get('identity_count'):
            return {}
        prev_ids = {}
        after = None
        while True:
            page = self.client.observation_ids(export.account, export.talker,
                                               after=after)
            for entry in page.get('entries') or []:
                prev_ids[entry['k']] = entry.get('fp')
            after = page.get('next')
            if not after:
                return prev_ids

    def _scan_identities(self, export, batch_dir=None, write_rows=True):
        """One streaming pass: identity summary (fp + refs), shard stats,
        and — when write_rows — record pages, media plan and gap needs."""
        if batch_dir is not None and write_rows:
            pages_dir = batch_dir / 'pages'
            pages_dir.mkdir(parents=True, exist_ok=True)
            writer = PageWriter(pages_dir, 'rec',
                                line_limit=self.limits['max_page_line_bytes'])
        else:
            writer = None
        identities = {}
        shards = {}
        media_plan = {}
        needs = []
        rows = 0
        prev_ids = getattr(self, '_plan_prev_ids', {}) or {}
        prev_observed_ms = getattr(self, '_plan_prev_ms', 0) or 0
        for record in export.records:
            ident = record['identity']
            key = identity_key_of_record(record)
            rows += 1
            if writer is not None:
                seen = key in prev_ids
                payload = record
                if not seen and prev_observed_ms and \
                        int(record.get('create_time') or 0) * 1000 < int(prev_observed_ms):
                    payload = dict(record)
                    payload['late'] = True
                writer.add({'op': 'upsert', 'identity': ident,
                            'record': payload})
            shard_key = f"{ident['shard']}:{ident['table']}"
            bucket = shards.setdefault(shard_key, {'row_count': 0,
                                                   'max_rowid': 0})
            bucket['row_count'] += 1
            bucket['max_rowid'] = max(bucket['max_rowid'],
                                      int(ident['local_rowid']))
            refs = record.get('media_refs') or []
            identities[key] = {'fp': record.get('record_sha256'),
                               'refs': [r['ref_key'] for r in refs]}
            for ref in refs:
                if ref.get('present'):
                    sha = ref.get('sha256')
                    if sha and sha not in media_plan:
                        media_plan[sha] = {
                            'sha256': sha, 'length': int(ref['length']),
                            'kind': ref.get('kind', 'asset'),
                            'filename': ref['filename'],
                            'ref_key': ref['ref_key'], 'identity': ident}
                else:
                    needs.append(_gap_need(ident, ref))
        # v2 migration source tree (source views + assets) rides the run as
        # run-level objects — history proof carried alongside the records
        for ref in ((export.manifest.get('legacy') or {}).get('sources')
                    or []):
            sha = ref.get('sha256')
            if sha and sha not in media_plan and ref.get('filename'):
                media_plan[sha] = {
                    'sha256': sha, 'length': int(ref['length']),
                    'kind': ref.get('kind', 'source-view'),
                    'filename': ref['filename'],
                    'ref_key': ref['ref_key'], 'identity': None}
        pages = writer.close() if writer is not None else []
        return identities, shards, pages, media_plan, needs, rows

    def _plan(self, export, batch_dir, prev_ids, prev_observed_ms):
        """Full planning pass → durable plan.json. Streams every record once;
        memory holds only the identity summary, the media plan (one entry per
        DISTINCT content hash) and the gap needs."""
        batch_dir.mkdir(parents=True, exist_ok=True)
        self._plan_prev_ids = prev_ids
        self._plan_prev_ms = prev_observed_ms or 0
        identities, shards, pages, media_plan, needs, rows = \
            self._scan_identities(export, batch_dir)
        missing = []
        if export.enumeration_complete:
            missing = [{'key': key, 'identity': identity_from_key(key)}
                       for key in prev_ids if key not in identities]
        # observation identity pages: sorted so the server rebuild is stable
        obs_writer = PageWriter(batch_dir / 'pages', 'obs',
                                row_limit=OBS_PAGE_ENTRIES,
                                line_limit=self.limits['max_page_line_bytes'])
        for key in sorted(identities):
            obs_writer.add({'k': key, **identities[key]})
        obs_pages = obs_writer.close()
        unknown = export.manifest.get('enumeration') or {}
        plan = {
            'needs': needs,
            'missing_candidates': missing,
            'header': {'version': RECORD_CONTRACT_VERSION,
                       'shards': shards,
                       'enumeration_complete': export.enumeration_complete,
                       'snapshot_created_at': export.created_at,
                       'unknown': {'tables': unknown.get('unknown_tables') or [],
                                   'columns': unknown.get('unknown_columns') or {}}},
            'pages': pages,
            'obs_pages': obs_pages,
            'media': list(media_plan.values()),
            'counts': {'rows': rows, 'media': len(media_plan),
                       'needs': len(needs), 'missing': len(missing)},
        }
        write_json_durable(batch_dir / 'plan.json', plan)
        return plan

    def _upload_media(self, export, batch_id, entry):
        """Upload one media file (client-side verification first). Returns
        None on success, or a `vanished` need when the file disappeared after
        the export — length/hash/permission problems are HARD failures."""
        path = (export.root / entry['filename']).resolve()
        if not path.is_relative_to(export.root.resolve()):
            raise ExportError('media_path_escape_rejected',
                              f"export media filename escapes the export dir: "
                              f"{entry['filename']!r}")
        import os
        try:
            size = os.stat(path).st_size
        except FileNotFoundError:
            return {'identity': entry.get('identity'),
                    'ref_key': entry['ref_key'], 'kind': entry['kind'],
                    'state': 'vanished', 'error': 'media_vanished_midflight'}
        except PermissionError as exc:
            raise ArchiveError('media_permission_denied',
                               f'cannot read staged media {entry["filename"]}: {exc}')
        if size != int(entry['length']):
            raise ExportError(
                'media_file_mismatch',
                f'{entry["filename"]} length {size} != export claim '
                f'{entry["length"]}',
                {'filename': entry['filename'], 'expected': entry['length'],
                 'actual': size})
        try:
            self.client.upload_file(path, entry['sha256'], entry['length'],
                                    batch_id, kind=entry['kind'])
        except PermissionError as exc:
            raise ArchiveError('media_permission_denied',
                               f'cannot read staged media {entry["filename"]}: {exc}')
        return None


def identity_key_of(candidate):
    from .util import identity_key
    ident = candidate.get('identity') or {}
    return identity_key(ident.get('server_id'), ident.get('shard'),
                        ident.get('table'), int(ident.get('local_rowid', 0)))


def identity_key_of_record(record):
    from .util import identity_key
    ident = record['identity']
    return identity_key(ident.get('server_id'), ident['shard'],
                        ident['table'], int(ident['local_rowid']))
