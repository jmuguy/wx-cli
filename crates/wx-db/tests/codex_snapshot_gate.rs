//! Independent acceptance regressions for snapshot publication.
use std::os::unix::fs::PermissionsExt;
use wx_db::snapshot::publish_snapshot;

#[test]
fn publication_removes_group_and_other_read_access() {
    let root = tempfile::tempdir().unwrap();
    let real_root = root.path().canonicalize().unwrap();
    let candidate = real_root.join("candidate.db");
    let published = real_root.join("published.db");
    std::fs::write(&candidate, b"synthetic encrypted candidate").unwrap();
    std::fs::set_permissions(&candidate, std::fs::Permissions::from_mode(0o644)).unwrap();
    publish_snapshot(&candidate, &published).unwrap();
    let mode = std::fs::metadata(published).unwrap().permissions().mode();
    assert_eq!(mode & 0o077, 0, "published snapshot must remain private");
}

#[test]
fn publication_cannot_replace_an_existing_generation() {
    let root = tempfile::tempdir().unwrap();
    let real_root = root.path().canonicalize().unwrap();
    let candidate = real_root.join("candidate.db");
    let published = real_root.join("published.db");
    std::fs::write(&candidate, b"new generation").unwrap();
    std::fs::write(&published, b"already published generation").unwrap();
    assert!(publish_snapshot(&candidate, &published).is_err());
    assert_eq!(std::fs::read(published).unwrap(), b"already published generation");
}
