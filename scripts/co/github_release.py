"""Create a GitHub Release and publish verified, immutable local assets."""

import hashlib
import json
import subprocess
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

RELEASE_REPOSITORY = "loiang/co"
CommandRunner = Callable[..., subprocess.CompletedProcess[str]]


def _run(
    command: list[str], *, capture: bool = False
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, check=False, capture_output=capture, text=True)


def _require(result: subprocess.CompletedProcess[str], action: str) -> str:
    if result.returncode:
        detail = (result.stderr or "").strip() or (result.stdout or "").strip()
        raise RuntimeError(f"{action}失败{(': ' + detail) if detail else ''}")
    return (result.stdout or "").strip()


def _release(tag: str, run: CommandRunner) -> dict[str, Any] | None:
    result = run(
        [
            "gh",
            "release",
            "view",
            tag,
            "--repo",
            RELEASE_REPOSITORY,
            "--json",
            "tagName,targetCommitish,isDraft,assets",
        ],
        capture=True,
    )
    if result.returncode:
        detail = (result.stderr or "").strip() or (result.stdout or "").strip()
        if "release not found" in detail.lower():
            return None
        raise RuntimeError(
            f"查询 GitHub Release {tag} 失败{(': ' + detail) if detail else ''}"
        )
    try:
        details = json.loads(result.stdout or "")
    except json.JSONDecodeError as error:
        raise RuntimeError(f"GitHub Release {tag} 返回无效 JSON") from error
    if not isinstance(details, dict):
        raise RuntimeError(f"GitHub Release {tag} 返回无效对象")
    return details


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _asset_map(details: dict[str, Any], tag: str) -> dict[str, dict[str, Any]]:
    raw_assets = details.get("assets", [])
    if not isinstance(raw_assets, list):
        raise RuntimeError(f"GitHub Release {tag} assets 返回无效数组")
    assets: dict[str, dict[str, Any]] = {}
    for raw_asset in raw_assets:
        if not isinstance(raw_asset, dict) or not isinstance(
            raw_asset.get("name"), str
        ):
            raise RuntimeError(f"GitHub Release {tag} asset 返回无效对象")
        name = raw_asset["name"]
        if name in assets:
            raise RuntimeError(f"GitHub Release {tag} 存在重复 asset: {name}")
        assets[name] = raw_asset
    return assets


def _downloaded_digest(path: Path, tag: str, run: CommandRunner) -> str:
    with tempfile.TemporaryDirectory(prefix="co-release-readback-") as temporary:
        directory = Path(temporary)
        _require(
            run(
                [
                    "gh",
                    "release",
                    "download",
                    tag,
                    "--repo",
                    RELEASE_REPOSITORY,
                    "--pattern",
                    path.name,
                    "--dir",
                    str(directory),
                ]
            ),
            f"下载 GitHub Release asset {path.name}",
        )
        downloaded = directory / path.name
        if not downloaded.is_file():
            raise RuntimeError(f"GitHub Release asset {path.name} 下载后缺失")
        return _sha256(downloaded)


def _verify_asset(
    path: Path, remote: dict[str, Any], tag: str, run: CommandRunner
) -> None:
    expected_digest = f"sha256:{_sha256(path)}"
    if remote.get("size") != path.stat().st_size:
        raise RuntimeError(f"GitHub Release asset {path.name} 内容冲突")
    remote_digest = remote.get("digest")
    if remote_digest is None:
        remote_digest = f"sha256:{_downloaded_digest(path, tag, run)}"
    if remote_digest != expected_digest:
        raise RuntimeError(f"GitHub Release asset {path.name} 内容冲突")


def _verify_identity(details: dict[str, Any], tag: str, commit: str) -> None:
    target = str(details.get("targetCommitish", ""))
    if details.get("tagName") != tag or target != commit:
        raise RuntimeError(f"GitHub Release {tag} 身份不一致")


def _missing_assets(
    details: dict[str, Any],
    tag: str,
    paths: tuple[Path, ...],
    run: CommandRunner,
) -> tuple[Path, ...]:
    remote_assets = _asset_map(details, tag)
    missing: list[Path] = []
    for path in paths:
        remote = remote_assets.get(path.name)
        if remote is None:
            missing.append(path)
        else:
            _verify_asset(path, remote, tag, run)
    return tuple(missing)


def _create_draft(tag: str, commit: str, run: CommandRunner) -> dict[str, Any]:
    _require(
        run(
            [
                "gh",
                "release",
                "create",
                tag,
                "--repo",
                RELEASE_REPOSITORY,
                "--verify-tag",
                "--target",
                commit,
                "--generate-notes",
                "--draft",
                "--title",
                tag,
            ]
        ),
        f"创建 GitHub Release {tag}",
    )
    return {
        "tagName": tag,
        "targetCommitish": commit,
        "isDraft": True,
        "assets": [],
    }


def _upload_assets(tag: str, paths: tuple[Path, ...], run: CommandRunner) -> None:
    if not paths:
        return
    _require(
        run(
            [
                "gh",
                "release",
                "upload",
                tag,
                *(str(path) for path in paths),
                "--repo",
                RELEASE_REPOSITORY,
            ]
        ),
        f"上传 GitHub Release {tag} assets",
    )


def _publish_draft(tag: str, commit: str, run: CommandRunner) -> None:
    _require(
        run(
            [
                "gh",
                "release",
                "edit",
                tag,
                "--repo",
                RELEASE_REPOSITORY,
                "--draft=false",
            ]
        ),
        f"发布 GitHub Release {tag}",
    )
    published = _release(tag, run)
    if published is None or published.get("isDraft"):
        raise RuntimeError(f"GitHub Release {tag} draft 未成功发布")
    _verify_identity(published, tag, commit)


def ensure_release(
    tag: str,
    commit: str,
    assets: tuple[Path, ...],
    dry_run: bool,
    run: CommandRunner = _run,
) -> None:
    """Create or reuse one release, upload assets, and verify remote digests.

    Existing same-name assets are never overwritten. Matching assets make a
    retry idempotent; a size or GitHub-computed SHA-256 mismatch stops release.

    Args:
        tag: Immutable build tag already validated against ``commit``.
        commit: Full release commit used to validate GitHub identity.
        assets: Local archive, manifest, and checksum files to publish.
        dry_run: When true, perform read-only conflict checks without writes.
        run: Injectable command runner used by behavior tests.
    """
    details = _release(tag, run)
    if details is None:
        if dry_run:
            return
        details = _create_draft(tag, commit, run)
    _verify_identity(details, tag, commit)
    missing = _missing_assets(details, tag, assets, run)
    if missing and not dry_run:
        _upload_assets(tag, missing, run)
    if dry_run:
        return
    verified = _release(tag, run)
    if verified is None:
        raise RuntimeError(f"GitHub Release {tag} 上传后不可读")
    _verify_identity(verified, tag, commit)
    if _missing_assets(verified, tag, assets, run):
        raise RuntimeError(f"GitHub Release {tag} assets 上传后仍缺失")
    if verified.get("isDraft"):
        _publish_draft(tag, commit, run)
