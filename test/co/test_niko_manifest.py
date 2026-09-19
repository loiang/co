"""Contract tests for the repository-owned Niko lifecycle manifest."""

import tomllib
from pathlib import Path


ROOT = Path(__file__).parents[2]
CAPABILITIES = {
    "upgrade",
    "upgrade-finalize",
    "promote",
    "test",
    "build",
    "test-host",
    "publish",
    "install",
}


def test_manifest_exposes_only_fork_lifecycle_capabilities() -> None:
    """Keep the public Niko surface limited to the fork-owned lifecycle."""
    manifest = tomllib.loads((ROOT / "niko.toml").read_text(encoding="utf-8"))

    assert manifest["schema_version"] == 2
    assert set(manifest["capabilities"]) == CAPABILITIES
    assert manifest["nix_develop"]["flake"] == "."
    assert manifest["nix_develop"]["profile"] == ".nix-develop"


def test_capabilities_dispatch_to_the_repository_cli() -> None:
    """Every owner command must resolve against the selected checkout."""
    manifest = tomllib.loads((ROOT / "niko.toml").read_text(encoding="utf-8"))

    for name, capability in manifest["capabilities"].items():
        assert capability["program"] == "python3"
        assert capability["cwd"] == "repo"
        assert capability["args"] == [
            "scripts/co/cli.py",
            name,
            "--repo",
            ".",
        ]


def test_publish_and_install_accept_explicit_owner_passthrough() -> None:
    """Owner preflight flags cross only the explicit argv boundary."""
    manifest = tomllib.loads((ROOT / "niko.toml").read_text(encoding="utf-8"))

    for name in ("publish", "install"):
        parameter = manifest["capabilities"][name]["parameters"][-1]
        assert parameter["id"] == "args"
        assert parameter["kind"] == "positional"
        assert parameter["repeatable"] is True

    install_args = manifest["capabilities"]["install"]["parameters"][-1]
    assert "TAG" in install_args["help"]
