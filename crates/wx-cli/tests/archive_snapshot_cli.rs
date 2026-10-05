//! End-to-end behavior tests for `wx-cli archive-snapshot`: real worker
//! subprocesses, real SQLCipher fixtures, real supervision. Everything runs
//! in isolated temp directories with synthetic data only.

use std::os::raw::c_void;
use std::os::unix::fs::PermissionsExt;
use std::path::Path;
use std::process::{Command, Output};
use std::time::{Duration, Instant};

use rusqlite::Connection;
use tempfile::TempDir;

const BIN: &str = env!("CARGO_BIN_EXE_wx-cli");

fn make_encrypted_db(path: &Path, raw_key: &[u8; 32], setup_sql: &str) {
    if let Some(parent) = path.parent() {
        std::fs::create_dir_all(parent).unwrap();
    }
    let conn = Connection::open(path).unwrap();
    unsafe {
        let rc =
            rusqlite::ffi::sqlite3_key(conn.handle(), raw_key.as_ptr() as *const c_void, 32);
        assert_eq!(rc, 0, "fixture sqlite3_key failed");
    }
    conn.execute_batch(setup_sql).unwrap();
    conn.close().unwrap();
}

fn write_key_file(dir: &Path, raw_key: &[u8; 32], mode: u32) -> std::path::PathBuf {
    let key_hex: String = raw_key.iter().map(|b| format!("{b:02x}")).collect();
    let path = dir.join("key.json");
    std::fs::write(&path, format!("{{\"raw_key\":\"{key_hex}\"}}")).unwrap();
    std::fs::set_permissions(&path, std::fs::Permissions::from_mode(mode)).unwrap();
    path
}

fn run_cli(args: &[&str]) -> Output {
    Command::new(BIN).args(args).output().expect("spawn wx-cli")
}

fn stdout_json(output: &Output) -> serde_json::Value {
    let text = String::from_utf8_lossy(&output.stdout);
    serde_json::from_str(text.trim()).expect("stdout must be one JSON object")
}

fn simple_source(root: &Path, raw_key: &[u8; 32]) {
    make_encrypted_db(
        &root.join("contact/contact.db"),
        raw_key,
        "CREATE TABLE contact (username TEXT); INSERT INTO contact VALUES ('wxid_a');",
    );
    make_encrypted_db(
        &root.join("session/session.db"),
        raw_key,
        "CREATE TABLE session (name TEXT); INSERT INTO session VALUES ('s1');",
    );
    make_encrypted_db(
        &root.join("message/message_0.db"),
        raw_key,
        "CREATE TABLE Msg_abc (sort_seq INTEGER, message_content TEXT); \
         INSERT INTO Msg_abc VALUES (1, 'hello'), (2, 'world');",
    );
}

fn mode_of(path: &Path) -> u32 {
    PermissionsExt::mode(&std::fs::metadata(path).unwrap().permissions())
}

