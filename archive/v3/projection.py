"""v3 query projection — physically isolated, policy-trimmed, read-only.

Split of duties (see work/external-archive-implementation/projection-contract.md):
- ProjectionBuilder runs WRITER-side, holding the open MasterStore (and the
  writer flock held by its caller). It reads one consistent master snapshot,
  applies the field whitelist + sender/tag/quote visibility policy, builds a
  static immutable generation under `<archive-root>/projection/`, and
  publishes it with ONE atomic action: replacing `manifest.json` (unique
  temp + fsync + rename + dir fsync through the guard binding). Exceptions
  never publish half-built output — the temp build file is unlinked and the
  manifest bytes stay untouched.
- ProjectionService runs QUERY-side under an independent non-root OS user.
  It receives ONLY the projection root (its own guard binding with its own
  marker — the accepted guard mechanism, unchanged) and never opens master /
  source packages / backups / keys / collector auth. SQL WHERE clauses only
  narrow searches inside the projection; physical isolation is the file
  system's job, not a query predicate.

Safety invariants (spec §1.6/§2 S4, requirements 1-5):
1. Everything sqlite touches on the writer side is anchored: on Linux the
   build connection and the query connections both go through the pinned
   VFS registered against a HELD directory fd of the fixed volume (no
   fallback — registration failure propagates, exactly like master); off
   Linux the synthetic tier uses bound paths + in-memory temp so no sqlite
   temp object ever leaves the volume.
2. Revocation order is fixed: master revoke → atomic revocations.json bump
   (kills every open reader on its NEXT request) → rebuild → publish →
   prune. Already-open sessions die via the per-request revalidation of the
   (epoch, policy_version, revocations_version, generation) tuple; a fresh
   session for a revoked talker is denied even while the old generation
   file still exists on disk (retention), because no session can reach it.
3. FTS5 (trigram) indexes ONLY `vt` — the policy-trimmed visible text built
   by WHITELIST DECODE: content_json.text when present, else a strict
   structural XML parse that admits a title ONLY on the exact path
   msg > appmsg > title and extracts ONLY its direct text nodes (child
   subtrees are dropped whole; unbalanced closes abort), else (non-XML)
   the plain body. content_text is NOT trusted as body-only — it may be
   the verbatim original XML, and raw XML never enters any column or the
   index verbatim. Quote text is admitted only under quotes=text_only AND
   only when the quote's OWN sender passes the rule's sender filter. Media
   references take an irreversible opaque id ('h' + sha256[:32]) — a ref
   that is a source path/URI is never exposed verbatim. raw_record_json /
   record_sha256 / packed_info / revision payloads (table never selected) /
   revoked originals never enter the projection in any column. Unknown
   content_json structures are omitted and reported as coverage gaps (key
   names only, never values).
4. Every query-side failure is fail-closed ProjectionError; there is no
   on-disk fallback path.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import sqlite3
import sys
import time
from pathlib import Path
from urllib.parse import quote

from .errors import GuardError, ProjectionError
from .guard import MARKER_NAME, SyntheticGuard, make_guard
from .util import canonical, now_ms
from .vfs import register_pinned_vfs, release_pinned_vfs

PROJECTION_DIR = 'projection'
POLICY_SUBDIR = 'policy'
MANIFEST_RELPATH = f'{PROJECTION_DIR}/manifest.json'
POLICY_RELPATH = f'{PROJECTION_DIR}/{POLICY_SUBDIR}/policy.json'
REVOCATIONS_RELPATH = f'{PROJECTION_DIR}/{POLICY_SUBDIR}/revocations.json'

PROJECTION_CONTRACT = 'wx-archive.projection'
PROJECTION_VERSION = 1

# published generation artifact names (strict — validated before ANY open)
_GEN_RE = re.compile(r'^g(\d{12})-([0-9a-f]{8})$')
_GEN_DB_RE = re.compile(r'^p-(g\d{12}-[0-9a-f]{8})\.db$')
_GEN_REPORT_RE = re.compile(r'^report-(g\d{12}-[0-9a-f]{8})\.json$')
_BUILD_TMP_RE = re.compile(r'^\.p-build-([0-9a-f]+)\.db$')

_DIR_MODE = 0o750      # projection subtree: owner rwx, query group rx
_FILE_MODE = 0o640     # projection files: owner rw, query group r

_KNOWN_CONTENT_KEYS = {'text', 'quote', 'tags'}
# quote objects: only `text` is ever whitelisted out; anything else in a
# quote (sender, xml, nested structures) is omitted and gap-reported
_QUOTE_TEXT_KEYS = {'text'}
_NESTED_QUOTE_KEYS = {'quote', 'nested_quote', 'nested'}
_MEDIA_KINDS = {'image', 'voice', 'video', 'file', 'emoji'}
_SENDER_MODES = {'all', 'allowlist'}
_TAG_MODES = {'all', 'allowlist', 'none'}
_QUOTE_MODES = {'none', 'text_only'}
_RULE_KEYS = {'query', 'senders', 'quotes', 'tags'}
_MAX_LIMIT = 200
_MAX_CONTEXT = 100

_PROJ_SCHEMA = [
    '''CREATE TABLE msgs (
      seq INTEGER PRIMARY KEY,
      account TEXT NOT NULL, talker TEXT NOT NULL,
      create_time INTEGER NOT NULL, sort_seq INTEGER NOT NULL,
      msg_type INTEGER NOT NULL, sub_type INTEGER NOT NULL DEFAULT 0,
      status INTEGER NOT NULL DEFAULT 0, direction TEXT,
      sender_display TEXT,
      vt TEXT NOT NULL,
      text_body TEXT NOT NULL,
      quote_text TEXT,
      tags_json TEXT NOT NULL DEFAULT '[]',
      has_revisions INTEGER NOT NULL DEFAULT 0,
      media_ids_json TEXT NOT NULL DEFAULT '[]')''',
    'CREATE INDEX msgs_window ON msgs(account, talker, create_time, sort_seq, seq)',
    "CREATE VIRTUAL TABLE msgs_fts USING fts5("
    "vt, content='msgs', content_rowid='seq', tokenize='trigram')",
    'CREATE TABLE proj_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)',
]

# the ONLY master columns the projection may ever read — whitelist itself
_MASTER_COLUMNS = (
    'msg_uid, account, talker, create_time, sort_seq, msg_type, sub_type, '
    'status, direction, sender_id, sender_display_name, content_text, '
    'content_json, media_refs_json, visibility, has_revisions, deleted_at')
_MASTER_SELECT = f'SELECT {_MASTER_COLUMNS} FROM messages ORDER BY msg_uid'


# ══════════════════════════════════════════════════════════════════
# policy validation / normalisation
# ══════════════════════════════════════════════════════════════════

def _invalid_policy(reason):
    return ProjectionError('policy_invalid', reason)


def _validate_policy(policy):
    """Strict validation, unknown fields rejected (never silently ignored)."""
    if not isinstance(policy, dict):
        raise _invalid_policy('policy must be an object')
    version = policy.get('policy_version')
    if not isinstance(version, int) or isinstance(version, bool) or version <= 0:
        raise _invalid_policy('policy_version must be a positive integer')
    rules = policy.get('rules')
    if not isinstance(rules, dict):
        raise _invalid_policy('policy.rules must be an object')
    for account, talkers in rules.items():
        if not isinstance(talkers, dict):
            raise _invalid_policy(f'rules[{account!r}] must be an object')
        for talker, rule in talkers.items():
            _validate_rule(account, talker, rule)
    return policy


def _validate_rule(account, talker, rule):
    where = f'rules[{account!r}][{talker!r}]'
    if not isinstance(rule, dict):
        raise _invalid_policy(f'{where} must be an object')
    unknown = set(rule) - _RULE_KEYS
    if unknown:
        raise _invalid_policy(f'{where} has unknown fields: {sorted(unknown)!r}')
    if not isinstance(rule.get('query'), bool):
        raise _invalid_policy(f'{where}.query must be a boolean')
    _norm_senders(where, rule.get('senders'))
    if rule.get('quotes', 'none') not in _QUOTE_MODES:
        raise _invalid_policy(f'{where}.quotes must be one of {sorted(_QUOTE_MODES)!r}')
    _norm_tags(where, rule.get('tags'))


def _norm_senders(where, senders):
    if senders is None:
        return {'mode': 'all', 'allow': []}
    if not isinstance(senders, dict):
        raise _invalid_policy(f'{where}.senders must be an object')
    unknown = set(senders) - {'mode', 'allow'}
    if unknown:
        raise _invalid_policy(f'{where}.senders has unknown fields: {sorted(unknown)!r}')
    mode = senders.get('mode', 'all')
    if mode not in _SENDER_MODES:
        raise _invalid_policy(f'{where}.senders.mode must be one of {sorted(_SENDER_MODES)!r}')
    allow = senders.get('allow', [])
    if mode == 'allowlist':
        if not isinstance(allow, list) or not all(isinstance(s, str) and s for s in allow):
            raise _invalid_policy(f'{where}.senders.allow must be a list of non-empty strings')
    return {'mode': mode, 'allow': list(allow)}


def _norm_tags(where, tags):
    if tags is None:
        return {'mode': 'none', 'allow': []}
    if not isinstance(tags, dict):
        raise _invalid_policy(f'{where}.tags must be an object')
    unknown = set(tags) - {'mode', 'allow'}
    if unknown:
        raise _invalid_policy(f'{where}.tags has unknown fields: {sorted(unknown)!r}')
    mode = tags.get('mode', 'none')
    if mode not in _TAG_MODES:
        raise _invalid_policy(f'{where}.tags.mode must be one of {sorted(_TAG_MODES)!r}')
    allow = tags.get('allow', [])
    if mode == 'allowlist':
        if not isinstance(allow, list) or not all(isinstance(t, str) and t for t in allow):
            raise _invalid_policy(f'{where}.tags.allow must be a list of non-empty strings')
    return {'mode': mode, 'allow': list(allow)}


def _rule_get(policy, account, talker):
    """Normalised rule or None. Missing rule = DENY (master scope semantics)."""
    rule = (policy.get('rules') or {}).get(account, {}).get(talker)
    if not isinstance(rule, dict) or not rule.get('query'):
        return None
    where = f'rules[{account!r}][{talker!r}]'
    return {
        'senders': _norm_senders(where, rule.get('senders')),
        'quotes': rule.get('quotes', 'none'),
        'tags': _norm_tags(where, rule.get('tags')),
    }


# ══════════════════════════════════════════════════════════════════
# coverage-gap tracking (key names only — never message values)
# ══════════════════════════════════════════════════════════════════

class _Gaps:
    def __init__(self):
        self.counts = {}
        self.samples = {}

    def add(self, kind, sample=None):
        self.counts[kind] = self.counts.get(kind, 0) + 1
        if sample is not None:
            bucket = self.samples.setdefault(kind, [])
            if sample not in bucket and len(bucket) < 8:
                bucket.append(str(sample))

    def report(self):
        return [{'kind': kind, 'count': count,
                 'sample_keys': self.samples.get(kind, [])}
                for kind, count in sorted(self.counts.items(),
                                          key=lambda kv: (-kv[1], kv[0]))]


def opaque_media_id(value):
    """Irreversible opaque id for a media reference value — the ONLY form a
    ref (which may itself be a file:// source path or URI) may ever take
    inside the projection. NAS-side correlation uses this same function."""
    return 'h' + hashlib.sha256(value.encode('utf-8')).hexdigest()[:32]


def _media_ids(media_json, gaps):
    """Opaque media ids only: {id, kind}. No sha, no length, no paths — the
    id is the irreversible hash, never the ref value itself."""
    try:
        refs = json.loads(media_json) if media_json else []
    except ValueError:
        refs = None
    if not isinstance(refs, list):
        if media_json not in (None, '', '[]'):
            gaps.add('media_ref_shape')
        return []
    out = []
    for ref in refs:
        if not isinstance(ref, dict):
            gaps.add('media_ref_shape')
            continue
        mid = None
        for key in ('ref_key', 'asset_id', 'id'):
            value = ref.get(key)
            if isinstance(value, str) and value:
                mid = opaque_media_id(value)
                break
        kind = ref.get('kind')
        if not isinstance(kind, str) or kind not in _MEDIA_KINDS:
            kind = 'other'
        if mid:
            out.append({'id': mid, 'kind': kind})
        else:
            gaps.add('media_ref_shape')
    return out


_TAG_TOKEN_RE = re.compile(
    r'<(/?)([A-Za-z_][-\w]*)((?:"[^"]*"|\'[^\']*\'|[^>"\'])*)>')

# the one whitelisted path to a body: exactly msg > appmsg > title
_TITLE_PATH = ['msg', 'appmsg']


def _find_xml_document(text):
    """The XML document embedded in text, or None. Catches both bare XML
    bodies and sender-prefixed ones (WeChat quote replies arrive as
    'sender:\\n<msg><appmsg>…</msg>'): anything from the first '<' that
    closes as a document is treated as XML-bearing."""
    if not isinstance(text, str):
        return None
    stripped = text.strip()
    if stripped.startswith('<') and stripped.endswith('>'):
        return stripped
    idx = text.find('<')
    if idx < 0 or not text.rstrip().endswith('>'):
        return None
    candidate = text[idx:].strip()
    if candidate.startswith('<') and candidate.endswith('>'):
        return candidate
    return None


def _title_direct_text(xml, start):
    """Direct text nodes of the currently open <title>, scanning from
    `start`. Child elements are skipped ENTIRELY — their subtree text
    (e.g. a nested refermsg payload) is hidden content and is never
    tag-stripped into the body. Returns (text, ok); ok=False on any
    malformed structure. Entities kept verbatim."""
    parts = []
    depth = 0
    pos = start
    n = len(xml)
    while pos < n:
        lt = xml.find('<', pos)
        if lt < 0:
            return '', False
        if lt > pos and depth == 0:
            parts.append(xml[pos:lt])
        match = _TAG_TOKEN_RE.match(xml, lt)
        if match is None:
            return '', False
        token = match.group(0)
        name = match.group(2)
        if token.endswith('/>'):
            pass  # self-closing: no subtree, no depth change
        elif match.group(1):
            if name == 'title' and depth == 0:
                text = ''.join(parts).strip()
                return (text, True) if text else ('', True)
            depth -= 1
            if depth < 0:
                return '', False
        else:
            depth += 1
        pos = match.end()
    return '', False


def _extract_appmsg_title(xml):
    """Strict structural whitelist decode. A title is admissible ONLY on
    the exact path msg > appmsg. Closing tags must match the open stack
    exactly — an unbalanced close aborts the whole extraction (conservative
    omission) instead of popping through refermsg ancestors. Inside the
    title only DIRECT text nodes are extracted. Anything malformed → ''."""
    stack = []
    pos = 0
    n = len(xml)
    while pos < n:
        lt = xml.find('<', pos)
        if lt < 0:
            break
        match = _TAG_TOKEN_RE.match(xml, lt)
        if match is None:
            return ''  # malformed tag — conservative omission
        token = match.group(0)
        name = match.group(2)
        if token.endswith('/>'):
            pass
        elif match.group(1):  # closing tag
            if not stack or stack[-1] != name:
                return ''  # unbalanced close never pops through ancestors
            stack.pop()
        else:
            stack.append(name)
            if name == 'title' and stack[:-1] == _TITLE_PATH:
                text, ok = _title_direct_text(xml, match.end())
                return text if ok else ''
            # any other title (nested in refermsg etc.) is skipped
        pos = match.end()
    return ''


def _decode_body(content_text, decoded_text):
    """Body per contract §6 priority. content_text is the VERBATIM master
    record text (possibly raw original XML, possibly sender-prefixed XML)
    and is never trusted as body-only: raw XML never enters the projection
    verbatim; in XML mode the body is the ancestry-valid title ONLY."""
    if isinstance(decoded_text, str) and decoded_text \
            and _find_xml_document(decoded_text) is None:
        return decoded_text, False  # whitelisted decode wins
    raw = content_text or ''
    xml = _find_xml_document(raw)
    if xml is not None:
        title = _extract_appmsg_title(xml)
        if title:
            return title, False
        return '', True  # unknown XML structure — omitted, gap-reported
    return raw, False


def _visible_texts(content_text, content_json_text, rule, gaps):
    """Whitelist extraction: body + (policy-permitting) ONE level of quote
    text; unknown/complex structures omitted and gap-reported. A gap is only
    reported where the POLICY did not ask for the omission — quotes=none and
    tags=none omissions are intended policy, not coverage loss."""
    decoded_text = None
    quote_text = None
    tags = []
    if content_json_text and content_json_text != '{}':
        try:
            cj = json.loads(content_json_text)
        except ValueError:
            cj = None
            gaps.add('content_json_unparseable')
        if cj is not None and not isinstance(cj, dict):
            cj = None
            gaps.add('content_json_shape')
        if isinstance(cj, dict):
            for key in cj:
                if key not in _KNOWN_CONTENT_KEYS:
                    gaps.add('unknown_content_key', key)
            t = cj.get('text')
            if isinstance(t, str) and t:
                decoded_text = t
            quotes_mode = rule['quotes']
            q = cj.get('quote')
            if q is not None and quotes_mode == 'text_only':
                # only in text_only mode can omission be a coverage loss
                if isinstance(q, dict):
                    for key in q:
                        if key not in _QUOTE_TEXT_KEYS:
                            gaps.add('unknown_content_key', f'quote.{key}')
                    if any(k in q for k in _NESTED_QUOTE_KEYS):
                        # nested recursion is never projected in ANY mode
                        gaps.add('nested_quote')
                    # the quote's OWN sender must pass the rule's sender
                    # filter — outer-message visibility is not inherited
                    if rule['senders']['mode'] == 'allowlist':
                        quote_sender = q.get('sender_id')
                        if not isinstance(quote_sender, str) \
                                or quote_sender not in rule['senders']['allow']:
                            gaps.add('quote_sender_filtered')
                            q = None
                else:
                    gaps.add('quote_shape')
                if isinstance(q, dict):
                    qt = q.get('text')
                    if isinstance(qt, str) and qt:
                        if _find_xml_document(qt) is not None:
                            gaps.add('quote_text_xml')
                        else:
                            quote_text = qt
                    elif qt is not None:
                        gaps.add('quote_text_shape')
            tags_mode = rule['tags']['mode']
            t = cj.get('tags')
            if t is not None and tags_mode != 'none':
                if isinstance(t, list) and all(isinstance(x, str) for x in t):
                    if tags_mode == 'all':
                        tags = list(t)
                    else:
                        tags = [x for x in t if x in rule['tags']['allow']]
                else:
                    gaps.add('tags_shape')
    body, xml_omitted = _decode_body(content_text, decoded_text)
    if xml_omitted:
        gaps.add('raw_xml_body_omitted')
    if quote_text:
        vt = f'{body}\n{quote_text}' if body else quote_text
    else:
        vt = body
    return body, quote_text, tags, vt


# ══════════════════════════════════════════════════════════════════
# builder (writer side)
# ══════════════════════════════════════════════════════════════════

class ProjectionBuilder:
    """Builds and publishes immutable query generations from master
    snapshots. Caller holds the writer flock; this class never takes it
    (flock is per-open-file-description — a second take would self-block)."""

    def __init__(self, store, projection_id):
        self.store = store
        self.binding = store.binding
        self.archive_id = store.archive_id
        self.projection_id = projection_id

    # ── explicit init ───────────────────────────────────────────────
    @classmethod
    def init(cls, store, policy, projection_guard=None):
        """One-time projection init. The projection root gets its OWN marker
        (the accepted guard mechanism, unchanged) with a distinct archive id
        so the query side can never mistake the master root for it. Production
        tier requires the NAS to pass a ProductionGuard whose expectation
        archive_id is '<archive_id>:projection'; the synthetic tier writes the
        marker automatically."""
        store.binding.check_alive()
        _validate_policy(policy)
        projection_id = f'{store.archive_id}:projection'
        projection_root = Path(store.binding.root) / PROJECTION_DIR
        try:
            store.binding.stat_path(f'{PROJECTION_DIR}/{MARKER_NAME}')
        except GuardError as exc:
            # path_missing = no marker; path_open_failed = projection dir
            # itself absent (missing intermediate in the stat chain) — both
            # mean "nothing initialised yet"
            if exc.code not in ('path_missing', 'path_open_failed'):
                raise
        else:
            raise ProjectionError(
                'projection_exists',
                'projection marker already present; refusing to re-init')
        fd = store.binding.ensure_dir(f'{PROJECTION_DIR}/{POLICY_SUBDIR}')
        os.close(fd)
        try:
            if projection_guard is not None:
                projection_guard.write_marker(projection_root, store.epoch(),
                                              int(time.time()))
            elif store.binding.tier == 'synthetic':
                SyntheticGuard.write_marker(projection_root, projection_id,
                                            store.epoch(), int(time.time()))
            else:
                raise ProjectionError(
                    'projection_guard_required',
                    'production tier requires projection_guard to write the '
                    'projection marker (explicit init only)')
        except GuardError as exc:
            if exc.code != 'marker_exists':
                raise
            raise ProjectionError('projection_exists',
                                  'projection marker already present')
        cls._set_modes(store.binding)
        store.binding.write_json_atomic(POLICY_RELPATH, policy, _FILE_MODE)
        store.binding.write_json_atomic(
            REVOCATIONS_RELPATH,
            {'version': 0, 'revoked': [], 'updated_at': now_ms()}, _FILE_MODE)
        return cls(store, projection_id)

    @staticmethod
    def _set_modes(binding):
        """Projection subtree modes: 0750 dirs / 0640 files so the query
        GROUP (ownership set by ops) can traverse+read, others cannot. The
        archive ROOT mode is ops' decision — a too-tight root merely makes
        the projection unreachable (still fail-closed), never looser."""
        for relpath in (PROJECTION_DIR, f'{PROJECTION_DIR}/{POLICY_SUBDIR}'):
            fd = binding.open_dir(relpath)
            try:
                os.fchmod(fd, _DIR_MODE)
            finally:
                os.close(fd)
        fd = binding.open_file_read(f'{PROJECTION_DIR}/{MARKER_NAME}')
        try:
            os.fchmod(fd, _FILE_MODE)
        finally:
            os.close(fd)

    # ── policy / revocations ────────────────────────────────────────
    def set_policy(self, policy):
        """Replace the policy atomically. policy_version change stale-dates
        every open session on its next request; the new rules take effect at
        the next build. Content changes require a strictly newer version;
        an identical policy/version pair is an idempotent refresh request."""
        _validate_policy(policy)
        self.binding.check_alive()
        current = self._read_policy()
        old_version = int(current['policy_version'])
        new_version = int(policy['policy_version'])
        if new_version < old_version:
            raise ProjectionError(
                'policy_invalid',
                'policy_version must increase when replacing the active policy',
                {'current_policy_version': old_version,
                 'requested_policy_version': new_version})
        if new_version == old_version:
            if canonical(policy) != canonical(current):
                raise ProjectionError(
                    'policy_invalid',
                    'policy content changed without increasing policy_version',
                    {'policy_version': old_version})
            return {'policy_version': old_version, 'unchanged': True}
        self.binding.write_json_atomic(POLICY_RELPATH, policy, _FILE_MODE)
        return {'policy_version': policy['policy_version']}

    def _read_policy(self):
        try:
            policy = self.binding.read_json(POLICY_RELPATH)
        except GuardError as exc:
            if exc.code == 'path_missing':
                raise ProjectionError('policy_missing',
                                      'projection policy file absent')
            raise
        except ValueError:
            raise ProjectionError('policy_invalid', 'policy file not valid JSON')
        return _validate_policy(policy)

    def _read_revocations(self):
        try:
            rev = self.binding.read_json(REVOCATIONS_RELPATH)
        except GuardError as exc:
            if exc.code == 'path_missing':
                raise ProjectionError('revocations_missing',
                                      'revocations file absent')
            raise
        except ValueError:
            raise ProjectionError('revocations_invalid',
                                  'revocations file not valid JSON')
        if not isinstance(rev, dict) or not isinstance(rev.get('version'), int) \
                or not isinstance(rev.get('revoked'), list):
            raise ProjectionError('revocations_invalid', 'revocations malformed')
        return rev

    def revoke(self, account, talker):
        """Revocation, ordered: (1) master FULL-transaction revoke, (2) atomic
        revocations.json bump — from this instant every already-open reader
        fails its NEXT request and new sessions for the talker are denied,
        even though the old generation file is still on disk — and only later
        (revoke_and_rebuild) does a clean generation replace it."""
        seq = self.store.revoke_talker(account, talker)  # MasterError propagates
        self.binding.check_alive()
        rev = self._read_revocations()
        entry = [account, talker]
        if entry not in rev['revoked']:
            rev['revoked'] = list(rev['revoked']) + [entry]
            rev['version'] = int(rev['version']) + 1
            rev['updated_at'] = now_ms()
            self.binding.write_json_atomic(REVOCATIONS_RELPATH, rev, _FILE_MODE)
        return {'account': account, 'talker': talker,
                'revocations_version': rev['version'],
                'revoke_commit_seq': seq}

    def revoke_and_rebuild(self, account, talker):
        info = self.revoke(account, talker)
        manifest = self.build_and_publish()
        pruned = self.prune()
        return {'revoke': info, 'manifest': manifest, 'pruned': pruned}

    # ── build + publish ─────────────────────────────────────────────
    def build_and_publish(self):
        """Static consistent generation: one master read_snapshot streamed
        into a fresh db built at a unique temp name on the fixed volume,
        integrity-checked, fsynced, chmod'ed, renamed into place, then
        published by the single atomic manifest replacement. Any failure
        leaves the previous manifest byte-identical and no new generation."""
        self.binding.check_alive()
        policy = self._read_policy()
        rev = self._read_revocations()
        gaps = _Gaps()
        counts = {'master_visible_rows': 0, 'included': 0, 'non_visible': 0,
                  'deleted': 0, 'revoked_talker': 0, 'query_not_allowed': 0,
                  'no_policy_rule': 0, 'hidden_sender': 0}
        talkers = set()
        token = secrets.token_hex(4)
        tmp_name = f'.p-build-{token}.db'
        with self.store.read_snapshot() as master:
            epoch = int(self.store.epoch())
            commit_seq = int(self.store.commit_seq())
            grants = {}
            for row in master.execute(
                    'SELECT account, talker, query_allowed, revoked_at '
                    'FROM scope_grants'):
                grants[(row['account'], row['talker'])] = \
                    (int(row['query_allowed']), row['revoked_at'])
            generation = f'g{commit_seq:012d}-{token}'
            db_name = f'p-{generation}.db'
            report_name = f'report-{generation}.json'
            proj_fd = self.binding.open_dir(PROJECTION_DIR)
            vfs = None
            conn = None
            try:
                # WE create the empty build file through the binding (never
                # sqlite): explicit, unique, pinned from the first byte
                fd = self.binding._open_chain(
                    f'{PROJECTION_DIR}/{tmp_name}',
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                    0o600)
                os.fsync(fd)
                os.close(fd)
                if sys.platform.startswith('linux'):
                    # ALL build-time sqlite objects (db/journal/temp) anchor
                    # to the held projection dir fd — same volume, no
                    # fallback; registration failure propagates like master.
                    vfs = register_pinned_vfs(proj_fd, tmp_name)
                    conn = sqlite3.connect(
                        f'file:{tmp_name}?vfs={vfs.name}&mode=rw',
                        uri=True, isolation_level=None, timeout=10.0)
                    reported = conn.execute('PRAGMA database_list').fetchone()[2]
                    if reported != vfs.anchored_main():
                        raise ProjectionError(
                            'build_failed',
                            'build connection is not anchored to the '
                            'projection root fd',
                            {'reported': reported})
                else:
                    build_path = str(Path(self.binding.root) / PROJECTION_DIR
                                     / tmp_name)
                    conn = sqlite3.connect('file:' + quote(build_path)
                                           + '?mode=rw', uri=True,
                                           isolation_level=None, timeout=10.0)
                    # synthetic tier: no disk temp outside the volume, ever
                    conn.execute('PRAGMA temp_store=MEMORY')
                conn.execute('PRAGMA journal_mode=MEMORY')
                conn.execute('PRAGMA synchronous=OFF')
                for stmt in _PROJ_SCHEMA:
                    conn.execute(stmt)
                conn.execute('INSERT INTO proj_meta VALUES(?,?)',
                             ('projection_contract', PROJECTION_CONTRACT))
                conn.execute('INSERT INTO proj_meta VALUES(?,?)',
                             ('projection_version', str(PROJECTION_VERSION)))
                conn.execute('INSERT INTO proj_meta VALUES(?,?)',
                             ('built_at', str(now_ms())))
                self._stream_rows(master, conn, grants, policy, gaps, counts,
                                  talkers)
                conn.execute('PRAGMA journal_mode=DELETE')
                check = conn.execute('PRAGMA integrity_check').fetchone()[0]
                if check != 'ok':
                    raise ProjectionError('build_failed',
                                          f'integrity_check: {check}')
                try:
                    conn.execute("INSERT INTO msgs_fts(msgs_fts) "
                                 "VALUES('integrity-check')")
                except sqlite3.DatabaseError as exc:
                    raise ProjectionError('build_failed',
                                          f'fts integrity-check: {exc}')
                conn.close()
                conn = None
                release_pinned_vfs(vfs)
                vfs = None
                # durable, group-readable generation file (hashing the leaf
                # is safe: no sqlite connection is open on it anymore)
                db_sha, db_bytes = self._fsync_and_hash(proj_fd, tmp_name)
                os.rename(tmp_name, db_name,
                          src_dir_fd=proj_fd, dst_dir_fd=proj_fd)
                os.fsync(proj_fd)
            except GuardError:
                self._cleanup_build(proj_fd, tmp_name, conn, vfs)
                raise
            except BaseException as exc:
                self._cleanup_build(proj_fd, tmp_name, conn, vfs)
                if isinstance(exc, ProjectionError):
                    raise
                raise ProjectionError('build_failed', str(exc)) from exc
            finally:
                release_pinned_vfs(vfs)
                try:
                    os.close(proj_fd)
                except OSError:
                    pass
        excluded = {k: counts[k] for k in ('revoked_talker',
                                           'query_not_allowed',
                                           'no_policy_rule', 'hidden_sender')}
        if counts['included'] + sum(excluded.values()) != \
                counts['master_visible_rows']:
            raise ProjectionError('build_failed', 'count accounting mismatch',
                                   counts)
        report = {
            'generation': generation, 'commit_seq': commit_seq, 'epoch': epoch,
            'policy_version': policy['policy_version'], 'built_at': now_ms(),
            'counts': counts, 'coverage_gaps': gaps.report(),
        }
        manifest = {
            'archive_id': self.archive_id,
            'projection_id': self.projection_id,
            'projection_contract': PROJECTION_CONTRACT,
            'projection_version': PROJECTION_VERSION,
            'epoch': epoch,
            'policy_version': policy['policy_version'],
            'commit_seq': commit_seq,
            'revocations_version': int(rev['version']),
            'generation': generation,
            'db': db_name,
            'report': report_name,
            'db_sha256': db_sha,
            'db_bytes': db_bytes,
            'message_count': counts['included'],
            'talkers': sorted(talkers),
            'built_at': report['built_at'],
        }
        self.binding.write_json_atomic(f'{PROJECTION_DIR}/{report_name}',
                                       report, _FILE_MODE)
        # THE publish action — everything above is invisible until this
        # atomic replacement lands (unique temp + fsync + rename + dir fsync)
        self.binding.write_json_atomic(MANIFEST_RELPATH, manifest, _FILE_MODE)
        return manifest

    def _stream_rows(self, master, conn, grants, policy, gaps, counts, talkers):
        cursor = master.execute(_MASTER_SELECT)
        batch = []
        while True:
            rows = cursor.fetchmany(256)
            if not rows:
                break
            for row in rows:
                included = self._project_row(row, grants, policy, gaps, counts,
                                             batch)
                if included:
                    talkers.add(row['talker'])
            if batch:
                conn.executemany(
                    'INSERT INTO msgs(seq, account, talker, create_time, '
                    'sort_seq, msg_type, sub_type, status, direction, '
                    'sender_display, vt, text_body, quote_text, tags_json, '
                    'has_revisions, media_ids_json) '
                    'VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)', batch)
                conn.executemany(
                    'INSERT INTO msgs_fts(rowid, vt) VALUES(?,?)',
                    [(r[0], r[10]) for r in batch])
                batch.clear()
        # each fetchmany round flushes its accumulated rows before the next;
        # the final non-empty round is flushed before the empty one ends it

    def _project_row(self, row, grants, policy, gaps, counts, batch):
        """Whitelist + visibility filter for ONE master row. Returns True iff
        the row entered the projection."""
        if row['visibility'] != 'visible':
            counts['non_visible'] += 1
            return False
        if row['deleted_at'] is not None:
            counts['deleted'] += 1
            return False
        counts['master_visible_rows'] += 1
        account, talker = row['account'], row['talker']
        query_allowed, revoked_at = grants.get((account, talker), (0, None))
        if revoked_at is not None:
            counts['revoked_talker'] += 1
            return False
        if not query_allowed:
            counts['query_not_allowed'] += 1
            return False
        rule = _rule_get(policy, account, talker)
        if rule is None:
            counts['no_policy_rule'] += 1
            return False
        if rule['senders']['mode'] == 'allowlist' \
                and row['sender_id'] not in rule['senders']['allow']:
            counts['hidden_sender'] += 1
            return False
        body, quote_text, tags, vt = _visible_texts(
            row['content_text'], row['content_json'], rule, gaps)
        batch.append((
            row['msg_uid'], account, talker, row['create_time'],
            row['sort_seq'], row['msg_type'], row['sub_type'],
            row['status'], row['direction'],
            row['sender_display_name'], vt, body, quote_text,
            canonical(tags), 1 if row['has_revisions'] else 0,
            canonical(_media_ids(row['media_refs_json'], gaps))))
        counts['included'] += 1
        return True

    def _fsync_and_hash(self, proj_fd, tmp_name):
        fd = os.open(tmp_name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=proj_fd)
        try:
            os.fchmod(fd, _FILE_MODE)
            digest = hashlib.sha256()
            size = 0
            offset = 0
            while True:
                block = os.pread(fd, 1 << 20, offset)
                if not block:
                    break
                digest.update(block)
                size += len(block)
                offset += len(block)
            os.fsync(fd)
            return digest.hexdigest(), size
        finally:
            os.close(fd)

    @staticmethod
    def _cleanup_build(proj_fd, tmp_name, conn, vfs):
        if conn is not None:
            try:
                conn.close()
            except sqlite3.Error:
                pass
        release_pinned_vfs(vfs)
        try:
            os.unlink(tmp_name, dir_fd=proj_fd)
        except OSError:
            pass

    # ── retention / reports ─────────────────────────────────────────
    def current_manifest(self):
        try:
            return self.binding.read_json(MANIFEST_RELPATH)
        except GuardError as exc:
            if exc.code == 'path_missing':
                raise ProjectionError('projection_unpublished',
                                      'no generation published yet')
            raise
        except ValueError:
            raise ProjectionError('projection_manifest_invalid',
                                  'manifest not valid JSON')

    def prune(self, keep=2):
        """Remove superseded generations (never the published one) and stale
        build temps. Deletion happens only with a readable manifest naming
        the current artifacts — without it, nothing is removed."""
        manifest = self.current_manifest()
        keep = max(1, int(keep))
        proj_fd = self.binding.open_dir(PROJECTION_DIR)
        removed = []
        try:
            entries = os.listdir(proj_fd)
            current_gen = manifest.get('generation')
            by_generation = {}
            for name in entries:
                m = _GEN_DB_RE.match(name) or _GEN_REPORT_RE.match(name)
                if m and m.group(1) != current_gen:
                    by_generation.setdefault(m.group(1), []).append(name)
            ordered = sorted(by_generation)  # commit_seq-embedded, sortable
            excess = ordered[:max(0, len(ordered) - (keep - 1))]
            for generation in excess:
                for name in by_generation[generation]:
                    os.unlink(name, dir_fd=proj_fd)
                    removed.append(name)
            for name in entries:
                if _BUILD_TMP_RE.match(name):
                    os.unlink(name, dir_fd=proj_fd)  # single writer: stale
                    removed.append(name)
            os.fsync(proj_fd)
        finally:
            os.close(proj_fd)
        return {'removed': removed}

    def coverage(self):
        manifest = self.current_manifest()
        report_name = manifest.get('report')
        if not _GEN_REPORT_RE.match(report_name or ''):
            raise ProjectionError('projection_manifest_invalid',
                                  'manifest report name malformed')
        try:
            return self.binding.read_json(f'{PROJECTION_DIR}/{report_name}')
        except GuardError as exc:
            if exc.code == 'path_missing':
                raise ProjectionError('projection_unpublished',
                                      'report for current generation missing')
            raise

    def close(self):
        """Nothing persistent is held beyond the caller's store/binding."""


# ══════════════════════════════════════════════════════════════════
# query service (query side — projection root only)
# ══════════════════════════════════════════════════════════════════

_COLS = ('seq, talker, create_time, sort_seq, msg_type, sub_type, status, '
         'direction, sender_display, text_body, quote_text, tags_json, '
         'has_revisions, media_ids_json')


def _row_out(row, snippet=None):
    out = {
        'seq': row['seq'], 'talker': row['talker'],
        'msg_time': row['create_time'], 'sort_seq': row['sort_seq'],
        'msg_type': row['msg_type'], 'sub_type': row['sub_type'],
        'status': row['status'], 'direction': row['direction'],
        'sender_display': row['sender_display'], 'text': row['text_body'],
        'has_revisions': bool(row['has_revisions']),
        'tags': json.loads(row['tags_json']),
        'media': json.loads(row['media_ids_json']),
    }
    if row['quote_text'] is not None:
        out['quote_text'] = row['quote_text']
    if snippet is not None:
        out['snippet'] = snippet
    return out


def _check_limit(limit, upper=_MAX_LIMIT):
    if isinstance(limit, bool) or not isinstance(limit, int) \
            or limit < 1 or limit > upper:
        raise ProjectionError('limit_invalid',
                              f'limit must be an integer in [1, {upper}]')
    return limit


def _check_window(value, upper=_MAX_CONTEXT):
    if isinstance(value, bool) or not isinstance(value, int) \
            or value < 0 or value > upper:
        raise ProjectionError('limit_invalid',
                              f'window must be an integer in [0, {upper}]')
    return value


class ProjectionService:
    """Independent query entry. Receives ONLY the projection root; the guard
    binding carries its own marker (archive id '<archive>:projection'), so
    pointing it at the master root fails verification — and as a second
    belt, a root that contains master.db is refused outright."""

    def __init__(self, binding, projection_id, account, expected_epoch,
                 verify_db=True):
        self.binding = binding
        self.projection_id = projection_id
        self.account = account
        self.expected_epoch = int(expected_epoch)
        self.verify_db = verify_db

    @classmethod
    def open(cls, root, *, projection_id, account, expected_epoch,
             guard_config=None, allow_synthetic=False, verify_db=True):
        if guard_config is None:
            if not allow_synthetic:
                raise ProjectionError(
                    'guard_config_required',
                    'production query service requires a guard config '
                    '(or explicit allow_synthetic for the test tier)')
            guard_config = {'tier': 'synthetic', 'archive_id': projection_id}
        guard = make_guard(root, guard_config, allow_synthetic)
        binding = guard.verify(root)  # marker enforces projection_id (fails
        #                          for a master root: different archive id)
        try:
            # belt: never serve queries from anything that IS a master root
            try:
                binding.stat_path('master.db')
            except GuardError as exc:
                if exc.code != 'path_missing':
                    raise
            else:
                raise ProjectionError(
                    'master_root_rejected',
                    'query entry must receive the projection root, not the '
                    'archive/master root')
            if not isinstance(account, str) or not account:
                raise ProjectionError('service_invalid',
                                      'account (server-side config) required')
            try:
                expected_epoch = int(expected_epoch)
            except (TypeError, ValueError):
                raise ProjectionError('service_invalid',
                                      'expected_epoch must be an integer')
            return cls(binding, projection_id, account, expected_epoch,
                       verify_db)
        except BaseException:
            binding.close()
            raise

    # ── per-request revalidation ────────────────────────────────────
    def _revalidate(self):
        """EVERY request (stateless or session) re-proves: binding liveness
        (production: full topology/UUID/capacity/marker re-proof), current
        manifest, current policy, current revocations. Cheap reads, zero
        trust in anything cached."""
        self.binding.check_alive()
        try:
            manifest = self.binding.read_json('manifest.json')
        except GuardError as exc:
            if exc.code == 'path_missing':
                raise ProjectionError('projection_unpublished',
                                      'no generation published yet')
            raise
        except ValueError:
            raise ProjectionError('projection_manifest_invalid',
                                  'manifest not valid JSON')
        self._validate_manifest(manifest)
        try:
            policy = self.binding.read_json('policy/policy.json')
        except GuardError as exc:
            if exc.code == 'path_missing':
                raise ProjectionError('policy_missing',
                                      'projection policy file absent')
            raise
        except ValueError:
            raise ProjectionError('policy_invalid',
                                  'policy file not valid JSON')
        _validate_policy(policy)
        if int(policy['policy_version']) != int(manifest['policy_version']):
            # A tightened active policy cannot wait for the next build while
            # readers continue consulting the older, broader generation.
            # Block stateless and newly opened readers as well as sessions;
            # the generation becomes queryable again only after publication
            # records the active policy version in its manifest.
            raise ProjectionError(
                'session_stale',
                'active policy version differs from the published projection; '
                'queries remain blocked until a matching generation is built',
                {'manifest_policy_version': manifest['policy_version'],
                 'active_policy_version': policy['policy_version']})
        try:
            rev = self.binding.read_json('policy/revocations.json')
        except GuardError as exc:
            if exc.code == 'path_missing':
                raise ProjectionError('revocations_missing',
                                      'revocations file absent')
            raise
        except ValueError:
            raise ProjectionError('revocations_invalid',
                                  'revocations file not valid JSON')
        if not isinstance(rev, dict) or not isinstance(rev.get('version'), int) \
                or not isinstance(rev.get('revoked'), list):
            raise ProjectionError('revocations_invalid', 'revocations malformed')
        return manifest, policy, rev

    def _validate_manifest(self, manifest):
        if not isinstance(manifest, dict):
            raise ProjectionError('projection_manifest_invalid',
                                  'manifest must be an object')
        problems = []
        if manifest.get('projection_contract') != PROJECTION_CONTRACT:
            problems.append('projection_contract')
        if manifest.get('projection_version') != PROJECTION_VERSION:
            problems.append('projection_version')
        if manifest.get('projection_id') != self.projection_id:
            problems.append('projection_id')
        generation = manifest.get('generation')
        if not isinstance(generation, str) or not _GEN_RE.match(generation):
            problems.append('generation')
        if manifest.get('db') != f'p-{generation}.db':
            problems.append('db')
        if manifest.get('report') != f'report-{generation}.json':
            problems.append('report')
        for key in ('epoch', 'policy_version', 'commit_seq',
                    'revocations_version', 'message_count', 'db_bytes',
                    'built_at'):
            value = manifest.get(key)
            if not isinstance(value, int) or isinstance(value, bool):
                problems.append(key)
        sha = manifest.get('db_sha256')
        if not isinstance(sha, str) or not re.fullmatch(r'[0-9a-f]{64}', sha):
            problems.append('db_sha256')
        if problems:
            raise ProjectionError('projection_manifest_invalid',
                                  'manifest fields invalid',
                                  {'fields': sorted(set(problems))})
        if manifest['epoch'] != self.expected_epoch:
            raise ProjectionError(
                'epoch_stale',
                'manifest epoch does not match the expected epoch',
                {'manifest_epoch': manifest['epoch'],
                 'expected_epoch': self.expected_epoch})

    def _authorize(self, policy, rev, talker):
        rule = _rule_get(policy, self.account, talker)
        if rule is None:
            raise ProjectionError(
                'talker_not_queryable',
                f'no query policy rule for talker {talker!r}',
                {'account': self.account, 'talker': talker})
        if [self.account, talker] in rev['revoked']:
            raise ProjectionError(
                'talker_revoked',
                f'talker {talker!r} was revoked',
                {'account': self.account, 'talker': talker})
        return rule

    # ── connections ─────────────────────────────────────────────────
    def _connect(self, manifest):
        """Read-only connection to the CURRENT generation. Name is validated
        against the strict pattern, the leaf is stat'ed through the binding
        (never opened post-connect — that would strip fcntl locks), sha256 is
        verified pre-connect, and on Linux the connection goes through the
        pinned VFS anchored to the projection binding's held root fd."""
        db_name = manifest['db']
        try:
            st = self.binding.stat_path(db_name)
        except GuardError as exc:
            if exc.code == 'path_missing':
                raise ProjectionError('projection_db_missing',
                                      'published generation db absent')
            raise
        if st.st_size != manifest['db_bytes']:
            raise ProjectionError('projection_db_corrupt',
                                  'generation db size differs from manifest')
        if self.verify_db:
            fd = self.binding.open_file_read(db_name)  # pre-connect only
            try:
                digest = hashlib.sha256()
                offset = 0
                while True:
                    block = os.pread(fd, 1 << 20, offset)
                    if not block:
                        break
                    digest.update(block)
                    offset += len(block)
            finally:
                os.close(fd)
            if digest.hexdigest() != manifest['db_sha256']:
                raise ProjectionError('projection_db_corrupt',
                                      'generation db sha256 mismatch')
        vfs = None
        conn = None
        try:
            if sys.platform.startswith('linux'):
                vfs = register_pinned_vfs(self.binding.root_fd, db_name)
                conn = sqlite3.connect(
                    f'file:{db_name}?vfs={vfs.name}&mode=ro',
                    uri=True, isolation_level=None, timeout=10.0)
                reported = conn.execute('PRAGMA database_list').fetchone()[2]
                if reported != vfs.anchored_main():
                    raise ProjectionError('projection_db_invalid',
                                          'query connection not anchored')
            else:
                path = str(Path(self.binding.root) / db_name)
                conn = sqlite3.connect('file:' + quote(path) + '?mode=ro',
                                       uri=True, isolation_level=None,
                                       timeout=10.0)
                conn.execute('PRAGMA temp_store=MEMORY')
            conn.row_factory = sqlite3.Row
            conn.execute('PRAGMA busy_timeout=10000')
            conn.execute('PRAGMA query_only=ON')
            row = conn.execute("SELECT value FROM proj_meta "
                               "WHERE key='projection_version'").fetchone()
            if row is None or row[0] != str(PROJECTION_VERSION):
                raise ProjectionError('projection_db_invalid',
                                      'db lacks projection version marker')
            return conn, vfs
        except BaseException:
            if conn is not None:
                try:
                    conn.close()
                except sqlite3.Error:
                    pass
            release_pinned_vfs(vfs)
            raise

    @staticmethod
    def _close_conn(conn, vfs):
        try:
            conn.close()
        finally:
            release_pinned_vfs(vfs)

    # ── stateless API (endpoint factory shape) ──────────────────────
    def manifest(self):
        manifest, _, _ = self._revalidate()
        return manifest

    def epoch(self):
        return self.manifest()['epoch']

    def policy_version(self):
        return self.manifest()['policy_version']

    def sessions(self):
        """Safe session listing for the configured account."""
        manifest, _, _ = self._revalidate()
        conn, vfs = self._connect(manifest)
        try:
            rows = conn.execute(
                'SELECT talker, COUNT(*) AS n, MAX(create_time) AS last_time '
                'FROM msgs WHERE account=? GROUP BY talker '
                'ORDER BY last_time DESC', (self.account,)).fetchall()
            return {'account': self.account, 'generation': manifest['generation'],
                    'sessions': [dict(r) for r in rows]}
        finally:
            self._close_conn(conn, vfs)

    def messages(self, talker, limit=50):
        limit = _check_limit(limit)
        manifest, policy, rev = self._revalidate()
        self._authorize(policy, rev, talker)
        conn, vfs = self._connect(manifest)
        try:
            rows = conn.execute(
                f'SELECT {_COLS} FROM msgs WHERE account=? AND talker=? '
                'ORDER BY create_time, sort_seq, seq LIMIT ?',
                (self.account, talker, limit)).fetchall()
            return {'talker': talker, 'generation': manifest['generation'],
                    'count': len(rows),
                    'messages': [_row_out(r) for r in rows]}
        finally:
            self._close_conn(conn, vfs)

    def search(self, talker, match, limit=20):
        limit = _check_limit(limit)
        if not isinstance(match, str) or len(match) < 3:
            raise ProjectionError('match_too_short',
                                  'trigram search needs at least 3 characters')
        manifest, policy, rev = self._revalidate()
        self._authorize(policy, rev, talker)
        # phrase-quote the needle: user input can never become FTS syntax
        needle = '"' + match.replace('"', '""') + '"'
        conn, vfs = self._connect(manifest)
        try:
            rows = conn.execute(
                f'SELECT {_COLS}, snippet(msgs_fts, 0, "[", "]", "…", 32) '
                'AS snip FROM msgs JOIN msgs_fts ON msgs_fts.rowid = msgs.seq '
                'WHERE msgs.account=? AND msgs.talker=? AND msgs_fts MATCH ? '
                'ORDER BY msgs.create_time, msgs.sort_seq, msgs.seq LIMIT ?',
                (self.account, talker, needle, limit)).fetchall()
            return {'talker': talker, 'match': match,
                    'generation': manifest['generation'], 'count': len(rows),
                    'results': [_row_out(r, snippet=r['snip']) for r in rows]}
        finally:
            self._close_conn(conn, vfs)

    def context(self, talker, seq, before=10, after=10):
        before = _check_window(before)
        after = _check_window(after)
        if isinstance(seq, bool) or not isinstance(seq, int):
            raise ProjectionError('limit_invalid', 'seq must be an integer')
        manifest, policy, rev = self._revalidate()
        self._authorize(policy, rev, talker)
        conn, vfs = self._connect(manifest)
        try:
            anchor = conn.execute(
                'SELECT create_time, sort_seq, seq FROM msgs '
                'WHERE account=? AND talker=? AND seq=?',
                (self.account, talker, seq)).fetchone()
            if anchor is None:
                raise ProjectionError('anchor_unknown',
                                      'anchor seq not present for talker',
                                      {'talker': talker, 'seq': seq})
            key = (anchor['create_time'], anchor['sort_seq'], anchor['seq'])
            older = conn.execute(
                f'SELECT {_COLS} FROM msgs WHERE account=? AND talker=? '
                'AND (create_time, sort_seq, seq) < (?, ?, ?) '
                'ORDER BY create_time DESC, sort_seq DESC, seq DESC LIMIT ?',
                (self.account, talker, *key, before)).fetchall()
            newer = conn.execute(
                f'SELECT {_COLS} FROM msgs WHERE account=? AND talker=? '
                'AND (create_time, sort_seq, seq) > (?, ?, ?) '
                'ORDER BY create_time, sort_seq, seq LIMIT ?',
                (self.account, talker, *key, after)).fetchall()
            return {'talker': talker, 'anchor': seq,
                    'generation': manifest['generation'],
                    'before': [_row_out(r) for r in reversed(older)],
                    'after': [_row_out(r) for r in newer]}
        finally:
            self._close_conn(conn, vfs)

    def coverage(self):
        manifest, _, _ = self._revalidate()
        report_name = manifest['report']
        try:
            return self.binding.read_json(report_name)
        except GuardError as exc:
            if exc.code == 'path_missing':
                raise ProjectionError('projection_unpublished',
                                      'report for current generation missing')
            raise

    # ── long-lived sessions ─────────────────────────────────────────
    def open_session(self):
        manifest, policy, rev = self._revalidate()
        conn, vfs = self._connect(manifest)
        return QuerySession(self, manifest, policy, rev, conn, vfs)

    def close(self):
        self.binding.close()


class QuerySession:
    """A long-lived query session over ONE generation. EVERY call re-runs the
    full per-request revalidation and compares the (epoch, policy_version,
    revocations_version, generation) tuple — revocation, policy change, epoch
    bump or any new publication stale-dates the session on its NEXT request;
    the client must re-open (and re-opening a revoked talker is denied)."""

    def __init__(self, service, manifest, policy, rev, conn, vfs):
        self.service = service
        self.conn = conn
        self._vfs = vfs
        # the LIVE policy-file version is part of the tuple: a tightened
        # policy stale-dates sessions even before the rebuilt generation
        # is published
        self._tuple = (manifest['epoch'], manifest['policy_version'],
                       policy['policy_version'], int(rev['version']),
                       manifest['generation'])
        self._generation = manifest['generation']

    def _check(self):
        manifest, policy, rev = self.service._revalidate()
        current = (manifest['epoch'], manifest['policy_version'],
                   policy['policy_version'], int(rev['version']),
                   manifest['generation'])
        if current != self._tuple:
            raise ProjectionError(
                'session_stale',
                'projection moved on (revocation, policy, epoch or new '
                'generation); re-open the session',
                {'session': list(self._tuple), 'current': list(current)})
        return manifest, policy, rev

    def messages(self, talker, limit=50):
        limit = _check_limit(limit)
        manifest, policy, rev = self._check()
        self.service._authorize(policy, rev, talker)
        rows = self.conn.execute(
            f'SELECT {_COLS} FROM msgs WHERE account=? AND talker=? '
            'ORDER BY create_time, sort_seq, seq LIMIT ?',
            (self.service.account, talker, limit)).fetchall()
        return {'talker': talker, 'generation': self._generation,
                'count': len(rows), 'messages': [_row_out(r) for r in rows]}

    def search(self, talker, match, limit=20):
        limit = _check_limit(limit)
        if not isinstance(match, str) or len(match) < 3:
            raise ProjectionError('match_too_short',
                                  'trigram search needs at least 3 characters')
        manifest, policy, rev = self._check()
        self.service._authorize(policy, rev, talker)
        needle = '"' + match.replace('"', '""') + '"'
        rows = self.conn.execute(
            f'SELECT {_COLS}, snippet(msgs_fts, 0, "[", "]", "…", 32) '
            'AS snip FROM msgs JOIN msgs_fts ON msgs_fts.rowid = msgs.seq '
            'WHERE msgs.account=? AND msgs.talker=? AND msgs_fts MATCH ? '
            'ORDER BY msgs.create_time, msgs.sort_seq, msgs.seq LIMIT ?',
            (self.service.account, talker, needle, limit)).fetchall()
        return {'talker': talker, 'match': match,
                'generation': self._generation, 'count': len(rows),
                'results': [_row_out(r, snippet=r['snip']) for r in rows]}

    def context(self, talker, seq, before=10, after=10):
        before = _check_window(before)
        after = _check_window(after)
        manifest, policy, rev = self._check()
        self.service._authorize(policy, rev, talker)
        anchor = self.conn.execute(
            'SELECT create_time, sort_seq, seq FROM msgs '
            'WHERE account=? AND talker=? AND seq=?',
            (self.service.account, talker, seq)).fetchone()
        if anchor is None:
            raise ProjectionError('anchor_unknown',
                                  'anchor seq not present for talker',
                                  {'talker': talker, 'seq': seq})
        key = (anchor['create_time'], anchor['sort_seq'], anchor['seq'])
        older = self.conn.execute(
            f'SELECT {_COLS} FROM msgs WHERE account=? AND talker=? '
            'AND (create_time, sort_seq, seq) < (?, ?, ?) '
            'ORDER BY create_time DESC, sort_seq DESC, seq DESC LIMIT ?',
            (self.service.account, talker, *key, before)).fetchall()
        newer = self.conn.execute(
            f'SELECT {_COLS} FROM msgs WHERE account=? AND talker=? '
            'AND (create_time, sort_seq, seq) > (?, ?, ?) '
            'ORDER BY create_time, sort_seq, seq LIMIT ?',
            (self.service.account, talker, *key, after)).fetchall()
        return {'talker': talker, 'anchor': seq,
                'generation': self._generation,
                'before': [_row_out(r) for r in reversed(older)],
                'after': [_row_out(r) for r in newer]}

    def close(self):
        try:
            self.conn.close()
        finally:
            release_pinned_vfs(self._vfs)
