"""Codex-built V8 artifact overrides for package Cargo builds."""

from __future__ import annotations

import hashlib
import os
import socket
import shutil
import tempfile
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import urlopen

from .targets import REPO_ROOT, TargetSpec

DOWNLOAD_TIMEOUT_SECS = 120
MAX_DOWNLOAD_ATTEMPTS = 3
RETRY_BACKOFF_SECS = 0.25
V8_ARTIFACT_PROFILE = "ptrcomp_sandbox_release"

Sleep = Callable[[float], None]


@dataclass(frozen=True)
class RustyV8ArtifactPair:
    archive: Path
    binding: Path


def resolve_codex_v8_cargo_env(
    spec: TargetSpec,
    *,
    environ: Mapping[str, str] | None = None,
    cache_root: Path | None = None,
) -> dict[str, str]:
    """Returns Cargo overrides for the verified Codex-built V8 artifacts."""
    environ = os.environ if environ is None else environ
    if environ.get("V8_FROM_SOURCE") in {"true", "1", "yes"}:
        return {}

    archive_override = environ.get("RUSTY_V8_ARCHIVE")
    binding_override = environ.get("RUSTY_V8_SRC_BINDING_PATH")
    if archive_override and binding_override:
        return {}
    if archive_override or binding_override:
        raise RuntimeError(
            "Cargo package builds need RUSTY_V8_ARCHIVE and RUSTY_V8_SRC_BINDING_PATH set together."
        )

    artifacts = fetch_codex_v8_artifacts(spec, cache_root=cache_root)
    return {
        "RUSTY_V8_ARCHIVE": str(artifacts.archive),
        "RUSTY_V8_SRC_BINDING_PATH": str(artifacts.binding),
    }


def fetch_codex_v8_artifacts(
    spec: TargetSpec,
    *,
    version: str | None = None,
    cache_root: Path | None = None,
    sleep: Sleep | None = None,
    max_attempts: int = MAX_DOWNLOAD_ATTEMPTS,
) -> RustyV8ArtifactPair:
    """Loads a pinned V8 artifact pair, downloading only invalid cache entries.

    The checksum manifest is authenticated by a repository pin before any
    network operation. Complete cache hits therefore stay offline, while every
    downloaded file is validated in a unique sibling temporary file before it
    can replace a formal cache entry.
    """
    if not 1 <= max_attempts <= MAX_DOWNLOAD_ATTEMPTS:
        raise ValueError(f"max_attempts must be between 1 and {MAX_DOWNLOAD_ATTEMPTS}")
    sleep = time.sleep if sleep is None else sleep
    version = version or resolved_v8_crate_version()
    release_url = (
        f"https://github.com/openai/codex/releases/download/rusty-v8-v{version}"
    )
    target = spec.target
    cache_dir = (cache_root or default_cache_root()) / f"rusty-v8-{version}-{target}"

    if spec.is_windows:
        archive_name = f"rusty_v8_{V8_ARTIFACT_PROFILE}_{target}.lib.gz"
    else:
        archive_name = f"librusty_v8_{V8_ARTIFACT_PROFILE}_{target}.a.gz"
    binding_name = f"src_binding_{V8_ARTIFACT_PROFILE}_{target}.rs"
    checksums_name = f"rusty_v8_{V8_ARTIFACT_PROFILE}_{target}.sha256"

    archive = cache_dir / archive_name
    binding = cache_dir / binding_name
    checksums = cache_dir / checksums_name

    trusted_manifest_checksum(checksums.name, version=version)
    expected_checksums = _cached_checksums(
        checksums, version=version, artifact_names={archive.name, binding.name}
    )
    if expected_checksums is None:
        _download_verified(
            f"{release_url}/{checksums.name}",
            checksums,
            lambda path: _validate_manifest(
                path,
                version=version,
                manifest_name=checksums.name,
                artifact_names={archive.name, binding.name},
            ),
            sleep=sleep,
            max_attempts=max_attempts,
        )
        expected_checksums = load_checksums(checksums, {archive.name, binding.name})
    for artifact in [archive, binding]:
        ensure_valid_artifact(
            artifact,
            expected_checksums[artifact.name],
            f"{release_url}/{artifact.name}",
            sleep=sleep,
            max_attempts=max_attempts,
        )

    return RustyV8ArtifactPair(archive=archive, binding=binding)


def resolved_v8_crate_version() -> str:
    """Reads the single V8 crate version resolved by the repository lockfile."""
    import tomllib

    cargo_lock = tomllib.loads((REPO_ROOT / "codex-rs" / "Cargo.lock").read_text())
    versions = sorted(
        {
            package["version"]
            for package in cargo_lock["package"]
            if package["name"] == "v8"
        }
    )
    if len(versions) != 1:
        raise RuntimeError(
            f"Expected exactly one resolved v8 version, found: {versions}"
        )
    return versions[0]


def default_cache_root() -> Path:
    """Returns the process-shared cache root used by package builds."""
    return Path(tempfile.gettempdir()) / "codex-package"


def verify_release_checksum_manifest(checksums_path: Path, *, version: str) -> None:
    """Verifies a downloaded manifest against the repository's trusted pin."""
    expected = trusted_manifest_checksum(checksums_path.name, version=version)
    if not has_checksum(checksums_path, expected):
        raise RuntimeError(
            f"V8 checksum manifest {checksums_path} does not match its trusted SHA-256."
        )


