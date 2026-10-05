//! `wx-cli archive-inspect`: offline, read-only inspection and COMPLETE
//! per-conversation export from published snapshot generations (S3).
//!
//! Everything here runs against the STATIC encrypted copies produced by
//! `archive-snapshot`, and only after the generation has been re-verified
//! item-by-item against its own manifest (relative path, regular-file type,
//! byte length, sha256, and an exact tree walk with no extra or missing
//! files). Databases are opened through `wx_db::snapshot::open_snapshot_copy`
//! (READONLY, keyed, query_only). The live WeChat source is never touched.
//!
//! The export implements cross-language record contract
//! `wx-archive.external-archive-records` **version 2** (aligned with the NAS
//! consumer's `RECORD_CONTRACT_VERSION = 2`), documented for the NAS side in
//! `work/external-archive-implementation/source-contract-update.md`:
//!
//! ```text
//! <export_dir>/export.json    # manifest: contract, version, snapshot pin,
//!                             # session shards, enumeration completeness
//! <export_dir>/records.jsonl  # one COMPLETE record per line
//! <export_dir>/media/         # optional staged media (0600; images are
//!                             # DECODED Mac-side into usable bytes — the
//!                             # raw encrypted .dat is never claimed as the
//!                             # delivered media)
//! ```
//!
//! Fidelity rules this implementation is held to:
//!
//! * **Lossless source records.** Every record carries a `raw` object with
//!   EVERY original source column, losslessly encoded (integers/floats as
//!   numbers, valid-UTF-8 TEXT as strings, BLOBs and invalid-UTF-8 TEXT as
//!   base64 objects). `message_content` is the decoded searchable text
//!   (verified zstd rule: `WCDB_CT == 4` or zstd magic); when the raw bytes
//!   cannot round-trip through that text they are additionally preserved in
//!   `message_content_raw_b64` with storage type and decode status. zstd
//!   decompression FAILURE is a hard export failure — never fake success.
//! * **Real shard identity (contract v2).** `identity.shard` is the DATABASE
//!   file (`message_N.db`) and `identity.table` (`Msg_<namehash>`) is a
//!   separate field, so the same table living in several shards after WeChat
//!   shard migration never collides on rowid. `server_id` is serialized as
//!   an exact decimal string (values exceed 2^53). Records are ordered by
//!   `(shard, local_rowid)` ascending.
//! * **Manifest-verified enumeration.** The `complete` boolean alone proves
//!   nothing: every listed database is checked (path shape, regular file,
//!   bytes, sha256) and the tree is walked exactly — a missing shard fails
//!   the export. Only `message/message_<digits>.db` files are message
//!   shards, and each must actually have the `Name2Id` schema.
//!   `message_resource`/FTS/other databases are verified and classified as
//!   non-message, never mixed in. SQL identifiers are `""`-escaped; read
//!   errors always propagate.
//! * **Media (local capability, honest gaps).** Image `.dat` files are
//!   DECODED on the Mac (XOR key auto-detection from the talker's attach
//!   tree, or a v2 AES key read from a private 0600 key file — never from
//!   argv, where `ps` would see it) and the decoded usable image bytes are
//!   staged — the encrypted `.dat` itself is never marked as delivered
//!   media. Voice blobs come from the snapshot media dbs; video/app-file
//!   bytes from the verified attach layouts via the snapshot hardlink db.
//!   Only PROVEN local absence is a soft gap (`present=false` + reason).
//!   Decode failures (unknown format, missing key, AES failure), zero-byte
//!   output, container-export errors, unreadable intermediate attach
//!   directories (absence cannot be proven through them), database,
//!   permission, and unknown errors are HARD failures that abort the
//!   export before `export.json` is published. `media.complete` in
//!   export.json is true only when an attach root was provided and every
//!   reference resolved to staged bytes.
//! * **Bounded work sets (fail closed).** Every input that could otherwise
//!   be unbounded carries an explicit, configurable cap in this path: media
//!   files are size-checked BEFORE reading and re-checked mid-read against
//!   growth (`media_input_over_limit`), voice blobs are length-probed in
//!   SQL before materialization, raw content cells are capped pre-decode
//!   (`content_over_limit`), zstd expansion streams through a take() bound
//!   rather than `decode_all` (`decoded_over_limit`), and the WHOLE raw row
//!   — every TEXT/BLOB cell, known and unknown columns summed — is capped
//!   by `--max-row-bytes` enforced BEFORE any payload of the row is read:
//!   a per-page `octet_length` probe sums each cell's byte length from the
//!   b-tree record header alone (SQLite OP_Column OPFLAG_BYTELENARG — no
//!   payload, no overflow chain, ever materialized) and refuses
//!   over-budget rows up front; the payload read that follows is bounded
//!   to the probed rowid window and re-asserted by the connection's
//!   `SQLITE_LIMIT_LENGTH` (per-cell SQLITE_TOOBIG) and a running Rust row
//!   total, with a derived cap on the serialized JSON line
//!   (`row_json_over_limit`). All three refusal layers carry the
//!   machine-readable `row_over_limit` token. Rows are built and written
//!   ONE AT A TIME — no page of full payloads is ever buffered. Over-limit
//!   or growing inputs abort the export and never publish an export.json —
//!   memory is never consumed first and rejected afterwards.
//! * **Image variant honesty.** Image refs carry `variant`
//!   (original/thumbnail/derivative/unknown, media-v1 naming tiers) plus
//!   the evidence that produced the label (layout + file name, dir1/dir2
//!   for hardlink sources). A staged thumbnail is labeled as exactly that
//!   with `original_present: false`, and media completeness stays false
//!   because the original was not captured — a thumbnail never
//!   impersonates the original.
use std::collections::{BTreeMap, HashMap};
use std::io::{Read, Write};
use std::os::unix::fs::{OpenOptionsExt, PermissionsExt};
use std::path::{Component, Path, PathBuf};

use clap::Args;
use rusqlite::limits::Limit;
use rusqlite::types::ValueRef;
use rusqlite::Connection;
use serde::{Deserialize, Serialize};
use serde_json::{json, Map, Value};
use wx_db::snapshot::{atomic_private_write, ensure_private_dir, open_snapshot_copy, SnapshotKeys};
use wx_db::{
    decode_content_lossless_bounded, decode_packed_info, split_local_type, MSG_TYPE_APP,
    MSG_TYPE_IMAGE, MSG_TYPE_VOICE, MSG_TYPE_VIDEO,
};

use crate::cmd::archive_snapshot::load_keys;
use crate::cmd::export_media::export_image_bytes;
use wx_media::{
    decrypt_dat, detect_xor_key, extract_md5_from_packed_info, extract_voice_with_conn,
    query_hardlink_with_conn, resolve_image_by_md5, DatDecryptOptions, MediaError,
};

/// Contract identity this producer implements (NAS: archive/v3/__init__.py).
const CONTRACT: &str = "wx-archive.external-archive-records";
/// v2 (2026-10-04): identity.shard = database file, identity.table separate,
/// `raw` carries every original source column losslessly, server_id is an
/// exact decimal string.
const CONTRACT_VERSION: u64 = 2;

/// Column positions in the per-row SELECT (rowid first). These constants are
/// the single source of truth for `build_record` — indexes are never written
/// inline.
const IDX_ROWID: usize = 0;
const IDX_SORT_SEQ: usize = 1;
const IDX_SERVER_ID: usize = 2;
const IDX_LOCAL_TYPE: usize = 3;
const IDX_CREATE_TIME: usize = 4;
const IDX_STATUS: usize = 5;
const IDX_MESSAGE_CONTENT: usize = 6;
const IDX_PACKED_INFO: usize = 7;
/// First optional known column (`WCDB_CT_message_content` when present).
const IDX_FIRST_OPTIONAL: usize = 8;

/// Columns every real WeChat 4.x `Msg_*` table must have for a COMPLETE
/// export. A message shard missing any of these is a hard failure, not a
/// degraded export.
const REQUIRED_MSG_COLUMNS: &[&str] = &[
    "sort_seq",
    "server_id",
    "local_type",
    "create_time",
    "message_content",
    "packed_info_data",
    "status",
];

/// Optional known columns preserved verbatim when present (also inside `raw`).
const OPTIONAL_KNOWN_COLUMNS: &[&str] = &[
    "WCDB_CT_message_content",
    "compress_content",
    "real_sender_id",
];

#[derive(Args, Debug)]
pub struct ArchiveInspectArgs {
    /// Published snapshot generation directory (the `--out` of
    /// `archive-snapshot`; must contain manifest.json)
    #[arg(long)]
    pub snapshots: PathBuf,

    /// 0600 key file (same format as archive-snapshot)
    #[arg(long)]
    pub key_file: PathBuf,

    /// What to do: `list` conversations, or `export` one talker completely
    #[arg(long, default_value = "list")]
    pub action: String,

    /// Talker to export (username or chatroom id) — required for export
    #[arg(long)]
    pub talker: Option<String>,

    /// Export directory to create (must not exist)
    #[arg(long)]
    pub out: PathBuf,

    /// Account wxid (owner) — required for export, pins archive identity
    #[arg(long)]
    pub account: Option<String>,

    /// v3 archive_id this export belongs to (must match the NAS side)
    #[arg(long)]
    pub archive_id: Option<String>,

    /// WeChat attach root (media source tree, outside the snapshot).
    /// Omit to export records without attach-backed media staging; the
    /// manifest then honestly reports media completeness = false.
    #[arg(long)]
    pub attach_root: Option<PathBuf>,

    /// Optional 0600 JSON key file for decoding image .dat files:
    /// {"v2_aes_key":"<32 hex chars>"} — synthetic keys only in tests.
    /// Secrets never appear on argv (visible to `ps`) or in error text;
    /// used ONLY Mac-side during export.
    #[arg(long)]
    pub dat_key_file: Option<PathBuf>,

    /// Rows per keyset page
    #[arg(long, default_value_t = 500)]
    pub page_size: usize,

    /// Upper bound for a single staged media file
    #[arg(long, default_value_t = 256 * 1024 * 1024)]
    pub max_media_bytes: u64,

    /// Cap on ONE raw content cell (message_content / packed_info_data /
    /// compress_content) enforced BEFORE any decode; over-limit rows fail
    /// closed with the machine-readable `content_over_limit` error. Bounds
    /// the per-row work set so no single row can drive unbounded memory use.
    #[arg(long, default_value_t = 64 * 1024 * 1024)]
    pub max_content_bytes: usize,

    /// Cap on the TOTAL byte length of ONE raw source row (sum of every
    /// TEXT/BLOB cell, known AND unknown columns), enforced BEFORE the row
    /// is materialized: a per-page `octet_length` probe sums each cell's
    /// byte length from the b-tree record header alone (no payload is ever
    /// read for the check) and over-budget rows fail closed with the
    /// machine-readable `row_over_limit` error — never silently skipped,
    /// never silently truncated. The payload read that follows re-asserts
    /// the budget through SQLite SQLITE_LIMIT_LENGTH (per-cell
    /// SQLITE_TOOBIG) and a running row total (defense in depth). The cap
    /// must be at least --max-content-bytes, which a legal content cell
    /// alone could otherwise always exceed. Also bounds the serialized
    /// JSON record line (derived cap, `row_json_over_limit`).
    #[arg(long, default_value_t = 256 * 1024 * 1024)]
    pub max_row_bytes: usize,

    /// Cap on DECOMPRESSED message content (zstd expansion bound): decodes
    /// stream through at most this many bytes and refuse beyond, failing
    /// closed with the machine-readable `decoded_over_limit` error instead
    /// of expanding a compressed bomb into memory first.
    #[arg(long, default_value_t = 64 * 1024 * 1024)]
    pub max_decoded_bytes: usize,
}

// ── small helpers ───────────────────────────────────────────────────────

/// Quote an SQL identifier (`"` doubled inside), for table/column names that
/// come from `sqlite_master` / `table_info` and must be interpolated.
fn quote_ident(name: &str) -> String {
    format!("\"{}\"", name.replace('"', "\"\""))
}

fn sha256_hex(bytes: &[u8]) -> String {
    use sha2::{Digest, Sha256};
    let mut hasher = Sha256::new();
    hasher.update(bytes);
    hex::encode(hasher.finalize())
}

fn sha256_file(path: &Path) -> Result<String, String> {
    use sha2::{Digest, Sha256};
    let mut file = std::fs::File::open(path).map_err(|e| format!("{}: {e}", path.display()))?;
    let mut hasher = Sha256::new();
    let mut buf = [0u8; 64 * 1024];
    loop {
        let n = file.read(&mut buf).map_err(|e| e.to_string())?;
        if n == 0 {
            break;
        }
        hasher.update(&buf[..n]);
    }
    Ok(hex::encode(hasher.finalize()))
}

/// Canonical JSON matching Python
/// `json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(',', ':'))`
/// for the value shapes this contract produces. serde_json's Map is a
/// BTreeMap without the preserve_order feature, so key order matches
/// sort_keys; its output is compact and never ASCII-escaped.
fn canonical_json(value: &Value) -> String {
    serde_json::to_string(value).unwrap_or_default()
}

fn base64_encode(data: &[u8]) -> String {
    // Tiny standalone base64 (standard alphabet, padded) — avoids adding a
    // dependency for a handful of preserved-blob fields.
    const TABLE: &[u8; 64] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
    let mut out = String::with_capacity(data.len().div_ceil(3) * 4);
    for chunk in data.chunks(3) {
        let b0 = chunk[0] as u32;
        let b1 = *chunk.get(1).unwrap_or(&0) as u32;
        let b2 = *chunk.get(2).unwrap_or(&0) as u32;
        let n = (b0 << 16) | (b1 << 8) | b2;
        out.push(TABLE[(n >> 18) as usize & 63] as char);
        out.push(TABLE[(n >> 12) as usize & 63] as char);
        out.push(if chunk.len() > 1 {
            TABLE[(n >> 6) as usize & 63] as char
        } else {
            '='
        });
        out.push(if chunk.len() > 2 {
            TABLE[n as usize & 63] as char
        } else {
            '='
        });
    }
    out
}

