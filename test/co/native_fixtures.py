"""Build canonical package fixtures with real files and archive/checksum evidence."""

import hashlib
import tarfile
from pathlib import Path

from common import sha256, write_json

BINARY = b"verified-codex-binary"
VERSION = "0.155.1"
METADATA = {
    "layoutVersion": 1,
    "version": VERSION,
    "target": "x86_64-unknown-linux-gnu",
    "variant": "codex",
    "entrypoint": "bin/codex",
    "resourcesDir": "codex-resources",
    "pathDir": "codex-path",
}


def make_package(root: Path) -> Path:
    """Materialize every Linux resource required by the upstream validator."""
    package = root / "package"
    for name in (
        "bin/codex",
        "bin/codex-code-mode-host",
        "codex-path/rg",
        "codex-resources/bwrap",
        "codex-resources/zsh/bin/zsh",
    ):
        path = package / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(BINARY if name == "bin/codex" else name.encode())
        path.chmod(0o755)
    write_json(package / "codex-package.json", METADATA)
    return package


def make_assets(root: Path, source: str, upstream: str) -> tuple[Path, ...]:
    """Emit a real v2 manifest and matching complete package archive."""
    package = make_package(root)
    archive = root / f"co-cli-x86_64-linux-{source[:10]}.tar.gz"
    with tarfile.open(archive, "w:gz") as output:
        for path in sorted(package.rglob("*")):
            output.add(path, arcname=path.relative_to(package), recursive=False)
    manifest = root / f"co-manifest-{source[:10]}.json"
    write_json(
        manifest,
        {
            "schemaVersion": 2,
            "repository": "loiang/co",
            "sourceRev": source,
            "upstreamRev": upstream,
            "sourceVersion": VERSION,
            "platform": "x86_64-linux",
            "package": METADATA,
            "checksums": {
                "bin/codex": hashlib.sha256(BINARY).hexdigest(),
                archive.name: sha256(archive),
            },
        },
    )
    sums = root / "SHA256SUMS"
    sums.write_text(
        f"{sha256(archive)}  {archive.name}\n{sha256(manifest)}  {manifest.name}\n",
        encoding="utf-8",
    )
    return archive, manifest, sums
