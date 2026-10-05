//! Independent raw-row budget gates; only synthetic encrypted databases.
use rusqlite::Connection;
use serde_json::Value;
use std::{
    os::{raw::c_void, unix::fs::PermissionsExt},
    process::{Command, Output},
};
use tempfile::TempDir;

fn fixture(extra: &str, rows: usize) -> TempDir {
    let tmp = TempDir::new().unwrap();
    let source = tmp.path().join("source/message");
    std::fs::create_dir_all(&source).unwrap();
    let db = Connection::open(source.join("message_0.db")).unwrap();
    let key = [0x93u8; 32];
    unsafe {
        assert_eq!(
            rusqlite::ffi::sqlite3_key(db.handle(), key.as_ptr() as *const c_void, 32),
            0
        );
    }
    let table = format!("Msg_{:x}", md5::compute("bounds@chatroom"));
    db.execute_batch(&format!("CREATE TABLE Name2Id(user_name TEXT); INSERT INTO Name2Id VALUES ('bounds@chatroom'); CREATE TABLE {table}(sort_seq INTEGER, server_id INTEGER, local_type INTEGER, create_time INTEGER, status INTEGER, message_content TEXT, packed_info_data BLOB);")).unwrap();
    for i in 0..rows {
        db.execute(
            &format!("INSERT INTO {table} VALUES(?1,?2,1,100,0,'合法正文',NULL)"),
            [i as i64 + 1, 9007199254740993i64 + i as i64],
        )
        .unwrap();
    }
    db.execute_batch(&extra.replace("TABLE_NAME", &table))
        .unwrap();
    db.close().unwrap();
    let key_file = tmp.path().join("key.json");
    std::fs::write(
        &key_file,
        format!("{{\"raw_key\":\"{}\"}}", "93".repeat(32)),
    )
    .unwrap();
    std::fs::set_permissions(&key_file, std::fs::Permissions::from_mode(0o600)).unwrap();
    let out = Command::new(env!("CARGO_BIN_EXE_wx-cli"))
        .args([
            "archive-snapshot",
            "--source",
            tmp.path().join("source").to_str().unwrap(),
            "--out",
            tmp.path().join("snapshot").to_str().unwrap(),
            "--key-file",
            key_file.to_str().unwrap(),
        ])
        .output()
        .unwrap();
    assert!(out.status.success(), "synthetic snapshot failed");
    tmp
}

fn inspect(tmp: &TempDir) -> Output {
    Command::new(env!("CARGO_BIN_EXE_wx-cli"))
        .args([
            "archive-inspect",
            "--snapshots",
            tmp.path().join("snapshot").to_str().unwrap(),
            "--key-file",
            tmp.path().join("key.json").to_str().unwrap(),
            "--action",
            "export",
            "--talker",
            "bounds@chatroom",
            "--account",
            "bounds-account",
            "--archive-id",
            "bounds-archive",
            "--out",
            tmp.path().join("export").to_str().unwrap(),
            "--max-content-bytes",
            "1024",
            "--max-row-bytes",
            "1024",
            "--page-size",
            "7",
        ])
        .output()
        .unwrap()
}

#[test]
fn unknown_blob_cannot_bypass_raw_row_budget() {
    let tmp = fixture("ALTER TABLE TABLE_NAME ADD COLUMN unknown_blob BLOB; UPDATE TABLE_NAME SET unknown_blob=zeroblob(8192);", 1);
    let out = inspect(&tmp);
    assert!(
        !out.status.success(),
        "unknown oversized BLOB bypassed budget"
    );
    assert!(
        !tmp.path().join("export/export.json").exists(),
        "failed export published manifest"
    );
}

#[test]
fn sum_of_small_unknown_cells_cannot_bypass_raw_row_budget() {
    let mut extra = String::new();
    for i in 0..6 {
        extra.push_str(&format!("ALTER TABLE TABLE_NAME ADD COLUMN unknown_{i} BLOB; UPDATE TABLE_NAME SET unknown_{i}=zeroblob(400);"));
    }
    let tmp = fixture(&extra, 1);
    let out = inspect(&tmp);
    assert!(!out.status.success(), "wide raw row bypassed total budget");
    assert!(!tmp.path().join("export/export.json").exists());
}

