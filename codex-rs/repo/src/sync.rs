use crate::backend;
use crate::backend_metadata::{self, BackendMetadata};
use anyhow::{Context, Result, bail};
use std::fs;
use std::path::{Path, PathBuf};
use std::process::Command;
use std::time::{SystemTime, UNIX_EPOCH};

const BINARY: &str = "co";

pub(crate) fn execute() -> Result<i32> {
    let root = source_root_for_sync(&std::env::current_exe()?)?;
    let home = std::env::var_os("HOME").context("HOME 未设置")?;
    execute_with(&root, Path::new(&home), &SystemRunner)
}

pub(crate) fn source_root_for_sync(executable: &Path) -> Result<PathBuf> {
    match backend::source_root_for_executable(executable)? {
        Some(root) => Ok(root),
        None => backend::repository_root(),
    }
}

pub(crate) trait CommandRunner {
    fn run(&self, args: &[String], cwd: &Path) -> Result<i32>;
}

struct SystemRunner;

impl CommandRunner for SystemRunner {
    fn run(&self, args: &[String], cwd: &Path) -> Result<i32> {
        let (program, arguments) = args.split_first().context("empty build command")?;
        let status = Command::new(program)
            .args(arguments)
            .current_dir(cwd)
            .status()
            .context("failed to start cargo build")?;
        Ok(status.code().unwrap_or(1))
    }
}

pub(crate) fn execute_with(root: &Path, home: &Path, runner: &dyn CommandRunner) -> Result<i32> {
    let paths = SyncPaths::new(root, home);
    let code = runner.run(&build_command(&paths.manifest), root)?;
    if code != 0 {
        return Ok(code);
    }
    install(&paths)?;
    Ok(0)
}

struct SyncPaths {
    root: PathBuf,
    manifest: PathBuf,
    binary: PathBuf,
    destination: PathBuf,
    generations: PathBuf,
    active: PathBuf,
}

impl SyncPaths {
    fn new(root: &Path, home: &Path) -> Self {
        let state = home.join(".local/share/co");
        Self {
            root: root.to_path_buf(),
            manifest: root.join("codex-rs/Cargo.toml"),
            binary: root.join("codex-rs/target/release/co"),
            destination: home.join(".local/bin/co"),
            generations: state.join("generations"),
            active: state.join("active"),
        }
    }
}

fn build_command(manifest: &Path) -> Vec<String> {
    vec![
        "cargo".into(),
        "build".into(),
        "--release".into(),
        "--locked".into(),
        "--manifest-path".into(),
        manifest.display().to_string(),
        "-p".into(),
        "codex-repo".into(),
        "--bin".into(),
        BINARY.into(),
    ]
}

fn install(paths: &SyncPaths) -> Result<()> {
    let metadata = BackendMetadata::from_source(&paths.root)?;
    if !paths.binary.is_file() {
        bail!("release binary 不存在: {}", paths.binary.display());
    }
    fs::create_dir_all(&paths.generations)?;
    validate_active(&paths.active, &paths.generations)?;
    validate_destination(&paths.destination, &paths.active)?;
    if active_matches(paths, &metadata)? {
        ensure_destination_link(&paths.destination, &paths.active)?;
        println!(
            "co is already synchronized at {}",
            paths.destination.display()
        );
        return Ok(());
    }

    let generation = paths.generations.join(generation_name());
    let staging = paths
        .generations
        .join(format!(".staging-{}", std::process::id()));
    if staging.exists() || generation.exists() {
        bail!("sync staging or generation already exists");
    }
    fs::create_dir(&staging)?;
    let result = (|| {
        let staged_binary = staging.join(BINARY);
        fs::copy(&paths.binary, &staged_binary)?;
        make_executable(&staged_binary)?;
        backend_metadata::write(&staging.join(backend_metadata::FILE_NAME), &metadata)?;
        fs::rename(&staging, &generation)?;
        let installed_link = ensure_destination_link(&paths.destination, &paths.active)?;
        if let Err(error) = activate(&generation, &paths.active, &paths.generations) {
            if installed_link {
                let _ = fs::remove_file(&paths.destination);
            }
            return Err(error);
        }
        println!("synchronized co at {}", paths.destination.display());
        Ok(())
    })();
    if result.is_err() {
        let _ = fs::remove_dir_all(&staging);
        let _ = fs::remove_dir_all(&generation);
    }
    result
}

fn active_matches(paths: &SyncPaths, expected: &BackendMetadata) -> Result<bool> {
    let target = match fs::read_link(&paths.active) {
        Ok(target) => resolve_link(&paths.active, &target),
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(false),
        Err(error) => return Err(error.into()),
    };
    let current = backend_metadata::read(&target.join(backend_metadata::FILE_NAME))?;
    if &current != expected {
        return Ok(false);
    }
    Ok(fs::read(target.join(BINARY))? == fs::read(&paths.binary)?)
}

