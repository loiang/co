"""GitHub release assets remain immutable and retry-safe."""

import hashlib
import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest

ROOT = Path(__file__).parents[2]
sys.path.insert(0, str(ROOT / "scripts" / "co"))

from github_release import ensure_release  # noqa: E402

TAG = "co-20260912T140000Z-aaaaaaaaaa"
COMMIT = "a" * 40


def _details(assets: tuple[Path, ...]) -> subprocess.CompletedProcess[str]:
    payload = {
        "tagName": TAG,
        "targetCommitish": COMMIT,
        "isDraft": False,
        "assets": [
            {
                "name": path.name,
                "size": path.stat().st_size,
                "digest": f"sha256:{hashlib.sha256(path.read_bytes()).hexdigest()}",
            }
            for path in assets
        ],
    }
    return subprocess.CompletedProcess([], 0, json.dumps(payload), "")


def test_matching_assets_are_reused_without_upload(tmp_path: Path) -> None:
    assets = tuple(tmp_path / name for name in ("archive", "manifest", "sums"))
    for index, path in enumerate(assets):
        path.write_text(f"asset-{index}", encoding="utf-8")
    run = Mock(side_effect=[_details(assets), _details(assets)])

    ensure_release(TAG, COMMIT, assets, False, run=run)

    assert all("upload" not in call.args[0] for call in run.call_args_list)


def test_conflicting_asset_is_rejected_without_clobber(tmp_path: Path) -> None:
    asset = tmp_path / "archive"
    asset.write_text("local", encoding="utf-8")
    payload = {
        "tagName": TAG,
        "targetCommitish": COMMIT,
        "isDraft": False,
        "assets": [{"name": asset.name, "size": 5, "digest": "sha256:wrong"}],
    }
    run = Mock(return_value=subprocess.CompletedProcess([], 0, json.dumps(payload), ""))

    with pytest.raises(RuntimeError, match="内容冲突"):
        ensure_release(TAG, COMMIT, (asset,), False, run=run)

    assert "--clobber" not in str(run.call_args_list)
