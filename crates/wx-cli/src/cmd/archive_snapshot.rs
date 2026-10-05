//! `wx-cli archive-snapshot`: consistent encrypted snapshots of every WeChat
//! source database into a fresh, private generation directory — supervised
//! by a parent watchdog so the live source is never held locked longer than
//! T_max.
//!
//! Process model (round-5 locked design):
//!
//! * the parent validates paths/keys fail-closed, enumerates source `.db`
//!   files, and for each one spawns a dedicated worker subprocess
//!   (`__archive-snapshot-worker`, same binary);
//! * the worker opens the source READONLY (key check only, no locks),
//!   emits a `pinning` event on a run-scoped event file and then blocks on
//!   its stdin until the parent writes an ack byte; only after that
//!   confirmation does it BEGIN the deferred read transaction, so the
//!   parent's monotonic clock (started at the ack write) provably covers the
//!   entire possible lock-holding window. It runs the same-key/salt/
//!   page-params encrypted backup under a monotonic deadline, emits
//!   `released` the moment the transaction ends, then verifies and publishes
//!   the static copy offline;
//! * the parent brackets exactly the possible lock-holding window with a
//!   monotonic clock: startup (spawn → `pinning`) gets `grace`, the
//!   transaction window (`pinning` → `released`) gets exactly `t_max` (plus
//!   the documented polling slack), post-release verification/publish gets
//!   `grace`. Exceeding any budget kills the worker — the OS then closes its
//!   file descriptors, releasing every source lock;
//! * events carry a per-run `run_id` and the parent truncates the event file
//!   before spawning, so stale events from an earlier run can never cancel
//!   monitoring;
//! * after a kill the parent removes the candidate and deletes any published
//!   file this run cannot trust (no `published` event), so half-finished
//!   products never survive;
//! * the generation manifest is serialized completely first, then written
//!   through `wx_db::snapshot::atomic_private_write` (0600 temp + fsync +
//!   atomic no-clobber link + dir fsync). `manifest.json` exists only for a
//!   fully complete generation; failures produce `manifest.partial.json`
//!   with machine-readable error kinds, and the process exits non-zero.
//!
//! No secrets or chat content appear in events, reports, or errors: key
//! material lives only in the 0600 key file (never on argv), and JSON parse
//! errors report position only, never the parsed text.
use std::io::{Read, Write};
use std::os::unix::fs::PermissionsExt;
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::time::{Duration, Instant};

use clap::Args;
use serde::{Deserialize, Serialize};
use wx_db::snapshot::{
    atomic_private_write, ensure_private_dir, enumerate_source_databases, open_snapshot_copy,
    publish_snapshot, verify_snapshot, BackupOptions, OpenedSource, SnapshotError, SnapshotKeys,
    SnapshotMeta,
};

/// Parent poll cadence for worker events and exit status.
const POLL_INTERVAL: Duration = Duration::from_millis(10);

// ── CLI surface ─────────────────────────────────────────────────────────

#[derive(Args, Debug)]
pub struct ArchiveSnapshotArgs {
    /// WeChat `db_storage` source directory (live, encrypted)
    #[arg(long)]
    pub source: PathBuf,

    /// Fresh generation directory to create (must not exist; never inside
    /// the source, and never a parent of the source)
    #[arg(long)]
    pub out: PathBuf,

    /// 0600 JSON key file: {"raw_key":"<64 hex>"} or
    /// {"derived":[{"key":"<64 hex>","salt":"<32 hex>"}]}
    #[arg(long)]
    pub key_file: PathBuf,

    /// Hard budget (ms) for each source read transaction (pinning→released)
    #[arg(long, default_value_t = 30_000)]
    pub t_max_ms: u64,

    /// Budget (ms) for startup and for post-release verification/publish,
    /// both strictly OUTSIDE the source transaction window
    #[arg(long, default_value_t = 60_000)]
    pub grace_ms: u64,

    /// Pages copied per backup step inside the transaction
    #[arg(long, default_value_t = 128)]
    pub pages_per_step: usize,

    /// Print the machine-readable JSON report on stdout (default: text)
    #[arg(long)]
    pub json: bool,
}

#[derive(Args, Debug)]
pub struct ArchiveSnapshotWorkerArgs {
    /// Source database to snapshot (absolute path)
    #[arg(long)]
    pub db: PathBuf,

    /// Candidate path (created O_EXCL 0600 by the snapshot core)
    #[arg(long)]
    pub candidate: PathBuf,

    /// Final published path inside the generation directory
    #[arg(long)]
    pub publish: PathBuf,

    /// 0600 key file (same format as the parent command)
    #[arg(long)]
    pub key_file: PathBuf,

    /// Hard budget (ms) for the transaction window
    #[arg(long)]
    pub t_max_ms: u64,

    /// Pages copied per backup step
    #[arg(long, default_value_t = 128)]
    pub pages_per_step: usize,

    /// Run-scoped event file (JSON lines, appended)
    #[arg(long)]
    pub events: PathBuf,

    /// Run id; events without this id are ignored
    #[arg(long)]
    pub run_id: u64,
}

// ── key file ────────────────────────────────────────────────────────────

#[derive(Deserialize)]
struct KeyFile {
    raw_key: Option<String>,
    derived: Option<Vec<DerivedPair>>,
}

#[derive(Deserialize)]
struct DerivedPair {
    key: String,
    salt: String,
}