fn validate_active(active: &Path, generations: &Path) -> Result<()> {
    match fs::symlink_metadata(active) {
        Ok(metadata) if !metadata.file_type().is_symlink() => {
            bail!("active 路径不是 symlink，拒绝覆盖: {}", active.display())
        }
        Ok(_) => {
            let target = resolve_link(active, &fs::read_link(active)?);
            let target = fs::canonicalize(target)?;
            let generations = fs::canonicalize(generations)?;
            if !target.starts_with(generations) {
                bail!(
                    "active symlink 指向非 co generation，拒绝覆盖: {}",
                    active.display()
                );
            }
            Ok(())
        }
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(()),
        Err(error) => Err(error.into()),
    }
}

fn validate_destination(destination: &Path, active: &Path) -> Result<()> {
    match fs::symlink_metadata(destination) {
        Ok(metadata) if metadata.file_type().is_symlink() => {
            let expected = active.join(BINARY);
            if fs::read_link(destination)? != expected {
                bail!("安装路径指向其他目标，拒绝覆盖: {}", destination.display());
            }
            Ok(())
        }
        Ok(_) => bail!("安装路径不是 symlink，拒绝覆盖: {}", destination.display()),
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(()),
        Err(error) => Err(error.into()),
    }
}

fn ensure_destination_link(destination: &Path, active: &Path) -> Result<bool> {
    let expected = active.join(BINARY);
    match fs::symlink_metadata(destination) {
        Ok(metadata) if metadata.file_type().is_symlink() => {
            if fs::read_link(destination)? != expected {
                bail!("安装路径指向其他目标，拒绝覆盖: {}", destination.display());
            }
            Ok(false)
        }
        Ok(_) => bail!("安装路径不是 symlink，拒绝覆盖: {}", destination.display()),
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
            atomic_symlink(&expected, destination)?;
            Ok(true)
        }
        Err(error) => Err(error.into()),
    }
}

fn activate(generation: &Path, active: &Path, generations: &Path) -> Result<()> {
    validate_active(active, generations)?;
    atomic_symlink(generation, active)
}

fn atomic_symlink(target: &Path, destination: &Path) -> Result<()> {
    let parent = destination.parent().context("symlink 缺少父目录")?;
    fs::create_dir_all(parent)?;
    let temporary = parent.join(format!(".co-link-{}-{}", std::process::id(), timestamp()));
    if temporary.exists() {
        bail!("symlink 临时路径已存在: {}", temporary.display());
    }
    std::os::unix::fs::symlink(target, &temporary)?;
    fs::rename(&temporary, destination).with_context(|| {
        let _ = fs::remove_file(&temporary);
        format!("无法原子更新 {}", destination.display())
    })
}

fn resolve_link(link: &Path, target: &Path) -> PathBuf {
    if target.is_absolute() {
        target.to_path_buf()
    } else {
        link.parent().unwrap_or_else(|| Path::new(".")).join(target)
    }
}

fn generation_name() -> String {
    format!("generation-{}-{}", std::process::id(), timestamp())
}

fn timestamp() -> u128 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|duration| duration.as_nanos())
        .unwrap_or_default()
}

#[cfg(unix)]
fn make_executable(path: &Path) -> Result<()> {
    use std::os::unix::fs::PermissionsExt;
    fs::set_permissions(path, fs::Permissions::from_mode(0o755))?;
    Ok(())
}

#[cfg(not(unix))]
compile_error!("co sync 需要 Unix symlink 支持");

#[cfg(test)]
mod tests {
    use super::{CommandRunner, SyncPaths, build_command, execute_with, install};
    use std::cell::RefCell;
    use std::fs;
    use std::path::{Path, PathBuf};
    use tempfile::tempdir;

    struct FakeRunner {
        code: i32,
        calls: RefCell<Vec<(Vec<String>, PathBuf)>>,
    }

    impl CommandRunner for FakeRunner {
        fn run(&self, args: &[String], cwd: &Path) -> anyhow::Result<i32> {
            self.calls
                .borrow_mut()
                .push((args.to_vec(), cwd.to_path_buf()));
            Ok(self.code)
        }
    }

    fn fixture() -> (tempfile::TempDir, SyncPaths) {
        let temp = tempdir().unwrap();
        let root = temp.path().join("co");
        let home = temp.path().join("home");
        fs::create_dir_all(root.join("scripts/co")).unwrap();
        fs::create_dir_all(root.join("codex-rs/target/release")).unwrap();
        fs::write(root.join("scripts/co/cli.py"), "print('co')\n").unwrap();
        fs::write(root.join("codex-rs/target/release/co"), "binary\n").unwrap();
        (temp, SyncPaths::new(&root, &home))
    }

