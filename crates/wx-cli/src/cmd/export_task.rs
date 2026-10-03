use std::cell::RefCell;
use std::collections::{BTreeMap, HashMap, HashSet};
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::{Arc, Mutex, OnceLock};

use rayon::prelude::*;
use rusqlite::Connection;

use crate::cmd::export_media::{
    export_image_bytes, MediaAsset, MediaKind, MediaState, MediaStats, MediaStatus,
};
use crate::schema::EnrichedMessage;
use crate::util::{format_month, sanitize_filename};
use wx_db::MessageContent;
use wx_media::{DatDecryptOptions, MediaError};

// ---------------------------------------------------------------------------
// Core types
// ---------------------------------------------------------------------------

/// Pre-scanned typed task descriptor produced by the classify stage.
#[derive(Debug, Clone)]
pub enum MediaTask {
    Image {
        md5: String,
        msg_index: usize,
    },
    Voice {
        server_id: i64,
        msg_index: usize,
    },
    Video {
        md5: String,
        create_time: i64,
        msg_index: usize,
    },
    File {
        md5: String,
        create_time: i64,
        title: Option<String>,
        msg_index: usize,
    },
}

impl MediaTask {
    pub fn kind(&self) -> TaskKind {
        match self {
            MediaTask::Image { .. } => TaskKind::Image,
            MediaTask::Voice { .. } => TaskKind::Voice,
            MediaTask::Video { .. } => TaskKind::Video,
            MediaTask::File { .. } => TaskKind::File,
        }
    }