fn hex_bytes(s: &str, expect: usize, what: &str) -> Result<Vec<u8>, String> {
    let trimmed = s.trim();
    if trimmed.len() != expect * 2 {
        return Err(format!(
            "key file field {what} must be exactly {expect} bytes of hex"
        ));
    }
    hex::decode(trimmed).map_err(|_| format!("key file field {what} is not valid hex"))
}

/// Load snapshot keys from a 0600 JSON key file. Errors never include the
/// file contents or any parsed value.
pub(crate) fn load_keys(path: &Path) -> Result<SnapshotKeys, String> {
    let meta = std::fs::symlink_metadata(path)
        .map_err(|e| format!("key file {} unreadable: {e}", path.display()))?;
    if meta.file_type().is_symlink() {
        return Err("key file must be a regular file, not a symlink".into());
    }
    if !meta.is_file() {
        return Err("key file must be a regular file".into());
    }
    if PermissionsExt::mode(&meta.permissions()) & 0o077 != 0 {
        return Err("key file must have mode 0600 (no group/other access)".into());
    }
    let text = std::fs::read_to_string(path)
        .map_err(|e| format!("key file read failed: {e}"))?;
    let parsed: KeyFile = serde_json::from_str(&text)
        .map_err(|e| format!("key file is not valid JSON: {e}"))?;
    if let Some(raw) = parsed.raw_key.as_deref() {
        let key = hex_bytes(raw, 32, "raw_key")?;
        let mut key_arr = [0u8; 32];
        key_arr.copy_from_slice(&key);
        return Ok(SnapshotKeys::from_raw(key_arr));
    }
    if let Some(pairs) = parsed.derived.as_deref() {
        let mut converted = Vec::with_capacity(pairs.len());
        for pair in pairs {
            let key = hex_bytes(&pair.key, 32, "derived[].key")?;
            let salt = hex_bytes(&pair.salt, 16, "derived[].salt")?;
            let mut key_arr = [0u8; 32];
            let mut salt_arr = [0u8; 16];
            key_arr.copy_from_slice(&key);
            salt_arr.copy_from_slice(&salt);
            converted.push(wx_decrypt::EncKeyPair {
                key: key_arr,
                salt: salt_arr,
            });
        }
        if converted.is_empty() {
            return Err("key file has an empty derived list".into());
        }
        return Ok(SnapshotKeys::from_derived_pairs(&converted));
    }
    Err("key file must contain either raw_key or a non-empty derived list".into())
}

// ── run-scoped events ───────────────────────────────────────────────────

#[derive(Debug, PartialEq, Eq, Clone, Copy)]
enum WorkerEvent {
    Pinning,
    Pinned,
    Released,
    Published,
}

impl WorkerEvent {
    fn as_str(self) -> &'static str {
        match self {
            WorkerEvent::Pinning => "pinning",
            WorkerEvent::Pinned => "pinned",
            WorkerEvent::Released => "released",
            WorkerEvent::Published => "published",
        }
    }
}

/// Parse one event-file line for `run_id`. Lines from any other run (stale
/// leftovers from a previous attempt) are ignored — they must never be able
/// to cancel monitoring of the current run.
fn parse_event_line(line: &str, run_id: u64) -> Option<WorkerEvent> {
    let value: serde_json::Value = serde_json::from_str(line).ok()?;
    if value.get("run_id")?.as_u64()? != run_id {
        return None;
    }
    match value.get("event")?.as_str()? {
        "pinning" => Some(WorkerEvent::Pinning),
        "pinned" => Some(WorkerEvent::Pinned),
        "released" => Some(WorkerEvent::Released),
        "published" => Some(WorkerEvent::Published),
        _ => None,
    }
}

fn append_event(path: &Path, run_id: u64, event: WorkerEvent) -> Result<(), String> {
    let line = format!(
        "{{\"run_id\":{run_id},\"event\":\"{}\"}}\n",
        event.as_str()
    );
    let mut file = std::fs::OpenOptions::new()
        .append(true)
        .create(true)
        .open(path)
        .map_err(|e| format!("event append failed: {e}"))?;
    file.write_all(line.as_bytes())
        .map_err(|e| format!("event append failed: {e}"))?;
    file.sync_data()
        .map_err(|e| format!("event fsync failed: {e}"))
}

/// Incrementally read new complete lines from the event file starting at
/// `offset`, returning parsed events for this run and the new offset.
fn drain_events(
    path: &Path,
    offset: &mut usize,
    run_id: u64,
) -> Result<Vec<WorkerEvent>, String> {
    let mut file = std::fs::File::open(path).map_err(|e| format!("event read failed: {e}"))?;
    let mut buf = Vec::new();
    file.read_to_end(&mut buf)
        .map_err(|e| format!("event read failed: {e}"))?;
    if buf.len() < *offset {
        // The file was replaced (never expected — the parent owns it); treat
        // everything as new rather than trusting stale offsets.
        *offset = 0;
    }
    let mut events = Vec::new();
    let mut start = *offset;
    let mut cursor = *offset;
    while let Some(pos) = buf[cursor..].iter().position(|&b| b == b'\n') {
        cursor += pos + 1;
        let line = std::str::from_utf8(&buf[start..cursor - 1])
            .map_err(|_| "event file contains invalid UTF-8".to_string())?;
        if let Some(event) = parse_event_line(line, run_id) {
            events.push(event);
        }
        start = cursor;
    }
    *offset = cursor;
    Ok(events)
}