/// Lossless JSON encoding of one raw column value for the record's `raw`
/// object: SQL storage types map 1:1 (INTEGER/REAL as numbers, valid-UTF-8
/// TEXT as strings); BLOBs and invalid-UTF-8 TEXT are wrapped in base64
/// objects so no byte is ever lost.
///
/// REAL edge classes are encoded so the CROSS-LANGUAGE canonical
/// fingerprint stays identical (Rust serde_json vs Python json.dumps):
/// finite floats whose shortest serde_json form uses exponent notation
/// (1e300, 1e-7) are tagged `{"real":"<shortest>"}` because Python formats
/// them differently ("1e+300") — a plain number would silently change the
/// record_sha256 each side computes. Non-finite REALs get the same tagged
/// treatment (SQLite stores NaN as NULL, but refuse to guess if one ever
/// arrives). Plain decimal floats and integers round-trip identically in
/// both languages (locked by the python3 cross-check in
/// tests/archive_inspect_cli.rs).
fn raw_column_value(value: Option<&ValueRefOwned>) -> Value {
    match value {
        None | Some(ValueRefOwned::Null) => Value::Null,
        Some(ValueRefOwned::Integer(i)) => json!(i),
        Some(ValueRefOwned::Real(f)) => {
            if !f.is_finite() {
                let name = if f.is_nan() { "nan" } else if *f > 0.0 { "inf" } else { "-inf" };
                json!({"real": name})
            } else {
                let encoded = json!(f);
                let text = serde_json::to_string(&encoded).unwrap_or_default();
                if text.contains(['e', 'E']) {
                    // Exponent form: Python json.dumps formats it differently
                    // (1e+300 vs 1e300), which would change the canonical
                    // fingerprint each side computes — tag it as a string so
                    // both languages canonicalize identical bytes.
                    json!({"real": text})
                } else {
                    encoded
                }
            }
        }
        Some(ValueRefOwned::Text(t)) => match std::str::from_utf8(t) {
            Ok(s) => json!(s),
            Err(_) => json!({"b64": base64_encode(t), "utf8": false}),
        },
        Some(ValueRefOwned::Blob(b)) => json!({"b64": base64_encode(b)}),
    }
}

/// Probe a candidate media path WITHOUT swallowing errors: NotFound is the
/// only legitimate miss. PermissionDenied and any other error, plus an
/// existing NON-REGULAR entry (a directory or device where a media file
/// should be), are hard failures — absence may never be "proven" through an
/// unreadable or malformed path.
fn probe_file(path: &Path) -> Result<bool, String> {
    match std::fs::metadata(path) {
        Ok(meta) if meta.is_file() => Ok(true),
        Ok(_) => Err(format!(
            "media candidate {} exists but is not a regular file",
            path.display()
        )),
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => Ok(false),
        Err(e) => Err(format!("cannot access media candidate {}: {e}", path.display())),
    }
}

/// Read at most `limit` bytes of a media input, refusing BEFORE unbounded
/// memory use: the file is stat'ed first (an over-limit input is rejected
/// without reading a byte), then at most `limit + 1` bytes are read so a
/// file that GROWS between stat and read still cannot exhaust memory (the
/// +1 byte is the over-limit signal, mirroring the decode bound). Errors
/// carry the machine-readable token `media_input_over_limit`.
fn read_bounded(path: &Path, limit: u64) -> Result<Vec<u8>, String> {
    let meta = std::fs::metadata(path)
        .map_err(|e| format!("cannot stat media input {}: {e}", path.display()))?;
    if meta.len() > limit {
        return Err(format!(
            "media_input_over_limit: {} is {} bytes, exceeds limit {limit}",
            path.display(),
            meta.len()
        ));
    }
    let file = std::fs::File::open(path)
        .map_err(|e| format!("media open failed at {}: {e}", path.display()))?;
    let mut reader = std::io::BufReader::new(file).take(limit.saturating_add(1));
    // Capacity hint is capped at the accepted limit (and a modest floor),
    // never at a stat value larger than what we already agreed to read.
    let mut buf =
        Vec::with_capacity(meta.len().min(limit.saturating_add(1)).min(16 * 1024 * 1024) as usize);
    reader
        .read_to_end(&mut buf)
        .map_err(|e| format!("media read failed at {}: {e}", path.display()))?;
    if buf.len() as u64 > limit {
        return Err(format!(
            "media_input_over_limit: {} grew past limit {limit} during read",
            path.display()
        ));
    }
    Ok(buf)
}

// ── dat decrypt key file ────────────────────────────────────────────────

#[derive(Deserialize)]
struct DatKeyFile {

    v2_aes_key: Option<String>,
}

/// Load the v2 AES dat key from a private 0600 JSON key file
/// ({"v2_aes_key":"<32 hex>"}). Fail closed on any deviation; the key VALUE
/// never appears in an error message, on argv, or in logs. Synthetic keys
/// only in tests — real key material stays in the user's keystore.
fn load_dat_key(path: &Path) -> Result<[u8; 16], String> {
    let meta = std::fs::symlink_metadata(path)
        .map_err(|e| format!("dat key file {} unreadable: {e}", path.display()))?;
    if meta.file_type().is_symlink() {
        return Err("dat key file must be a regular file, not a symlink".into());
    }
    if !meta.is_file() {
        return Err("dat key file must be a regular file".into());
    }
    if PermissionsExt::mode(&meta.permissions()) & 0o077 != 0 {
        return Err("dat key file must have mode 0600 (no group/other access)".into());
    }
    let text =
        std::fs::read_to_string(path).map_err(|e| format!("dat key file read failed: {e}"))?;
    let parsed: DatKeyFile = serde_json::from_str(&text)
        .map_err(|e| format!("dat key file is not valid JSON: {e}"))?;
    let Some(hex_key) = parsed.v2_aes_key.as_deref() else {
        return Err("dat key file must contain v2_aes_key".into());
    };
    let bytes = hex::decode(hex_key.trim())
        .map_err(|_| "dat key file field v2_aes_key is not valid hex".to_string())?;
    if bytes.len() != 16 {
        return Err("dat key file field v2_aes_key must be exactly 16 bytes of hex".into());
    }
    let mut key = [0u8; 16];
    key.copy_from_slice(&bytes);
    Ok(key)
}

// ── generation manifest + item-by-item verification ─────────────────────

#[derive(Debug, Clone)]
struct GenDb {
    rel: String,
    sha256: Option<String>,
    bytes: Option<u64>,
}

#[derive(Debug)]
struct GenerationManifest {
    generation: String,
    started_unix_ms: u128,
    databases: Vec<GenDb>,
}

/// A fully verified generation: every manifest database checked (path shape,
/// regular file, byte length, sha256), the tree walked with no extra or
/// missing files, and databases classified by role.
#[derive(Debug)]
struct VerifiedGeneration {
    manifest: GenerationManifest,
    /// `message/message_<digits>.db` entries (message shards, file names).
    message_dbs: Vec<String>,
    /// media db relative paths (voice blobs).
    media_dbs: Vec<String>,
    /// hardlink db relative path when present.
    hardlink_db: Option<String>,
    /// Every other verified database (contact/session/fts/
    /// message_resource/...): pinned by sha256, never scanned for message
    /// tables.
    other_dbs: Vec<String>,
}

fn is_message_shard_name(file_name: &str) -> bool {
    let Some(stem) = file_name.strip_prefix("message_") else {
        return false;
    };
    let Some(digits) = stem.strip_suffix(".db") else {
        return false;
    };
    !digits.is_empty() && digits.bytes().all(|b| b.is_ascii_digit())
}

/// Load the generation manifest; only COMPLETE generations may be exported.
fn load_generation(snapshots: &Path) -> Result<GenerationManifest, String> {
    let manifest_path = snapshots.join("manifest.json");
    let text = std::fs::read_to_string(&manifest_path)
        .map_err(|e| format!("generation manifest missing at {}: {e}", manifest_path.display()))?;
    let value: Value = serde_json::from_str(&text)
        .map_err(|e| format!("generation manifest is not valid JSON: {e}"))?;
    let complete = value
        .get("complete")
        .and_then(Value::as_bool)
        .ok_or("generation manifest lacks a boolean `complete`")?;
    if !complete {
        return Err(
            "generation is incomplete (manifest.json reports complete=false); \
             only complete generations may be exported"
                .into(),
        );
    }
    let generation = value
        .get("generation")
        .and_then(Value::as_str)
        .unwrap_or_default()
        .to_string();
    let started_unix_ms = value
        .get("started_unix_ms")
        .and_then(Value::as_u64)
        .unwrap_or(0) as u128;
    let mut databases = Vec::new();
    for db in value
        .get("databases")
        .and_then(Value::as_array)
        .ok_or("generation manifest lacks databases[]")?
    {
        databases.push(GenDb {
            rel: db
                .get("db")
                .and_then(Value::as_str)
                .unwrap_or_default()
                .to_string(),
            sha256: db.get("sha256").and_then(Value::as_str).map(String::from),
            bytes: db.get("bytes").and_then(Value::as_u64),
        });
    }
    Ok(GenerationManifest {
        generation,
        started_unix_ms,
        databases,
    })
}

/// Verify the generation against its own manifest, item by item. The
/// `complete` boolean alone proves nothing: every listed database must exist
/// as a REGULAR file (symlinks rejected) at exactly the listed relative
/// path, with exactly the listed byte length and sha256, and the generation
/// tree must contain no file the manifest does not account for.
fn verify_generation(
    snapshots: &Path,
    manifest: &GenerationManifest,
) -> Result<VerifiedGeneration, String> {
    let mut seen_files: BTreeMap<String, ()> = BTreeMap::new();
    let mut message_dbs = Vec::new();
    let mut media_dbs = Vec::new();
    let mut hardlink_db = None;
    let mut other_dbs = Vec::new();

    let mut listed: BTreeMap<&str, ()> = BTreeMap::new();
    for db in &manifest.databases {
        // Relative path shape: plain relative components only.
        let rel_path = Path::new(&db.rel);
        if db.rel.is_empty()
            || rel_path.is_absolute()
            || !rel_path.components().all(|c| matches!(c, Component::Normal(_)))
            || db.rel == "manifest.json"
        {
            return Err(format!(
                "manifest lists database with unusable relative path {:?}",
                db.rel
            ));
        }
        // A duplicate manifest entry would verify twice and enumerate the
        // same shard twice — duplicating capture. Reject it outright.
        if listed.insert(db.rel.as_str(), ()).is_some() {
            return Err(format!(
                "manifest lists database {} more than once; duplicate entries cannot verify as a complete generation",
                db.rel
            ));
        }
        let full = snapshots.join(rel_path);
        let meta = std::fs::symlink_metadata(&full).map_err(|_| {
            format!(
                "manifest database {} is missing from the generation ({} bytes expected); \
                 a missing shard can never export as complete",
                db.rel,
                db.bytes.unwrap_or(0)
            )
        })?;
        if meta.file_type().is_symlink() {
            return Err(format!(
                "manifest database {} is a symlink; generation files must be regular",
                db.rel
            ));
        }
        if !meta.is_file() {
            return Err(format!(
                "manifest database {} is not a regular file",
                db.rel
            ));
        }
        let expected_bytes = db.bytes.ok_or_else(|| {
            format!("manifest database {} lacks a byte length; cannot verify", db.rel)
        })?;
        if meta.len() != expected_bytes {
            return Err(format!(
                "manifest database {} length mismatch: manifest says {} bytes, file has {}",
                db.rel,
                expected_bytes,
                meta.len()
            ));
        }
        let expected_sha = db.sha256.as_deref().ok_or_else(|| {
            format!("manifest database {} lacks a sha256; cannot verify", db.rel)
        })?;
        let actual_sha = sha256_file(&full)?;
        if !expected_sha.eq_ignore_ascii_case(&actual_sha) {
            return Err(format!(
                "manifest database {} sha256 mismatch: manifest pins {}, file hashes to {}",
                db.rel, expected_sha, actual_sha
            ));
        }
        seen_files.insert(db.rel.clone(), ());

        // Classify by role. Only message/message_<digits>.db are message
        // shards; message_resource/FTS/contact/session are NOT and must never
        // be mixed into message enumeration.
        let top = rel_path
            .components()
            .next()
            .and_then(|c| c.as_os_str().to_str())
            .unwrap_or_default()
            .to_string();
        let file_name = rel_path
            .file_name()
            .map(|n| n.to_string_lossy().to_string())
            .unwrap_or_default();
        if top == "message" && is_message_shard_name(&file_name) {
            message_dbs.push(file_name);
        } else if top == "media" && file_name.starts_with("media") && file_name.ends_with(".db") {
            media_dbs.push(db.rel.clone());
        } else if db.rel == "hardlink/hardlink.db" {
            hardlink_db = Some(db.rel.clone());
        } else {
            other_dbs.push(db.rel.clone());
        }
    }

    // Exact tree walk: every regular file under the generation must be
    // manifest.json or a listed database; every listed database must be
    // present in the walk.
    let mut walked: Vec<String> = Vec::new();
    walk_generation_tree(snapshots, snapshots, &mut walked)
        .map_err(|e| format!("generation tree walk failed: {e}"))?;
    for rel in &walked {
        if rel == "manifest.json" {
            continue;
        }
        if seen_files.remove(rel).is_none() {
            return Err(format!(
                "generation contains file {rel} that the manifest does not account for"
            ));
        }
    }
    if !seen_files.is_empty() {
        let missing = seen_files.keys().cloned().collect::<Vec<_>>().join(", ");
        return Err(format!("manifest databases missing from tree: {missing}"));
    }
    if message_dbs.is_empty() {
        return Err("no message_<N>.db shards found in the generation".into());
    }
    Ok(VerifiedGeneration {
        manifest: GenerationManifest {
            generation: manifest.generation.clone(),
            started_unix_ms: manifest.started_unix_ms,
            databases: manifest.databases.clone(),
        },
        message_dbs,
        media_dbs,
        hardlink_db,
        other_dbs,
    })
}

