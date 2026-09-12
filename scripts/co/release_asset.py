"""Resolve and verify one immutable CLI release without rebuilding its source.

The installer consumes the published archive only after its Git tag, manifest,
checksum file, tar shape, and embedded binary all agree with one source commit.
Nix performs fixed-URL downloads; Python owns the structured validation needed
before the consumer repository may enter its compare-and-swap transaction.
"""

import json
import re
import shutil
import tarfile
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from common import LifecycleError, git, run, sha256

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
            "manifest": details(self.manifest),
            "checksums": details(self.checksums),
            "cli": {
                **details(self.cli),
                "binaryPath": "codex",
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
    result = run(
        ["nix", "store", "prefetch-file", "--json", url],
        cwd=root,
    )
    try:
        payload = json.loads(result.stdout or "")
        path = Path(payload["storePath"])
    except (json.JSONDecodeError, KeyError, TypeError) as error:
        raise LifecycleError(f"Nix prefetch 未返回有效 store path: {name}") from error
    if not path.is_file():
        raise LifecycleError(f"release asset 不是 regular file: {name}")
    return Artifact(name, url, path, sha256(path), path.stat().st_size)


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
    payload: dict[str, Any], source_rev: str, archive_name: str
) -> tuple[str, str, str]:
    checksums = payload.get("checksums")
    expected_keys = {"codex", archive_name}
    valid = (
        payload.get("schemaVersion") == 1
        and payload.get("repository") == REPOSITORY
        and payload.get("sourceRev") == source_rev
        and payload.get("platform") == PLATFORM
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
        str(checksums["codex"]),
    )


def fetch_release_bundle(root: Path, tag: str, source_rev: str) -> ReleaseBundle:
    """Download and cross-check all producer evidence for one CLI release.

    Args:
        root: Checkout used as the explicit Nix command directory.
        tag: Exact published release tag.
        source_rev: Expected peeled Git commit for that tag.

    Returns:
        A release bundle safe to inspect and pass through the host gate.

    Raises:
        LifecycleError: Any tag, manifest, checksum, or asset identity differs.
    """
    if TAG_RE.fullmatch(tag) is None or SHA_RE.fullmatch(source_rev) is None:
        raise LifecycleError("release tag 或 source SHA 格式无效")
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
    binary_size = _archive_binary_size(cli.path)
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
    )


def _archive_member(archive: tarfile.TarFile) -> tarfile.TarInfo:
    members = archive.getmembers()
    if (
        len(members) != 1
        or members[0].name != "codex"
        or not members[0].isreg()
        or not 0 < members[0].size <= MAX_BINARY_SIZE
    ):
        raise LifecycleError("CLI archive 必须只含安全路径下的单一 regular codex")
    return members[0]


def _archive_binary_size(archive_path: Path) -> int:
    try:
        with tarfile.open(archive_path, mode="r:gz") as archive:
            return _archive_member(archive).size
    except (OSError, tarfile.TarError) as error:
        raise LifecycleError("CLI archive 无法安全读取") from error


@contextmanager
def verified_cli(bundle: ReleaseBundle) -> Iterator[Path]:
    """Materialize the one verified binary for a bounded compatibility test.

    Args:
        bundle: Cross-checked release metadata and archive.

    Yields:
        Temporary executable path removed after the caller's host gate.

    Raises:
        LifecycleError: Tar shape, decompression, or binary digest is invalid.
    """
    try:
        with tempfile.TemporaryDirectory(prefix="co-release-") as directory:
            target = Path(directory) / "codex"
            with tarfile.open(bundle.cli.path, mode="r:gz") as archive:
                source = archive.extractfile(_archive_member(archive))
                if source is None:
                    raise LifecycleError("CLI archive regular member 无法读取")
                with source, target.open("wb") as output:
                    shutil.copyfileobj(source, output, length=1024 * 1024)
            target.chmod(0o755)
            if target.stat().st_size != bundle.binary_size:
                raise LifecycleError("CLI binary size 与 archive metadata 不一致")
            if sha256(target) != bundle.binary_sha256:
                raise LifecycleError("CLI binary SHA-256 与 manifest 不一致")
            yield target
    except (OSError, tarfile.TarError) as error:
        raise LifecycleError("CLI archive 解包验证失败") from error