#[test]
fn happy_path_publishes_complete_private_generation() {
    let tmp = TempDir::new().unwrap();
    let source = tmp.path().join("source");
    let out = tmp.path().join("out");
    let raw_key = [0x9A_u8; 32];
    simple_source(&source, &raw_key);
    let key_file = write_key_file(tmp.path(), &raw_key, 0o600);

    let output = run_cli(&[
        "archive-snapshot",
        "--source",
        source.to_str().unwrap(),
        "--out",
        out.to_str().unwrap(),
        "--key-file",
        key_file.to_str().unwrap(),
        "--json",
    ]);
    assert!(output.status.success(), "stdout={}", String::from_utf8_lossy(&output.stdout));

    let report = stdout_json(&output);
    assert_eq!(report["complete"], serde_json::json!(true));
    assert_eq!(report["databases"].as_array().unwrap().len(), 3);

    // manifest.json (the complete-generation marker) is durable and private.
    let manifest_path = out.join("manifest.json");
    assert!(manifest_path.exists());
    assert_eq!(mode_of(&manifest_path) & 0o077, 0);
    let manifest: serde_json::Value =
        serde_json::from_str(&std::fs::read_to_string(&manifest_path).unwrap()).unwrap();
    assert_eq!(manifest["complete"], serde_json::json!(true));
    for db in manifest["databases"].as_array().unwrap() {
        assert_eq!(db["ok"], serde_json::json!(true), "{db}");
        assert!(db["sha256"].as_str().unwrap().len() == 64, "{db}");
    }
    assert!(!out.join("manifest.partial.json").exists());

    // Published copies are exactly 0400 in 0700 directories.
    for rel in ["contact/contact.db", "session/session.db", "message/message_0.db"] {
        assert_eq!(mode_of(&out.join(rel)) & 0o7777, 0o400, "{rel}");
        assert_eq!(mode_of(out.join(rel).parent().unwrap()) & 0o077, 0, "{rel}");
        assert!(!out.join(format!("{rel}.candidate")).exists());
    }
    // No event files or other leftovers.
    let names: Vec<String> = std::fs::read_dir(&out)
        .unwrap()
        .filter_map(|e| e.ok())
        .map(|e| e.file_name().to_string_lossy().to_string())
        .collect();
    assert!(names.iter().all(|n| !n.contains(".events-")), "{names:?}");
}

#[test]
fn rerunning_into_existing_generation_fails_closed() {
    let tmp = TempDir::new().unwrap();
    let source = tmp.path().join("source");
    let out = tmp.path().join("out");
    let raw_key = [0x9B_u8; 32];
    simple_source(&source, &raw_key);
    let key_file = write_key_file(tmp.path(), &raw_key, 0o600);

    let first = run_cli(&[
        "archive-snapshot", "--source", source.to_str().unwrap(),
        "--out", out.to_str().unwrap(), "--key-file", key_file.to_str().unwrap(),
    ]);
    assert!(first.status.success());
    let sentinel = out.join("contact/contact.db");
    let before = std::fs::read(&sentinel).unwrap();

    let second = run_cli(&[
        "archive-snapshot", "--source", source.to_str().unwrap(),
        "--out", out.to_str().unwrap(), "--key-file", key_file.to_str().unwrap(),
    ]);
    assert!(!second.status.success());
    let report = stdout_json(&second);
    assert!(report["error"].as_str().unwrap().contains("already exists"));

    // The existing generation is untouched.
    assert_eq!(std::fs::read(&sentinel).unwrap(), before);
}

#[test]
fn tx_window_timeout_publishes_nothing_and_reports_kind() {
    let tmp = TempDir::new().unwrap();
    let source = tmp.path().join("source");
    let out = tmp.path().join("out");
    let raw_key = [0x9C_u8; 32];
    // Big enough that a 1-page-per-step backup cannot finish in 1ms.
    let mut setup = String::from("CREATE TABLE big (k INTEGER PRIMARY KEY, blob BLOB);");
    for i in 0..4000 {
        setup.push_str(&format!("INSERT INTO big VALUES ({i}, x'{}');", "ab".repeat(900)));
    }
    make_encrypted_db(&source.join("message/message_0.db"), &raw_key, &setup);
    let key_file = write_key_file(tmp.path(), &raw_key, 0o600);

    let started = Instant::now();
    let output = run_cli(&[
        "archive-snapshot",
        "--source", source.to_str().unwrap(),
        "--out", out.to_str().unwrap(),
        "--key-file", key_file.to_str().unwrap(),
        "--t-max-ms", "1",
        "--pages-per-step", "1",
        "--grace-ms", "5000",
        "--json",
    ]);
    assert!(!output.status.success());
    // The whole run (including the kill) must respect the T_max gate: with
    // t_max=1ms the worker cannot survive ~1s of supervision.
    assert!(started.elapsed() < Duration::from_secs(3), "kill was too slow");

    let report = stdout_json(&output);
    assert_eq!(report["complete"], serde_json::json!(false));
    let db_entry = &report["databases"][0];
    let kind = db_entry["kind"].as_str().unwrap();
    assert!(
        kind == "timeout" || kind.starts_with("parent_killed_"),
        "unexpected kind {kind}"
    );

    // Nothing was published, no candidate survived, only the partial marker.
    assert!(!out.join("message/message_0.db").exists(), "no half copy");
    assert!(!out.join("message/message_0.db.candidate").exists());
    assert!(!out.join("manifest.json").exists());
    let partial = out.join("manifest.partial.json");
    assert!(partial.exists());
    let manifest: serde_json::Value =
        serde_json::from_str(&std::fs::read_to_string(&partial).unwrap()).unwrap();
    assert_eq!(manifest["complete"], serde_json::json!(false));
}

