"""NAS endpoint: the forced-command stdio server and the staging manager.

This is the single writer. Every op arrives as a wire frame, is dispatched
per role, and every mutation lands in one master FULL transaction that also
writes data + receipt + row-level mutation log. Physical durability precedes
the database transaction: staged objects are fsynced (file and directory)
BEFORE the FULL tx touches the batch.

Upload discipline: begin_upload durably records the session server-side;
chunks are hash-verified individually; finish_upload assembles STREAMING
(bounded memory for large assets), verifies total length+sha256, and
finalises an immutable content-addressed object. Replaying finish with the
same hash is idempotent; a different hash for the same upload_id is a
conflict, never an overwrite.

Real-NAS behaviour is UNVERIFIED (no device); everything here is proven
against the local-subprocess transport with synthetic fixtures.
"""
from __future__ import annotations

import hashlib
import os
import sys
from bisect import bisect_right

from .errors import (ArchiveError, AuthError, MasterError, ProtocolError,
                     ReconcileError)
from .guard import GuardError
from .master import (BATCH_ABORTED, BATCH_COMMITTED, BATCH_PENDING,
                     BATCH_REJECTED, MasterStore)
from .reconcile import Receipt, RunApplier
from .util import canonical, sha256_hex
from .wire import (MAX_FRAME_BYTES, ROLE_COLLECTOR, ROLE_OPS, ROLE_QUERY, decode_frame,
                   encode_frame, make_response, validate_request)

CHUNK_MAX_BYTES = 1024 * 1024  # wire chunk bound (b64-expanded under frame cap)
AUTH_FILE = 'auth.json'
STAGING_DIR = 'staging'
UPLOADS_DIR = 'staging/uploads'
STAGE_OBJECTS_DIR = 'staging/objects'
OBJECTS_DIR = 'objects'

# ── machine-readable capacity limits (also announced in hello) ───────────
# A capture run arrives PAGE-BY-PAGE as content-addressed staging objects
# (`run.record_pages` = row-op jsonl pages, `run.observation_pages` /
# `run.recheck.identity_pages` = identity-summary jsonl pages); the endpoint
# materialises them STREAMING inside the one FULL apply transaction. Rejection
# at every limit is a machine-readable ProtocolError carrying {limit, actual}.
MAX_RECORD_PAGES = 8192          # pages per run (rows split across them)
MAX_TOTAL_ROWS = 2_000_000       # declared rows (inline + pages) per run
MAX_PAGE_LINE_BYTES = 8 * 1024 * 1024   # one jsonl line inside a page object
MAX_MEDIA_UPLOADS = 20000        # distinct media objects listed in one run
OBSERVATION_INLINE_MAX_BYTES = 1024 * 1024  # get_observation inline cutoff
OBSERVATION_IDS_PAGE_DEFAULT = 5000
OBSERVATION_IDS_PAGE_MAX = 20000


