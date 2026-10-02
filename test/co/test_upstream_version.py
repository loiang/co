"""SemVer selection and bounded authenticated Git ref validation."""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[2] / "scripts/co"))
from common import LifecycleError  # noqa: E402
from upstream_version import latest_tag_version, version_key  # noqa: E402


def refs(*versions: str) -> bytes:
    return json.dumps(
        [
            {
                "ref": f"refs/tags/rust-v{version}",
                "object": {"type": "commit", "sha": "a" * 40},
            }
            for version in versions
        ]
    ).encode()


@pytest.mark.parametrize(
    ("versions", "expected"),
    [
        (("0.160.0", "0.161.0-alpha.9", "0.161.0-alpha.11"), "0.161.0-alpha.11"),
        (("1.0.0-rc.1", "1.0.0"), "1.0.0"),
        (("1.0.0-alpha", "1.0.0-alpha.1", "1.0.0-beta"), "1.0.0-beta"),
        (("1.0.0-9", "1.0.0-a", "1.0.0-a.1"), "1.0.0-a.1"),
        (("999.0", "01.0.0", "1.0.0-alpha.01", "1.0.0"), "1.0.0"),
    ],
)
def test_selects_highest_semver(versions: tuple[str, ...], expected: str) -> None:
    assert latest_tag_version(refs(*versions)) == expected


def test_build_metadata_does_not_change_precedence() -> None:
    assert version_key("1.0.0+build.1") == version_key("1.0.0+build.2")


@pytest.mark.parametrize(
    "raw",
    [
        b"{}",
        b"null",
        b"[1]",
        b"[]",
        b"not JSON",
        refs("1.0"),
        b'[{"ref":"refs/heads/main","object":{"type":"commit","sha":"bad"}}]',
        b'[{"ref":"refs/tags/rust-v1.0.0","object":{"type":"tree","sha":"bad"}}]',
    ],
)
def test_rejects_untrusted_refs(raw: bytes) -> None:
    with pytest.raises(LifecycleError):
        latest_tag_version(raw)


def test_rejects_metadata_and_count_over_limit() -> None:
    from unittest.mock import patch

    with patch("upstream_version.MAX_METADATA_BYTES", 1), pytest.raises(LifecycleError):
        latest_tag_version(refs("1.0.0"))
    with patch("upstream_version.MAX_TAGS", 1), pytest.raises(LifecycleError):
        latest_tag_version(refs("1.0.0", "2.0.0"))
