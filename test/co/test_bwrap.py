"""Static upstream bwrap asset resolution and cache safety tests."""

import io
import json
import os
import sys
import tarfile
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).parents[2]
sys.path.insert(0, str(ROOT / "scripts" / "co"))
sys.path.insert(0, str(ROOT / "scripts"))
os.environ.setdefault("CODEX_REPO_ROOT", str(ROOT))

from bwrap import BwrapAsset, fetch_bwrap_binary, resolve_bwrap_asset  # noqa: E402
from common import LifecycleError, sha256  # noqa: E402
from codex_package.cargo import build_source_binaries  # noqa: E402
from codex_package.targets import PACKAGE_VARIANTS, TARGET_SPECS  # noqa: E402


VERSION = "0.155.1"
TAG = f"rust-v{VERSION}"
ARCHIVE_NAME = "bwrap-x86_64-unknown-linux-musl.tar.gz"


def _archive_bytes(
    *,
    name: str = "bwrap-x86_64-unknown-linux-musl",
    mode: int = 0o755,
    extra: bool = False,
) -> bytes:
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:gz") as archive:
        member = tarfile.TarInfo(name)
        member.mode = mode
        member.size = 6
        archive.addfile(member, io.BytesIO(b"bwrap\n"))
        if extra:
            other = tarfile.TarInfo("README")
            other.size = 1
            archive.addfile(other, io.BytesIO(b"x"))
    return output.getvalue()


def _asset(data: bytes, *, tag: str = TAG, digest: str | None = None) -> BwrapAsset:
    return BwrapAsset(
        VERSION,
        tag,
        "x86_64",
        ARCHIVE_NAME,
        f"https://github.com/openai/codex/releases/download/{tag}/{ARCHIVE_NAME}",
        len(data),
        digest or sha256_bytes(data),
    )


def sha256_bytes(data: bytes) -> str:
    import hashlib

    return hashlib.sha256(data).hexdigest()


def _metadata(*, tag: str = TAG, digest: str = "0" * 64, size: int = 1) -> bytes:
    return json.dumps(
        {
            "tag_name": tag,
            "draft": False,
            "prerelease": False,
            "assets": [
                {
                    "name": ARCHIVE_NAME,
                    "size": size,
                    "digest": f"sha256:{digest}",
                    "browser_download_url": (
                        f"https://github.com/openai/codex/releases/download/{tag}/{ARCHIVE_NAME}"
                    ),
                }
            ],
        }
    ).encode()


@pytest.mark.parametrize(
    ("target", "name", "architecture"),
    [
        ("x86_64-unknown-linux-gnu", ARCHIVE_NAME, "x86_64"),
        (
            "aarch64-unknown-linux-musl",
            "bwrap-aarch64-unknown-linux-musl.tar.gz",
            "aarch64",
        ),
    ],
)
def test_resolves_exact_release_tag_and_target_architecture(
    target: str, name: str, architecture: str
) -> None:
    payload = json.loads(_metadata().decode())
    payload["assets"][0]["name"] = name
    payload["assets"][0]["browser_download_url"] = (
        f"https://github.com/openai/codex/releases/download/{TAG}/{name}"
    )
    with patch(
        "bwrap.urlopen", return_value=io.BytesIO(json.dumps(payload).encode())
    ) as fetch:
        asset = resolve_bwrap_asset(VERSION, target)

    assert asset.tag == TAG
    assert asset.architecture == architecture
    assert asset.name == name
    assert (
        fetch.call_args.args[0].full_url
        == f"https://api.github.com/repos/openai/codex/releases/tags/{TAG}"
    )


@pytest.mark.parametrize(
    "metadata",
    [
        _metadata(digest="g" * 64),
        _metadata(size=0),
        _metadata(tag="rust-v0.155.0"),
    ],
)
def test_rejects_untrusted_release_metadata(metadata: bytes) -> None:
    with (
        patch("bwrap.urlopen", return_value=io.BytesIO(metadata)),
        pytest.raises(LifecycleError),
    ):
        resolve_bwrap_asset(VERSION, "x86_64-unknown-linux-gnu")


