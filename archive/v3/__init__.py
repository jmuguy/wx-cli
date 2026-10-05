"""External archive v3 — NAS-side protocol stack and Mac bounded-queue client.

Implements task card S4 of docs/specs/external-archive-v3.md:
production/synthetic disk guard, forced-command SSH stdio capture protocol,
single-writer master with FULL transactions and row-level mutation log,
semantic collector, C0-protected reconcile, physically isolated query
projection, encrypted backup with restore drill, and v2 migration.

Production defaults are FAIL CLOSED. Nothing here touches the existing
wechat_archive.py pipeline, launchd jobs, or MCP.

Cross-language record contract: work/external-archive-implementation/nas-contract.md
(version 1). The collector consumes complete per-session records with media
references — never hash-only metadata.
"""

PROTOCOL_VERSION = 1
RECORD_CONTRACT = 'wx-archive.external-archive-records'
# v2 (2026-10-04): identity.shard is the DATABASE file (message_N.db), with
# identity.table (Msg_<namehash>) as a separate field — the same Msg table
# name exists in multiple message_N.db shards, so table-name-as-shard
# collides across databases. Records carry `raw` (every original source
# column, lossless) separate from the decoded text fields.
RECORD_CONTRACT_VERSION = 2
SCHEMA_VERSION = 4

__all__ = ['PROTOCOL_VERSION', 'RECORD_CONTRACT', 'RECORD_CONTRACT_VERSION',
           'SCHEMA_VERSION']