#[test]
fn parent_kill_releases_source_locks() {
    let tmp = TempDir::new().unwrap();
    let source = tmp.path().join("source");
    let out = tmp.path().join("out");
    let raw_key = [0x9D_u8; 32];
    // Non-WAL source + long backup: the worker's read transaction blocks a
    // concurrent writer's COMMIT until the parent kills the worker.
    let mut setup = String::from("CREATE TABLE big (k INTEGER PRIMARY KEY, blob BLOB);");
    for i in 0..6000 {
        setup.push_str(&format!("INSERT INTO big VALUES ({i}, x'{}');", "cd".repeat(900)));
    }
    let db_path = source.join("message/message_0.db");
    make_encrypted_db(&db_path, &raw_key, &setup);
    let key_file = write_key_file(tmp.path(), &raw_key, 0o600);

    let mut cmd = Command::new(BIN);
    cmd.args([
        "archive-snapshot",
        "--source", source.to_str().unwrap(),
        "--out", out.to_str().unwrap(),
        "--key-file", key_file.to_str().unwrap(),
        "--t-max-ms", "15",
        "--pages-per-step", "1",
        "--grace-ms", "5000",
    ]);
    let child = cmd.spawn().unwrap();

    // Writer: grab RESERVED immediately, write, hold, then commit — the
    // commit needs EXCLUSIVE, which the worker's pinned read transaction
    // denies until the parent kills the worker and the OS releases its locks.
    let writer_db = db_path.clone();
    let writer = std::thread::spawn(move || {
        let conn = Connection::open(&writer_db).unwrap();
        unsafe {
            rusqlite::ffi::sqlite3_key(
                conn.handle(),
                [0x9D_u8; 32].as_ptr() as *const c_void,
                32,
            );
        }
        conn.execute_batch("BEGIN IMMEDIATE; INSERT INTO big VALUES (999999, x'00');")
            .unwrap();
        std::thread::sleep(Duration::from_millis(60));
        conn.execute_batch("COMMIT;").expect("commit must succeed after the kill");
    });

    let output = child.wait_with_output().unwrap();
    assert!(!output.status.success());
    // The blocked writer completes only because SIGKILL released the locks.
    writer.join().expect("writer thread must not panic");

    // And the failed run still left a machine-readable partial marker.
    let partial = out.join("manifest.partial.json");
    assert!(partial.exists());
}

#[test]
fn one_bad_database_marks_generation_incomplete_but_keeps_good_copies() {
    let tmp = TempDir::new().unwrap();
    let source = tmp.path().join("source");
    let out = tmp.path().join("out");
    let raw_key = [0x9E_u8; 32];
    simple_source(&source, &raw_key);
    // A plaintext junk .db: enumerated (not silently skipped) but key check
    // fails — this database is reported, the run is incomplete.
    std::fs::create_dir_all(source.join("message")).unwrap();
    std::fs::write(source.join("message/broken.db"), b"not a database at all").unwrap();
    let key_file = write_key_file(tmp.path(), &raw_key, 0o600);

    let output = run_cli(&[
        "archive-snapshot",
        "--source", source.to_str().unwrap(),
        "--out", out.to_str().unwrap(),
        "--key-file", key_file.to_str().unwrap(),
        "--json",
    ]);
    assert!(!output.status.success());
    let report = stdout_json(&output);
    assert_eq!(report["complete"], serde_json::json!(false));
    let dbs = report["databases"].as_array().unwrap();
    assert_eq!(dbs.len(), 4, "broken.db must be reported, not skipped");
    let broken = dbs.iter().find(|d| d["db"] == "message/broken.db").unwrap();
    assert_eq!(broken["ok"], serde_json::json!(false));
    assert_eq!(broken["kind"], serde_json::json!("key"));

    // Good copies still published; only the partial manifest exists.
    assert!(out.join("contact/contact.db").exists());
    assert!(!out.join("manifest.json").exists());
    assert!(out.join("manifest.partial.json").exists());
}

