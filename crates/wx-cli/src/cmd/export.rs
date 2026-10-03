use std::io::Write;
use std::path::PathBuf;

use serde::Serialize;
use wx_context::{
    AccountContext, ContactResolver, DecryptRequest, Direction, PersistentCache, ResolveParams,
};
use wx_db::{is_group_chat, MessageContent, MessageQuery, SortOrder, MAX_QUERY_LIMIT};

use crate::cmd::contacts::build_visibility;
use crate::cmd::export_media::{MediaKind, MediaStats, MediaStatus, MediaSummary};
use crate::cmd::query::resolve_talker;
use crate::output::{JsonEnvelope, PagingMeta, StatsMeta};
use crate::schema::{enrich_message, project_message_items, EnrichedMessage};
use crate::util::{
    decrypt_progress_callback, open_db_all, print_cache_stats, print_detection_note,
    sanitize_filename,
};
use crate::{ExportFormat, SortOrderArg};

// ── JSON export types ───────────────────────────────────────────────

#[derive(Serialize)]
struct ExportEnvelope<T: Serialize> {
    export_info: ExportInfo,
    conversation: ConversationMeta,
    media: MediaSummary,
    #[serde(flatten)]
    envelope: JsonEnvelope<T>,
}

#[derive(Serialize)]
struct ExportInfo {
    version: &'static str,
    exported_at: String,
    generator: String,
}

#[derive(Serialize)]
struct ConversationMeta {
    talker: String,
    display_name: String,
    #[serde(rename = "type")]
    conv_type: &'static str,
    message_count: usize,
    #[serde(skip_serializing_if = "Option::is_none")]
    time_range_start: Option<i64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    time_range_end: Option<i64>,
}

#[derive(Serialize)]
struct ExportedMessage {
    #[serde(flatten)]
    enriched: EnrichedMessage,
    #[serde(skip_serializing_if = "Vec::is_empty")]
    media_files: Vec<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    media_status: Option<MediaStatus>,
}

// ── Main entry ──────────────────────────────────────────────────────

