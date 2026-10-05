//! Source-side S3 fixture tests: real SQLCipher synthetic sources →
//! archive-snapshot → archive-inspect export, asserting the contract-v2
//! record shape end to end. Synthetic data only — no real WeChat sources,
//! keys, or NAS access anywhere in this file.
//!
//! Covers the independent-review requirements:
//! * compressed (zstd/WCDB_CT=4) and BLOB content stay lossless
//!   (`raw` columns + raw_b64 preservation, decode failure would hard-fail);
//! * cross-database same-table same-rowid rows keep distinct identities;
//! * tampered and missing manifest shards are rejected, never exported as
//!   complete;
//! * media of every kind: image .dat is DECODED Mac-side (encrypted bytes
//!   are never the delivered media), voice from snapshot media dbs, video
//!   and app-file from the verified attach layouts via hardlink.db;
//!   permission errors on media candidates, UNDECODABLE .dat bytes, and
//!   unreadable intermediate attach directories (absence cannot be proven
//!   through them) are hard failures; without an attach root the export
//!   honestly reports media completeness = false;
//! * the dat decrypt key arrives via a private 0600 key file (never argv)
//!   and fails closed on malformed input without echoing the value;
//! * media inputs and content decoding run under explicit, configurable
//!   work-set caps enforced BEFORE the memory is used (`media_input_over_limit`
//!   pre-read stat / mid-read growth check, SQL voice length probe,
//!   `content_over_limit` raw-cell cap, `decoded_over_limit` streaming zstd
//!   bound) — over-limit exports never publish an export.json;
//! * a thumbnail-only image dat is staged labeled variant=thumbnail with
//!   original_present=false and media completeness false (a thumbnail never
//!   impersonates the original);
//! * record_sha256 canonicalization is verified CROSS-LANGUAGE by
//!   recomputing every fingerprint with Python json.dumps over the real
//!   exported records (integers, integral/decimal REALs, BLOB b64, Chinese
//!   text).
use std::os::raw::c_void;
use std::os::unix::fs::PermissionsExt;
use std::path::{Path, PathBuf};
use std::process::{Command, Output};

use rusqlite::{params, Connection};
use serde_json::Value;
use tempfile::TempDir;

const BIN: &str = env!("CARGO_BIN_EXE_wx-cli");
const RAW_KEY: [u8; 32] = [0x5A; 32];
const TALKER: &str = "talker-a@synthetic";

fn invoke(args: &[&str]) -> Output {
    Command::new(BIN).args(args).output().unwrap()
}

fn open_cipher(path: &Path) -> Connection {
    let db = Connection::open(path).unwrap();
    unsafe {
        assert_eq!(
            rusqlite::ffi::sqlite3_key(db.handle(), RAW_KEY.as_ptr() as *const c_void, 32),
            0
        );
    }
    db
}

fn md5_hex(s: &str) -> String {
    format!("{:x}", md5::compute(s.as_bytes()))
}

fn sha256_hex(data: &[u8]) -> String {
    use sha2::{Digest, Sha256};
    let mut hasher = Sha256::new();
    hasher.update(data);
    hex::encode(hasher.finalize())
}

struct Fixture {
    tmp: TempDir,
    table: String,
    img_md5: String,
    video_md5: String,
    file_md5: String,
    png_plain: Vec<u8>,
    video_bytes: Vec<u8>,
    file_bytes: Vec<u8>,
    voice_bytes: Vec<u8>,
    zstd_blob: Vec<u8>,
    invalid_utf8: Vec<u8>,
}

impl Fixture {
    fn root(&self) -> &Path {
        self.tmp.path()
    }
    fn snapshots(&self) -> PathBuf {
        self.root().join("snapshot")
    }
    fn key_file(&self) -> PathBuf {
        self.root().join("key.json")
    }
    fn export_dir(&self, name: &str) -> PathBuf {
        self.root().join(name)
    }
    fn inspect(&self, out: &Path, attach_root: Option<&Path>) -> Output {
        self.inspect_with_dat_key(out, attach_root, None)
    }
    fn inspect_with_dat_key(
        &self,
        out: &Path,
        attach_root: Option<&Path>,
        dat_key_file: Option<&Path>,
    ) -> Output {
        self.inspect_full(out, attach_root, dat_key_file, &[])
    }
    /// Full-control variant: extra CLI flags (e.g. --max-media-bytes /
    /// --max-decoded-bytes / --max-content-bytes) for the bounded-work-set
    /// tests.
    fn inspect_full(
        &self,
        out: &Path,
        attach_root: Option<&Path>,
        dat_key_file: Option<&Path>,
        extra: &[&str],
    ) -> Output {
        let mut args: Vec<String> = vec![
            "archive-inspect".into(),
            "--snapshots".into(),
            self.snapshots().to_string_lossy().to_string(),
            "--key-file".into(),
            self.key_file().to_string_lossy().to_string(),
            "--action".into(),
            "export".into(),
            "--talker".into(),
            TALKER.into(),
            "--account".into(),
            "synthetic-account".into(),
            "--archive-id".into(),
            "synthetic-archive".into(),
            "--out".into(),
            out.to_string_lossy().to_string(),
            "--page-size".into(),
            "2".into(),
        ];
        if let Some(root) = attach_root {
            args.push("--attach-root".into());
            args.push(root.to_string_lossy().to_string());
        }
        if let Some(key) = dat_key_file {
            args.push("--dat-key-file".into());
            args.push(key.to_string_lossy().to_string());
        }
        for flag in extra {
            args.push((*flag).into());
        }
        let refs: Vec<&str> = args.iter().map(String::as_str).collect();
        invoke(&refs)
    }
    fn records(&self, out: &Path) -> Vec<Value> {
        let text = std::fs::read_to_string(out.join("records.jsonl")).unwrap();
        text.lines()
            .map(|line| serde_json::from_str(line).unwrap())
            .collect()
    }
    fn manifest(&self, out: &Path) -> Value {
        serde_json::from_slice(&std::fs::read(out.join("export.json")).unwrap()).unwrap()
    }
}

/// Build a full synthetic source tree (two message shards with the SAME Msg
/// table, a media db with one voice blob, a hardlink db, an attach root with
/// an XOR-encrypted image, a video and a file), then snapshot it.
fn fixture() -> Fixture {
    fixture_with_image(false, None)
}

/// Fixture with the encrypted image bytes replaced (undecodable .dat test).
fn fixture_with_image_dat(image_dat_override: Option<Vec<u8>>) -> Fixture {
    fixture_with_image(false, image_dat_override)
}