def test_download_rejects_size_and_digest_tampering(tmp_path: Path) -> None:
    archive = _archive_bytes()
    asset = _asset(archive, digest="0" * 64)
    with (
        patch("bwrap.urlopen", return_value=io.BytesIO(archive)),
        pytest.raises(LifecycleError, match="size/digest"),
    ):
        fetch_bwrap_binary(asset, cache_root=tmp_path)
    assert not list(tmp_path.rglob("*.tar.gz"))

    wrong_size = _asset(archive)
    wrong_size = BwrapAsset(
        wrong_size.version,
        wrong_size.tag,
        wrong_size.architecture,
        wrong_size.name,
        wrong_size.url,
        wrong_size.size + 1,
        wrong_size.digest,
    )
    with (
        patch("bwrap.urlopen", return_value=io.BytesIO(archive)),
        pytest.raises(LifecycleError, match="size/digest"),
    ):
        fetch_bwrap_binary(wrong_size, cache_root=tmp_path)


def test_extracts_member_named_after_verified_asset(tmp_path: Path) -> None:
    archive = _archive_bytes()
    asset = _asset(archive)

    with patch("bwrap.urlopen", return_value=io.BytesIO(archive)):
        binary = fetch_bwrap_binary(asset, cache_root=tmp_path)

    assert binary.read_bytes() == b"bwrap\n"


def test_rejects_legacy_bare_bwrap_member(tmp_path: Path) -> None:
    archive = _archive_bytes(name="bwrap")
    asset = _asset(archive)

    with pytest.raises(LifecycleError, match="普通可执行"):
        with patch("bwrap.urlopen", return_value=io.BytesIO(archive)):
            fetch_bwrap_binary(asset, cache_root=tmp_path)


@pytest.mark.parametrize(
    ("kwargs", "error"),
    [
        ({"name": "../bwrap"}, "普通可执行"),
        ({"name": "bwrap", "mode": 0o644}, "普通可执行"),
        ({"name": "bwrap", "extra": True}, "一个成员"),
    ],
)
def test_safe_extraction_rejects_dangerous_or_nonunique_archives(
    tmp_path: Path, kwargs: dict[str, object], error: str
) -> None:
    archive = _archive_bytes(**kwargs)
    asset = _asset(archive)
    with pytest.raises(LifecycleError, match=error):
        with patch("bwrap.urlopen", return_value=io.BytesIO(archive)):
            fetch_bwrap_binary(asset, cache_root=tmp_path)


def test_cache_is_version_and_digest_isolated(tmp_path: Path) -> None:
    archive = _archive_bytes()
    first = _asset(archive)
    second = _asset(archive, tag="rust-v0.155.2")
    with patch("bwrap.urlopen", return_value=io.BytesIO(archive)):
        first_path = fetch_bwrap_binary(first, cache_root=tmp_path)
    with patch("bwrap.urlopen", return_value=io.BytesIO(archive)):
        second_path = fetch_bwrap_binary(second, cache_root=tmp_path)
    assert first_path != second_path
    assert first_path.is_file() and second_path.is_file()


def test_tampered_cached_binary_is_restored_from_verified_archive(
    tmp_path: Path,
) -> None:
    archive = _archive_bytes()
    asset = _asset(archive)
    with patch("bwrap.urlopen", return_value=io.BytesIO(archive)):
        binary = fetch_bwrap_binary(asset, cache_root=tmp_path)
    binary.write_bytes(b"tampered")
    binary.chmod(0o755)

    restored = fetch_bwrap_binary(asset, cache_root=tmp_path)

    assert restored == binary
    assert restored.read_bytes() == b"bwrap\n"


def test_cargo_receives_bwrap_digest_pin_for_prebuilt_binary(tmp_path: Path) -> None:
    output = tmp_path / "target/x86_64-unknown-linux-gnu/release"
    output.mkdir(parents=True)
    for name in ("codex", "codex-code-mode-host"):
        path = output / name
        path.write_bytes(name.encode())
        path.chmod(0o755)
    bwrap = tmp_path / "bwrap"
    bwrap.write_bytes(b"static bwrap")
    bwrap.chmod(0o755)

    def capture(command: list[str], **kwargs: object) -> None:
        assert command[0:2] == ["cargo", "build"]
        assert kwargs["env"]["CODEX_BWRAP_SHA256"] == sha256(bwrap)

    with (
        patch("codex_package.cargo.cargo_target_dir", return_value=tmp_path / "target"),
        patch("codex_package.cargo.resolve_codex_v8_cargo_env", return_value={}),
        patch("codex_package.cargo.subprocess.run", side_effect=capture),
    ):
        build_source_binaries(
            TARGET_SPECS["x86_64-unknown-linux-gnu"],
            PACKAGE_VARIANTS["codex"],
            cargo="cargo",
            profile="release",
            entrypoint_bin=None,
            code_mode_host_bin=None,
            bwrap_bin=bwrap,
            codex_command_runner_bin=None,
            codex_windows_sandbox_setup_bin=None,
        )