#[allow(clippy::too_many_arguments)]
pub fn cmd_export(
    contact: &str,
    output_dir: PathBuf,
    data_dir: Option<PathBuf>,
    account: Option<String>,
    key: Option<String>,
    since: Option<i64>,
    until: Option<i64>,
    limit: usize,
    offset: usize,
    order: SortOrderArg,
    all: bool,
    format: ExportFormat,
    no_media: bool,
    show_emoji: bool,
    show_hidden: bool,
    parallel: Option<usize>,
) -> Result<(), Box<dyn std::error::Error>> {
    let params = &wx_decrypt::MACOS_4_1_7_31;

    // Bootstrap
    let acct = AccountContext::resolve(&ResolveParams {
        account: account.as_deref(),
        data_dir: data_dir.as_deref(),
        key_hex: key.as_deref(),
    })?;
    print_detection_note(&acct);

    // For --no-media with raw_key: fully direct. Otherwise need cache for media.
    let (db, cache) = if acct.raw_key.is_some() && no_media {
        let (db, _, _) = open_db_all(&acct, decrypt_progress_callback)?;
        (db, None::<PersistentCache>)
    } else if acct.raw_key.is_some() {
        // Messages via direct encrypted open; media files (dat/video/file) live outside
        // db_storage and need decrypted hardlink.db + message_resource DB for resolution.
        // DecryptScope doesn't yet have a MediaOnly variant, so we decrypt all DBs.
        // The message *query* still goes through the direct encrypted WechatDb.
        eprintln!(
            "Direct encrypted open (SQLCipher) for messages; decrypt cache for media resolution"
        );
        let db = wx_context::open_encrypted_db(&acct)?;
        let cache = PersistentCache::new(&acct, params)?;
        let stats = DecryptRequest::new()
            .all()
            .execute_with_progress(&cache, decrypt_progress_callback)?;
        print_cache_stats(&stats);
        (db, Some(cache))
    } else {
        let cache = PersistentCache::new(&acct, params)?;
        let stats = DecryptRequest::new()
            .all()
            .execute_with_progress(&cache, decrypt_progress_callback)?;
        print_cache_stats(&stats);
        let db = wx_db::WechatDb::open(cache.decrypted_root())?;
        (db, Some(cache))
    };
    let resolver = ContactResolver::build(&db)?;
    let visibility = build_visibility(&acct, &resolver);
    let self_wxid = &acct.base_wxid;

    let talker = resolve_talker(contact, &resolver, &db, Some(&visibility), show_hidden)?;
    let is_group = is_group_chat(&talker);

    // Query params
    let effective_order: SortOrder = order.into();
    let effective_limit = if all {
        usize::MAX
    } else {
        wx_db::effective_limit(limit)
    };

    // Batch query
    let mut all_messages = Vec::new();
    let mut cursor = offset;
    let mut total_count: Option<usize> = None;
    let mut total_scanned = 0usize;
    let mut total_skipped = 0usize;
    let mut shard_warnings = Vec::new();

    if all {
        loop {
            let mut query = MessageQuery::for_talker(&talker)
                .limit(MAX_QUERY_LIMIT)
                .offset(cursor)
                .order(effective_order)
                .with_filtered_count(cursor == offset);
            if let Some(s) = since {
                query = query.since(s);
            }
            if let Some(u) = until {
                query = query.until(u);
            }
            let result = db.query_messages(&query)?;
            let count = result.items.len();
            if cursor == offset {
                total_count = Some(
                    result
                        .stats
                        .filtered_count
                        .unwrap_or(result.stats.total_rows),
                );
            }
            // Only capture stats from the first batch (they cover the full query scope)
            if cursor == offset {
                total_scanned = result.stats.total_rows;
                total_skipped = result.stats.skipped;
                shard_warnings = result.shard_warnings;
            }
            all_messages.extend(result.items);
            if count < MAX_QUERY_LIMIT {
                break;
            }
            cursor += count;
        }
    } else {
        loop {
            let remaining = effective_limit.saturating_sub(all_messages.len());
            if remaining == 0 {
                break;
            }
            let batch_size = remaining.min(MAX_QUERY_LIMIT);
            let mut query = MessageQuery::for_talker(&talker)
                .limit(batch_size)
                .offset(cursor)
                .order(effective_order)
                .with_filtered_count(cursor == offset);
            if let Some(s) = since {
                query = query.since(s);
            }
            if let Some(u) = until {
                query = query.until(u);
            }
            let result = db.query_messages(&query)?;
            let count = result.items.len();
            if cursor == offset {
                total_count = Some(
                    result
                        .stats
                        .filtered_count
                        .unwrap_or(result.stats.total_rows),
                );
            }
            if cursor == offset {
                total_scanned = result.stats.total_rows;
                total_skipped = result.stats.skipped;
                shard_warnings = result.shard_warnings;
            }
            all_messages.extend(result.items);
            if count < batch_size {
                break;
            }
            cursor += count;
        }
    }

    // Print warnings
    for w in &shard_warnings {
        eprintln!("warning: shard {}: {}", w.path, w.reason);
    }
    if total_skipped > 0 {
        eprintln!("warning: {total_skipped} messages skipped (decode error)");
    }

    if all_messages.is_empty() {
        eprintln!("No messages found for export.");
        return Ok(());
    }

    // Phase 2: Enrich messages, then apply sender projection
    let enriched: Vec<EnrichedMessage> = all_messages
        .into_iter()
        .map(|m| enrich_message(m, self_wxid, &resolver))
        .collect();
    let projected = project_message_items(enriched, &talker, &visibility, show_hidden);

    if projected.is_empty() {
        eprintln!("No visible messages found for export (all filtered by sender hiding).");
        return Ok(());
    }

    // Create output directories
    std::fs::create_dir_all(&output_dir)?;
    let media_dir = output_dir.join("media");
    if !no_media {
        std::fs::create_dir_all(&media_dir)?;
    }

    // Resolve media via parallel pipeline, retaining a status for every eligible item.
    let (media_map, media_stats, media_statuses) = if no_media {
        (
            vec![vec![]; projected.len()],
            MediaStats::default(),
            crate::cmd::export_task::initial_statuses(&projected, true),
        )
    } else if let Some(c) = cache.as_ref() {
        let attach_dir = acct.data_dir.join("msg").join("attach");
        let decrypted_media = c.decrypted_root().join("message");
        let hardlink_db = c.decrypted_root().join("hardlink").join("hardlink.db");

        let v2_aes_key = wx_media::derive_v2_key_from_dir(&acct.data_dir).ok();
        let dat_opts = wx_media::DatDecryptOptions {
            v2_aes_key,
            xor_key: None,
        };

        let file_dir = acct.data_dir.join("msg").join("file");
        let video_dir = acct.data_dir.join("msg").join("video");

        let ctx = crate::cmd::export_task::build_shared_context(
            attach_dir,
            decrypted_media,
            file_dir,
            video_dir,
            hardlink_db,
            media_dir.clone(),
            &talker,
            dat_opts,
        );

        let tasks = crate::cmd::export_task::classify(&projected);
        let (unique_tasks, dup_map) = crate::cmd::export_task::dedup(tasks);
        let results = crate::cmd::export_task::resolve_parallel(
            unique_tasks,
            std::sync::Arc::new(ctx),
            parallel,
        );
        let (media_map, stats, errors, mut statuses) =
            crate::cmd::export_task::collect(results, &dup_map, projected.len());
        for (status, message) in statuses.iter_mut().zip(&projected) {
            if status.is_none() {
                *status = crate::cmd::export_task::initial_status(message, false);
            }
        }
        errors.print_report();
        (media_map, stats, statuses)
    } else {
        (
            vec![vec![]; projected.len()],
            MediaStats::default(),
            crate::cmd::export_task::initial_statuses(&projected, false),
        )
    };
    for status in media_statuses.iter().flatten() {
        match status.reason {
            Some("missing_reference") => eprintln!("media unavailable: missing reference"),
            Some("media_cache_unavailable") => eprintln!("error: media cache unavailable"),
            _ => {}
        }
    }
    let media_summary = MediaSummary::from_statuses(&media_statuses, no_media);

    let total_media: usize = media_map.iter().map(Vec::len).sum();

    // Output
    let talker_display = resolver.display_name(&talker).to_string();
    let safe_name = sanitize_filename(&talker_display);
    let date_str = chrono::Local::now().format("%Y-%m-%d").to_string();

    match format {
        ExportFormat::Txt => {
            let filename = format!("{safe_name}_{date_str}.txt");
            let out_path = output_dir.join(&filename);
            write_txt(
                &out_path,
                &talker,
                &talker_display,
                self_wxid,
                is_group,
                &projected,
                &media_map,
                &resolver,
                show_emoji,
            )?;
            eprintln!(
                "Exported {} messages, {} media files → {}",
                projected.len(),
                total_media,
                out_path.display()
            );
        }
        ExportFormat::Json => {
            let filename = format!("{safe_name}_{date_str}.json");
            let out_path = output_dir.join(&filename);
            let exported_count = projected.len();
            write_json(
                &out_path,
                &talker,
                &talker_display,
                is_group,
                projected,
                &media_map,
                &media_statuses,
                media_summary,
                effective_limit,
                offset,
                total_count.unwrap_or(0),
                total_scanned,
                total_skipped,
                shard_warnings,
            )?;
            eprintln!(
                "Exported {} messages, {} media files → {}",
                exported_count,
                total_media,
                out_path.display()
            );
        }
    }

    // Media quality hints
    for hint in media_quality_hints(&media_stats) {
        eprintln!("{hint}");
    }

    Ok(())
}