#[test]
fn loose_key_file_is_rejected_before_anything_is_created() {
    let tmp = TempDir::new().unwrap();
    let source = tmp.path().join("source");
    let out = tmp.path().join("out");
    let raw_key = [0x81_u8; 32];
    simple_source(&source, &raw_key);
    let key_file = write_key_file(tmp.path(), &raw_key, 0o644);

    let output = run_cli(&[
        "archive-snapshot",
        "--source", source.to_str().unwrap(),
        "--out", out.to_str().unwrap(),
        "--key-file", key_file.to_str().unwrap(),
        "--json",
    ]);
    assert!(!output.status.success());
    let report = stdout_json(&output);
    assert!(report["error"].as_str().unwrap().contains("0600"));
    assert!(!out.exists(), "no output may be created on rejection");
}

#[test]
fn output_inside_source_is_rejected() {
    let tmp = TempDir::new().unwrap();
    let source = tmp.path().join("source");
    let out = source.join("gen");
    let raw_key = [0x82_u8; 32];
    simple_source(&source, &raw_key);
    let key_file = write_key_file(tmp.path(), &raw_key, 0o600);

    let output = run_cli(&[
        "archive-snapshot",
        "--source", source.to_str().unwrap(),
        "--out", out.to_str().unwrap(),
        "--key-file", key_file.to_str().unwrap(),
        "--json",
    ]);
    assert!(!output.status.success());
    assert!(stdout_json(&output)["error"]
        .as_str()
        .unwrap()
        .contains("inside the source"));
    assert!(!out.exists());
}

#[test]
fn output_containing_source_is_rejected() {
    let tmp = TempDir::new().unwrap();
    let source = tmp.path().join("root").join("source");
    let out = tmp.path().join("root");
    let raw_key = [0x83_u8; 32];
    simple_source(&source, &raw_key);
    let key_file = write_key_file(tmp.path(), &raw_key, 0o600);

    let output = run_cli(&[
        "archive-snapshot",
        "--source", source.to_str().unwrap(),
        "--out", out.to_str().unwrap(),
        "--key-file", key_file.to_str().unwrap(),
        "--json",
    ]);
    assert!(!output.status.success());
    assert!(stdout_json(&output)["error"]
        .as_str()
        .unwrap()
        .contains("contains the source"));
}

#[test]
fn symlinked_source_alias_is_rejected() {
    let tmp = TempDir::new().unwrap();
    let source = tmp.path().join("source");
    let out = tmp.path().join("out");
    let raw_key = [0x84_u8; 32];
    simple_source(&source, &raw_key);
    let key_file = write_key_file(tmp.path(), &raw_key, 0o600);
    let alias = tmp.path().join("source-alias");
    std::os::unix::fs::symlink(&source, &alias).unwrap();

    let output = run_cli(&[
        "archive-snapshot",
        "--source", alias.to_str().unwrap(),
        "--out", out.to_str().unwrap(),
        "--key-file", key_file.to_str().unwrap(),
        "--json",
    ]);
    assert!(!output.status.success());
    assert!(stdout_json(&output)["error"]
        .as_str()
        .unwrap()
        .contains("symlink"));
    assert!(!out.exists());
}

