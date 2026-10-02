"""Exercise release stamping with real Cargo while preserving the source checkout."""

import os
import subprocess
import sys
from pathlib import Path

import pytest
import tomllib

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from codex_package.versioned_source import versioned_workspace


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    root = tmp_path / "source"
    crate = root / "codex-rs" / "probe"
    (crate / "src").mkdir(parents=True)
    (root / "codex-rs/Cargo.toml").write_text(
        '[workspace]\nmembers = ["probe"]\nresolver = "2"\n'
        '[workspace.package]\nversion = "0.0.0"\n',
        encoding="utf-8",
    )
    (crate / "Cargo.toml").write_text(
        '[package]\nname = "version-probe"\nversion.workspace = true\n'
        'edition = "2021"\n',
        encoding="utf-8",
    )
    (crate / "src/main.rs").write_text(
        'fn main() { println!("{}|{}.{}.{}", env!("CARGO_PKG_VERSION"), '
        'env!("CARGO_PKG_VERSION_MAJOR"), env!("CARGO_PKG_VERSION_MINOR"), '
        'env!("CARGO_PKG_VERSION_PATCH")); }\n',
        encoding="utf-8",
    )
    subprocess.run(["git", "init", "--quiet", str(root)], check=True)
    subprocess.run(
        ["cargo", "generate-lockfile", "--offline"], cwd=crate.parent, check=True
    )
    subprocess.run(["git", "add", "."], cwd=root, check=True)
    return root


def test_compiled_package_and_components_follow_release_without_source_edits(
    workspace: Path, tmp_path: Path
) -> None:
    originals = {
        name: (workspace / "codex-rs" / name).read_bytes()
        for name in ("Cargo.toml", "Cargo.lock")
    }
    env = {**os.environ, "CARGO_TARGET_DIR": str(tmp_path / "target")}
    for version in ("0.159.3", "0.160.0"):
        with versioned_workspace(workspace, version, "cargo", env) as staged:
            result = subprocess.run(
                ["cargo", "run", "--locked", "--offline", "--quiet"],
                cwd=staged,
                env=env,
                check=True,
                text=True,
                capture_output=True,
            )
            assert result.stdout.strip() == f"{version}|{version}"
            lock = tomllib.loads((staged / "Cargo.lock").read_text())
            assert lock["package"][0]["version"] == version
        assert not staged.exists()
    assert {
        name: (workspace / "codex-rs" / name).read_bytes() for name in originals
    } == originals


def test_staging_preserves_worktree_edits_and_cleans_up_on_failure(
    workspace: Path,
) -> None:
    source = workspace / "codex-rs/probe/src/main.rs"
    source.write_text("// local change\n", encoding="utf-8")
    with (
        pytest.raises(RuntimeError, match="failed build"),
        versioned_workspace(workspace, "0.159.3", "cargo", dict(os.environ)) as staged,
    ):
        assert (staged / "probe/src/main.rs").read_bytes() == source.read_bytes()
        raise RuntimeError("failed build")
    assert not staged.exists()
    assert source.read_text() == "// local change\n"


def test_matching_workspace_version_uses_original_checkout(workspace: Path) -> None:
    with versioned_workspace(workspace, "0.0.0", "cargo", dict(os.environ)) as staged:
        assert staged == workspace / "codex-rs"


def test_lock_refresh_failure_preserves_original_files(workspace: Path) -> None:
    manifest = workspace / "codex-rs/Cargo.toml"
    lock = workspace / "codex-rs/Cargo.lock"
    original = (manifest.read_bytes(), lock.read_bytes())
    with (
        pytest.raises(FileNotFoundError),
        versioned_workspace(
            workspace, "0.159.3", str(workspace / "missing-cargo"), dict(os.environ)
        ),
    ):
        pytest.fail("A failed Cargo lock refresh must prevent the build")
    assert (manifest.read_bytes(), lock.read_bytes()) == original