// ── TXT writer ──────────────────────────────────────────────────────

#[allow(clippy::too_many_arguments)]
fn write_txt(
    out_path: &std::path::Path,
    _talker: &str,
    talker_display: &str,
    self_wxid: &str,
    is_group: bool,
    messages: &[EnrichedMessage],
    media_map: &[Vec<crate::cmd::export_media::MediaAsset>],
    resolver: &ContactResolver,
    show_emoji: bool,
) -> Result<(), Box<dyn std::error::Error>> {
    let mut f = std::io::BufWriter::new(std::fs::File::create(out_path)?);

    let self_name = {
        let name = resolver.display_name(self_wxid);
        if name == self_wxid {
            self_wxid.to_string()
        } else {
            name.to_string()
        }
    };

    // Header
    if is_group {
        writeln!(f, "\"{}\"的聊天记录如下:", talker_display)?;
    } else {
        writeln!(
            f,
            "\"{}\"和\"{}\"的聊天记录如下:",
            self_name, talker_display
        )?;
    }

    // Meta info
    let export_time = chrono::Local::now().format("%Y-%m-%d %H:%M:%S").to_string();
    let first_time = messages.iter().map(|em| em.message.create_time).min();
    let last_time = messages.iter().map(|em| em.message.create_time).max();

    writeln!(f)?;
    writeln!(f, "导出时间：{export_time}")?;
    writeln!(f, "消息数：{}", messages.len())?;
    if let (Some(first), Some(last)) = (first_time, last_time) {
        let first_date = format_date_short(first);
        let last_date = format_date_short(last);
        writeln!(f, "时间范围：{first_date} ~ {last_date}")?;
    }

    // Messages
    let mut last_date: Option<chrono::NaiveDate> = None;
    let mut image_counter = 0usize;
    let mut voice_counter = 0usize;
    let mut video_counter = 0usize;
    let mut file_counter = 0usize;
    let mut attachments: Vec<(String, String)> = Vec::new(); // (label, filename)

    for (i, em) in messages.iter().enumerate() {
        let msg = &em.message;
        let dt = chrono::DateTime::from_timestamp(msg.create_time, 0)
            .map(|dt| dt.with_timezone(&chrono::Local));

        // Date separator
        if let Some(dt) = &dt {
            let date = dt.date_naive();
            if last_date != Some(date) {
                let (y, m, d) = (date.year(), date.month(), date.day());
                write!(f, "\n\n—————  {y}-{m}-{d}  —————\n")?;
                last_date = Some(date);
            }
        }

        // Sender name
        let sender_name = if em.direction == Direction::Outgoing {
            self_name.clone()
        } else {
            em.sender_display_name.clone()
        };

        // Time
        let time_str = dt
            .map(|dt| dt.format("%H:%M").to_string())
            .unwrap_or_default();

        // Content — use enriched snippet (already includes quote redaction)
        let assets = &media_map[i];
        let content = if assets.is_empty() {
            if !show_emoji && matches!(&msg.content, MessageContent::Emoji(_)) {
                "[动画表情]".to_string()
            } else {
                em.snippet.clone()
            }
        } else {
            let mut parts = Vec::new();
            for asset in assets {
                let label = match asset.kind {
                    MediaKind::Image => {
                        image_counter += 1;
                        format!("图片{image_counter}")
                    }
                    MediaKind::Voice => {
                        voice_counter += 1;
                        format!("语音{voice_counter}")
                    }
                    MediaKind::Video => {
                        video_counter += 1;
                        format!("视频{video_counter}")
                    }
                    MediaKind::File => {
                        file_counter += 1;
                        format!("文件{file_counter}")
                    }
                };
                parts.push(format!("{label}（可在附件中查看）"));
                attachments.push((label, media_asset_path(&asset.filename)));
            }
            parts.join("\n")
        };

        write!(f, "\n{sender_name} {time_str}\n{content}\n")?;
    }

    // Attachment list
    if !attachments.is_empty() {
        write!(f, "\n附件：\n")?;
        for (label, path) in &attachments {
            writeln!(f, "\n[{label}] {path}")?;
        }
    }

    f.flush()?;
    Ok(())
}