#[test]
fn source_symlink_inside_tree_fails_closed() {
    let tmp = TempDir::new().unwrap();
    let source = tmp.path().join("source");
    let out = tmp.path().join("out");
    let raw_key = [0x85_u8; 32];
    simple_source(&source, &raw_key);
    let key_file = write_key_file(tmp.path(), &raw_key, 0o600);
    // An in-tree symlink database: enumeration must fail closed, not skip.
    std::os::unix::fs::symlink(
        source.join("contact/contact.db"),
        source.join("contact/evil.db"),
    )
    .unwrap();

    let output = run_cli(&[
        "archive-snapshot",
        "--source", source.to_str().unwrap(),
        "--out", out.to_str().unwrap(),
        "--key-file", key_file.to_str().unwrap(),
        "--json",
    ]);
    assert!(!output.status.success());
    assert!(stdout_json(&output)["error"].as_str().unwrap().contains("symlink"));
}

#[test]
fn mixed_alias_spellings_cannot_smuggle_output_into_source() {
    // Regression for the /var vs /private/var alias: a CANONICAL source
    // spelling combined with an ALIAS output spelling previously compared
    // unequal and let the generation directory be created INSIDE the source
    // tree. Both spellings must resolve to real paths for the check.
    let tmp = TempDir::new().unwrap();
    let source_real = tmp.path().join("source");
    let raw_key = [0x86_u8; 32];
    simple_source(&source_real, &raw_key);
    let key_file = write_key_file(tmp.path(), &raw_key, 0o600);

    let source_canonical = source_real.canonicalize().unwrap();
    let source_str = source_canonical.to_str().unwrap();
    assert!(source_str.starts_with("/private/"), "fixture must use the real spelling");
    // Re-spell the same tree through the /var alias for the output path.
    let alias_root: String = source_str.replacen("/private/var", "/var", 1);
    let out_alias = format!("{alias_root}/gen");

    let output = run_cli(&[
        "archive-snapshot",
        "--source", source_str,
        "--out", &out_alias,
        "--key-file", key_file.to_str().unwrap(),
        "--json",
    ]);
    assert!(
        !output.status.success(),
        "alias-spelled output inside the source must be rejected"
    );
    let report = stdout_json(&output);
    assert!(
        report["error"].as_str().unwrap().contains("inside the source"),
        "{}",
        report["error"].as_str().unwrap()
    );
    // And the source tree stays clean — no generation leaked into it.
    assert!(!source_real.join("gen").exists());
}

#[test]
fn worker_refuses_to_begin_without_parent_ack() {
    // Direct worker invocation with an immediately-closed stdin: the
    // pinning event is written, but no ack ever arrives (EOF), so the worker
    // must abort with kind=no_parent_ack WITHOUT starting the transaction
    // or creating a candidate.
    let tmp = TempDir::new().unwrap();
    let source = tmp.path().join("source");
    let raw_key = [0x87_u8; 32];
    simple_source(&source, &raw_key);
    let key_file = write_key_file(tmp.path(), &raw_key, 0o600);
    let events = tmp.path().join("events.jsonl");
    std::fs::write(&events, "").unwrap();
    let candidate = tmp.path().join("candidate.db");
    let publish = tmp.path().join("published.db");

    let output = Command::new(BIN)
        .args([
            "__archive-snapshot-worker",
            "--db", source.join("contact/contact.db").to_str().unwrap(),
            "--candidate", candidate.to_str().unwrap(),
            "--publish", publish.to_str().unwrap(),
            "--key-file", key_file.to_str().unwrap(),
            "--t-max-ms", "5000",
            "--pages-per-step", "128",
            "--events", events.to_str().unwrap(),
            "--run-id", "42",
        ])
        .stdin(std::process::Stdio::null())
        .output()
        .unwrap();

    assert!(!output.status.success());
    let report = stdout_json(&output);
    assert_eq!(report["ok"], serde_json::json!(false));
    assert_eq!(report["kind"], serde_json::json!("no_parent_ack"));
    // No locks were taken (nothing published, no candidate), and the pinning
    // handshake event IS present — the parent would have acked in a real
    // run; here EOF must abort before BEGIN.
    assert!(!candidate.exists());
    assert!(!publish.exists());
    let events_text = std::fs::read_to_string(&events).unwrap();
    assert!(events_text.contains("\"event\":\"pinning\""), "{events_text}");
}