fn walk_generation_tree(root: &Path, dir: &Path, out: &mut Vec<String>) -> Result<(), String> {
    let entries = std::fs::read_dir(dir).map_err(|e| format!("read_dir {}: {e}", dir.display()))?;
    for entry in entries {
        let entry = entry.map_err(|e| format!("readdir {}: {e}", dir.display()))?;
        let meta =
            std::fs::symlink_metadata(entry.path()).map_err(|e| format!("lstat failed: {e}"))?;
        let rel = entry
            .path()
            .strip_prefix(root)
            .map_err(|e| e.to_string())?
            .to_string_lossy()
            .to_string();
        if meta.file_type().is_symlink() {
            return Err(format!("symlink inside generation tree: {rel}"));
        }
        if meta.is_dir() {
            walk_generation_tree(root, &entry.path(), out)?;
        } else if meta.is_file() {
            out.push(rel);
        } else {
            return Err(format!("non-regular file inside generation tree: {rel}"));
        }
    }
    Ok(())
}

// ── message shards + conversation discovery ─────────────────────────────

fn md5_hex(s: &str) -> String {
    format!("{:x}", md5::compute(s.as_bytes()))
}

#[derive(Serialize)]
struct Conversation {
    talker: String,
    name_hash: String,
    shards: Vec<ConversationShard>,
}

#[derive(Serialize, Clone)]
struct ConversationShard {
    /// Real snapshot database file (message_16.db) the table lives in.
    shard: String,
    table: String,
    row_count: i64,
}

/// Open every message shard, keyed and read-only, and prove the message
/// schema (Name2Id present) — a message_<N>.db without Name2Id is a wrong
/// schema and fails closed instead of being mixed in or skipped.
fn open_message_shards(
    snapshots: &Path,
    keys: &SnapshotKeys,
    generation: &VerifiedGeneration,
) -> Result<Vec<(String, Connection)>, String> {
    let mut shards = Vec::new();
    for file_name in &generation.message_dbs {
        let path = snapshots.join("message").join(file_name);
        let conn = open_snapshot_copy(&path, keys)
            .map_err(|e| format!("cannot open snapshot shard {file_name}: {e}"))?;
        let has_name2id: bool = conn
            .query_row(
                "SELECT count(*) FROM sqlite_master WHERE type='table' AND name='Name2Id'",
                [],
                |r| r.get::<_, i64>(0),
            )
            .map_err(|e| format!("schema probe failed in {file_name}: {e}"))?
            > 0;
        if !has_name2id {
            return Err(format!(
                "message shard {file_name} lacks the Name2Id table (wrong schema for a message shard); refusing to enumerate"
            ));
        }
        shards.push((file_name.clone(), conn));
    }
    Ok(shards)
}

/// rowid -> username per shard, for real_sender_id resolution.
fn load_name2id(conn: &Connection, shard: &str) -> Result<HashMap<i64, String>, String> {
    let mut map = HashMap::new();
    let mut stmt = conn
        .prepare("SELECT rowid, user_name FROM Name2Id")
        .map_err(|e| format!("Name2Id unreadable in {shard}: {e}"))?;
    let mut rows = stmt.query([]).map_err(|e| e.to_string())?;
    while let Some(row) = rows.next().map_err(|e| e.to_string())? {
        let rowid: i64 = row.get(0).map_err(|e| e.to_string())?;
        let user: String = row.get(1).map_err(|e| e.to_string())?;
        map.insert(rowid, user);
    }
    Ok(map)
}

/// Map every Msg_* table in every shard to its talker via the shard's
/// Name2Id table (table suffix = md5(username)). Tables matching no username
/// are returned as unknown — never dropped. Read errors propagate.
fn discover_conversations(
    shards: &[(String, Connection)],
    name2id_maps: &HashMap<String, HashMap<i64, String>>,
) -> Result<(Vec<Conversation>, Vec<String>), String> {
    let mut by_talker: BTreeMap<String, Conversation> = BTreeMap::new();
    let mut unknown_tables: Vec<String> = Vec::new();

    for (shard_name, conn) in shards {
        let usernames: Vec<String> = name2id_maps[shard_name].values().cloned().collect();
        let hash_to_talker: BTreeMap<String, String> = usernames
            .iter()
            .map(|u| (md5_hex(u), u.clone()))
            .collect();

        let mut stmt = conn
            .prepare("SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'Msg_%'")
            .map_err(|e| format!("table list unreadable in {shard_name}: {e}"))?;
        let tables: Vec<String> = stmt
            .query_map([], |r| r.get::<_, String>(0))
            .map_err(|e| e.to_string())?
            .collect::<Result<Vec<_>, _>>()
            .map_err(|e| format!("table enumeration failed in {shard_name}: {e}"))?;
        drop(stmt);

        for table in tables {
            let suffix = table.trim_start_matches("Msg_").to_string();
            let row_count: i64 = conn
                .query_row(
                    &format!("SELECT count(*) FROM {}", quote_ident(&table)),
                    [],
                    |r| r.get(0),
                )
                .map_err(|e| format!("count failed for {table} in {shard_name}: {e}"))?;
            match hash_to_talker.get(&suffix) {
                Some(talker) => {
                    let conversation = by_talker.entry(talker.clone()).or_insert_with(|| {
                        Conversation {
                            talker: talker.clone(),
                            name_hash: suffix.clone(),
                            shards: Vec::new(),
                        }
                    });
                    conversation.shards.push(ConversationShard {
                        shard: shard_name.clone(),
                        table,
                        row_count,
                    });
                }
                None => unknown_tables.push(table),
            }
        }
    }
    let mut conversations: Vec<Conversation> = by_talker.into_values().collect();
    for conversation in &mut conversations {
        conversation
            .shards
            .sort_by(|a, b| a.shard.cmp(&b.shard).then(a.table.cmp(&b.table)));
    }
    Ok((conversations, unknown_tables))
}

// ── export manifest shapes ──────────────────────────────────────────────

#[derive(Serialize)]
struct ExportManifestDb {
    file: String,
    sha256: String,
    bytes: u64,
}

#[derive(Serialize)]
struct SnapshotSection {
    generation: String,
    /// Generation start, Unix SECONDS (contract example is second-resolution).
    created_at: u64,
    databases: Vec<ExportManifestDb>,
}

#[derive(Serialize)]
struct SessionSection {
    talker_name: String,
    name_hash: String,
    shards: Vec<ShardStat>,
}

#[derive(Serialize)]
struct ShardStat {
    table: String,
    database: String,
    row_count: i64,
}

#[derive(Serialize)]
struct EnumerationSection {
    complete: bool,
    unknown_tables: Vec<String>,
    unknown_columns: BTreeMap<String, Vec<String>>,
}

#[derive(Serialize)]
struct RecordsSection {
    file: &'static str,
    count: usize,
}

/// Additive media section (contract §6: consumers ignore-and-record unknown
/// manifest fields). `complete` is true only when an attach root was
/// provided AND every reference resolved to staged bytes. Decode and output
/// failures never appear here as counts — they abort the export before any
/// manifest is published; only PROVEN absence (layouts exhausted, every
/// intermediate directory readable) is reported as unavailable. Kinds with
/// no local bytes at all (emoji, link-only app messages) are reported
/// separately as recorded gaps.
#[derive(Serialize)]
struct MediaSection {
    dir: &'static str,
    complete: bool,
    attach_root_provided: bool,
    staged_count: usize,
    unavailable_count: usize,
    /// Kinds that have no local bytes by nature — recorded gaps, never fake
    /// refs.
    kinds_without_local_bytes: BTreeMap<String, usize>,
    notes: Vec<&'static str>,
    sample_unavailable: Vec<String>,
}

#[derive(Serialize)]
struct ExportManifest {
    contract: &'static str,
    version: u64,
    kind: &'static str,
    archive_id: String,
    account: String,
    talker: String,
    snapshot: SnapshotSection,
    session: SessionSection,
    enumeration: EnumerationSection,
    records: RecordsSection,
    media_dir: &'static str,
    /// Additive: record ordering/pagination key for this producer.
    ordering: &'static str,
    /// Additive: media staging summary (see source-contract-update.md).
    media: MediaSection,
}

// ── per-table dynamic schema ────────────────────────────────────────────

struct TableSchema {
    unknown_columns: Vec<String>,
    has_wcdb_ct: bool,
    has_compress: bool,
    has_real_sender: bool,
}

fn inspect_table_schema(
    conn: &Connection,
    table: &str,
    shard: &str,
) -> Result<TableSchema, String> {
    let mut stmt = conn
        .prepare(&format!("PRAGMA table_info({})", quote_ident(table)))
        .map_err(|e| format!("table_info failed for {table} in {shard}: {e}"))?;
    let mut rows = stmt.query([]).map_err(|e| e.to_string())?;
    let mut columns: Vec<String> = Vec::new();
    while let Some(row) = rows.next().map_err(|e| e.to_string())? {
        columns.push(row.get::<_, String>(1).map_err(|e| e.to_string())?);
    }
    for required in REQUIRED_MSG_COLUMNS {
        if !columns.iter().any(|c| c == required) {
            return Err(format!(
                "message table {table} in {shard} lacks required column {required}; \
                 refusing to export it incompletely",
            ));
        }
    }
    let has = |name: &str| columns.iter().any(|c| c == name);
    Ok(TableSchema {
        has_wcdb_ct: has("WCDB_CT_message_content"),
        has_compress: has("compress_content"),
        has_real_sender: has("real_sender_id"),
        unknown_columns: columns
            .into_iter()
            .filter(|c| {
                !REQUIRED_MSG_COLUMNS.contains(&c.as_str())
                    && !OPTIONAL_KNOWN_COLUMNS.contains(&c.as_str())
                    && c != "rowid"
            })
            .collect(),
    })
}

/// Bounded work-set limits for one content row (archive export only): a
/// single raw cell, its decompressed expansion, and the whole raw ROW (all
/// columns summed) may never exceed these. Over-limit rows fail closed with
/// the machine-readable tokens `content_over_limit` / `decoded_over_limit` /
/// `row_over_limit` instead of letting one row drive unbounded memory use.
#[derive(Debug)]
struct ContentLimits {
    max_cell_bytes: usize,
    max_decoded_bytes: usize,
    /// Sum-of-row cap for every TEXT/BLOB cell (known and unknown columns).
    /// Applied twice: as SQLite `SQLITE_LIMIT_LENGTH` on the message-shard
    /// connection (per-cell, enforced inside sqlite3_step BEFORE any byte
    /// reaches Rust) and as a running total while the row's cells are read
    /// (wide rows whose cells each pass but whose sum exceeds the cap).
    max_row_bytes: usize,
    /// Cap on one serialized records.jsonl line. Derived, not free-standing:
    /// the record repeats each raw cell at most once (text <= 6x under JSON
    /// control-character escaping, base64 <= 4/3x) plus the decoded text
    /// (<= 6x of the decode cap) plus a fixed field overhead.
    max_json_line_bytes: usize,
}

impl ContentLimits {
    /// Derive the full limit set from CLI arguments. Fails closed on
    /// inconsistent configuration: a row cap smaller than the cell cap would
    /// reject every legal content cell as an over-limit row.
    fn from_args(max_cell_bytes: usize, max_decoded_bytes: usize, max_row_bytes: usize) -> Result<Self, String> {
        if max_row_bytes < max_cell_bytes {
            return Err(format!(
                "row bounds are inconsistent: --max-row-bytes {max_row_bytes} is smaller than \
                 --max-content-bytes {max_cell_bytes}; a legal content cell alone would always be \
                 rejected as an over-limit row — raise --max-row-bytes or lower --max-content-bytes"
            ));
        }
        let max_json_line_bytes = 6usize
            .saturating_mul(max_row_bytes.saturating_add(max_decoded_bytes))
            .saturating_add(65536);
        Ok(ContentLimits {
            max_cell_bytes,
            max_decoded_bytes,
            max_row_bytes,
            max_json_line_bytes,
        })
    }

    /// Value passed to `sqlite3_limit(SQLITE_LIMIT_LENGTH)`: the C API takes
    /// an i32, so anything larger is clamped to `i32::MAX`.
    fn sqlite_length_limit(&self) -> i32 {
        self.max_row_bytes.min(i32::MAX as usize) as i32
    }
}

/// Classify a rusqlite error as SQLite's SQLITE_TOOBIG: with
/// SQLITE_LIMIT_LENGTH set on the connection, any string/BLOB cell longer
/// than the limit (and any record SQLite refuses to build) makes
/// sqlite3_step itself fail with TOOBIG — before a single payload byte has
/// crossed into Rust memory.
fn is_sqlite_toobig(error: &rusqlite::Error) -> bool {
    matches!(
        error,
        rusqlite::Error::SqliteFailure(e, _) if e.code == rusqlite::ffi::ErrorCode::TooBig
    )
}

/// Payload bytes of a BORROWED column value (TEXT/BLOB only). Used to check
/// the running row total against the cap BEFORE the value is cloned into
/// owned memory — the clone happens only for cells already within budget.
fn value_ref_byte_len(value: &ValueRef<'_>) -> usize {
    match value {
        ValueRef::Text(t) => t.len(),
        ValueRef::Blob(b) => b.len(),
        _ => 0,
    }
}