// ── JSON writer ─────────────────────────────────────────────────────

#[allow(clippy::too_many_arguments)]
fn write_json(
    out_path: &std::path::Path,
    talker: &str,
    talker_display: &str,
    is_group: bool,
    messages: Vec<EnrichedMessage>,
    media_map: &[Vec<crate::cmd::export_media::MediaAsset>],
    media_statuses: &[Option<MediaStatus>],
    media_summary: MediaSummary,
    effective_limit: usize,
    user_offset: usize,
    total: usize,
    scanned: usize,
    skipped: usize,
    shard_warnings: Vec<wx_db::ShardWarning>,
) -> Result<(), Box<dyn std::error::Error>> {
    let message_count = messages.len();
    let time_range_start = messages.iter().map(|em| em.message.create_time).min();
    let time_range_end = messages.iter().map(|em| em.message.create_time).max();

    let exported_items: Vec<ExportedMessage> = messages
        .into_iter()
        .enumerate()
        .map(|(i, enriched)| {
            let media_files = media_asset_paths(&media_map[i]);
            ExportedMessage {
                enriched,
                media_files,
                media_status: media_statuses[i],
            }
        })
        .collect();

    let returned = exported_items.len();
    let has_more = user_offset + returned < total;

    let envelope = ExportEnvelope {
        export_info: ExportInfo {
            version: "1",
            exported_at: chrono::Local::now().to_rfc3339(),
            generator: format!("wx-cli {}", env!("CARGO_PKG_VERSION")),
        },
        conversation: ConversationMeta {
            talker: talker.to_string(),
            display_name: talker_display.to_string(),
            conv_type: if is_group { "group" } else { "private" },
            message_count,
            time_range_start,
            time_range_end,
        },
        media: media_summary,
        envelope: JsonEnvelope {
            items: exported_items,
            paging: PagingMeta {
                limit: effective_limit,
                offset: user_offset,
                returned,
                has_more,
                total,
            },
            stats: StatsMeta {
                scanned,
                skipped,
                elapsed_ms: None,
                shard_warnings,
            },
        },
    };

    let json = serde_json::to_string_pretty(&envelope)?;
    std::fs::write(out_path, json)?;
    Ok(())
}

