//! Consistent encrypted snapshots of live SQLCipher WeChat source databases.
//!
//! Implements the round-5 locked source-capture method: for each source
//! database, open one READONLY SQLCipher connection, `BEGIN` and perform a
//! real read to pin a fixed read view, complete a same-key/salt/page-params
//! encrypted backup inside that same transaction with bounded steps and a
//! monotonic deadline, then release the source transaction immediately and
//! verify the static copy offline (including SQLCipher's own per-page
//! `cipher_integrity_check`). Verification and all later enumeration run
//! against the published copy only; the live source is never opened with
//! `immutable`, never rekeyed, and never converted to plaintext.
//!
//! Publication is fail-closed: candidate files are created `0600` with
//! `O_EXCL|O_NOFOLLOW`, directories are `0700`, and the verified copy is
//! published read-only (`0400`) through an atomic no-clobber `link()` so an
//! existing generation can never be replaced (TOCTOU-free). Unsupported
//! source formats are rejected explicitly instead of guessed.
//!
//! Nothing here touches a NAS; key material is supplied by the caller and is
//! never included in errors, debug output, or reports. No step reads chat
//! content from the live source (only schema metadata inside the pinned
//! transaction); full content scans happen later on the static copy via
//! [`open_snapshot_copy`].
use std::collections::HashMap;
use std::ffi::CString;
use std::fmt;
use std::fs::File;
use std::io::Write;
use std::os::raw::c_void;
use std::os::unix::ffi::OsStrExt;
use std::os::unix::fs::PermissionsExt;
use std::path::{Path, PathBuf};
use std::time::{Duration, Instant};

use rusqlite::Connection;

use crate::error::DbError;

/// Key material for snapshotting, supporting derived-only input.
///
/// `from_derived_pairs` never holds the raw key: SQLCipher's raw keyspec is
/// literally `x'<derived key><salt>'`, so pre-derived `EncKeyPair`s suffice to
/// open the source and to key the destination copy.
#[derive(Clone)]
pub struct SnapshotKeys {
    raw_key: Option<[u8; 32]>,
    derived: HashMap<[u8; 16], [u8; 32]>,
}

impl fmt::Debug for SnapshotKeys {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        // Never expose key bytes in debug output.
        f.debug_struct("SnapshotKeys")
            .field("has_raw_key", &self.raw_key.is_some())
            .field("derived_key_count", &self.derived.len())
            .finish()
    }
}

impl SnapshotKeys {
    /// Key material from a raw WeChat key; per-salt keys are derived on demand.
    pub fn from_raw(key: [u8; 32]) -> Self {
        Self {
            raw_key: Some(key),
            derived: HashMap::new(),
        }
    }

    /// Derived-only key material: pre-derived per-salt keys, no raw key.
    pub fn from_derived_pairs(pairs: &[wx_decrypt::EncKeyPair]) -> Self {
        Self {
            raw_key: None,
            derived: pairs.iter().map(|p| (p.salt, p.key)).collect(),
        }
    }

    /// True when no raw key is held (derived-only mode).
    pub fn is_derived_only(&self) -> bool {
        self.raw_key.is_none()
    }

    fn keyspec_for(&self, salt: [u8; 16]) -> Result<Vec<u8>, SnapshotError> {
        let key = if let Some(key) = self.derived.get(&salt) {
            *key
        } else if let Some(raw) = self.raw_key {
            wx_decrypt::kdf::derive_enc_key(&raw, &salt, &wx_decrypt::MACOS_4_1_7_31)
        } else {
            return Err(SnapshotError::Key(
                "no derived key registered for this database salt".into(),
            ));
        };
        Ok(format!("x'{}{}'", hex::encode(key), hex::encode(salt)).into_bytes())
    }
}

/// Failure classification for snapshot operations; machine-readable via
/// `kind()` in worker reports.
#[derive(Debug)]
pub enum SnapshotError {
    /// Wrong key, unencrypted file, unreadable salt, or missing derived pair.
    Key(String),
    /// The source uses a format this implementation does not support;
    /// reported explicitly instead of guessing parameters.
    Unsupported(String),
    /// Source (or destination) busy; per round-5 the step fails fast instead
    /// of holding the source transaction indefinitely.
    Busy(String),
    /// The monotonic deadline for the source transaction was exceeded.
    Timeout { elapsed_ms: u128 },
    /// Post-release verification of the static copy failed.
    Verify(String),
    /// Publication target already exists; generations are immutable.
    AlreadyPublished(PathBuf),
    Io(String),
    Sql(String),
}

impl fmt::Display for SnapshotError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            SnapshotError::Key(m) => write!(f, "snapshot key error: {m}"),
            SnapshotError::Unsupported(m) => write!(f, "unsupported source format: {m}"),
            SnapshotError::Busy(m) => write!(f, "snapshot busy: {m}"),
            SnapshotError::Timeout { elapsed_ms } => {
                write!(f, "snapshot exceeded T_max after {elapsed_ms}ms")
            }
            SnapshotError::Verify(m) => write!(f, "snapshot verification failed: {m}"),
            SnapshotError::AlreadyPublished(p) => write!(
                f,
                "publication target {} already exists; generations are immutable",
                p.display()
            ),
            SnapshotError::Io(m) => write!(f, "snapshot io error: {m}"),
            SnapshotError::Sql(m) => write!(f, "snapshot sqlite error: {m}"),
        }
    }
}

impl std::error::Error for SnapshotError {}

impl SnapshotError {
    pub fn kind(&self) -> &'static str {
        match self {
            SnapshotError::Key(_) => "key",
            SnapshotError::Unsupported(_) => "unsupported_format",
            SnapshotError::Busy(_) => "busy",
            SnapshotError::Timeout { .. } => "timeout",
            SnapshotError::Verify(_) => "verify_failed",
            SnapshotError::AlreadyPublished(_) => "already_published",
            SnapshotError::Io(_) => "io",
            SnapshotError::Sql(_) => "sqlite",
        }
    }
}

impl From<std::io::Error> for SnapshotError {
    fn from(e: std::io::Error) -> Self {
        SnapshotError::Io(e.to_string())
    }
}

impl From<rusqlite::Error> for SnapshotError {
    fn from(e: rusqlite::Error) -> Self {
        SnapshotError::Sql(e.to_string())
    }
}

impl From<DbError> for SnapshotError {
    fn from(e: DbError) -> Self {
        SnapshotError::Key(e.to_string())
    }
}

// ── secure filesystem helpers ───────────────────────────────────────────

fn cstr(path: &Path) -> Result<CString, SnapshotError> {
    CString::new(path.as_os_str().as_bytes())
        .map_err(|_| SnapshotError::Io("path contains NUL byte".into()))
}