/// SQL for the page ROW-WIDTH PROBE: `rowid` plus the summed
/// `octet_length` of every selected column. On direct column references the
/// bundled engine (SQLite 3.51 OP_Column OPFLAG_BYTELENARG) answers
/// `octet_length` from the b-tree RECORD HEADER alone — TEXT and BLOB
/// payloads, overflow chains included, are never materialized — and it
/// counts exact BYTES (unlike `length()`, which counts characters for
/// UTF-8 text). NULL contributes zero via COALESCE; INTEGER/REAL
/// contribute only their tiny text representation. The probe is therefore
/// the whole-raw-row budget check that fires BEFORE any payload byte of
/// the row is read anywhere (SQLite registers included).
fn row_width_probe_sql(table: &str, select_columns: &[String], page_size: usize) -> String {
    let terms = select_columns
        .iter()
        .map(|column| format!("COALESCE(octet_length({}),0)", quote_ident(column)))
        .collect::<Vec<_>>()
        .join(" + ");
    format!(
        "SELECT rowid, ({terms}) FROM {} WHERE rowid > ?1 ORDER BY rowid ASC LIMIT {page_size}",
        quote_ident(table)
    )
}

// ── media staging ───────────────────────────────────────────────────────

/// Hard-error classification for media resolution: database, permission and
/// unknown failures abort the export; only genuine absence is a soft gap.
fn hard_media_error(e: &MediaError) -> bool {
    !matches!(
        e,
        MediaError::LookupMiss(_)
            | MediaError::NotFound(_)
            | MediaError::NoDatFiles { .. }
            | MediaError::NoMediaDbs(_)
            | MediaError::SchemaMissing(_)
    )
}

/// One located image source, carrying WHERE it came from so the exported
/// ref can state its evidence instead of asserting a tier it cannot prove.
struct ImageCandidate {
    path: PathBuf,
    /// "attach" (verified attach layout) or "hardlink" (hardlink.db entry).
    layout: &'static str,
    file_name: String,
    /// dir1/dir2 provenance for hardlink-sourced candidates.
    dir1: Option<String>,
    dir2: Option<String>,
    /// Whether an original-tier file (`<md5>.dat` / `<md5>_h.dat`) exists
    /// among the attach candidates for this md5 — evidence about what the
    /// staged bytes substitute for when they are not the original tier.
    original_available: bool,
}

/// Classify an image candidate by file name, following the verified
/// media-v1 naming tiers: exact `<md5>.dat` and `<md5>_h.dat` (high
/// quality) are the ORIGINAL tier, `<md5>_t.dat` is a THUMBNAIL — never
/// delivered or counted as the original — other same-md5 files are
/// derivatives, and unrecognized names are `unknown` rather than a guess.
fn classify_image_variant(file_name: &str, md5: &str) -> &'static str {
    if file_name == format!("{md5}.dat") || file_name == format!("{md5}_h.dat") {
        "original"
    } else if file_name == format!("{md5}_t.dat") {
        "thumbnail"
    } else if file_name.starts_with(md5) {
        "derivative"
    } else {
        "unknown"
    }
}

struct MediaStager<'a> {
    export_dir: &'a Path,
    attach_root: Option<&'a Path>,
    /// Talker (chat partner): the attach image layout is keyed by
    /// md5(talker), not by the account.
    talker: &'a str,
    hardlink_conn: Option<Connection>,
    media_conns: Vec<Connection>,
    max_bytes: u64,
    /// Decoding options for image .dat files (keys stay Mac-side; they are
    /// never exported).
    dat_opts: DatDecryptOptions,
    /// ref_key -> ref json (cache: same ref staged once).
    staged: BTreeMap<String, Value>,
    /// "ref_key: reason" for references whose local bytes are confirmed
    /// absent (proven: layouts exhausted, every intermediate dir readable).
    unavailable: Vec<String>,
    /// Kinds with no local bytes at all (recorded gaps).
    kinds_without_local_bytes: BTreeMap<String, usize>,
}

impl<'a> MediaStager<'a> {
    #[allow(clippy::too_many_arguments)]
    fn new(
        snapshots: &Path,
        export_dir: &'a Path,
        attach_root: Option<&'a Path>,
        talker: &'a str,
        keys: &SnapshotKeys,
        generation: &VerifiedGeneration,
        max_bytes: u64,
        dat_opts: DatDecryptOptions,
    ) -> Result<Self, String> {
        let hardlink_conn = match &generation.hardlink_db {
            Some(rel) => Some(
                open_snapshot_copy(&snapshots.join(rel), keys)
                    .map_err(|e| format!("cannot open snapshot hardlink.db: {e}"))?,
            ),
            None => None,
        };
        let mut media_conns = Vec::new();
        for rel in &generation.media_dbs {
            let conn = open_snapshot_copy(&snapshots.join(rel), keys)
                .map_err(|e| format!("cannot open snapshot media db {rel}: {e}"))?;
            media_conns.push(conn);
        }
        Ok(MediaStager {
            export_dir,
            attach_root,
            talker,
            hardlink_conn,
            media_conns,
            max_bytes,
            dat_opts,
            staged: BTreeMap::new(),
            unavailable: Vec::new(),
            kinds_without_local_bytes: BTreeMap::new(),
        })
    }

    fn record_gap(&mut self, kind: &str) {
        *self.kinds_without_local_bytes.entry(kind.to_string()).or_insert(0) += 1;
    }

    /// Query the snapshot hardlink db, classifying misses (no entry / no
    /// table for the type) as empty and everything else as a hard error.
    fn hardlink_entries(
        &self,
        media_type: &str,
        key: &str,
    ) -> Result<Vec<wx_media::HardlinkEntry>, String> {
        let Some(conn) = &self.hardlink_conn else {
            return Ok(Vec::new());
        };
        match query_hardlink_with_conn(conn, media_type, key) {
            Ok(entries) => Ok(entries),
            Err(e) if !hard_media_error(&e) => Ok(Vec::new()),
            Err(e) => Err(format!(
                "hardlink db query failed for {media_type} {key}: {e}"
            )),
        }
    }

    /// Image: the raw `.dat` is located via the verified attach layout
    /// (keyed by md5(talker)) or hardlink candidates, then DECODED on the
    /// Mac into usable image bytes, which are what gets staged — the
    /// encrypted `.dat` is never delivered as media. Decode failures
    /// (unknown format, missing key, AES failure), zero-byte output, and
    /// container-export failures are HARD errors: the export aborts and no
    /// export.json is published. Only a proven-absent `.dat` is a gap.
    fn stage_image(&mut self, md5: &str) -> Result<Value, String> {
        let ref_key = format!("md5_{md5}");
        if let Some(cached) = self.staged.get(&ref_key) {
            return Ok(cached.clone());
        }
        let Some(candidate) = self.locate_image(md5)? else {
            return Ok(self.gap(
                "image",
                &ref_key,
                "no local .dat found (attach layout and hardlink.db exhausted; intermediates readable)",
            ));
        };
        // Bound the work set BEFORE reading: over-limit (or growing) inputs
        // are refused up front — never "read it all, then notice".
        let raw = read_bounded(&candidate.path, self.max_bytes)
            .map_err(|e| format!("{ref_key}: {e}"))?;
        let raw_sha256 = sha256_hex(&raw);
        let decoded = decrypt_dat(&raw, &self.dat_opts).map_err(|e| {
            format!("{ref_key}: image .dat decode failed (keys stay Mac-side): {e}")
        })?;
        // Container normalization follows the verified media-v1 semantics:
        // a transcode error propagates (never silently staged as usable
        // media); a wxgf container that could not be transcoded is delivered
        // as-is with its status recorded in the ref.
        let (bytes, ext, transcoded, wxgf_passthrough) =
            export_image_bytes(decoded.data, &decoded.ext)
                .map_err(|e| format!("{ref_key}: image container export failed: {e}"))?;
        if bytes.is_empty() {
            return Err(format!(
                "{ref_key}: image decoded to zero bytes; refusing to publish empty media"
            ));
        }
        let (length, sha256) = self.stage_bytes(&ref_key, &bytes)?;
        // Source variant layering (media-v1 naming tiers): the ref states
        // WHICH tier the bytes came from plus the evidence for that label.
        let variant = classify_image_variant(&candidate.file_name, md5);
        let mut evidence = Map::new();
        evidence.insert("layout".into(), json!(candidate.layout));
        evidence.insert("file_name".into(), json!(candidate.file_name));
        if let Some(dir1) = &candidate.dir1 {
            evidence.insert("dir1".into(), json!(dir1));
        }
        if let Some(dir2) = &candidate.dir2 {
            evidence.insert("dir2".into(), json!(dir2));
        }
        let original_present = variant == "original" || candidate.original_available;
        if variant != "original" {
            // Honest degradation: the bytes ARE staged (a thumbnail is real
            // local media), but the export records that the original was
            // not captured — media completeness stays false.
            self.unavailable.push(format!(
                "{ref_key}: only {variant} available (original absent); staged as-is, not a substitute for the original"
            ));
        }
        let value = json!({
            "kind": "image",
            "ref_key": ref_key,
            "present": true,
            "length": length,
            "sha256": sha256,
            "raw_sha256": raw_sha256,
            "decoded": true,
            "ext": ext,
            "variant": variant,
            "variant_evidence": Value::Object(evidence),
            "original_present": original_present,
            "transcoded": transcoded,
            "wxgf_passthrough": wxgf_passthrough,
            "filename": format!("media/{ref_key}"),
        });
        self.staged.insert(ref_key.clone(), value.clone());
        Ok(value)
    }

    fn stage_video(&mut self, md5: &str) -> Result<Value, String> {
        let ref_key = format!("md5_{md5}");
        if let Some(cached) = self.staged.get(&ref_key) {
            return Ok(cached.clone());
        }
        let located = self.locate_video(md5)?;
        self.finish_file_ref("video", &ref_key, located)
    }

    fn stage_file(&mut self, md5: &str) -> Result<Value, String> {
        let ref_key = format!("md5_{md5}");
        if let Some(cached) = self.staged.get(&ref_key) {
            return Ok(cached.clone());
        }
        let located = self.locate_file(md5)?;
        self.finish_file_ref("file", &ref_key, located)
    }

    /// Shared tail for verbatim-file refs. `located` is `Ok(None)` for
    /// confirmed absence (soft gap) and `Ok(Some(path))` when found; hard
    /// failures (permission, IO, unknown) propagate as `Err` and abort the
    /// export instead of degrading to `present=false`.
    fn finish_file_ref(
        &mut self,
        kind: &str,
        ref_key: &str,
        located: Option<PathBuf>,
    ) -> Result<Value, String> {
        let Some(path) = located else {
            return Ok(self.gap(
                kind,
                ref_key,
                "no local file found (snapshot hardlink.db and attach layout exhausted)",
            ));
        };
        let (length, sha256) = self
            .copy_into_media(ref_key, &path)
            .map_err(|e| format!("{ref_key}: staging failed: {e}"))?;
        let value = json!({
            "kind": kind,
            "ref_key": ref_key,
            "present": true,
            "length": length,
            "sha256": sha256,
            "filename": format!("media/{ref_key}"),
        });
        self.staged.insert(ref_key.to_string(), value.clone());
        Ok(value)
    }

    fn gap(&mut self, kind: &str, ref_key: &str, reason: &str) -> Value {
        self.unavailable.push(format!("{ref_key}: {reason}"));
        let value = json!({
            "kind": kind,
            "ref_key": ref_key,
            "present": false,
            "reason": reason,
        });
        self.staged.insert(ref_key.to_string(), value.clone());
        value
    }

    /// Image raw .dat location: verified attach layout keyed by md5(talker)
    /// first (the query/export path's rule), hardlink candidates second.
    /// Resolver errors are classified — soft misses fall through, but before
    /// "absence" may be reported the attach tree must be proven READABLE:
    /// the resolver swallows directory IO errors, and an unreadable
    /// intermediate dir must never masquerade as a missing file. The winner
    /// is returned with its provenance (layout + file name) so the exported
    /// ref can carry variant evidence; the media-v1 preference order
    /// (`_h.dat` > exact > first candidate) picks the best available tier.
    fn locate_image(&self, md5: &str) -> Result<Option<ImageCandidate>, String> {
        let Some(root) = self.attach_root else {
            return Ok(None);
        };
        let talker_dir = root.join(md5_hex(self.talker));
        match resolve_image_by_md5(self.talker, root, md5) {
            Ok(lookup) => {
                let exact = format!("{md5}.dat");
                let hd = format!("{md5}_h.dat");
                let original_available = lookup.candidates.iter().any(|path| {
                    let name = path.file_name().map(|n| n.to_string_lossy().to_string());
                    name.as_deref() == Some(exact.as_str()) || name.as_deref() == Some(hd.as_str())
                });
                let best = lookup
                    .recommended
                    .clone()
                    .or_else(|| lookup.candidates.first().cloned());
                if let Some(best) = best {
                    if probe_file(&best)? {
                        let file_name = best
                            .file_name()
                            .map(|n| n.to_string_lossy().to_string())
                            .unwrap_or_default();
                        return Ok(Some(ImageCandidate {
                            path: best,
                            layout: "attach",
                            file_name,
                            dir1: None,
                            dir2: None,
                            original_available,
                        }));
                    }
                }
            }
            Err(e) if !hard_media_error(&e) => {
                ensure_attach_tree_readable(&talker_dir)?;
            }
            Err(e) => return Err(format!("image lookup failed for {md5}: {e}")),
        }
        for entry in self.hardlink_entries("image", md5)? {
            let candidate = root.join(&entry.dir1).join(&entry.dir2).join(&entry.file_name);
            if probe_file(&candidate)? {
                return Ok(Some(ImageCandidate {
                    path: candidate,
                    layout: "hardlink",
                    file_name: entry.file_name.clone(),
                    dir1: Some(entry.dir1.clone()),
                    dir2: Some(entry.dir2.clone()),
                    // A hardlink entry is one file; nothing here can prove an
                    // original exists elsewhere, so the label must not claim it.
                    original_available: false,
                }));
            }
        }
        Ok(None)
    }

