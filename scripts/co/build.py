"""Build the upstream native package and emit source-bound release assets."""

import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

from common import (
    LifecycleError,
    require_clean,
    require_repo,
    sha256,
    timestamp,
    write_json,
)
from evidence import source_identity
from build_worktree import build_lock, build_worktree
from native_package import NativePackage, PackageRequest, build_package

_OFFICIAL_RELEASE_API = "repos/openai/codex/releases/latest"
_MAX_RELEASE_METADATA_BYTES = 1024 * 1024
_STABLE_RELEASE_TAG = re.compile(
    r"rust-v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"
)


def _official_version() -> str:
    """Resolve the stable upstream version once for a reproducible build input."""
    try:
        result = subprocess.run(
            [
                "gh",
                "api",
                _OFFICIAL_RELEASE_API,
                "--header",
                "Accept: application/vnd.github+json",
                "--header",
                "X-GitHub-Api-Version: 2022-11-28",
            ],
            check=False,
            capture_output=True,
        )
    except FileNotFoundError as error:
        raise LifecycleError("gh CLI 不可用；请安装 GitHub CLI") from error
    if result.returncode:
        stderr = (result.stderr or b"").decode(errors="replace").lower()
        if any(
            marker in stderr
            for marker in (
                "not logged into",
                "authentication",
                "bad credentials",
                "requires authentication",
                "401",
            )
        ):
            raise LifecycleError("gh CLI 未认证或认证已失效；请先完成 gh auth login")
        raise LifecycleError(
            f"gh api 查询官方 latest stable release 失败 (exit {result.returncode})"
        )
    raw_metadata = result.stdout or b""
    if len(raw_metadata) > _MAX_RELEASE_METADATA_BYTES:
        raise LifecycleError("官方 latest stable release metadata 超过 1 MiB")
    try:
        metadata = json.loads(raw_metadata)
    except (json.JSONDecodeError, UnicodeError) as error:
        raise LifecycleError(
            "官方 latest stable release metadata 不是有效 JSON"
        ) from error
    if not isinstance(metadata, dict):
        raise LifecycleError("官方 latest stable release metadata 不是 JSON object")
    tag = metadata.get("tag_name")
    if metadata.get("draft") is not False or metadata.get("prerelease") is not False:
        raise LifecycleError("官方 latest stable release 不是稳定 release")
    if not isinstance(tag, str) or _STABLE_RELEASE_TAG.fullmatch(tag) is None:
        raise LifecycleError("官方 latest stable release tag 不是 rust-v<semver>")
    return tag.removeprefix("rust-v")


def _emit_assets(
    root: Path,
    identity: dict[str, Any],
    package: NativePackage,
    version: str,
) -> Path:
    archive = package.archive
    build_dir = archive.parent
    entrypoint = package.metadata["entrypoint"]
    binary = package.directory / entrypoint
    manifest_path = build_dir / f"co-manifest-{identity['sourceRev'][:10]}.json"
    manifest = {
        "schemaVersion": 2,
        "repository": "loiang/co",
        "sourceRev": identity["sourceRev"],
        "sourceVersion": version,
        "platform": package.platform,
        "package": package.metadata,
        "checksums": {entrypoint: sha256(binary), archive.name: sha256(archive)},
    }
    write_json(manifest_path, manifest)
    checksums = build_dir / "SHA256SUMS"
    checksums.write_text(
        f"{sha256(archive)}  {archive.name}\n"
        f"{sha256(manifest_path)}  {manifest_path.name}\n",
        encoding="utf-8",
    )
    record = {
        "schemaVersion": 2,
        **identity,
        "completedAt": timestamp(),
        "packageDir": str(package.directory.relative_to(root)),
        "package": package.metadata,
        "manifestSha256": sha256(manifest_path),
        "assets": [
            str(path.relative_to(root)) for path in (archive, manifest_path, checksums)
        ],
    }
    latest = root / ".states/co/build/latest.json"
    write_json(latest, record)
    return latest


def build(repository: Path, cores: int = 0) -> Path:
    """Build one complete native package with the upstream grouped Cargo builder.

    Args:
        repository: Checkout whose source and lock identities bind the assets.
        cores: Cargo compilation jobs; zero uses Cargo's host CPU default.

    Returns:
        Path to the build evidence record and checksummed publication assets.
    """
    if cores < 0:
        raise LifecycleError("Cargo cores 必须是非负整数")
    root = require_repo(repository)
    require_clean(root)
    with build_lock(root) as lock_fd:
        identity = source_identity(root)
        version = _official_version()
        build_dir = (
            root / ".states/co/build" / f"{timestamp()}-{identity['sourceRev'][:10]}"
        )
        build_dir.mkdir(parents=True, exist_ok=False)
        try:
            with build_worktree(root, identity["sourceRev"]) as source:
                package = build_package(
                    PackageRequest(
                        source,
                        build_dir,
                        identity["sourceRev"],
                        version,
                        cores,
                        target_dir=root / "codex-rs/target",
                        lock_fd=lock_fd,
                    )
                )
            return _emit_assets(root, identity, package, version)
        except BaseException as error:
            try:
                shutil.rmtree(build_dir)
            except OSError as cleanup_error:
                raise LifecycleError(
                    f"build 失败: {error}; artifact 清理失败: {cleanup_error}"
                ) from error
            raise
