"""Model immutable release assets without coupling them to a packaging layout."""

from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class Artifact:
    """Describe downloaded bytes and their verified file identity."""

    name: str
    url: str
    path: Path
    sha256: str
    size: int


@dataclass(frozen=True)
class ReleaseBundle:
    """Keep legacy package and standalone descriptors explicitly distinguishable."""

    tag: str
    source_rev: str
    source_version: str
    platform: str
    cli: Artifact
    manifest: Artifact
    checksums: Artifact
    binary_sha256: str
    binary_size: int
    package: dict[str, Any] | None
    artifact: dict[str, Any] | None = None

    def evidence(self) -> dict[str, Any]:
        """Return stable release facts without machine-local download paths."""
        descriptor = self.artifact if self.artifact is not None else self.package
        if descriptor is None:
            raise ValueError("release bundle lacks artifact descriptor")
        values = {
            "repository": "loiang/co",
            "releaseTag": self.tag,
            "sourceRev": self.source_rev,
            "sourceVersion": self.source_version,
            "platform": self.platform,
            "artifact" if self.artifact is not None else "package": descriptor,
            "manifest": _details(self.manifest),
            "checksums": _details(self.checksums),
            "cli": {
                **_details(self.cli),
                "binaryPath": descriptor["entrypoint"],
                "binarySha256": self.binary_sha256,
                "binarySize": self.binary_size,
            },
        }
        return values


def _details(artifact: Artifact) -> dict[str, str | int]:
    return {
        "name": artifact.name,
        "url": artifact.url,
        "sha256": artifact.sha256,
        "size": artifact.size,
    }