/// Create an empty private candidate file `0600` with
/// `O_WRONLY|O_CREAT|O_EXCL|O_NOFOLLOW`. Fails if it already exists — a
/// candidate collision is always an error, never a clobber.
fn create_candidate_file(path: &Path) -> Result<(), SnapshotError> {
    let c = cstr(path)?;
    let fd = unsafe {
        libc::open(
            c.as_ptr(),
            libc::O_WRONLY | libc::O_CREAT | libc::O_EXCL | libc::O_NOFOLLOW,
            0o600,
        )
    };
    if fd < 0 {
        return Err(SnapshotError::Io(format!(
            "candidate create {} failed: {}",
            path.display(),
            std::io::Error::last_os_error()
        )));
    }
    let rc = unsafe { libc::close(fd) };
    if rc != 0 {
        return Err(SnapshotError::Io(format!(
            "candidate close failed: {}",
            std::io::Error::last_os_error()
        )));
    }
    Ok(())
}

/// macOS ships standard root symlinks (`/var` → `/private/var`, `/tmp`,
/// `/etc`). These are the only symlink components ever tolerated in a
/// managed path, and only when they point at their expected standard
/// targets; any other symlink component is rejected. Without this whitelist
/// every temp directory on macOS (under `/var/folders`) would be unusable.
const MACOS_STANDARD_SYMLINKS: &[(&str, &str)] = &[
    ("/var", "/private/var"),
    ("/tmp", "/private/tmp"),
    ("/etc", "/private/etc"),
];

fn symlink_component_allowed(path: &Path) -> Result<bool, SnapshotError> {
    // macOS symlink targets are relative ("private/var"), so compare resolved
    // paths rather than the read_link text.
    let resolved = std::fs::canonicalize(path).map_err(|e| {
        SnapshotError::Io(format!("canonicalize {} failed: {e}", path.display()))
    })?;
    Ok(MACOS_STANDARD_SYMLINKS
        .iter()
        .any(|(link, expected)| path == Path::new(link) && resolved == Path::new(expected)))
}

/// Create `path` and any missing parents so every newly created directory is
/// `0700`. Existing components must be real directories; symlink components
/// are rejected except the macOS standard roots listed above.
/// `mkdir(0700)` is umask-safe because umasks can only clear bits.
pub fn ensure_private_dir(path: &Path) -> Result<(), SnapshotError> {
    let mut current = PathBuf::new();
    for component in path.components() {
        current.push(component);
        let c = cstr(&current)?;
        let rc = unsafe { libc::mkdir(c.as_ptr(), 0o700) };
        if rc == 0 {
            continue;
        }
        let err = std::io::Error::last_os_error();
        if err.kind() == std::io::ErrorKind::AlreadyExists {
            let meta = std::fs::symlink_metadata(&current).map_err(|e| {
                SnapshotError::Io(format!("stat {} failed: {e}", current.display()))
            })?;
            if meta.is_dir() {
                continue;
            }
            if meta.file_type().is_symlink() && symlink_component_allowed(&current)? {
                continue;
            }
            return Err(SnapshotError::Io(format!(
                "{} exists but is not a real directory (non-standard symlinks are rejected)",
                current.display()
            )));
        } else {
            return Err(SnapshotError::Io(format!(
                "mkdir {} failed: {err}",
                current.display()
            )));
        }
    }
    Ok(())
}

/// Durably and privately publish `bytes` at `path` with atomic no-clobber
/// semantics: a fresh `0600` temporary file in the same directory is written
/// and fsynced, then `link()`ed into place (fails if `path` exists), then the
/// temporary name is removed and the directory fsynced. Readers either see no
/// file or the complete content; an existing target is never replaced.
pub fn atomic_private_write(path: &Path, bytes: &[u8]) -> Result<(), SnapshotError> {
    let parent = path
        .parent()
        .ok_or_else(|| SnapshotError::Io("target has no parent directory".into()))?;
    ensure_private_dir(parent)?;
    let unique_nanos = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_nanos())
        .unwrap_or(0);
    let temp = parent.join(format!(
        ".{}.tmp-{}-{}",
        path.file_name()
            .map(|n| n.to_string_lossy().to_string())
            .unwrap_or_else(|| "file".into()),
        std::process::id(),
        unique_nanos
    ));
    create_candidate_file(&temp)?;
    {
        let mut file = std::fs::OpenOptions::new().write(true).open(&temp)?;
        file.write_all(bytes)?;
        file.sync_all()?;
    }
    let rc = unsafe { libc::link(cstr(&temp)?.as_ptr(), cstr(path)?.as_ptr()) };
    if rc != 0 {
        let _ = std::fs::remove_file(&temp);
        let err = std::io::Error::last_os_error();
        if err.kind() == std::io::ErrorKind::AlreadyExists {
            return Err(SnapshotError::AlreadyPublished(path.to_path_buf()));
        }
        return Err(SnapshotError::Io(format!(
            "link into place failed: {err}"
        )));
    }
    std::fs::remove_file(&temp)?;
    let dir = File::open(parent)?;
    dir.sync_all()?;
    Ok(())
}

// ── cipher parameter handling ───────────────────────────────────────────

/// Page sizes this implementation explicitly supports, in probe order. A
/// wrong page size fails SQLCipher's per-page HMAC check cryptographically,
/// so probing is verification, not guessing. WeChat 4.x uses 4096.
const PAGE_SIZE_PROBE: [usize; 7] = [4096, 1024, 2048, 8192, 16384, 32768, 65536];

/// The cipher parameters that must match between source and copy. `reserve`
/// is derived by SQLCipher from the HMAC algorithm and IV size, both of
/// which are pinned by the copied settings and re-proven by
/// `cipher_integrity_check` over every page of the copy. Every field below
/// is read back from SQLCipher pragmas (empirically readable on a keyed
/// connection: `cipher_use_hmac`, `cipher_hmac_algorithm`,
/// `cipher_kdf_algorithm`, `kdf_iter`, `cipher_plaintext_header_size`);
/// unreadable knobs fail closed instead of defaulting to "match".
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct CipherParams {
    pub page_size: usize,
    pub use_hmac: bool,
    pub hmac_algorithm: String,
    pub kdf_algorithm: String,
    pub kdf_iter: i64,
    pub plaintext_header_size: i64,
}

