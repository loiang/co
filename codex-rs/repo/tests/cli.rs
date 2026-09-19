use codex_utils_cargo_bin::cargo_bin;
use pretty_assertions::assert_eq;
use serde_json::json;
use std::io::Write;
use std::process::Command;
use std::process::Stdio;
use tempfile::TempDir;

fn fixture() -> TempDir {
    let root = tempfile::tempdir().unwrap();
    assert!(
        Command::new("git")
            .arg("init")
            .arg(root.path())
            .output()
            .unwrap()
            .status
            .success()
    );
    std::fs::create_dir_all(root.path().join("scripts/co")).unwrap();
    std::fs::create_dir(root.path().join("nested")).unwrap();
    std::fs::write(
        root.path().join("scripts/co/cli.py"),
        r#"
import json, os, signal, sys
print(json.dumps(dict(argv=sys.argv[1:], cwd=os.getcwd(), stdin=sys.stdin.read())))
print("backend stderr", file=sys.stderr)
if os.environ.get("FIXTURE_SIGNAL"):
    os.kill(os.getpid(), signal.SIGTERM)
sys.exit(int(os.environ.get("FIXTURE_EXIT", "0")))
"#,
    )
    .unwrap();
    root
}

fn command(root: &TempDir) -> Command {
    let mut command = Command::new(cargo_bin("repo").unwrap());
    command.current_dir(root.path().join("nested"));
    command
        .env_remove("REPO_BACKEND_PATH")
        .env_remove("REPO_PYTHON");
    command
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    command
}

#[test]
fn forwards_all_commands_from_repository_root() {
    let root = fixture();
    let cases = [
        vec!["upgrade"],
        vec![
            "upgrade",
            "upstream/topic",
            "--ni-repo",
            "../host repo",
            "--cores",
            "2",
        ],
        vec![
            "upgrade-finalize",
            "--upstream-rev",
            "abc123",
            "--cores",
            "0",
        ],
        vec!["promote", "upgrade/candidate"],
        vec!["test"],
        vec!["build", "--cores", "4"],
        vec!["test-host", "--ni-repo", "../ni"],
        vec!["publish", "--dry-run"],
        vec!["install", "desktop"],
        vec![
            "install",
            "desktop",
            "v1.2.3",
            "--ni-repo",
            "../ni",
            "--dry-run",
        ],
    ];
    for args in cases {
        let output = command(&root).args(&args).output().unwrap();
        assert_eq!(output.status.code(), Some(0));
        let mut forwarded = vec![args[0], "--repo", "."];
        forwarded.extend_from_slice(&args[1..]);
        let mut actual: serde_json::Value = serde_json::from_slice(&output.stdout).unwrap();
        actual["cwd"] = json!(
            std::path::Path::new(actual["cwd"].as_str().unwrap())
                .canonicalize()
                .unwrap()
        );
        assert_eq!(
            actual,
            json!({"argv": forwarded, "cwd": root.path().canonicalize().unwrap(), "stdin": ""})
        );
        assert_eq!(
            String::from_utf8(output.stderr).unwrap().trim(),
            "backend stderr"
        );
    }
}

#[test]
fn propagates_stdin_and_failure_exit_status() {
    let root = fixture();
    let mut child = command(&root)
        .arg("test")
        .env("FIXTURE_EXIT", "37")
        .spawn()
        .unwrap();
    child
        .stdin
        .take()
        .unwrap()
        .write_all(b"input payload")
        .unwrap();
    let output = child.wait_with_output().unwrap();
    assert_eq!(output.status.code(), Some(37));
    let mut actual: serde_json::Value = serde_json::from_slice(&output.stdout).unwrap();
    actual["cwd"] = json!(
        std::path::Path::new(actual["cwd"].as_str().unwrap())
            .canonicalize()
            .unwrap()
    );
    assert_eq!(
        actual,
        json!({"argv": ["test", "--repo", "."], "cwd": root.path().canonicalize().unwrap(), "stdin": "input payload"})
    );
}

#[cfg(unix)]
#[test]
fn signal_termination_is_not_reported_as_success() {
    let root = fixture();
    let output = command(&root)
        .arg("test")
        .env("FIXTURE_SIGNAL", "1")
        .output()
        .unwrap();
    assert_eq!(output.status.code(), Some(143));
}

#[test]
fn supports_installed_backend_and_python_overrides() {
    let root = fixture();
    let backend = root.path().join("external backend.py");
    std::fs::rename(root.path().join("scripts/co/cli.py"), &backend).unwrap();
    let output = command(&root)
        .arg("test")
        .env("REPO_BACKEND_PATH", backend)
        .env("REPO_PYTHON", "python3")
        .output()
        .unwrap();
    assert_eq!(output.status.code(), Some(0));
}

#[test]
fn help_and_invalid_arguments_do_not_start_backend() {
    let root = fixture();
    let output = command(&root).arg("--help").output().unwrap();
    assert_eq!(output.status.code(), Some(0));
    assert!(
        String::from_utf8(output.stdout)
            .unwrap()
            .contains("upgrade-finalize")
    );
    for args in [
        vec![],
        vec!["co"],
        vec!["help"],
        vec!["upgrade-finalize"],
        vec!["promote"],
        vec!["install"],
        vec!["build", "--cores", "-1"],
        vec!["build", "--cores", "invalid"],
        vec!["test", "--repo", "."],
    ] {
        let output = command(&root).args(args).output().unwrap();
        assert_eq!(output.status.code(), Some(2));
        assert!(output.stdout.is_empty());
    }
}

#[test]
fn reports_repository_and_interpreter_failures() {
    let outside = tempfile::tempdir().unwrap();
    let output = Command::new(cargo_bin("repo").unwrap())
        .arg("test")
        .current_dir(outside.path())
        .output()
        .unwrap();
    assert_eq!(output.status.code(), Some(1));
    let root = fixture();
    let output = command(&root)
        .arg("test")
        .env("REPO_PYTHON", root.path().join("missing-python"))
        .output()
        .unwrap();
    assert_eq!(output.status.code(), Some(1));
}
