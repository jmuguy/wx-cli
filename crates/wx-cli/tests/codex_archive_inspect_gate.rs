//! Independent black-box archive-inspect completeness gates. Synthetic data only.
use rusqlite::Connection;
use serde_json::Value;
use std::{
    os::{raw::c_void, unix::fs::PermissionsExt},
    path::Path,
    process::{Command, Output},
};
use tempfile::TempDir;
const BIN: &str = env!("CARGO_BIN_EXE_wx-cli");
fn invoke(args: &[&str]) -> Output {
    Command::new(BIN).args(args).output().unwrap()
}
fn fixture() -> TempDir {
    fixture_with_compression(false)
}
fn fixture_with_compression(compressed: bool) -> TempDir {
    let tmp = TempDir::new().unwrap();
    let source = tmp.path().join("source/message");
    std::fs::create_dir_all(&source).unwrap();
    let key = [0xA7u8; 32];
    let table = format!("Msg_{:x}", md5::compute("synthetic@chatroom"));
    for i in 0..2 {
        let db = Connection::open(source.join(format!("message_{i}.db"))).unwrap();
        unsafe {
            assert_eq!(
                rusqlite::ffi::sqlite3_key(db.handle(), key.as_ptr() as *const c_void, 32),
                0
            );
        }
        db.execute_batch(&format!("CREATE TABLE Name2Id(user_name TEXT); INSERT INTO Name2Id VALUES ('synthetic@chatroom'); CREATE TABLE {table}(sort_seq INTEGER, server_id INTEGER, local_type INTEGER, create_time INTEGER, status INTEGER, message_content TEXT, packed_info_data BLOB); INSERT INTO {table} VALUES(1,{},1,100,0,'synthetic body',NULL);", 9007199254740993i64+i)).unwrap();
        if compressed {
            // Fixed zstd frame for a synthetic Unicode message, no real source data.
            let bytes: &[u8] = &[
                40, 181, 47, 253, 4, 88, 33, 1, 0, 229, 144, 136, 230, 136, 144, 229, 142, 139,
                231, 188, 169, 230, 173, 163, 230, 150, 135, 32, 115, 121, 110, 116, 104, 101, 116,
                105, 99, 32, 109, 101, 115, 115, 97, 103, 101, 214, 55, 40, 184,
            ];
            db.execute_batch(&format!("ALTER TABLE {table} ADD COLUMN WCDB_CT_message_content INTEGER; UPDATE {table} SET WCDB_CT_message_content=4;")).unwrap();
            db.execute(&format!("UPDATE {table} SET message_content=?"), [bytes])
                .unwrap();
        }
        db.close().unwrap();
    }
    let key_file = tmp.path().join("key.json");
    std::fs::write(
        &key_file,
        format!("{{\"raw_key\":\"{}\"}}", "a7".repeat(32)),
    )
    .unwrap();
    std::fs::set_permissions(&key_file, std::fs::Permissions::from_mode(0o600)).unwrap();
    let result = invoke(&[
        "archive-snapshot",
        "--source",
        tmp.path().join("source").to_str().unwrap(),
        "--out",
        tmp.path().join("snapshot").to_str().unwrap(),
        "--key-file",
        key_file.to_str().unwrap(),
    ]);
    assert!(result.status.success(), "snapshot fixture failed");
    tmp
}
fn inspect(root: &Path) -> Output {
    invoke(&[
        "archive-inspect",
        "--snapshots",
        root.join("snapshot").to_str().unwrap(),
        "--key-file",
        root.join("key.json").to_str().unwrap(),
        "--action",
        "export",
        "--talker",
        "synthetic@chatroom",
        "--account",
        "synthetic-account",
        "--archive-id",
        "synthetic-archive",
        "--out",
        root.join("export").to_str().unwrap(),
        "--page-size",
        "1",
    ])
}
#[test]
fn codex_missing_manifest_shard_rejects_complete_export() {
    let tmp = fixture();
    std::fs::remove_file(tmp.path().join("snapshot/message/message_1.db")).unwrap();
    let result = inspect(tmp.path());
    assert!(
        !result.status.success(),
        "missing expected shard was silently exported as complete"
    );
}
#[test]
fn codex_real_shard_identity_and_large_server_id_survive_export() {
    let tmp = fixture();
    let result = inspect(tmp.path());
    assert!(
        result.status.success(),
        "{}",
        String::from_utf8_lossy(&result.stdout)
    );
    let text = std::fs::read_to_string(tmp.path().join("export/records.jsonl")).unwrap();
    let rows: Vec<Value> = text
        .lines()
        .map(|s| serde_json::from_str(s).unwrap())
        .collect();
    assert_eq!(rows.len(), 2);
    assert_ne!(
        rows[0]["identity"]["shard"], rows[1]["identity"]["shard"],
        "different database shards collapsed to same table identity"
    );
    for row in rows {
        assert_eq!(
            row["message_content"], "synthetic body",
            "source body changed during export"
        );
        assert!(
            row["identity"]["server_id"].is_string(),
            "large server_id must be an exact string"
        );
    }
}

#[test]
fn codex_duplicate_manifest_entry_cannot_duplicate_capture() {
    let tmp = fixture();
    let path = tmp.path().join("snapshot/manifest.json");
    let mut manifest: Value = serde_json::from_slice(&std::fs::read(&path).unwrap()).unwrap();
    let entries = manifest["databases"].as_array_mut().unwrap();
    entries.push(entries[0].clone());
    std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o600)).unwrap();
    std::fs::write(&path, serde_json::to_vec(&manifest).unwrap()).unwrap();
    let result = inspect(tmp.path());
    assert!(
        !result.status.success(),
        "duplicate manifest entry accepted as complete generation"
    );
}

#[test]
fn codex_compressed_source_reaches_python_collector_losslessly() {
    let tmp = fixture_with_compression(true);
    let result = inspect(tmp.path());
    assert!(
        result.status.success(),
        "{}",
        String::from_utf8_lossy(&result.stdout)
    );
    let export = tmp.path().join("export");
    let text = std::fs::read_to_string(export.join("records.jsonl")).unwrap();
    for line in text.lines() {
        let record: Value = serde_json::from_str(line).unwrap();
        assert_eq!(record["message_content"], "合成压缩正文 synthetic message");
        assert_eq!(record["raw"]["WCDB_CT_message_content"], 4);
        assert!(record["raw"]["message_content"]["b64"].is_string());
    }
    let root = Path::new(env!("CARGO_MANIFEST_DIR"))
        .parent()
        .unwrap()
        .parent()
        .unwrap();
    let output=Command::new("python3").current_dir(root).args(["-c",
        "import sys; from archive.v3.collector import load_session_export; x=load_session_export(sys.argv[1]); assert sum(1 for _ in x.records)==2",
        export.to_str().unwrap()]).output().unwrap();
    assert!(
        output.status.success(),
        "Python rejected real Rust export: {}",
        String::from_utf8_lossy(&output.stderr)
    );
}