// ── fail-closed path validation ─────────────────────────────────────────

/// True when every component of `path` (up to `upto`, inclusive) is a real
/// directory/file — no symlinks, except the macOS standard roots allowed by
/// `wx_db::snapshot::ensure_private_dir`.
fn components_are_real(path: &Path) -> Result<bool, String> {
    let mut current = PathBuf::new();
    for component in path.components() {
        current.push(component);
        let meta = match std::fs::symlink_metadata(&current) {
            Ok(meta) => meta,
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => {
                return Err(format!("path component {} does not exist", current.display()))
            }
            Err(e) => return Err(format!("stat {} failed: {e}", current.display())),
        };
        if meta.file_type().is_symlink() {
            // Only the macOS standard roots (/var, /tmp, /etc) are tolerated,
            // and ensure_private_dir re-checks them; anything else fails.
            let resolved = std::fs::canonicalize(&current).map_err(|e| e.to_string())?;
            let allowed = ["/var", "/tmp", "/etc"].iter().any(|std_root| {
                current == Path::new(std_root)
                    && resolved == Path::new(std_root).canonicalize().unwrap_or_default()
            });
            if !allowed {
                return Err(format!(
                    "path component {} is a symlink; source/output paths must be real directories",
                    current.display()
                ));
            }
        }
    }
    Ok(true)
}

/// Overlap and freshness checks between the source tree and the output
/// generation. All failures are hard errors.
fn validate_paths(source: &Path, out: &Path) -> Result<PathBuf, String> {
    // Check the path AS GIVEN (made absolute) before canonicalizing: a
    // symlink alias would otherwise resolve away and escape the check.
    let source_lex = absolute(source);
    components_are_real(&source_lex)?;
    let source_abs = std::fs::canonicalize(&source_lex)
        .map_err(|e| format!("source {} not accessible: {e}", source.display()))?;
    if !source_abs.is_dir() {
        return Err(format!("source {} is not a directory", source_abs.display()));
    }
    components_are_real(&source_abs)?;

    let out_abs = absolute(out);
    if let Ok(meta) = std::fs::symlink_metadata(&out_abs) {
        if meta.file_type().is_symlink() {
            return Err(format!(
                "output {} is a symlink; generations must be real directories",
                out_abs.display()
            ));
        }
    }
    // Validate the existing ancestors of the output path.
    if let Some(parent) = out_abs.parent() {
        components_are_real(parent)?;
    }
    // Resolve the output to its REAL path (canonical existing ancestors +
    // the not-yet-created tail) before overlap checks: comparing a
    // canonicalized source against a lexically-spelled output let macOS
    // aliases (/var vs /private/var) smuggle the output INTO the source
    // tree. Both sides must be real paths. Overlap violations are reported
    // BEFORE the freshness check so the boundary error is never masked by
    // "already exists".
    let out_real = real_path(&out_abs);
    if source_abs == out_real {
        return Err("source and output are the same directory".into());
    }
    if out_real.starts_with(&source_abs) {
        return Err(format!(
            "output {} is inside the source tree {}",
            out_real.display(),
            source_abs.display()
        ));
    }
    if source_abs.starts_with(&out_real) {
        return Err(format!(
            "output {} contains the source tree {}; snapshots must not nest",
            out_real.display(),
            source_abs.display()
        ));
    }
    if std::fs::symlink_metadata(&out_abs).is_ok() {
        return Err(format!(
            "output {} already exists; generations are immutable and never overwritten",
            out_abs.display()
        ));
    }
    Ok(source_abs)
}

/// Resolve `path` to its real on-disk location: canonicalize the deepest
/// EXISTING ancestor and append the not-yet-created tail unchanged. This
/// defeats alias spellings (/var vs /private/var) for paths that do not
/// exist yet, which `canonicalize` alone cannot handle.
fn real_path(path: &Path) -> PathBuf {
    let mut prefix = path.to_path_buf();
    let mut tail: Vec<std::ffi::OsString> = Vec::new();
    loop {
        if let Ok(real) = std::fs::canonicalize(&prefix) {
            let mut resolved = real;
            for component in tail.iter().rev() {
                resolved.push(component);
            }
            return resolved;
        }
        match (prefix.parent(), prefix.file_name()) {
            (Some(parent), Some(name)) => {
                tail.push(name.to_os_string());
                prefix = parent.to_path_buf();
            }
            _ => return path.to_path_buf(),
        }
    }
}

fn absolute(path: &Path) -> PathBuf {
    if path.is_absolute() {
        normalize(path)
    } else {
        let cwd = std::env::current_dir().unwrap_or_else(|_| PathBuf::from("."));
        normalize(&cwd.join(path))
    }
}

/// Lexically normalize `..` and `.` without touching the filesystem, so the
/// overlap checks cannot be fooled by non-canonical spellings of paths that
/// do not exist yet.
fn normalize(path: &Path) -> PathBuf {
    let mut out = PathBuf::new();
    for component in path.components() {
        match component {
            std::path::Component::CurDir => {}
            std::path::Component::ParentDir => {
                out.pop();
            }
            other => out.push(other),
        }
    }
    out
}

// ── reports ─────────────────────────────────────────────────────────────