class StagingManager:
    """Content-addressed staging store, all access through the guard binding."""

    def __init__(self, binding):
        self.binding = binding

    # ── upload sessions ──────────────────────────────────────────────
    def begin_upload(self, upload_id, kind, expected_sha256, expected_length,
                     batch_id):
        if not isinstance(upload_id, str) or len(upload_id) < 8 or \
                len(upload_id) > 128 or any(c in upload_id for c in '/\x00'):
            raise ProtocolError('upload_id_invalid', 'upload_id malformed')
        session_path = f'{UPLOADS_DIR}/{upload_id}/session.json'
        try:
            existing = self.binding.read_json(session_path)
        except GuardError as exc:
            if exc.code != 'path_missing':
                raise  # symlink/permission problems fail closed
            existing = None
        except (ValueError, UnicodeDecodeError) as exc:
            # a corrupt durable intent is NEVER silently re-initialised —
            # that would erase the record of what was supposed to land here
            raise ProtocolError('upload_intent_corrupt',
                                f'staged upload session unreadable: {exc}',
                                {'upload_id': upload_id})
        if existing is not None:
            if existing.get('expected_sha256') != expected_sha256 or \
                    int(existing.get('expected_length', -1)) != int(expected_length):
                raise ProtocolError('upload_conflict',
                                    'upload_id reused with different content')
            return existing
        session = {
            'upload_id': upload_id, 'kind': kind,
            'expected_sha256': expected_sha256,
            'expected_length': int(expected_length), 'batch_id': batch_id,
            'state': 'open',
        }
        self.binding.write_json_atomic(session_path, session)
        return session

    def write_chunk(self, upload_id, seq, data: bytes, chunk_sha256):
        if seq < 0 or not isinstance(seq, int):
            raise ProtocolError('chunk_seq_invalid', 'seq must be a non-negative int')
        if len(data) > CHUNK_MAX_BYTES:
            raise ProtocolError('chunk_too_large',
                                f'chunk {len(data)} > {CHUNK_MAX_BYTES}')
        if sha256_hex(data) != chunk_sha256:
            raise ProtocolError('chunk_hash_mismatch',
                                'chunk sha256 does not match payload',
                                {'seq': seq})
        path = f'{UPLOADS_DIR}/{upload_id}/chunks/{seq:08d}'
        self.binding.write_file_atomic(path, data)

    def _iter_chunks(self, upload_id, expected_count):
        for seq in range(expected_count):
            fd = self.binding.open_file_read(
                f'{UPLOADS_DIR}/{upload_id}/chunks/{seq:08d}')
            try:
                while True:
                    block = os.read(fd, 256 * 1024)
                    if not block:
                        break
                    yield block
            finally:
                os.close(fd)

    def finish_upload(self, upload_id, chunk_count):
        session_path = f'{UPLOADS_DIR}/{upload_id}/session.json'
        session = self.binding.read_json(session_path)
        if session.get('state') == 'finalized':
            # idempotent replay ONLY while the object physically exists — a
            # finalized session whose object was released (e.g. run pages
            # unlinked after their commit) must RE-ASSEMBLE from the still-
            # staged chunks, not claim success for bytes that are gone
            if self._staged_object_present(session['expected_sha256'],
                                           session['expected_length']):
                return {'sha256': session['expected_sha256'],
                        'length': session['expected_length'], 'replay': True}
        digest = hashlib.sha256()
        length = 0

        def stream():
            nonlocal length
            for block in self._iter_chunks(upload_id, chunk_count):
                digest.update(block)
                length += len(block)
                yield block

        target = f'{STAGE_OBJECTS_DIR}/{session["expected_sha256"]}'
        try:
            existing_fd = self.binding.open_file_read(target)
        except GuardError as exc:
            if exc.code != 'path_missing':
                raise
            existing_fd = None
        if existing_fd is not None:
            st = os.fstat(existing_fd)
            os.close(existing_fd)
            if st.st_size != session['expected_length']:
                raise ProtocolError('staging_corrupt',
                                    'existing staged object has wrong length')
            self._finalize_session(session_path, session, upload_id, chunk_count)
            return {'sha256': session['expected_sha256'],
                    'length': session['expected_length'], 'replay': True}
        self.binding.write_stream(target, stream())
        if length != session['expected_length'] or \
                digest.hexdigest() != session['expected_sha256']:
            # never leave a mismatched object behind
            try:
                self.binding._ensure_or_open(STAGE_OBJECTS_DIR)
                dir_fd = self.binding.open_dir(STAGE_OBJECTS_DIR)
                try:
                    os.unlink(session['expected_sha256'], dir_fd=dir_fd)
                finally:
                    os.close(dir_fd)
            except OSError:
                pass
            raise ProtocolError('upload_hash_mismatch',
                                'assembled object fails length/hash verification',
                                {'expected_sha256': session['expected_sha256'],
                                 'actual_length': length,
                                 'actual_sha256': digest.hexdigest()})
        self._finalize_session(session_path, session, upload_id, chunk_count)
        return {'sha256': session['expected_sha256'],
                'length': session['expected_length'], 'replay': False}

    def _finalize_session(self, session_path, session, upload_id, chunk_count):
        session = dict(session)
        session['state'] = 'finalized'
        session['chunk_count'] = chunk_count
        self.binding.write_json_atomic(session_path, session)

    # ── object verification / promotion ──────────────────────────────
    def _open_staged(self, sha256):
        """Open a staged object for reading; a MISSING object is a protocol
        rejection (client claimed an upload that never staged), not a guard
        failure — it must not kill the endpoint session."""
        try:
            return self.binding.open_file_read(f'{STAGE_OBJECTS_DIR}/{sha256}')
        except GuardError as exc:
            if exc.code == 'path_missing':
                raise ProtocolError(
                    'staging_object_missing',
                    f'staged object {sha256[:12]} does not exist',
                    {'sha256': sha256}) from exc
            raise

    def _staged_object_present(self, sha256, length):
        """True iff the staged object still physically exists with the
        expected length (cheap fstat — content was hash-verified when it was
        assembled and objects are immutable once written)."""
        try:
            fd = self.binding.open_file_read(f'{STAGE_OBJECTS_DIR}/{sha256}')
        except GuardError as exc:
            if exc.code == 'path_missing':
                return False
            raise
        try:
            return os.fstat(fd).st_size == int(length)
        finally:
            os.close(fd)

    def require_object(self, sha256, length):
        fd = self._open_staged(sha256)
        try:
            st = os.fstat(fd)
        finally:
            os.close(fd)
        if st.st_size != int(length):
            raise ProtocolError('staging_length_mismatch',
                                f'staged object {sha256[:12]} length {st.st_size} != {length}')
        return True

    def verify_object(self, sha256, length):
        """FULL physical verification of a staged object: it must exist, its
        length must match, and its CONTENT must hash to its own address —
        a missing object, wrong length, or hash mismatch can never be ACKed."""
        fd = self._open_staged(sha256)
        digest = hashlib.sha256()
        size = 0
        try:
            while True:
                block = os.read(fd, 256 * 1024)
                if not block:
                    break
                digest.update(block)
                size += len(block)
        finally:
            os.close(fd)
        if size != int(length) or digest.hexdigest() != sha256:
            raise ProtocolError(
                'staging_hash_mismatch',
                'staged object fails length/hash verification',
                {'sha256': sha256, 'expected_length': int(length),
                 'actual_length': size, 'actual_sha256': digest.hexdigest()})
        return True

    def promote(self, sha256):
        """Physical promotion BEFORE the FULL tx: move into the immutable
        objects store, fsync both directories. Idempotent per content hash."""
        shard_dir = f'{OBJECTS_DIR}/{sha256[:2]}'
        target = f'{shard_dir}/{sha256}'
        try:
            fd = self.binding.open_file_read(target)
        except GuardError as exc:
            if exc.code != 'path_missing':
                raise
            fd = None
        if fd is not None:
            os.close(fd)
            return False
        self.binding._ensure_or_open(shard_dir)
        self.binding.rename_under(STAGE_OBJECTS_DIR, sha256, shard_dir, sha256)
        return True

    def object_reader(self, sha256):
        """Context manager yielding a read fd for a committed object."""
        return self.binding.open_file_read(f'{OBJECTS_DIR}/{sha256[:2]}/{sha256}')

    def object_path(self, sha256):
        return f'{OBJECTS_DIR}/{sha256[:2]}/{sha256}'

    # ── run pages (records / observation identity summaries) ────────────
    def iter_page_json(self, sha256):
        """Stream a page object line by line as decoded JSON values. Memory is
        bounded by the largest SINGLE line (capped by MAX_PAGE_LINE_BYTES) —
        never by the page or run size. Malformed lines reject the run."""
        import json as _json
        fd = self._open_staged(sha256)
        pending = b''
        try:
            while True:
                block = os.read(fd, 256 * 1024)
                if not block:
                    break
                pending += block
                *complete, pending = pending.split(b'\n')
                for line in complete:
                    if not line.strip():
                        continue
                    if len(line) > MAX_PAGE_LINE_BYTES:
                        raise ProtocolError(
                            'page_line_too_large',
                            f'page {sha256[:12]} has a line of {len(line)} bytes',
                            {'limit': MAX_PAGE_LINE_BYTES, 'actual': len(line)})
                    try:
                        yield _json.loads(line)
                    except ValueError as exc:
                        raise ProtocolError(
                            'page_line_invalid',
                            f'page {sha256[:12]} line is not JSON: {exc}',
                            {'sha256': sha256}) from exc
                # an UNTERMINATED line already over the cap is rejected NOW —
                # the reader stops consuming the object instead of buffering
                # an arbitrarily long line to check it at EOF
                if len(pending) > MAX_PAGE_LINE_BYTES:
                    raise ProtocolError(
                        'page_line_too_large',
                        f'page {sha256[:12]} has an unterminated line over '
                        f'{len(pending)} bytes',
                        {'limit': MAX_PAGE_LINE_BYTES, 'actual': len(pending)})
            if pending.strip():
                try:
                    yield _json.loads(pending)
                except ValueError as exc:
                    raise ProtocolError(
                        'page_line_invalid',
                        f'page {sha256[:12]} final line is not JSON: {exc}',
                        {'sha256': sha256}) from exc
        finally:
            os.close(fd)

    def count_page_rows(self, sha256):
        """Count entries without materialising the page (pre-apply check)."""
        return sum(1 for _ in self.iter_page_json(sha256))

    def unlink_staged(self, sha256):
        """Best-effort removal of a transient page object after its run
        committed. Pages are content-addressed and re-uploadable, so a failed
        unlink only costs disk, never correctness; a missing object is fine."""
        try:
            dir_fd = self.binding.open_dir(STAGE_OBJECTS_DIR)
        except GuardError as exc:
            if exc.code != 'path_missing':
                raise
            return
        try:
            os.unlink(sha256, dir_fd=dir_fd)
        except FileNotFoundError:
            pass
        finally:
            os.close(dir_fd)