// ── Helpers ─────────────────────────────────────────────────────────

fn media_asset_path(filename: &str) -> String {
    format!("media/{filename}")
}

fn media_asset_paths(assets: &[crate::cmd::export_media::MediaAsset]) -> Vec<String> {
    assets
        .iter()
        .map(|asset| media_asset_path(&asset.filename))
        .collect()
}

fn media_quality_hints(stats: &MediaStats) -> Vec<String> {
    let mut hints = Vec::new();

    if stats.fallback_videos > 0 {
        hints.push(format!(
            "hint: {} video(s) resolved via directory scan fallback",
            stats.fallback_videos
        ));
    }
    if stats.fallback_files > 0 {
        hints.push(format!(
            "hint: {} file(s) resolved via directory scan fallback",
            stats.fallback_files
        ));
    }
    if stats.skipped_videos > 0 {
        hints.push(format!(
            "hint: {} video(s) skipped — not found after hardlink + directory scan",
            stats.skipped_videos
        ));
    }
    if stats.thumbnail_images > 0 {
        hints.push(format!(
            "hint: {} image(s) exported as thumbnail only — open them in WeChat to download full resolution",
            stats.thumbnail_images
        ));
    }
    if stats.silk_voices > 0 {
        hints.push(format!(
            "hint: {} voice(s) exported as raw SILK — install ffmpeg for MP3 transcoding",
            stats.silk_voices
        ));
    }
    if stats.skipped_files > 0 {
        hints.push(format!(
            "hint: {} file(s) skipped — not found after hardlink + directory scan",
            stats.skipped_files
        ));
    }
    if stats.wxgf_transcoded > 0 {
        hints.push(format!(
            "hint: {} wxgf image(s) transcoded to standard image format",
            stats.wxgf_transcoded
        ));
    }
    if stats.wxgf_fallback > 0 {
        hints.push(format!(
            "hint: {} wxgf image(s) kept as .wxgf - install ffmpeg for PNG/GIF export",
            stats.wxgf_fallback
        ));
    }

    hints
}