#[derive(Serialize)]
struct ManifestDb {
    db: String,
    ok: bool,
    #[serde(skip_serializing_if = "Option::is_none")]
    kind: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    sha256: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    bytes: Option<u64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pages: Option<usize>,
    #[serde(skip_serializing_if = "Option::is_none")]
    elapsed_ms: Option<u128>,
    /// Additive: how far past the exact deadline the kill decision fired
    /// (poll + OS scheduling latency). Reported honestly; never budgeted.
    #[serde(skip_serializing_if = "Option::is_none")]
    kill_overshoot_ms: Option<u128>,
    /// Additive: wall time of the kill() syscall itself.
    #[serde(skip_serializing_if = "Option::is_none")]
    kill_syscall_ms: Option<u128>,
}

#[derive(Serialize)]
struct Manifest {
    complete: bool,
    /// Generation id: the output directory's name. Consumers (S3 export,
    /// NAS collector) pin every derived artifact to this id.
    generation: String,
    started_unix_ms: u128,
    t_max_ms: u64,
    grace_ms: u64,
    pages_per_step: usize,
    source_db_count: usize,
    other_regular_files: usize,
    databases: Vec<ManifestDb>,
    #[serde(skip_serializing_if = "Option::is_none")]
    error: Option<String>,
}

// ── parent command ──────────────────────────────────────────────────────

pub fn cmd_archive_snapshot(args: ArchiveSnapshotArgs) -> i32 {
    let report = run_archive_snapshot(&args);
    let code = if report.0.complete && report.1.is_none() { 0 } else { 1 };
    // Stdout is ALWAYS the machine-readable JSON report — including on
    // failure — so consumers and the exit status can never disagree.
    println!("{}", serde_json::to_string(&report.0).unwrap_or_default());
    if let Some(fatal) = report.1 {
        eprintln!("archive-snapshot fatal: {fatal}");
    }
    code
}

type RunResult = (Manifest, Option<String>);

fn run_archive_snapshot(args: &ArchiveSnapshotArgs) -> RunResult {
    let started_unix_ms = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_millis())
        .unwrap_or(0);

    // Everything that can be validated before creating anything is a hard
    // error: bad keys, bad paths, overlapping trees.
    if let Err(e) = load_keys(&args.key_file) {
        return fatal(args, started_unix_ms, e);
    }
    let source_abs = match validate_paths(&args.source, &args.out) {
        Ok(path) => path,
        Err(e) => return fatal(args, started_unix_ms, e),
    };
    let out_abs = absolute(&args.out);
    let generation = out_abs
        .file_name()
        .map(|n| n.to_string_lossy().to_string())
        .unwrap_or_else(|| format!("gen-{started_unix_ms}"));
    let enumeration = match enumerate_source_databases(&source_abs) {
        Ok(e) => e,
        Err(e) => return fatal(args, started_unix_ms, e.to_string()),
    };

    // Create the generation directory only after all validation passed.
    if let Err(e) = ensure_private_dir(&out_abs) {
        return fatal(args, started_unix_ms, e.to_string());
    }
    let run_id = new_run_id();
    let events_path = out_abs.join(format!(".events-{run_id}.jsonl"));
    // Truncate the event file so no stale content can ever be read, even
    // before run_id filtering applies.
    if let Err(e) = std::fs::File::create(&events_path).and_then(|f| {
        f.sync_all()?;
        Ok(())
    }) {
        return fatal(args, started_unix_ms, format!("event file create failed: {e}"));
    }

    let t_max = Duration::from_millis(args.t_max_ms.max(1));
    let grace = Duration::from_millis(args.grace_ms.max(1));
    let mut databases: Vec<ManifestDb> = Vec::new();
    let mut fatal_error: Option<String> = None;

    for db_path in &enumeration.databases {
        let rel = db_path
            .strip_prefix(&source_abs)
            .unwrap_or(db_path.as_path())
            .to_string_lossy()
            .to_string();
        let publish = out_abs.join(&rel);
        let candidate = out_abs.join(format!("{rel}.candidate"));
        if let Err(e) = publish.parent().map(ensure_private_dir).transpose() {
            databases.push(ManifestDb {
                db: rel,
                ok: false,
                kind: Some("io".into()),
                sha256: None,
                bytes: None,
                pages: None,
                elapsed_ms: None,
                kill_overshoot_ms: None,
                kill_syscall_ms: None,
            });
            fatal_error = Some(e.to_string());
            continue;
        }

        let outcome = snapshot_one(
            db_path,
            &candidate,
            &publish,
            &args.key_file,
            &events_path,
            run_id,
            t_max,
            grace,
            args.pages_per_step,
        );

        if !outcome.ok {
            // Half-finished products never survive: drop the candidate, and
            // drop any published file this run cannot vouch for.
            let _ = std::fs::remove_file(&candidate);
            if outcome.published_trusted.is_none() {
                let _ = std::fs::remove_file(&publish);
            }
        }
        databases.push(ManifestDb {
            db: rel,
            ok: outcome.ok,
            kind: outcome.kind,
            sha256: outcome.sha256,
            bytes: outcome.bytes,
            pages: outcome.pages,
            elapsed_ms: outcome.elapsed_ms,
            kill_overshoot_ms: outcome.kill_overshoot_ms,
            kill_syscall_ms: outcome.kill_syscall_ms,
        });
    }

    let _ = std::fs::remove_file(&events_path);

    let complete = !databases.is_empty()
        && databases.iter().all(|d| d.ok)
        && fatal_error.is_none();
    let manifest = Manifest {
        complete,
        generation,
        started_unix_ms,
        t_max_ms: args.t_max_ms,
        grace_ms: args.grace_ms,
        pages_per_step: args.pages_per_step,
        source_db_count: enumeration.databases.len(),
        other_regular_files: enumeration.other_regular_files,
        databases,
        error: fatal_error.clone(),
    };

    // Serialize the ENTIRE manifest first; only a successful serialization
    // may be published, and the exit code below agrees with what is on disk.
    let manifest_bytes = match serde_json::to_vec_pretty(&manifest) {
        Ok(bytes) => bytes,
        Err(e) => {
            return (
                Manifest {
                    complete: false,
                    error: Some(format!("manifest serialization failed: {e}")),
                    ..manifest
                },
                Some(format!("manifest serialization failed: {e}")),
            )
        }
    };
    let manifest_name = if complete {
        "manifest.json"
    } else {
        "manifest.partial.json"
    };
    let manifest_path = out_abs.join(manifest_name);
    match atomic_private_write(&manifest_path, &manifest_bytes) {
        Ok(()) => (manifest, None),
        Err(e) => (
            Manifest {
                complete: false,
                error: Some(format!("manifest publication failed: {}", e.kind())),
                ..manifest
            },
            Some(format!("manifest publication failed: {}", e.kind())),
        ),
    }
}

