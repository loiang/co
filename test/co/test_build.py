"""Native package build integration tests using the official layout and validator."""

import io
import json
import subprocess
import sys
import tarfile
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).parents[2]
sys.path.insert(0, str(ROOT / "scripts" / "co"))

from build import _official_version, build  # noqa: E402
from common import LifecycleError, git, run, sha256  # noqa: E402


@pytest.fixture
def source_repo(tmp_path: Path) -> Path:
    """Keep source identity real while isolating every generated artifact."""
    (tmp_path / "scripts").symlink_to(ROOT / "scripts", target_is_directory=True)
    (tmp_path / "codex-rs").mkdir()
    (tmp_path / "codex-rs/Cargo.toml").write_text(
        '[workspace.package]\nversion = "0.0.0"\n', encoding="utf-8"
    )
    (tmp_path / ".co").mkdir()
    (tmp_path / ".co/upstream-rev").write_text("a" * 40, encoding="utf-8")
    (tmp_path / "flake.lock").write_text("{}", encoding="utf-8")
    (tmp_path / ".gitignore").write_text(".states/\n", encoding="utf-8")
    git(tmp_path, "init", "--quiet")
    git(tmp_path, "config", "user.name", "co-test")
    git(tmp_path, "config", "user.email", "co-test@localhost")
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "--quiet", "-m", "native build fixture")
    return tmp_path


def test_official_version_resolves_latest_stable_rust_release() -> None:
    metadata = io.BytesIO(
        b'{"tag_name":"rust-v0.154.0","draft":false,"prerelease":false}'
    )
    with patch("build.urlopen", return_value=metadata) as urlopen:
        assert _official_version() == "0.154.0"
    assert urlopen.call_args.args[0].full_url == (
        "https://api.github.com/repos/openai/codex/releases/latest"
    )


@pytest.mark.parametrize(
    "metadata",
    [
        b'{"tag_name":"rust-v0.154.0-alpha.1","draft":false,"prerelease":true}',
        b'{"tag_name":"v0.154.0","draft":false,"prerelease":false}',
        b'{"tag_name":"rust-v0.154.0","draft":true,"prerelease":false}',
        b"[]",
        b"not JSON",
    ],
)
def test_official_version_rejects_invalid_release(metadata: bytes) -> None:
    with (
        patch("build.urlopen", return_value=io.BytesIO(metadata)),
        pytest.raises(LifecycleError, match="stable release"),
    ):
        _official_version()


def _run_with_prebuilt(
    command: list[str], kwargs: dict[str, object], binary: Path
) -> subprocess.CompletedProcess[str]:
    command = [*command]
    for flag in (
        "--entrypoint-bin",
        "--code-mode-host-bin",
        "--bwrap-bin",
        "--rg-bin",
        "--zsh-bin",
    ):
        command.extend([flag, str(binary)])
    return run(command, **kwargs)