impl CipherParams {
    /// The PRAGMA statements that put a fresh destination connection into
    /// exactly this cipher configuration. Applied BEFORE the key so the very
    /// first page written is already in the source's format.
    fn destination_pragmas(&self) -> Result<String, SnapshotError> {
        for alg in [&self.hmac_algorithm, &self.kdf_algorithm] {
            if alg.is_empty()
                || !alg
                    .chars()
                    .all(|c| c.is_ascii_uppercase() || c.is_ascii_digit() || c == '_')
            {
                return Err(SnapshotError::Unsupported(format!(
                    "unrecognized cipher algorithm name: {alg:?}"
                )));
            }
        }
        Ok(format!(
            // Order matters: cipher_use_hmac / cipher_hmac_algorithm change
            // the per-page reserve and RESET cipher_page_size to the
            // default, so cipher_page_size must come last (empirically
            // confirmed: setting it earlier produced a mixed-page copy that
            // validated at no page size).
            "PRAGMA cipher_use_hmac = {}; \
             PRAGMA cipher_hmac_algorithm = {}; \
             PRAGMA cipher_kdf_algorithm = {}; \
             PRAGMA cipher_kdf_iter = {}; \
             PRAGMA cipher_plaintext_header_size = {}; \
             PRAGMA cipher_page_size = {};",
            self.use_hmac as i64,
            self.hmac_algorithm,
            self.kdf_algorithm,
            self.kdf_iter,
            self.plaintext_header_size,
            self.page_size,
        ))
    }
}

/// Read the logical page size of a keyed connection. SQLCipher reports
/// `PRAGMA page_size` as TEXT with column name `cipher_page_size`, so go
/// through the pragma table function and CAST explicitly.
fn read_page_size(conn: &Connection) -> Result<usize, SnapshotError> {
    let value: i64 = conn.query_row(
        "SELECT CAST(page_size AS INTEGER) FROM pragma_page_size",
        [],
        |r| r.get(0),
    )?;
    Ok(value.max(0) as usize)
}

/// Read a pragma that returns exactly one TEXT row, failing closed when it
/// returns nothing or a non-text value: an unverifiable parameter must not
/// default to "match".
fn read_text_pragma(conn: &Connection, name: &str) -> Result<String, SnapshotError> {
    conn.query_row(&format!("PRAGMA {name}"), [], |r| r.get::<_, String>(0))
        .map_err(|e| SnapshotError::Unsupported(format!("PRAGMA {name} unreadable: {e}")))
}

fn read_int_pragma(conn: &Connection, name: &str) -> Result<i64, SnapshotError> {
    let raw = read_text_pragma(conn, name)?;
    raw.trim()
        .parse::<i64>()
        .map_err(|_| SnapshotError::Unsupported(format!("PRAGMA {name} returned non-integer")))
}

/// Read the full cipher parameter set of a keyed connection.
fn read_cipher_params(conn: &Connection) -> Result<CipherParams, SnapshotError> {
    let params = CipherParams {
        page_size: read_page_size(conn)?,
        use_hmac: read_int_pragma(conn, "cipher_use_hmac")? != 0,
        hmac_algorithm: read_text_pragma(conn, "cipher_hmac_algorithm")?,
        kdf_algorithm: read_text_pragma(conn, "cipher_kdf_algorithm")?,
        kdf_iter: read_int_pragma(conn, "kdf_iter")?,
        plaintext_header_size: read_int_pragma(conn, "cipher_plaintext_header_size")?,
    };
    if params.page_size == 0 {
        return Err(SnapshotError::Unsupported("source reported page_size 0".into()));
    }
    if !params.use_hmac {
        // Pre-4.0 page format without per-page HMAC. Explicitly unsupported
        // rather than silently copied.
        return Err(SnapshotError::Unsupported(
            "source uses cipher_use_hmac=OFF (legacy SQLCipher format)".into(),
        ));
    }
    if params.plaintext_header_size != 0 {
        return Err(SnapshotError::Unsupported(format!(
            "source uses cipher_plaintext_header_size={}",
            params.plaintext_header_size
        )));
    }
    Ok(params)
}

// ── source opening and pinning ──────────────────────────────────────────

fn apply_keyspec(conn: &Connection, keyspec: &[u8]) -> Result<(), SnapshotError> {
    unsafe {
        let rc = rusqlite::ffi::sqlite3_key(
            conn.handle(),
            keyspec.as_ptr() as *const c_void,
            keyspec.len() as i32,
        );
        if rc != 0 {
            return Err(SnapshotError::Key(format!("sqlite3_key failed: rc={rc}")));
        }
    }
    Ok(())
}

/// Open `path` READONLY, keyed with `keyspec`, proving the key by reading
/// `sqlite_master`. `query_only` is forced on; no immutable flag is ever
/// used. Returns the connection plus the `cipher_page_size` that unlocked it.
fn open_readonly_keyed(path: &Path, keyspec: &[u8]) -> Result<(Connection, usize), SnapshotError> {
    let mut last_key_error =
        SnapshotError::Unsupported("no supported page size allowed decryption".into());
    for &page_size in &PAGE_SIZE_PROBE {
        let conn = Connection::open_with_flags(path, rusqlite::OpenFlags::SQLITE_OPEN_READ_ONLY)?;
        apply_keyspec(&conn, keyspec)?;
        if page_size != 4096 {
            conn.execute_batch(&format!("PRAGMA cipher_page_size = {page_size};"))?;
        }
        match conn.query_row("SELECT count(*) FROM sqlite_master", [], |r| r.get::<_, i64>(0)) {
            Ok(_) => {
                conn.execute_batch("PRAGMA query_only = ON")?;
                return Ok((conn, page_size));
            }
            Err(_) => {
                last_key_error = SnapshotError::Key(
                    "incorrect key or not an encrypted database at any supported page size"
                        .into(),
                );
            }
        }
    }
    Err(last_key_error)
}

/// A source database opened and key-verified but with no transaction held.
/// [`OpenedSource::begin`] starts the pinned read view; splitting the two
/// lets the supervising process bracket exactly the transaction window.
pub struct OpenedSource {
    conn: Connection,
    cipher: CipherParams,
    salt: [u8; 16],
    keyspec: Vec<u8>,
}

impl OpenedSource {
    /// Open the source READONLY, verify the key, and read its cipher
    /// parameters. No transaction is started and no locks are held beyond
    /// the momentary schema probe.
    pub fn open(path: &Path, keys: &SnapshotKeys) -> Result<Self, SnapshotError> {
        let salt = wx_decrypt::read_db_salt(path)
            .map_err(|e| SnapshotError::Key(format!("failed to read database salt: {e}")))?;
        let keyspec = keys.keyspec_for(salt)?;
        let (conn, _page_size) = open_readonly_keyed(path, &keyspec)?;
        let cipher = read_cipher_params(&conn)?;
        Ok(OpenedSource {
            conn,
            cipher,
            salt,
            keyspec,
        })
    }

    pub fn cipher_params(&self) -> &CipherParams {
        &self.cipher
    }

