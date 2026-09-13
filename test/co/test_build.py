"""Static CLI build command regression tests."""

import io
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).parents[2]
sys.path.insert(0, str(ROOT / "scripts" / "co"))

from build import _nix_build, _official_version  # noqa: E402
from common import LifecycleError  # noqa: E402


def test_official_version_resolves_latest_stable_rust_release() -> None:
    """Bind custom builds to the stable Rust release named by GitHub."""
    metadata = io.BytesIO(
        b'{"tag_name":"rust-v0.154.0","draft":false,"prerelease":false}'
    )

    with patch("build.urlopen", return_value=metadata) as urlopen:
        assert _official_version() == "0.154.0"

    request = urlopen.call_args.args[0]
    assert request.full_url == (
        "https://api.github.com/repos/openai/codex/releases/latest"
    )


@pytest.mark.parametrize(
    "metadata",
    [
        b'{"tag_name":"rust-v0.154.0-alpha.1","draft":false,"prerelease":true}',
        b'{"tag_name":"v0.154.0","draft":false,"prerelease":false}',
        b'{"tag_name":"rust-v0.154.0","draft":true,"prerelease":false}',
    ],
)
def test_official_version_rejects_nonstable_or_malformed_release(
    metadata: bytes,
) -> None:
    """Fail the build instead of silently restoring the development version."""
    with (
        patch("build.urlopen", return_value=io.BytesIO(metadata)),
        pytest.raises(LifecycleError, match="stable release"),
    ):
        _official_version()


def test_nix_build_uses_all_cores_for_one_derivation(tmp_path: Path) -> None:
    """Keep package concurrency bounded while exposing every CPU core."""
    store = tmp_path / "store"
    version_input = tmp_path / "version-input"
    version_input.mkdir()
    result = subprocess.CompletedProcess([], 0, f"{store}\n", "")

    with (
        patch("build.run", return_value=result) as run,
        patch("build.verify_static_elf"),
        patch("build.tempfile.TemporaryDirectory") as temporary_directory,
    ):
        temporary_directory.return_value.__enter__.return_value = str(version_input)
        output, binary = _nix_build(tmp_path, "0.154.0")

    assert output == str(store)
    assert binary == store / "bin/codex"
    assert (version_input / "version").read_text(encoding="utf-8") == "0.154.0\n"
    run.assert_called_once_with(
        [
            "nix",
            "build",
            f"git+{tmp_path.as_uri()}#codex",
            "--override-input",
            "build-version",
            f"path:{version_input}",
            "--no-link",
            "--print-out-paths",
            "--no-write-lock-file",
            "--max-jobs",
            "1",
            "--cores",
            "0",
        ],
        cwd=tmp_path,
    )


def test_nix_build_honors_explicit_core_limit(tmp_path: Path) -> None:
    """Allow one invocation to reduce cores without changing the default."""
    store = tmp_path / "store"
    result = subprocess.CompletedProcess([], 0, f"{store}\n", "")

    with (
        patch("build.run", return_value=result) as run,
        patch("build.verify_static_elf"),
    ):
        _nix_build(tmp_path, "0.154.0", cores=4)

    assert run.call_args.args[0][-2:] == ["--cores", "4"]


def test_nix_build_rejects_negative_core_limit(tmp_path: Path) -> None:
    """Reject invalid limits before invoking Nix."""
    with (
        patch("build.run") as run,
        pytest.raises(LifecycleError, match="cores"),
    ):
        _nix_build(tmp_path, "0.154.0", cores=-1)

    run.assert_not_called()
