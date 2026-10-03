#!/usr/bin/env python3
"""Scheduled sync, discovery and reconciliation for the local WeChat archive.

Stage 3 sync layer on top of store.Archive: bounded inclusive export windows,
checkpoint-overlap incrementals, whitelist discovery via local
`wx sessions --no-server`, and full-range reconciliation. The module only
invokes local wx processes; it makes no network calls, never logs chat text,
and redacts key-shaped material from private logs under the archive root.
"""
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import threading
import time

from store import Archive

__all__ = ['sync', 'sync_incremental', 'discover', 'reconcile']

EXPORT_TIMEOUT_SECONDS = 1800
SESSIONS_TIMEOUT_SECONDS = 300
SESSIONS_MAX_PAGES = 1000

# A window is trusted-empty ONLY on: exit 0 + this exact marker line + no
# warning/error line + no JSON. "No visible messages found for export (...)"
# (privacy filtering) is a different line and must fail closed.
EMPTY_MARKER = 'No messages found for export.'
# Only real line severity is fatal, never "error" inside a filename or an
# expected-absence diagnostic. Match ordinary and timestamped tracing lines.
WARNING_LINE_PREFIXES = ('warning:', 'error:')
ANSI_ESCAPE_RE = re.compile(r'\x1b\[[0-?]*[ -/]*[@-~]')
TRACE_WARNING_RE = re.compile(
    r'^(?:\d{4}-\d{2}-\d{2}[T ][^\s]+\s+)?(?:WARN|ERROR)(?:\s|:)', re.I)
KEY_HEX_RE = re.compile(rb'[0-9a-fA-F]{64}')

# Reentrant per-process lock registry: one flock per archive root shared by
# nested sync/discover/reconcile calls in the same process (no self-deadlock),
# while excluding any second process or thread on the same root.
_lock_registry = {}
_lock_guard = threading.Lock()


class _LockState:
    def __init__(self, path):
        self.path = path
        self.guard = threading.RLock()
        self.depth = 0
        self.handle = None


@contextmanager
def _sync_lock(archive):
    path = Path(archive.root) / 'sync.lock'
    with _lock_guard:
        state = _lock_registry.get(str(path))
        if state is None:
            state = _LockState(path)
            _lock_registry[str(path)] = state
    with state.guard:
        if state.depth == 0:
            handle = open(path, 'a')
            os.chmod(path, 0o600)
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            state.handle = handle
        state.depth += 1
        try:
            yield
        finally:
            state.depth -= 1
            if state.depth == 0:
                fcntl.flock(state.handle.fileno(), fcntl.LOCK_UN)
                state.handle.close()
                state.handle = None


def _require_archive(archive):
    if not isinstance(archive, Archive):
        raise TypeError('archive must be a store.Archive instance')


def _write_log(archive, name, stderr):
    """Persist tool stderr privately (0600) with 64-hex key material redacted."""
    path = Path(archive.root) / name
    path.write_bytes(KEY_HEX_RE.sub(b'<redacted>', stderr))
    os.chmod(path, 0o600)


def _warning_lines(stderr):
    hits = []
    for line in stderr.decode('utf-8', 'replace').splitlines():
        lowered = ANSI_ESCAPE_RE.sub('', line).strip().lower()
        if lowered.startswith(WARNING_LINE_PREFIXES) or TRACE_WARNING_RE.match(lowered):
            hits.append(line)
    return hits


def _has_empty_marker(stderr):
    return any(line.strip() == EMPTY_MARKER
               for line in stderr.decode('utf-8', 'replace').splitlines())


def _chunks(since, until, chunk_seconds):
    """Split [since, until] into inclusive windows of at most chunk_seconds.

    wx filters on whole seconds (create_time >= since AND <= until), so
    second-aligned windows never split a same-second message group across
    exports; wx export --all pages inside each window.
    """
    start = since
    while start <= until:
        end = min(start + chunk_seconds - 1, until)
        yield start, end
        start = end + 1