#[test]
fn bounded_pagination_preserves_all_large_ids_and_bodies() {
    let tmp = fixture("", 43);
    let out = inspect(&tmp);
    assert!(
        out.status.success(),
        "{}",
        String::from_utf8_lossy(&out.stdout)
    );
    let text = std::fs::read_to_string(tmp.path().join("export/records.jsonl")).unwrap();
    let rows: Vec<Value> = text
        .lines()
        .map(|l| serde_json::from_str(l).unwrap())
        .collect();
    assert_eq!(rows.len(), 43);
    for (i, row) in rows.iter().enumerate() {
        assert_eq!(
            row["identity"]["server_id"],
            (9007199254740993i64 + i as i64).to_string()
        );
        assert_eq!(row["message_content"], "合法正文");
    }
}

#[test]
fn real_rust_capture_after_claim_protects_against_delayed_older_view() {
    let old = fixture(
        "ALTER TABLE TABLE_NAME ADD COLUMN real_sender_id INTEGER; INSERT INTO Name2Id VALUES ('bounds-sender'); UPDATE TABLE_NAME SET message_content='old synthetic state',real_sender_id=2;",
        1,
    );
    let newer = fixture(
        "ALTER TABLE TABLE_NAME ADD COLUMN real_sender_id INTEGER; INSERT INTO Name2Id VALUES ('bounds-sender'); UPDATE TABLE_NAME SET message_content='new synthetic state',real_sender_id=2;",
        1,
    );
    let root = std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
        .parent()
        .unwrap()
        .parent()
        .unwrap();
    let script = r#"
import sys, subprocess, shutil
from pathlib import Path
from archive.v3.guard import SyntheticGuard
from archive.v3.master import MasterStore
from archive.v3.endpoint import Endpoint
from archive.v3.collector import CaptureClient, Collector
from archive.v3.util import sha256_hex
binary, old, new = sys.argv[1:]
base=Path(old); nas=base/'nas'; nas.mkdir(mode=0o700)
SyntheticGuard.write_marker(nas, 'bounds-archive', 1, 0)
binding=SyntheticGuard('bounds-archive').verify(nas)
store=MasterStore.initialize(binding, 'bounds-archive', {'bounds-account': {'bounds@chatroom': {'capture': True}}})
ep=Endpoint(binding, 'bounds-archive'); ep.store=store
ep.auth={'roles': {'collector': {'token_sha256': sha256_hex(b'synthetic-token')}}}
class Transport:
    def request(self, frame): return ep.handle_frame(frame)
c=Collector(CaptureClient(Transport(), 'bounds-archive', 'synthetic-token'), base/'work')
def capture(source, name):
    source=Path(source); dest=base/name
    def runner(claim):
        snapshot=base/(name+'-fresh-snapshot')
        subprocess.run([binary,'archive-snapshot','--source',str(source/'source'),'--out',str(snapshot),'--key-file',str(source/'key.json')],check=True,capture_output=True)
        payload=dest/'payload'
        subprocess.run([binary,'archive-inspect','--snapshots',str(snapshot),'--key-file',str(source/'key.json'),'--action','export','--account','bounds-account','--talker','bounds@chatroom','--archive-id','bounds-archive','--out',str(payload)],check=True,capture_output=True)
        for item in payload.iterdir(): shutil.move(str(item), str(dest/item.name))
        payload.rmdir()
    c.claim_source_snapshot(dest, 'bounds-account','bounds@chatroom',source_runner=runner)
    return dest
try:
    old_view=capture(old,'old-claimed')
    new_view=capture(new,'new-claimed')
    c.collect_session(new_view)
    assert store.db.execute('SELECT sender_id FROM messages').fetchone()[0]=='bounds-sender', 'real Rust real_sender_name was lost before query sender policy'
    c.collect_session(old_view)
    assert store.db.execute('SELECT content_text FROM messages').fetchone()[0]=='new synthetic state', 'actual old Rust view overwrote newer committed capture'
    fresh=capture(old,'fresh-claimed')
    c.collect_session(fresh)
    assert store.db.execute('SELECT content_text FROM messages').fetchone()[0]=='old synthetic state', 'fresh legitimate claimed capture could not update'
finally:
    store.close(); binding.close()
"#;
    let output = Command::new("python3")
        .current_dir(root)
        .args([
            "-c",
            script,
            env!("CARGO_BIN_EXE_wx-cli"),
            old.path().to_str().unwrap(),
            newer.path().to_str().unwrap(),
        ])
        .output()
        .unwrap();
    assert!(
        output.status.success(),
        "real Rust/Python claim chain failed: {}",
        String::from_utf8_lossy(&output.stderr)
    );
}