    pub fn msg_index(&self) -> usize {
        match self {
            MediaTask::Image { msg_index, .. } => *msg_index,
            MediaTask::Voice { msg_index, .. } => *msg_index,
            MediaTask::Video { msg_index, .. } => *msg_index,
            MediaTask::File { msg_index, .. } => *msg_index,
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum TaskKind {
    Image,
    Voice,
    Video,
    File,
}

/// Result of resolving a single task.
#[derive(Debug)]
pub struct ResolvedAsset {
    pub msg_index: usize,
    pub asset: Option<MediaAsset>,
    pub tags: Vec<TaskTag>,
    pub error: Option<ExportError>,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum TaskTag {
    ThumbnailImage,
    SilkVoice,
    WxgfTranscoded,
    WxgfFallback,
    FallbackVideo,
    FallbackFile,
    SkippedVideo,
    SkippedFile,
}

impl TaskTag {
    /// Whether this tag should be counted for duplicate messages.
    ///
    /// Matches old `MediaBridge` behavior:
    /// - Image tags (Thumbnail, WxgfTranscoded, WxgfFallback): NOT counted for duplicates
    ///   because old code returned early when `exported.insert()` failed (before counting).
    /// - All other tags: counted for duplicates because old code counted them
    ///   before or regardless of the `exported.insert()` check.
    pub fn counts_on_duplicate(self) -> bool {
        match self {
            TaskTag::ThumbnailImage => false,
            TaskTag::WxgfTranscoded => false,
            TaskTag::WxgfFallback => false,
            TaskTag::SilkVoice => true,
            TaskTag::FallbackVideo => true,
            TaskTag::FallbackFile => true,
            TaskTag::SkippedVideo => true,
            TaskTag::SkippedFile => true,
        }
    }
}

/// Structured error for a single failed task.
#[derive(Debug)]
pub struct ExportError {
    pub task_kind: &'static str,
    pub key: String,
    pub reason: String,
    pub status: MediaStatus,
}

/// Aggregated error summary with grouped reporting.
#[derive(Debug, Default)]
pub struct ErrorSummary {
    pub errors: Vec<ExportError>,
}

impl ErrorSummary {
    pub fn print_report(&self) {
        for error in &self.errors {
            let label = if error.status.state == MediaState::Missing {
                "media unavailable"
            } else {
                "error: media"
            };
            eprintln!(
                "{label} [{}]: {}: {}",
                error.task_kind, error.key, error.reason
            );
        }
    }
}

// ---------------------------------------------------------------------------
// Write gate — thread-safe output filename dedup
// ---------------------------------------------------------------------------

pub struct WriteGate {
    written: Mutex<HashSet<String>>,
}

impl WriteGate {
    pub fn new() -> Self {
        Self {
            written: Mutex::new(HashSet::new()),
        }
    }

    /// Serialize writes and publish a filename only after its write succeeds.
    pub fn write(
        &self,
        filename: &str,
        write: impl FnOnce() -> std::io::Result<()>,
    ) -> std::io::Result<()> {
        let mut written = self
            .written
            .lock()
            .map_err(|_| std::io::Error::other("media write gate poisoned"))?;
        if !written.contains(filename) {
            write()?;
            written.insert(filename.to_string());
        }
        Ok(())
    }
}

// ---------------------------------------------------------------------------
// Thread-local connection pools
// ---------------------------------------------------------------------------

/// Per-thread connection pool for voice media_*.db files.
pub struct VoiceConnectionPool {
    media_dir: PathBuf,
    path_key: u64,
}

impl VoiceConnectionPool {
    pub fn new(media_dir: &Path) -> Self {
        Self {
            media_dir: media_dir.to_path_buf(),
            path_key: path_key(media_dir),
        }
    }

    fn open_all(&self) -> Result<Vec<Connection>, MediaError> {
        let mut paths = Vec::new();
        for entry in read_dir_if_present(&self.media_dir)? {
            let entry = entry?;
            let name = entry.file_name();
            let name = name.to_string_lossy();
            if (name == "media.db" || name.starts_with("media_")) && name.ends_with(".db") {
                paths.push(entry.path());
            }
        }
        paths.sort();
        paths
            .into_iter()
            .map(|path| {
                source_metadata(&path)?
                    .ok_or_else(|| MediaError::NotFound(path.display().to_string()))?;
                Connection::open_with_flags(path, rusqlite::OpenFlags::SQLITE_OPEN_READ_ONLY)
                    .map_err(MediaError::from)
            })
            .collect()
    }

    pub fn with_connections<R>(
        &self,
        f: impl FnOnce(&[Connection]) -> Result<R, MediaError>,
    ) -> Result<R, MediaError> {
        thread_local! {
            static CONNS: RefCell<Option<(u64, Vec<Connection>)>> = const { RefCell::new(None) };
        }
        CONNS.with(|cell| {
            let mut borrow = cell.borrow_mut();
            if let Some((key, conns)) = borrow.as_ref() {
                if *key == self.path_key {
                    return f(conns);
                }
            }
            let conns = self.open_all()?;
            *borrow = Some((self.path_key, conns));
            f(borrow.as_ref().unwrap().1.as_slice())
        })
    }
}

/// Per-thread connection pool for hardlink.db.
pub struct HardlinkConnectionPool {
    db_path: PathBuf,
    path_key: u64,
}

impl HardlinkConnectionPool {
    pub fn new(db_path: PathBuf) -> Self {
        let path_key = path_key(&db_path);
        Self { db_path, path_key }
    }

    pub fn with_connection<R>(
        &self,
        f: impl FnOnce(&Connection) -> Result<R, MediaError>,
    ) -> Result<R, MediaError> {
        thread_local! {
            static CONN: RefCell<Option<(u64, Connection)>> = const { RefCell::new(None) };
        }
        CONN.with(|cell| {
            let mut borrow = cell.borrow_mut();
            if let Some((key, conn)) = borrow.as_ref() {
                if *key == self.path_key {
                    return f(conn);
                }
            }
            source_metadata(&self.db_path)?
                .ok_or_else(|| MediaError::NotFound(self.db_path.display().to_string()))?;
            let conn = Connection::open_with_flags(
                &self.db_path,
                rusqlite::OpenFlags::SQLITE_OPEN_READ_ONLY,
            )?;
            *borrow = Some((self.path_key, conn));
            f(&borrow.as_ref().unwrap().1)
        })
    }
}

// ---------------------------------------------------------------------------
// Shared context — immutable, Arc-shared across rayon threads
// ---------------------------------------------------------------------------

pub struct SharedContext {
    pub attach_dir: PathBuf,
    pub file_dir: PathBuf,
    pub video_dir: PathBuf,
    pub output_media_dir: PathBuf,
    pub dat_opts: DatDecryptOptions,
    pub voice_chat_name_id_hint: Arc<Mutex<Option<i64>>>,
    pub voice_pool: VoiceConnectionPool,
    pub hardlink_pool: HardlinkConnectionPool,
    pub write_gate: WriteGate,
    /// Lazy per-export snapshot of image candidates; the attach tree is
    /// scanned once on the first image lookup, so exports without images
    /// never touch the filesystem and every export starts from a fresh scan.
    // Initialization needs this export's runtime account/talker path.
    image_index: OnceLock<Result<ImageIndex, std::io::Error>>,
    image_base: PathBuf,
}

// ---------------------------------------------------------------------------
// DupMap — dedup tracking
// ---------------------------------------------------------------------------

pub struct DupMap {
    /// (duplicate msg_index, canonical msg_index)
    pub duplicates: Vec<(usize, usize)>,
}

// ---------------------------------------------------------------------------
// Pipeline functions
// ---------------------------------------------------------------------------

/// Build shared context from account/session info (pre-compute stage).
#[allow(clippy::too_many_arguments)]
pub fn build_shared_context(
    attach_dir: PathBuf,
    media_dir: PathBuf,
    file_dir: PathBuf,
    video_dir: PathBuf,
    hardlink_db: PathBuf,
    output_media_dir: PathBuf,
    talker: &str,
    dat_opts: DatDecryptOptions,
) -> SharedContext {
    // Pre-detect XOR key
    let mut dat_opts = dat_opts;
    let username_hash = format!("{:x}", wx_media::md5_hash(talker.as_bytes()));
    let talker_attach = attach_dir.join(&username_hash);
    if let Some(key) = wx_media::detect_xor_key(&talker_attach) {
        dat_opts.xor_key = Some(key);
    }

    // Pre-cache ffmpeg availability (OnceLock, one-time check)
    let _ = wx_media::ffmpeg_available();

    SharedContext {
        voice_chat_name_id_hint: Arc::new(Mutex::new(None)),
        voice_pool: VoiceConnectionPool::new(&media_dir),
        hardlink_pool: HardlinkConnectionPool::new(hardlink_db),
        write_gate: WriteGate::new(),
        image_index: OnceLock::new(),
        image_base: talker_attach,
        attach_dir,
        file_dir,
        video_dir,
        output_media_dir,
        dat_opts,
    }
}

fn update_voice_chat_name_id_hint(ctx: &SharedContext, blob: &wx_media::VoiceBlob) {
    if let Some(chat_name_id) = blob.chat_name_id {
        if let Ok(mut hint) = ctx.voice_chat_name_id_hint.lock() {
            *hint = Some(chat_name_id);
        }
    }
}

/// Stage 1: Classify messages into typed tasks.
pub fn classify(messages: &[EnrichedMessage]) -> Vec<MediaTask> {
    let mut tasks = Vec::new();
    for (idx, em) in messages.iter().enumerate() {
        match &em.message.content {
            MessageContent::Image { md5: Some(md5) } if !md5.is_empty() => {
                tasks.push(MediaTask::Image {
                    md5: md5.clone(),
                    msg_index: idx,
                });
            }
            MessageContent::Voice if em.message.server_id > 0 => {
                tasks.push(MediaTask::Voice {
                    server_id: em.message.server_id,
                    msg_index: idx,
                });
            }
            MessageContent::Video { md5: Some(md5) } if !md5.is_empty() => {
                tasks.push(MediaTask::Video {
                    md5: md5.clone(),
                    create_time: em.message.create_time,
                    msg_index: idx,
                });
            }
            MessageContent::File {
                md5: Some(md5),
                title,
                ..
            } if !md5.is_empty() => {
                tasks.push(MediaTask::File {
                    md5: md5.clone(),
                    create_time: em.message.create_time,
                    title: title.clone(),
                    msg_index: idx,
                });
            }
            _ => {}
        }
    }
    tasks
}

/// Stage 2: Deduplicate tasks by content key.
///
/// Image/voice are safely deduped (md5/server_id fully determines output).
/// Video/file are NOT deduped when they have different fallback parameters
/// (create_time/title), since different parameters may hit different source files.
pub fn dedup(tasks: Vec<MediaTask>) -> (Vec<MediaTask>, DupMap) {
    let mut canonical: HashMap<String, usize> = HashMap::new();
    let mut duplicates: Vec<(usize, usize)> = Vec::new();
    let mut unique: Vec<MediaTask> = Vec::new();

    for task in tasks {
        let msg_idx = task.msg_index();
        let (key, can_dedup) = match &task {
            MediaTask::Image { md5, .. } => (format!("img:{md5}"), true),
            MediaTask::Voice { server_id, .. } => (format!("voi:{server_id}"), true),
            MediaTask::Video {
                md5, create_time, ..
            } => (format!("vid:{md5}:{create_time}"), false),
            MediaTask::File {
                md5,
                create_time,
                title,
                ..
            } => {
                let t = title.as_deref().unwrap_or("");
                (format!("fil:{md5}:{create_time}:{t}"), false)
            }
        };

        if can_dedup {
            if let Some(&canonical_msg_idx) = canonical.get(&key) {
                duplicates.push((msg_idx, canonical_msg_idx));
                continue;
            }
        } else {
            // For video/file: only dedup if the key is identical
            // (same md5 AND same fallback params)
            if let Some(&canonical_msg_idx) = canonical.get(&key) {
                duplicates.push((msg_idx, canonical_msg_idx));
                continue;
            }
        }

        canonical.insert(key, msg_idx);
        unique.push(task);
    }

    (unique, DupMap { duplicates })
}

/// Default rayon thread pool size: min(num_cpus, 4).
fn rayon_default_threads() -> usize {
    let cpus = std::thread::available_parallelism()
        .map(|n| n.get())
        .unwrap_or(1);
    cpus.min(4)
}

/// Stage 3-4: Parallel resolve.
///
/// Tasks are batched by type, each batch runs in parallel within a rayon thread pool.
/// Progress is reported per-type at ~10% intervals.
pub fn resolve_parallel(
    tasks: Vec<MediaTask>,
    ctx: Arc<SharedContext>,
    parallel: Option<usize>,
) -> Vec<ResolvedAsset> {
    let num_threads = parallel.unwrap_or_else(rayon_default_threads).max(1);
    let pool = rayon::ThreadPoolBuilder::new()
        .num_threads(num_threads)
        .build()
        .unwrap();

    // Group by kind
    let mut batches: HashMap<TaskKind, Vec<MediaTask>> = HashMap::new();
    for task in tasks {
        batches.entry(task.kind()).or_default().push(task);
    }

    let order = [
        TaskKind::Image,
        TaskKind::Voice,
        TaskKind::Video,
        TaskKind::File,
    ];
    let mut all_results = Vec::new();

    for kind in order {
        let batch = match batches.remove(&kind) {
            Some(b) => b,
            None => continue,
        };
        let total = batch.len();
        if total == 0 {
            continue;
        }

        let counter = AtomicUsize::new(0);
        let kind_label = match kind {
            TaskKind::Image => "image",
            TaskKind::Voice => "voice",
            TaskKind::Video => "video",
            TaskKind::File => "file",
        };

        let results: Vec<ResolvedAsset> = pool.install(|| {
            batch
                .par_iter()
                .map(|task| {
                    let result = resolve_one(task, &ctx);
                    let done = counter.fetch_add(1, Ordering::Relaxed) + 1;
                    let prev_threshold = (done - 1) * 10 / total;
                    let cur_threshold = done * 10 / total;
                    if cur_threshold != prev_threshold || done == total {
                        eprintln!("media: {kind_label} {done}/{total}");
                    }
                    result
                })
                .collect()
        });

        all_results.extend(results);
    }

    all_results
}

/// Resolve a single task.
fn resolve_one(task: &MediaTask, ctx: &SharedContext) -> ResolvedAsset {
    match task {
        MediaTask::Image { md5, msg_index } => resolve_image(md5, *msg_index, ctx),
        MediaTask::Voice {
            server_id,
            msg_index,
        } => resolve_voice(*server_id, *msg_index, ctx),
        MediaTask::Video {
            md5,
            create_time,
            msg_index,
        } => resolve_video(md5, *create_time, *msg_index, ctx),
        MediaTask::File {
            md5,
            create_time,
            title,
            msg_index,
        } => resolve_file(md5, *create_time, title.as_deref(), *msg_index, ctx),
    }
}

fn path_key(path: &Path) -> u64 {
    use std::hash::{Hash, Hasher};
    let mut hasher = std::collections::hash_map::DefaultHasher::new();
    path.hash(&mut hasher);
    hasher.finish()
}

fn source_metadata(path: &Path) -> Result<Option<std::fs::Metadata>, MediaError> {
    match std::fs::metadata(path) {
        Ok(metadata) => Ok(Some(metadata)),
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(None),
        Err(error) => Err(error.into()),
    }
}

fn read_dir_if_present(
    path: &Path,
) -> Result<std::iter::Flatten<std::option::IntoIter<std::fs::ReadDir>>, MediaError> {
    let entries = match std::fs::read_dir(path) {
        Ok(entries) => Some(entries),
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => None,
        Err(error) => return Err(error.into()),
    };
    Ok(entries.into_iter().flatten())
}

fn source_is_file(path: &Path) -> Result<bool, MediaError> {
    match source_metadata(path)? {
        None => Ok(false),
        Some(metadata) if metadata.is_file() => Ok(true),
        Some(_) => Err(MediaError::InvalidFormat {
            reason: format!("expected media file: {}", path.display()),
        }),
    }
}

fn lookup_status(error: &MediaError) -> MediaStatus {
    match error {
        MediaError::NotFound(_) | MediaError::LookupMiss(_) => {
            MediaStatus::new(MediaState::Missing, Some("missing_local_media"))
        }
        MediaError::Io(error) if error.kind() == std::io::ErrorKind::NotFound => {
            MediaStatus::new(MediaState::Missing, Some("missing_local_media"))
        }
        MediaError::Sqlite(_) | MediaError::SchemaMissing(_) => {
            MediaStatus::new(MediaState::Error, Some("media_database_error"))
        }
        MediaError::Io(_) => MediaStatus::new(MediaState::Error, Some("media_read_error")),
        _ => MediaStatus::new(MediaState::Error, Some("media_lookup_error")),
    }
}

fn failed(
    msg_index: usize,
    task_kind: &'static str,
    key: &str,
    status: MediaStatus,
    reason: impl ToString,
) -> ResolvedAsset {
    ResolvedAsset {
        msg_index,
        asset: None,
        tags: vec![],
        error: Some(ExportError {
            task_kind,
            key: key.to_string(),
            reason: reason.to_string(),
            status,
        }),
    }
}

fn lookup_failed(
    msg_index: usize,
    task_kind: &'static str,
    key: &str,
    error: MediaError,
) -> ResolvedAsset {
    failed(msg_index, task_kind, key, lookup_status(&error), error)
}

fn hard_failed(
    msg_index: usize,
    task_kind: &'static str,
    key: &str,
    code: &'static str,
    error: impl ToString,
) -> ResolvedAsset {
    failed(
        msg_index,
        task_kind,
        key,
        MediaStatus::new(MediaState::Error, Some(code)),
        error,
    )
}

// ---------------------------------------------------------------------------
// Image index — one lazy snapshot of the attach tree per export
// ---------------------------------------------------------------------------

/// Snapshot of every `.dat` image candidate under the talker's attach tree.
///
/// Built once per export (lazily, on the first image lookup) so per-message
/// lookups borrow from one enumeration instead of rescanning the same month
/// directories for every md5. Living on [`SharedContext`] guarantees a fresh
/// snapshot per export, keeping newly downloaded media visible on overlap.
struct ImageIndex {
    /// File name → candidate paths across all month directories.
    files: BTreeMap<String, Vec<PathBuf>>,
}

impl ImageIndex {
    fn build(base: &Path) -> std::io::Result<Self> {
        let mut files: BTreeMap<String, Vec<PathBuf>> = BTreeMap::new();
        if let Some(months) = read_dir_complete(base)? {
            for month in months {
                if !entry_is_dir(&month)? {
                    continue;
                }
                if let Some(images) = read_dir_complete(&month.path().join("Img"))? {
                    for file in images {
                        let name = file.file_name();
                        let name = name.to_string_lossy();
                        if name.ends_with(".dat") {
                            files
                                .entry(name.into_owned())
                                .or_default()
                                .push(file.path());
                        }
                    }
                }
            }
        }
        Ok(Self { files })
    }

    /// Same selection the per-md5 scan produced: prefer `{md5}_h.dat`, then
    /// `{md5}.dat`, then the lexicographically first remaining candidate.
    fn find(&self, md5: &str) -> Option<&Path> {
        let mut best: Option<(u8, &Path)> = None;
        for (name, paths) in self
            .files
            .range::<str, _>((std::ops::Bound::Included(md5), std::ops::Bound::Unbounded))
        {
            if !name.starts_with(md5) {
                break;
            }
            let rank = match &name[md5.len()..] {
                "_h.dat" => 0,
                ".dat" => 1,
                _ => 2,
            };
            for path in paths {
                let candidate = (rank, path.as_path());
                if best.is_none_or(|current| candidate < current) {
                    best = Some(candidate);
                }
            }
        }
        best.map(|(_, path)| path)
    }
}

/// Collect one complete directory enumeration, restarting after interruptions.
///
/// A `ReadDir` iterator can become exhausted after an error, so merely
/// skipping an `Interrupted` result would silently drop the entries that were
/// not reached yet; the enumeration restarts from a fresh open instead.
/// Absent directories enumerate as empty; every other error is a hard error.
fn enumerate_complete<T, I>(
    mut open: impl FnMut() -> std::io::Result<Option<I>>,
) -> std::io::Result<Option<Vec<T>>>
where
    I: Iterator<Item = std::io::Result<T>>,
{
    loop {
        let entries = match open() {
            Ok(Some(entries)) => entries,
            Ok(None) => return Ok(None),
            Err(error) if error.kind() == std::io::ErrorKind::Interrupted => continue,
            Err(error) => return Err(error),
        };
        let mut collected = Vec::new();
        let mut interrupted = false;
        for entry in entries {
            match entry {
                Ok(entry) => collected.push(entry),
                Err(error) if error.kind() == std::io::ErrorKind::Interrupted => {
                    interrupted = true;
                    break;
                }
                Err(error) => return Err(error),
            }
        }
        if !interrupted {
            return Ok(Some(collected));
        }
    }
}

fn read_dir_complete(dir: &Path) -> std::io::Result<Option<Vec<std::fs::DirEntry>>> {
    enumerate_complete(|| match std::fs::read_dir(dir) {
        Ok(entries) => Ok(Some(entries)),
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(None),
        Err(error) => Err(error),
    })
}

fn entry_is_dir(entry: &std::fs::DirEntry) -> std::io::Result<bool> {
    loop {
        match entry.file_type() {
            Ok(file_type) => return Ok(file_type.is_dir()),
            Err(error) if error.kind() == std::io::ErrorKind::Interrupted => continue,
            Err(error) => return Err(error),
        }
    }
}

#[derive(Debug)]
enum ImageLookupError<'a> {
    Missing,
    Index(&'a std::io::Error),
}

/// Strict scans preserve selection conventions without lossy filesystem errors.
///
/// The first lookup of an export builds the snapshot; later lookups borrow
/// from it without touching the filesystem again.
fn find_image<'a>(ctx: &'a SharedContext, md5: &str) -> Result<&'a Path, ImageLookupError<'a>> {
    match ctx
        .image_index
        .get_or_init(|| ImageIndex::build(&ctx.image_base))
    {
        Ok(index) => index.find(md5).ok_or(ImageLookupError::Missing),
        Err(error) => Err(ImageLookupError::Index(error)),
    }
}

fn resolve_image(md5: &str, msg_index: usize, ctx: &SharedContext) -> ResolvedAsset {
    let path = match find_image(ctx, md5) {
        Ok(path) => path,
        Err(ImageLookupError::Missing) => {
            return failed(
                msg_index,
                "image",
                md5,
                MediaStatus::new(MediaState::Missing, Some("missing_local_media")),
                "local image not found",
            );
        }
        Err(ImageLookupError::Index(error)) => {
            return hard_failed(msg_index, "image", md5, "media_read_error", error);
        }
    };
    let data = match std::fs::read(path) {
        Ok(data) => data,
        Err(error) => return lookup_failed(msg_index, "image", md5, error.into()),
    };
    let decoded = match wx_media::decrypt_dat(&data, &ctx.dat_opts) {
        Ok(decoded) => decoded,
        Err(error) => return hard_failed(msg_index, "image", md5, "media_decode_error", error),
    };
    let (data, ext, transcoded, fallback) = match export_image_bytes(decoded.data, &decoded.ext) {
        Ok(result) => result,
        Err(error) => return hard_failed(msg_index, "image", md5, "media_decode_error", error),
    };
    let filename = format!("{md5}.{ext}");
    if let Err(error) = ctx.write_gate.write(&filename, || {
        std::fs::write(ctx.output_media_dir.join(&filename), &data)
    }) {
        return hard_failed(msg_index, "image", md5, "media_write_error", error);
    }
    let mut tags = vec![];
    if path
        .file_name()
        .is_some_and(|name| name.to_string_lossy().contains("_t."))
    {
        tags.push(TaskTag::ThumbnailImage);
    }
    if transcoded {
        tags.push(TaskTag::WxgfTranscoded);
    }
    if fallback {
        tags.push(TaskTag::WxgfFallback);
    }
    ResolvedAsset {
        msg_index,
        asset: Some(MediaAsset {
            kind: MediaKind::Image,
            filename,
        }),
        tags,
        error: None,
    }
}

fn resolve_voice(server_id: i64, msg_index: usize, ctx: &SharedContext) -> ResolvedAsset {
    let key = server_id.to_string();
    let hint = match ctx.voice_chat_name_id_hint.lock() {
        Ok(hint) => *hint,
        Err(error) => return hard_failed(msg_index, "voice", &key, "media_context_error", error),
    };
    let blob = ctx.voice_pool.with_connections(|conns| {
        let mut found = None;
        // A good shard cannot mask a corrupt/schema-invalid shard.
        for conn in conns {
            match wx_media::extract_voice_with_conn_hint(conn, &key, hint) {
                Ok(blob) => {
                    if found.is_none() {
                        found = Some(blob);
                    }
                }
                Err(MediaError::LookupMiss(_) | MediaError::NotFound(_)) => {}
                Err(error) => return Err(error),
            }
        }
        found.ok_or_else(|| MediaError::LookupMiss(format!("voice {key}")))
    });
    let blob = match blob {
        Ok(blob) => blob,
        Err(error) => return lookup_failed(msg_index, "voice", &key, error),
    };
    update_voice_chat_name_id_hint(ctx, &blob);
    let audio = match wx_media::transcode_silk_to_mp3(&blob.data) {
        Ok(audio) => audio,
        Err(error) => return hard_failed(msg_index, "voice", &key, "media_decode_error", error),
    };
    let filename = format!("{key}.{}", audio.ext);
    if let Err(error) = ctx.write_gate.write(&filename, || {
        std::fs::write(ctx.output_media_dir.join(&filename), &audio.data)
    }) {
        return hard_failed(msg_index, "voice", &key, "media_write_error", error);
    }
    ResolvedAsset {
        msg_index,
        asset: Some(MediaAsset {
            kind: MediaKind::Voice,
            filename,
        }),
        tags: if audio.transcoded {
            vec![]
        } else {
            vec![TaskTag::SilkVoice]
        },
        error: None,
    }
}

#[allow(clippy::too_many_arguments)]
fn copy_source(
    source: &Path,
    filename: String,
    kind: MediaKind,
    label: &'static str,
    md5: &str,
    msg_index: usize,
    ctx: &SharedContext,
    tags: Vec<TaskTag>,
) -> ResolvedAsset {
    let mut input = match std::fs::File::open(source) {
        Ok(input) => input,
        Err(error) => return lookup_failed(msg_index, label, md5, error.into()),
    };
    if let Err(error) = ctx.write_gate.write(&filename, || {
        let mut output = std::fs::File::create(ctx.output_media_dir.join(&filename))?;
        std::io::copy(&mut input, &mut output)?;
        Ok(())
    }) {
        return hard_failed(msg_index, label, md5, "media_write_error", error);
    }
    ResolvedAsset {
        msg_index,
        asset: Some(MediaAsset { kind, filename }),
        tags,
        error: None,
    }
}

fn find_video(dir: &Path, md5: &str, month: &str) -> Result<Option<PathBuf>, MediaError> {
    let target = format!("{md5}.mp4");
    let hint = dir.join(month).join(&target);
    if source_is_file(&hint)? {
        return Ok(Some(hint));
    }
    for entry in read_dir_if_present(dir)? {
        let entry = entry?;
        let name = entry.file_name();
        let name = name.to_string_lossy();
        let bytes = name.as_bytes();
        if bytes.len() != 7
            || bytes[4] != b'-'
            || !bytes[..4].iter().all(u8::is_ascii_digit)
            || !bytes[5..].iter().all(u8::is_ascii_digit)
            || !(1..=12).contains(&((bytes[5] - b'0') * 10 + bytes[6] - b'0'))
            || name == month
        {
            continue;
        }
        let path = entry.path().join(&target);
        if source_is_file(&path)? {
            return Ok(Some(path));
        }
    }
    Ok(None)
}

struct HardlinkSource {
    file_name: String,
    dir1: String,
    dir2: String,
}

/// The library drops failed row decodes; export must retain those database errors.
fn query_hardlink(
    conn: &Connection,
    kind: &'static str,
    key: &str,
) -> Result<Vec<HardlinkSource>, MediaError> {
    let tables = match kind {
        "video" => ["video_hardlink_info_v3", "video_hardlink_info_v4"],
        "file" => ["file_hardlink_info_v3", "file_hardlink_info_v4"],
        _ => {
            return Err(MediaError::InvalidFormat {
                reason: "unsupported hardlink type".into(),
            })
        }
    };
    let mut selected = None;
    for table in tables {
        let exists: bool = conn.query_row(
            "SELECT EXISTS(SELECT 1 FROM sqlite_master WHERE type='table' AND name=?1)",
            [table],
            |row| row.get(0),
        )?;
        if exists {
            selected = Some(table);
            break;
        }
    }
    let table =
        selected.ok_or_else(|| MediaError::SchemaMissing(format!("{kind} hardlink table")))?;
    let sql = format!(
        "SELECT f.file_name, IFNULL(d1.username, ''), IFNULL(d2.username, '')
         FROM {table} f
         LEFT JOIN dir2id d1 ON d1.rowid = f.dir1
         LEFT JOIN dir2id d2 ON d2.rowid = f.dir2
         WHERE f.md5 = ?1 OR f.file_name LIKE ?2 || '%'"
    );
    let mut stmt = conn.prepare(&sql)?;
    let entries = stmt
        .query_map(rusqlite::params![key, key], |row| {
            Ok(HardlinkSource {
                file_name: row.get(0)?,
                dir1: row.get(1)?,
                dir2: row.get(2)?,
            })
        })?
        .collect::<Result<Vec<_>, _>>()?;
    if entries.is_empty() {
        return Err(MediaError::LookupMiss(format!("{kind} {key}")));
    }
    Ok(entries)
}

fn resolve_video(
    md5: &str,
    create_time: i64,
    msg_index: usize,
    ctx: &SharedContext,
) -> ResolvedAsset {
    let entries = match ctx
        .hardlink_pool
        .with_connection(|conn| query_hardlink(conn, "video", md5))
    {
        Ok(entries) => entries,
        Err(MediaError::LookupMiss(_) | MediaError::NotFound(_)) => vec![],
        Err(error) => return lookup_failed(msg_index, "video", md5, error),
    };
    if let Some(entry) = entries.first() {
        let candidates = [
            ctx.attach_dir
                .join(&entry.dir1)
                .join(&entry.dir2)
                .join("Video")
                .join(&entry.file_name),
            ctx.attach_dir
                .join(&entry.dir1)
                .join(&entry.dir2)
                .join(&entry.file_name),
            ctx.attach_dir
                .join(&entry.dir1)
                .join("Video")
                .join(&entry.file_name),
        ];
        for source in candidates {
            match source_is_file(&source) {
                Ok(true) => {
                    return copy_source(
                        &source,
                        entry.file_name.clone(),
                        MediaKind::Video,
                        "video",
                        md5,
                        msg_index,
                        ctx,
                        vec![],
                    )
                }
                Ok(false) => {}
                Err(error) => return lookup_failed(msg_index, "video", md5, error),
            }
        }
    }
    match find_video(&ctx.video_dir, md5, &format_month(create_time)) {
        Ok(Some(source)) => copy_source(
            &source,
            format!("{md5}.mp4"),
            MediaKind::Video,
            "video",
            md5,
            msg_index,
            ctx,
            vec![TaskTag::FallbackVideo],
        ),
        Ok(None) => {
            let mut result =
                lookup_failed(msg_index, "video", md5, MediaError::NotFound(md5.into()));
            result.tags.push(TaskTag::SkippedVideo);
            result
        }
        Err(error) => lookup_failed(msg_index, "video", md5, error),
    }
}

fn resolve_file(
    md5: &str,
    create_time: i64,
    title: Option<&str>,
    msg_index: usize,
    ctx: &SharedContext,
) -> ResolvedAsset {
    let entries = match ctx
        .hardlink_pool
        .with_connection(|conn| query_hardlink(conn, "file", md5))
    {
        Ok(entries) => entries,
        Err(MediaError::LookupMiss(_) | MediaError::NotFound(_)) => vec![],
        Err(error) => return lookup_failed(msg_index, "file", md5, error),
    };
    if let Some(entry) = entries.first() {
        let candidates = [
            ctx.file_dir
                .join(&entry.dir1)
                .join(&entry.dir2)
                .join(&entry.file_name),
            ctx.file_dir.join(&entry.dir1).join(&entry.file_name),
        ];
        for source in candidates {
            match source_is_file(&source) {
                Ok(true) => {
                    return copy_source(
                        &source,
                        format!("{md5}_{}", entry.file_name),
                        MediaKind::File,
                        "file",
                        md5,
                        msg_index,
                        ctx,
                        vec![],
                    )
                }
                Ok(false) => {}
                Err(error) => return lookup_failed(msg_index, "file", md5, error),
            }
        }
    }
    if let Some(basename) = title.and_then(|title| Path::new(title).file_name()) {
        let source = ctx.file_dir.join(format_month(create_time)).join(basename);
        match source_is_file(&source) {
            Ok(true) => {
                let filename = format!("{md5}_{}", sanitize_filename(&basename.to_string_lossy()));
                return copy_source(
                    &source,
                    filename,
                    MediaKind::File,
                    "file",
                    md5,
                    msg_index,
                    ctx,
                    vec![TaskTag::FallbackFile],
                );
            }
            Ok(false) => {}
            Err(error) => return lookup_failed(msg_index, "file", md5, error),
        }
    }
    let mut result = lookup_failed(msg_index, "file", md5, MediaError::NotFound(md5.into()));
    result.tags.push(TaskTag::SkippedFile);
    result
}

/// Cover eligible messages even when no task can be classified or cache is absent.
pub fn initial_statuses(messages: &[EnrichedMessage], no_media: bool) -> Vec<Option<MediaStatus>> {
    messages
        .iter()
        .map(|message| initial_status(message, no_media))
        .collect()
}

pub fn initial_status(message: &EnrichedMessage, no_media: bool) -> Option<MediaStatus> {
    let has_reference = match &message.message.content {
        MessageContent::Image { md5 }
        | MessageContent::Video { md5 }
        | MessageContent::File { md5, .. } => md5.as_ref().is_some_and(|md5| !md5.is_empty()),
        MessageContent::Voice => message.message.server_id > 0,
        _ => return None,
    };
    Some(if no_media {
        MediaStatus::new(MediaState::MetadataOnly, None)
    } else if !has_reference {
        MediaStatus::new(MediaState::Missing, Some("missing_reference"))
    } else {
        MediaStatus::new(MediaState::Error, Some("media_cache_unavailable"))
    })
}

/// Stage 5: Collect resolved assets back into a media_map indexed by message position.
///
/// Also populates MediaStats from TaskTags.
pub fn collect(
    results: Vec<ResolvedAsset>,
    dup_map: &DupMap,
    total_messages: usize,
) -> (
    Vec<Vec<MediaAsset>>,
    MediaStats,
    ErrorSummary,
    Vec<Option<MediaStatus>>,
) {
    let mut media_map: Vec<Vec<MediaAsset>> = vec![vec![]; total_messages];
    let mut statuses = vec![None; total_messages];
    let mut stats = MediaStats::default();
    let mut errors = ErrorSummary::default();
    let mut by_index = HashMap::new();
    for mut result in results {
        let status = if let Some(error) = result.error.take() {
            let status = error.status;
            // Even a produced fallback must not expose an asset after hard failure.
            result.asset = None;
            errors.errors.push(error);
            status
        } else if result.asset.is_some() {
            MediaStatus::new(MediaState::Available, None)
        } else {
            MediaStatus::new(MediaState::Missing, Some("missing_local_media"))
        };
        statuses[result.msg_index] = Some(status);
        apply_tags(&mut stats, &result.tags);
        by_index.insert(result.msg_index, result);
    }
    for &(duplicate, canonical) in &dup_map.duplicates {
        if let Some(result) = by_index.get(&canonical) {
            statuses[duplicate] = statuses[canonical];
            for tag in &result.tags {
                if tag.counts_on_duplicate() {
                    apply_tags(&mut stats, std::slice::from_ref(tag));
                }
            }
            if let Some(asset) = &result.asset {
                media_map[duplicate].push(asset.clone());
            }
        }
    }
    for (index, result) in by_index {
        if let Some(asset) = result.asset {
            media_map[index].push(asset);
        }
    }
    (media_map, stats, errors, statuses)
}

fn apply_tags(stats: &mut MediaStats, tags: &[TaskTag]) {
    for tag in tags {
        match tag {
            TaskTag::ThumbnailImage => stats.thumbnail_images += 1,
            TaskTag::SilkVoice => stats.silk_voices += 1,
            TaskTag::WxgfTranscoded => stats.wxgf_transcoded += 1,
            TaskTag::WxgfFallback => stats.wxgf_fallback += 1,
            TaskTag::FallbackVideo => stats.fallback_videos += 1,
            TaskTag::FallbackFile => stats.fallback_files += 1,
            TaskTag::SkippedVideo => stats.skipped_videos += 1,
            TaskTag::SkippedFile => stats.skipped_files += 1,
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::Arc;

    #[test]
    fn hardlink_row_decode_error_is_not_lookup_miss_with_available_fallback() {
        let tmp = tempfile::TempDir::new().unwrap();
        let ctx = media_context(tmp.path());
        Connection::open(tmp.path().join("hardlink.db")).unwrap().execute_batch(
            "CREATE TABLE dir2id(username TEXT);
             CREATE TABLE file_hardlink_info_v3(md5 TEXT, file_name TEXT, dir1 INTEGER, dir2 INTEGER);
             INSERT INTO file_hardlink_info_v3 VALUES ('abc', X'ff', 0, 0);",
        ).unwrap();
        let month = format_month(0);
        std::fs::create_dir_all(ctx.file_dir.join(&month)).unwrap();
        std::fs::write(
            ctx.file_dir.join(month).join("report.pdf"),
            b"available file",
        )
        .unwrap();
        let result = resolve_file("abc", 0, Some("report.pdf"), 0, &ctx);
        assert!(result.asset.is_none());
        assert_eq!(
            result.error.unwrap().status,
            MediaStatus::new(MediaState::Error, Some("media_database_error"))
        );
    }

    #[test]
    fn corrupt_voice_shard_remains_hard_even_after_another_shard_returns_blob() {
        let tmp = tempfile::TempDir::new().unwrap();
        let ctx = media_context(tmp.path());
        std::fs::create_dir_all(tmp.path().join("media")).unwrap();
        create_voice_media_db(
            &tmp.path().join("media/media_0.db"),
            &[(1, 0, 1, 12, b"available blob")],
        );
        Connection::open(tmp.path().join("media/media_1.db"))
            .unwrap()
            .execute_batch("CREATE TABLE unrelated (value TEXT);")
            .unwrap();
        let result = resolve_voice(12, 0, &ctx);
        assert!(result.asset.is_none());
        assert_eq!(
            result.error.unwrap().status,
            MediaStatus::new(MediaState::Error, Some("media_database_error"))
        );
    }

    fn media_context(root: &Path) -> SharedContext {
        build_shared_context(
            root.join("attach"),
            root.join("media"),
            root.join("file"),
            root.join("video"),
            root.join("hardlink.db"),
            root.join("output"),
            "wxid_test",
            DatDecryptOptions {
                v2_aes_key: None,
                xor_key: Some(0xa5),
            },
        )
    }

    #[test]
    fn proven_absence_survives_parallel_resolve_and_duplicate_fanout() {
        let tmp = tempfile::TempDir::new().unwrap();
        let tasks = vec![
            MediaTask::Image {
                md5: "absent".into(),
                msg_index: 0,
            },
            MediaTask::Image {
                md5: "absent".into(),
                msg_index: 1,
            },
            MediaTask::Voice {
                server_id: 12,
                msg_index: 2,
            },
            MediaTask::Video {
                md5: "absent".into(),
                create_time: 0,
                msg_index: 3,
            },
            MediaTask::Video {
                md5: "absent".into(),
                create_time: 0,
                msg_index: 4,
            },
            MediaTask::File {
                md5: "absent".into(),
                title: None,
                create_time: 0,
                msg_index: 5,
            },
        ];
        let (tasks, duplicates) = dedup(tasks);
        let results = resolve_parallel(tasks, Arc::new(media_context(tmp.path())), Some(2));
        let (media, stats, diagnostics, statuses) = collect(results, &duplicates, 6);
        assert!(media.iter().all(Vec::is_empty));
        assert_eq!(
            statuses,
            vec![
                Some(MediaStatus::new(
                    MediaState::Missing,
                    Some("missing_local_media"),
                ));
                6
            ]
        );
        assert!(diagnostics
            .errors
            .iter()
            .all(|error| error.status.state == MediaState::Missing));
        assert_eq!(stats.skipped_videos, 2);
        assert_eq!(stats.skipped_files, 1);
        let summary = crate::cmd::export_media::MediaSummary::from_statuses(&statuses, false);
        assert_eq!(
            (
                summary.expected,
                summary.missing,
                summary.errors,
                summary.not_attempted
            ),
            (6, 6, 0, 0)
        );
    }

    #[test]
    fn hardlink_schema_error_cannot_be_masked_by_file_or_video_fallback() {
        let tmp = tempfile::TempDir::new().unwrap();
        let ctx = media_context(tmp.path());
        Connection::open(tmp.path().join("hardlink.db"))
            .unwrap()
            .execute_batch("CREATE TABLE unrelated (value TEXT);")
            .unwrap();
        let month = format_month(0);
        std::fs::create_dir_all(ctx.video_dir.join(&month)).unwrap();
        std::fs::create_dir_all(ctx.file_dir.join(&month)).unwrap();
        std::fs::create_dir_all(&ctx.output_media_dir).unwrap();
        std::fs::write(ctx.video_dir.join(&month).join("abc.mp4"), b"real video").unwrap();
        std::fs::write(ctx.file_dir.join(&month).join("report.pdf"), b"real file").unwrap();
        let results = resolve_parallel(
            vec![
                MediaTask::Video {
                    md5: "abc".into(),
                    create_time: 0,
                    msg_index: 0,
                },
                MediaTask::File {
                    md5: "abc".into(),
                    create_time: 0,
                    title: Some("report.pdf".into()),
                    msg_index: 1,
                },
            ],
            Arc::new(ctx),
            Some(1),
        );
        let (media, _, errors, statuses) = collect(results, &DupMap { duplicates: vec![] }, 2);
        assert!(media.iter().all(Vec::is_empty));
        assert_eq!(errors.errors.len(), 2);
        assert!(statuses.iter().all(|status| *status
            == Some(MediaStatus::new(
                MediaState::Error,
                Some("media_database_error"),
            ))));
        assert_eq!(
            std::fs::read_dir(tmp.path().join("output"))
                .unwrap()
                .count(),
            0
        );
    }

    #[test]
    fn only_typed_absence_is_missing_and_duplicate_hard_errors_remain_errors() {
        for error in [
            MediaError::SchemaMissing("not found".into()),
            MediaError::Sqlite(rusqlite::Error::InvalidQuery),
            MediaError::Io(std::io::Error::from(std::io::ErrorKind::PermissionDenied)),
            MediaError::MissingV2Key,
            MediaError::AesDecryptFailed {
                reason: "missing file".into(),
            },
            MediaError::XorKeyDetectionFailed,
            MediaError::InvalidFormat {
                reason: "not found".into(),
            },
        ] {
            let result = lookup_failed(0, "image", "abc", error);
            let (media, _, _, statuses) = collect(
                vec![result],
                &DupMap {
                    duplicates: vec![(1, 0)],
                },
                2,
            );
            assert!(media.iter().all(Vec::is_empty));
            assert_eq!(statuses[0], statuses[1]);
            assert_eq!(statuses[1].unwrap().state, MediaState::Error);
            let summary = crate::cmd::export_media::MediaSummary::from_statuses(&statuses, false);
            assert_eq!(
                (summary.expected, summary.errors, summary.missing),
                (2, 2, 0)
            );
        }
    }

    #[test]
    fn image_decode_and_output_not_found_are_hard_not_missing() {
        let tmp = tempfile::TempDir::new().unwrap();
        let ctx = media_context(tmp.path());
        let hash = format!("{:x}", wx_media::md5_hash(b"wxid_test"));
        let img = ctx.attach_dir.join(hash).join("2026-10").join("Img");
        std::fs::create_dir_all(&img).unwrap();
        let encrypted: Vec<_> = b"wxgf".iter().map(|byte| byte ^ 0xa5).collect();
        std::fs::write(img.join("bad.dat"), encrypted).unwrap();
        let result = resolve_image("bad", 0, &ctx);
        assert!(result.asset.is_none());
        assert_eq!(
            result.error.unwrap().status,
            MediaStatus::new(MediaState::Error, Some("media_decode_error"))
        );
        let source = tmp.path().join("source.pdf");
        std::fs::write(&source, b"real attachment").unwrap();
        let result = copy_source(
            &source,
            "asset.pdf".into(),
            MediaKind::File,
            "file",
            "abc",
            0,
            &ctx,
            vec![],
        );
        assert!(result.asset.is_none());
        assert_eq!(
            result.error.unwrap().status,
            MediaStatus::new(MediaState::Error, Some("media_write_error"))
        );
    }

    #[test]
    fn interrupted_enumeration_restart_keeps_later_entries() {
        // A ReadDir can be exhausted after an error, so an interrupted scan
        // must restart the directory instead of skipping the error; the file
        // that had not been reached yet may not be lost.
        let mut opens = 0;
        let entries = enumerate_complete(|| {
            opens += 1;
            match opens {
                1 => Ok(Some(
                    vec![
                        Ok("a.dat"),
                        Err(std::io::Error::from(std::io::ErrorKind::Interrupted)),
                    ]
                    .into_iter(),
                )),
                _ => Ok(Some(
                    vec![Ok("a.dat"), Ok("b-after-interrupt.dat")].into_iter(),
                )),
            }
        })
        .unwrap()
        .unwrap();
        assert_eq!(entries, ["a.dat", "b-after-interrupt.dat"]);
    }

    #[test]
    fn interrupted_directory_open_is_retried() {
        let mut opens = 0;
        let entries: Option<Vec<&str>> = enumerate_complete(|| {
            opens += 1;
            if opens == 1 {
                return Err(std::io::Error::from(std::io::ErrorKind::Interrupted));
            }
            Ok(Some(vec![Ok("a.dat")].into_iter()))
        })
        .unwrap();
        assert_eq!(entries, Some(vec!["a.dat"]));
    }

    #[test]
    fn hard_enumeration_errors_stay_hard_not_partial_scans() {
        // Permission failures are never retried away, never turned into
        // absence, and never yield the partial entries collected so far.
        let error = enumerate_complete(|| {
            Ok(Some(
                vec![
                    Ok("a.dat"),
                    Err(std::io::Error::from(std::io::ErrorKind::PermissionDenied)),
                ]
                .into_iter(),
            ))
        })
        .unwrap_err();
        assert_eq!(error.kind(), std::io::ErrorKind::PermissionDenied);
    }

    #[test]
    fn image_snapshot_keeps_selection_conventions_and_refreshes_per_export() {
        let tmp = tempfile::TempDir::new().unwrap();
        let hash = format!("{:x}", wx_media::md5_hash(b"wxid_test"));
        let base = tmp.path().join("attach").join(&hash);
        let put = |month: &str, name: &str| {
            let img = base.join(month).join("Img");
            std::fs::create_dir_all(&img).unwrap();
            std::fs::write(img.join(name), b"dat").unwrap();
        };
        put("2026-01", "md5x_t.dat");
        put("2026-02", "md5x.dat");
        put("2026-01", "md5y.dat");
        put("2026-02", "md5y.dat");
        let ctx = media_context(tmp.path());
        // The exact full image beats the thumbnail.
        assert_eq!(
            find_image(&ctx, "md5x").unwrap().file_name().unwrap(),
            "md5x.dat"
        );
        // The same file name in two months resolves deterministically to the
        // sorted-first path, like the per-md5 scan did.
        assert_eq!(
            find_image(&ctx, "md5y").unwrap(),
            base.join("2026-01").join("Img").join("md5y.dat").as_path()
        );
        assert!(matches!(
            find_image(&ctx, "absent"),
            Err(ImageLookupError::Missing)
        ));
        // A new export re-snapshots, so newly downloaded media is visible…
        put("2026-03", "md5x_h.dat");
        let refreshed = media_context(tmp.path());
        assert_eq!(
            find_image(&refreshed, "md5x").unwrap().file_name().unwrap(),
            "md5x_h.dat"
        );
        // …while the old export's snapshot stays stable within that export.
        assert_eq!(
            find_image(&ctx, "md5x").unwrap().file_name().unwrap(),
            "md5x.dat"
        );
    }

    #[test]
    fn image_scan_hard_errors_remain_errors_across_lookups() {
        let tmp = tempfile::TempDir::new().unwrap();
        let ctx = media_context(tmp.path());
        let hash = format!("{:x}", wx_media::md5_hash(b"wxid_test"));
        let img = ctx.attach_dir.join(hash).join("2026-01").join("Img");
        std::fs::create_dir_all(img.parent().unwrap()).unwrap();
        std::fs::write(&img, b"file where the Img directory is expected").unwrap();
        for msg_index in 0..2 {
            let result = resolve_image("abc", msg_index, &ctx);
            assert!(result.asset.is_none());
            assert_eq!(
                result.error.unwrap().status,
                MediaStatus::new(MediaState::Error, Some("media_read_error"))
            );
        }
    }

    #[test]
    fn invalid_source_directory_is_hard_not_absence() {
        let tmp = tempfile::TempDir::new().unwrap();
        let ctx = media_context(tmp.path());
        std::fs::write(&ctx.video_dir, b"not a directory").unwrap();
        let result = resolve_video("abc", 0, 0, &ctx);
        assert!(result.asset.is_none());
        assert_eq!(result.error.unwrap().status.state, MediaState::Error);
    }

    #[test]
    fn test_classify_empty_messages() {
        let tasks = classify(&[]);
        assert!(tasks.is_empty());
    }

    #[test]
    fn test_dedup_no_duplicates() {
        let tasks = vec![
            MediaTask::Image {
                md5: "aaa".to_string(),
                msg_index: 0,
            },
            MediaTask::Image {
                md5: "bbb".to_string(),
                msg_index: 1,
            },
        ];
        let (unique, dup_map) = dedup(tasks);
        assert_eq!(unique.len(), 2);
        assert!(dup_map.duplicates.is_empty());
    }

    #[test]
    fn test_dedup_duplicate_md5_image() {
        let tasks = vec![
            MediaTask::Image {
                md5: "same".to_string(),
                msg_index: 0,
            },
            MediaTask::Image {
                md5: "same".to_string(),
                msg_index: 1,
            },
        ];
        let (unique, dup_map) = dedup(tasks);
        assert_eq!(unique.len(), 1);
        assert_eq!(dup_map.duplicates.len(), 1);
        assert_eq!(dup_map.duplicates[0].0, 1); // duplicate msg_index
    }

    #[test]
    fn test_dedup_video_different_create_time_not_deduped() {
        let tasks = vec![
            MediaTask::Video {
                md5: "same".to_string(),
                create_time: 1000,
                msg_index: 0,
            },
            MediaTask::Video {
                md5: "same".to_string(),
                create_time: 2000,
                msg_index: 1,
            },
        ];
        let (unique, dup_map) = dedup(tasks);
        assert_eq!(unique.len(), 2);
        assert!(dup_map.duplicates.is_empty());
    }

    #[test]
    fn test_dedup_file_different_title_not_deduped() {
        let tasks = vec![
            MediaTask::File {
                md5: "same".to_string(),
                create_time: 1000,
                title: Some("file_a.pdf".to_string()),
                msg_index: 0,
            },
            MediaTask::File {
                md5: "same".to_string(),
                create_time: 1000,
                title: Some("file_b.pdf".to_string()),
                msg_index: 1,
            },
        ];
        let (unique, dup_map) = dedup(tasks);
        assert_eq!(unique.len(), 2);
        assert!(dup_map.duplicates.is_empty());
    }

    #[test]
    fn test_dedup_voice_same_server_id() {
        let tasks = vec![
            MediaTask::Voice {
                server_id: 123,
                msg_index: 0,
            },
            MediaTask::Voice {
                server_id: 123,
                msg_index: 1,
            },
        ];
        let (unique, dup_map) = dedup(tasks);
        assert_eq!(unique.len(), 1);
        assert_eq!(dup_map.duplicates.len(), 1);
    }

    #[test]
    fn failed_write_does_not_publish_asset_to_later_message() {
        let tmp = tempfile::TempDir::new().unwrap();
        let gate = WriteGate::new();
        let path = tmp.path().join("asset");
        let error = gate.write("asset", || {
            Err(std::io::Error::from(std::io::ErrorKind::PermissionDenied))
        });
        assert_eq!(
            error.unwrap_err().kind(),
            std::io::ErrorKind::PermissionDenied
        );
        gate.write("asset", || std::fs::write(&path, b"actual media"))
            .unwrap();
        assert_eq!(std::fs::read(path).unwrap(), b"actual media");
    }

    // --- Parity / integration tests ---

    /// Verify that duplicate messages get the same asset but tags are NOT double-counted.
    /// This matches the old MediaBridge behavior where `exported.insert()` returned early
    /// for duplicates, skipping stat increments.
    #[test]
    fn test_collect_duplicate_image_no_double_tag_count() {
        // Two image messages with same md5 → dedup removes one, collect copies asset
        let tasks = vec![
            MediaTask::Image {
                md5: "abc123".to_string(),
                msg_index: 0,
            },
            MediaTask::Image {
                md5: "abc123".to_string(),
                msg_index: 1,
            },
        ];
        let (unique, dup_map) = dedup(tasks);
        assert_eq!(unique.len(), 1);
        assert_eq!(dup_map.duplicates.len(), 1);

        // Simulate resolve producing a thumbnail + wxgf_transcoded asset
        let results = vec![ResolvedAsset {
            msg_index: 0,
            asset: Some(MediaAsset {
                kind: MediaKind::Image,
                filename: "abc123.png".to_string(),
            }),
            tags: vec![TaskTag::ThumbnailImage, TaskTag::WxgfTranscoded],
            error: None,
        }];

        let (media_map, stats, _errors, _) = collect(results, &dup_map, 2);

        // Both messages should have the asset
        assert_eq!(media_map[0].len(), 1);
        assert_eq!(media_map[1].len(), 1);
        assert_eq!(media_map[0][0].filename, "abc123.png");
        assert_eq!(media_map[1][0].filename, "abc123.png");

        // Tags should only be counted once (for the canonical task), not for the duplicate
        assert_eq!(stats.thumbnail_images, 1);
        assert_eq!(stats.wxgf_transcoded, 1);
    }

    /// Verify that duplicate voice messages DO count silk_voices for each duplicate.
    /// Matches old MediaBridge where silk was counted BEFORE the export check.
    #[test]
    fn test_collect_duplicate_voice_counts_silk() {
        let tasks = vec![
            MediaTask::Voice {
                server_id: 42,
                msg_index: 0,
            },
            MediaTask::Voice {
                server_id: 42,
                msg_index: 1,
            },
        ];
        let (unique, dup_map) = dedup(tasks);
        assert_eq!(unique.len(), 1);

        let results = vec![ResolvedAsset {
            msg_index: 0,
            asset: Some(MediaAsset {
                kind: MediaKind::Voice,
                filename: "42.mp3".to_string(),
            }),
            tags: vec![TaskTag::SilkVoice],
            error: None,
        }];

        let (media_map, stats, _errors, _) = collect(results, &dup_map, 2);

        assert_eq!(media_map[0].len(), 1);
        assert_eq!(media_map[1].len(), 1);
        // silk_voices counted for each message (old behavior: counted before export check)
        assert_eq!(stats.silk_voices, 2);
    }

    /// Verify that duplicate video fallback counts fallback_videos for each duplicate.
    #[test]
    fn test_collect_duplicate_video_fallback_counts() {
        let tasks = vec![
            MediaTask::Video {
                md5: "v1".to_string(),
                create_time: 1000,
                msg_index: 0,
            },
            MediaTask::Video {
                md5: "v1".to_string(),
                create_time: 1000,
                msg_index: 1,
            },
        ];
        let (unique, dup_map) = dedup(tasks);
        assert_eq!(unique.len(), 1);

        let results = vec![ResolvedAsset {
            msg_index: 0,
            asset: Some(MediaAsset {
                kind: MediaKind::Video,
                filename: "v1.mp4".to_string(),
            }),
            tags: vec![TaskTag::FallbackVideo],
            error: None,
        }];

        let (_, stats, _, _) = collect(results, &dup_map, 2);

        // fallback_videos counted for each message (old behavior)
        assert_eq!(stats.fallback_videos, 2);
    }

    /// Verify that duplicate skipped videos DO count skipped_videos for each duplicate.
    /// Matches old MediaBridge where skipped_videos was counted unconditionally.
    #[test]
    fn test_collect_duplicate_skipped_video_counts() {
        let tasks = vec![
            MediaTask::Video {
                md5: "missing".to_string(),
                create_time: 1000,
                msg_index: 0,
            },
            MediaTask::Video {
                md5: "missing".to_string(),
                create_time: 1000,
                msg_index: 1,
            },
        ];
        let (unique, dup_map) = dedup(tasks);
        assert_eq!(unique.len(), 1);

        let results = vec![ResolvedAsset {
            msg_index: 0,
            asset: None,
            tags: vec![TaskTag::SkippedVideo],
            error: None,
        }];

        let (_, stats, _, _) = collect(results, &dup_map, 2);

        // skipped_videos counted for each message (old behavior: unconditional count)
        assert_eq!(stats.skipped_videos, 2);
    }

    /// Verify dedup produces consistent output: two identical image messages
    /// result in the same asset being placed at both msg positions.
    #[test]
    fn test_dedup_produces_consistent_output() {
        let tasks = vec![
            MediaTask::Image {
                md5: "img1".to_string(),
                msg_index: 0,
            },
            MediaTask::Image {
                md5: "img2".to_string(),
                msg_index: 1,
            },
            MediaTask::Image {
                md5: "img1".to_string(), // duplicate of msg 0
                msg_index: 2,
            },
        ];
        let (unique, dup_map) = dedup(tasks);
        assert_eq!(unique.len(), 2);
        assert_eq!(dup_map.duplicates.len(), 1);
        assert_eq!(dup_map.duplicates[0], (2, 0)); // msg 2 is dup of canonical msg 0

        // Simulate resolve for the 2 unique tasks
        let results = vec![
            ResolvedAsset {
                msg_index: 0,
                asset: Some(MediaAsset {
                    kind: MediaKind::Image,
                    filename: "img1.jpg".to_string(),
                }),
                tags: vec![],
                error: None,
            },
            ResolvedAsset {
                msg_index: 1,
                asset: Some(MediaAsset {
                    kind: MediaKind::Image,
                    filename: "img2.jpg".to_string(),
                }),
                tags: vec![],
                error: None,
            },
        ];

        let (media_map, _stats, _errors, _) = collect(results, &dup_map, 3);

        // All 3 messages should have their assets
        assert_eq!(media_map[0].len(), 1);
        assert_eq!(media_map[0][0].filename, "img1.jpg");
        assert_eq!(media_map[1].len(), 1);
        assert_eq!(media_map[1][0].filename, "img2.jpg");
        assert_eq!(media_map[2].len(), 1);
        assert_eq!(media_map[2][0].filename, "img1.jpg"); // duplicate gets canonical's asset
    }

    /// Verify classify correctly maps message types to tasks.
    #[test]
    fn test_classify_mixed_messages() {
        use crate::schema::EnrichedMessage;
        use wx_context::Direction;
        use wx_db::Message;

        let msgs = vec![
            EnrichedMessage {
                message: Message {
                    sort_seq: 0,
                    server_id: 1,
                    local_id: 1,
                    source_shard: None,
                    msg_type: 3,
                    sub_type: 0,
                    sender: "a".into(),
                    talker: "b".into(),
                    create_time: 1000,
                    content: MessageContent::Image {
                        md5: Some("md5_a".into()),
                    },
                    status: 0,
                },
                sender_display_name: "A".into(),
                direction: Direction::Incoming,
                snippet: String::new(),
            },
            EnrichedMessage {
                message: Message {
                    sort_seq: 1,
                    server_id: 2,
                    local_id: 2,
                    source_shard: None,
                    msg_type: 34,
                    sub_type: 0,
                    sender: "a".into(),
                    talker: "b".into(),
                    create_time: 1001,
                    content: MessageContent::Voice,
                    status: 0,
                },
                sender_display_name: "A".into(),
                direction: Direction::Incoming,
                snippet: String::new(),
            },
            EnrichedMessage {
                message: Message {
                    sort_seq: 2,
                    server_id: 3,
                    local_id: 3,
                    source_shard: None,
                    msg_type: 43,
                    sub_type: 0,
                    sender: "a".into(),
                    talker: "b".into(),
                    create_time: 1002,
                    content: MessageContent::Text("hello".into()),
                    status: 0,
                },
                sender_display_name: "A".into(),
                direction: Direction::Incoming,
                snippet: "hello".into(),
            },
        ];

        let tasks = classify(&msgs);
        assert_eq!(tasks.len(), 2); // Text message skipped

        // First task: Image
        assert!(matches!(
            &tasks[0],
            MediaTask::Image { md5, msg_index: 0 } if md5 == "md5_a"
        ));

        // Second task: Voice
        assert!(matches!(
            &tasks[1],
            MediaTask::Voice {
                server_id: 2,
                msg_index: 1
            }
        ));
    }

    /// End-to-end parity test: resolve an image through the new parallel pipeline
    /// and compare with the old MediaBridge serial oracle.
    /// Uses real .dat file I/O with mock XOR-encrypted data.
    #[test]
    fn test_parallel_equals_serial_image_resolve() {
        use crate::cmd::export_media::MediaBridge;

        let tmp = tempfile::TempDir::new().unwrap();
        let root = tmp.path();
        let talker = "wxid_testuser";
        let md5 = "4865625c4e99e4d3b0959a0fe84f41cd";
        let xor_key = 0xa5u8;

        // Create a sample .dat file: XOR-encrypted embedded PNG WXGF
        let png = vec![
            0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A, 0x00, 0x00, 0x00, 0x0D, 0x49, 0x48,
            0x44, 0x52, 0x00, 0x00, 0x00, 0x01, 0x00, 0x00, 0x00, 0x01, 0x08, 0x06, 0x00, 0x00,
            0x00, 0x1F, 0x15, 0xC4, 0x89, 0x00, 0x00, 0x00, 0x0D, 0x49, 0x44, 0x41, 0x54, 0x78,
            0x9C, 0x63, 0xF8, 0xCF, 0xC0, 0xF0, 0x1F, 0x00, 0x05, 0x00, 0x01, 0xFF, 0x89, 0x99,
            0x3D, 0x1D, 0x00, 0x00, 0x00, 0x00, 0x49, 0x45, 0x4E, 0x44, 0xAE, 0x42, 0x60, 0x82,
        ];
        let mut wxgf = b"wxgfmetadata".to_vec();
        wxgf.extend_from_slice(&png);
        let encrypted: Vec<u8> = wxgf.iter().map(|b| b ^ xor_key).collect();

        let username_hash = format!("{:x}", wx_media::md5_hash(talker.as_bytes()));
        let img_dir = root
            .join("attach")
            .join(&username_hash)
            .join("2026-03")
            .join("Img");
        std::fs::create_dir_all(&img_dir).unwrap();
        std::fs::write(img_dir.join(format!("{md5}.dat")), &encrypted).unwrap();

        let output_media = root.join("output");
        std::fs::create_dir_all(&output_media).unwrap();

        let dat_opts = wx_media::DatDecryptOptions {
            v2_aes_key: None,
            xor_key: Some(xor_key),
        };

        // --- Serial oracle (old MediaBridge) ---
        let mut bridge = MediaBridge::new(
            root.join("attach"),
            root.join("media"),
            root.join("file"),
            root.join("video"),
            root.join("hardlink.db"),
            output_media.clone(),
            dat_opts.clone(),
        );
        let serial_assets = bridge.resolve(
            &wx_db::Message {
                sort_seq: 0,
                server_id: 1,
                local_id: 1,
                source_shard: None,
                msg_type: 3,
                sub_type: 0,
                sender: "sender".into(),
                talker: talker.into(),
                create_time: 1_700_000_000,
                content: wx_db::MessageContent::Image {
                    md5: Some(md5.to_string()),
                },
                status: 0,
            },
            talker,
        );

        // --- New parallel pipeline ---
        let ctx = build_shared_context(
            root.join("attach"),
            root.join("media"),
            root.join("file"),
            root.join("video"),
            root.join("hardlink.db"),
            output_media.clone(),
            talker,
            dat_opts,
        );

        // Clean output dir so the new pipeline can write
        let _ = std::fs::remove_dir_all(&output_media);
        std::fs::create_dir_all(&output_media).unwrap();

        let tasks = vec![
            MediaTask::Image {
                md5: md5.to_string(),
                msg_index: 0,
            },
            MediaTask::Image {
                md5: md5.to_string(),
                msg_index: 1,
            },
        ];
        let (tasks, duplicates) = dedup(tasks);
        let results = resolve_parallel(tasks, std::sync::Arc::new(ctx), Some(1));
        let (media_map, stats, errors, statuses) = collect(results, &duplicates, 2);
        assert!(errors.errors.is_empty());
        assert_eq!(
            statuses,
            vec![Some(MediaStatus::new(MediaState::Available, None)); 2]
        );
        let summary = crate::cmd::export_media::MediaSummary::from_statuses(&statuses, false);
        assert_eq!(
            (
                summary.expected,
                summary.available,
                summary.missing,
                summary.errors
            ),
            (2, 2, 0, 0)
        );
        assert_eq!(media_map[0][0].filename, media_map[1][0].filename);

        // Compare: both should produce the same filename
        assert_eq!(serial_assets.len(), 1);
        assert_eq!(media_map[0].len(), 1);
        assert_eq!(serial_assets[0].filename, media_map[0][0].filename);

        // Both should have written the same file
        let serial_bytes = std::fs::read(output_media.join(&serial_assets[0].filename)).unwrap();
        let parallel_bytes = std::fs::read(output_media.join(&media_map[0][0].filename)).unwrap();
        assert_eq!(serial_bytes, parallel_bytes);

        // Stats should match (wxgf_transcoded = 1 in both)
        assert_eq!(bridge.stats.wxgf_transcoded, stats.wxgf_transcoded);
    }

    #[cfg(feature = "audio")]
    fn sample_silk() -> Vec<u8> {
        silk_rs::encode_silk(vec![0_u8; 24_000 / 1_000 * 40 * 2], 24_000, 24_000, true).unwrap()
    }

    fn create_voice_media_db(path: &Path, rows: &[(i64, i64, i64, i64, &[u8])]) {
        let conn = rusqlite::Connection::open(path).unwrap();
        conn.execute_batch(
            "CREATE TABLE VoiceInfo (
                chat_name_id INTEGER,
                create_time INTEGER,
                local_id INTEGER,
                svr_id INTEGER,
                voice_data BLOB,
                data_index TEXT DEFAULT '0'
            );
            CREATE INDEX VoiceInfo_INDEX ON VoiceInfo(chat_name_id, svr_id);",
        )
        .unwrap();
        let mut stmt = conn
            .prepare(
                "INSERT INTO VoiceInfo (chat_name_id, create_time, local_id, svr_id, voice_data)
                 VALUES (?, ?, ?, ?, ?)",
            )
            .unwrap();
        for &(chat_name_id, create_time, local_id, svr_id, data) in rows {
            stmt.execute(rusqlite::params![
                chat_name_id,
                create_time,
                local_id,
                svr_id,
                data
            ])
            .unwrap();
        }
    }

    #[cfg(feature = "audio")]
    #[test]
    fn test_resolve_voice_caches_chat_name_id_hint_after_first_lookup() {
        let tmp = tempfile::TempDir::new().unwrap();
        let media_dir = tmp.path().join("media");
        let output_media = tmp.path().join("output");
        std::fs::create_dir_all(&media_dir).unwrap();
        std::fs::create_dir_all(&output_media).unwrap();

        let silk = sample_silk();
        create_voice_media_db(
            &media_dir.join("media_0.db"),
            &[(55, 1000, 1, 101, &silk), (55, 1001, 2, 102, &silk)],
        );

        let ctx = Arc::new(build_shared_context(
            tmp.path().join("attach"),
            media_dir,
            tmp.path().join("file"),
            tmp.path().join("video"),
            tmp.path().join("hardlink.db"),
            output_media,
            "he593121260",
            wx_media::DatDecryptOptions::default(),
        ));

        assert_eq!(*ctx.voice_chat_name_id_hint.lock().unwrap(), None);

        let first = resolve_voice(101, 0, &ctx);
        assert!(first.error.is_none());
        assert_eq!(*ctx.voice_chat_name_id_hint.lock().unwrap(), Some(55));

        let second = resolve_voice(102, 1, &ctx);
        assert!(second.error.is_none());
        assert_eq!(*ctx.voice_chat_name_id_hint.lock().unwrap(), Some(55));
    }
}
