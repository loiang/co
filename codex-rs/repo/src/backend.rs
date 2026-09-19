use anyhow::Context;
use anyhow::Result;
use anyhow::bail;
use std::ffi::OsString;
use std::path::PathBuf;
use std::process::Command;
use std::process::ExitStatus;

pub(crate) fn run(args: &[OsString]) -> Result<i32> {
    let root = repository_root()?;
    // Installed wrappers can supply store paths without coupling root discovery to the binary.
    let backend = std::env::var_os("REPO_BACKEND_PATH")
        .map(PathBuf::from)
        .unwrap_or_else(|| root.join("scripts/co/cli.py"));
    let python = std::env::var_os("REPO_PYTHON").unwrap_or_else(|| "python3".into());
    let status = Command::new(python)
        .arg(backend)
        .arg(&args[0])
        .args(["--repo", "."])
        .args(&args[1..])
        .current_dir(root)
        .status()
        .context("failed to start Python lifecycle backend")?;
    Ok(exit_code(status))
}

fn repository_root() -> Result<PathBuf> {
    let output = Command::new("git")
        .args(["rev-parse", "--show-toplevel"])
        .output()
        .context("failed to locate repository with git")?;
    if !output.status.success() {
        bail!("current directory is not inside a Git repository");
    }
    let root = String::from_utf8(output.stdout).context("repository path is not UTF-8")?;
    Ok(PathBuf::from(root.trim_end_matches(['\r', '\n'])))
}

fn exit_code(status: ExitStatus) -> i32 {
    if let Some(code) = status.code() {
        return code;
    }
    #[cfg(unix)]
    {
        use std::os::unix::process::ExitStatusExt;
        status.signal().map_or(1, |signal| 128 + signal)
    }
    #[cfg(not(unix))]
    {
        1
    }
}