    /// Begin the deferred read transaction and perform a real schema read to
    /// pin the fixed view. From this point until
    /// [`PinnedSnapshot::release`] the source read transaction is held.
    pub fn begin(self) -> Result<PinnedSnapshot, SnapshotError> {
        let conn = self.conn;
        conn.execute_batch("BEGIN DEFERRED")?;
        let mut tables: Vec<String> = Vec::new();
        {
            let mut stmt = conn
                .prepare("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")?;
            let rows = stmt.query_map([], |row| row.get::<_, String>(0))?;
            for row in rows {
                tables.push(row?);
            }
        }
        Ok(PinnedSnapshot {
            conn,
            cipher: self.cipher.clone(),
            tables,
            salt: self.salt,
            keyspec: self.keyspec,
        })
    }
}

/// A pinned read view over one live encrypted source database.
pub struct PinnedSnapshot {
    conn: Connection,
    cipher: CipherParams,
    tables: Vec<String>,
    salt: [u8; 16],
    keyspec: Vec<u8>,
}

impl fmt::Debug for PinnedSnapshot {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        // Structure only: never key material or schema contents.
        f.debug_struct("PinnedSnapshot")
            .field("cipher", &self.cipher)
            .field("table_count", &self.tables.len())
            .finish_non_exhaustive()
    }
}

impl PinnedSnapshot {
    /// Convenience wrapper: [`OpenedSource::open`] followed by
    /// [`OpenedSource::begin`].
    pub fn pin(path: &Path, keys: &SnapshotKeys, busy_timeout: Duration) -> Result<Self, SnapshotError> {
        let _ = busy_timeout; // probe path never blocks: key check is immediate
        OpenedSource::open(path, keys)?.begin()
    }

    pub fn cipher_params(&self) -> &CipherParams {
        &self.cipher
    }

    pub fn tables(&self) -> &[String] {
        &self.tables
    }

    pub fn salt(&self) -> [u8; 16] {
        self.salt
    }

    /// What a verified copy of this pinned view must look like.
    pub fn expectation(&self) -> SnapshotExpectation {
        SnapshotExpectation {
            cipher: self.cipher.clone(),
            tables: self.tables.clone(),
            salt: self.salt,
        }
    }

    /// Run the same-key/salt/page-params encrypted backup inside the pinned
    /// transaction, with bounded page steps and a monotonic deadline.
    ///
    /// The destination candidate is created fresh `0600`
    /// (`O_EXCL|O_NOFOLLOW`); it is keyed with the same keyspec — same
    /// derived key AND same salt — and its `cipher_page_size`/`cipher_hmac`
    /// are set from the source before the first page is written. The backup
    /// runs through explicit `sqlite3_backup_*` FFI so the
    /// `sqlite3_backup_finish` result code is checked rather than silently
    /// dropped by a Drop handler. Busy or locked steps fail fast; exceeding
    /// `deadline` aborts with [`SnapshotError::Timeout`], leaving the partial
    /// candidate for the caller to discard.
    pub fn encrypted_backup(
        &self,
        temp_dst: &Path,
        opts: &BackupOptions,
    ) -> Result<BackupStats, SnapshotError> {
        create_candidate_file(temp_dst)?;
        let mut dst = Connection::open(temp_dst)?;
        // Empirically established order (see snapshot tests): the raw keyspec
        // must be applied FIRST and the cipher pragmas after it, before the
        // first page is written — setting cipher_page_size before the key
        // produces a copy that validates at no page size. Within the pragmas,
        // cipher_page_size comes last because cipher_use_hmac/hmac_algorithm
        // reset it to the default.
        apply_keyspec(&dst, &self.keyspec)?;
        dst.execute_batch(&self.cipher.destination_pragmas()?)?;
        let started = Instant::now();
        let stats = unsafe { self.run_raw_backup(&mut dst, opts, started) };
        drop(dst);
        stats
    }

    /// Explicit-FFI backup loop. Safety: `src`/`dst` are valid live
    /// connections for the duration; the raw backup handle is finished
    /// exactly once (explicitly on success, in Drop on early exit).
    unsafe fn run_raw_backup(
        &self,
        dst: &mut Connection,
        opts: &BackupOptions,
        started: Instant,
    ) -> Result<BackupStats, SnapshotError> {
        let main = b"main\0";
        let mut backup = RawBackup::init(dst, &self.conn, main)?;
        let mut steps: u64 = 0;
        loop {
            if let Some(deadline) = opts.deadline {
                let elapsed = started.elapsed();
                if elapsed >= deadline {
                    return Err(SnapshotError::Timeout {
                        elapsed_ms: elapsed.as_millis(),
                    });
                }
            }
            let pages = opts.pages_per_step.clamp(1, i32::MAX as usize) as i32;
            let rc = rusqlite::ffi::sqlite3_backup_step(backup.as_ptr(), pages);
            if rc == rusqlite::ffi::SQLITE_DONE {
                break;
            }
            if rc == rusqlite::ffi::SQLITE_OK {
                steps += 1;
                continue;
            }
            if rc == rusqlite::ffi::SQLITE_BUSY || rc == rusqlite::ffi::SQLITE_LOCKED {
                return Err(SnapshotError::Busy(
                    "backup step returned busy/locked; failing fast".into(),
                ));
            }
            return Err(SnapshotError::Sql(format!(
                "backup step failed: rc={rc}"
            )));
        }
        let page_count = rusqlite::ffi::sqlite3_backup_pagecount(backup.as_ptr()).max(0) as usize;
        // finish() commits the destination; its return code must be checked.
        backup.finish()?;
        Ok(BackupStats {
            page_count,
            page_size: self.cipher.page_size,
            steps: steps.max(1),
        })
    }

    /// End the read transaction and close the source connection, releasing
    /// every source lock immediately. Consumes the pinned view.
    pub fn release(self) {
        // Explicit rollback first so the transaction always ends here; the
        // connection drop then closes the file descriptors.
        let conn = self.conn;
        let _ = conn.execute_batch("ROLLBACK");
        drop(conn);
    }
}

/// Raw `sqlite3_backup_*` handle wrapper: the finish result code is checked
/// when the backup completes normally, and Drop still calls finish exactly
/// once on early exits so no handle leaks.
struct RawBackup {
    b: *mut rusqlite::ffi::sqlite3_backup,
    finished: bool,
}

impl RawBackup {
    unsafe fn init(
        dst: &mut Connection,
        src: &Connection,
        main: &[u8],
    ) -> Result<Self, SnapshotError> {
        let name = main.as_ptr().cast();
        let b = rusqlite::ffi::sqlite3_backup_init(dst.handle(), name, src.handle(), name);
        if b.is_null() {
            return Err(SnapshotError::Sql(
                "sqlite3_backup_init failed".into(),
            ));
        }
        Ok(RawBackup {
            b,
            finished: false,
        })
    }

    fn as_ptr(&self) -> *mut rusqlite::ffi::sqlite3_backup {
        self.b
    }