/// `thumb_only` names the image dat `<md5>_t.dat` (thumbnail tier) instead
/// of the exact `<md5>.dat`, so the resolver has ONLY a thumbnail to offer —
/// the export must stage it labeled as a thumbnail, never as the original.
fn fixture_with_image(thumb_only: bool, image_dat_override: Option<Vec<u8>>) -> Fixture {
    let tmp = TempDir::new().unwrap();
    let root = tmp.path();
    std::fs::create_dir_all(root.join("source/message")).unwrap();
    std::fs::create_dir_all(root.join("source/media")).unwrap();
    std::fs::create_dir_all(root.join("source/hardlink")).unwrap();

    let key_file = root.join("key.json");
    std::fs::write(&key_file, format!("{{\"raw_key\":\"{}\"}}", "5a".repeat(32))).unwrap();
    std::fs::set_permissions(&key_file, std::fs::Permissions::from_mode(0o600)).unwrap();

    let table = format!("Msg_{}", md5_hex(TALKER));
    let img_md5 = format!("{:032x}", 0x1111u64);
    let video_md5 = format!("{:032x}", 0x2222u64);
    let file_md5 = format!("{:032x}", 0x3333u64);

    let png_plain: Vec<u8> = {
        let mut v = vec![0x89, b'P', b'N', b'G', 0x0D, 0x0A, 0x1A, 0x0A];
        v.extend_from_slice(b"synthetic-png-payload-for-archive-tests");
        v
    };
    let video_bytes = b"synthetic-video-bytes-0123456789".to_vec();
    let file_bytes = b"synthetic-file-bytes-abcdef".to_vec();
    let voice_bytes = b"SYNTHETIC-SILK-VOICE-BYTES".to_vec();
    let zstd_blob = zstd::encode_all(b"compressed body v2".as_slice(), 3).unwrap();
    let invalid_utf8: Vec<u8> = vec![0xff, 0xfe, b'b', b'a', b'd'];

    let packed_image = wx_db::encode_packed_info_for_test(Some(&img_md5), None);
    let packed_video = wx_db::encode_packed_info_for_test(None, Some(&video_md5));
    let marker_blob = {
        let mut v = b"\x12\x22\x0a\x20".to_vec();
        v.extend_from_slice(file_md5.as_bytes());
        v
    };
    let compress_blob = zstd::encode_all(b"<appmsg>quote</appmsg>".as_slice(), 3).unwrap();

    // Shard 0: the full-column table with every content shape.
    {
        let db = open_cipher(&root.join("source/message/message_0.db"));
        db.execute_batch(&format!(
            "CREATE TABLE Name2Id(user_name TEXT);
             INSERT INTO Name2Id(rowid, user_name) VALUES (1, '{TALKER}'), (2, 'sender-x');
             CREATE TABLE {table}(
                 sort_seq INTEGER, server_id INTEGER, local_type INTEGER,
                 create_time INTEGER, status INTEGER, message_content BLOB,
                 packed_info_data BLOB, WCDB_CT_message_content INTEGER,
                 compress_content BLOB, real_sender_id INTEGER, extra_col TEXT,
                 real_col REAL
             );"
        ))
        .unwrap();
        let app_local_type: i64 = 49 + (57i64 << 32);
        let insert = format!(
            "INSERT INTO {table}(sort_seq, server_id, local_type, create_time, status, \
             message_content, packed_info_data, WCDB_CT_message_content, compress_content, \
             real_sender_id, extra_col, real_col) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)"
        );
        // 1: plain TEXT round-trip, large server_id (exact-string contract).
        db.execute(
            &insert,
            params![1, 9007199254740993i64, 1, 100, 0, "hello v2", None::<Vec<u8>>, None::<i64>, None::<Vec<u8>>, None::<i64>, "x1", 2.25f64],
        )
        .unwrap();
        // 2: zstd-compressed BLOB flagged WCDB_CT=4.
        db.execute(
            &insert,
            params![2, 42, 1, 101, 0, zstd_blob.clone(), None::<Vec<u8>>, 4, None::<Vec<u8>>, 2, "x2", 0.1f64],
        )
        .unwrap();
        // 3: invalid-UTF-8 BLOB with no server_id (local-only identity).
        db.execute(
            &insert,
            params![3, 0, 1, 102, 0, invalid_utf8.clone(), None::<Vec<u8>>, None::<i64>, None::<Vec<u8>>, 2, None::<String>, None::<f64>],
        )
        .unwrap();
        // 4: image (packed image_md5).
        db.execute(
            &insert,
            params![4, 43, 3, 103, 0, "img", packed_image.clone(), None::<i64>, None::<Vec<u8>>, None::<i64>, None::<String>, 0.001f64],
        )
        .unwrap();
        // 5: voice by server_id.
        db.execute(
            &insert,
            params![5, 777001, 34, 104, 0, "voice", None::<Vec<u8>>, None::<i64>, None::<Vec<u8>>, None::<i64>, None::<String>, 5.0f64],
        )
        .unwrap();
        // 6: video (packed video_md5).
        db.execute(
            &insert,
            params![6, 44, 43, 105, 0, "video", packed_video.clone(), None::<i64>, None::<Vec<u8>>, None::<i64>, None::<String>, -0.5f64],
        )
        .unwrap();
        // 7: app/file with high-bits sub_type 57 + compress_content + marker
        // md5 + non-ASCII text (canonicalization must not escape it).
        db.execute(
            &insert,
            params![7, 45, app_local_type, 106, 0, "文件 file xml", marker_blob.clone(), None::<i64>, compress_blob.clone(), None::<i64>, None::<String>, 123456789012345.6f64],
        )
        .unwrap();
        db.close().unwrap();
    }

    // Shard 1: SAME table name, SAME rowid 1 — must keep a distinct identity.
    {
        let db = open_cipher(&root.join("source/message/message_1.db"));
        db.execute_batch(&format!(
            "CREATE TABLE Name2Id(user_name TEXT);
             INSERT INTO Name2Id VALUES ('{TALKER}');
             CREATE TABLE {table}(
                 sort_seq INTEGER, server_id INTEGER, local_type INTEGER,
                 create_time INTEGER, status INTEGER, message_content TEXT,
                 packed_info_data BLOB
             );
             INSERT INTO {table} VALUES (1, 9007199254740994, 1, 200, 0, 'shard one body', NULL);"
        ))
        .unwrap();
        db.close().unwrap();
    }

    // Media db with the voice blob (svr_id is matched as TEXT).
    {
        let db = open_cipher(&root.join("source/media/media_0.db"));
        db.execute_batch(
            "CREATE TABLE VoiceInfo(svr_id TEXT, chat_name_id INTEGER, voice_data BLOB);
             INSERT INTO VoiceInfo VALUES ('777001', 1, x'');",
        )
        .unwrap();
        db.execute(
            "UPDATE VoiceInfo SET voice_data = ?1 WHERE svr_id = '777001'",
            params![voice_bytes.clone()],
        )
        .unwrap();
        db.close().unwrap();
    }

    // Hardlink db: video and file entries pointing into the attach tree.
    {
        let db = open_cipher(&root.join("source/hardlink/hardlink.db"));
        db.execute_batch(
            "CREATE TABLE dir2id(username TEXT);
             INSERT INTO dir2id(rowid, username) VALUES (1,'vdir1'),(2,'vdir2'),(3,'fdir1'),(4,'fdir2');
             CREATE TABLE video_hardlink_info_v3(md5 TEXT, file_name TEXT, file_size INTEGER, modify_time INTEGER, dir1 INTEGER, dir2 INTEGER);
             CREATE TABLE file_hardlink_info_v3(md5 TEXT, file_name TEXT, file_size INTEGER, modify_time INTEGER, dir1 INTEGER, dir2 INTEGER);",
        )
        .unwrap();
        db.execute(
            "INSERT INTO video_hardlink_info_v3 VALUES (?,?,?,?,?,?)",
            params![video_md5, "vfile.bin", video_bytes.len() as i64, 0, 1, 2],
        )
        .unwrap();
        db.execute(
            "INSERT INTO file_hardlink_info_v3 VALUES (?,?,?,?,?,?)",
            params![file_md5, "ffile.bin", file_bytes.len() as i64, 0, 3, 4],
        )
        .unwrap();
        db.close().unwrap();
    }

    // Attach tree (OUTSIDE the snapshot source): XOR-encrypted image dat at
    // the verified attach/<md5(talker)>/<month>/Img/<md5>.dat layout, plus
    // the video/file bytes the hardlink entries point at.
    let attach = root.join("attach");
    {
        let img_dir = attach.join(md5_hex(TALKER)).join("2026-03").join("Img");
        std::fs::create_dir_all(&img_dir).unwrap();
        let encrypted: Vec<u8> = image_dat_override
            .unwrap_or_else(|| png_plain.iter().map(|b| b ^ 0xA5).collect());
        let dat_name = if thumb_only {
            format!("{img_md5}_t.dat")
        } else {
            format!("{img_md5}.dat")
        };
        std::fs::write(img_dir.join(dat_name), encrypted).unwrap();
        std::fs::create_dir_all(attach.join("vdir1").join("vdir2").join("Video")).unwrap();
        std::fs::write(
            attach.join("vdir1").join("vdir2").join("Video").join("vfile.bin"),
            &video_bytes,
        )
        .unwrap();
        std::fs::create_dir_all(attach.join("fdir1").join("fdir2")).unwrap();
        std::fs::write(
            attach.join("fdir1").join("fdir2").join("ffile.bin"),
            &file_bytes,
        )
        .unwrap();
    }

    let result = invoke(&[
        "archive-snapshot",
        "--source",
        root.join("source").to_str().unwrap(),
        "--out",
        root.join("snapshot").to_str().unwrap(),
        "--key-file",
        key_file.to_str().unwrap(),
    ]);
    assert!(
        result.status.success(),
        "snapshot fixture failed: {}",
        String::from_utf8_lossy(&result.stdout)
    );

    Fixture {
        tmp,
        table,
        img_md5,
        video_md5,
        file_md5,
        png_plain,
        video_bytes,
        file_bytes,
        voice_bytes,
        zstd_blob,
        invalid_utf8,
    }
}

