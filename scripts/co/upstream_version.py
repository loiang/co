"""Select an immutable build version from authenticated upstream Git refs.

SemVer precedence includes prereleases; release metadata is deliberately not
used to decide which source version is newest.
"""

import json
import re
import subprocess

from common import LifecycleError

TAGS_API = "repos/openai/codex/git/matching-refs/tags/rust-v"
MAX_METADATA_BYTES = 8 * 1024 * 1024
MAX_TAGS = 10000
_NUMBER = r"(0|[1-9][0-9]*)"
_VERSION = re.compile(
    rf"{_NUMBER}\.{_NUMBER}\.{_NUMBER}"
    r"(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?"
    r"(?:\+([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?"
)
_OID = re.compile(r"[0-9a-f]{40}")


def version_key(version: str) -> tuple | None:
    """Return SemVer precedence, rejecting malformed numeric identifiers."""
    match = _VERSION.fullmatch(version)
    if match is None:
        return None
    major, minor, patch, prerelease, _ = match.groups()
    identifiers = prerelease.split(".") if prerelease else []
    if any(
        part.isdigit() and len(part) > 1 and part.startswith("0")
        for part in identifiers
    ):
        return None
    return (
        (len(major), major),
        (len(minor), minor),
        (len(patch), patch),
        not identifiers,
        tuple(
            (0, (len(part), part)) if part.isdigit() else (1, part)
            for part in identifiers
        ),
    )


def latest_tag_version(raw: bytes) -> str:
    """Validate bounded Git ref metadata and select its largest SemVer tag."""
    if len(raw) > MAX_METADATA_BYTES:
        raise LifecycleError("官方 tags metadata 超过 8 MiB")
    try:
        refs = json.loads(raw)
    except (json.JSONDecodeError, UnicodeError) as error:
        raise LifecycleError("官方 tags metadata 不是有效 JSON") from error
    if not isinstance(refs, list) or len(refs) > MAX_TAGS:
        raise LifecycleError("官方 tags metadata 必须是最多 10000 条 ref 的 JSON array")
    versions = []
    for entry in refs:
        if not isinstance(entry, dict):
            raise LifecycleError("官方 tags ref 必须是 JSON object")
        ref, obj = entry.get("ref"), entry.get("object")
        if (
            not isinstance(ref, str)
            or not ref.startswith("refs/tags/rust-v")
            or not isinstance(obj, dict)
            or obj.get("type") not in ("commit", "tag")
            or not isinstance(obj.get("sha"), str)
            or _OID.fullmatch(obj["sha"]) is None
        ):
            raise LifecycleError("官方 tags ref metadata 无效")
        version = ref.removeprefix("refs/tags/rust-v")
        key = version_key(version)
        if key is not None:
            versions.append((key, version))
    if not versions:
        raise LifecycleError("官方 tags 中没有有效完整 rust-v<semver>")
    # Build metadata has no SemVer precedence; lexical order breaks ties only.
    return max(versions)[1]


def official_version() -> str:
    """Query tags once through gh's credential store and freeze the selection."""
    try:
        result = subprocess.run(
            [
                "gh",
                "api",
                TAGS_API,
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
    except OSError as error:
        raise LifecycleError("gh CLI 执行失败") from error
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
        raise LifecycleError(f"gh api 查询官方 tags 失败 (exit {result.returncode})")
    if not isinstance(result.stdout, bytes):
        raise LifecycleError("gh CLI 返回非 binary payload")
    return latest_tag_version(result.stdout)