fn fatal(args: &ArchiveSnapshotArgs, started_unix_ms: u128, message: String) -> RunResult {
    let generation = absolute(&args.out)
        .file_name()
        .map(|n| n.to_string_lossy().to_string())
        .unwrap_or_else(|| format!("gen-{started_unix_ms}"));
    (
        Manifest {
            complete: false,
            generation,
            started_unix_ms,
            t_max_ms: args.t_max_ms,
            grace_ms: args.grace_ms,
            pages_per_step: args.pages_per_step,
            source_db_count: 0,
            other_regular_files: 0,
            databases: Vec::new(),
            error: Some(message.clone()),
        },
        Some(message),
    )
}

fn new_run_id() -> u64 {
    let nanos = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_nanos() as u64)
        .unwrap_or(0);
    nanos ^ (std::process::id() as u64) << 32
}

struct DbOutcome {
    ok: bool,
    kind: Option<String>,
    sha256: Option<String>,
    bytes: Option<u64>,
    pages: Option<usize>,
    elapsed_ms: Option<u128>,
    /// Some(()) when the run saw a `published` event and a successful worker
    /// report — the only conditions under which a published file is trusted.
    published_trusted: Option<()>,
    /// How long after the exact deadline (T_max / grace / absolute) the kill
    /// decision actually fired — poll cadence + OS scheduling latency,
    /// MEASURED and reported, never granted as extra read-transaction
    /// budget. None when no deadline kill happened.
    kill_overshoot_ms: Option<u128>,
    /// Wall time of the kill() syscall itself (SIGKILL dispatch).
    kill_syscall_ms: Option<u128>,
}

#[allow(clippy::too_many_arguments)]
fn snapshot_one(
    db: &Path,
    candidate: &Path,
    publish: &Path,
    key_file: &Path,
    events: &Path,
    run_id: u64,
    t_max: Duration,
    grace: Duration,
    pages_per_step: usize,
) -> DbOutcome {
    let exe = std::env::current_exe().unwrap_or_else(|_| PathBuf::from("wx-cli"));
    let child = Command::new(exe)
        .arg("__archive-snapshot-worker")
        .arg("--db")
        .arg(db)
        .arg("--candidate")
        .arg(candidate)
        .arg("--publish")
        .arg(publish)
        .arg("--key-file")
        .arg(key_file)
        .arg("--t-max-ms")
        .arg(t_max.as_millis().to_string())
        .arg("--pages-per-step")
        .arg(pages_per_step.to_string())
        .arg("--events")
        .arg(events)
        .arg("--run-id")
        .arg(run_id.to_string())
        // The worker's stdin is the ack channel: the worker must not BEGIN
        // its source transaction until the parent confirms receipt of the
        // `pinning` event, so the parent's clock provably covers the whole
        // lock-holding window.
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::null())
        .spawn();

    let mut child = match child {
        Ok(child) => child,
        Err(e) => {
            return DbOutcome {
                ok: false,
                kind: Some(format!("spawn_failed:{e}")),
                sha256: None,
                bytes: None,
                pages: None,
                elapsed_ms: None,
                published_trusted: None,
                kill_overshoot_ms: None,
                kill_syscall_ms: None,
            }
        }
    };

    let supervision = supervise(&mut child, events, run_id, t_max, grace);
    let mut stdout = String::new();
    if let Some(mut pipe) = child.stdout.take() {
        let _ = pipe.read_to_string(&mut stdout);
    }
    let _ = child.wait();

    let report: Result<WorkerReport, String> = serde_json::from_str(stdout.trim())
        .map_err(|e| format!("worker report unparseable: {e}"));

    match (report, supervision.killed_reason) {
        (Ok(r), None) if r.ok => DbOutcome {
            ok: true,
            kind: None,
            sha256: r.sha256,
            bytes: r.bytes,
            pages: r.pages,
            elapsed_ms: r.elapsed_ms,
            published_trusted: Some(()),
            kill_overshoot_ms: None,
            kill_syscall_ms: None,
        },
        (Ok(r), None) => DbOutcome {
            ok: false,
            kind: r.kind.or(Some("worker_failed".into())),
            sha256: None,
            bytes: None,
            pages: None,
            elapsed_ms: r.elapsed_ms,
            published_trusted: if supervision.saw_published { Some(()) } else { None },
            kill_overshoot_ms: None,
            kill_syscall_ms: None,
        },
        (Ok(r), Some(_reason)) => DbOutcome {
            ok: false,
            kind: Some(format!("parent_killed_{}", supervision.killed_reason.unwrap())),
            sha256: None,
            bytes: None,
            pages: None,
            elapsed_ms: r.elapsed_ms,
            published_trusted: if supervision.saw_published { Some(()) } else { None },
            kill_overshoot_ms: supervision.kill_overshoot_ms,
            kill_syscall_ms: supervision.kill_syscall_ms,
        },
        (Err(e), killed) => DbOutcome {
            ok: false,
            kind: Some(match killed {
                Some(reason) => format!("parent_killed_{reason}"),
                None => e,
            }),
            sha256: None,
            bytes: None,
            pages: None,
            elapsed_ms: None,
            published_trusted: if supervision.saw_published { Some(()) } else { None },
            kill_overshoot_ms: supervision.kill_overshoot_ms,
            kill_syscall_ms: supervision.kill_syscall_ms,
        },
    }
}

