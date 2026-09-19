use crate::backend_metadata;
use anyhow::Context;
use anyhow::Result;
use anyhow::bail;
use std::ffi::OsString;
use std::fs;
use std::path::PathBuf;
use std::process::Command;
use std::process::ExitStatus;

pub(crate) fn run(args: &[OsString]) -> Result<i32> {
    let resolution = resolve_backend()?;
    let python = std::env::var_os("REPO_PYTHON").unwrap_or_else(|| "python3".into());
    let status = Command::new(python)
        .arg(resolution.backend)
        .arg(&args[0])
        .args(["--repo", "."])
        .args(&args[1..])
        .current_dir(resolution.source_root)
        .status()
        .context("failed to start Python lifecycle backend")?;
    Ok(exit_code(status))
}

struct BackendResolution {
    backend: PathBuf,
    source_root: PathBuf,
}

fn resolve_backend() -> Result<BackendResolution> {
    if let Some(backend) = std::env::var_os("REPO_BACKEND_PATH") {
        return Ok(BackendResolution {
            backend: backend.into(),
            source_root: repository_root()?,
        });
    }
    if let Some(resolution) = resolve_backend_for_executable(&std::env::current_exe()?)? {
        return Ok(resolution);
    }
    let source_root = repository_root()?;
    Ok(BackendResolution {
        backend: source_root.join("scripts/co/cli.py"),
        source_root,
    })
}

fn resolve_backend_for_executable(
    executable: &std::path::Path,
) -> Result<Option<BackendResolution>> {
    let executable = fs::canonicalize(executable)
        .with_context(|| format!("failed to resolve executable: {}", executable.display()))?;
    let metadata_path = executable
        .parent()
        .context("resolved executable has no parent")?
        .join(backend_metadata::FILE_NAME);
    match fs::symlink_metadata(&metadata_path) {
        Ok(_) => {
            let metadata = backend_metadata::read(&metadata_path)?;
            Ok(Some(BackendResolution {
                backend: metadata.backend_path,
                source_root: metadata.source_root,
            }))
        }
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(None),
        Err(error) => Err(error.into()),
    }
}

pub(crate) fn source_root_for_executable(executable: &std::path::Path) -> Result<Option<PathBuf>> {
    Ok(resolve_backend_for_executable(executable)?.map(|resolution| resolution.source_root))
}

pub(crate) fn repository_root() -> Result<PathBuf> {
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

#[cfg(test)]
mod tests {
    use super::resolve_backend_for_executable;
    use crate::backend_metadata::BackendMetadata;
    use std::fs;
    use tempfile::tempdir;

    #[test]
    fn resolves_installed_backend_outside_source_checkout() {
        let temp = tempdir().unwrap();
        let source = temp.path().join("source");
        let generation = temp.path().join("generation");
        fs::create_dir_all(source.join("scripts/co")).unwrap();
        fs::create_dir_all(&generation).unwrap();
        let backend = source.join("scripts/co/cli.py");
        let executable = generation.join("co");
        let launcher = temp.path().join("bin/co");
        fs::write(&backend, "print('ok')\n").unwrap();
        fs::write(&executable, "binary\n").unwrap();
        fs::create_dir_all(launcher.parent().unwrap()).unwrap();
        std::os::unix::fs::symlink(&executable, &launcher).unwrap();
        let metadata = BackendMetadata::from_source(&source).unwrap();
        crate::backend_metadata::write(
            &generation.join(crate::backend_metadata::FILE_NAME),
            &metadata,
        )
        .unwrap();

        let resolution = resolve_backend_for_executable(&launcher).unwrap().unwrap();
        assert_eq!(resolution.source_root, source.canonicalize().unwrap());
        assert_eq!(resolution.backend, backend.canonicalize().unwrap());
    }

    #[test]
    fn missing_generation_metadata_falls_back_to_repository_lookup() {
        let temp = tempdir().unwrap();
        let executable = temp.path().join("co");
        fs::write(&executable, "binary\n").unwrap();
        assert!(
            resolve_backend_for_executable(&executable)
                .unwrap()
                .is_none()
        );
    }
}
