"""Publish one provenance-bound candidate branch and immutable release tag."""

import re
import subprocess
from pathlib import Path
from typing import Any

from common import (
    LifecycleError,
    git,
    head,
    read_json,
    require_clean,
    require_repo,
    sha256,
    timestamp,
    write_json,
)
from evidence import require_release_records
from github_release import ensure_release

TAG_RE = re.compile(r"^co-[0-9]{8}T[0-9]{6}Z-[0-9a-f]{10}$")
ORIGIN_RE = re.compile(r"github\.com(?::|/)loiang/co(?:\.git)?$")


def _succeeds(root: Path, *args: str) -> bool:
    result = subprocess.run(
        ["git", "-C", str(root), *args],
        cwd=root,
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return result.returncode == 0


def _source_branch(root: Path) -> str:
    """Require a main, temporary upgrade, or one-time bootstrap branch."""
    branch = git(root, "branch", "--show-current")
    allowed = branch == "main" or branch.startswith(("upgrade/", "custom/"))
    if not allowed:
        raise LifecycleError(f"发布源码分支不是 main/upgrade candidate: {branch}")
    return branch


def _require_origin(root: Path) -> None:
    origin = git(root, "remote", "get-url", "origin")
    if ORIGIN_RE.search(origin) is None:
        raise LifecycleError(f"origin 不是 loiang/co: {origin}")


def _remote_tag_commit(root: Path, tag: str) -> str | None:
    output = git(
        root,
        "ls-remote",
        "origin",
        f"refs/tags/{tag}",
        f"refs/tags/{tag}^{{}}",
    )
    direct = None
    peeled = None
    for line in output.splitlines():
        fields = line.split()
        if len(fields) != 2:
            continue
        if fields[1] == f"refs/tags/{tag}^{{}}":
            peeled = fields[0]
        elif fields[1] == f"refs/tags/{tag}":
            direct = fields[0]
    return peeled or direct


def _pending(
    root: Path,
    source_rev: str,
    assets: tuple[Path, ...],
    *,
    archive_completed: bool,
) -> dict[str, Any] | None:
    path = root / ".states/co/publish/pending.json"
    if not path.exists():
        return None
    record = read_json(path)
    tag = record.get("tag")
    if not isinstance(tag, str) or TAG_RE.fullmatch(tag) is None:
        raise LifecycleError("待续发布记录含无效 tag")
    if record.get("sourceRev") != source_rev:
        _archive_released_pending(path, record, persist=archive_completed)
        return None
    expected_assets = [str(path.relative_to(root)) for path in assets]
    if record.get("assets") != expected_assets:
        raise LifecycleError("待续发布记录与当前 build assets 不一致")
    expected_digests = {path.name: sha256(path) for path in assets}
    if record.get("assetSha256") != expected_digests:
        raise LifecycleError("待续发布 assets digest 不一致，拒绝替换")
    return record


def _archive_released_pending(
    path: Path, record: dict[str, Any], *, persist: bool
) -> None:
    """Move one completed prior-source retry record into immutable history."""
    if record.get("phase") != "released":
        raise LifecycleError("存在属于其他 source commit 的待续发布记录")
    source_rev = record.get("sourceRev")
    tag = record.get("tag")
    assets = record.get("assets")
    valid = (
        isinstance(source_rev, str)
        and re.fullmatch(r"[0-9a-f]{40,64}", source_rev) is not None
        and isinstance(tag, str)
        and TAG_RE.fullmatch(tag) is not None
        and tag.endswith(f"-{source_rev[:10]}")
        and isinstance(assets, list)
        and all(isinstance(asset, str) for asset in assets)
    )
    if not valid:
        raise LifecycleError("已完成待续发布记录身份无效，拒绝归档")
    if not persist:
        return
    completed = path.parent / "completed" / f"{tag}.json"
    if completed.exists():
        if read_json(completed) != record:
            raise LifecycleError(f"已归档发布记录内容冲突: {tag}")
        path.unlink()
        return
    completed.parent.mkdir(parents=True, exist_ok=True)
    path.replace(completed)


def _ensure_local_tag(root: Path, tag: str, source_rev: str) -> None:
    reference = f"refs/tags/{tag}"
    if _succeeds(root, "show-ref", "--verify", "--quiet", reference):
        if git(root, "rev-list", "-n", "1", tag) != source_rev:
            raise LifecycleError(f"本地 tag 已指向其他 commit，拒绝覆盖: {tag}")
        return
    git(root, "tag", "-a", tag, source_rev, "-m", f"co CLI release {tag}")


def _require_main_fast_forward(root: Path) -> None:
    output = git(root, "ls-remote", "origin", "refs/heads/main")
    if not output:
        return
    fields = output.split()
    if len(fields) != 2:
        raise LifecycleError("远端 main identity 无法解析")
    git(root, "fetch", "--no-tags", "origin", "refs/heads/main")
    fetched = git(root, "rev-parse", "FETCH_HEAD^{commit}")
    if fetched != fields[0] or not _succeeds(
        root, "merge-base", "--is-ancestor", fetched, "HEAD"
    ):
        raise LifecycleError("candidate 不能 fast-forward 远端 main，拒绝发布")


def _push_refs(root: Path, tag: str) -> None:
    refspecs = [
        "HEAD:refs/heads/main",
        f"refs/tags/{tag}:refs/tags/{tag}",
    ]
    if _succeeds(root, "show-ref", "--verify", "--quiet", "refs/notes/commits"):
        refspecs.append("refs/notes/commits:refs/notes/commits")
    git(root, "push", "--atomic", "origin", *refspecs, capture=False)


def _record(
    root: Path,
    tag: str,
    source_branch: str,
    source_rev: str,
    assets: tuple[Path, ...],
    phase: str,
) -> dict[str, Any]:
    return {
        "schemaVersion": 2,
        "tag": tag,
        "sourceBranch": source_branch,
        "targetBranch": "main",
        "sourceRev": source_rev,
        "assets": [str(path.relative_to(root)) for path in assets],
        "assetSha256": {path.name: sha256(path) for path in assets},
        "phase": phase,
        "updatedAt": timestamp(),
    }


def _write_pending(root: Path, record: dict[str, Any]) -> None:
    write_json(root / ".states/co/publish/pending.json", record)


def publish(repository: Path, *, dry_run: bool = False) -> str:
    """Atomically push one candidate/tag and publish verified GitHub assets.

    Args:
        repository: Clean candidate checkout with exact verification records.
        dry_run: Perform remote collision checks without creating local refs.

    Returns:
        The immutable tag selected for this commit or its resumable attempt.
    """
    root = require_repo(repository)
    require_clean(root)
    source_branch = _source_branch(root)
    _require_origin(root)
    source_rev = head(root)
    assets = require_release_records(root)
    pending = _pending(root, source_rev, assets, archive_completed=not dry_run)
    tag = str(pending["tag"]) if pending else f"co-{timestamp()}-{source_rev[:10]}"
    if TAG_RE.fullmatch(tag) is None:
        raise LifecycleError(f"生成的 release tag 无效: {tag}")
    remote_commit = _remote_tag_commit(root, tag)
    if remote_commit is not None and remote_commit != source_rev:
        raise LifecycleError(f"远端 tag 已指向其他 commit，拒绝覆盖: {tag}")
    _require_main_fast_forward(root)
    if dry_run:
        try:
            ensure_release(tag, source_rev, assets, True)
        except RuntimeError as error:
            raise LifecycleError(str(error)) from error
        return tag
    _ensure_local_tag(root, tag, source_rev)
    record = _record(root, tag, source_branch, source_rev, assets, "local-tag")
    _write_pending(root, record)
    _push_refs(root, tag)
    record["phase"] = "refs-pushed"
    record["updatedAt"] = timestamp()
    _write_pending(root, record)
    try:
        ensure_release(tag, source_rev, assets, False)
    except RuntimeError as error:
        raise LifecycleError(f"refs 已推送，GitHub Release 可重试: {error}") from error
    record["phase"] = "released"
    record["updatedAt"] = timestamp()
    _write_pending(root, record)
    write_json(root / ".states/co/publish/latest.json", record)
    return tag