def _run_export(archive, binary, account, talker, since, until, no_media,
                log_name='last-sync.log'):
    """Run one bounded `wx export --all --format json` and classify it.

    Returns ('imported', path, tmpdir) when exactly one unambiguous JSON
    export was produced, or ('empty', None, tmpdir) after a trusted
    no-messages observation. Anything ambiguous raises so the checkpoint
    can never advance on an uncertain source.
    """
    tmp = Path(tempfile.mkdtemp(prefix='export-', dir=archive.root))
    try:
        cmd = [str(binary), 'export', str(talker), '--account', str(account),
               '--since', str(since), '--until', str(until), '--all',
               '--format', 'json', '--order', 'asc', '-o', str(tmp)]
        if no_media:
            cmd.append('--no-media')
        try:
            proc = subprocess.run(cmd, capture_output=True,
                                  timeout=EXPORT_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired as exc:
            raise ValueError(
                f'export timed out after {EXPORT_TIMEOUT_SECONDS}s; checkpoint unchanged'
            ) from exc
        _write_log(archive, log_name, proc.stderr)
        if proc.returncode != 0:
            raise ValueError(f'export exited with code {proc.returncode}; '
                             f'inspect private {log_name}; checkpoint unchanged')
        if _warning_lines(proc.stderr):
            raise ValueError(f'export warned; inspect private {log_name}; '
                             'checkpoint unchanged')
        files = sorted(tmp.glob('*.json'))
        marker = _has_empty_marker(proc.stderr)
        if len(files) == 1 and not marker:
            return 'imported', files[0], tmp
        if not files and marker:
            return 'empty', None, tmp
        raise ValueError('no unambiguous export; empty windows need the exact '
                         'no-messages marker; checkpoint unchanged')
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise


def _totals(results):
    totals = {'imported': 0, 'added': 0, 'changed': 0, 'missing': 0,
              'media_files': 0, 'missing_media': 0, 'source_missing_media': 0}
    for result in results:
        for key in totals:
            totals[key] += result.get(key) or 0
    return totals


def _summary(archive, account, talker, windows, results, statuses, no_media):
    media_status = {'available': 0, 'missing': 0, 'metadata_only': 0}
    for result in results:
        for state in media_status:
            media_status[state] += result.get('media_status', {}).get(state, 0)
    return {'account': account, 'talker': talker,
            'since': windows[0][0], 'until': windows[-1][1],
            'chunks': len(windows),
            'status': 'imported' if 'imported' in statuses else 'empty',
            'media_mode': 'metadata_only' if no_media else 'exported_available_media',
            'media_status': media_status,
            **_totals(results),
            'sources': [r['source_sha256'] for r in results if r.get('source_sha256')],
            'checkpoint': archive.checkpoint(account, talker)}


def _import_locked(archive, binary, account, talker, windows, no_media,
                   advance_checkpoint):
    """Import chunk by chunk; a failing chunk stops the run with the
    checkpoint left at the last fully successful chunk."""
    results, statuses = [], []
    for since, until in windows:
        status, path, tmp = _run_export(archive, binary, account, talker,
                                       since, until, no_media)
        try:
            if status == 'empty':
                result = archive.import_empty(account, talker, (since, until),
                                              advance_checkpoint=advance_checkpoint)
            else:
                result = archive.import_export(path, account, talker,
                                               (since, until),
                                               advance_checkpoint=advance_checkpoint,
                                               require_media_contract=True,
                                               expected_media_mode='metadata_only' if no_media else 'enabled')
            results.append(result)
            statuses.append(result.get('status'))
        finally:
            # The store already owns immutable source and asset copies.
            # Release each bounded export before starting the next window.
            shutil.rmtree(tmp, ignore_errors=True)
    return _summary(archive, account, talker, windows, results, statuses,
                    no_media)


def _reconcile_locked(archive, binary, account, talker, windows, no_media):
    """Gather the complete requested range before applying anything.

    Export failures abort before any import. An outer SQLite savepoint also
    rolls back every applied chunk on late validation, asset, or SQL failure;
    store imports use nested savepoints and cannot commit the outer scope.
    Missing detection is bounds-scoped per chunk, and reconciliation never
    advances the incremental checkpoint.
    """
    gathered, tmps = [], []
    try:
        for since, until in windows:
            status, path, tmp = _run_export(archive, binary, account, talker,
                                            since, until, no_media,
                                            log_name='last-reconcile.log')
            gathered.append((since, until, status, path))
            tmps.append(tmp)
        results, statuses = [], []
        archive.db.execute('SAVEPOINT archive_sync_reconcile')
        try:
            for since, until, status, path in gathered:
                if status == 'empty':
                    result = archive.import_empty(account, talker, (since, until),
                                                  advance_checkpoint=False,
                                                  reconcile=True)
                else:
                    result = archive.import_export(path, account, talker,
                                                   (since, until),
                                                   advance_checkpoint=False,
                                                   reconcile=True,
                                                   require_media_contract=True,
                                                   expected_media_mode='metadata_only' if no_media else 'enabled')
                results.append(result)
                statuses.append(result.get('status'))
        except BaseException:
            archive.db.execute('ROLLBACK TO SAVEPOINT archive_sync_reconcile')
            archive.db.execute('RELEASE SAVEPOINT archive_sync_reconcile')
            raise
        else:
            archive.db.execute('RELEASE SAVEPOINT archive_sync_reconcile')
    finally:
        for tmp in tmps:
            shutil.rmtree(tmp, ignore_errors=True)
    summary = _summary(archive, account, talker, windows, results, statuses,
                       no_media)
    summary['reconcile'] = True
    return summary


def sync(archive, binary, account, talker, since, until, no_media=False, *,
         chunk_seconds=86400, advance_checkpoint=True, reconcile=False):
    """Export [since, until] for one conversation in bounded chunks and import.

    Serialized per archive root via sync.lock. Any warning, nonzero exit, or
    ambiguous output stops the run; the checkpoint never moves past the last
    fully successful chunk. With reconcile=True the full range is gathered
    before any import (missing detection), and the checkpoint is never
    advanced by reconciliation regardless of advance_checkpoint.
    """
    _require_archive(archive)
    since, until, chunk_seconds = int(since), int(until), int(chunk_seconds)
    if chunk_seconds < 1:
        raise ValueError('chunk_seconds must be positive')
    if since > until or until > int(time.time()):
        raise ValueError('invalid fixed window')
    with _sync_lock(archive):
        windows = list(_chunks(since, until, chunk_seconds))
        if reconcile:
            return _reconcile_locked(archive, binary, account, talker,
                                     windows, no_media)
        return _import_locked(archive, binary, account, talker, windows,
                              no_media, advance_checkpoint)


def sync_incremental(archive, binary, account, talker, *, initial_since,
                     overlap_seconds=3600, settle_seconds=120,
                     chunk_seconds=86400, no_media=False, now=None):
    """Checkpoint-overlap incremental through now-settle for one talker.

    The checkpoint is read under the same lock that guards export/import, so
    concurrent runs can neither act on stale state nor interleave a reconcile
    between read and import. Windows that are not due yet report 'not_due'
    with no export, no import, and no checkpoint mutation.
    """
    _require_archive(archive)
    now = int(time.time()) if now is None else int(now)
    initial_since = int(initial_since)
    overlap_seconds, settle_seconds = int(overlap_seconds), int(settle_seconds)
    chunk_seconds = int(chunk_seconds)
    if chunk_seconds < 1:
        raise ValueError('chunk_seconds must be positive')
    with _sync_lock(archive):
        checkpoint = archive.checkpoint(account, talker)
        since = (checkpoint - overlap_seconds if checkpoint is not None
                 else initial_since)
        until = now - settle_seconds
        if since > until:
            summary = {'account': account, 'talker': talker,
                       'status': 'not_due', 'since': since, 'until': until,
                       'chunks': 0, 'imported': 0, 'added': 0, 'changed': 0,
                       'missing': 0, 'media_files': 0, 'sources': [],
                       'checkpoint': checkpoint,
                       'media_mode': ('metadata_only' if no_media
                                      else 'exported_available_media')}
        else:
            windows = list(_chunks(since, until, chunk_seconds))
            summary = _import_locked(archive, binary, account, talker,
                                     windows, no_media, advance_checkpoint=True)
    summary['checkpoint_before'] = checkpoint
    summary['overlap_seconds'] = overlap_seconds
    return summary


def _changed_talkers(archive, binary, account, whitelist, since):
    """Local-only session listing (wx sessions --no-server --all --format json).

    Pages until has_more is false, validates errors/warnings/skips, and keeps
    only whitelist conversations whose last activity (sort_timestamp) is
    >= since. Non-whitelist conversations are never returned, even if wx
    reports them.
    """
    allowed = set(whitelist)
    changed = []
    offset = 0
    for _ in range(SESSIONS_MAX_PAGES):
        cmd = [str(binary), 'sessions', '--no-server', '--all',
               '--format', 'json', '--account', str(account),
               '--offset', str(offset)]
        try:
            proc = subprocess.run(cmd, capture_output=True,
                                  timeout=SESSIONS_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired as exc:
            raise ValueError(
                f'sessions timed out after {SESSIONS_TIMEOUT_SECONDS}s'
            ) from exc
        _write_log(archive, 'last-discover.log', proc.stderr)
        if proc.returncode != 0:
            raise ValueError(f'sessions exited with code {proc.returncode}; '
                             'inspect private last-discover.log')
        if _warning_lines(proc.stderr):
            raise ValueError('sessions warned; inspect private last-discover.log')
        try:
            envelope = json.loads(proc.stdout.decode('utf-8'))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError('sessions output is not valid JSON') from exc
        items, paging = envelope.get('items'), envelope.get('paging')
        if not isinstance(items, list) or not isinstance(paging, dict):
            raise ValueError('sessions envelope missing items/paging')
        stats = envelope.get('stats') or {}
        if stats.get('skipped') or stats.get('shard_warnings'):
            raise ValueError('sessions reported skipped messages or shard warnings')
        for item in items:
            if not isinstance(item, dict):
                raise ValueError('malformed session item')
            username, activity = item.get('username'), item.get('sort_timestamp')
            if not isinstance(username, str) or type(activity) is not int:
                raise ValueError('malformed session identity/timestamp')
            if (username in allowed and activity >= since
                    and username not in changed):
                changed.append(username)
        has_more, returned = paging.get('has_more'), paging.get('returned')
        if not isinstance(has_more, bool) or type(returned) is not int or returned < 0:
            raise ValueError('sessions paging metadata invalid')
        if paging.get('offset') != offset:
            raise ValueError('sessions page offset mismatch')
        if not has_more:
            return changed
        if returned == 0:
            raise ValueError('sessions paging made no progress')
        offset += returned
    raise ValueError(f'sessions paging exceeded {SESSIONS_MAX_PAGES} pages')


def discover(archive, binary, account, talkers, since, *, initial_since,
             overlap_seconds=3600, settle_seconds=120, chunk_seconds=86400,
             no_media=False, now=None):
    """Find whitelist conversations active since `since`, then sync each.

    Session discovery is local-only (--no-server); no timeline payload is
    archived. The whole discovery plus per-conversation incrementals run
    under the single sync lock (reentrant within this process).
    """
    _require_archive(archive)
    whitelist = list(dict.fromkeys(str(t) for t in talkers))
    if not whitelist:
        raise ValueError('talkers whitelist must not be empty')
    since = int(since)
    now = int(time.time()) if now is None else int(now)
    with _sync_lock(archive):
        changed = _changed_talkers(archive, binary, account, whitelist, since)
        results = {}
        for talker in changed:
            results[talker] = sync_incremental(
                archive, binary, account, talker,
                initial_since=initial_since, overlap_seconds=overlap_seconds,
                settle_seconds=settle_seconds, chunk_seconds=chunk_seconds,
                no_media=no_media, now=now)
    return {'account': account, 'since': since, 'changed': changed,
            'results': results}


def reconcile(archive, binary, account, talker, *, days=30, chunk_seconds=86400,
              no_media=False, now=None):
    """Re-export the last `days` and diff against the archive.

    Reports added/changed/missing for the complete recent range. Failures
    mark nothing missing and never advance the checkpoint; messages outside
    the requested bounds are untouched.
    """
    _require_archive(archive)
    days, chunk_seconds = int(days), int(chunk_seconds)
    if days < 1:
        raise ValueError('days must be positive')
    if chunk_seconds < 1:
        raise ValueError('chunk_seconds must be positive')
    now = int(time.time()) if now is None else int(now)
    since, until = now - days * 86400, now
    with _sync_lock(archive):
        windows = list(_chunks(since, until, chunk_seconds))
        summary = _reconcile_locked(archive, binary, account, talker,
                                    windows, no_media)
    summary['days'] = days
    return summary