    /// Video: hardlink db + the verified candidate layouts used by the
    /// media export path.
    fn locate_video(&self, md5: &str) -> Result<Option<PathBuf>, String> {
        let Some(root) = self.attach_root else {
            return Ok(None);
        };
        for entry in self.hardlink_entries("video", md5)? {
            let candidates = [
                root.join(&entry.dir1)
                    .join(&entry.dir2)
                    .join("Video")
                    .join(&entry.file_name),
                root.join(&entry.dir1).join(&entry.dir2).join(&entry.file_name),
                root.join(&entry.dir1).join("Video").join(&entry.file_name),
            ];
            for candidate in &candidates {
                if probe_file(candidate)? {
                    return Ok(Some(candidate.clone()));
                }
            }
        }
        Ok(None)
    }

    /// App-file: hardlink db + verified two-level layout.
    fn locate_file(&self, md5: &str) -> Result<Option<PathBuf>, String> {
        let Some(root) = self.attach_root else {
            return Ok(None);
        };
        for entry in self.hardlink_entries("file", md5)? {
            let candidates = [
                root.join(&entry.dir1).join(&entry.dir2).join(&entry.file_name),
                root.join(&entry.dir1).join(&entry.file_name),
            ];
            for candidate in &candidates {
                if probe_file(candidate)? {
                    return Ok(Some(candidate.clone()));
                }
            }
        }
        Ok(None)
    }

    /// Voice blobs live INSIDE the snapshot (media dbs, keyed open, looked
    /// up by svr_id) — no attach root needed. The blob's length is probed
    /// in SQL BEFORE it is materialized, so an over-limit blob is refused
    /// (`media_input_over_limit`) without ever being read into memory.
    /// Database errors are hard failures; a miss in every media db is a
    /// soft gap.
    fn stage_voice(&mut self, svr_id: i64) -> Result<Value, String> {
        let ref_key = format!("voice_{svr_id}");
        if let Some(cached) = self.staged.get(&ref_key) {
            return Ok(cached.clone());
        }
        let key = svr_id.to_string();
        for conn in &self.media_conns {
            // Bound the work set BEFORE the blob is read: `length()` answers
            // from the record header without materializing the blob. A
            // missing VoiceInfo table in THIS db is a legitimate schema
            // variant — the extract below already reports that softly.
            let voice_table: i64 = conn
                .query_row(
                    "SELECT count(*) FROM sqlite_master WHERE type='table' AND name='VoiceInfo'",
                    [],
                    |r| r.get(0),
                )
                .map_err(|e| format!("voice schema probe failed for svr_id {svr_id}: {e}"))?;
            if voice_table > 0 {
                let blob_len: Option<i64> = conn
                    .query_row(
                        "SELECT length(voice_data) FROM VoiceInfo WHERE svr_id = ?1",
                        [&key],
                        |r| r.get(0),
                    )
                    .ok();
                if let Some(len) = blob_len {
                    if len < 0 || len as u64 > self.max_bytes {
                        return Err(format!(
                            "{ref_key}: media_input_over_limit: voice blob is {len} bytes, exceeds limit {}",
                            self.max_bytes
                        ));
                    }
                }
            }
            match extract_voice_with_conn(conn, &key) {
                Ok(blob) => {
                    if blob.data.is_empty() {
                        continue;
                    }
                    let (length, sha256) = self.stage_bytes(&ref_key, &blob.data)?;
                    let value = json!({
                        "kind": "voice",
                        "ref_key": ref_key,
                        "present": true,
                        "length": length,
                        "sha256": sha256,
                        "format": "silk",
                        "filename": format!("media/{ref_key}"),
                    });
                    self.staged.insert(ref_key.clone(), value.clone());
                    return Ok(value);
                }
                // Miss in this db (or no VoiceInfo table — legitimate schema
                // variant): keep looking in the others.
                Err(MediaError::LookupMiss(_)) => {}
                Err(MediaError::SchemaMissing(_)) => {}
                // Database errors are hard failures.
                Err(e) => {
                    return Err(format!("voice lookup failed for svr_id {svr_id}: {e}"));
                }
            }
        }
        let reason = if self.media_conns.is_empty() {
            "no media databases in this snapshot generation"
        } else {
            "voice blob not found in any snapshot media database"
        };
        Ok(self.gap("voice", &ref_key, reason))
    }

    /// Stage raw bytes already held in memory (decoded images, voice blobs)
    /// at `media/<ref_key>` with 0600 creation and sha256 pinning.
    fn stage_bytes(&self, ref_key: &str, bytes: &[u8]) -> Result<(u64, String), String> {
        let media_dir = self.export_dir.join("media");
        ensure_private_dir(&media_dir).map_err(|e| e.to_string())?;
        let target = media_dir.join(ref_key);
        if target.exists() {
            return Err("staged media collision (unexpected duplicate ref)".into());
        }
        if bytes.len() as u64 > self.max_bytes {
            return Err(format!(
                "media file exceeds --max-media-bytes ({} > {})",
                bytes.len(),
                self.max_bytes
            ));
        }
        let mut writer = std::fs::OpenOptions::new()
            .write(true)
            .create_new(true)
            .mode(0o600)
            .open(&target)
            .map_err(|e| format!("stage create failed for {ref_key}: {e}"))?;
        writer
            .write_all(bytes)
            .map_err(|e| format!("stage write failed for {ref_key}: {e}"))?;
        writer
            .sync_all()
            .map_err(|e| format!("stage fsync failed for {ref_key}: {e}"))?;
        Ok((bytes.len() as u64, sha256_hex(bytes)))
    }

    /// Copy a source file verbatim into `media/<ref_key>` (0600 create-new,
    /// hash while copying, fsync) for kinds stored as plain bytes on disk
    /// (video/app-file). All IO and permission errors are hard failures.
    fn copy_into_media(&self, ref_key: &str, source: &Path) -> Result<(u64, String), String> {
        let meta = std::fs::metadata(source).map_err(|e| format!("stat failed: {e}"))?;
        if meta.len() > self.max_bytes {
            return Err(format!(
                "media file exceeds --max-media-bytes ({} > {})",
                meta.len(),
                self.max_bytes
            ));
        }
        let mut reader =
            std::fs::File::open(source).map_err(|e| format!("media open failed: {e}"))?;
        let media_dir = self.export_dir.join("media");
        ensure_private_dir(&media_dir).map_err(|e| e.to_string())?;
        let target = media_dir.join(ref_key);
        if target.exists() {
            return Err("staged media collision (unexpected duplicate ref)".into());
        }
        let mut writer = std::fs::OpenOptions::new()
            .write(true)
            .create_new(true)
            .mode(0o600)
            .open(&target)
            .map_err(|e| format!("stage create failed for {ref_key}: {e}"))?;
        use sha2::{Digest, Sha256};
        let mut hasher = Sha256::new();
        let mut buf = [0u8; 64 * 1024];
        let mut length: u64 = 0;
        loop {
            let n = reader
                .read(&mut buf)
                .map_err(|e| format!("media read failed: {e}"))?;
            if n == 0 {
                break;
            }
            writer
                .write_all(&buf[..n])
                .map_err(|e| format!("stage write failed: {e}"))?;
            hasher.update(&buf[..n]);
            length += n as u64;
        }
        writer
            .sync_all()
            .map_err(|e| format!("stage fsync failed: {e}"))?;
        Ok((length, hex::encode(hasher.finalize())))
    }
}

/// Prove the talker's attach tree is READABLE before "no local .dat" may be
/// reported as proven absence: the image resolver swallows directory IO
/// errors inside its layout walk, so an unreadable intermediate directory
/// (permission revoked mid-export, disk failing) would otherwise masquerade
/// as a missing file. `NotFound` is the only tolerated error — a talker with
/// no attach tree at all is genuine absence.
fn ensure_attach_tree_readable(talker_dir: &Path) -> Result<(), String> {
    let entries = match std::fs::read_dir(talker_dir) {
        Ok(entries) => entries,
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => return Ok(()),
        Err(e) => {
            return Err(format!(
                "cannot enumerate attach dir {}: {e}; absence cannot be proven through an unreadable directory",
                talker_dir.display()
            ))
        }
    };
    for entry in entries {
        let entry =
            entry.map_err(|e| format!("readdir {} failed: {e}", talker_dir.display()))?;
        let path = entry.path();
        let is_dir = entry
            .file_type()
            .map_err(|e| format!("cannot stat {}: {e}", path.display()))?
            .is_dir();
        if !is_dir {
            continue;
        }
        let img_dir = path.join("Img");
        match std::fs::read_dir(&img_dir) {
            Ok(_) => {}
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => {}
            Err(e) => {
                return Err(format!(
                    "cannot enumerate {}: {e}; absence cannot be proven through an unreadable directory",
                    img_dir.display()
                ))
            }
        }
    }
    Ok(())
}

// ── command entry ───────────────────────────────────────────────────────

pub fn cmd_archive_inspect(args: ArchiveInspectArgs) -> i32 {
    match run_inspect(&args) {
        Ok(summary) => {
            println!("{}", serde_json::to_string(&summary).unwrap_or_default());
            0
        }
        Err(message) => {
            // Machine-readable failure on stdout; nothing about keys or
            // message content is ever included.
            println!(
                "{}",
                json!({"ok": false, "kind": "export_failed", "error": message})
            );
            1
        }
    }
}

fn run_inspect(args: &ArchiveInspectArgs) -> Result<Value, String> {
    let keys = load_keys(&args.key_file)?;
    let manifest = load_generation(&args.snapshots)?;
    let generation = verify_generation(&args.snapshots, &manifest)?;
    let shards = open_message_shards(&args.snapshots, &keys, &generation)?;
    let mut name2id_maps: HashMap<String, HashMap<i64, String>> = HashMap::new();
    for (shard_name, conn) in &shards {
        name2id_maps.insert(shard_name.clone(), load_name2id(conn, shard_name)?);
    }
    let (conversations, unknown_tables) = discover_conversations(&shards, &name2id_maps)?;

    match args.action.as_str() {
        "list" => Ok(json!({
            "ok": true,
            "action": "list",
            "generation": generation.manifest.generation,
            "conversations": conversations,
            "unknown_tables": unknown_tables,
            "message_dbs": generation.message_dbs,
            "media_dbs": generation.media_dbs,
            "hardlink_db": generation.hardlink_db,
            "other_dbs": generation.other_dbs,
        })),
        "export" => {
            let talker = args
                .talker
                .as_deref()
                .filter(|t| !t.is_empty())
                .ok_or("export requires --talker")?;
            let account = args
                .account
                .as_deref()
                .filter(|a| !a.is_empty())
                .ok_or("export requires --account")?;
            let archive_id = args
                .archive_id
                .as_deref()
                .filter(|a| !a.is_empty())
                .ok_or("export requires --archive-id")?;
            let conversation = conversations
                .iter()
                .find(|c| c.talker == talker)
                .ok_or_else(|| {
                    format!(
                        "talker not found in this generation ({} conversations discovered)",
                        conversations.len()
                    )
                })?;
            export_session(
                args,
                &keys,
                &generation,
                conversation,
                &name2id_maps,
                unknown_tables,
                account,
                archive_id,
            )
        }
        other => Err(format!("unknown action {other:?} (expected list or export)")),
    }
}

/// Build the .dat decoding options: the v2 AES key comes from a private
/// 0600 key file when provided (never argv), and the XOR key is
/// auto-detected from the talker's public attach tree.
fn dat_decrypt_options(
    args: &ArchiveInspectArgs,
    attach_root: Option<&Path>,
    talker: &str,
) -> Result<DatDecryptOptions, String> {
    let v2_aes_key = match &args.dat_key_file {
        Some(path) => Some(load_dat_key(path)?),
        None => None,
    };
    let mut opts = DatDecryptOptions {
        v2_aes_key,
        xor_key: None,
    };
    if let Some(root) = attach_root {
        let talker_attach = root.join(md5_hex(talker));
        if let Some(key) = detect_xor_key(&talker_attach) {
            opts.xor_key = Some(key);
        }
    }
    Ok(opts)
}

