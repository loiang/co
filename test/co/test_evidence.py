"""Build evidence permits local dirty builds while publication stays clean-only."""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).parents[2]
sys.path.insert(0, str(ROOT / "scripts" / "co"))

from evidence import read_build_record  # noqa: E402


def test_matching_dirty_build_record_is_valid_for_local_host_gate(
    tmp_path: Path,
) -> None:
    identity = {
        "sourceRev": "a" * 40,
        "upstreamRev": "b" * 40,
        "flakeLockSha256": "c" * 64,
        "dirty": True,
    }
    record_path = tmp_path / ".states/co/build/latest.json"
    record_path.parent.mkdir(parents=True)
    record_path.write_text(json.dumps(identity), encoding="utf-8")

    assert read_build_record(tmp_path, identity) == identity