fn format_date_short(ts: i64) -> String {
    chrono::DateTime::from_timestamp(ts, 0)
        .map(|dt| {
            let local = dt.with_timezone(&chrono::Local);
            let d = local.date_naive();
            format!("{}-{}-{}", d.year(), d.month(), d.day())
        })
        .unwrap_or_else(|| ts.to_string())
}

use chrono::Datelike;

#[cfg(test)]
mod tests {
    use super::*;
    use crate::cmd::export_media::MediaAsset;
    use crate::schema::enrich_message;
    use rusqlite::Connection;

    #[test]
    fn json_marks_all_eligible_contents_even_without_references_or_cache() {
        let tmp = tempfile::TempDir::new().unwrap();
        let resolver = test_resolver(tmp.path());
        let contents = [
            MessageContent::Image { md5: None },
            MessageContent::Video {
                md5: Some(String::new()),
            },
            MessageContent::File {
                md5: None,
                title: Some("report.pdf".into()),
                file_ext: None,
                file_size: None,
                raw_xml: String::new(),
            },
            MessageContent::Voice,
            MessageContent::Image {
                md5: Some("abc".into()),
            },
            MessageContent::Text("keep this text".into()),
        ];
        for no_media in [false, true] {
            let messages: Vec<_> = contents
                .iter()
                .map(|content| {
                    let mut message = sample_image_message();
                    message.content = content.clone();
                    enrich_message(message, "wxid_me", &resolver)
                })
                .collect();
            let statuses = crate::cmd::export_task::initial_statuses(&messages, no_media);
            let out_path = tmp.path().join("export.json");
            write_json(
                &out_path,
                "wxid_other",
                "Alice",
                false,
                messages,
                &vec![vec![]; contents.len()],
                &statuses,
                MediaSummary::from_statuses(&statuses, no_media),
                100,
                0,
                contents.len(),
                contents.len(),
                0,
                vec![],
            )
            .unwrap();
            let json: serde_json::Value =
                serde_json::from_slice(&std::fs::read(out_path).unwrap()).unwrap();
            assert_eq!(json["media"]["version"], 1);
            assert_eq!(json["media"]["expected"], 5);
            assert_eq!(json["media"]["available"], 0);
            assert!(json["items"][5].get("media_status").is_none());
            for item in json["items"].as_array().unwrap() {
                assert!(item.get("media_files").is_none());
            }
            if no_media {
                assert_eq!(json["media"]["mode"], "metadata_only");
                assert_eq!(json["media"]["not_attempted"], 5);
                assert_eq!(json["media"]["errors"], 0);
                assert_eq!(json["media"]["missing"], 0);
                for item in &json["items"].as_array().unwrap()[..5] {
                    assert_eq!(item["media_status"]["state"], "metadata_only");
                }
            } else {
                assert_eq!(json["media"]["mode"], "enabled");
                assert_eq!(json["media"]["not_attempted"], 0);
                assert_eq!(json["media"]["missing"], 3);
                assert_eq!(json["media"]["errors"], 2);
                for item in &json["items"].as_array().unwrap()[..3] {
                    assert_eq!(item["media_status"]["state"], "missing");
                    assert_eq!(item["media_status"]["reason"], "missing_reference");
                }
                assert_eq!(json["items"][3]["media_status"]["state"], "error");
                assert_eq!(
                    json["items"][4]["media_status"]["reason"],
                    "media_cache_unavailable"
                );
            }
        }
    }