    fn finish(&mut self) -> Result<(), SnapshotError> {
        if self.finished {
            return Ok(());
        }
        self.finished = true;
        let rc = unsafe { rusqlite::ffi::sqlite3_backup_finish(self.b) };
        if rc != rusqlite::ffi::SQLITE_OK {
            return Err(SnapshotError::Sql(format!(
                "sqlite3_backup_finish failed: rc={rc}"
            )));
        }
        Ok(())
    }
}

impl Drop for RawBackup {
    fn drop(&mut self) {
        if !self.finished {
            self.finished = true;
            unsafe { rusqlite::ffi::sqlite3_backup_finish(self.b) };
        }
    }
}

/// Bounded-step backup parameters.
#[derive(Clone, Copy, Debug)]
pub struct BackupOptions {
    /// Pages copied per `sqlite3_backup_step`.
    pub pages_per_step: usize,
    /// Hard monotonic budget for the whole in-transaction backup (T_max).
    pub deadline: Option<Duration>,
}

impl Default for BackupOptions {
    fn default() -> Self {
        Self {
            pages_per_step: 128,
            deadline: None,
        }
    }
}

#[derive(Clone, Copy, Debug)]
pub struct BackupStats {
    pub page_count: usize,
    pub page_size: usize,
    pub steps: u64,
}

/// Structure a verified published copy must match.
#[derive(Clone, Debug)]
pub struct SnapshotExpectation {
    pub cipher: CipherParams,
    pub tables: Vec<String>,
    pub salt: [u8; 16],
}

#[derive(Clone, Debug)]
pub struct SnapshotMeta {
    pub cipher: CipherParams,
    pub tables: Vec<String>,
    pub salt: [u8; 16],
    pub bytes: u64,
}

/// Offline verification of a completed copy, run only after the source
/// transaction has been released. Reopens the copy READONLY with the same
/// keys and checks, in order: the salt, SQLCipher's per-page
/// `cipher_integrity_check`, SQLite `integrity_check`, the full cipher
/// parameter set against the pinned source view, and the table list
/// captured inside the pinned transaction.
pub fn verify_snapshot(
    copy: &Path,
    keys: &SnapshotKeys,
    expected: &SnapshotExpectation,
) -> Result<SnapshotMeta, SnapshotError> {
    let salt = wx_decrypt::read_db_salt(copy)
        .map_err(|e| SnapshotError::Verify(format!("failed to read copy salt: {e}")))?;
    if salt != expected.salt {
        return Err(SnapshotError::Verify("copy salt differs from source".into()));
    }
    let keyspec = keys.keyspec_for(salt)?;
    let conn = open_readonly_keyed(copy, &keyspec)?.0;

    // SQLCipher's cipher_integrity_check reports one row per problem; a
    // clean database returns NO rows (empirically confirmed on 4.14.0 — it
    // never prints "ok").
    {
        let mut stmt = conn.prepare("PRAGMA cipher_integrity_check")?;
        let mut rows = stmt.query([])?;
        let mut problems: Vec<String> = Vec::new();
        while let Some(row) = rows.next()? {
            let line: String = row.get(0).unwrap_or_else(|_| "<unreadable>".into());
            problems.push(line);
            if problems.len() >= 10 {
                break; // report shape, not page-by-page detail
            }
        }
        if !problems.is_empty() {
            return Err(SnapshotError::Verify(format!(
                "cipher_integrity_check reported: {}",
                problems.join("; ")
            )));
        }
    }

    let integrity: String = conn.query_row("PRAGMA integrity_check", [], |r| r.get(0))?;
    if integrity != "ok" {
        return Err(SnapshotError::Verify(format!(
            "integrity_check reported: {integrity}"
        )));
    }

    let cipher = read_cipher_params(&conn)?;
    if cipher != expected.cipher {
        return Err(SnapshotError::Verify(format!(
            "copy cipher params {cipher:?} differ from pinned source view {:?}",
            expected.cipher
        )));
    }

    let mut tables: Vec<String> = Vec::new();
    {
        let mut stmt = conn
            .prepare("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")?;
        let rows = stmt.query_map([], |row| row.get::<_, String>(0))?;
        for row in rows {
            tables.push(row?);
        }
    }
    let mut expected_tables = expected.tables.clone();
    expected_tables.sort();
    if tables != expected_tables {
        return Err(SnapshotError::Verify(
            "copy table list differs from pinned source view".into(),
        ));
    }
    let bytes = std::fs::metadata(copy)?.len();
    drop(conn);
    Ok(SnapshotMeta {
        cipher,
        tables,
        salt,
        bytes,
    })
}

/// Open a published snapshot copy READONLY with the snapshot keys. This is
/// the only door later archive stages (inspection, collection) use to read
/// source data — they never touch the live source or the old in-place
/// decryption cache.
pub fn open_snapshot_copy(path: &Path, keys: &SnapshotKeys) -> Result<Connection, SnapshotError> {
    let salt = wx_decrypt::read_db_salt(path)
        .map_err(|e| SnapshotError::Key(format!("failed to read copy salt: {e}")))?;
    let keyspec = keys.keyspec_for(salt)?;
    Ok(open_readonly_keyed(path, &keyspec)?.0)
}

/// Durably publish a verified candidate: fsync the file, make it exactly
/// `0400`, `link()` it into place atomically (never replacing an existing
/// generation), remove the candidate name, and fsync the parent directory.
/// The candidate must be a regular file; the final path must not exist.
pub fn publish_snapshot(temp: &Path, final_path: &Path) -> Result<(), SnapshotError> {
    let meta = std::fs::symlink_metadata(temp)?;
    if !meta.is_file() {
        return Err(SnapshotError::Io(format!(
            "candidate {} is not a regular file",
            temp.display()
        )));
    }
    {
        let file = File::open(temp)?;
        file.sync_all()?;
    }
    std::fs::set_permissions(temp, std::fs::Permissions::from_mode(0o400))?;
    let parent = final_path
        .parent()
        .ok_or_else(|| SnapshotError::Io("final path has no parent".into()))?;
    ensure_private_dir(parent)?;
    let rc = unsafe { libc::link(cstr(temp)?.as_ptr(), cstr(final_path)?.as_ptr()) };
    if rc != 0 {
        let err = std::io::Error::last_os_error();
        if err.kind() == std::io::ErrorKind::AlreadyExists {
            return Err(SnapshotError::AlreadyPublished(final_path.to_path_buf()));
        }
        return Err(SnapshotError::Io(format!(
            "atomic link publication failed: {err}"
        )));
    }
    std::fs::remove_file(temp)?;
    {
        let dir = File::open(parent)?;
        dir.sync_all()?;
    }
    let final_mode = PermissionsExt::mode(&std::fs::metadata(final_path)?.permissions());
    if final_mode & 0o7777 != 0o400 {
        return Err(SnapshotError::Verify(format!(
            "published file mode {final_mode:o} is not exactly 0400"
        )));
    }
    Ok(())
}