@pytest.mark.parametrize("cores", [0, 4])
def test_build_uses_official_package_and_emits_bound_assets(
    source_repo: Path, cores: int
) -> None:
    """Replace only source compilation; execute upstream packaging and validation."""
    binary = source_repo / ".states/prebuilt"
    binary.parent.mkdir()
    binary.write_bytes(b"native package executable fixture\n")
    binary.chmod(0o755)
    calls = []

    def capture(
        command: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        assert "nix" not in command
        calls.append((command, kwargs))
        if command[0] == "rustc":
            return subprocess.CompletedProcess(
                command, 0, "host: x86_64-unknown-linux-gnu\n", ""
            )
        if command[1].endswith("build_codex_package.py"):
            return _run_with_prebuilt(command, kwargs, binary)
        return run(command, **kwargs)

    with (
        patch("native_package.run", side_effect=capture),
        patch(
            "build.urlopen",
            return_value=io.BytesIO(
                b'{"tag_name":"rust-v0.154.0","draft":false,"prerelease":false}'
            ),
        ),
    ):
        latest = build(source_repo, cores)

    record = json.loads(latest.read_text())
    assert record["schemaVersion"] == 2
    assert "storePath" not in record
    assert record["dirty"] is False
    assert record["flakeLockSha256"] == sha256(source_repo / "flake.lock")
    assert record["sourceRev"] == git(source_repo, "rev-parse", "HEAD")
    package_dir = source_repo / record["packageDir"]
    metadata = json.loads((package_dir / "codex-package.json").read_text())
    assert record["package"] == metadata
    assert metadata["version"] == "0.154.0"
    assert metadata["variant"] == "codex"
    assert metadata["target"] == "x86_64-unknown-linux-gnu"
    assert metadata["entrypoint"] == "bin/codex"
    archive, manifest_path, sums = [source_repo / name for name in record["assets"]]
    with tarfile.open(archive) as bundle:
        members = {member.name for member in bundle.getmembers() if member.isfile()}
    assert members == {
        "bin/codex",
        "bin/codex-code-mode-host",
        "codex-package.json",
        "codex-path/rg",
        "codex-resources/bwrap",
        "codex-resources/zsh/bin/zsh",
    }
    manifest = json.loads(manifest_path.read_text())
    assert manifest["schemaVersion"] == 2
    assert manifest["package"] == metadata
    assert manifest["checksums"] == {
        "bin/codex": sha256(package_dir / "bin/codex"),
        archive.name: sha256(archive),
    }
    assert sums.read_text() == (
        f"{sha256(archive)}  {archive.name}\n"
        f"{sha256(manifest_path)}  {manifest_path.name}\n"
    )
    builder_command, builder_kwargs = next(
        item for item in calls if item[0][1].endswith("build_codex_package.py")
    )
    assert builder_command[2:] == [
        "--variant",
        "codex",
        "--target",
        metadata["target"],
        "--package-version",
        "0.154.0",
        "--package-dir",
        str(package_dir),
        "--cargo-profile",
        "release",
        "--archive-output",
        str(archive),
    ]
    assert builder_kwargs["env"]["CODEX_REPO_ROOT"] == str(source_repo)
    if cores:
        assert builder_kwargs["env"]["CARGO_BUILD_JOBS"] == str(cores)
    else:
        assert "CARGO_BUILD_JOBS" not in builder_kwargs["env"]
    assert any("validate_package_dir" in " ".join(command) for command, _ in calls)


def test_build_rejects_negative_core_limit_before_commands(source_repo: Path) -> None:
    with (
        patch("native_package.run") as command,
        pytest.raises(LifecycleError, match="cores"),
    ):
        build(source_repo, cores=-1)
    command.assert_not_called()


def test_failed_builder_never_writes_latest(source_repo: Path) -> None:
    def reject(
        command: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        if command[1].endswith("build_codex_package.py"):
            raise LifecycleError("source cargo failed")
        return run(command, **kwargs)

    with (
        patch("native_package.run", side_effect=reject),
        patch("build._official_version", return_value="0.154.0"),
        pytest.raises(LifecycleError, match="source cargo failed"),
    ):
        build(source_repo)
    assert not (source_repo / ".states/co/build/latest.json").exists()


@pytest.mark.parametrize(
    ("target", "expected_platform"),
    [
        ("x86_64-unknown-linux-gnu", "x86_64-linux"),
        ("aarch64-unknown-linux-gnu", "aarch64-linux"),
        ("aarch64-apple-darwin", "aarch64-darwin"),
        ("x86_64-pc-windows-msvc", "x86_64-windows"),
    ],
)
def test_host_target_uses_rustc_host_and_official_support_table(
    source_repo: Path, target: str, expected_platform: str
) -> None:
    from native_package import PackageRequest, _environment, _host_target

    def capture(
        command: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        if command[0] == "rustc":
            return subprocess.CompletedProcess(command, 0, f"host: {target}\n", "")
        return run(command, **kwargs)

    env = _environment(PackageRequest(source_repo, source_repo, "a" * 40, "0.154.0"))
    with patch("native_package.run", side_effect=capture):
        assert _host_target(source_repo, env) == (target, expected_platform)


@pytest.mark.parametrize(
    "host",
    ["", "host: \n", "host: a\nhost: b\n", "host: riscv64gc-unknown-linux-gnu\n"],
)
def test_invalid_or_unsupported_host_fails_before_build(
    source_repo: Path, host: str
) -> None:
    def capture(
        command: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        assert not command[1].endswith("build_codex_package.py")
        if command[0] == "rustc":
            return subprocess.CompletedProcess(command, 0, host, "")
        return run(command, **kwargs)

    with (
        patch("native_package.run", side_effect=capture),
        patch("build._official_version", return_value="0.154.0"),
        pytest.raises(LifecycleError),
    ):
        build(source_repo)
    assert not (source_repo / ".states/co/build/latest.json").exists()


@pytest.mark.parametrize(
    ("mutation", "error"),
    [
        ("missing-entrypoint", "Missing package file: bin/codex"),
        ("wrong-entrypoint", "Invalid package metadata field 'entrypoint'"),
        ("wrong-version", "package version"),
        ("missing-archive", "未生成 package archive"),
    ],
)
def test_invalid_builder_output_never_emits_evidence(
    source_repo: Path, mutation: str, error: str
) -> None:
    """Run the actual upstream validator against corrupted builder output."""
    binary = source_repo / ".states/prebuilt"
    binary.parent.mkdir()
    binary.write_bytes(b"native package executable fixture\n")
    binary.chmod(0o755)

    def capture(
        command: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        if command[0] == "rustc":
            return subprocess.CompletedProcess(
                command, 0, "host: x86_64-unknown-linux-gnu\n", ""
            )
        if not command[1].endswith("build_codex_package.py"):
            return run(command, **kwargs)
        result = _run_with_prebuilt(command, kwargs, binary)
        package_dir = Path(command[command.index("--package-dir") + 1])
        metadata_path = package_dir / "codex-package.json"
        metadata = json.loads(metadata_path.read_text())
        if mutation == "missing-entrypoint":
            (package_dir / "bin/codex").unlink()
        elif mutation == "wrong-entrypoint":
            metadata["entrypoint"] = "../codex"
        elif mutation == "wrong-version":
            metadata["version"] = "0.0.0"
        elif mutation == "missing-archive":
            Path(command[command.index("--archive-output") + 1]).unlink()
        metadata_path.write_text(json.dumps(metadata))
        return result

    with (
        patch("native_package.run", side_effect=capture),
        patch("build._official_version", return_value="0.154.0"),
        pytest.raises(LifecycleError, match=error),
    ):
        build(source_repo)
    assert not (source_repo / ".states/co/build/latest.json").exists()