def trusted_manifest_checksum(name: str, *, version: str) -> str:
    """Returns the pinned digest for a release manifest or raises if absent."""
    version_suffix = version.replace(".", "_")
    trusted_checksums = (
        REPO_ROOT
        / "third_party"
        / "v8"
        / f"rusty_v8_{version_suffix}_release_manifests.sha256"
    )

    for line in trusted_checksums.read_text(encoding="utf-8").splitlines():
        parts = line.split(maxsplit=1)
        if len(parts) != 2:
            raise RuntimeError(f"Invalid trusted V8 checksum line: {line!r}")
        digest, artifact_name = parts
        if artifact_name != name:
            continue
        if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
            raise RuntimeError(f"Invalid trusted V8 checksum digest: {digest}")
        return digest

    raise RuntimeError(
        f"V8 checksum manifest {name} has no trusted SHA-256 for {version}."
    )


def _cached_checksums(
    checksums_path: Path, *, version: str, artifact_names: set[str]
) -> dict[str, str] | None:
    try:
        verify_release_checksum_manifest(checksums_path, version=version)
        return load_checksums(checksums_path, artifact_names)
    except (FileNotFoundError, RuntimeError):
        return None


def _validate_manifest(
    path: Path,
    *,
    version: str,
    manifest_name: str,
    artifact_names: set[str],
) -> None:
    try:
        expected = trusted_manifest_checksum(manifest_name, version=version)
        if not has_checksum(path, expected):
            raise RuntimeError(
                f"V8 checksum manifest {path} does not match its trusted SHA-256."
            )
        load_checksums(path, artifact_names)
    except (FileNotFoundError, RuntimeError) as error:
        raise _DownloadedValidationError(str(error)) from error


def load_checksums(checksums_path: Path, artifact_names: set[str]) -> dict[str, str]:
    """Parses an exact two-entry release checksum manifest."""
    checksums: dict[str, str] = {}
    lines = checksums_path.read_text(encoding="utf-8").splitlines()
    if len(lines) != len(artifact_names):
        raise RuntimeError(
            f"Expected {len(artifact_names)} V8 checksums in {checksums_path}, found {len(lines)}."
        )

    for line in lines:
        parts = line.split(maxsplit=1)
        if len(parts) != 2:
            raise RuntimeError(
                f"Invalid V8 checksum line in {checksums_path}: {line!r}"
            )

        digest, artifact_name = parts[0], parts[1].strip()
        if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
            raise RuntimeError(
                f"Invalid V8 checksum digest in {checksums_path}: {digest}"
            )
        if artifact_name not in artifact_names:
            raise RuntimeError(
                f"Unexpected V8 checksum artifact in {checksums_path}: {artifact_name}"
            )
        checksums[artifact_name] = digest

    if checksums.keys() != artifact_names:
        raise RuntimeError(
            f"V8 checksum manifest {checksums_path} does not cover {artifact_names}."
        )
    return checksums


def ensure_valid_artifact(
    artifact: Path,
    checksum: str,
    url: str,
    *,
    sleep: Sleep | None = None,
    max_attempts: int = MAX_DOWNLOAD_ATTEMPTS,
) -> None:
    """Keeps a valid artifact or atomically replaces it with a verified download."""
    if has_checksum(artifact, checksum):
        return

    _download_verified(
        url,
        artifact,
        lambda path: _validate_artifact(path, checksum),
        sleep=time.sleep if sleep is None else sleep,
        max_attempts=max_attempts,
    )


class _DownloadedValidationError(RuntimeError):
    """Marks a downloaded file that failed a trusted content validation."""


def _validate_artifact(path: Path, checksum: str) -> None:
    if not has_checksum(path, checksum):
        raise _DownloadedValidationError(
            "Codex-built V8 artifact failed checksum validation."
        )


def _download_verified(
    url: str,
    destination: Path,
    validator: Callable[[Path], None],
    *,
    sleep: Sleep,
    max_attempts: int,
) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    last_error: Exception | None = None
    for attempt in range(max_attempts):
        temporary = _temporary_path(destination)
        try:
            _download_once(url, temporary)
            validator(temporary)
            temporary.replace(destination)
            return
        except Exception as error:
            last_error = error
            if not _retryable(error) or attempt == max_attempts - 1:
                raise
            sleep(RETRY_BACKOFF_SECS * (2**attempt))
        finally:
            temporary.unlink(missing_ok=True)
    assert last_error is not None
    raise last_error


def _temporary_path(destination: Path) -> Path:
    descriptor, path = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent
    )
    os.close(descriptor)
    return Path(path)


def _download_once(url: str, destination: Path) -> None:
    with urlopen(url, timeout=DOWNLOAD_TIMEOUT_SECS) as response:
        with destination.open("wb") as output:
            shutil.copyfileobj(response, output)


def _retryable(error: Exception) -> bool:
    if isinstance(error, HTTPError):
        return error.code in {408, 429} or 500 <= error.code <= 599
    return isinstance(
        error,
        (
            _DownloadedValidationError,
            TimeoutError,
            socket.timeout,
            ConnectionResetError,
            URLError,
        ),
    )


def has_checksum(path: Path, expected: str) -> bool:
    """Checks a regular file's SHA-256 without changing it."""
    if not path.is_file():
        return False

    digest = hashlib.sha256()
    with path.open("rb") as artifact:
        for chunk in iter(lambda: artifact.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest() == expected


def download_file(url: str, dest: Path) -> None:
    """Downloads an unvalidated file using the same bounded atomic transport."""
    _download_verified(
        url,
        dest,
        lambda _path: None,
        sleep=time.sleep,
        max_attempts=MAX_DOWNLOAD_ATTEMPTS,
    )
