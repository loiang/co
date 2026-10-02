"""Create real standalone archives for producer and consumer evidence tests."""

import tarfile
from pathlib import Path

from common import sha256, write_json
from native_fixtures import BINARY

VERSION = "0.162.0-alpha.4"
DESCRIPTOR = {
    "kind": "standalone-cli",
    "version": VERSION,
    "target": "x86_64-unknown-linux-gnu",
    "entrypoint": "codex",
}


def make_assets(root: Path, source: str) -> tuple[Path, ...]:
    """Bind a single executable to its schema 3 manifest and checksum file."""
    directory = root / "artifact"
    directory.mkdir(parents=True)
    binary = directory / "codex"
    binary.write_bytes(BINARY)
    binary.chmod(0o755)
    archive = root / f"co-cli-x86_64-linux-{source[:10]}.tar.gz"
    with tarfile.open(archive, "w:gz") as output:
        output.add(binary, arcname="codex")
    manifest = root / f"co-manifest-{source[:10]}.json"
    write_json(
        manifest,
        {
            "schemaVersion": 3,
            "repository": "loiang/co",
            "sourceRev": source,
            "sourceVersion": VERSION,
            "platform": "x86_64-linux",
            "artifact": DESCRIPTOR,
            "checksums": {"codex": sha256(binary), archive.name: sha256(archive)},
        },
    )
    sums = root / "SHA256SUMS"
    sums.write_text(
        f"{sha256(archive)}  {archive.name}\n{sha256(manifest)}  {manifest.name}\n",
        encoding="utf-8",
    )
    return archive, manifest, sums