    #[test]
    fn build_command_is_locked_and_scoped_to_governance_binary() {
        let command = build_command(Path::new("/repo/co/codex-rs/Cargo.toml"));
        assert_eq!(
            command,
            vec![
                "cargo",
                "build",
                "--release",
                "--locked",
                "--manifest-path",
                "/repo/co/codex-rs/Cargo.toml",
                "-p",
                "codex-repo",
                "--bin",
                "co"
            ]
        );
    }

    #[test]
    fn nonzero_build_preserves_environment() {
        let (_temp, paths) = fixture();
        let runner = FakeRunner {
            code: 17,
            calls: RefCell::new(Vec::new()),
        };
        assert_eq!(
            execute_with(
                &paths.root,
                paths
                    .destination
                    .parent()
                    .and_then(Path::parent)
                    .and_then(Path::parent)
                    .unwrap(),
                &runner,
            )
            .unwrap(),
            17
        );
        assert!(!paths.generations.exists());
        assert_eq!(runner.calls.borrow().len(), 1);
    }

    #[test]
    fn foreign_destination_is_rejected_without_replacement() {
        let (_temp, paths) = fixture();
        fs::create_dir_all(paths.destination.parent().unwrap()).unwrap();
        fs::write(&paths.destination, "foreign").unwrap();
        assert!(install(&paths).is_err());
        assert_eq!(fs::read_to_string(paths.destination).unwrap(), "foreign");
    }

    #[cfg(unix)]
    #[test]
    fn foreign_destination_symlink_is_rejected_without_replacement() {
        let (_temp, paths) = fixture();
        fs::create_dir_all(paths.destination.parent().unwrap()).unwrap();
        let foreign = paths.destination.parent().unwrap().join("foreign");
        std::os::unix::fs::symlink(&foreign, &paths.destination).unwrap();
        assert!(install(&paths).is_err());
        assert_eq!(fs::read_link(paths.destination).unwrap(), foreign);
    }

    #[test]
    fn non_symlink_active_is_rejected_before_destination_creation() {
        let (_temp, paths) = fixture();
        fs::create_dir_all(paths.active.parent().unwrap()).unwrap();
        fs::write(&paths.active, "foreign").unwrap();
        assert!(install(&paths).is_err());
        assert!(!paths.destination.exists());
        assert_eq!(fs::read_to_string(paths.active).unwrap(), "foreign");
    }

    #[test]
    fn activation_failure_rolls_back_new_destination_link() {
        let (_temp, paths) = fixture();
        fs::create_dir_all(&paths.generations).unwrap();
        let installed_link =
            super::ensure_destination_link(&paths.destination, &paths.active).unwrap();
        assert!(installed_link);
        fs::write(&paths.active, "foreign").unwrap();
        let result = super::activate(
            &paths.generations.join("new"),
            &paths.active,
            &paths.generations,
        );
        if result.is_err() && installed_link {
            fs::remove_file(&paths.destination).unwrap();
        }
        assert!(result.is_err());
        assert!(!paths.destination.exists());
        assert_eq!(fs::read_to_string(paths.active).unwrap(), "foreign");
    }

    #[test]
    fn installed_executable_source_root_wins_outside_checkout() {
        let (_temp, paths) = fixture();
        let generation = paths.generations.join("generation-installed");
        fs::create_dir_all(&generation).unwrap();
        fs::copy(&paths.binary, generation.join("co")).unwrap();
        let metadata = super::BackendMetadata::from_source(&paths.root).unwrap();
        super::backend_metadata::write(
            &generation.join(super::backend_metadata::FILE_NAME),
            &metadata,
        )
        .unwrap();
        let executable = generation.join("co");
        assert_eq!(
            super::source_root_for_sync(&executable).unwrap(),
            paths.root.canonicalize().unwrap()
        );
    }

    #[test]
    fn repeated_install_from_same_source_is_idempotent() {
        let (_temp, paths) = fixture();
        install(&paths).unwrap();
        let active = fs::read_link(&paths.active).unwrap();
        install(&paths).unwrap();
        assert_eq!(fs::read_link(&paths.active).unwrap(), active);
        let generations = fs::read_dir(&paths.generations)
            .unwrap()
            .filter_map(Result::ok)
            .filter(|entry| {
                entry
                    .file_name()
                    .to_string_lossy()
                    .starts_with("generation-")
            })
            .count();
        assert_eq!(generations, 1);
    }
}
