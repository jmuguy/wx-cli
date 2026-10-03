use std::process::Command;

fn bin() -> &'static str {
    env!("CARGO_BIN_EXE_wx-cli")
}

#[test]
fn paths_json_outputs_valid_json_with_expected_fields() {
    let output = Command::new(bin())
        .args(["paths", "--json"])
        .output()
        .expect("run paths --json");
    assert!(output.status.success(), "paths --json failed: {output:?}");

    let json: serde_json::Value =
        serde_json::from_slice(&output.stdout).expect("valid JSON from paths --json");
    let obj = json.as_object().expect("JSON is an object");

    let expected_fields = [
        "platform",
        "config_dir",
        "keys_file",
        "settings_file",
        "cache_root",
        "state_root",
        "logs_dir",
        "server_state_dir",
        "server_stdout_log",
        "server_stderr_log",
        "temp_root",
    ];
    for field in &expected_fields {
        assert!(
            obj.contains_key(*field),
            "missing field '{field}' in paths --json output"
        );
    }
}