/// Minimal fixture: one message shard with a voice row and NO media dbs at
/// all — the voice reference must become a recorded gap, not a failure.
fn voiceless_fixture() -> (TempDir, String) {
    let tmp = TempDir::new().unwrap();
    let root = tmp.path();
    std::fs::create_dir_all(root.join("source/message")).unwrap();
    let key_file = root.join("key.json");
    std::fs::write(&key_file, format!("{{\"raw_key\":\"{}\"}}", "5a".repeat(32))).unwrap();
    std::fs::set_permissions(&key_file, std::fs::Permissions::from_mode(0o600)).unwrap();
    let table = format!("Msg_{}", md5_hex(TALKER));
    let db = open_cipher(&root.join("source/message/message_0.db"));
    db.execute_batch(&format!(
        "CREATE TABLE Name2Id(user_name TEXT);
         INSERT INTO Name2Id VALUES ('{TALKER}');
         CREATE TABLE {table}(
             sort_seq INTEGER, server_id INTEGER, local_type INTEGER,
             create_time INTEGER, status INTEGER, message_content TEXT,
             packed_info_data BLOB
         );
         INSERT INTO {table} VALUES (1, 888002, 34, 300, 0, 'voice', NULL);"
    ))
    .unwrap();
    db.close().unwrap();
    let result = invoke(&[
        "archive-snapshot",
        "--source",
        root.join("source").to_str().unwrap(),
        "--out",
        root.join("snapshot").to_str().unwrap(),
        "--key-file",
        key_file.to_str().unwrap(),
    ]);
    assert!(result.status.success(), "voiceless snapshot failed");
    (tmp, table)
}

fn inspect_generic(
    root: &Path,
    out_name: &str,
) -> Output {
    inspect_extra(root, out_name, &[])
}

/// inspect on a bare fixture root, with extra CLI flags appended.
fn inspect_extra(root: &Path, out_name: &str, extra: &[&str]) -> Output {
    let mut args: Vec<String> = vec![
        "archive-inspect".into(),
        "--snapshots".into(),
        root.join("snapshot").to_string_lossy().to_string(),
        "--key-file".into(),
        root.join("key.json").to_string_lossy().to_string(),
        "--action".into(),
        "export".into(),
        "--talker".into(),
        TALKER.into(),
        "--account".into(),
        "synthetic-account".into(),
        "--archive-id".into(),
        "synthetic-archive".into(),
        "--out".into(),
        root.join(out_name).to_string_lossy().to_string(),
    ];
    for flag in extra {
        args.push((*flag).into());
    }
    let refs: Vec<&str> = args.iter().map(String::as_str).collect();
    invoke(&refs)
}

/// Minimal single-row fixture whose message_content is a ~1 MB-expanding
/// zstd blob flagged WCDB_CT=4 — the compressed-expansion work-set probe.
fn bomb_fixture() -> (TempDir, String) {
    let tmp = TempDir::new().unwrap();
    let root = tmp.path();
    std::fs::create_dir_all(root.join("source/message")).unwrap();
    let key_file = root.join("key.json");
    std::fs::write(&key_file, format!("{{\"raw_key\":\"{}\"}}", "5a".repeat(32))).unwrap();
    std::fs::set_permissions(&key_file, std::fs::Permissions::from_mode(0o600)).unwrap();
    let table = format!("Msg_{}", md5_hex(TALKER));
    let bomb = zstd::encode_all(vec![b'a'; 1_000_000].as_slice(), 3).unwrap();
    let db = open_cipher(&root.join("source/message/message_0.db"));
    db.execute_batch(&format!(
        "CREATE TABLE Name2Id(user_name);
         INSERT INTO Name2Id VALUES ('{TALKER}');
         CREATE TABLE {table}(
             sort_seq INTEGER, server_id INTEGER, local_type INTEGER,
             create_time INTEGER, status INTEGER, message_content BLOB,
             packed_info_data BLOB, WCDB_CT_message_content INTEGER
         );"
    ))
    .unwrap();
    db.execute(
        &format!("INSERT INTO {table} VALUES (1, 1, 1, 400, 0, ?1, NULL, 4)"),
        params![bomb],
    )
    .unwrap();
    db.close().unwrap();
    let result = invoke(&[
        "archive-snapshot",
        "--source",
        root.join("source").to_str().unwrap(),
        "--out",
        root.join("snapshot").to_str().unwrap(),
        "--key-file",
        key_file.to_str().unwrap(),
    ]);
    assert!(
        result.status.success(),
        "bomb snapshot failed: {}",
        String::from_utf8_lossy(&result.stdout)
    );
    (tmp, table)
}

#[test]
fn export_v2_lossless_records_and_decoded_media() {
    let fx = fixture();
    let out = fx.export_dir("export-full");
    let attach = fx.root().join("attach");
    let result = fx.inspect(&out, Some(&attach));
    assert!(
        result.status.success(),
        "export failed: {}",
        String::from_utf8_lossy(&result.stdout)
    );

    let rows = fx.records(&out);
    assert_eq!(rows.len(), 8, "all rows across both shards exported");

    // Contract v2 ordering: (shard=database, table, local_rowid) ascending —
    // message_0.db rows first, then message_1.db.
    assert_eq!(rows[0]["identity"]["shard"], "message_0.db");
    assert_eq!(rows[7]["identity"]["shard"], "message_1.db");
    for pair in rows.windows(2) {
        let a = &pair[0]["identity"];
        let b = &pair[1]["identity"];
        let ok = a["shard"].as_str().unwrap() < b["shard"].as_str().unwrap()
            || (a["shard"] == b["shard"]
                && a["local_rowid"].as_i64().unwrap() < b["local_rowid"].as_i64().unwrap());
        assert!(ok, "records must ascend by (shard, local_rowid)");
    }

    // Cross-shard same-table same-rowid: distinct shard identities.
    assert_eq!(rows[7]["identity"]["table"], fx.table);
    assert_ne!(rows[0]["identity"]["shard"], rows[7]["identity"]["shard"]);
    assert_eq!(rows[7]["message_content"], "shard one body");

    // Row 1: plain text, large server_id as exact string.
    let r1 = &rows[0];
    assert_eq!(r1["message_content"], "hello v2");
    assert_eq!(r1["message_content_decode_status"], "ok");
    assert!(r1["message_content_raw_b64"].is_null());
    assert_eq!(r1["message_content_raw_type"], "text");
    assert_eq!(
        r1["identity"]["server_id"],
        serde_json::json!("9007199254740993")
    );
    assert_eq!(r1["raw"]["server_id"], serde_json::json!(9007199254740993i64));
    assert_eq!(r1["raw"]["message_content"], "hello v2");
    assert_eq!(r1["raw"]["extra_col"], "x1");
    assert_eq!(r1["raw"]["real_col"], serde_json::json!(2.25));

    // Row 2: zstd BLOB decodes AND preserves its exact compressed bytes.
    let r2 = &rows[1];
    assert_eq!(r2["message_content"], "compressed body v2");
    assert_eq!(r2["message_content_decode_status"], "ok");
    assert_eq!(r2["message_content_raw_type"], "blob");
    assert_eq!(r2["wcdb_ct"], 4);
    assert_eq!(r2["raw"]["WCDB_CT_message_content"], 4);
    assert_eq!(
        r2["message_content_raw_b64"]
            .as_str()
            .and_then(base64_decode),
        Some(fx.zstd_blob.clone())
    );
    assert_eq!(
        r2["raw"]["message_content"]["b64"]
            .as_str()
            .and_then(base64_decode),
        Some(fx.zstd_blob.clone())
    );

    // Row 3: invalid UTF-8 — no fabricated text, raw bytes preserved,
    // local-only identity (server_id null) still carries the table name.
    let r3 = &rows[2];
    assert!(r3["message_content"].is_null());
    assert_eq!(r3["message_content_decode_status"], "utf8_invalid");
    assert_eq!(r3["message_content_raw_type"], "blob");
    assert_eq!(
        r3["message_content_raw_b64"]
            .as_str()
            .and_then(base64_decode),
        Some(fx.invalid_utf8.clone())
    );
    assert!(r3["identity"]["server_id"].is_null());
    assert_eq!(r3["identity"]["table"], fx.table);
    assert_eq!(r3["real_sender_name"], "sender-x");
    assert_eq!(r3["raw"]["real_sender_id"], 2);

    // Row 4: image — the staged media is the DECODED png, never the
    // encrypted .dat bytes.
    let r4 = &rows[3];
    let img_ref = &r4["media_refs"][0];
    assert_eq!(img_ref["kind"], "image");
    assert_eq!(img_ref["present"], true);
    assert_eq!(img_ref["decoded"], true);
    assert_eq!(img_ref["ext"], "png");
    // Variant layering: the exact `<md5>.dat` is the original tier, with its
    // provenance recorded alongside.
    assert_eq!(img_ref["variant"], "original");
    assert_eq!(img_ref["original_present"], true);
    assert_eq!(img_ref["variant_evidence"]["layout"], "attach");
    assert_eq!(
        img_ref["variant_evidence"]["file_name"],
        format!("{}.dat", fx.img_md5)
    );
    let staged_img =
        std::fs::read(out.join("media").join(format!("md5_{}", fx.img_md5))).unwrap();
    assert_eq!(staged_img, fx.png_plain, "staged image must be decoded plaintext");
    assert_eq!(img_ref["sha256"], sha256_hex(&fx.png_plain));

    // Row 5: voice staged verbatim from the snapshot media db.
    let r5 = &rows[4];
    let voice_ref = &r5["media_refs"][0];
    assert_eq!(voice_ref["kind"], "voice");
    assert_eq!(voice_ref["present"], true);
    let staged_voice = std::fs::read(out.join("media").join("voice_777001")).unwrap();
    assert_eq!(staged_voice, fx.voice_bytes);

    // Row 6: video staged verbatim from the verified candidate layout.
    let r6 = &rows[5];
    let video_ref = &r6["media_refs"][0];
    assert_eq!(video_ref["present"], true);
    let staged_video =
        std::fs::read(out.join("media").join(format!("md5_{}", fx.video_md5))).unwrap();
    assert_eq!(staged_video, fx.video_bytes);

    // Row 7: app/file via the packed marker + compress_content preserved.
    let r7 = &rows[6];
    assert_eq!(r7["sub_type"], 57);
    let file_ref = &r7["media_refs"][0];
    assert_eq!(file_ref["kind"], "file");
    assert_eq!(file_ref["present"], true);
    let staged_file =
        std::fs::read(out.join("media").join(format!("md5_{}", fx.file_md5))).unwrap();
    assert_eq!(staged_file, fx.file_bytes);
    assert!(
        r7["compress_content_b64"]
            .as_str()
            .and_then(base64_decode)
            .is_some()
    );
    assert_eq!(
        r7["packed_info_sha256"],
        sha256_hex(b"\x12\x22\x0a\x20".iter().chain(fx.file_md5.as_bytes().iter()).cloned().collect::<Vec<u8>>().as_slice())
    );

    // Manifest: version 2, complete enumeration, honest media summary.
    let manifest = fx.manifest(&out);
    assert_eq!(manifest["contract"], "wx-archive.external-archive-records");
    assert_eq!(manifest["version"], 2);
    assert_eq!(manifest["enumeration"]["complete"], true);
    assert_eq!(manifest["records"]["count"], 8);
    assert_eq!(manifest["media"]["complete"], true);
    assert_eq!(manifest["media"]["staged_count"], 4);
    assert_eq!(manifest["media"]["unavailable_count"], 0);

    // record_sha256 is reproducible: canonical JSON of the record minus the
    // field itself (same canonicalization the Python side applies).
    for row in &rows {
        let mut copy = row.clone();
        let expected = copy["record_sha256"].clone();
        copy.as_object_mut().unwrap().remove("record_sha256");
        let canonical = serde_json::to_string(&copy).unwrap();
        assert_eq!(
            expected,
            serde_json::json!(sha256_hex(canonical.as_bytes())),
            "record_sha256 mismatch for row {:?}",
            row["identity"]
        );
    }
}

