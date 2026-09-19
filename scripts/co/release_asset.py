"""Resolve and verify one immutable CLI release without rebuilding its source.

The installer consumes the published archive only after its Git tag, manifest,
checksum file, tar shape, and embedded binary all agree with one source commit.
Python performs fixed-URL downloads and owns the structured validation needed
before the consumer repository may enter its compare-and-swap transaction.
"""

import json
import os
import re
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.request import urlopen

from common import LifecycleError, git, sha256
from package_verification import package_metadata, verified_package

REPOSITORY = "loiang/co"
REPOSITORY_URL = "https://github.com/loiang/co.git"
RELEASE_BASE = "https://github.com/loiang/co/releases/download"
PLATFORM = "x86_64-linux"
TAG_RE = re.compile(r"^co-[0-9]{8}T[0-9]{6}Z-[0-9a-f]{10}$")
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
MAX_MANIFEST_SIZE = 64 * 1024
MAX_CHECKSUM_SIZE = 4 * 1024
MAX_BINARY_SIZE = 1024 * 1024 * 1024


@dataclass(frozen=True)
class Artifact:
    """Describe one downloaded release asset and its verified file identity."""

    name: str
    url: str
    path: Path
    sha256: str
    size: int


@dataclass(frozen=True)
class ReleaseBundle:
    """Bind one CLI archive and producer evidence to an immutable source tag."""

    tag: str
    source_rev: str
    upstream_rev: str
    source_version: str
    platform: str
    cli: Artifact
    manifest: Artifact
    checksums: Artifact
    binary_sha256: str
    binary_size: int
    package: dict[str, Any]

    def evidence(self) -> dict[str, Any]:
        """Return stable release facts without leaking machine-local paths."""

        def details(artifact: Artifact) -> dict[str, str | int]:
            return {
                "name": artifact.name,
                "url": artifact.url,
                "sha256": artifact.sha256,
                "size": artifact.size,
            }

        return {
            "repository": REPOSITORY,
            "releaseTag": self.tag,
            "sourceRev": self.source_rev,
            "upstreamRev": self.upstream_rev,
            "sourceVersion": self.source_version,
            "platform": self.platform,
            "package": self.package,
            "manifest": details(self.manifest),
            "checksums": details(self.checksums),
            "cli": {
                **details(self.cli),
                "binaryPath": self.package["entrypoint"],
                "binarySha256": self.binary_sha256,
                "binarySize": self.binary_size,
            },
        }


def resolve_release_tag(root: Path, reference: str, source_rev: str) -> str:
    """Resolve a main commit to its unique immutable release tag.

    Args:
        root: Checkout used only as the explicit Git command directory.
        reference: Requested source ref, either ``main`` or a release tag.
        source_rev: Exact commit resolved for that source ref.

    Returns:
        The release tag whose peeled commit equals ``source_rev``.

    Raises:
        LifecycleError: The identity is invalid or main lacks one unique tag.
    """
    if SHA_RE.fullmatch(source_rev) is None:
        raise LifecycleError("release source SHA 必须是完整 40 位十六进制")
    if reference != "main":
        if TAG_RE.fullmatch(reference) is None:
            raise LifecycleError(f"Codex release tag 格式无效: {reference}")
        return reference
    output = git(
        root,
        "ls-remote",
        "--tags",
        REPOSITORY_URL,
        "refs/tags/co-*",
    )
    matches = _matching_peeled_tags(output, source_rev)
    if len(matches) != 1:
        raise LifecycleError(
            f"main SHA 必须且只能对应一个 published co release tag，实际 {len(matches)}"
        )
    return matches[0]


def _matching_peeled_tags(output: str, source_rev: str) -> tuple[str, ...]:
    matches: list[str] = []
    for line in output.splitlines():
        fields = line.split()
        if len(fields) != 2 or not fields[1].endswith("^{}"):
            continue
        tag = fields[1].removeprefix("refs/tags/").removesuffix("^{}")
        if fields[0] == source_rev and TAG_RE.fullmatch(tag) is not None:
            matches.append(tag)
    return tuple(sorted(set(matches)))


def _asset_url(tag: str, name: str) -> str:
    return f"{RELEASE_BASE}/{tag}/{name}"


def _prefetch(root: Path, tag: str, name: str) -> Artifact:
    url = _asset_url(tag, name)
    directory = root / ".states/co/downloads" / tag
    directory.mkdir(parents=True, exist_ok=True)
    limits = {"SHA256SUMS": MAX_CHECKSUM_SIZE}
    limit = limits.get(
        name, MAX_MANIFEST_SIZE if name.endswith(".json") else MAX_BINARY_SIZE * 4
    )
    try:
        with tempfile.NamedTemporaryFile(dir=directory, delete=False) as output:
            temporary = Path(output.name)
            with urlopen(url, timeout=60) as response:
                size = 0
                while block := response.read(1024 * 1024):
                    size += len(block)
                    if size > limit:
                        raise LifecycleError(f"release asset 超过安全大小限制: {name}")
                    output.write(block)
            output.flush()
            os.fsync(output.fileno())
        digest = sha256(temporary)
        path = directory / f"{digest}-{name}"
        temporary.replace(path)
    except OSError as error:
        raise LifecycleError(f"release asset 下载失败: {name}") from error
    finally:
        if "temporary" in locals():
            temporary.unlink(missing_ok=True)
    return Artifact(name, url, path, digest, size)


