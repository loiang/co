"""Behavior tests for merge-based upgrades in real temporary repositories."""

import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).parents[2]
sys.path.insert(0, str(ROOT / "scripts" / "co"))

from git_lifecycle import (  # noqa: E402
    Candidate,
    CandidateConflict,
    create_upgrade_candidate,
    finalize_candidate,
)


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _commit(root: Path, message: str) -> str:
    _git(root, "add", "-A")
    _git(root, "commit", "-m", message)
    return _git(root, "rev-parse", "HEAD")


def _write(root: Path, relative: str, content: str) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _repositories(tmp_path: Path) -> tuple[Path, Path, str]:
    upstream = tmp_path / "upstream"
    subprocess.run(["git", "init", "-b", "main", str(upstream)], check=True)
    _git(upstream, "config", "user.name", "Co Test")
    _git(upstream, "config", "user.email", "co-test@example.invalid")
    _write(upstream, "shared.txt", "base\n")
    _write(upstream, "codex-rs/Cargo.toml", '[workspace.package]\nversion = "0.0.0"\n')
    _write(upstream, "codex-rs/Cargo.lock", "lock-base\n")
    baseline = _commit(upstream, "base")

    checkout = tmp_path / "co"
    subprocess.run(["git", "clone", str(upstream), str(checkout)], check=True)
    _git(checkout, "remote", "rename", "origin", "upstream")
    _git(checkout, "config", "user.name", "Co Test")
    _git(checkout, "config", "user.email", "co-test@example.invalid")
    _write(checkout, ".co/upstream-rev", baseline + "\n")
    _write(checkout, "custom.txt", "custom\n")
    _commit(checkout, "customization")
    return upstream, checkout, baseline


def test_merges_upstream_and_preserves_custom_history(tmp_path: Path) -> None:
    upstream, checkout, _ = _repositories(tmp_path)
    custom_head = _git(checkout, "rev-parse", "HEAD")
    _write(upstream, "upstream.txt", "new\n")
    _write(upstream, "codex-rs/Cargo.lock", "lock-advanced\n")
    target = _commit(upstream, "upstream advance")

    candidate = create_upgrade_candidate(checkout, created_at="20260912T130000Z")

    assert candidate.changed is True
    assert candidate.branch == f"upgrade/20260912T130000Z-{target[:10]}"
    assert (candidate.root / "custom.txt").read_text(encoding="utf-8") == "custom\n"
    assert (candidate.root / "upstream.txt").read_text(encoding="utf-8") == "new\n"
    assert 'version = "0.0.0"' in (candidate.root / "codex-rs/Cargo.toml").read_text()
    assert (candidate.root / "codex-rs/Cargo.lock").read_text() == "lock-advanced\n"
    assert _git(checkout, "branch", "--show-current") == "main"
    assert len(_git(candidate.root, "show", "-s", "--format=%P", "HEAD").split()) == 2
    _git(candidate.root, "merge-base", "--is-ancestor", custom_head, "HEAD")
    _git(candidate.root, "merge-base", "--is-ancestor", target, "HEAD")


def test_same_upstream_sha_is_a_noop(tmp_path: Path) -> None:
    _, checkout, baseline = _repositories(tmp_path)

    candidate = create_upgrade_candidate(checkout, created_at="20260912T130001Z")

    assert candidate.changed is False
    assert candidate.root == checkout.resolve()
    assert candidate.upstream_rev == baseline
    assert not (checkout / ".states" / "worktrees").exists()


def test_conflict_preserves_candidate_worktree_for_continuation(tmp_path: Path) -> None:
    upstream, checkout, _ = _repositories(tmp_path)
    _write(checkout, "shared.txt", "custom edit\n")
    _commit(checkout, "custom conflict")
    _write(upstream, "shared.txt", "upstream edit\n")
    _commit(upstream, "upstream conflict")

    with pytest.raises(CandidateConflict, match="merge --continue") as captured:
        create_upgrade_candidate(checkout, created_at="20260912T130002Z")

    message = str(captured.value)
    candidate_root = checkout / ".states" / "worktrees" / "20260912T130002Z-"
    matching = tuple(candidate_root.parent.glob(candidate_root.name + "*"))
    assert len(matching) == 1
    assert matching[0].is_dir()
    assert str(matching[0]) in message
    assert "just --justfile" in message
    assert "co-upgrade-finalize" in message


def test_finalize_locks_without_unscoped_flake_update(tmp_path: Path) -> None:
    upstream, checkout, _ = _repositories(tmp_path)
    _write(upstream, "upstream.txt", "new\n")
    target = _commit(upstream, "upstream advance")
    _write(checkout, "flake.lock", "{}\n")
    _write(checkout, "nix/cargo-git-hashes.nix", "{}\n")
    commands: list[list[str]] = []

    def capture(
        command: list[str], **_kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        return subprocess.CompletedProcess(command, 0, "", "")

    with patch("git_lifecycle.run", side_effect=capture):
        finalize_candidate(Candidate(checkout, "main", target, True))

    assert ["nix", "flake", "lock"] in commands
    assert not any(command[:3] == ["nix", "flake", "update"] for command in commands)
    assert any(
        any(item.endswith("update-cargo-git-hashes.py") for item in command)
        for command in commands
    )
    assert [
        "nix",
        "build",
        ".#codex-cargo-deps",
        "--no-link",
        "--no-write-lock-file",
    ] in commands