struct Supervision {
    killed_reason: Option<&'static str>,
    saw_published: bool,
    /// Deadline overshoot of the kill decision (poll + OS scheduling past
    /// the exact budget) — measured, reported, never budgeted.
    kill_overshoot_ms: Option<u128>,
    kill_syscall_ms: Option<u128>,
}

/// Supervise one worker with monotonic budgets and a confirmed handshake.
///
/// The worker writes a `pinning` event and then BLOCKS on its stdin until
/// the parent writes an ack byte. The parent starts its T_max clock at the
/// moment it writes that ack — strictly before the worker can BEGIN — so the
/// entire possible lock-holding window provably lies inside
/// [ack_written, ack_written + t_max]. A lost or late `pinning` event
/// therefore degrades to "no ack, no BEGIN" and the worker is killed by the
/// startup budget instead of holding uncounted locks.
///
/// Budgets are EXACT: startup (spawn → ack) gets `grace`, the transaction
/// window gets exactly `t_max`, post-release verification/publish gets
/// `grace`. The kill decision fires the moment a deadline is observed
/// passed — no slack is ever added to an authorized budget. Latency the
/// parent cannot remove (poll cadence, OS scheduling, the kill syscall
/// itself) is MEASURED (`kill_overshoot_ms`, `kill_syscall_ms`) and
/// reported in the manifest instead of being silently granted. Event-file
/// read failures are fatal (kill immediately), never swallowed.
fn supervise(
    child: &mut Child,
    events: &Path,
    run_id: u64,
    t_max: Duration,
    grace: Duration,
) -> Supervision {
    let spawned = Instant::now();
    let mut offset = 0usize;
    let mut acked_at: Option<Instant> = None;
    let mut released_at: Option<Instant> = None;
    let mut saw_published = false;
    let mut killed_reason: Option<&'static str> = None;
    let mut kill_overshoot_ms: Option<u128> = None;
    let mut kill_syscall_ms: Option<u128> = None;
    // Absolute backstop: even a worker that never sends a single event
    // cannot outlive startup + t_max + release grace.
    let absolute_budget = grace + t_max + grace;

    loop {
        // Reap first: a worker that already exited needs no killing.
        match child.try_wait() {
            Ok(Some(_status)) => break,
            Ok(None) => {}
            Err(_) => break,
        }
        // A read failure here is fatal: without event visibility the parent
        // cannot bound the worker, so it must kill immediately.
        let drained = match drain_events(events, &mut offset, run_id) {
            Ok(events) => events,
            Err(_) => {
                if killed_reason.is_none() {
                    let kill_started = Instant::now();
                    let _ = child.kill();
                    kill_syscall_ms = Some(kill_started.elapsed().as_millis());
                    killed_reason = Some("events_unreadable");
                }
                Vec::new()
            }
        };
        let mut saw_pinning = false;
        for event in drained {
            match event {
                WorkerEvent::Pinning => saw_pinning = true,
                WorkerEvent::Pinned => {}
                WorkerEvent::Released => released_at = released_at.or(Some(Instant::now())),
                WorkerEvent::Published => saw_published = true,
            }
        }
        // Handshake: on first `pinning`, ack on stdin and start the clock at
        // the ack write — the worker cannot BEGIN before receiving it.
        if saw_pinning && acked_at.is_none() && killed_reason.is_none() {
            let ack_at = Instant::now();
            let mut acked = false;
            if let Some(stdin) = child.stdin.as_mut() {
                acked = stdin.write_all(b"K\n").is_ok() && stdin.flush().is_ok();
            }
            if acked {
                acked_at = Some(ack_at);
            } else {
                let kill_started = Instant::now();
                let _ = child.kill();
                kill_syscall_ms = Some(kill_started.elapsed().as_millis());
                killed_reason = Some("ack_write_failed");
            }
        }
        if killed_reason.is_none() {
            let now = Instant::now();
            let reason = if let Some(at) = acked_at {
                if released_at.is_none() && now.duration_since(at) > t_max {
                    Some("tx_timeout")
                } else if let Some(rel) = released_at {
                    if now.duration_since(rel) > grace {
                        Some("release_grace_exceeded")
                    } else {
                        None
                    }
                } else {
                    None
                }
            } else if now.duration_since(spawned) > grace {
                Some("startup_timeout")
            } else {
                None
            };
            let reason = reason
                .or(if now.duration_since(spawned) > absolute_budget {
                    Some("absolute_timeout")
                } else {
                    None
                });
            if let Some(reason) = reason {
                // Exact-deadline kill: SIGKILL so the OS closes the worker's
                // descriptors, ending every fcntl lock on the source
                // immediately. How far past the deadline this decision fired
                // (poll cadence + scheduler latency) is measured and
                // reported — never granted as extra transaction budget.
                let deadline = match reason {
                    "tx_timeout" => acked_at.expect("tx deadline needs ack") + t_max,
                    "release_grace_exceeded" => {
                        released_at.expect("release deadline needs release") + grace
                    }
                    "startup_timeout" => spawned + grace,
                    _ => spawned + absolute_budget,
                };
                kill_overshoot_ms = Some(now.saturating_duration_since(deadline).as_millis());
                let kill_started = Instant::now();
                let _ = child.kill();
                kill_syscall_ms = Some(kill_started.elapsed().as_millis());
                killed_reason = Some(reason);
            }
        }
        std::thread::sleep(POLL_INTERVAL);
    }
    // Final drain so late events still count towards trust decisions.
    for event in drain_events(events, &mut offset, run_id).unwrap_or_default() {
        if event == WorkerEvent::Published {
            saw_published = true;
        }
    }
    Supervision {
        killed_reason,
        saw_published,
        kill_overshoot_ms,
        kill_syscall_ms,
    }
}