#[test]
fn no_attach_root_reports_incomplete_media_but_complete_records() {
    let fx = fixture();
    let out = fx.export_dir("export-noattach");
    let result = fx.inspect(&out, None);
    assert!(
        result.status.success(),
        "records export must succeed without an attach root: {}",
        String::from_utf8_lossy(&result.stdout)
    );
    let rows = fx.records(&out);
    assert_eq!(rows.len(), 8);
    // Voice comes from the snapshot itself — still staged.
    let voice_ref = &rows[4]["media_refs"][0];
    assert_eq!(voice_ref["present"], true);
    // Attach-backed kinds are honest gaps, never fake refs.
    for index in [3usize, 5, 6] {
        let r = &rows[index]["media_refs"][0];
        assert_eq!(r["present"], false, "row {index} must not claim presence");
        assert!(r["reason"].is_string());
    }
    let manifest = fx.manifest(&out);
    assert_eq!(manifest["media"]["attach_root_provided"], false);
    assert_eq!(manifest["media"]["complete"], false);
    assert_eq!(manifest["records"]["count"], 8);
    assert_eq!(manifest["enumeration"]["complete"], true);
}

#[test]
fn tampered_manifest_shard_is_rejected() {
    let fx = fixture();
    let shard = fx.snapshots().join("message").join("message_0.db");
    // Published files are read-only; make it writable, flip one payload byte
    // (length unchanged), restore permissions.
    std::fs::set_permissions(&shard, std::fs::Permissions::from_mode(0o600)).unwrap();
    let mut bytes = std::fs::read(&shard).unwrap();
    let mid = bytes.len() / 2;
    bytes[mid] ^= 0xFF;
    std::fs::write(&shard, &bytes).unwrap();
    std::fs::set_permissions(&shard, std::fs::Permissions::from_mode(0o400)).unwrap();

    let out = fx.export_dir("export-tampered");
    let attach = fx.root().join("attach");
    let result = fx.inspect(&out, Some(&attach));
    assert!(!result.status.success(), "tampered shard exported as complete");
    let stdout = String::from_utf8_lossy(&result.stdout);
    assert!(
        stdout.contains("sha256 mismatch"),
        "failure must name the sha256 mismatch: {stdout}"
    );
    assert!(
        !out.join("export.json").exists(),
        "no manifest may be published for a failed export"
    );
}

#[test]
fn media_permission_error_is_a_hard_failure() {
    let fx = fixture();
    let video_file = fx
        .root()
        .join("attach")
        .join("vdir1")
        .join("vdir2")
        .join("Video")
        .join("vfile.bin");
    std::fs::set_permissions(&video_file, std::fs::Permissions::from_mode(0o000)).unwrap();

    let out = fx.export_dir("export-denied");
    let attach = fx.root().join("attach");
    let result = fx.inspect(&out, Some(&attach));
    assert!(
        !result.status.success(),
        "permission error must abort the export, not degrade to present=false"
    );
    let stdout = String::from_utf8_lossy(&result.stdout);
    assert!(
        stdout.contains("Permission denied") || stdout.contains("cannot access"),
        "failure must name the permission problem: {stdout}"
    );
    assert!(!out.join("export.json").exists());
}

#[test]
fn missing_voice_blob_is_a_recorded_gap_not_a_failure() {
    let (tmp, _table) = voiceless_fixture();
    let out = tmp.path().join("export-voiceless");
    let result = inspect_generic(tmp.path(), "export-voiceless");
    assert!(
        result.status.success(),
        "export must succeed: {}",
        String::from_utf8_lossy(&result.stdout)
    );
    let text = std::fs::read_to_string(out.join("records.jsonl")).unwrap();
    let row: Value = serde_json::from_str(text.trim()).unwrap();
    let voice_ref = &row["media_refs"][0];
    assert_eq!(voice_ref["kind"], "voice");
    assert_eq!(voice_ref["present"], false);
    assert!(voice_ref["reason"].as_str().unwrap().contains("no media databases"));
    let manifest: Value =
        serde_json::from_slice(&std::fs::read(out.join("export.json")).unwrap()).unwrap();
    assert_eq!(manifest["media"]["complete"], false);
    assert_eq!(manifest["records"]["count"], 1);
}

#[test]
fn undecodable_image_dat_is_a_hard_failure() {
    // A .dat that exists but is opaque garbage (no V1/V2 signature, no
    // image magic reachable by any XOR key) must ABORT the export — never
    // degrade to present=false, never stage the raw bytes.
    let fx = fixture_with_image_dat(Some(b"not-an-image-just-opaque-bytes-0123456789".to_vec()));
    let out = fx.export_dir("export-undecodable");
    let attach = fx.root().join("attach");
    let result = fx.inspect(&out, Some(&attach));
    assert!(
        !result.status.success(),
        "undecodable .dat must abort the export, not become a gap"
    );
    let stdout = String::from_utf8_lossy(&result.stdout);
    assert!(
        stdout.contains("decode failed"),
        "failure must name the decode failure: {stdout}"
    );
    assert!(!out.join("export.json").exists());
}