def _validate_page_manifest(page, index, label):
    """Structural check of one page manifest entry: content-addressed object
    with declared length and row count. Physical verification (sha256 of the
    actual bytes) happens separately via staging.verify_object."""
    if not isinstance(page, dict):
        raise ProtocolError('page_manifest_invalid',
                            f'{label}[{index}] is not an object',
                            {'list': label, 'index': index})
    sha = page.get('sha256')
    if not isinstance(sha, str) or len(sha) != 64 or \
            any(c not in '0123456789abcdef' for c in sha):
        raise ProtocolError('page_manifest_invalid',
                            f'{label}[{index}] needs a hex sha256',
                            {'list': label, 'index': index})
    length = page.get('length')
    if not isinstance(length, int) or length < 0:
        raise ProtocolError('page_manifest_invalid',
                            f'{label}[{index}] needs a non-negative length',
                            {'list': label, 'index': index})
    rows = page.get('rows')
    if not isinstance(rows, int) or rows < 0:
        raise ProtocolError('page_manifest_invalid',
                            f'{label}[{index}] needs a non-negative row count',
                            {'list': label, 'index': index})


def _validate_page_list(pages, label, *, max_pages):
    """Validate a page-manifest list and reject duplicates WITHIN it (the same
    manifest legitimately appearing in two different lists — e.g. observation
    pages reused as recheck identity pages — is fine; a duplicate inside one
    list would apply the same rows twice)."""
    if not isinstance(pages, list):
        raise ProtocolError('page_manifest_invalid',
                            f'{label} must be a list', {'list': label})
    if len(pages) > max_pages:
        raise ProtocolError(
            'run_pages_exceeded',
            f'{label} lists {len(pages)} pages over the limit',
            {'list': label, 'limit': max_pages, 'actual': len(pages)})
    seen = set()
    for i, page in enumerate(pages):
        _validate_page_manifest(page, i, label)
        if page['sha256'] in seen:
            raise ProtocolError(
                'page_duplicate',
                f'{label} lists {page["sha256"][:12]} twice; duplicate pages '
                'would apply the same rows twice',
                {'list': label, 'sha256': page['sha256']})
        seen.add(page['sha256'])
    return pages