#[test]
fn rejected_wide_row_does_not_materialize_all_unknown_cells() {
    let mut extra = String::new();
    for i in 0..16 {
        extra.push_str(&format!("ALTER TABLE TABLE_NAME ADD COLUMN wide_{i} BLOB; UPDATE TABLE_NAME SET wide_{i}=zeroblob(4194304);"));
    }
    let small = fixture("", 1);
    let wide = fixture(&extra, 1);
    fn peak(tmp: &TempDir) -> (i64, bool) {
        let script = r#"
import subprocess, resource, sys, json
from pathlib import Path
binary, path=sys.argv[1:]; p=Path(path)
r=subprocess.run([binary,'archive-inspect','--snapshots',str(p/'snapshot'),'--key-file',str(p/'key.json'),'--action','export','--talker','bounds@chatroom','--account','bounds-account','--archive-id','bounds-archive','--out',str(p/'export'),'--max-row-bytes','8388608','--max-content-bytes','1024'],capture_output=True)
peak=resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss
if sys.platform!='darwin': peak*=1024
print(json.dumps([peak,r.returncode==0]))
"#;
        let output = Command::new("python3")
            .args([
                "-c",
                script,
                env!("CARGO_BIN_EXE_wx-cli"),
                tmp.path().to_str().unwrap(),
            ])
            .output()
            .unwrap();
        assert!(output.status.success());
        serde_json::from_slice(&output.stdout).unwrap()
    }
    let (small_peak, small_ok) = peak(&small);
    let (wide_peak, wide_ok) = peak(&wide);
    assert!(small_ok);
    assert!(!wide_ok);
    assert!(wide_peak < small_peak + 32 * 1024 * 1024,
        "64MiB rejected row was materialized before 8MiB total-row check: small RSS={small_peak}, wide RSS={wide_peak}");
}

#[test]
fn explicit_python_cli_claims_before_real_rust_snapshot_and_collects() {
    let tmp = fixture("", 3);
    let script = r#"
import json,os,pathlib,subprocess,sys
root=pathlib.Path(sys.argv[1]); cli=sys.argv[2]
def private(name,obj):
 p=root/name;p.write_text(json.dumps(obj));p.chmod(0o600);return p
def run(cmd,*args):
 p=subprocess.run([sys.executable,'-m','archive.v3',cmd,*map(str,args)],capture_output=True,timeout=60)
 assert p.returncode==0,(cmd,p.stderr.decode())
 return json.loads(p.stdout)
master=root/'nas';master.mkdir(mode=0o700)
scope=private('scope.json',{'bounds-account':{'bounds@chatroom':{'capture':True,'query':True}}})
auth=private('raw-auth.json',{'roles':{'collector':{'token':'synthetic-cli-capture'}}})
token=private('client-token.json',{'token':'synthetic-cli-capture'})
base=['--root',master,'--archive-id','bounds-archive','--allow-synthetic-guard']
run('init',*base,'--scope',scope,'--auth-file',auth)
net=base+['--auth-file',master/'auth.json','--token-file',token,'--work-dir',root/'mac-work']
out=root/'cli-export'
result=run('snapshot',*net,'--source',root/'source','--source-key-file',root/'key.json',
 '--wx-cli',cli,'--account','bounds-account','--talker','bounds@chatroom','--out',out)
claim=json.loads((out/'snapshot-claim.json').read_text())
assert result['claim']['run_id']==claim['run_id']
assert (out/'export.json').exists() and (out/'records.jsonl').exists()
run('collect',*net,'--export',out)
state=run('status',*base)
assert state['messages']==3
from archive.v3.guard import SyntheticGuard
from archive.v3.master import MasterStore
binding=SyntheticGuard('bounds-archive').verify(master)
store=MasterStore(binding,'bounds-archive',readonly=True)
try:
 rows=store.db.execute('SELECT server_id,content_text FROM messages ORDER BY sort_seq').fetchall()
 assert [r['server_id'] for r in rows]==[str(9007199254740993+i) for i in range(3)]
 assert all(r['content_text']=='合法正文' for r in rows)
 runrow=store.get_run(claim['run_id'])
 assert runrow is not None and runrow['status']=='reconciled'
finally:store.close();binding.close()
"#;
    let out = Command::new("python3")
        .args([
            "-c",
            script,
            tmp.path().to_str().unwrap(),
            env!("CARGO_BIN_EXE_wx-cli"),
        ])
        .current_dir(std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("../.."))
        .output()
        .unwrap();
    assert!(
        out.status.success(),
        "CLI claim/snapshot/inspect/collect chain failed: {}",
        String::from_utf8_lossy(&out.stderr)
    );
}