#[test]
fn unreadable_attach_intermediate_dir_is_a_hard_failure() {
    let fx = fixture();
    let month_dir = fx
        .root()
        .join("attach")
        .join(md5_hex(TALKER))
        .join("2026-03");
    std::fs::set_permissions(&month_dir, std::fs::Permissions::from_mode(0o000)).unwrap();
    let out = fx.export_dir("export-unreadable-dir");
    let attach = fx.root().join("attach");
    let result = fx.inspect(&out, Some(&attach));
    // Restore so TempDir cleanup works regardless of the assertion outcome.
    let _ = std::fs::set_permissions(&month_dir, std::fs::Permissions::from_mode(0o755));
    assert!(
        !result.status.success(),
        "unreadable intermediate dir must abort, not report the image missing"
    );
    let stdout = String::from_utf8_lossy(&result.stdout);
    assert!(
        stdout.contains("cannot enumerate") && stdout.contains("Permission denied"),
        "failure must name the unreadable directory: {stdout}"
    );
    assert!(!out.join("export.json").exists());
}

#[test]
fn dat_key_file_input_boundary_is_enforced() {
    let fx = fixture();
    let attach = fx.root().join("attach");
    let secret = "00112233445566778899aabbccddeeff";
    let kf = fx.root().join("dat-key.json");

    // Valid synthetic key: loads and the export succeeds.
    std::fs::write(&kf, format!("{{\"v2_aes_key\":\"{secret}\"}}")).unwrap();
    std::fs::set_permissions(&kf, std::fs::Permissions::from_mode(0o600)).unwrap();
    let ok_out = fx.export_dir("export-datkey-ok");
    let result = fx.inspect_with_dat_key(&ok_out, Some(&attach), Some(&kf));
    assert!(
        result.status.success(),
        "{}",
        String::from_utf8_lossy(&result.stdout)
    );

    // Wrong length fails closed.
    std::fs::write(&kf, "{\"v2_aes_key\":\"0123\"}").unwrap();
    std::fs::set_permissions(&kf, std::fs::Permissions::from_mode(0o600)).unwrap();
    let len_out = fx.export_dir("export-datkey-len");
    let result = fx.inspect_with_dat_key(&len_out, Some(&attach), Some(&kf));
    assert!(!result.status.success());
    let stdout = String::from_utf8_lossy(&result.stdout);
    assert!(stdout.contains("exactly 16 bytes"), "{stdout}");
    assert!(!len_out.join("export.json").exists());

    // Loose permissions fail closed WITHOUT echoing the key value.
    std::fs::write(&kf, format!("{{\"v2_aes_key\":\"{secret}\"}}")).unwrap();
    std::fs::set_permissions(&kf, std::fs::Permissions::from_mode(0o644)).unwrap();
    let perm_out = fx.export_dir("export-datkey-perm");
    let result = fx.inspect_with_dat_key(&perm_out, Some(&attach), Some(&kf));
    assert!(!result.status.success());
    let stdout = String::from_utf8_lossy(&result.stdout);
    assert!(stdout.contains("0600"), "{stdout}");
    assert!(
        !stdout.contains(secret),
        "key material must never appear in CLI output: {stdout}"
    );
    assert!(!perm_out.join("export.json").exists());

    // Symlinked key files are rejected.
    let real = fx.root().join("dat-key-real.json");
    std::fs::write(&real, format!("{{\"v2_aes_key\":\"{secret}\"}}")).unwrap();
    std::fs::set_permissions(&real, std::fs::Permissions::from_mode(0o600)).unwrap();
    let _ = std::fs::remove_file(&kf);
    std::os::unix::fs::symlink(&real, &kf).unwrap();
    let sym_out = fx.export_dir("export-datkey-symlink");
    let result = fx.inspect_with_dat_key(&sym_out, Some(&attach), Some(&kf));
    assert!(!result.status.success());
    assert!(String::from_utf8_lossy(&result.stdout).contains("symlink"));
}