def _upload_manifest_map(uploads, label):
    """sha256 → (length, kind) for one upload list, rejecting malformed or
    duplicate entries — a duplicate would silently collapse in a plain dict
    and hide a disagreeing manifest."""
    manifest = {}
    for i, upload in enumerate(uploads):
        if not isinstance(upload, dict) or 'sha256' not in upload \
                or 'length' not in upload:
            raise ProtocolError('media_manifest_mismatch',
                                f'{label}[{i}] needs sha256 and length',
                                {'list': label, 'index': i})
        sha = upload['sha256']
        if sha in manifest:
            raise ProtocolError('media_manifest_mismatch',
                                f'{label} lists {sha[:12]} twice',
                                {'list': label, 'sha256': sha})
        manifest[sha] = (int(upload['length']), upload.get('kind', 'asset'))
    return manifest


class Endpoint:
    """Serves one role-authenticated client over a line transport."""

    def __init__(self, binding, archive_id, projection_service_factory=None):
        self.binding = binding
        self.archive_id = archive_id
        self.staging = StagingManager(binding)
        self.store = None
        self.auth = None
        self.role = None
        self.hello_done = False
        self._projection_service = None
        self._projection_factory = projection_service_factory
        # sorted-identity cache for the chunked observation_ids walk,
        # invalidated whenever the stored observation row changes
        self._obs_ids_cache = None

    # ── lifecycle ────────────────────────────────────────────────────
    def open_master(self, *, writer=True):
        self.store = MasterStore(self.binding, self.archive_id)
        if writer:
            self.store.acquire_writer_lock()
        self.auth = self.binding.read_json(AUTH_FILE)
        if not isinstance(self.auth, dict) or 'roles' not in self.auth:
            raise AuthError('auth_config_missing', 'auth.json missing roles')

    def close(self):
        if self.store is not None:
            self.store.close()
            self.store = None

    # ── dispatch ─────────────────────────────────────────────────────
    def handle_frame(self, frame: dict) -> dict:
        try:
            req_id, op = validate_request(frame)
        except ProtocolError as exc:
            return make_response(frame.get('id'), error=exc)
        try:
            return make_response(req_id, result=self.dispatch(op, frame))
        except GuardError as exc:
            # GuardError derives from ArchiveError, so it must be handled
            # first: a lost volume binding is fatal to this endpoint.
            self.fatal = 'guard_failed'
            return make_response(req_id, error=exc)
        except ArchiveError as exc:
            return make_response(req_id, error=exc)
        except Exception as exc:  # never leak a traceback to the wire
            if isinstance(exc, (KeyError, TypeError, ValueError)):
                return make_response(req_id, error=ProtocolError(
                    'request_malformed', f'{type(exc).__name__}: {exc}'))
            self.fatal = 'internal_error'
            return make_response(req_id, error=ArchiveError(
                'internal_error', f'{type(exc).__name__}: {exc}'))

    fatal = None

    def dispatch(self, op, frame):
        if op == 'hello':
            return self.op_hello(frame)
        if not self.hello_done:
            raise ProtocolError('hello_required', 'first op must be hello')
        if op not in ROLE_OPS.get(self.role, frozenset()):
            raise AuthError('op_not_allowed_for_role',
                            f'op {op!r} not allowed for role {self.role!r}')
        handler = getattr(self, f'op_{op}', None)
        if handler is None:
            raise ProtocolError('op_unknown', f'unimplemented op {op!r}')
        return handler(frame)

    # ── session ──────────────────────────────────────────────────────
    def _epoch_of(self):
        """Current epoch. A query-role endpoint NEVER opens the master store:
        its epoch, if knowable at all, comes from the projection service
        (which subscribed to epoch rotations on its own feed)."""
        if self.store is not None:
            return self.store.epoch()
        service = self._projection()
        epoch = getattr(service, 'epoch', None)
        if callable(epoch):
            epoch = epoch()
        return epoch

    def op_hello(self, frame):
        role = frame.get('role')
        token = frame.get('token')
        archive_id = frame.get('archive_id')
        client_epoch = frame.get('epoch')
        if role not in ROLE_OPS:
            raise AuthError('role_unknown', f'unknown role {role!r}')
        if not isinstance(token, str) or not token:
            raise AuthError('auth_denied', 'token missing')
        expected = self.auth['roles'].get(role, {}).get('token_sha256')
        if not expected or sha256_hex(token.encode('utf-8')) != expected:
            raise AuthError('auth_denied', 'token rejected')
        if archive_id != self.archive_id:
            raise AuthError('archive_id_mismatch',
                            f'endpoint serves {self.archive_id!r}')
        current_epoch = self._epoch_of()
        if current_epoch is not None and client_epoch is not None and \
                int(client_epoch) != current_epoch:
            raise MasterError('epoch_stale',
                              'client epoch does not match archive epoch',
                              {'current_epoch': current_epoch})
        self.role = role
        self.hello_done = True
        # machine-readable capacity limits: the collector sizes its pages,
        # chunking and expectations against THESE numbers — never guesses
        return {'archive_id': self.archive_id, 'epoch': current_epoch,
                'protocol': 1, 'role': role,
                'limits': {
                    'chunk_max_bytes': CHUNK_MAX_BYTES,
                    'max_record_pages': MAX_RECORD_PAGES,
                    'max_total_rows': MAX_TOTAL_ROWS,
                    'max_page_line_bytes': MAX_PAGE_LINE_BYTES,
                    'max_media_uploads': MAX_MEDIA_UPLOADS,
                    'observation_inline_max_bytes': OBSERVATION_INLINE_MAX_BYTES,
                    'observation_ids_page_max': OBSERVATION_IDS_PAGE_MAX,
                }}

    def op_ping(self, frame):
        return {'alive': True, 'epoch': self._epoch_of()}

    # ── capture ops (collector) ──────────────────────────────────────
    def op_begin_run(self, frame):
        run_id = frame['run_id']
        c0 = self.store.begin_run(run_id, frame['account'], frame['talker'],
                                  frame.get('kind', 'collect'),
                                  claim=bool(frame.get('claim')))
        return {'run_id': run_id, 'c0': c0, 'epoch': self.store.epoch(),
                'claimed': bool(frame.get('claim'))}

    def op_get_observation(self, frame):
        """Header of the last stored source observation. The FULL identity map
        is returned inline only while it fits OBSERVATION_INLINE_MAX_BYTES —
        beyond that the caller walks it chunked via observation_ids (frames are
        bounded by MAX_FRAME_BYTES and a large session's identity map would
        not fit one frame; the cutoff is announced in the response)."""
        row = self.store.get_last_observation(frame['account'], frame['talker'])
        if row is None:
            return {'observation': None, 'commit_seq': None,
                    'observed_at': None, 'identity_count': 0}
        observation = None
        oversize = False
        identity_count = 0
        if row['observation_json']:
            observation = __import__('json').loads(row['observation_json'])
            identity_count = len(observation.get('identities') or {})
            if len(canonical(observation)) > OBSERVATION_INLINE_MAX_BYTES:
                observation = None
                oversize = True
        return {'observation': observation,
                'observation_oversize': oversize,
                'identity_count': identity_count,
                'commit_seq': row['commit_seq'],
                'observed_at': row['observed_at']}

    def op_run_status(self, frame):
        """Authoritative status of a run THIS server opened — the collector's
        recovery path when begin_run answers run_exists (crash before finish,
        rerun of the same export). The response carries the persisted binding
        so the client can verify the run is really its own before reusing the
        run_start_seq as c0."""
        row = self.store.get_run(frame['run_id'])
        if row is None:
            raise MasterError('run_unknown',
                              f"no run {frame['run_id']!r} was begun on this server",
                              {'run_id': frame['run_id']})
        return {'run_id': row['run_id'], 'status': row['status'],
                'run_start_seq': row['run_start_seq'], 'epoch': row['epoch'],
                'account': row['account'], 'talker': row['talker'],
                'kind': row['kind'], 'claimed': bool(row['claimed'])}

    def op_observation_ids(self, frame):
        """Chunked walk of the last observation's identity map, ascending by
        identity key, resumable via `after` (the last key of the previous
        page). Lets the collector diff a large session against the previous
        observation without ever materialising it in one frame."""
        import json as _json
        row = self.store.get_last_observation(frame['account'], frame['talker'])
        if row is None or not row['observation_json']:
            return {'entries': [], 'next': None, 'total': 0,
                    'observed_at': row['observed_at'] if row else None,
                    'commit_seq': row['commit_seq'] if row else None}
        observation = _json.loads(row['observation_json'])
        identities = observation.get('identities') or {}
        cache_key = (frame['account'], frame['talker'], row['commit_seq'])
        if self._obs_ids_cache is None or self._obs_ids_cache[0] != cache_key:
            self._obs_ids_cache = (cache_key, sorted(identities))
        keys = self._obs_ids_cache[1]
        after = frame.get('after')
        limit = min(int(frame.get('limit') or OBSERVATION_IDS_PAGE_DEFAULT),
                    OBSERVATION_IDS_PAGE_MAX)
        start = 0
        if after is not None:
            start = bisect_right(keys, after)
        page_keys = keys[start:start + limit]
        entries = [{'k': key,
                    'fp': (identities.get(key) or {}).get('fp'),
                    'refs': (identities.get(key) or {}).get('refs') or []}
                   for key in page_keys]
        nxt = page_keys[-1] if len(page_keys) == limit and \
            start + limit < len(keys) else None
        return {'entries': entries, 'next': nxt, 'total': len(keys),
                'observed_at': row['observed_at'],
                'commit_seq': row['commit_seq']}

    def op_begin_upload(self, frame):
        session = self.staging.begin_upload(
            frame['upload_id'], frame.get('kind', 'asset'),
            frame['expected_sha256'], frame['expected_length'],
            frame.get('batch_id'))
        return {'upload_id': session['upload_id'], 'state': session['state']}

    def op_upload_chunk(self, frame):
        from .util import b64d
        try:
            data = b64d(frame['data_b64'])
        except Exception as exc:
            raise ProtocolError('chunk_malformed', f'bad base64: {exc}')
        self.staging.write_chunk(frame['upload_id'], frame['seq'], data,
                                 frame['sha256'])
        return {'seq': frame['seq'], 'bytes': len(data)}

    def op_finish_upload(self, frame):
        result = self.staging.finish_upload(frame['upload_id'],
                                            int(frame['chunk_count']))
        return result

    def op_batch_status(self, frame):
        row = self.store.get_batch(frame['batch_id'])
        if row is None:
            return {'state': 'unknown'}
        result = {'state': row['state']}
        if row['receipt_json']:
            import json
            receipt = json.loads(row['receipt_json'])
            # ACK-loss recovery is epoch-bound: a batch committed in a
            # previous archive epoch (pre-restore) must never re-issue its
            # receipt as live ACK authority — the client must re-capture
            # under the new epoch instead of trusting a stale one
            if receipt.get('epoch') is not None \
                    and int(receipt['epoch']) != self.store.epoch():
                result['epoch_stale'] = True
                return result
            result['receipt'] = receipt
        if row['error_code']:
            result['error'] = {'code': row['error_code']}
            if row['error_detail_json']:
                import json
                result['error']['detail'] = json.loads(row['error_detail_json'])
        return result

    def op_abort_batch(self, frame):
        batch_id = frame['batch_id']
        row = self.store.get_batch(batch_id)
        if row is None:
            raise MasterError('batch_unknown', f'no batch {batch_id!r}')
        with self.store.full_transaction() as tx:
            ok = self.store.cas_batch_state(batch_id, BATCH_PENDING, BATCH_ABORTED)
            if not ok:
                current = self.store.get_batch(batch_id)
                raise MasterError(
                    'batch_not_pending',
                    f"batch is {current['state']!r}; only pending batches abort")
            tx.mutation('batches', batch_id, 'abort',
                        dict(self.store.get_batch(batch_id) or {}),
                        {'by': self.role})
        return {'batch_id': batch_id, 'state': BATCH_ABORTED}

    def op_commit_batch(self, frame):
        batch_id = frame['batch_id']
        content_sha256 = frame['content_sha256']
        run = frame.get('run')
        media_uploads = frame.get('media_uploads') or []
        if not isinstance(run, dict):
            raise ProtocolError('request_malformed', 'run payload required')
        # Server-side verification of the client's manifest hash: the claim
        # is recomputed from the run AS RECEIVED (before any server-side
        # mutation). Mismatch rejects the batch before any state is created —
        # the claimed hash is never trusted for idempotency decisions.
        actual_sha256 = sha256_hex(canonical(run).encode('utf-8'))
        if actual_sha256 != content_sha256:
            raise ProtocolError('content_hash_mismatch',
                                'run content hash does not match the claim',
                                {'claimed': content_sha256, 'actual': actual_sha256})
        run['batch_id'] = batch_id
        run.setdefault('run_id', batch_id)

        # idempotent replay of an already-committed batch
        row = self.store.get_batch(batch_id)
        if row is not None:
            if row['state'] == BATCH_COMMITTED:
                import json
                # An idempotent re-ACK is only valid while the archive still
                # lives in the run's epoch. A RESTORED archive was rotated to
                # a new epoch precisely to kill old-epoch ACK authority —
                # re-issuing a pre-restore receipt would resurrect it.
                run_row = self.store.get_run(row['run_id'])
                if run_row is not None and \
                        int(run_row['epoch']) != self.store.epoch():
                    raise MasterError(
                        'run_epoch_stale',
                        'batch was committed in a previous archive epoch; '
                        'restored archives never re-issue old-epoch ACKs',
                        {'run_id': row['run_id'],
                         'run_epoch': int(run_row['epoch']),
                         'current_epoch': self.store.epoch()})
                if row['content_sha256'] == content_sha256:
                    receipt = json.loads(row['receipt_json'])
                    receipt['replay'] = True
                    return receipt
                raise MasterError('batch_id_content_mismatch',
                                  'batch_id reused with different content',
                                  {'batch_id': batch_id})
            if row['state'] == BATCH_ABORTED:
                raise MasterError('batch_aborted',
                                  'batch was aborted; use a new batch_id')
            if row['state'] == BATCH_REJECTED:
                import json
                detail = json.loads(row['error_detail_json'] or '{}')
                raise ReconcileError(row['error_code'] or 'batch_rejected',
                                     'batch was rejected by policy', detail)

        # Run binding + SERVER-AUTHORITATIVE C0 — before ANY state is created.
        # The run must be one this server opened via begin_run (missing runs
        # are rejected, never assumed), bound to the same account/talker/kind,
        # still open, and from the current epoch. The client's c0 claim must
        # EQUAL the persisted runs.run_start_seq; the applier then reads C0
        # from the runs row, so a forged c0 cannot bypass capture ordering.
        run_row = self.store.verify_run_binding(
            run['run_id'], run['account'], run['talker'], run.get('kind'))
        server_c0 = int(run_row['run_start_seq'])
        client_c0 = run.get('c0')
        if client_c0 is None:
            raise MasterError('run_c0_missing',
                              'run payload must carry the c0 returned by begin_run',
                              {'run_id': run['run_id'], 'server_c0': server_c0})
        if int(client_c0) != server_c0:
            raise MasterError(
                'run_c0_mismatch',
                'client c0 does not match the server-persisted run_start_seq',
                {'run_id': run['run_id'], 'client_c0': int(client_c0),
                 'server_c0': server_c0})
        run['c0'] = server_c0

        # Media manifest cross-verification: the frame-level upload list and
        # the run manifest's media.uploads are TWO independently supplied
        # lists — neither is trusted alone. They must agree exactly (same
        # sha256 → length/kind, no duplicates, no one-sided entries), because
        # the content hash covers the run manifest while the physical objects
        # are verified from the frame list.
        frame_map = _upload_manifest_map(media_uploads, 'media_uploads')
        run_map = _upload_manifest_map(
            (run.get('media') or {}).get('uploads') or [], 'run.media.uploads')
        if frame_map != run_map:
            raise ProtocolError(
                'media_manifest_mismatch',
                'frame media_uploads and run.media.uploads disagree',
                {'frame_only': sorted(set(frame_map) - set(run_map)),
                 'run_only': sorted(set(run_map) - set(frame_map)),
                 'length_kind_conflicts': sorted(
                     sha for sha in set(frame_map) & set(run_map)
                     if frame_map[sha] != run_map[sha])})

        # staged objects must be physically verified (length AND content
        # hash) before the tx — no ACK on missing/tampered assets
        for upload in media_uploads:
            self.staging.verify_object(upload['sha256'], upload['length'])

        # ── run pages: bounded content-addressed row/observation streams ──
        # A large run arrives page-by-page as staging objects referenced from
        # the run manifest (the frame itself stays small). Every page list is
        # structurally validated against machine-readable limits and every
        # object is PHYSICALLY verified — content must hash to its address —
        # before any batch state exists, exactly like media objects.
        record_pages = _validate_page_list(run.get('record_pages') or [],
                                           'run.record_pages',
                                           max_pages=MAX_RECORD_PAGES)
        obs_pages = _validate_page_list(run.get('observation_pages') or [],
                                        'run.observation_pages',
                                        max_pages=MAX_RECORD_PAGES)
        recheck = run.get('recheck')
        identity_pages = []
        if isinstance(recheck, dict):
            identity_pages = _validate_page_list(
                recheck.get('identity_pages') or [],
                'run.recheck.identity_pages', max_pages=MAX_RECORD_PAGES)
        declared_rows = sum(p['rows'] for p in record_pages) + len(run['rows'])
        if declared_rows > MAX_TOTAL_ROWS:
            raise ProtocolError(
                'run_rows_exceeded',
                'declared rows (inline + record pages) over the per-run limit',
                {'limit': MAX_TOTAL_ROWS, 'actual': declared_rows})
        if len(media_uploads) > MAX_MEDIA_UPLOADS:
            raise ProtocolError(
                'media_uploads_exceeded',
                'media upload list over the per-run limit',
                {'limit': MAX_MEDIA_UPLOADS, 'actual': len(media_uploads)})
        for page in record_pages + obs_pages + identity_pages:
            self.staging.verify_object(page['sha256'], page['length'])

        # durable server-side intent, then physical promotion (fsync first)
        if row is None:
            with self.store.full_transaction():
                self.store.insert_batch_pending(
                    batch_id, content_sha256, run['account'], run['talker'],
                    run['run_id'])
        for upload in media_uploads:
            self.staging.promote(upload['sha256'])

        receipt = Receipt(batch_id, run['run_id'])
        try:
            with self.store.full_transaction() as tx:
                applier = RunApplier(self.store)

                # Inline rows first, then every record page STREAMED — the
                # whole run (rows from every page) is applied inside this ONE
                # FULL transaction: completeness is never faked by splitting a
                # reconcile across several commits. A page whose declared row
                # count disagrees with its content rejects the batch and the
                # transaction rolls back atomically.
                def rows_stream():
                    yield from run['rows']
                    for page in record_pages:
                        seen = 0
                        for op in self.staging.iter_page_json(page['sha256']):
                            yield op
                            seen += 1
                        if seen != page['rows']:
                            raise ProtocolError(
                                'page_rows_mismatch',
                                f'page {page["sha256"][:12]} declared '
                                f'{page["rows"]} rows but contains {seen}',
                                {'sha256': page['sha256'],
                                 'declared': page['rows'], 'actual': seen})

                applier.apply(run, tx, receipt, rows=rows_stream(),
                              page_reader=self.staging.iter_page_json)
                for upload in media_uploads:
                    self.store.db.execute(
                        'INSERT INTO objects(sha256,kind,length,state,first_batch,'
                        'created_at,committed_at) VALUES(?,?,?,?,?,?,?) '
                        'ON CONFLICT(sha256) DO NOTHING',
                        (upload['sha256'], upload.get('kind', 'asset'),
                         int(upload['length']), 'committed', batch_id,
                         tx.commit_seq, tx.commit_seq))
                    tx.mutation('objects', upload['sha256'], 'object_committed',
                                dict(self.store.db.execute(
                                    'SELECT * FROM objects WHERE sha256=?',
                                    (upload['sha256'],)).fetchone() or {}),
                                {'batch_id': batch_id})
                ok = self.store.cas_batch_state(
                    batch_id, BATCH_PENDING, BATCH_COMMITTED,
                    receipt=receipt.to_json(), commit_seq=tx.commit_seq)
                if not ok:
                    current = self.store.get_batch(batch_id)
                    raise MasterError(
                        'batch_state_conflict',
                        f"batch became {current['state']!r} during commit",
                        {'state': current['state']})
                tx.mutation('batches', batch_id, 'capture_ack',
                            dict(self.store.get_batch(batch_id) or {}),
                            {'commit_seq': tx.commit_seq,
                             'epoch': receipt.epoch})
        except ArchiveError as exc:
            self._mark_rejected(batch_id, content_sha256, run, exc)
            raise
        self.store.finish_run(run['run_id'], 'reconciled',
                              {'batch_id': batch_id,
                               'commit_seq': receipt.commit_seq})
        # transient run pages: the committed replay path answers from the
        # batches row without reading them, so release the staging bytes now
        # (best-effort; content-addressed and re-uploadable on retry)
        for page in record_pages + obs_pages + identity_pages:
            self.staging.unlink_staged(page['sha256'])
        ack = receipt.to_json()
        return ack

    def _mark_rejected(self, batch_id, content_sha256, run, exc):
        try:
            with self.store.full_transaction():
                row = self.store.get_batch(batch_id)
                if row is not None and row['state'] == BATCH_PENDING:
                    self.store.db.execute(
                        'UPDATE batches SET state=?, updated_at=?, error_code=?, '
                        'error_detail_json=? WHERE batch_id=? AND state=?',
                        (BATCH_REJECTED, __import__('time').time(), exc.code,
                         canonical(exc.detail or {}), batch_id, BATCH_PENDING))
        except Exception:
            pass  # rejection bookkeeping is best-effort; the tx already rolled back

    # ── query ops (projection only — master is never readable here) ──
    def _projection(self):
        if self._projection_service is None:
            if self._projection_factory is None:
                raise ProtocolError('projection_unavailable',
                                    'endpoint has no projection service')
            self._projection_service = self._projection_factory()
        return self._projection_service

    def op_query_manifest(self, frame):
        return self._projection().manifest()

    def op_query_messages(self, frame):
        return self._projection().messages(frame['talker'],
                                            limit=frame.get('limit', 50))

    def op_query_search(self, frame):
        return self._projection().search(frame['talker'], frame['match'],
                                         limit=frame.get('limit', 20))