/// Result of enumerating a source directory.
#[derive(Debug)]
pub struct SourceEnumeration {
    /// Every `.db` file found, sorted; unknown `.db` names are included (and
    /// reported as such) rather than silently skipped.
    pub databases: Vec<PathBuf>,
    /// Regular files that are not databases (sidecars, logs); counted, never
    /// silently forgotten.
    pub other_regular_files: usize,
}

/// Enumerate every `.db` file under `root` (the `db_storage` directory).
/// Symlinks anywhere are hard errors; so are unreadable directories and
/// unknown file types. Everything found is snapshotted or explicitly
/// reported — the archive path must not silently skip a source database.
pub fn enumerate_source_databases(root: &Path) -> Result<SourceEnumeration, SnapshotError> {
    let mut result = SourceEnumeration {
        databases: Vec::new(),
        other_regular_files: 0,
    };
    collect_db_files(root, &mut result)?;
    result.databases.sort();
    Ok(result)
}

fn collect_db_files(dir: &Path, out: &mut SourceEnumeration) -> Result<(), SnapshotError> {
    let entries = std::fs::read_dir(dir).map_err(|e| {
        SnapshotError::Io(format!("read_dir {} failed: {e}", dir.display()))
    })?;
    for entry in entries {
        let entry = entry.map_err(|e| {
            SnapshotError::Io(format!("readdir {} failed: {e}", dir.display()))
        })?;
        let meta = std::fs::symlink_metadata(entry.path()).map_err(|e| {
            SnapshotError::Io(format!("lstat {:?} failed: {e}", entry.file_name()))
        })?;
        let file_type = meta.file_type();
        if file_type.is_symlink() {
            return Err(SnapshotError::Io(format!(
                "symlink found in source tree: {}",
                entry.path().display()
            )));
        }
        if file_type.is_dir() {
            collect_db_files(&entry.path(), out)?;
        } else if file_type.is_file() {
            if entry.path().extension().is_some_and(|e| e == "db") {
                out.databases.push(entry.path());
            } else {
                out.other_regular_files += 1;
            }
        } else {
            return Err(SnapshotError::Unsupported(format!(
                "unknown file type in source tree: {}",
                entry.path().display()
            )));
        }
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::os::raw::c_void;
    use tempfile::TempDir;

    fn create_encrypted_db(path: &Path, raw_key: &[u8; 32], setup_sql: &str, wal: bool) {
        if let Some(parent) = path.parent() {
            std::fs::create_dir_all(parent).unwrap();
        }
        let conn = Connection::open(path).unwrap();
        unsafe {
            let rc =
                rusqlite::ffi::sqlite3_key(conn.handle(), raw_key.as_ptr() as *const c_void, 32);
            assert_eq!(rc, 0, "sqlite3_key failed during test DB creation");
        }
        if wal {
            conn.execute_batch("PRAGMA journal_mode=WAL;").unwrap();
        }
        conn.execute_batch(setup_sql).unwrap();
        conn.close().unwrap();
    }

    fn derived_pairs(raw_key: &[u8; 32], path: &Path) -> Vec<wx_decrypt::EncKeyPair> {
        let salt = wx_decrypt::read_db_salt(path).unwrap();
        vec![wx_decrypt::EncKeyPair {
            key: wx_decrypt::kdf::derive_enc_key(raw_key, &salt, &wx_decrypt::MACOS_4_1_7_31),
            salt,
        }]
    }

    fn rows(conn: &Connection, table: &str) -> Vec<(i64, String)> {
        let mut stmt = conn
            .prepare(&format!("SELECT rowid, name FROM {table} ORDER BY rowid"))
            .unwrap();
        let rows = stmt
            .query_map([], |r| Ok((r.get::<_, i64>(0)?, r.get::<_, String>(1)?)))
            .unwrap();
        rows.collect::<Result<Vec<_>, _>>().unwrap()
    }

    #[test]
    fn snapshot_copy_matches_begin_time_view_under_concurrent_writes() {
        let tmp = TempDir::new().unwrap();
        let src = tmp.path().join("message_0.db");
        let raw_key = [0xA7_u8; 32];
        create_encrypted_db(
            &src,
            &raw_key,
            "CREATE TABLE msg (name TEXT, server_id INTEGER); \
             INSERT INTO msg VALUES ('base-1', 1), ('base-2', 2);",
            true, // WAL, like live WeChat sources
        );

        let keys = SnapshotKeys::from_raw(raw_key);
        let source = OpenedSource::open(&src, &keys).unwrap();
        let pinned = source.begin().unwrap();

        // A concurrent writer commits while the read view is pinned. The copy
        // must still equal the BEGIN-time state, and the backup must not
        // restart to pick the new commit up.
        let writer = Connection::open(&src).unwrap();
        unsafe {
            rusqlite::ffi::sqlite3_key(
                writer.handle(),
                raw_key.as_ptr() as *const c_void,
                32,
            );
        }
        writer
            .execute("INSERT INTO msg VALUES ('late-commit', 3)", [])
            .unwrap();

        let candidate = tmp.path().join("candidate.db");
        let stats = pinned
            .encrypted_backup(
                &candidate,
                &BackupOptions {
                    pages_per_step: 2,
                    deadline: Some(Duration::from_secs(30)),
                },
            )
            .unwrap();
        assert!(stats.page_count > 0);
        let expectation = pinned.expectation();
        pinned.release();

        // Writer proceeds fine once the source transaction is released.
        writer
            .execute("INSERT INTO msg VALUES ('after-release', 4)", [])
            .unwrap();

        let meta = verify_snapshot(&candidate, &keys, &expectation).unwrap();
        assert_eq!(meta.cipher.page_size, expectation.cipher.page_size);

        let copy_conn = open_snapshot_copy(&candidate, &keys).unwrap();
        let got = rows(&copy_conn, "msg");
        assert_eq!(
            got,
            vec![(1, "base-1".into()), (2, "base-2".into())],
            "copy must equal BEGIN-time view, not later commits"
        );
    }

    #[test]
    fn derived_only_keys_snapshot_without_raw_key() {
        let tmp = TempDir::new().unwrap();
        let src = tmp.path().join("contact.db");
        let raw_key = [0x5C_u8; 32];
        create_encrypted_db(
            &src,
            &raw_key,
            "CREATE TABLE contact (name TEXT); INSERT INTO contact VALUES ('u1');",
            false,
        );

        let keys = SnapshotKeys::from_derived_pairs(&derived_pairs(&raw_key, &src));
        assert!(keys.is_derived_only());
        let pinned = PinnedSnapshot::pin(&src, &keys, Duration::from_millis(500)).unwrap();
        let candidate = tmp.path().join("candidate.db");
        pinned
            .encrypted_backup(&candidate, &BackupOptions::default())
            .unwrap();
        let expectation = pinned.expectation();
        pinned.release();
        let meta = verify_snapshot(&candidate, &keys, &expectation).unwrap();
        assert_eq!(meta.tables, expectation.tables);

        // A salt with no registered derived pair fails closed in derived-only
        // mode (no raw key to fall back to).
        let other = tmp.path().join("other.db");
        create_encrypted_db(&other, &[0x11_u8; 32], "CREATE TABLE t (x)", false);
        let err = PinnedSnapshot::pin(&other, &keys, Duration::from_millis(500));
        assert!(matches!(err, Err(SnapshotError::Key(_))), "{err:?}");
    }

    #[test]
    fn wrong_key_fails_and_publishes_nothing() {
        let tmp = TempDir::new().unwrap();
        let src = tmp.path().join("session.db");
        create_encrypted_db(
            &src,
            &[0x22_u8; 32],
            "CREATE TABLE s (name TEXT);",
            false,
        );
        let keys = SnapshotKeys::from_raw([0x99_u8; 32]);
        let err = PinnedSnapshot::pin(&src, &keys, Duration::from_millis(500));
        assert!(matches!(err, Err(SnapshotError::Key(_))), "{err:?}");
        assert!(
            !tmp.path().join("candidate.db").exists(),
            "no copy may exist when the key is wrong"
        );
    }

    #[test]
    fn deadline_exceeded_aborts_without_complete_copy() {
        let tmp = TempDir::new().unwrap();
        let src = tmp.path().join("big.db");
        let raw_key = [0x71_u8; 32];
        // Enough pages that a 1-page-per-step backup cannot finish within a
        // tiny deadline: a genuine timeout, not an injected one (~1000 pages
        // of live data at µs-per-step far exceeds the 1ms budget).
        let mut setup = String::from("CREATE TABLE big (k INTEGER PRIMARY KEY, blob BLOB);");
        for i in 0..4000 {
            setup.push_str(&format!(
                "INSERT INTO big VALUES ({i}, x'{}');",
                "ab".repeat(900)
            ));
        }
        create_encrypted_db(&src, &raw_key, &setup, false);

        let keys = SnapshotKeys::from_raw(raw_key);
        let pinned = PinnedSnapshot::pin(&src, &keys, Duration::from_millis(500)).unwrap();
        let candidate = tmp.path().join("candidate.db");
        let err = pinned.encrypted_backup(
            &candidate,
            &BackupOptions {
                pages_per_step: 1,
                deadline: Some(Duration::from_millis(1)),
            },
        );
        let expectation = pinned.expectation();
        pinned.release();
        assert!(matches!(err, Err(SnapshotError::Timeout { .. })), "{err:?}");
        // The candidate is partial and must never verify or publish.
        assert!(
            verify_snapshot(&candidate, &keys, &expectation).is_err(),
            "partial copy must fail verification"
        );
        let published = tmp.path().join("published.db");
        assert!(!published.exists(), "nothing may be published on timeout");
    }

    #[test]
    fn published_copy_is_private_and_readonly() {
        let tmp = TempDir::new().unwrap();
        let src = tmp.path().join("m.db");
        let raw_key = [0x33_u8; 32];
        create_encrypted_db(
            &src,
            &raw_key,
            "CREATE TABLE t (name TEXT); INSERT INTO t VALUES ('x');",
            false,
        );
        let keys = SnapshotKeys::from_raw(raw_key);
        let pinned = PinnedSnapshot::pin(&src, &keys, Duration::from_millis(500)).unwrap();
        let candidate = tmp.path().join("candidate.db");
        pinned
            .encrypted_backup(&candidate, &BackupOptions::default())
            .unwrap();
        let expectation = pinned.expectation();
        pinned.release();
        verify_snapshot(&candidate, &keys, &expectation).unwrap();

        // Candidate must be private from creation (O_EXCL 0600).
        let candidate_mode = PermissionsExt::mode(&std::fs::metadata(&candidate).unwrap().permissions());
        assert_eq!(candidate_mode & 0o077, 0, "candidate must be 0600");

        let final_path = tmp.path().join("gen").join("m.db");
        publish_snapshot(&candidate, &final_path).unwrap();
        assert!(!candidate.exists(), "candidate renamed away");
        let mode = PermissionsExt::mode(&std::fs::metadata(&final_path).unwrap().permissions());
        assert_eq!(mode & 0o7777, 0o400, "published copy must be exactly 0400");
        // Openable read-only with the same keyspec.
        let conn = open_snapshot_copy(&final_path, &keys).unwrap();
        let count: i64 = conn.query_row("SELECT count(*) FROM t", [], |r| r.get(0)).unwrap();
        assert_eq!(count, 1);
        // The generation directory is private.
        let dir_mode =
            PermissionsExt::mode(&std::fs::metadata(final_path.parent().unwrap()).unwrap().permissions());
        assert_eq!(dir_mode & 0o077, 0, "generation directory must be 0700");
    }

    #[test]
    fn publish_never_replaces_an_existing_generation() {
        let tmp = TempDir::new().unwrap();
        let candidate = tmp.path().join("candidate.db");
        std::fs::write(&candidate, b"new").unwrap();
        let final_path = tmp.path().join("gen").join("m.db");
        ensure_private_dir(final_path.parent().unwrap()).unwrap();
        std::fs::write(&final_path, b"existing generation").unwrap();
        let err = publish_snapshot(&candidate, &final_path).unwrap_err();
        assert_eq!(err.kind(), "already_published");
        assert_eq!(
            std::fs::read(&final_path).unwrap(),
            b"existing generation"
        );
    }

    #[test]
    fn candidate_collision_fails_closed() {
        let tmp = TempDir::new().unwrap();
        let src = tmp.path().join("m.db");
        let raw_key = [0x36_u8; 32];
        create_encrypted_db(&src, &raw_key, "CREATE TABLE t (name TEXT);", false);
        let keys = SnapshotKeys::from_raw(raw_key);
        let pinned = PinnedSnapshot::pin(&src, &keys, Duration::from_millis(500)).unwrap();
        let candidate = tmp.path().join("candidate.db");
        std::fs::write(&candidate, b"pre-existing").unwrap();
        let err = pinned.encrypted_backup(&candidate, &BackupOptions::default());
        pinned.release();
        assert!(matches!(err, Err(SnapshotError::Io(_))), "{err:?}");
        assert_eq!(std::fs::read(&candidate).unwrap(), b"pre-existing");
    }

    #[test]
    fn symlinked_candidate_path_fails_closed() {
        let tmp = TempDir::new().unwrap();
        let real = tmp.path().join("real-target.db");
        std::fs::write(&real, b"diversion target").unwrap();
        let candidate = tmp.path().join("candidate.db");
        std::os::unix::fs::symlink(&real, &candidate).unwrap();
        let err = create_candidate_file(&candidate);
        assert!(matches!(err, Err(SnapshotError::Io(_))), "{err:?}");
        // The symlink itself is untouched (O_NOFOLLOW did not follow it).
        assert!(candidate
            .symlink_metadata()
            .unwrap()
            .file_type()
            .is_symlink());
    }

    #[test]
    fn copy_keeps_source_cipher_page_parameters() {
        let tmp = TempDir::new().unwrap();
        let src = tmp.path().join("paged.db");
        let raw_key = [0x44_u8; 32];
        // Non-default page size proves the copy inherits cipher_page_size
        // from the source rather than silently using library defaults.
        create_encrypted_db(
            &src,
            &raw_key,
            "PRAGMA cipher_page_size=1024; CREATE TABLE t (name TEXT); INSERT INTO t VALUES ('p');",
            false,
        );
        let keys = SnapshotKeys::from_raw(raw_key);
        let pinned = PinnedSnapshot::pin(&src, &keys, Duration::from_millis(500)).unwrap();
        assert_eq!(pinned.cipher_params().page_size, 1024);
        let candidate = tmp.path().join("candidate.db");
        pinned
            .encrypted_backup(&candidate, &BackupOptions::default())
            .unwrap();
        let expectation = pinned.expectation();
        pinned.release();
        let meta = verify_snapshot(&candidate, &keys, &expectation).unwrap();
        assert_eq!(meta.cipher.page_size, 1024);
        assert_eq!(meta.cipher, expectation.cipher);
        assert_eq!(meta.cipher.hmac_algorithm, "HMAC_SHA512");
    }

    #[test]
    fn corrupted_copy_fails_cipher_integrity_check() {
        let tmp = TempDir::new().unwrap();
        let src = tmp.path().join("m.db");
        let raw_key = [0x55_u8; 32];
        // Enough data to span several pages so a DEEP page can be corrupted
        // without touching page 1 (whose damage the key check catches first).
        let mut setup = String::from("CREATE TABLE t (k INTEGER PRIMARY KEY, blob BLOB);");
        for i in 0..300 {
            setup.push_str(&format!(
                "INSERT INTO t VALUES ({i}, x'{}');",
                "cd".repeat(400)
            ));
        }
        create_encrypted_db(&src, &raw_key, &setup, false);
        let keys = SnapshotKeys::from_raw(raw_key);
        let pinned = PinnedSnapshot::pin(&src, &keys, Duration::from_millis(2000)).unwrap();
        let candidate = tmp.path().join("candidate.db");
        pinned
            .encrypted_backup(&candidate, &BackupOptions::default())
            .unwrap();
        let expectation = pinned.expectation();
        pinned.release();
        verify_snapshot(&candidate, &keys, &expectation).unwrap();

        // Flip one byte inside page 3's body: page 1 still decrypts (the key
        // check passes) and only cipher_integrity_check catches the HMAC
        // mismatch — proving the per-page integrity gate actually runs.
        let mut bytes = std::fs::read(&candidate).unwrap();
        assert!(bytes.len() > 2 * 4096 + 64, "fixture too small");
        bytes[2 * 4096 + 37] ^= 0xFF;
        std::fs::write(&candidate, bytes).unwrap();
        let err = verify_snapshot(&candidate, &keys, &expectation).unwrap_err();
        assert_eq!(err.kind(), "verify_failed", "{err:?}");
    }

    #[test]
    fn atomic_private_write_is_no_clobber_and_durable() {
        let tmp = TempDir::new().unwrap();
        let target = tmp.path().join("sub").join("manifest.json");
        atomic_private_write(&target, b"{\"complete\": true}").unwrap();
        assert_eq!(std::fs::read(&target).unwrap(), b"{\"complete\": true}");
        let mode = PermissionsExt::mode(&std::fs::metadata(&target).unwrap().permissions());
        assert_eq!(mode & 0o077, 0, "published manifest must be private");
        // No leftover temporaries.
        let siblings: Vec<_> = std::fs::read_dir(target.parent().unwrap())
            .unwrap()
            .filter_map(|e| e.ok())
            .map(|e| e.file_name().to_string_lossy().to_string())
            .collect();
        assert_eq!(siblings, vec!["manifest.json".to_string()]);
        // Writing again is refused; content stays intact.
        let err = atomic_private_write(&target, b"{\"complete\": false}");
        assert!(matches!(err, Err(SnapshotError::AlreadyPublished(_))));
        assert_eq!(std::fs::read(&target).unwrap(), b"{\"complete\": true}");
    }

    #[test]
    fn enumerate_finds_all_source_layout_databases_and_rejects_symlinks() {
        let tmp = TempDir::new().unwrap();
        for rel in [
            "contact/contact.db",
            "session/session.db",
            "message/message_0.db",
            "message/message_1.db",
            "message/message_fts.db",
            "message/contact_fts.db",
            "message/message_resource.db",
            "hardlink/hardlink.db",
        ] {
            std::fs::create_dir_all(tmp.path().join(rel).parent().unwrap()).unwrap();
            std::fs::write(tmp.path().join(rel), b"x").unwrap();
        }
        // Sidecars and notes are counted as other regular files, not dropped.
        std::fs::write(tmp.path().join("message/message_0.db-wal"), b"x").unwrap();
        std::fs::write(tmp.path().join("notes.txt"), b"x").unwrap();
        let found = enumerate_source_databases(tmp.path()).unwrap();
        let names: Vec<String> = found
            .databases
            .iter()
            .map(|p| p.strip_prefix(tmp.path()).unwrap().display().to_string())
            .collect();
        assert_eq!(names.len(), 8, "{names:?}");
        assert!(names.contains(&"hardlink/hardlink.db".to_string()));
        assert!(!names.iter().any(|p| p.ends_with("-wal")));
        assert_eq!(found.other_regular_files, 2);

        // A symlink inside the source tree is a hard error.
        std::os::unix::fs::symlink(
            tmp.path().join("contact/contact.db"),
            tmp.path().join("contact/evil.db"),
        )
        .unwrap();
        let err = enumerate_source_databases(tmp.path());
        assert!(matches!(err, Err(SnapshotError::Io(_))), "{err:?}");
    }

    #[test]
    fn ensure_private_dir_rejects_symlink_components() {
        let tmp = TempDir::new().unwrap();
        let real = tmp.path().join("realdir");
        std::fs::create_dir_all(&real).unwrap();
        let link = tmp.path().join("linkdir");
        std::os::unix::fs::symlink(&real, &link).unwrap();
        let err = ensure_private_dir(&link.join("sub"));
        assert!(matches!(err, Err(SnapshotError::Io(_))), "{err:?}");
    }
}