def _parse_checksums(path: Path, expected_names: set[str]) -> dict[str, str]:
    if path.stat().st_size > MAX_CHECKSUM_SIZE:
        raise LifecycleError("SHA256SUMS 超过安全大小限制")
    values: dict[str, str] = {}
    pattern = re.compile(r"([0-9a-f]{64})  ([A-Za-z0-9][A-Za-z0-9._-]*)")
    for line in path.read_text(encoding="utf-8").splitlines():
        match = pattern.fullmatch(line)
        if match is None or match[2] in values:
            raise LifecycleError("SHA256SUMS 格式、路径或重复项无效")
        values[match[2]] = match[1]
    if set(values) != expected_names:
        raise LifecycleError("SHA256SUMS 必须精确覆盖 manifest 与 CLI archive")
    return values


def _load_manifest(path: Path) -> dict[str, Any]:
    if path.stat().st_size > MAX_MANIFEST_SIZE:
        raise LifecycleError("release manifest 超过安全大小限制")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise LifecycleError("release manifest 不是有效 JSON") from error
    if not isinstance(payload, dict):
        raise LifecycleError("release manifest 必须是 object")
    return payload


def _manifest_identity(
    payload: dict[str, Any],
    source_rev: str,
    archive_name: str,
    platform: str = PLATFORM,
) -> tuple[str, str, str]:
    checksums = payload.get("checksums")
    metadata = package_metadata(payload)
    entrypoint = metadata["entrypoint"]
    expected_keys = {entrypoint, archive_name}
    valid = (
        payload.get("schemaVersion") == 2
        and payload.get("repository") == REPOSITORY
        and payload.get("sourceRev") == source_rev
        and payload.get("platform") == platform
        and SHA_RE.fullmatch(str(payload.get("upstreamRev", ""))) is not None
        and isinstance(payload.get("sourceVersion"), str)
        and bool(payload.get("sourceVersion"))
        and isinstance(checksums, dict)
        and set(checksums) == expected_keys
        and all(SHA256_RE.fullmatch(str(value)) for value in checksums.values())
    )
    if not valid:
        raise LifecycleError("release manifest identity 或 checksum schema 不一致")
    return (
        str(payload["upstreamRev"]),
        str(payload["sourceVersion"]),
        str(checksums[entrypoint]),
    )


def fetch_release_bundle(root: Path, tag: str, source_rev: str) -> ReleaseBundle:
    """Download and cross-check all producer evidence for one CLI release.

    Args:
        root: Checkout owning the verified download cache.
        tag: Exact published release tag.
        source_rev: Expected peeled Git commit for that tag.

    Returns:
        A release bundle safe to inspect and pass through the host gate.

    Raises:
        LifecycleError: Any tag, manifest, checksum, or asset identity differs.
    """
    if TAG_RE.fullmatch(tag) is None or SHA_RE.fullmatch(source_rev) is None:
        raise LifecycleError("release tag 或 source SHA 格式无效")
    if not tag.endswith(f"-{source_rev[:10]}"):
        raise LifecycleError("release tag 与 source SHA 不一致")
    short_rev = source_rev[:10]
    archive_name = f"co-cli-{PLATFORM}-{short_rev}.tar.gz"
    manifest_name = f"co-manifest-{short_rev}.json"
    sums = _prefetch(root, tag, "SHA256SUMS")
    expected = _parse_checksums(sums.path, {archive_name, manifest_name})
    manifest = _prefetch(root, tag, manifest_name)
    if manifest.sha256 != expected[manifest_name]:
        raise LifecycleError("release manifest SHA-256 与 SHA256SUMS 不一致")
    payload = _load_manifest(manifest.path)
    upstream_rev, version, binary_sha = _manifest_identity(
        payload, source_rev, archive_name
    )
    cli = _prefetch(root, tag, archive_name)
    archive_sha = str(payload["checksums"][archive_name])
    if cli.sha256 != expected[archive_name] or cli.sha256 != archive_sha:
        raise LifecycleError("CLI archive SHA-256 在 manifest/SHA256SUMS 间不一致")
    metadata = package_metadata(payload)
    with verified_package(cli.path, metadata, binary_sha) as directory:
        binary_size = (directory / metadata["entrypoint"]).stat().st_size
    return ReleaseBundle(
        tag,
        source_rev,
        upstream_rev,
        version,
        PLATFORM,
        cli,
        manifest,
        sums,
        binary_sha,
        binary_size,
        metadata,
    )


@contextmanager
def verified_cli(bundle: ReleaseBundle) -> Iterator[Path]:
    """Materialize the verified entrypoint alongside all required package resources."""
    if sha256(bundle.cli.path) != bundle.cli.sha256:
        raise LifecycleError("CLI archive SHA-256 在下载后发生变化")
    with verified_package(
        bundle.cli.path, bundle.package, bundle.binary_sha256
    ) as directory:
        binary = directory / bundle.package["entrypoint"]
        if binary.stat().st_size != bundle.binary_size:
            raise LifecycleError("CLI binary size 与 archive metadata 不一致")
        yield binary