    #[test]
    fn write_txt_uses_asset_filenames_for_attachment_list() {
        let tmp = tempfile::TempDir::new().unwrap();
        let resolver = test_resolver(tmp.path());
        let out_path = tmp.path().join("export.txt");

        write_txt(
            &out_path,
            "wxid_other",
            "Alice",
            "wxid_me",
            false,
            &[sample_enriched_message(&resolver)],
            &[vec![MediaAsset {
                kind: MediaKind::Image,
                filename: "cdb2f853d5e1cdebbdc66bb8c80e1714.wxgf".into(),
            }]],
            &resolver,
            true,
        )
        .unwrap();

        let txt = std::fs::read_to_string(out_path).unwrap();
        assert!(txt.contains("图片1（可在附件中查看）"));
        assert!(txt.contains("[图片1] media/cdb2f853d5e1cdebbdc66bb8c80e1714.wxgf"));
    }

    fn sample_image_message() -> wx_db::Message {
        wx_db::Message {
            sort_seq: 1,
            server_id: 1,
            local_id: 1,
            source_shard: None,
            msg_type: wx_db::MSG_TYPE_IMAGE,
            sub_type: 0,
            sender: "wxid_other".into(),
            talker: "wxid_other".into(),
            create_time: 1_710_504_000,
            content: wx_db::MessageContent::Image {
                md5: Some("deadbeef".into()),
            },
            status: 0,
        }
    }

    fn sample_enriched_message(resolver: &ContactResolver) -> EnrichedMessage {
        enrich_message(sample_image_message(), "wxid_me", resolver)
    }

    fn test_resolver(base: &std::path::Path) -> ContactResolver {
        let contact_dir = base.join("contact");
        let session_dir = base.join("session");
        let message_dir = base.join("message");
        std::fs::create_dir_all(&contact_dir).unwrap();
        std::fs::create_dir_all(&session_dir).unwrap();
        std::fs::create_dir_all(&message_dir).unwrap();

        let contact_conn = Connection::open(contact_dir.join("contact.db")).unwrap();
        contact_conn
            .execute_batch(
                "CREATE TABLE contact (
                    username TEXT PRIMARY KEY,
                    alias TEXT DEFAULT '',
                    remark TEXT DEFAULT '',
                    nick_name TEXT DEFAULT '',
                    description TEXT DEFAULT NULL,
                    extra_buffer BLOB DEFAULT NULL
                );
                CREATE TABLE contact_label (
                    label_id_ TEXT,
                    label_name_ TEXT,
                    sort_order_ INTEGER
                );",
            )
            .unwrap();
        contact_conn
            .execute(
                "INSERT INTO contact (username, remark, nick_name) VALUES (?1, ?2, ?3)",
                rusqlite::params!["wxid_me", "Me", ""],
            )
            .unwrap();
        contact_conn
            .execute(
                "INSERT INTO contact (username, remark, nick_name) VALUES (?1, ?2, ?3)",
                rusqlite::params!["wxid_other", "Alice", ""],
            )
            .unwrap();

        let session_conn = Connection::open(session_dir.join("session.db")).unwrap();
        session_conn
            .execute_batch(
                "CREATE TABLE SessionTable (
                    username TEXT,
                    sort_timestamp INTEGER,
                    summary TEXT,
                    last_msg_type INTEGER,
                    last_msg_sender TEXT,
                    last_sender_display_name TEXT
                );",
            )
            .unwrap();

        let db = wx_db::WechatDb::open(base).unwrap();
        ContactResolver::build(&db).unwrap()
    }
}
