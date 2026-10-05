"""Run application (reconcile) — applies one staged capture run inside the
master's single-writer FULL transaction.

Guarantees implemented here (external-archive-v3 §S4 reconcile):
- C0 protection covers ALL content updates, not only missing marks: a row is
  updated only if its last_content_or_presence_commit_seq <= run.c0; otherwise
  the row is recorded as a conflict and left untouched — an old capture can
  never overwrite content committed by a newer one.
- Identity rules: server_id is the stable primary identity; local
  (shard,rowid) only for rows without server_id. Rowid reuse never overwrites
  history: existing rows are preserved, the new row enters under an
  independent identity, and an identity_bind event is recorded.
- Missing candidates require BOTH dual conditions (existed at c0: never
  committed after run start) AND a fresh complete re-verification view across
  every shard of the session; incomplete enumeration or unknown shards never
  publish missing.
- Anomaly gate: suspiciously large destructive diffs reject the whole batch
  for manual review (nothing applied). Thresholds are configurable defaults
  pending real-scale approval.
- soft missing never deletes archive history and never hides currently
  visible rows.
- source_edit vs source_upgrade are distinct: a fingerprint change on an
  ordinary row is source_edit; restoring a full payload onto a row whose
  provenance is legacy_visibility_projected (or an explicit backfill run) is
  source_upgrade — counted separately in the receipt.

Consumer semantics for the Rust source contract (2026-10-05 review):
- Sender fields are `real_sender_id` / `real_sender_name` (Name2Id-resolved).
  They are consumed verbatim; a record without them stores NO sender and is
  audited (`sender_unknown`) — a sender is never fabricated from other
  fields.
- `message_content` is the DECODED ORIGINAL and is not guaranteed body-only:
  XML-typed messages carry structure (appmsg, refermsg, …). The master's
  `content_text` is a conservatively derived BODY-ONLY view: plain text
  passes through unchanged; XML is parsed and only KNOWN text-bearing nodes
  (title / des / a first-level refermsg) may surface, nested quotes never do,
  and anything unrecognized is excluded and audited (`unknown_content_*`
  counts carry element NAMES only, never values). The verbatim decoded
  original always stays in raw_record_json; projections never see XML.
"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET

from .errors import ReconcileError
from .util import canonical, identity_key, now_ms

ROW_UPSERT = 'upsert'
ROW_MISSING = 'missing_candidate'

# Known WeChat XML text-bearing elements for the conservative body-only
# derivation. Anything outside this set is excluded from content_text and
# audited by element NAME only.
_XML_TEXT_NODES = ('title', 'des')
_XML_QUOTE_NODE = 'refermsg'
_XML_QUOTE_TEXT_NODES = ('content', 'displayname')
_XML_ROOT_ALLOWED = ('msg', 'appmsg', 'ReferMessage')
# structural (non-body) elements recognized by NAME: they carry no body text
# and do not trip the unknown-element audit. Text-node-named elements are
# deliberately ABSENT — their eligibility is POSITIONAL, not nominal.
_XML_STRUCTURAL_KNOWN = frozenset((
    'msg', 'appmsg', 'refermsg',
    'appinfo', 'fromusername', 'scene', 'appattach', 'thumburl',
    'weburl', 'weappinfo', 'type',
    'svrid', 'fromusr', 'chatusr', 'createtime',
))


def _xml_text_of(node):
    """Direct text of an element, or None — never recursive (nested structure
    must not leak into the body-only view)."""
    if node is None or node.text is None:
        return None
    text = node.text.strip()
    return text or None


def _tag_of(element):
    return element.tag if isinstance(element.tag, str) else '<anonymous>'


def derive_content_view(message_content):
    """Conservative body-only derivation from the decoded original.

    Returns (text_body, quote_text, structured, audits) where `structured`
    is the content_json the projection consumes ({text, quote, tags} with
    only keys confidently derived) and `audits` is a list of
    {'kind': 'unknown_content_root'|'unknown_content_element'|
    'nested_quote'|'sender_prefix_excluded', 'names': [element names]}
    entries — names only, values never leave the derivation.

    Plain text without any XML document passes through verbatim (body-only
    by construction — the source contract only guarantees text for non-XML
    rows). WeChat group rows prefix the in-chat sender ("wxid:\\n<msg>…"):
    the prefix is sender metadata and never becomes body text, and the XML
    part is NEVER treated as plaintext just because it carries a prefix.

    XML eligibility is POSITIONAL (msg/appmsg direct-ancestor whitelist):
    title/des count as body text only as DIRECT children of the root appmsg,
    or of an appmsg that is itself a direct child of the root msg. A
    document-order scan (root.iter, first title anywhere) would promote a
    refermsg-INTERNAL title to the body — exactly the hidden-quote leak this
    derivation exists to prevent — so positions are tracked explicitly."""
    audits = []
    if message_content is None:
        return None, None, {}, audits
    if not isinstance(message_content, str):
        return None, None, {}, audits
    stripped = message_content.lstrip()
    prefix = None
    xml_source = stripped
    cut = stripped.find('<')
    if not stripped.startswith('<'):
        if cut <= 0:
            return message_content, None, {'text': message_content}, audits
        prefix = stripped[:cut]
        xml_source = stripped[cut:]
    try:
        root = ET.fromstring(xml_source)
    except ET.ParseError:
        # unparseable-after-'<' is conservatively withheld: the string is
        # not proven plain text (the '<' may open structure) and is never
        # promoted on a guess
        audits.append({'kind': 'unknown_content_root', 'names': ['<unparseable>']})
        return None, None, {}, audits
    if root.tag not in _XML_ROOT_ALLOWED:
        audits.append({'kind': 'unknown_content_root', 'names': [root.tag]})
        return None, None, {}, audits
    if prefix is not None:
        audits.append({'kind': 'sender_prefix_excluded', 'names': []})
    text_body = []
    quote_text = []
    unknown = []

    def note_unknown(tag):
        if tag not in unknown:
            unknown.append(tag)

    def scan_quote(element):
        """Direct children of a refermsg only: content/displayname are quote
        candidates; title/des here are quote-INTERNAL fields (excluded and
        audited); nothing inside a refermsg is ever body-eligible, so the
        walk never descends past it."""
        for child in element:
            tag = _tag_of(child)
            if tag in _XML_QUOTE_TEXT_NODES:
                value = _xml_text_of(child)
                if value is not None and value.startswith('<'):
                    # a quote whose own content is XML is a NESTED quote:
                    # excluded under every policy, audited, never indexed
                    audits.append({'kind': 'nested_quote', 'names': ['refermsg']})
                elif value and not quote_text:
                    quote_text.append(value)
            elif tag in _XML_TEXT_NODES or tag not in _XML_STRUCTURAL_KNOWN:
                note_unknown(tag)

    stack = [(root, root.tag == 'appmsg')]
    while stack:
        element, eligible_here = stack.pop()
        for child in element:
            tag = _tag_of(child)
            if tag in _XML_TEXT_NODES:
                value = _xml_text_of(child) if eligible_here else None
                if eligible_here and not text_body and value:
                    text_body.append(value)
                else:
                    # title/des in a non-eligible position (e.g. INSIDE a
                    # refermsg) is excluded and audited by name — the value
                    # never leaves the derivation
                    note_unknown(tag)
            elif tag == _XML_QUOTE_NODE:
                scan_quote(child)
            else:
                if tag not in _XML_STRUCTURAL_KNOWN:
                    note_unknown(tag)
                eligible_child = tag == 'appmsg' and element is root \
                    and root.tag == 'msg'
                stack.append((child, eligible_child))
    if unknown:
        audits.append({'kind': 'unknown_content_element', 'names': unknown[:8]})
    text = text_body[0] if text_body else None
    quote = quote_text[0] if quote_text else None
    structured = {}
    if text is not None:
        structured['text'] = text
    if quote is not None:
        structured['quote'] = quote
    return text, quote, structured, audits


def consume_sender(record):
    """(sender_id, sender_display_name, audited) from the Rust contract.

    real_sender_name is the Name2Id-RESOLVED stable account id (wxid) — the
    sender's IDENTITY and the only value that may become master.sender_id
    (projection sender allowlists match on it). real_sender_id is only the
    per-shard Name2Id ROW NUMBER (values collide across shards): it never
    becomes sender_id — the insert/update paths keep it in the raw block for
    audit. sender_display_name comes only from an explicit display field
    (real_sender_display_name); the resolved wxid is an id, not a contact
    name, and is never fabricated into one. A record with no resolved name
    stores NO sender and is audited (sender_unknown)."""
    resolved = record.get('real_sender_name')
    if resolved is not None and not isinstance(resolved, str):
        resolved = str(resolved)
    display = record.get('real_sender_display_name')
    if display is not None and not isinstance(display, str):
        display = str(display)
    sender_id = resolved if resolved else None
    audited = sender_id is None
    return sender_id, (display or None), audited


def _raw_block_of(record):
    """The lossless raw block, extended with the per-shard sender row number:
    real_sender_id collides across shards and must stay an audit fact in
    raw_record_json — never a queryable identity column."""
    raw = dict(record.get('raw') or {})
    local_sender_row = record.get('real_sender_id')
    if local_sender_row is not None:
        raw.setdefault('real_sender_id', local_sender_row)
    return canonical(raw)


class Receipt:
    def __init__(self, batch_id, run_id):
        self.batch_id = batch_id
        self.run_id = run_id
        self.epoch = None
        self.commit_seq = None
        self.counts = {
            'rows_inserted': 0, 'rows_updated': 0, 'late': 0,
            'source_edit': 0, 'source_upgrade': 0, 'identity_binds': 0,
            'conflicts': 0, 'no_change': 0,
            'media_assets_stored': 0, 'media_needs_open': 0,
            'media_needs_resolved': 0,
            'media_vanished_errors': 0, 'media_upload_errors': 0,
            'missing_published': 0, 'missing_skipped_incomplete': 0,
            'missing_refused_unclaimed': 0,
            # consumer-semantics audits (names only, never values)
            'sender_unknown': 0, 'unknown_content_root': 0,
            'unknown_content_element': 0, 'nested_quote': 0,
            'sender_prefix_excluded': 0,
            # legacy migration block
            'legacy_rows': 0, 'legacy_revisions': 0, 'legacy_missing': 0,
            'legacy_audit_unknown': 0,
        }
        self.conflicts = []
        self.warnings = []

    def to_json(self):
        return {
            'batch_id': self.batch_id, 'run_id': self.run_id,
            'kind': 'capture_ack', 'epoch': self.epoch,
            'commit_seq': self.commit_seq, 'counts': dict(self.counts),
            'conflicts': self.conflicts, 'warnings': self.warnings,
        }


def _validate_row_op(op, i):
    """Structural validation of ONE row op (inline or streamed from a page) —
    malformed ops never touch the master (consumer-visible error, not a
    crash)."""
    if not isinstance(op, dict) or 'op' not in op or 'identity' not in op:
        raise ReconcileError('run_invalid', f'row op #{i} malformed')
    if op['op'] not in (ROW_UPSERT, ROW_MISSING):
        raise ReconcileError('run_invalid', f'row op #{i} unknown kind {op["op"]!r}')
    ident = op['identity']
    if not isinstance(ident, dict):
        raise ReconcileError('run_invalid', f'row op #{i} identity malformed')
    if op['op'] == ROW_MISSING and ident.get('server_id') is not None:
        # a server-identity missing candidate is topology-independent: the
        # row is identified by its server id alone (迁片-safe)
        if not isinstance(ident['server_id'], (str, int)):
            raise ReconcileError('run_invalid',
                                 f'row op #{i} server_id must be a string/int')
    elif 'shard' not in ident or 'local_rowid' not in ident:
        # upserts and local-only rows need the full v2 local identity
        raise ReconcileError('run_invalid', f'row op #{i} identity malformed')
    if ident.get('server_id') is None and not ident.get('table'):
        raise ReconcileError(
            'run_invalid',
            f'row op #{i}: local-only identity requires the source table '
            '(contract v2: local identity is database+table+rowid)')
    if op['op'] == ROW_UPSERT and not isinstance(op.get('record'), dict):
        raise ReconcileError('run_invalid', f'row op #{i} missing record')


def validate_run(run):
    """Structural validation of the run manifest and its INLINE rows before
    any DB work. Rows streamed from record pages get the same per-op
    validation as they arrive (see _validate_row_op).

    Identity is contract v2: `shard` is the DATABASE file (message_N.db) and
    `table` (Msg_<namehash>) is separate; local-only rows (no server_id) MUST
    carry `table`, because (database, table, rowid) is the local identity —
    the same table name exists in several shards."""
    if not isinstance(run, dict):
        raise ReconcileError('run_invalid', 'run payload must be an object')
    for key in ('run_id', 'account', 'talker', 'rows'):
        if key not in run:
            raise ReconcileError('run_invalid', f'run missing {key!r}')
    if not isinstance(run['rows'], list):
        raise ReconcileError('run_invalid', 'run.rows must be a list')
    for i, op in enumerate(run['rows']):
        _validate_row_op(op, i)
    media = run.get('media') or {}
    if not isinstance(media, dict):
        raise ReconcileError('run_invalid', 'run.media must be an object')
    for field in ('uploads', 'needs'):
        if not isinstance(media.get(field, []), list):
            raise ReconcileError('run_invalid', f'media.{field} must be a list')
    for i, upload in enumerate(media.get('uploads') or []):
        if not isinstance(upload, dict) or 'sha256' not in upload \
                or 'length' not in upload:
            raise ReconcileError('run_invalid',
                                 f'media.uploads[{i}] needs sha256 and length')
    _validate_legacy(run.get('legacy'))
    return media


LEGACY_MAX_REVISIONS = 20000
LEGACY_MAX_MISSING = 20000


def _validate_legacy(legacy):
    """Structural validation of the legacy migration block: provenance
    override, historical revisions, v2 missing marks, and the honesty audit.
    Revisions/missing ride INLINE in the run frame — the wire frame bound is
    the hard cap, so oversized blocks name a smaller migration slice."""
    if legacy is None:
        return
    if not isinstance(legacy, dict):
        raise ReconcileError('run_invalid', 'run.legacy must be an object')
    override = legacy.get('provenance_override')
    if override is not None and not isinstance(override, str):
        raise ReconcileError('run_invalid',
                             'legacy.provenance_override must be a string')
    revisions = legacy.get('revisions')
    if revisions is not None:
        if not isinstance(revisions, list) or len(revisions) > LEGACY_MAX_REVISIONS:
            raise ReconcileError(
                'legacy_block_too_large',
                f'legacy.revisions carries {len(revisions) if isinstance(revisions, list) else "non-list"} '
                f'entries (cap {LEGACY_MAX_REVISIONS}); migrate per talker')
        for i, rev in enumerate(revisions):
            if not isinstance(rev, dict) or not isinstance(rev.get('identity'), dict) \
                    or 'payload' not in rev:
                raise ReconcileError('run_invalid',
                                     f'legacy.revisions[{i}] malformed')
    missing = legacy.get('missing')
    if missing is not None:
        if not isinstance(missing, list) or len(missing) > LEGACY_MAX_MISSING:
            raise ReconcileError(
                'legacy_block_too_large',
                f'legacy.missing carries {len(missing) if isinstance(missing, list) else "non-list"} '
                f'entries (cap {LEGACY_MAX_MISSING}); migrate per talker')
        for i, miss in enumerate(missing):
            if not isinstance(miss, dict) or not isinstance(miss.get('identity'), dict):
                raise ReconcileError('run_invalid', f'legacy.missing[{i}] malformed')
    audit = legacy.get('audit')
    if audit is not None and not isinstance(audit, dict):
        raise ReconcileError('run_invalid', 'legacy.audit must be an object')


def _anomaly_gate(store, run, missing_count, live_rows):
    cfg = store.anomaly_config()
    if missing_count >= cfg['missing_hard']:
        raise ReconcileError(
            'anomaly_gate', 'missing-candidate count over hard limit; manual review',
            {'missing': missing_count, 'hard': cfg['missing_hard']})
    if missing_count >= cfg['missing_abs_min'] and live_rows > 0:
        pct = 100.0 * missing_count / max(live_rows, 1)
        if pct >= cfg['missing_pct']:
            raise ReconcileError(
                'anomaly_gate',
                'missing-candidate ratio over threshold; manual review',
                {'missing': missing_count, 'live_rows': live_rows,
                 'pct': round(pct, 2), 'threshold_pct': cfg['missing_pct'],
                 'threshold_abs': cfg['missing_abs_min']})


class RunApplier:
    """Applied inside one full_transaction by the endpoint."""

    def __init__(self, store):
        self.store = store
        self.db = store.db
        self._recheck_cache = None

    # ── entry ────────────────────────────────────────────────────────
    def apply(self, run, tx, receipt, rows=None, page_reader=None):
        """Apply one run inside a single FULL transaction.

        `rows` streams the row ops (inline rows + record pages — the endpoint
        builds the stream); when None, the run's inline `rows` list is used.
        `page_reader(sha256)` streams a page object when the run references
        observation/recheck identity pages. The anomaly gate runs AFTER the
        row pass but still inside this transaction: a rejection rolls the
        whole run back, so page-by-page arrival can never leak a partially
        applied reconcile."""
        media = validate_run(run)
        account, talker = run['account'], run['talker']
        # independent NAS-side re-verification of account/talker scope
        self.store.check_scope(account, talker, 'capture')
        # SERVER-AUTHORITATIVE run binding and C0. The run must be one this
        # server opened via begin_run (a missing run is unacceptable, not
        # assumed), still open, in the current epoch, and bound to the same
        # account/talker/kind. C0 is read from the persisted runs row — the
        # client's run['c0'] claim is verified against it by the endpoint and
        # is NEVER consumed here (a forged c0 must not bypass capture order).
        run_row = self.store.verify_run_binding(
            run['run_id'], account, talker, run.get('kind'))
        c0 = int(run_row['run_start_seq'])
        self._recheck_cache = None
        # claim authority: only a run begun BEFORE its source view was read
        # (runs.claimed, set by begin_run(claim=True) from
        # claim_source_snapshot) may overwrite existing history or publish
        # missing. The source clock NEVER authorizes either — same-second
        # and rolled-back clocks carry no causal information.
        claimed = bool(run_row['claimed'])
        live_rows = self.db.execute(
            'SELECT COUNT(*) FROM messages WHERE account=? AND talker=? '
            'AND deleted_at IS NULL', (account, talker)).fetchone()[0]

        staged = {u['sha256']: u for u in media['uploads']}
        row_index = 0
        missing_count = 0
        # ref_key → uploaded sha for refs re-observed present with bytes
        # staged in THIS run; drives the derived fulfilment of open needs
        refs_seen = {}
        for op in (rows if rows is not None else run['rows']):
            _validate_row_op(op, row_index)
            row_index += 1
            if op['op'] == ROW_UPSERT:
                self._apply_upsert(run, op, tx, receipt, staged, c0, claimed)
                for ref in op['record'].get('media_refs') or []:
                    if isinstance(ref, dict) and ref.get('present') \
                            and ref.get('ref_key') \
                            and ref.get('sha256') in staged:
                        refs_seen[ref['ref_key']] = ref.get('sha256')
            else:
                missing_count += 1
                self._apply_missing(run, op, tx, receipt, c0, page_reader,
                                    claimed)
        # gate after the pass, inside the same transaction (see docstring)
        _anomaly_gate(self.store, run, missing_count, live_rows)
        self._apply_media_needs(run, media, tx, receipt)
        self._auto_resolve_needs(run, refs_seen, tx, receipt)
        self._apply_assets(media, staged, tx, receipt, run)
        self._apply_legacy(run, tx, receipt)

        # store the observation summary (the compare base for the NEXT run);
        # only complete enumerations may become the authoritative base
        observation = self._materialize_observation(run, page_reader)
        if isinstance(observation, dict) and observation.get(
                'enumeration_complete', run.get('enumeration', {}).get('complete')):
            self.store.put_observation(account, talker, run['run_id'], observation,
                                       tx.commit_seq)
            tx.mutation('source_observations', f'{account}/{talker}', 'put',
                        self._full_row('source_observations',
                                       'account=? AND talker=?',
                                       (account, talker)))
        elif observation is not None:
            receipt.warnings.append('observation_incomplete_not_stored')
        receipt.epoch = self.store.epoch()
        receipt.commit_seq = tx.commit_seq
        return receipt

    # ── row upsert with C0 + identity discipline ─────────────────────
    def _apply_upsert(self, run, op, tx, receipt, staged, c0, claimed):
        account, talker = run['account'], run['talker']
        ident = op['identity']
        server_id = ident.get('server_id')
        shard = ident['shard']
        shard_table = ident.get('table')
        rowid = int(ident['local_rowid'])
        record = op['record']

        uid, bound_server, bound_local = self.store.resolve_identity(
            account, talker, None if server_id is None else str(server_id),
            shard, shard_table, rowid)
        identity_bind = False
        if uid is None:
            # rowid reuse detection: local slot already bound to a different identity
            conflicts = self.store.local_identity_conflict(
                account, talker, shard, shard_table, rowid)
            if conflicts:
                identity_bind = True
                receipt.counts['identity_binds'] += 1
                tx.mutation('identity_map', f'{shard}:{shard_table}:{rowid}',
                            'identity_bind',
                            {'previous': conflicts,
                             'new_identity': identity_key(server_id, shard,
                                                          shard_table, rowid)},
                            {'run_id': run['run_id']})
            uid = self._insert_message(run, op, tx, receipt, identity_bind)
        else:
            self._update_message(run, op, uid, tx, receipt, staged, c0,
                                 bound_server, identity_bind, claimed)

    def _row_after_image(self, uid):
        """The COMPLETE stored row as a dict — mutation-log after-images must
        be replayable to reconstruct full text, not partial fingerprints."""
        row = self.db.execute('SELECT * FROM messages WHERE msg_uid=?',
                              (uid,)).fetchone()
        return dict(row) if row is not None else None

    def _full_row(self, table, where, params):
        """Complete stored row of ANY replayable table — the incremental
        backup replays mutation-log after-images verbatim, so every logged
        table carries its whole row, never a partial fingerprint."""
        row = self.db.execute(f'SELECT * FROM {table} WHERE {where}',
                              params).fetchone()
        return dict(row) if row is not None else None

    def _insert_message(self, run, op, tx, receipt, identity_bind):
        account, talker = run['account'], run['talker']
        ident = op['identity']
        record = op['record']
        server_id = ident.get('server_id')
        server_key = None if server_id is None else str(server_id)
        uid = self.store.next_msg_uid()
        legacy = (run.get('legacy') or {})
        if legacy.get('provenance_override'):
            provenance = legacy['provenance_override']
        elif record.get('late'):
            provenance = 'late'
        else:
            provenance = 'capture'
        refs = self._refs_json(record)
        sender_id, sender_display, sender_unknown = consume_sender(record)
        if sender_unknown:
            receipt.counts['sender_unknown'] += 1
        raw_block = _raw_block_of(record)
        # content_text is the conservatively derived BODY-ONLY view; the
        # verbatim decoded original (possibly XML) always lives in
        # raw_record_json — projections must never index structure
        text_body, _quote, structured, audits = derive_content_view(
            record.get('message_content'))
        for audit in audits:
            receipt.counts[audit['kind']] += 1
        # raw = every original source column, kept lossless and SEPARATE from
        # the decoded text view (contract v2)
        self.db.execute(
            'INSERT INTO messages(msg_uid,account,talker,server_id,shard,shard_table,'
            'local_rowid,create_time,sort_seq,msg_type,sub_type,status,direction,'
            'sender_id,sender_display_name,content_text,content_json,'
            'packed_info_sha256,record_sha256,raw_record_json,media_refs_json,'
            'provenance,visibility,has_revisions,'
            'first_commit_seq,last_content_or_presence_commit_seq) '
            'VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0,?,?)',
            (uid, account, talker, server_key, ident['shard'],
             ident.get('table'), int(ident['local_rowid']),
             int(record.get('create_time', 0)), int(record.get('sort_seq', 0)),
             int(record.get('local_type', record.get('msg_type', 0)) or 0),
             int(record.get('sub_type', 0) or 0), int(record.get('status', 0) or 0),
             record.get('direction'), sender_id, sender_display,
             text_body or '',
             canonical(structured),
             record.get('packed_info_sha256'), record.get('record_sha256'),
             raw_block,
             refs, provenance, 'visible',
             tx.commit_seq, tx.commit_seq))
        self.db.execute(
            'INSERT INTO identity_map(account,talker,server_id,shard,shard_table,'
            'local_rowid,msg_uid) VALUES(?,?,?,?,?,?,?)',
            (account, talker, server_key, ident['shard'], ident.get('table'),
             int(ident['local_rowid']), uid))
        tx.mutation('messages', str(uid), 'insert',
                    self._row_after_image(uid),
                    {'run_id': run['run_id'], 'identity_bind': identity_bind})
        receipt.counts['rows_inserted'] += 1
        if provenance == 'late':
            receipt.counts['late'] += 1
        if run.get('legacy', {}).get('provenance_override'):
            receipt.counts['source_upgrade'] += 0  # legacy inserts counted separately
            receipt.counts['legacy_rows'] = receipt.counts.get('legacy_rows', 0) + 1
        return uid

    def _update_message(self, run, op, uid, tx, receipt, staged, c0,
                        bound_server, identity_bind, claimed=False):
        record = op['record']
        row = self.db.execute('SELECT * FROM messages WHERE msg_uid=?', (uid,)).fetchone()
        # C0 covers ALL content updates, not only missing marks
        if row['last_content_or_presence_commit_seq'] > c0:
            receipt.counts['conflicts'] += 1
            receipt.conflicts.append({
                'msg_uid': uid, 'reason': 'row_committed_after_c0',
                'row_commit_seq': row['last_content_or_presence_commit_seq'],
                'run_c0': c0, 'identity': op['identity'],
            })
            tx.mutation('messages', str(uid), 'conflict',
                        {'reason': 'row_committed_after_c0',
                         'row_commit_seq': row['last_content_or_presence_commit_seq'],
                         'run_c0': c0},
                        {'run_id': run['run_id']})
            return
        new_fp = record.get('record_sha256')
        refs = self._refs_json(record)
        refs_changed = refs != row['media_refs_json']
        if new_fp == row['record_sha256'] and not refs_changed:
            # Unchanged re-observation: still advance the PRESENCE sequence —
            # a run started BEFORE this observation must not be able to mark
            # the row missing afterwards (dual-condition missing gate).
            self.db.execute(
                'UPDATE messages SET last_content_or_presence_commit_seq=? '
                'WHERE msg_uid=?', (tx.commit_seq, uid))
            tx.mutation('messages', str(uid), 'presence',
                        self._row_after_image(uid),
                        {'run_id': run['run_id']})
            receipt.counts['no_change'] += 1
            if identity_bind:
                tx.mutation('identity_map', str(uid), 'identity_bind_reobserved',
                            {'identity': op['identity']}, {'run_id': run['run_id']})
            return
        if not claimed:
            # An UNCLAIMED run's C0 was minted after its source view was
            # read: nothing proves this view postdates the stored content.
            # Refuse the overwrite conservatively (the row keeps its
            # accepted state); a legitimate re-observation must re-claim.
            receipt.counts['conflicts'] += 1
            receipt.conflicts.append({
                'msg_uid': uid, 'reason': 'update_requires_claim',
                'identity': op['identity'], 'run_id': run['run_id'],
            })
            tx.mutation('messages', str(uid), 'conflict',
                        {'reason': 'update_requires_claim',
                         'run_id': run['run_id']})
            return
        # fingerprint change → revision with distinct change_kind; the payload
        # carries the COMPLETE previous and new records (full original columns
        # included) so history is replayable without partial fingerprints
        legacy = run.get('legacy') or {}
        is_backfill = bool(legacy.get('provenance_override')) or \
            run.get('kind') == 'backfill'
        if row['provenance'] == 'legacy_visibility_projected' or is_backfill:
            change_kind = 'source_upgrade'
        else:
            change_kind = 'source_edit'
        previous_image = dict(row)
        seq = self.db.execute(
            'SELECT COALESCE(MAX(seq),0)+1 FROM message_revisions WHERE msg_uid=?',
            (uid,)).fetchone()[0]
        provenance = change_kind
        # local coordinates follow the CURRENT source location (迁片: a row
        # moves message_0.db#1 → message_1.db#9 with the same server identity
        # — that is an update, never a missing candidate). If another row
        # already occupies the new slot, history is preserved and the
        # collision is recorded as an identity_bind event.
        ident = op['identity']
        new_shard, new_table = ident['shard'], ident.get('table')
        new_rowid = int(ident['local_rowid'])
        coords_moved = (new_shard, new_table, new_rowid) != \
            (row['shard'], row['shard_table'], row['local_rowid'])
        if coords_moved:
            for other in self.store.local_identity_conflict(
                    run['account'], run['talker'], new_shard, new_table,
                    new_rowid):
                if other['msg_uid'] != uid:
                    receipt.counts['identity_binds'] += 1
                    tx.mutation('identity_map',
                                f'{new_shard}:{new_table}:{new_rowid}',
                                'identity_bind',
                                {'previous': other,
                                 'new_identity': identity_key(
                                     ident.get('server_id'), new_shard,
                                     new_table, new_rowid)},
                                {'run_id': run['run_id']})
        update_sender_id, update_sender_display, update_sender_unknown = \
            consume_sender(record)
        if update_sender_unknown:
            receipt.counts['sender_unknown'] += 1
        update_text, _update_quote, update_structured, update_audits = \
            derive_content_view(record.get('message_content'))
        for audit in update_audits:
            receipt.counts[audit['kind']] += 1
        update_raw_block = _raw_block_of(record)
        self.db.execute(
            'UPDATE messages SET create_time=?, sort_seq=?, msg_type=?, sub_type=?, '
            'status=?, direction=?, sender_id=?, sender_display_name=?, content_text=?, '
            'content_json=?, packed_info_sha256=?, record_sha256=?, '
            'raw_record_json=?, media_refs_json=?, '
            'shard=?, shard_table=?, local_rowid=?, '
            'provenance=?, has_revisions=1, last_content_or_presence_commit_seq=? '
            'WHERE msg_uid=?',
            (int(record.get('create_time', 0)), int(record.get('sort_seq', 0)),
             int(record.get('local_type', record.get('msg_type', 0)) or 0),
             int(record.get('sub_type', 0) or 0), int(record.get('status', 0) or 0),
             record.get('direction'), update_sender_id, update_sender_display,
             update_text or '', canonical(update_structured),
             record.get('packed_info_sha256'), new_fp,
             update_raw_block, refs,
             new_shard, new_table, new_rowid, provenance,
             tx.commit_seq, uid))
        if coords_moved:
            self.db.execute(
                'UPDATE identity_map SET shard=?, shard_table=?, local_rowid=? '
                'WHERE msg_uid=? AND server_id IS NOT NULL',
                (new_shard, new_table, new_rowid, uid))
        self.db.execute(
            'INSERT INTO message_revisions(msg_uid,seq,change_kind,observed_at,'
            'payload_json,commit_seq) VALUES(?,?,?,?,?,?)',
            (uid, seq, change_kind, now_ms(),
             canonical({'previous': previous_image,
                        'new': self._row_after_image(uid)}),
             tx.commit_seq))
        tx.mutation('message_revisions', f'{uid}:{seq}', change_kind,
                    self._full_row('message_revisions', 'msg_uid=? AND seq=?',
                                   (uid, seq)),
                    {'run_id': run['run_id']})
        tx.mutation('messages', str(uid), change_kind,
                    self._row_after_image(uid),
                    {'run_id': run['run_id'],
                     'previous_record_sha256': row['record_sha256']})
        receipt.counts['rows_updated'] += 1
        receipt.counts[change_kind] += 1

    # ── missing candidates: dual condition + full re-verification ────
    def _recheck_keys(self, run, page_reader):
        """Identity keys of the fresh re-verification view: the inline dict
        plus, for large sessions, the streamed identity pages. Cached per
        apply — the pages are read at most once per run."""
        if self._recheck_cache is not None:
            return self._recheck_cache
        recheck = run.get('recheck')
        keys = None
        if isinstance(recheck, dict):
            keys = set(recheck.get('identities') or {})
            for page in recheck.get('identity_pages') or []:
                if page_reader is None:
                    raise ReconcileError(
                        'run_invalid',
                        'recheck identity pages require a page reader')
                for entry in page_reader(page['sha256']):
                    if not isinstance(entry, dict) or \
                            not isinstance(entry.get('k'), str):
                        raise ReconcileError(
                            'run_invalid',
                            'recheck identity page entry malformed')
                    keys.add(entry['k'])
        self._recheck_cache = keys
        return keys

    def _apply_missing(self, run, op, tx, receipt, c0, page_reader=None,
                       claimed=False):
        account, talker = run['account'], run['talker']
        ident = op['identity']
        server_id = ident.get('server_id')
        key = identity_key(server_id, ident.get('shard'), ident.get('table'),
                           int(ident.get('local_rowid', 0) or 0))
        if not claimed:
            # absence is only provable from a view whose run PREDATES it;
            # an unclaimed recheck mints a fresh C0 after reading the
            # source and cannot causally order itself against reappearing
            # rows — refuse, never guess
            receipt.counts['missing_refused_unclaimed'] += 1
            receipt.warnings.append(
                f'missing refused (unclaimed run): {key}')
            return
        recheck = run.get('recheck')
        # Never publish missing without a COMPLETE fresh view across all shards
        if not isinstance(recheck, dict) or not recheck.get('complete'):
            receipt.counts['missing_skipped_incomplete'] += 1
            receipt.warnings.append(
                f'missing skipped (no complete recheck): {key}')
            return
        identities = self._recheck_keys(run, page_reader)
        if key in identities:
            # the fresh view still sees the row → not missing; record nothing
            receipt.counts['missing_skipped_incomplete'] += 1
            receipt.warnings.append(f'missing candidate still present in recheck: {key}')
            return
        uid, _, _ = self.store.resolve_identity(
            account, talker, None if server_id is None else str(server_id),
            ident.get('shard'), ident.get('table'),
            int(ident.get('local_rowid', 0) or 0))
        if uid is None:
            # never archived → nothing to mark; absence of an unknown row is
            # not a gap in the archive
            return
        row = self.db.execute('SELECT first_commit_seq, '
                              'last_content_or_presence_commit_seq FROM messages '
                              'WHERE msg_uid=?', (uid,)).fetchone()
        if row['first_commit_seq'] > c0 or \
                row['last_content_or_presence_commit_seq'] > c0:
            receipt.counts['conflicts'] += 1
            receipt.conflicts.append({
                'msg_uid': uid, 'reason': 'missing_candidate_committed_after_c0',
                'first_commit_seq': row['first_commit_seq'],
                'last_commit_seq': row['last_content_or_presence_commit_seq'],
                'run_c0': c0, 'identity': ident,
            })
            tx.mutation('messages', str(uid), 'conflict',
                        {'reason': 'missing_candidate_committed_after_c0',
                         'run_c0': c0}, {'run_id': run['run_id']})
            return
        # soft missing: record the observation, never delete history, never
        # hide a currently visible row
        self.db.execute(
            'INSERT INTO missing_observations(msg_uid,account,talker,identity_json,'
            'kind,soft,run_id,first_observed_commit_seq,detail_json) '
            'VALUES(?,?,?,?,?,1,?,?,?)',
            (uid, account, talker, canonical(ident), op.get('kind', 'row_missing'),
             run['run_id'], tx.commit_seq, canonical(op.get('detail') or {})))
        tx.mutation('missing_observations', key, 'missing_observed',
                    self._full_row('missing_observations',
                                   'msg_uid=? AND run_id=? AND kind=?',
                                   (uid, run['run_id'],
                                    op.get('kind', 'row_missing'))))
        receipt.counts['missing_published'] += 1

    # ── legacy migration block (v2 → v3) ────────────────────────────
    def _apply_legacy(self, run, tx, receipt):
        """Apply the legacy migration block inside the same FULL transaction:
        historical v2 revisions become message_revisions rows (change_kind
        legacy_revision), v2 missing_in_source marks become soft
        missing_observations (kind legacy_v2_missing), and the honesty audit
        (untraceable v2 ignore config → unknown) lands in the receipt."""
        legacy = run.get('legacy')
        if not legacy:
            return
        account, talker = run['account'], run['talker']
        for rev in legacy.get('revisions') or []:
            ident = rev['identity']
            uid, _, _ = self.store.resolve_identity(
                account, talker,
                None if ident.get('server_id') is None else str(ident['server_id']),
                ident.get('shard'), ident.get('table'),
                int(ident.get('local_rowid', 0) or 0))
            if uid is None:
                receipt.warnings.append(
                    'legacy revision without archived row: skipped '
                    '(names only) ' + canonical(
                        {k: ident.get(k) for k in
                         ('server_id', 'shard', 'table', 'local_rowid')}))
                continue
            seq = self.db.execute(
                'SELECT COALESCE(MAX(seq),0)+1 FROM message_revisions '
                'WHERE msg_uid=?', (uid,)).fetchone()[0]
            self.db.execute(
                'INSERT INTO message_revisions(msg_uid,seq,change_kind,observed_at,'
                'payload_json,commit_seq) VALUES(?,?,?,?,?,?)',
                (uid, seq, 'legacy_revision',
                 int(rev.get('revised_at') or 0) or now_ms(),
                 canonical({'legacy_payload': rev.get('payload'),
                            'payload_hash': rev.get('payload_hash')}),
                 tx.commit_seq))
            self.db.execute('UPDATE messages SET has_revisions=1 WHERE msg_uid=?',
                            (uid,))
            tx.mutation('message_revisions', f'{uid}:{seq}', 'legacy_revision',
                        self._full_row('message_revisions',
                                       'msg_uid=? AND seq=?', (uid, seq)),
                        {'run_id': run['run_id']})
            receipt.counts['legacy_revisions'] += 1
        for miss in legacy.get('missing') or []:
            ident = miss['identity']
            uid, _, _ = self.store.resolve_identity(
                account, talker,
                None if ident.get('server_id') is None else str(ident['server_id']),
                ident.get('shard'), ident.get('table'),
                int(ident.get('local_rowid', 0) or 0))
            if uid is None:
                receipt.warnings.append(
                    'legacy missing mark without archived row: skipped '
                    '(names only) ' + canonical(
                        {k: ident.get(k) for k in
                         ('server_id', 'shard', 'table', 'local_rowid')}))
                continue
            self.db.execute(
                'INSERT INTO missing_observations(msg_uid,account,talker,'
                'identity_json,kind,soft,run_id,first_observed_commit_seq,'
                'detail_json) VALUES(?,?,?,?,?,1,?,?,?)',
                (uid, account, talker, canonical(ident), 'legacy_v2_missing',
                 run['run_id'], tx.commit_seq,
                 canonical({'legacy': 'v2 missing_in_source'})))
            tx.mutation('missing_observations',
                        identity_key(ident.get('server_id'), ident.get('shard'),
                                     ident.get('table'),
                                     int(ident.get('local_rowid', 0) or 0)),
                        'legacy_missing_observed',
                        self._full_row(
                            'missing_observations',
                            'msg_uid=? AND run_id=? AND kind=?',
                            (uid, run['run_id'], 'legacy_v2_missing')),
                        {'run_id': run['run_id']})
            receipt.counts['legacy_missing'] += 1
        audit = legacy.get('audit') or {}
        if audit.get('unknown'):
            receipt.counts['legacy_audit_unknown'] += int(audit['unknown'])
        for note in (audit.get('notes') or [])[:20]:
            receipt.warnings.append(f'legacy audit: {note}')

    # ── media ledger ─────────────────────────────────────────────────
    def _materialize_observation(self, run, page_reader):
        """The observation summary as one dict: inline when supplied, else
        rebuilt from the run's identity pages + header. The full map is O(rows)
        by nature (it is what gets stored as the next diff base); pages only
        bound how it TRAVELS, streamed, never in a single frame."""
        inline = run.get('observation')
        if inline is not None:
            if isinstance(inline, dict) and \
                    inline.get('snapshot_created_at') is None and \
                    run.get('snapshot_created_at') is not None:
                inline = dict(inline)
                inline['snapshot_created_at'] = run.get('snapshot_created_at')
            return inline
        pages = run.get('observation_pages')
        if not pages:
            return None
        if page_reader is None:
            raise ReconcileError('run_invalid',
                                 'observation pages require a page reader')
        header = run.get('observation_header') or {}
        identities = {}
        for page in pages:
            for entry in page_reader(page['sha256']):
                if not isinstance(entry, dict) or \
                        not isinstance(entry.get('k'), str):
                    raise ReconcileError('run_invalid',
                                         'observation page entry malformed')
                identities[entry['k']] = {'fp': entry.get('fp'),
                                          'refs': entry.get('refs') or []}
        return {'version': header.get('version') or 2,
                'shards': header.get('shards') or {},
                'identities': identities,
                'enumeration_complete': bool(header.get('enumeration_complete')),
                'unknown': header.get('unknown') or {},
                # the source-view clock of the observation that becomes the
                # next stale-view watermark (absent → no watermark stored)
                'snapshot_created_at': header.get('snapshot_created_at')
                if header.get('snapshot_created_at') is not None
                else run.get('snapshot_created_at')}

    def _auto_resolve_needs(self, run, refs_seen, tx, receipt):
        """Fulfilment DERIVED from what was applied: a ref re-observed
        present whose bytes were staged in THIS run closes the open needs for
        that ref_key (scoped to this account/talker). No client-side
        'fulfilled' claim list exists — the server saw the bytes."""
        if not refs_seen:
            return
        for ref_key in refs_seen:
            rows = self.db.execute(
                'SELECT mn.need_id FROM media_needs mn JOIN messages m '
                'ON m.msg_uid=mn.msg_uid WHERE m.account=? AND m.talker=? '
                'AND mn.ref_key=? AND mn.state=?',
                (run['account'], run['talker'], ref_key, 'open')).fetchall()
            for row in rows:
                self.db.execute(
                    'UPDATE media_needs SET state=?, resolved_commit_seq=? '
                    'WHERE need_id=?',
                    ('fulfilled', tx.commit_seq, row['need_id']))
                tx.mutation('media_needs', f'{row["need_id"]}:{ref_key}',
                            'need_fulfilled_derived',
                            self._full_row('media_needs', 'need_id=?',
                                           (row['need_id'],)),
                            {'ref_key': ref_key, 'run_id': run['run_id']})
                receipt.counts['media_needs_resolved'] += 1

    def _apply_media_needs(self, run, media, tx, receipt):
        for need in media.get('needs', []):
            ident = need.get('identity') or {}
            server_id = ident.get('server_id')
            uid, _, _ = self.store.resolve_identity(
                run['account'], run['talker'],
                None if server_id is None else str(server_id),
                ident.get('shard'), ident.get('table'),
                int(ident.get('local_rowid', -1)))
            state = need.get('state', 'open')
            if uid is None:
                # need for a row not yet archived: keep as an error gap, never silent
                receipt.counts['media_upload_errors'] += 1
                receipt.warnings.append(
                    f"media need without archived row: {need.get('ref_key')!r}")
                continue
            existing = self.db.execute(
                'SELECT need_id, state FROM media_needs WHERE msg_uid=? AND ref_key=?',
                (uid, need['ref_key'])).fetchone()
            if existing is None:
                self.db.execute(
                    'INSERT INTO media_needs(msg_uid,ref_key,ref_kind,state,'
                    'first_seen_run,last_error,opened_commit_seq,resolved_commit_seq) '
                    'VALUES(?,?,?,?,?,?,?,?)',
                    (uid, need['ref_key'], need.get('kind', 'unknown'), state,
                     run['run_id'], need.get('error'),
                     tx.commit_seq,
                     tx.commit_seq if state in ('fulfilled', 'vanished', 'error') else None))
                tx.mutation('media_needs', f'{uid}:{need["ref_key"]}', f'need_{state}',
                            self._full_row('media_needs',
                                           'msg_uid=? AND ref_key=?',
                                           (uid, need['ref_key'])),
                            {'ref_kind': need.get('kind'), 'run_id': run['run_id']})
            else:
                self.db.execute(
                    'UPDATE media_needs SET state=?, last_error=?, first_seen_run=?, '
                    'resolved_commit_seq=? WHERE need_id=?',
                    (state, need.get('error'), run['run_id'],
                     tx.commit_seq if state in ('fulfilled', 'vanished', 'error')
                     else None, existing['need_id']))
                tx.mutation('media_needs', f'{uid}:{need["ref_key"]}', f'need_{state}',
                            self._full_row('media_needs', 'need_id=?',
                                           (existing['need_id'],)),
                            {'previous_state': existing['state'],
                             'run_id': run['run_id']})
            if state == 'open':
                receipt.counts['media_needs_open'] += 1
            elif state == 'vanished':
                receipt.counts['media_vanished_errors'] += 1
            elif state == 'error':
                receipt.counts['media_upload_errors'] += 1

    def _apply_assets(self, media, staged, tx, receipt, run):
        """Register staged+verified media objects as committed assets."""
        for sha, upload in staged.items():
            existing = self.db.execute(
                'SELECT state FROM media_assets WHERE sha256=?', (sha,)).fetchone()
            if existing is not None:
                continue
            self.db.execute(
                'INSERT INTO media_assets(sha256,asset_id,kind,length,state,'
                'first_batch,created_commit_seq) VALUES(?,?,?,?,?,?,?)',
                (sha, sha, upload.get('kind', 'unknown'), int(upload['length']),
                 'stored', run.get('batch_id'), tx.commit_seq))
            tx.mutation('media_assets', sha, 'asset_stored',
                        self._full_row('media_assets', 'sha256=?', (sha,)),
                        {'batch_id': run.get('batch_id')})
            receipt.counts['media_assets_stored'] += 1

    @staticmethod
    def _refs_json(record):
        refs = record.get('media_refs') or []
        return canonical(refs)