#[test]
fn record_fingerprints_match_python_canonicalization() {
    let fx = fixture();
    let out = fx.export_dir("export-python");
    let attach = fx.root().join("attach");
    let result = fx.inspect(&out, Some(&attach));
    assert!(
        result.status.success(),
        "{}",
        String::from_utf8_lossy(&result.stdout)
    );

    // Recompute every record_sha256 the way the NAS collector does
    // (json.dumps ensure_ascii=False, sort_keys, compact) and require the
    // fingerprints to agree. This is the real cross-language gate over
    // integers, integral REALs, decimal REALs, BLOB b64 objects, and
    // non-ASCII text.
    let script = r#"
import sys, json, hashlib
bad = 0
with open(sys.argv[1], encoding='utf-8') as fh:
    for i, line in enumerate(fh):
        line = line.strip()
        if not line:
            continue
        rec = json.loads(line)
        claimed = rec.pop('record_sha256')
        canon = json.dumps(rec, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
        actual = hashlib.sha256(canon.encode('utf-8')).hexdigest()
        if actual != claimed:
            print('fingerprint mismatch at record %d' % i)
            bad += 1
sys.exit(1 if bad else 0)
"#;
    let output = Command::new("python3")
        .arg("-c")
        .arg(script)
        .arg(out.join("records.jsonl"))
        .output()
        .expect("python3 is required for the cross-language fingerprint gate");
    assert!(
        output.status.success(),
        "Rust and Python canonical fingerprints diverge: {}",
        String::from_utf8_lossy(&output.stdout)
    );
}

#[test]
fn thumbnail_only_image_is_labeled_and_never_an_original() {
    // Only `<md5>_t.dat` exists. The thumbnail bytes stage fine (they are
    // real local media), but the ref must say variant=thumbnail /
    // original_present=false, and the manifest must NOT claim a complete
    // media set — a thumbnail is never a substitute for the original.
    let fx = fixture_with_image(true, None);
    let out = fx.export_dir("export-thumb");
    let attach = fx.root().join("attach");
    let result = fx.inspect(&out, Some(&attach));
    assert!(
        result.status.success(),
        "{}",
        String::from_utf8_lossy(&result.stdout)
    );

    let rows = fx.records(&out);
    let img_ref = &rows[3]["media_refs"][0];
    assert_eq!(img_ref["kind"], "image");
    assert_eq!(img_ref["present"], true);
    assert_eq!(img_ref["decoded"], true);
    assert_eq!(img_ref["variant"], "thumbnail");
    assert_eq!(img_ref["original_present"], false);
    assert_eq!(
        img_ref["variant_evidence"]["file_name"],
        format!("{}_t.dat", fx.img_md5)
    );
    // The staged bytes are still the decoded thumbnail payload.
    let staged =
        std::fs::read(out.join("media").join(format!("md5_{}", fx.img_md5))).unwrap();
    assert_eq!(staged, fx.png_plain);

    let manifest = fx.manifest(&out);
    assert_eq!(manifest["media"]["complete"], false);
    assert_eq!(manifest["media"]["unavailable_count"], 1);
    let samples = manifest["media"]["sample_unavailable"]
        .as_array()
        .unwrap()
        .iter()
        .map(|v| v.as_str().unwrap_or_default())
        .collect::<String>();
    assert!(
        samples.contains("thumbnail") && samples.contains("original"),
        "the recorded gap must say only a thumbnail was captured: {samples}"
    );
}

#[test]
fn media_inputs_over_limit_fail_closed_before_reading() {
    let fx = fixture();
    let attach = fx.root().join("attach");

    // Image .dat (46 bytes) over a 16-byte cap: rejected at the stat BEFORE
    // any read; nothing is published.
    let img_out = fx.export_dir("export-img-limit");
    let result = fx.inspect_full(&img_out, Some(&attach), None, &["--max-media-bytes", "16"]);
    assert!(
        !result.status.success(),
        "over-limit image input exported as complete"
    );
    let stdout = String::from_utf8_lossy(&result.stdout);
    assert!(
        stdout.contains("media_input_over_limit"),
        "failure must carry the machine-readable token: {stdout}"
    );
    assert!(!img_out.join("export.json").exists());

    // Voice blob (26 bytes) over an 8-byte cap: the SQL length probe fires
    // BEFORE the blob is materialized (no attach root, so image/video/file
    // are honest gaps first and the voice row drives the failure).
    let voice_out = fx.export_dir("export-voice-limit");
    let result = fx.inspect_full(&voice_out, None, None, &["--max-media-bytes", "8"]);
    assert!(
        !result.status.success(),
        "over-limit voice input exported as complete"
    );
    let stdout = String::from_utf8_lossy(&result.stdout);
    assert!(
        stdout.contains("media_input_over_limit") && stdout.contains("voice blob"),
        "failure must name the bounded voice input: {stdout}"
    );
    assert!(!voice_out.join("export.json").exists());
}

#[test]
fn zstd_expansion_is_bounded_and_machine_readable() {
    // One row whose message_content is a highly compressible zstd blob.
    // Positive control: the default 64 MiB decode cap decodes the full text.
    // A 1 KiB cap refuses with `decoded_over_limit` (never expanded first);
    // a tiny --max-content-bytes refuses the RAW CELL pre-decode with the
    // distinct `content_over_limit` token. No failure publishes a manifest.
    let (tmp, table) = bomb_fixture();

    let ok_out = tmp.path().join("bomb-ok");
    let result = inspect_extra(tmp.path(), "bomb-ok", &[]);
    assert!(
        result.status.success(),
        "{}",
        String::from_utf8_lossy(&result.stdout)
    );
    let text = std::fs::read_to_string(ok_out.join("records.jsonl")).unwrap();
    let row: Value = serde_json::from_str(text.trim()).unwrap();
    assert_eq!(row["identity"]["table"], table);
    assert_eq!(
        row["message_content"].as_str().map(str::len),
        Some(1_000_000),
        "positive control: the bomb must decode fully under the default cap"
    );

    let limit_out = tmp.path().join("bomb-limit");
    let result = inspect_extra(tmp.path(), "bomb-limit", &["--max-decoded-bytes", "1024"]);
    assert!(
        !result.status.success(),
        "over-limit expansion exported as complete"
    );
    let stdout = String::from_utf8_lossy(&result.stdout);
    assert!(
        stdout.contains("decoded_over_limit"),
        "failure must carry the machine-readable token: {stdout}"
    );
    assert!(!limit_out.join("export.json").exists());

    let cell_out = tmp.path().join("bomb-cell");
    let result = inspect_extra(tmp.path(), "bomb-cell", &["--max-content-bytes", "8"]);
    assert!(
        !result.status.success(),
        "over-limit raw cell exported as complete"
    );
    let stdout = String::from_utf8_lossy(&result.stdout);
    assert!(
        stdout.contains("content_over_limit"),
        "failure must carry the machine-readable token: {stdout}"
    );
    assert!(!cell_out.join("export.json").exists());
}

/// Row-bounds fixture: one message shard whose table carries THREE unknown
/// BLOB columns (payload_a / payload_b / mystery — none of them are known
/// contract columns) plus WCDB_CT. Rowid 1 is the positive control (zstd
/// body flagged WCDB_CT=4, large server_id as exact-string evidence),
/// rowids 2..=normal_rows+1 are small legal rows, and an optional final row
/// carries caller-chosen payload sizes to drive the over-limit cases.
/// Everything stays synthetic: SQLCipher file in a temp dir, random key.
fn row_bounds_fixture(
    normal_rows: usize,
    oversize_row: Option<(usize, usize, usize)>,
) -> (TempDir, String) {
    let tmp = TempDir::new().unwrap();
    let root = tmp.path();
    std::fs::create_dir_all(root.join("source/message")).unwrap();
    let key_file = root.join("key.json");
    std::fs::write(&key_file, format!("{{\"raw_key\":\"{}\"}}", "5a".repeat(32))).unwrap();
    std::fs::set_permissions(&key_file, std::fs::Permissions::from_mode(0o600)).unwrap();
    let table = format!("Msg_{}", md5_hex(TALKER));
    let compressed = zstd::encode_all(b"compressed row-bounds body".as_slice(), 3).unwrap();
    let db = open_cipher(&root.join("source/message/message_0.db"));
    db.execute_batch(&format!(
        "CREATE TABLE Name2Id(user_name TEXT);
         INSERT INTO Name2Id VALUES ('{TALKER}');
         CREATE TABLE {table}(
             sort_seq INTEGER, server_id INTEGER, local_type INTEGER,
             create_time INTEGER, status INTEGER, message_content BLOB,
             packed_info_data BLOB, WCDB_CT_message_content INTEGER,
             payload_a BLOB, payload_b BLOB, mystery BLOB
         );"
    ))
    .unwrap();
    let insert = format!(
        "INSERT INTO {table}(sort_seq, server_id, local_type, create_time, status, \
         message_content, WCDB_CT_message_content, payload_a, payload_b, mystery) \
         VALUES (?,?,?,?,?,?,?,?,?,?)"
    );
    let small = vec![b'p'; 64];
    // Rowid 1: compressed body + server_id beyond 2^53.
    db.execute(
        &insert,
        params![
            1,
            9007199254740993i64,
            1,
            100,
            0,
            compressed,
            4,
            small.clone(),
            small.clone(),
            small.clone()
        ],
    )
    .unwrap();
    for i in 0..normal_rows {
        db.execute(
            &insert,
            params![
                (i + 2) as i64,
                1_000_000i64 + i as i64,
                1,
                200 + i as i64,
                0,
                format!("legal row {i}"),
                None::<i64>,
                small.clone(),
                small.clone(),
                small.clone()
            ],
        )
        .unwrap();
    }
    if let Some((a, b, m)) = oversize_row {
        db.execute(
            &insert,
            params![
                normal_rows as i64 + 2,
                7,
                1,
                999,
                0,
                "wide row",
                None::<i64>,
                vec![b'a'; a],
                vec![b'b'; b],
                vec![b'c'; m]
            ],
        )
        .unwrap();
    }
    db.close().unwrap();
    let result = invoke(&[
        "archive-snapshot",
        "--source",
        root.join("source").to_str().unwrap(),
        "--out",
        root.join("snapshot").to_str().unwrap(),
        "--key-file",
        key_file.to_str().unwrap(),
    ]);
    assert!(
        result.status.success(),
        "row-bounds snapshot failed: {}",
        String::from_utf8_lossy(&result.stdout)
    );
    (tmp, table)
}

#[test]
fn row_bounds_unknown_big_blob_rejected_before_payload_read() {
    // The unknown `mystery` column holds an 8192-byte BLOB. With
    // --max-row-bytes 4096 the header-only octet_length probe computes the
    // row total (8192+) from record headers — BEFORE any payload byte is
    // read anywhere — and the export fails closed with the
    // machine-readable token, publishing no manifest.
    let (tmp, _table) = row_bounds_fixture(3, Some((64, 64, 8192)));
    let out = tmp.path().join("export-blob-limit");
    let result = inspect_extra(
        tmp.path(),
        "export-blob-limit",
        &["--max-row-bytes", "4096", "--max-content-bytes", "4096"],
    );
    assert!(
        !result.status.success(),
        "over-limit unknown-column BLOB exported as complete"
    );
    let stdout = String::from_utf8_lossy(&result.stdout);
    assert!(
        stdout.contains("row_over_limit"),
        "failure must carry the machine-readable token: {stdout}"
    );
    assert!(
        stdout.contains("probed from record headers"),
        "failure must state the payload bytes were never read: {stdout}"
    );
    assert!(
        stdout.contains("message_0.db") && stdout.contains("Msg_"),
        "failure must name the shard and table: {stdout}"
    );
    assert!(!out.join("export.json").exists());
}

#[test]
fn row_bounds_wide_row_total_rejected_by_header_probe() {
    // Each payload cell (800 bytes) is UNDER the 2048-byte row cap — no
    // per-cell limit can catch this — but the three unknown columns sum
    // past the cap, and the header-only probe refuses the row before any
    // payload of it is materialized (in SQLite registers or in Rust).
    let (tmp, _table) = row_bounds_fixture(3, Some((800, 800, 800)));
    let out = tmp.path().join("export-wide-limit");
    let result = inspect_extra(
        tmp.path(),
        "export-wide-limit",
        &["--max-row-bytes", "2048", "--max-content-bytes", "1024"],
    );
    assert!(
        !result.status.success(),
        "wide over-limit row exported as complete"
    );
    let stdout = String::from_utf8_lossy(&result.stdout);
    assert!(
        stdout.contains("row_over_limit"),
        "failure must carry the machine-readable token: {stdout}"
    );
    assert!(
        stdout.contains("exceeds --max-row-bytes 2048"),
        "failure must state the cap: {stdout}"
    );
    assert!(
        stdout.contains("probed from record headers"),
        "failure must state the payload bytes were never read: {stdout}"
    );
    assert!(!out.join("export.json").exists());
}

#[test]
fn row_bounds_many_legal_rows_stream_across_pages_without_loss() {
    // 26 legal rows (1 compressed + 25 plain), --page-size 2 → 13 keyset
    // pages streamed one row at a time. Nothing may be lost or duplicated:
    // every rowid exactly once, ascending, and the manifest count must
    // agree with the discovered row count.
    let (tmp, _table) = row_bounds_fixture(25, None);
    let out = tmp.path().join("export-manypages");
    let result = inspect_extra(
        tmp.path(),
        "export-manypages",
        &[
            "--page-size",
            "2",
            "--max-row-bytes",
            "65536",
            "--max-content-bytes",
            "65536",
        ],
    );
    assert!(
        result.status.success(),
        "legal rows must export under a tight row cap: {}",
        String::from_utf8_lossy(&result.stdout)
    );
    let text = std::fs::read_to_string(out.join("records.jsonl")).unwrap();
    let rows: Vec<Value> = text
        .lines()
        .map(|line| serde_json::from_str(line).unwrap())
        .collect();
    assert_eq!(rows.len(), 26, "26 rows must survive 13 keyset pages");
    let mut rowids: Vec<i64> = rows
        .iter()
        .map(|r| r["identity"]["local_rowid"].as_i64().unwrap())
        .collect();
    let mut expected: Vec<i64> = (1..=26).collect();
    rowids.sort_unstable();
    assert_eq!(rowids, expected, "every rowid exactly once, none duplicated");
    // Ordering as written is ascending by local_rowid within the shard.
    let written: Vec<i64> = rows
        .iter()
        .map(|r| r["identity"]["local_rowid"].as_i64().unwrap())
        .collect();
    expected.sort_unstable();
    assert_eq!(written, expected, "records must be written in rowid order");

    // Positive controls: the compressed body decodes and the beyond-2^53
    // server_id survives as an exact decimal string, unchanged by the row
    // bounds.
    let first = &rows[0];
    assert_eq!(first["message_content"], "compressed row-bounds body");
    assert_eq!(
        first["identity"]["server_id"],
        serde_json::json!("9007199254740993")
    );

    let manifest: Value =
        serde_json::from_slice(&std::fs::read(out.join("export.json")).unwrap()).unwrap();
    assert_eq!(manifest["records"]["count"], 26);
    assert_eq!(manifest["enumeration"]["complete"], true);
    // All three unknown columns are preserved in `raw` for legal rows.
    assert!(
        first["raw"]["payload_a"]["b64"].is_string()
            && first["raw"]["payload_b"]["b64"].is_string()
            && first["raw"]["mystery"]["b64"].is_string()
    );
}

#[test]
fn row_bounds_caps_below_content_cap_fail_upfront() {
    // --max-row-bytes smaller than --max-content-bytes is an inconsistent
    // configuration: every legal content cell would be rejected as an
    // over-limit row. It must fail with a configuration error before any
    // export output is produced.
    let (tmp, _table) = row_bounds_fixture(1, None);
    let out = tmp.path().join("export-badcaps");
    let result = inspect_extra(tmp.path(), "export-badcaps", &["--max-row-bytes", "1024"]);
    assert!(
        !result.status.success(),
        "inconsistent caps accepted silently"
    );
    let stdout = String::from_utf8_lossy(&result.stdout);
    assert!(
        stdout.contains("--max-row-bytes") && stdout.contains("--max-content-bytes"),
        "failure must explain the inconsistency: {stdout}"
    );
    assert!(!out.join("export.json").exists());
}

/// Optional-known-column MATRIX fixture: the seven required columns plus
/// ONLY the optional known columns the caller asks for (WCDB_CT /
/// compress_content / real_sender_id), mirroring every real schema
/// combination a source table can present. Name2Id rowid 2 is
/// 'matrix-sender'; when the sender column exists its value is 2, so a
/// correct export must resolve real_sender_name from Name2Id regardless of
/// which OTHER optional columns are absent (a positional read that assumes
/// WCDB_CT always exists picks the wrong cell and yields null).
fn sender_matrix_fixture(wcdb: bool, compress: bool, sender: bool) -> (TempDir, String) {
    let tmp = TempDir::new().unwrap();
    let root = tmp.path();
    std::fs::create_dir_all(root.join("source/message")).unwrap();
    let key_file = root.join("key.json");
    std::fs::write(&key_file, format!("{{\"raw_key\":\"{}\"}}", "5a".repeat(32))).unwrap();
    std::fs::set_permissions(&key_file, std::fs::Permissions::from_mode(0o600)).unwrap();
    let table = format!("Msg_{}", md5_hex(TALKER));
    // Optional columns appended in EXACTLY the order the export assembles
    // its SELECT (wcdb, then compress, then sender) — that coupling is the
    // bug surface under test.
    let optional_ddl = [
        (wcdb, ", WCDB_CT_message_content INTEGER"),
        (compress, ", compress_content BLOB"),
        (sender, ", real_sender_id INTEGER"),
    ]
    .iter()
    .filter(|(present, _)| *present)
    .map(|(_, ddl)| *ddl)
    .collect::<String>();
    let db = open_cipher(&root.join("source/message/message_0.db"));
    db.execute_batch(&format!(
        "CREATE TABLE Name2Id(user_name TEXT);
         INSERT INTO Name2Id(rowid, user_name) VALUES (1, '{TALKER}'), (2, 'matrix-sender');
         CREATE TABLE {table}(
             sort_seq INTEGER, server_id INTEGER, local_type INTEGER,
             create_time INTEGER, status INTEGER, message_content TEXT,
             packed_info_data BLOB{optional_ddl}
         );
         INSERT INTO {table}(sort_seq, server_id, local_type, create_time, status, \
         message_content, packed_info_data)
             VALUES (1, 9007199254740993, 1, 100, 0, 'matrix body', NULL);"
    ))
    .unwrap();
    if wcdb {
        // WCDB_CT == 4 declares message_content to be a zstd frame. Store a
        // real frame for that schema combination so the fixture exercises
        // the production decoder contract instead of relying on invalid
        // metadata over plain-text bytes.
        let message_body = zstd::encode_all(b"matrix body".as_slice(), 3).unwrap();
        db.execute(
            &format!("UPDATE {table} SET message_content = ?1"),
            params![message_body],
        )
        .unwrap();
        db.execute(
            &format!("UPDATE {table} SET WCDB_CT_message_content = 4"),
            [],
        )
        .unwrap();
    }
    let compress_blob = zstd::encode_all(b"matrix compress payload".as_slice(), 3).unwrap();
    if compress {
        db.execute(
            &format!("UPDATE {table} SET compress_content = ?1"),
            params![compress_blob.clone()],
        )
        .unwrap();
    }
    if sender {
        db.execute(&format!("UPDATE {table} SET real_sender_id = 2"), [])
            .unwrap();
    }
    db.close().unwrap();
    let result = invoke(&[
        "archive-snapshot",
        "--source",
        root.join("source").to_str().unwrap(),
        "--out",
        root.join("snapshot").to_str().unwrap(),
        "--key-file",
        key_file.to_str().unwrap(),
    ]);
    assert!(
        result.status.success(),
        "sender-matrix snapshot failed: {}",
        String::from_utf8_lossy(&result.stdout)
    );
    (tmp, table)
}

#[test]
fn optional_known_columns_resolve_by_actual_schema_combination() {
    // All 8 real combinations of optional known columns × actual export: the
    // positional reads for WCDB_CT / compress_content / real_sender_id must
    // follow the schema flags cumulatively. The regression this locks out:
    // a table WITHOUT WCDB_CT whose real_sender_id was read at index 9
    // (assuming a WCDB placeholder) → null → real_sender_name lost → the
    // NAS collector's consume_sender chain fails downstream.
    for wcdb in [false, true] {
        for compress in [false, true] {
            for sender in [false, true] {
                let (tmp, table) = sender_matrix_fixture(wcdb, compress, sender);
                let out = tmp.path().join("export-matrix");
                let result = inspect_extra(tmp.path(), "export-matrix", &[]);
                assert!(
                    result.status.success(),
                    "wcdb={wcdb} compress={compress} sender={sender} export failed: {}",
                    String::from_utf8_lossy(&result.stdout)
                );
                let text = std::fs::read_to_string(out.join("records.jsonl")).unwrap();
                let row: Value = serde_json::from_str(text.trim()).unwrap();
                assert_eq!(row["identity"]["table"], table);

                // Sender: present only when the column exists, and then the
                // id must resolve to the Name2Id name — never null.
                if sender {
                    assert_eq!(
                        row["real_sender_id"], 2,
                        "wcdb={wcdb} compress={compress}: real_sender_id misread"
                    );
                    assert_eq!(
                        row["real_sender_name"], "matrix-sender",
                        "wcdb={wcdb} compress={compress}: real_sender_name lost"
                    );
                    assert_eq!(row["raw"]["real_sender_id"], 2);
                } else {
                    assert!(
                        row.get("real_sender_id").is_none()
                            && row.get("real_sender_name").is_none(),
                        "sender fields must not appear without the column"
                    );
                }
                // Compress: preserved losslessly only when the column exists.
                if compress {
                    assert_eq!(
                        row["compress_content_b64"]
                            .as_str()
                            .and_then(base64_decode),
                        Some(zstd::encode_all(b"matrix compress payload".as_slice(), 3).unwrap()),
                        "wcdb={wcdb} sender={sender}: compress_content misread"
                    );
                    assert!(row["raw"]["compress_content"]["b64"].is_string());
                } else {
                    assert!(row.get("compress_content_b64").is_none());
                }
                // WCDB_CT: exactly the stored flag when the column exists.
                if wcdb {
                    assert_eq!(
                        row["wcdb_ct"], 4,
                        "compress={compress} sender={sender}: WCDB_CT misread"
                    );
                } else {
                    assert!(row.get("wcdb_ct").is_none());
                }
                // The row body itself is unaffected by column combination.
                assert_eq!(row["message_content"], "matrix body");
            }
        }
    }
}

/// Memory-measurement fixture for the header-probe proof: one legal row,
/// plus (in the `wide` variant) a second row carrying 16 unknown BLOB
/// columns of 4 MiB each — a 64 MiB raw row whose every cell passes an
/// 8 MiB per-cell limit. zeroblob() keeps fixture building cheap.
fn row_memory_fixture(wide: bool) -> TempDir {
    let tmp = TempDir::new().unwrap();
    let root = tmp.path();
    std::fs::create_dir_all(root.join("source/message")).unwrap();
    let key_file = root.join("key.json");
    std::fs::write(&key_file, format!("{{\"raw_key\":\"{}\"}}", "5a".repeat(32))).unwrap();
    std::fs::set_permissions(&key_file, std::fs::Permissions::from_mode(0o600)).unwrap();
    let table = format!("Msg_{}", md5_hex(TALKER));
    let wide_columns = if wide {
        format!(
            ", {}",
            (0..16)
                .map(|i| format!("wide_{i} BLOB"))
                .collect::<Vec<_>>()
                .join(", ")
        )
    } else {
        String::new()
    };
    let db = open_cipher(&root.join("source/message/message_0.db"));
    db.execute_batch(&format!(
        "CREATE TABLE Name2Id(user_name TEXT);
         INSERT INTO Name2Id VALUES ('{TALKER}');
         CREATE TABLE {table}(
             sort_seq INTEGER, server_id INTEGER, local_type INTEGER,
             create_time INTEGER, status INTEGER, message_content TEXT,
             packed_info_data BLOB{wide_columns}
         );
         INSERT INTO {table}(sort_seq, server_id, local_type, create_time, status, \
         message_content, packed_info_data)
             VALUES (1, 9007199254740993, 1, 100, 0, 'legal body', NULL);"
    ))
    .unwrap();
    if wide {
        let columns = (0..16)
            .map(|i| format!("wide_{i}"))
            .collect::<Vec<_>>()
            .join(", ");
        let values = (0..16)
            .map(|_| "zeroblob(4194304)")
            .collect::<Vec<_>>()
            .join(", ");
        db.execute_batch(&format!(
            "INSERT INTO {table}(sort_seq, server_id, local_type, create_time, status, \
             message_content, packed_info_data, {columns})
             VALUES (2, 42, 1, 101, 0, 'wide body', NULL, {values});"
        ))
        .unwrap();
    }
    db.close().unwrap();
    let result = invoke(&[
        "archive-snapshot",
        "--source",
        root.join("source").to_str().unwrap(),
        "--out",
        root.join("snapshot").to_str().unwrap(),
        "--key-file",
        key_file.to_str().unwrap(),
    ]);
    assert!(
        result.status.success(),
        "row-memory snapshot failed: {}",
        String::from_utf8_lossy(&result.stdout)
    );
    tmp
}

/// Run one export through a python3 wrapper that reports the CLI child's
/// PEAK RSS (ru_maxrss of RUSAGE_CHILDREN; bytes on macOS, pages->bytes
/// elsewhere) alongside the exit status and combined output. This mirrors
/// the independent memory gate's measurement method.
fn measured_export(root: &Path) -> (i64, bool, String) {
    let script = r#"
import json, resource, subprocess, sys
from pathlib import Path
binary, path = sys.argv[1:]
p = Path(path)
r = subprocess.run([
    binary, 'archive-inspect',
    '--snapshots', str(p / 'snapshot'),
    '--key-file', str(p / 'key.json'),
    '--action', 'export',
    '--talker', 'talker-a@synthetic',
    '--account', 'synthetic-account',
    '--archive-id', 'synthetic-archive',
    '--out', str(p / 'export'),
    '--max-row-bytes', '8388608',
    '--max-content-bytes', '1024',
], capture_output=True)
peak = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss
if sys.platform != 'darwin':
    peak *= 1024
output = (r.stdout + r.stderr).decode('utf-8', 'replace')
print(json.dumps([peak, r.returncode == 0, output]))
"#;
    let output = Command::new("python3")
        .arg("-c")
        .arg(script)
        .arg(BIN)
        .arg(root)
        .output()
        .expect("python3 is required for the peak-RSS measurement");
    assert!(output.status.success(), "measurement wrapper failed");
    let parsed: serde_json::Value = serde_json::from_slice(&output.stdout).unwrap();
    (
        parsed[0].as_i64().unwrap(),
        parsed[1].as_bool().unwrap(),
        parsed[2].as_str().unwrap_or_default().to_string(),
    )
}

#[test]
fn wide_rejected_row_is_refused_without_materializing_payloads() {
    // The measured memory gate (source-side mirror of the independent
    // acceptance): a 64 MiB raw row built from 16 unknown 4 MiB cells —
    // every cell PASSES the 8 MiB per-cell limit, so only the whole-row
    // budget can refuse it. The header-only probe must reject the row
    // BEFORE any payload is materialized: the failing export's peak RSS
    // may exceed the legal export's by at most 32 MiB. The pre-probe
    // implementation materialized the full row first and measured ~4x the
    // legal baseline.
    let small = row_memory_fixture(false);
    let wide = row_memory_fixture(true);

    let (small_peak, small_ok, _) = measured_export(small.path());
    let (wide_peak, wide_ok, wide_output) = measured_export(wide.path());

    assert!(small_ok, "legal single-row export must succeed");
    assert!(
        !wide_ok,
        "64 MiB raw row must be refused under an 8 MiB row budget"
    );
    assert!(
        wide_output.contains("row_over_limit"),
        "refusal must carry the machine-readable token: {wide_output}"
    );
    assert!(
        !wide.path().join("export").join("export.json").exists(),
        "failed export published a manifest"
    );
    assert!(
        wide_peak < small_peak + 32 * 1024 * 1024,
        "rejected row was materialized before the total-row check: \
         small RSS={small_peak}, wide RSS={wide_peak}"
    );
}

/// Minimal standard-alphabet base64 decoder for asserting lossless
/// round-trips of preserved blobs.
fn base64_decode(input: &str) -> Option<Vec<u8>> {
    const TABLE: &[u8; 64] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
    let mut out = Vec::new();
    let mut buffer: u32 = 0;
    let mut bits = 0u32;
    for ch in input.chars() {
        if ch == '=' {
            break;
        }
        let value = TABLE.iter().position(|t| *t as char == ch)? as u32;
        buffer = (buffer << 6) | value;
        bits += 6;
        if bits >= 8 {
            bits -= 8;
            out.push(((buffer >> bits) & 0xFF) as u8);
        }
    }
    Some(out)
}