// ── worker command ──────────────────────────────────────────────────────

#[derive(Serialize, Deserialize, Debug)]
struct WorkerReport {
    ok: bool,
    #[serde(skip_serializing_if = "Option::is_none")]
    kind: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    error: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    sha256: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    bytes: Option<u64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pages: Option<usize>,
    #[serde(skip_serializing_if = "Option::is_none")]
    elapsed_ms: Option<u128>,
}

pub fn cmd_worker(args: ArchiveSnapshotWorkerArgs) -> i32 {
    let started = Instant::now();
    let report = run_worker(&args, started);
    let ok = report.ok;
    println!(
        "{}",
        serde_json::to_string(&report).unwrap_or_else(|_| "{\"ok\":false}".into())
    );
    if ok {
        0
    } else {
        1
    }
}

fn run_worker(args: &ArchiveSnapshotWorkerArgs, started: Instant) -> WorkerReport {
    let keys = match load_keys(&args.key_file) {
        Ok(keys) => keys,
        Err(e) => {
            return WorkerReport {
                ok: false,
                kind: Some("key".into()),
                error: Some(e),
                sha256: None,
                bytes: None,
                pages: None,
                elapsed_ms: Some(started.elapsed().as_millis()),
            }
        }
    };

    let outcome = snapshot_database(args, &keys);
    let elapsed_ms = Some(started.elapsed().as_millis());
    match outcome {
        Ok((meta, sha256, pages)) => WorkerReport {
            ok: true,
            kind: None,
            error: None,
            sha256: Some(sha256),
            bytes: Some(meta.bytes),
            pages: Some(pages),
            elapsed_ms,
        },
        Err((kind, message)) => {
            // Never leave a partial candidate behind on failure.
            let _ = std::fs::remove_file(&args.candidate);
            WorkerReport {
                ok: false,
                kind: Some(kind),
                error: Some(message),
                sha256: None,
                bytes: None,
                pages: None,
                elapsed_ms,
            }
        }
    }
}

type WorkerStepResult = Result<(SnapshotMeta, String, usize), (String, String)>;

fn snapshot_database(args: &ArchiveSnapshotWorkerArgs, keys: &SnapshotKeys) -> WorkerStepResult {
    let fail = |e: &SnapshotError| (e.kind().to_string(), e.to_string());

    // Startup budget (parent: grace): open + key check, no locks held.
    let opened = match OpenedSource::open(&args.db, keys) {
        Ok(opened) => opened,
        Err(e) => {
            let (kind, message) = fail(&e);
            return Err((kind, message));
        }
    };

    // Transaction window (parent: exactly t_max, clocked from the parent's
    // ack write). The worker announces `pinning`, then BLOCKS until the
    // parent confirms — no BEGIN can happen before the parent's clock
    // starts. If the parent is gone (EOF) or sends garbage, abort without
    // ever holding locks.
    if let Err(e) = append_event(&args.events, args.run_id, WorkerEvent::Pinning) {
        return Err(("io".into(), e));
    }
    {
        let mut ack = [0u8; 2];
        let mut stdin = std::io::stdin().lock();
        if stdin.read_exact(&mut ack).is_err() || &ack != b"K\n" {
            return Err((
                "no_parent_ack".into(),
                "parent did not confirm the pinning handshake; refusing to begin".into(),
            ));
        }
    }
    let tx_started = Instant::now();
    let pinned = match opened.begin() {
        Ok(pinned) => pinned,
        Err(e) => {
            let (kind, message) = fail(&e);
            return Err((kind, message));
        }
    };
    if let Err(e) = append_event(&args.events, args.run_id, WorkerEvent::Pinned) {
        return Err(("io".into(), e));
    }
    let t_max = Duration::from_millis(args.t_max_ms.max(1));
    let elapsed = tx_started.elapsed();
    let deadline = t_max.checked_sub(elapsed).unwrap_or(Duration::from_millis(1));
    let stats = match pinned.encrypted_backup(
        &args.candidate,
        &BackupOptions {
            pages_per_step: args.pages_per_step,
            deadline: Some(deadline),
        },
    ) {
        Ok(stats) => stats,
        Err(e) => {
            let (kind, message) = fail(&e);
            return Err((kind, message));
        }
    };
    let expectation = pinned.expectation();
    pinned.release();
    if let Err(e) = append_event(&args.events, args.run_id, WorkerEvent::Released) {
        return Err(("io".into(), e));
    }

    // Post-release budget (parent: grace): verify + publish the static copy.
    let meta = match verify_snapshot(&args.candidate, keys, &expectation) {
        Ok(meta) => meta,
        Err(e) => {
            let (kind, message) = fail(&e);
            return Err((kind, message));
        }
    };
    if let Err(e) = publish_snapshot(&args.candidate, &args.publish) {
        let (kind, message) = fail(&e);
        return Err((kind, message));
    }
    if let Err(e) = append_event(&args.events, args.run_id, WorkerEvent::Published) {
        return Err(("io".into(), e));
    }
    // The published copy must be readable with the same keys before this
    // worker reports success.
    match open_snapshot_copy(&args.publish, keys) {
        Ok(_) => {}
        Err(e) => {
            let (kind, message) = fail(&e);
            return Err((kind, message));
        }
    }
    let sha256 = match sha256_file(&args.publish) {
        Ok(sha) => sha,
        Err(e) => return Err(("io".into(), e)),
    };
    Ok((meta, sha256, stats.page_count))
}

