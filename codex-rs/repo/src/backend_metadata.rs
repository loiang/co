use anyhow::{Context, Result, bail};
use serde::{Deserialize, Serialize};
use std::fs;
use std::path::{Path, PathBuf};

pub(crate) const FILE_NAME: &str = "co-backend.json";

#[derive(Debug, Deserialize, Serialize, PartialEq, Eq)]
pub(crate) struct BackendMetadata {
    pub(crate) schema_version: u8,
    pub(crate) source_root: PathBuf,
    pub(crate) backend_path: PathBuf,
}

impl BackendMetadata {
    pub(crate) fn from_source(source_root: &Path) -> Result<Self> {
        let source_root = canonical_directory(source_root, "source root")?;
        let backend_path =
            canonical_regular_file(&source_root.join("scripts/co/cli.py"), "backend")?;
        Ok(Self {
            schema_version: 1,
            source_root,
            backend_path,
        })
    }
}

pub(crate) fn read(path: &Path) -> Result<BackendMetadata> {
    let metadata_path = canonical_regular_file(path, "backend metadata")?;
    let contents = fs::read_to_string(&metadata_path)
        .with_context(|| format!("failed to read {}", metadata_path.display()))?;
    let metadata: BackendMetadata = serde_json::from_str(&contents)
        .with_context(|| format!("invalid backend metadata: {}", metadata_path.display()))?;
    validate(&metadata)
}

pub(crate) fn write(path: &Path, metadata: &BackendMetadata) -> Result<()> {
    validate(metadata)?;
    let contents =
        serde_json::to_vec_pretty(metadata).context("failed to serialize backend metadata")?;
    fs::write(path, [contents.as_slice(), b"\n"].concat())
        .with_context(|| format!("failed to write {}", path.display()))?;
    Ok(())
}

pub(crate) fn validate(metadata: &BackendMetadata) -> Result<BackendMetadata> {
    if metadata.schema_version != 1 {
        bail!(
            "unsupported backend metadata schema: {}",
            metadata.schema_version
        );
    }
    let source_root = canonical_directory(&metadata.source_root, "metadata source root")?;
    let backend_path = canonical_regular_file(&metadata.backend_path, "metadata backend")?;
    if !backend_path.starts_with(&source_root) {
        bail!(
            "metadata backend is outside source root: {}",
            backend_path.display()
        );
    }
    Ok(BackendMetadata {
        schema_version: metadata.schema_version,
        source_root,
        backend_path,
    })
}

pub(crate) fn canonical_directory(path: &Path, label: &str) -> Result<PathBuf> {
    if !path.is_absolute() {
        bail!("{label} path must be absolute: {}", path.display());
    }
    let canonical = fs::canonicalize(path)
        .with_context(|| format!("failed to resolve {label}: {}", path.display()))?;
    if !canonical.is_dir() {
        bail!("{label} is not a directory: {}", canonical.display());
    }
    Ok(canonical)
}

pub(crate) fn canonical_regular_file(path: &Path, label: &str) -> Result<PathBuf> {
    if !path.is_absolute() {
        bail!("{label} path must be absolute: {}", path.display());
    }
    let canonical = fs::canonicalize(path)
        .with_context(|| format!("failed to resolve {label}: {}", path.display()))?;
    if !canonical.is_file() {
        bail!("{label} is not a regular file: {}", canonical.display());
    }
    Ok(canonical)
}