#[allow(clippy::too_many_arguments)]
fn export_session(
    args: &ArchiveInspectArgs,
    keys: &SnapshotKeys,
    generation: &VerifiedGeneration,
    conversation: &Conversation,
    name2id_maps: &HashMap<String, HashMap<i64, String>>,
    unknown_tables: Vec<String>,
    account: &str,
    archive_id: &str,
) -> Result<Value, String> {
    // Fresh, private export directory.
    if args.out.exists() {
        return Err(format!(
            "export directory {} already exists; exports are immutable",
            args.out.display()
        ));
    }
    ensure_private_dir(&args.out).map_err(|e| e.to_string())?;

    let records_path = args.out.join("records.jsonl");
    let mut records_file = std::fs::OpenOptions::new()
        .write(true)
        .create_new(true)
        .mode(0o600)
        .open(&records_path)
        .map_err(|e| format!("records.jsonl create failed: {e}"))?;

    let dat_opts =
        dat_decrypt_options(args, args.attach_root.as_deref(), &conversation.talker)?;
    let mut stager = MediaStager::new(
        &args.snapshots,
        &args.out,
        args.attach_root.as_deref(),
        &conversation.talker,
        keys,
        generation,
        args.max_media_bytes,
        dat_opts,
    )?;

    let limits = ContentLimits::from_args(
        args.max_content_bytes,
        args.max_decoded_bytes,
        args.max_row_bytes,
    )?;

    let mut unknown_columns: BTreeMap<String, Vec<String>> = BTreeMap::new();
    // Enumeration completeness is a hard invariant, not a reported maybe:
    // any pagination/count mismatch or read error aborts the export above,
    // so a written manifest can only carry complete=true.
    let enumeration_ok = true;
    let mut count: usize = 0;
    let page_size = args.page_size.clamp(1, 10_000);

    // Contract order: records ascend by (shard = database file, table,
    // local_rowid) — the real shard database is part of the ordering key so
    // the same table in several message_N.db files (WeChat shard migration)
    // never collides or interleaves rows.
    let mut ordered_shards: Vec<&ConversationShard> = conversation.shards.iter().collect();
    ordered_shards.sort_by(|a, b| a.shard.cmp(&b.shard).then(a.table.cmp(&b.table)));
    for shard_stat in ordered_shards {
        let shard_name = &shard_stat.shard;
        let table = &shard_stat.table;
        let shard_path = args.snapshots.join("message").join(shard_name);
        let conn = open_snapshot_copy(&shard_path, keys)
            .map_err(|e| format!("reopen shard {shard_name} failed: {e}"))?;
        // Row-length bound, BEFORE any row is read: with SQLITE_LIMIT_LENGTH
        // set on this offline connection, an over-limit TEXT/BLOB cell (any
        // column, known or unknown) fails inside sqlite3_step with
        // SQLITE_TOOBIG — the row is refused before a single payload byte is
        // copied into Rust memory.
        conn.set_limit(Limit::SQLITE_LIMIT_LENGTH, limits.sqlite_length_limit())
            .map_err(|e| format!("cannot set row length limit on {shard_name}: {e}"))?;
        let schema = inspect_table_schema(&conn, table, shard_name)?;
        if !schema.unknown_columns.is_empty() {
            unknown_columns.insert(table.clone(), schema.unknown_columns.clone());
        }

        // Fixed select prefix; optional known columns inserted in a stable
        // order; unknown columns appended verbatim. select_columns mirrors
        // this order (minus rowid) and drives the record's `raw` object.
        let mut select_cols = String::from(
            "rowid, sort_seq, server_id, local_type, create_time, status, \
             message_content, packed_info_data",
        );
        let mut select_columns: Vec<String> = vec![
            "sort_seq".into(),
            "server_id".into(),
            "local_type".into(),
            "create_time".into(),
            "status".into(),
            "message_content".into(),
            "packed_info_data".into(),
        ];
        if schema.has_wcdb_ct {
            select_cols.push_str(", WCDB_CT_message_content");
            select_columns.push("WCDB_CT_message_content".into());
        }
        if schema.has_compress {
            select_cols.push_str(", compress_content");
            select_columns.push("compress_content".into());
        }
        if schema.has_real_sender {
            select_cols.push_str(", real_sender_id");
            select_columns.push("real_sender_id".into());
        }
        for col in &schema.unknown_columns {
            select_cols.push_str(", ");
            select_cols.push_str(&quote_ident(col));
            select_columns.push(col.clone());
        }
        let base_select = format!("SELECT {select_cols} FROM {}", quote_ident(table));

        let mut last_rowid: i64 = 0;
        let mut exported_for_table: i64 = 0;
        // Two-stage keyset pagination, both stages streaming one row at a
        // time — no page of full payloads is ever buffered:
        //
        // 1. WIDTH PROBE (record headers only): rowid + summed octet_length
        //    of every selected column. An over-budget row is refused HERE,
        //    before a single payload byte of it is materialized anywhere
        //    (SQLite's step of a payload SELECT would otherwise materialize
        //    every cell of the row into engine registers first — a 16-cell
        //    wide row slips 16x the per-cell limit past a cell-only check).
        // 2. PAYLOAD READ bounded to the probed rowid window. The snapshot
        //    is a static, sha256-pinned copy, so the probe result is exact
        //    for exactly these rows; the data statement re-verifies each row
        //    through the connection's SQLITE_LIMIT_LENGTH (per-cell TOOBIG)
        //    and the running Rust total (defense in depth on every layer).
        //
        // The keyset anchor and the exported-vs-enumerated count check below
        // keep pagination lossless.
        let probe_sql = row_width_probe_sql(table, &select_columns, page_size);
        let data_sql = format!("{base_select} WHERE rowid > ?1 AND rowid <= ?2 ORDER BY rowid ASC");
        loop {
            let mut probe = conn
                .prepare(&probe_sql)
                .map_err(|e| format!("row-width probe prepare failed for {table}: {e}"))?;
            let mut probed: usize = 0;
            let mut anchor: Option<i64> = None;
            {
                let mut probe_rows = probe
                    .query([last_rowid])
                    .map_err(|e| format!("row-width probe failed for {table}: {e}"))?;
                while probed < page_size {
                    let row = match probe_rows.next() {
                        Ok(Some(row)) => row,
                        Ok(None) => break,
                        Err(e) if is_sqlite_toobig(&e) => {
                            return Err(format!(
                                "row_over_limit: sqlite refused the row-width probe of {table} \
                                 in {shard_name} past rowid {last_rowid} (a cell exceeds \
                                 --max-row-bytes {}): {e}",
                                limits.max_row_bytes
                            ));
                        }
                        Err(e) => {
                            return Err(format!(
                                "row-width probe iteration failed for {table}: {e}"
                            ));
                        }
                    };
                    let rowid: i64 = row.get(0).map_err(|e| e.to_string())?;
                    let row_octets: i64 = row.get(1).map_err(|e| e.to_string())?;
                    if row_octets < 0 || row_octets as u64 > limits.max_row_bytes as u64 {
                        return Err(format!(
                            "row_over_limit: raw row byte total of {table}#{rowid} in {shard_name} \
                             is {row_octets} (probed from record headers before any payload read), \
                             exceeds --max-row-bytes {}",
                            limits.max_row_bytes
                        ));
                    }
                    anchor = Some(rowid);
                    probed += 1;
                }
            }
            drop(probe);
            let Some(anchor_rowid) = anchor else {
                break; // no rows left past the keyset anchor
            };

            let mut stmt = conn
                .prepare(&data_sql)
                .map_err(|e| format!("prepare failed for {table}: {e}"))?;
            let columns = stmt.column_count();
            let mut rows = stmt
                .query(rusqlite::params![last_rowid, anchor_rowid])
                .map_err(|e| format!("query failed for {table}: {e}"))?;
            while let Some(row) = match rows.next() {
                Ok(row) => row,
                // SQLITE_TOOBIG from the connection's SQLITE_LIMIT_LENGTH:
                // SQLite refused the row (a cell longer than
                // --max-row-bytes) before materializing it for us. Rows in
                // this window passed the width probe, so this firing means
                // the probe and the engine disagree — refuse either way.
                Err(e) if is_sqlite_toobig(&e) => {
                    return Err(format!(
                        "row_over_limit: sqlite refused a row of {table} in {shard_name} past \
                         rowid {last_rowid} (a cell exceeds --max-row-bytes {}): {e}",
                        limits.max_row_bytes
                    ));
                }
                Err(e) => {
                    return Err(format!("row iteration failed for {table}: {e}"));
                }
            } {
                let rowid: i64 = row.get(IDX_ROWID).map_err(|e| e.to_string())?;
                let mut values: Vec<ValueRefOwned> = Vec::with_capacity(columns);
                let mut row_total: usize = 0;
                for index in 0..columns {
                    let value_ref = match row.get_ref(index) {
                        Ok(value) => value,
                        Err(e) if is_sqlite_toobig(&e) => {
                            return Err(format!(
                                "row_over_limit: sqlite refused a cell of {table}#{rowid} in \
                                 {shard_name} (exceeds --max-row-bytes {}): {e}",
                                limits.max_row_bytes
                            ));
                        }
                        Err(e) => {
                            return Err(format!(
                                "column read failed for {table}#{rowid} in {shard_name}: {e}"
                            ));
                        }
                    };
                    let cell_bytes = value_ref_byte_len(&value_ref);
                    // Defense in depth: the row already passed the header
                    // probe, so this running total re-asserts the same budget
                    // on the materialized copy — checked BEFORE the cell is
                    // cloned, keeping owned row memory within the cap.
                    if row_total.saturating_add(cell_bytes) > limits.max_row_bytes {
                        let column = select_columns
                            .get(index.saturating_sub(1))
                            .map(String::as_str)
                            .unwrap_or("rowid");
                        return Err(format!(
                            "row_over_limit: row byte total of {table}#{rowid} in {shard_name} \
                             exceeds --max-row-bytes {} at column {column:?} ({} bytes into the \
                             row, next cell adds {cell_bytes})",
                            limits.max_row_bytes, row_total
                        ));
                    }
                    row_total += cell_bytes;
                    values.push(ValueRefOwned::from(value_ref));
                }
                let record = build_record(
                    shard_name,
                    table,
                    rowid,
                    &select_columns,
                    &values,
                    &schema,
                    &limits,
                    name2id_maps
                        .get(shard_name)
                        .ok_or_else(|| format!("Name2Id map missing for {shard_name}"))?,
                    &mut stager,
                )?;
                let line = serde_json::to_string(&record)
                    .map_err(|e| format!("record serialization failed: {e}"))?;
                // The encoded record line is bounded too: a row within the
                // raw cap can only serialize to a bounded multiple of it, and
                // exceeding that derivation is a machine-readable failure —
                // never an unbounded line in records.jsonl.
                if line.len() > limits.max_json_line_bytes {
                    return Err(format!(
                        "row_json_over_limit: serialized record for {table}#{rowid} in \
                         {shard_name} is {} bytes, exceeds the derived line cap {}",
                        line.len(),
                        limits.max_json_line_bytes
                    ));
                }
                writeln!(records_file, "{line}")
                    .map_err(|e| format!("records.jsonl write failed: {e}"))?;
                count += 1;
                exported_for_table += 1;
            }
            drop(rows);
            drop(stmt);

            last_rowid = anchor_rowid;
            if probed < page_size {
                break; // statement window already covered the table tail
            }
        }
        // Keyset pagination must be lossless: the rows exported from this
        // table must equal the count discovered during enumeration.
        if exported_for_table != shard_stat.row_count {
            return Err(format!(
                "pagination lost rows for {table} in {shard_name}: enumerated {} but exported {exported_for_table}",
                shard_stat.row_count
            ));
        }
    }
    records_file
        .sync_all()
        .map_err(|e| format!("records.jsonl fsync failed: {e}"))?;
    drop(records_file);

    // Media completeness is honest: true only when an attach root was
    // provided AND every reference resolved to staged bytes (no proven
    // gaps). Decode/output failures never reach this point — they abort the
    // export above. Kinds with no local bytes at all (emoji, link-only app
    // messages) are reported separately.
    let media_complete = args.attach_root.is_some() && stager.unavailable.is_empty();
    let media_staged_count = stager
        .staged
        .values()
        .filter(|v| v["present"] == json!(true))
        .count();
    let media_unavailable_count = stager.unavailable.len();
    let media_section = MediaSection {
        dir: "media",
        complete: media_complete,
        attach_root_provided: args.attach_root.is_some(),
        staged_count: media_staged_count,
        unavailable_count: media_unavailable_count,
        kinds_without_local_bytes: stager.kinds_without_local_bytes.clone(),
        notes: vec![
            "images are decoded Mac-side into usable bytes; encrypted .dat is never delivered as media",
            "decode/output failures and unreadable attach directories abort the export instead of producing gaps",
            "decoding keys stay Mac-side (0600 key file, never argv); voice blobs are raw SILK from snapshot media dbs",
            "image refs carry variant (original/thumbnail/derivative/unknown) with evidence; a staged thumbnail leaves original_present=false and media completeness false",
            "bounded work sets: media inputs are size-checked before reading, voice blobs before materialization, content cells pre-decode, and zstd expansion streams through a cap (machine-readable over-limit errors)",
        ],
        sample_unavailable: stager.unavailable.iter().take(20).cloned().collect(),
    };
    let media_dir_exists = args.out.join("media").exists();
    let export_manifest = ExportManifest {
        contract: CONTRACT,
        version: CONTRACT_VERSION,
        kind: "session_export",
        archive_id: archive_id.to_string(),
        account: account.to_string(),
        talker: conversation.talker.clone(),
        snapshot: SnapshotSection {
            generation: generation.manifest.generation.clone(),
            created_at: (generation.manifest.started_unix_ms / 1000) as u64,
            databases: generation
                .manifest
                .databases
                .iter()
                .map(|db| ExportManifestDb {
                    // Contract example pins databases[].file to the bare
                    // file name ("message_16.db").
                    file: Path::new(&db.rel)
                        .file_name()
                        .map(|n| n.to_string_lossy().to_string())
                        .unwrap_or_else(|| db.rel.clone()),
                    sha256: db.sha256.clone().unwrap_or_default(),
                    bytes: db.bytes.unwrap_or(0),
                })
                .collect(),
        },
        session: SessionSection {
            talker_name: conversation.talker.clone(),
            name_hash: conversation.name_hash.clone(),
            shards: conversation
                .shards
                .iter()
                .map(|s| ShardStat {
                    table: s.table.clone(),
                    database: s.shard.clone(),
                    row_count: s.row_count,
                })
                .collect(),
        },
        enumeration: EnumerationSection {
            complete: enumeration_ok,
            unknown_tables,
            unknown_columns,
        },
        records: RecordsSection {
            file: "records.jsonl",
            count,
        },
        media_dir: "media",
        ordering: "(shard,local_rowid) ascending",
        media: media_section,
    };
    let manifest_bytes = serde_json::to_vec_pretty(&export_manifest)
        .map_err(|e| format!("export manifest serialization failed: {e}"))?;
    atomic_private_write(&args.out.join("export.json"), &manifest_bytes)
        .map_err(|e| format!("export.json publication failed: {}", e.kind()))?;

    Ok(json!({
        "ok": true,
        "action": "export",
        "talker": conversation.talker,
        "generation": generation.manifest.generation,
        "records": count,
        "media_staged": media_staged_count,
        "media_unavailable": media_unavailable_count,
        "media_complete": media_complete,
        "enumeration_complete": enumeration_ok,
        "media_dir": media_dir_exists,
        "out": args.out,
    }))
}