def serve_endpoint(endpoint: Endpoint, stdin, stdout):
    """Forced-command entry loop: JSON lines in, JSON lines out, EOF exits.
    A fatal guard/internal failure stops serving further ops (fail closed)."""
    while True:
        raw = stdin.readline(MAX_FRAME_BYTES + 1)
        if not raw:
            return 0
        if isinstance(raw, str):
            raw = raw.encode('utf-8')
        if len(raw) > MAX_FRAME_BYTES:
            exc = ProtocolError('frame_too_large',
                                'inbound frame exceeds limit')
            try:
                stdout.write(encode_frame(make_response(-1, error=exc)))
                stdout.flush()
            except OSError:
                return 1
            # The unread suffix is not a new request.  Close this stdio
            # session so framing cannot resynchronise inside an oversized
            # attacker-controlled line.
            return 2
        if not raw.strip():
            continue
        try:
            frame = decode_frame(raw)
        except ProtocolError as exc:
            try:
                stdout.write(encode_frame(make_response(-1, error=exc)))
                stdout.flush()
            except OSError:
                return 1
            continue
        response = endpoint.handle_frame(frame)
        try:
            stdout.write(encode_frame(response))
            stdout.flush()
        except OSError:
            return 1
        if endpoint.fatal:
            return 3
    return 0