fn sha256_file(path: &Path) -> Result<String, String> {
    use sha2::{Digest, Sha256};
    let mut file = std::fs::File::open(path).map_err(|e| e.to_string())?;
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

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn events_from_other_runs_are_ignored() {
        let line_current = "{\"run_id\":7,\"event\":\"released\"}".to_string();
        assert_eq!(
            parse_event_line(&line_current, 7),
            Some(WorkerEvent::Released)
        );
        // Stale content from a different run must not parse as this run's
        // events — it can never cancel monitoring.
        let stale = "{\"run_id\":6,\"event\":\"released\"}";
        assert_eq!(parse_event_line(stale, 7), None);
        // Garbage lines are skipped, not fatal.
        assert_eq!(parse_event_line("not json", 7), None);
        assert_eq!(
            parse_event_line("{\"run_id\":7,\"event\":\"pinning\"}", 7),
            Some(WorkerEvent::Pinning)
        );
    }

    #[test]
    fn key_file_rejects_wrong_lengths_and_bad_hex_without_leaking() {
        let tmp = tempfile::TempDir::new().unwrap();
        let kf = tmp.path().join("key.json");
        // Explicit 0600: file creation inherits the launcher's umask, and
        // this test exercises length/hex validation — NOT the (correct)
        // permission rejection, which its sibling test covers.
        std::fs::write(&kf, format!("{{\"raw_key\":\"{}\"}}", "ab".repeat(31))).unwrap();
        std::fs::set_permissions(&kf, std::fs::Permissions::from_mode(0o600)).unwrap();
        let err = load_keys(&kf).unwrap_err();
        assert!(err.contains("must be exactly 32 bytes"), "{err}");
        assert!(!err.contains("abab"), "error must not echo key material");
        std::fs::write(&kf, format!("{{\"raw_key\":\"{}\"}}", "zz".repeat(32))).unwrap();
        std::fs::set_permissions(&kf, std::fs::Permissions::from_mode(0o600)).unwrap();
        assert!(load_keys(&kf).unwrap_err().contains("not valid hex"));
    }

    #[test]
    fn key_file_rejects_loose_permissions() {
        let tmp = tempfile::TempDir::new().unwrap();
        let kf = tmp.path().join("key.json");
        std::fs::write(&kf, format!("{{\"raw_key\":\"{}\"}}", "11".repeat(32))).unwrap();
        std::fs::set_permissions(&kf, std::fs::Permissions::from_mode(0o644)).unwrap();
        assert!(load_keys(&kf).unwrap_err().contains("0600"));
        std::fs::set_permissions(&kf, std::fs::Permissions::from_mode(0o600)).unwrap();
        assert!(load_keys(&kf).is_ok());
    }

    #[test]
    fn overlap_checks_are_direction_aware() {
        let tmp = tempfile::TempDir::new().unwrap();
        let source = tmp.path().join("src");
        std::fs::create_dir_all(&source).unwrap();
        let abs_source = source.canonicalize().unwrap();

        let inside = abs_source.join("gen");
        assert!(validate_paths(&source, &inside)
            .unwrap_err()
            .contains("inside the source"));
        let above = tmp.path().to_path_buf();
        assert!(validate_paths(&source, &above)
            .unwrap_err()
            .contains("contains the source"));
        let existing = tmp.path().join("already");
        std::fs::create_dir_all(&existing).unwrap();
        assert!(validate_paths(&source, &existing)
            .unwrap_err()
            .contains("already exists"));
        let ok_out = tmp.path().join("fresh");
        assert!(validate_paths(&source, &ok_out).is_ok());
    }

    #[test]
    fn normalization_defeats_dotdot_spelling_of_overlap() {
        let tmp = tempfile::TempDir::new().unwrap();
        let source = tmp.path().join("src");
        std::fs::create_dir_all(&source).unwrap();
        let sneaky = tmp.path().join("other").join("..").join("src").join("gen");
        assert!(validate_paths(&source, &sneaky)
            .unwrap_err()
            .contains("inside the source"));
    }
}