// ── record building ─────────────────────────────────────────────────────

/// Owned copy of a rusqlite ValueRef (rows are consumed before building
/// records).
enum ValueRefOwned {
    Null,
    Integer(i64),
    Real(f64),
    Text(Vec<u8>),
    Blob(Vec<u8>),
}

impl From<ValueRef<'_>> for ValueRefOwned {
    fn from(value: ValueRef<'_>) -> Self {
        match value {
            ValueRef::Null => ValueRefOwned::Null,
            ValueRef::Integer(i) => ValueRefOwned::Integer(i),
            ValueRef::Real(f) => ValueRefOwned::Real(f),
            ValueRef::Text(t) => ValueRefOwned::Text(t.to_vec()),
            ValueRef::Blob(b) => ValueRefOwned::Blob(b.to_vec()),
        }
    }
}

/// Build the COMPLETE contract-v2 record for one row: real shard identity
/// (shard = database file, table separate, server_id as exact decimal
/// string), decoded searchable text PLUS the lossless `raw` object with
/// every original source column, media refs, and `record_sha256` over the
/// canonical JSON of everything except that field itself. Decode (zstd)
/// failure is a hard error.
#[allow(clippy::too_many_arguments)]
fn build_record(
    database: &str,
    table: &str,
    rowid: i64,
    select_columns: &[String],
    values: &[ValueRefOwned],
    schema: &TableSchema,
    limits: &ContentLimits,
    name2id: &HashMap<i64, String>,
    stager: &mut MediaStager<'_>,
) -> Result<Value, String> {
    let int_or_null = |index: usize| -> Value {
        match values.get(index) {
            Some(ValueRefOwned::Integer(i)) => json!(i),
            _ => Value::Null,
        }
    };
    let server_id: i64 = match values.get(IDX_SERVER_ID) {
        Some(ValueRefOwned::Integer(i)) => *i,
        _ => 0,
    };
    let local_type: i64 = match values.get(IDX_LOCAL_TYPE) {
        Some(ValueRefOwned::Integer(i)) => *i,
        _ => 0,
    };
    // Verified repo rule: msg_type = low 32 bits, sub_type = high 32 bits.
    let (msg_type, sub_type_u32) = split_local_type(local_type);

    // ── message_content: decoded searchable text + lossless raw bytes ──
    // Borrowed, not cloned: the row's cells are already bounded copies (the
    // SQLite length limit + row total check upstream); re-copying them here
    // would only double the transient working set for no value.
    let raw_content: Option<&[u8]> = match values.get(IDX_MESSAGE_CONTENT) {
        Some(ValueRefOwned::Text(t)) => Some(t.as_slice()),
        Some(ValueRefOwned::Blob(b)) => Some(b.as_slice()),
        _ => None,
    };
    let raw_is_blob = matches!(values.get(IDX_MESSAGE_CONTENT), Some(ValueRefOwned::Blob(_)));
    let wcdb_ct: Option<i64> = match values.get(IDX_FIRST_OPTIONAL) {
        Some(ValueRefOwned::Integer(i)) if schema.has_wcdb_ct => Some(*i),
        _ => None,
    };
    // Bounded work set: refuse the raw cell BEFORE decoding it — one row may
    // never force unbounded memory use.
    if let Some(raw) = &raw_content {
        if raw.len() > limits.max_cell_bytes {
            return Err(format!(
                "content_over_limit: message_content is {} bytes, exceeds --max-content-bytes {} \
                 for {table}#{rowid} in {database}",
                raw.len(),
                limits.max_cell_bytes
            ));
        }
    }
    let (decoded_text, decode_status) = match &raw_content {
        None => (None, "null"),
        Some(raw) => {
            let wcdb_ct_i32 = wcdb_ct.map(|v| v as i32);
            // Hard failure on decode error: never fabricate success on
            // undecodable content. Decompression streams through the
            // --max-decoded-bytes bound (never decode_all), so a compressed
            // bomb is refused after at most limit+1 decoded bytes.
            let (text, _was_zstd) =
                decode_content_lossless_bounded(raw, wcdb_ct_i32, limits.max_decoded_bytes)
                    .map_err(|e| {
                        format!(
                            "message_content decode failed for {table}#{rowid} in {database}: {e}"
                        )
                    })?;
            match text {
                Some(t) => (Some(t), "ok"),
                None => (None, "utf8_invalid"),
            }
        }
    };
    // Raw bytes are additionally preserved whenever the decoded text cannot
    // round-trip them exactly (BLOB storage, invalid UTF-8, compression).
    let raw_needs_preservation = match (&raw_content, &decoded_text) {
        (None, _) => false,
        (Some(raw), Some(text)) => raw_is_blob || text.as_bytes() != *raw,
        (Some(_), None) => true,
    };
    let message_content_raw_b64 = if raw_needs_preservation {
        raw_content.map(base64_encode)
    } else {
        None
    };

    // ── packed_info ──
    let packed: Option<&[u8]> = match values.get(IDX_PACKED_INFO) {
        Some(ValueRefOwned::Blob(b)) if !b.is_empty() => Some(b.as_slice()),
        Some(ValueRefOwned::Text(t)) if !t.is_empty() => Some(t.as_slice()),
        _ => None,
    };
    if let Some(packed) = &packed {
        if packed.len() > limits.max_cell_bytes {
            return Err(format!(
                "content_over_limit: packed_info_data is {} bytes, exceeds --max-content-bytes {} \
                 for {table}#{rowid} in {database}",
                packed.len(),
                limits.max_cell_bytes
            ));
        }
    }
    let packed_info_data = packed.map(base64_encode);
    let packed_info_sha256 = packed.map(sha256_hex);
    let packed_decoded = packed.and_then(decode_packed_info);

    // ── optional known columns ──
    // Positions are CUMULATIVE over the schema flags, mirroring exactly how
    // the SELECT was assembled (wcdb_ct, then compress, then sender — each
    // only when its column exists). A missing column occupies NO slot, so
    // any unconditional +1 would read the wrong cell (e.g. a table without
    // WCDB_CT puts compress_content at IDX_FIRST_OPTIONAL itself).
    let wcdb_offset = usize::from(schema.has_wcdb_ct);
    let compress_content: Option<&[u8]> = if schema.has_compress {
        match values.get(IDX_FIRST_OPTIONAL + wcdb_offset) {
            Some(ValueRefOwned::Blob(b)) if !b.is_empty() => Some(b.as_slice()),
            _ => None,
        }
    } else {
        None
    };
    if let Some(compress) = &compress_content {
        if compress.len() > limits.max_cell_bytes {
            return Err(format!(
                "content_over_limit: compress_content is {} bytes, exceeds --max-content-bytes {} \
                 for {table}#{rowid} in {database}",
                compress.len(),
                limits.max_cell_bytes
            ));
        }
    }
    let sender_offset = wcdb_offset + usize::from(schema.has_compress);
    let real_sender_id: Option<i64> = if schema.has_real_sender {
        match values.get(IDX_FIRST_OPTIONAL + sender_offset) {
            Some(ValueRefOwned::Integer(i)) => Some(*i),
            _ => None,
        }
    } else {
        None
    };
    let real_sender_name = real_sender_id.and_then(|id| name2id.get(&id).cloned());

    // ── media references (existing local media capability, honest gaps) ──
    let mut media_refs: Vec<Value> = Vec::new();
    match msg_type {
        MSG_TYPE_IMAGE => match packed_decoded.as_ref().and_then(|p| p.image_md5.clone()) {
            Some(md5) => media_refs.push(stager.stage_image(&md5)?),
            None => {
                stager.record_gap("image_no_packed_md5");
            }
        },
        MSG_TYPE_VIDEO => match packed_decoded.as_ref().and_then(|p| p.video_md5.clone()) {
            Some(md5) => media_refs.push(stager.stage_video(&md5)?),
            None => {
                stager.record_gap("video_no_packed_md5");
            }
        },
        MSG_TYPE_VOICE => {
            if server_id > 0 {
                media_refs.push(stager.stage_voice(server_id)?);
            } else {
                stager.record_gap("voice_without_server_id");
            }
        }
        MSG_TYPE_APP => {
            // App/file messages: try the packed md5 marker (the verified
            // marker-scan capability); link-only app messages have no local
            // bytes and are recorded as a gap, never a fake ref.
            let marker_md5 = packed.and_then(extract_md5_from_packed_info);
            match marker_md5 {
                Some(md5) => media_refs.push(stager.stage_file(&md5)?),
                None => stager.record_gap("app_without_local_file_ref"),
            }
        }
        _ => {
            // Emoji/location/system/revoke/text: payload is inline (XML or
            // text) — no separate local media bytes exist for these kinds.
        }
    }

    // ── `raw`: EVERY original source column, lossless (contract v2) ──
    // values[0] is rowid (captured in identity); values[1..] align with
    // select_columns.
    let mut raw = Map::new();
    for (index, column) in select_columns.iter().enumerate() {
        raw.insert(
            column.clone(),
            raw_column_value(values.get(index + 1)),
        );
    }

    // ── assemble ──
    let identity = json!({
        // Contract v2: shard is the DATABASE file; table is separate so the
        // same Msg table across shards never collides on rowid.
        "shard": database,
        "table": table,
        "database": database,
        "local_rowid": rowid,
        // Exact decimal string: server_id values exceed 2^53 and must
        // survive any JSON number coercion losslessly.
        "server_id": if server_id != 0 { json!(server_id.to_string()) } else { Value::Null },
    });
    let mut record = Map::new();
    record.insert("identity".into(), identity);
    record.insert("create_time".into(), int_or_null(IDX_CREATE_TIME));
    record.insert("sort_seq".into(), int_or_null(IDX_SORT_SEQ));
    record.insert("local_type".into(), int_or_null(IDX_LOCAL_TYPE));
    record.insert("sub_type".into(), json!(sub_type_u32));
    record.insert("status".into(), int_or_null(IDX_STATUS));
    record.insert(
        "message_content".into(),
        decoded_text.map(Value::from).into(),
    );
    record.insert(
        "message_content_raw_b64".into(),
        message_content_raw_b64.map(Value::from).into(),
    );
    record.insert(
        "message_content_raw_type".into(),
        match values.get(IDX_MESSAGE_CONTENT) {
            Some(ValueRefOwned::Text(_)) => json!("text"),
            Some(ValueRefOwned::Blob(_)) => json!("blob"),
            _ => Value::Null,
        },
    );
    record.insert("message_content_decode_status".into(), json!(decode_status));
    if schema.has_wcdb_ct {
        record.insert("wcdb_ct".into(), wcdb_ct.map(Value::from).into());
    }
    if schema.has_compress {
        record.insert(
            "compress_content_b64".into(),
            compress_content.map(base64_encode).map(Value::from).into(),
        );
    }
    if schema.has_real_sender {
        record.insert("real_sender_id".into(), real_sender_id.map(Value::from).into());
        record.insert(
            "real_sender_name".into(),
            real_sender_name.map(Value::from).into(),
        );
    }
    record.insert("packed_info_data".into(), packed_info_data.into());
    record.insert(
        "packed_info_sha256".into(),
        packed_info_sha256.map(Value::from).into(),
    );
    record.insert("media_refs".into(), Value::Array(media_refs));
    record.insert("raw".into(), Value::Object(raw));

    // Fingerprint over the canonical JSON minus the field itself.
    let without_fp = Value::Object(record.clone());
    let record_sha256 = sha256_hex(canonical_json(&without_fp).as_bytes());
    record.insert("record_sha256".into(), json!(record_sha256));
    Ok(Value::Object(record))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn canonical_json_matches_python_shape() {
        // Python: json.dumps({"a":1,"b":"π","c":None,"d":[1,2]},
        //   ensure_ascii=False, sort_keys=True, separators=(',',':'))
        // == '{"a":1,"b":"π","c":null,"d":[1,2]}'
        let value = json!({"b": "π", "a": 1, "c": null, "d": [1, 2]});
        assert_eq!(canonical_json(&value), r#"{"a":1,"b":"π","c":null,"d":[1,2]}"#);
    }

    #[test]
    fn base64_encoder_matches_standard_alphabet() {
        assert_eq!(base64_encode(b""), "");
        assert_eq!(base64_encode(b"f"), "Zg==");
        assert_eq!(base64_encode(b"fo"), "Zm8=");
        assert_eq!(base64_encode(b"foo"), "Zm9v");
        assert_eq!(base64_encode(b"foobar"), "Zm9vYmFy");
    }

    #[test]
    fn identifier_quoting_doubles_embedded_quotes() {
        assert_eq!(quote_ident("Msg_abc"), "\"Msg_abc\"");
        assert_eq!(quote_ident("weird\"name"), "\"weird\"\"name\"");
    }

    #[test]
    fn message_shard_classification_is_strict() {
        assert!(is_message_shard_name("message_0.db"));
        assert!(is_message_shard_name("message_16.db"));
        assert!(!is_message_shard_name("message_fts.db"));
        assert!(!is_message_shard_name("message_resource.db"));
        assert!(!is_message_shard_name("message.db"));
        assert!(!is_message_shard_name("message_.db"));
        assert!(!is_message_shard_name("message_x.db"));
    }

    #[test]
    fn raw_column_encoding_is_lossless() {
        assert_eq!(raw_column_value(None), Value::Null);
        assert_eq!(raw_column_value(Some(&ValueRefOwned::Null)), Value::Null);
        assert_eq!(
            raw_column_value(Some(&ValueRefOwned::Integer(-42))),
            json!(-42)
        );
        assert_eq!(
            raw_column_value(Some(&ValueRefOwned::Text(b"hi".to_vec()))),
            json!("hi")
        );
        // Invalid UTF-8 TEXT keeps its bytes via base64, flagged as such.
        let invalid = ValueRefOwned::Text(vec![0xff, 0xfe]);
        let encoded = raw_column_value(Some(&invalid));
        assert_eq!(encoded["utf8"], json!(false));
        let b64 = encoded["b64"].as_str().unwrap();
        assert_eq!(b64, base64_encode(&[0xff, 0xfe]));
        // BLOBs are always base64-wrapped (empty blob ≠ null).
        let blob = ValueRefOwned::Blob(vec![]);
        assert_eq!(raw_column_value(Some(&blob)), json!({"b64": ""}));
    }

    #[test]
    fn raw_real_columns_stay_cross_language_canonical() {
        // Plain decimal REALs (including integral REALs) stay plain numbers:
        // serde_json and Python json.dumps emit identical bytes for these.
        for value in [2.25f64, 0.1, 0.001, 5.0, -0.5, 123456789012345.6] {
            let encoded = raw_column_value(Some(&ValueRefOwned::Real(value)));
            assert_eq!(encoded, json!(value), "value {value} must stay a number");
            let text = serde_json::to_string(&encoded).unwrap();
            assert!(!text.contains('e') && !text.contains('E'));
        }
        // Exponent-form REALs are tagged as strings: Python formats 1e300
        // as "1e+300", so a plain number would break the shared
        // record_sha256 canonicalization.
        for value in [1e300f64, 1e-7, 1e16] {
            let encoded = raw_column_value(Some(&ValueRefOwned::Real(value)));
            let text = serde_json::to_string(&encoded).unwrap();
            assert!(text.contains("\"real\""), "{value} must be tagged, got {text}");
        }
        // Non-finite REALs (defensive; SQLite stores NaN as NULL) are tagged
        // too — a JSON number cannot represent them.
        let nan = raw_column_value(Some(&ValueRefOwned::Real(f64::NAN)));
        assert_eq!(nan, json!({"real": "nan"}));
        let inf = raw_column_value(Some(&ValueRefOwned::Real(f64::INFINITY)));
        assert_eq!(inf, json!({"real": "inf"}));
    }

    #[test]
    fn dat_key_file_fails_closed_without_echoing_the_key() {
        let tmp = tempfile::TempDir::new().unwrap();
        let kf = tmp.path().join("dat-key.json");
        let secret = "0123456789abcdef".repeat(2);

        // Wrong length: valid hex, but not 16 bytes.
        std::fs::write(&kf, format!("{{\"v2_aes_key\":\"{}\"}}", "0123")).unwrap();
        std::fs::set_permissions(&kf, std::fs::Permissions::from_mode(0o600)).unwrap();
        let err = load_dat_key(&kf).unwrap_err();
        assert!(err.contains("exactly 16 bytes"), "{err}");

        // Invalid hex.
        std::fs::write(&kf, format!("{{\"v2_aes_key\":\"{}\"}}", "zz".repeat(16))).unwrap();
        assert!(load_dat_key(&kf).unwrap_err().contains("not valid hex"));

        // Missing field.
        std::fs::write(&kf, "{}").unwrap();
        assert!(load_dat_key(&kf).unwrap_err().contains("v2_aes_key"));

        // Loose permissions.
        std::fs::write(&kf, format!("{{\"v2_aes_key\":\"{secret}\"}}")).unwrap();
        std::fs::set_permissions(&kf, std::fs::Permissions::from_mode(0o644)).unwrap();
        assert!(load_dat_key(&kf).unwrap_err().contains("0600"));

        // Symlink.
        let target = tmp.path().join("real-key.json");
        std::fs::write(&target, format!("{{\"v2_aes_key\":\"{secret}\"}}")).unwrap();
        std::fs::set_permissions(&target, std::fs::Permissions::from_mode(0o600)).unwrap();
        let _ = std::fs::remove_file(&kf);
        std::os::unix::fs::symlink(&target, &kf).unwrap();
        assert!(load_dat_key(&kf).unwrap_err().contains("symlink"));

        // Valid synthetic key loads; no error path ever echoed the value.
        let _ = std::fs::remove_file(&kf);
        std::fs::write(&kf, format!("{{\"v2_aes_key\":\"{secret}\"}}")).unwrap();
        std::fs::set_permissions(&kf, std::fs::Permissions::from_mode(0o600)).unwrap();
        let key = load_dat_key(&kf).unwrap();
        assert_eq!(key.len(), 16);
    }

    #[test]
    fn select_index_constants_match_documented_order() {
        // rowid, sort_seq, server_id, local_type, create_time, status,
        // message_content, packed_info_data, [wcdb], [compress], [sender]
        assert_eq!(IDX_ROWID, 0);
        assert_eq!(IDX_SORT_SEQ, 1);
        assert_eq!(IDX_SERVER_ID, 2);
        assert_eq!(IDX_LOCAL_TYPE, 3);
        assert_eq!(IDX_CREATE_TIME, 4);
        assert_eq!(IDX_STATUS, 5);
        assert_eq!(IDX_MESSAGE_CONTENT, 6);
        assert_eq!(IDX_PACKED_INFO, 7);
        assert_eq!(IDX_FIRST_OPTIONAL, 8);
    }

    #[test]
    fn content_limits_reject_row_cap_below_cell_cap() {
        // A row cap smaller than the cell cap would reject every legal
        // content cell as an over-limit row — fail closed with a
        // configuration error naming both flags.
        let err = ContentLimits::from_args(4096, 1024, 1024).unwrap_err();
        assert!(
            err.contains("--max-row-bytes") && err.contains("--max-content-bytes"),
            "{err}"
        );
        // At exactly the cell cap the configuration is accepted.
        assert!(ContentLimits::from_args(4096, 1024, 4096).is_ok());
    }

    #[test]
    fn content_limits_derive_json_cap_and_clamp_sqlite_limit() {
        let limits = ContentLimits::from_args(1024, 2048, 4096).unwrap();
        // Each raw cell is repeated at most once (<= 6x under JSON control
        // escaping) and the decoded text contributes <= 6x of the decode
        // cap, plus fixed field overhead.
        assert_eq!(limits.max_json_line_bytes, 6 * (4096 + 2048) + 65536);
        assert_eq!(limits.sqlite_length_limit(), 4096);
        // Row caps beyond the C API's i32 clamp instead of wrapping.
        let huge =
            ContentLimits::from_args(i32::MAX as usize, 0, i32::MAX as usize + 10).unwrap();
        assert_eq!(huge.sqlite_length_limit(), i32::MAX);
    }

    #[test]
    fn value_ref_byte_len_counts_only_text_and_blob_payloads() {
        // Fixed-size scalars contribute nothing to the row byte total; only
        // TEXT/BLOB payloads count.
        assert_eq!(value_ref_byte_len(&ValueRef::Null), 0);
        assert_eq!(value_ref_byte_len(&ValueRef::Integer(-5)), 0);
        assert_eq!(value_ref_byte_len(&ValueRef::Real(1.5)), 0);
        assert_eq!(value_ref_byte_len(&ValueRef::Text(b"0123456789abcdef")), 16);
        // An empty BLOB is a real value (never null) but carries no payload.
        assert_eq!(value_ref_byte_len(&ValueRef::Blob(b"")), 0);
        assert_eq!(value_ref_byte_len(&ValueRef::Blob(&[1u8; 9])), 9);
    }

    #[test]
    fn row_width_probe_sql_sums_octet_length_of_every_column() {
        let sql = row_width_probe_sql(
            "Msg_abc",
            &["sort_seq".into(), "message_content".into(), "weird\"col".into()],
            7,
        );
        assert_eq!(
            sql,
            "SELECT rowid, (COALESCE(octet_length(\"sort_seq\"),0) + \
             COALESCE(octet_length(\"message_content\"),0) + \
             COALESCE(octet_length(\"weird\"\"col\"),0)) FROM \"Msg_abc\" \
             WHERE rowid > ?1 ORDER BY rowid ASC LIMIT 7"
        );
    }

    #[test]
    fn octet_length_probe_is_header_only_on_the_bundled_engine() {
        // The decisive engine-level proof for the probe: under a connection
        // running SQLITE_LIMIT_LENGTH, a payload read of an over-limit cell
        // fails with SQLITE_TOOBIG while the octet_length probe over the
        // SAME cell succeeds — i.e. octet_length is answered from the
        // record header without materializing the payload. Plus EXPLAIN
        // evidence: every column term compiles to an OP_Column carrying
        // OPFLAG_BYTELENARG (0xc0) in p5.
        let conn = Connection::open_in_memory().unwrap();
        // Write the oversized cell BEFORE tightening the limit (the length
        // limit governs record writes too, same as reads).
        conn.execute_batch(
            "CREATE TABLE t(rowid INTEGER PRIMARY KEY, body BLOB, note TEXT);
             INSERT INTO t VALUES (1, zeroblob(16*1024*1024), 'tiny');
             INSERT INTO t VALUES (2, NULL, '三字节字符');",
        )
        .unwrap();
        conn.set_limit(Limit::SQLITE_LIMIT_LENGTH, 1024 * 1024)
            .unwrap();
        // Payload read: refused by the engine's limit.
        let err = conn.query_row("SELECT body FROM t", [], |_| Ok(())).unwrap_err();
        assert!(is_sqlite_toobig(&err), "expected TOOBIG, got: {err}");
        // Probe over the same cell: answered from the header.
        let octets: i64 = conn
            .query_row(
                "SELECT COALESCE(octet_length(body),0) FROM t WHERE rowid = 1",
                [],
                |r| r.get(0),
            )
            .unwrap();
        assert_eq!(octets, 16 * 1024 * 1024);
        // UTF-8 TEXT counts exact BYTES, not characters.
        let text_octets: i64 = conn
            .query_row(
                "SELECT COALESCE(octet_length(note),0) FROM t WHERE rowid = 2",
                [],
                |r| r.get(0),
            )
            .unwrap();
        assert_eq!(text_octets, 15); // 5 chars x 3 UTF-8 bytes
        // EXPLAIN: each column term is an OP_Column with OPFLAG_BYTELENARG
        // (EXPLAIN columns: addr, opcode, p1..p4, p5, comment — p5 is index 6).
        let plan: Vec<(String, i64)> = conn
            .prepare("EXPLAIN SELECT rowid, COALESCE(octet_length(body),0) + COALESCE(octet_length(note),0) FROM t")
            .unwrap()
            .query_map([], |r| Ok((r.get::<_, String>(1)?, r.get::<_, i64>(6)?)))
            .unwrap()
            .collect::<Result<_, _>>()
            .unwrap();
        let bytelen_columns = plan
            .iter()
            .filter(|(op, p5)| op == "Column" && (p5 & 0xc0) == 0xc0)
            .count();
        assert!(
            bytelen_columns >= 2,
            "probe columns must compile to header-only OP_Column, plan: {plan:?}"
        );
    }

    #[test]
    fn toobig_classification_matches_only_sqlite_toobig() {
        let toobig = rusqlite::Error::SqliteFailure(
            rusqlite::ffi::Error {
                code: rusqlite::ffi::ErrorCode::TooBig,
                extended_code: rusqlite::ffi::SQLITE_TOOBIG,
            },
            None,
        );
        assert!(is_sqlite_toobig(&toobig));
        let busy = rusqlite::Error::SqliteFailure(
            rusqlite::ffi::Error {
                code: rusqlite::ffi::ErrorCode::DatabaseBusy,
                extended_code: rusqlite::ffi::SQLITE_BUSY,
            },
            None,
        );
        assert!(!is_sqlite_toobig(&busy));
        assert!(!is_sqlite_toobig(&rusqlite::Error::QueryReturnedNoRows));
    }

    #[test]
    fn image_variant_classification_follows_media_v1_tiers() {
        let md5 = "abcd1234";
        // Original tier: exact name and the `_h` high-quality variant.
        assert_eq!(classify_image_variant("abcd1234.dat", md5), "original");
        assert_eq!(classify_image_variant("abcd1234_h.dat", md5), "original");
        // Thumbnail tier: never the original.
        assert_eq!(classify_image_variant("abcd1234_t.dat", md5), "thumbnail");
        // Same-md5 derivatives are labeled as such; anything else is unknown.
        assert_eq!(classify_image_variant("abcd1234_hd.dat", md5), "derivative");
        assert_eq!(classify_image_variant("other-name.dat", md5), "unknown");
    }

    #[test]
    fn bounded_read_rejects_over_limit_inputs_before_reading() {
        let tmp = tempfile::TempDir::new().unwrap();
        let big = tmp.path().join("big.dat");
        std::fs::write(&big, vec![0u8; 4096]).unwrap();
        // Over-limit inputs are refused with the machine-readable token and
        // the real byte count — before a single byte is read.
        let err = read_bounded(&big, 1024).unwrap_err();
        assert!(err.contains("media_input_over_limit"), "{err}");
        assert!(err.contains("4096"), "{err}");
        // At and below the limit, reads succeed and return exactly the file.
        assert_eq!(read_bounded(&big, 4096).unwrap().len(), 4096);
        assert_eq!(read_bounded(&big, 8192).unwrap().len(), 4096);
    }
}
