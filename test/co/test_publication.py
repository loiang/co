"""Immutable publication retries against a real temporary Git remote."""

import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).parents[2]
sys.path.insert(0, str(ROOT / "scripts" / "co"))

from common import LifecycleError  # noqa: E402
from publication import (  # noqa: E402
    _ensure_local_tag,
    _push_refs,
    _require_main_fast_forward,
    publish,
)


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _repository(tmp_path: Path) -> tuple[Path, Path, str]:
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", str(remote)], check=True)
    root = tmp_path / "co"
    subprocess.run(["git", "init", "-b", "main", str(root)], check=True)
    _git(root, "config", "user.name", "Co Test")
    _git(root, "config", "user.email", "co-test@example.invalid")
    (root / "tracked").write_text("source\n", encoding="utf-8")
    (root / ".gitignore").write_text(".states/\n", encoding="utf-8")
    _git(root, "add", "tracked", ".gitignore")
    _git(root, "commit", "-m", "source")
    _git(root, "switch", "-c", "custom/test")
    _git(root, "remote", "add", "origin", str(remote))
    return root, remote, _git(root, "rev-parse", "HEAD")


def test_publish_failure_keeps_exact_refs_for_idempotent_retry(tmp_path: Path) -> None:
    root, remote, source_rev = _repository(tmp_path)
    asset = root / ".states/co/build/asset"
    asset.parent.mkdir(parents=True)
    asset.write_text("asset", encoding="utf-8")
    tag = f"co-20260912T140000Z-{source_rev[:10]}"

    with (
        patch("publication.require_release_records", return_value=(asset,)),
        patch("publication._require_origin"),
        patch("publication.timestamp", return_value="20260912T140000Z"),
        patch("publication.ensure_release", side_effect=RuntimeError("network")),
        pytest.raises(LifecycleError, match="可重试"),
    ):
        publish(root)

    pending = json.loads((root / ".states/co/publish/pending.json").read_text())
    assert pending["tag"] == tag
    assert pending["phase"] == "refs-pushed"
    assert _git(remote, "rev-parse", f"refs/tags/{tag}^{{}}") == source_rev
    assert _git(remote, "rev-parse", "refs/heads/main") == source_rev

    with (
        patch("publication.require_release_records", return_value=(asset,)),
        patch("publication._require_origin"),
        patch("publication.timestamp", return_value="20260912T140001Z"),
        patch("publication.ensure_release"),
    ):
        assert publish(root) == tag
    assert json.loads((root / ".states/co/publish/latest.json").read_text())[
        "phase"
    ] == ("released")


def test_existing_local_tag_cannot_be_retargeted(tmp_path: Path) -> None:
    root, _, source_rev = _repository(tmp_path)
    tag = f"co-20260912T140002Z-{source_rev[:10]}"
    _ensure_local_tag(root, tag, source_rev)
    (root / "tracked").write_text("other\n", encoding="utf-8")
    _git(root, "commit", "-am", "other")

    with pytest.raises(LifecycleError, match="拒绝覆盖"):
        _ensure_local_tag(root, tag, _git(root, "rev-parse", "HEAD"))


def test_atomic_push_rejects_concurrent_remote_main(tmp_path: Path) -> None:
    root, remote, source_rev = _repository(tmp_path)
    _git(root, "push", "origin", f"{source_rev}:refs/heads/main")
    competitor = tmp_path / "competitor"
    subprocess.run(
        ["git", "clone", "--branch", "main", str(remote), str(competitor)],
        check=True,
    )
    _git(competitor, "config", "user.name", "Co Test")
    _git(competitor, "config", "user.email", "co-test@example.invalid")
    (competitor / "remote").write_text("advance\n", encoding="utf-8")
    _git(competitor, "add", "remote")
    _git(competitor, "commit", "-m", "remote advance")
    _git(competitor, "push", "origin", "HEAD:refs/heads/main")
    remote_head = _git(remote, "rev-parse", "refs/heads/main")
    (root / "tracked").write_text("candidate\n", encoding="utf-8")
    _git(root, "commit", "-am", "candidate advance")
    candidate_head = _git(root, "rev-parse", "HEAD")
    tag = f"co-20260912T140003Z-{candidate_head[:10]}"
    _ensure_local_tag(root, tag, candidate_head)

    with pytest.raises(LifecycleError, match="fast-forward"):
        _require_main_fast_forward(root)
    with pytest.raises(LifecycleError):
        _push_refs(root, tag)

    assert _git(remote, "rev-parse", "refs/heads/main") == remote_head
    assert (
        subprocess.run(
            ["git", "-C", str(remote), "show-ref", "--verify", f"refs/tags/{tag}"],
            check=False,
        ).returncode
        != 0
    )
