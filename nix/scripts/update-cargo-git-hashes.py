#!/usr/bin/env python3
"""Refresh Nix fixed-output hashes for git dependencies in Cargo.lock.

The Nix source build imports Cargo.lock directly, but Nix additionally requires
one recursive source hash for each git revision. This updater derives that map
from the lock file and atomically rewrites the checked-in Nix attribute set.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import tomllib
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit


@dataclass(frozen=True, order=True)
class GitSource:
    """Identify one immutable git checkout shared by one or more crates."""

    url: str
    revision: str


def _parse_git_source(raw_source: str) -> GitSource:
    source, separator, revision = raw_source.removeprefix("git+").rpartition("#")
    if not separator or not revision:
        raise ValueError(f"git dependency has no locked revision: {raw_source}")
    parsed = urlsplit(source)
    url = urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))
    return GitSource(url=url, revision=revision)


def _locked_git_packages(lock_file: Path) -> dict[str, GitSource]:
    with lock_file.open("rb") as handle:
        lock = tomllib.load(handle)

    packages: dict[str, GitSource] = {}
    for package in lock.get("package", []):
        source = package.get("source", "")
        if not source.startswith("git+"):
            continue
        name_version = f"{package['name']}-{package['version']}"
        git_source = _parse_git_source(source)
        previous = packages.setdefault(name_version, git_source)
        if previous != git_source:
            raise ValueError(f"ambiguous git dependency key: {name_version}")
    if not packages:
        raise ValueError(f"no git dependencies found in {lock_file}")
    return packages


def _prefetch(source: GitSource) -> str:
    completed = subprocess.run(
        [
            "nix-prefetch-git",
            "--quiet",
            "--url",
            source.url,
            "--rev",
            source.revision,
        ],
        check=True,
        stdout=subprocess.PIPE,
        text=True,
    )
    result = json.loads(completed.stdout)
    raw_hash = result.get("hash") or result["sha256"]
    if raw_hash.startswith("sha256-"):
        return raw_hash
    return subprocess.run(
        ["nix", "hash", "convert", "--hash-algo", "sha256", "--to", "sri", raw_hash],
        check=True,
        stdout=subprocess.PIPE,
        text=True,
    ).stdout.strip()


def _render_hashes(packages: dict[str, GitSource]) -> str:
    hashes_by_source = {source: _prefetch(source) for source in set(packages.values())}
    lines = ["{"]
    for name_version, source in sorted(packages.items()):
        lines.append(
            f"  {json.dumps(name_version)} = {json.dumps(hashes_by_source[source])};"
        )
    lines.append("}")
    return "\n".join(lines) + "\n"


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", delete=False
    ) as handle:
        temporary = Path(handle.name)
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())
    try:
        temporary.chmod(0o644)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def main() -> int:
    """Refresh the repository's git dependency hashes from its Cargo lock."""
    repository = Path(__file__).resolve().parents[2]
    lock_file = repository / "codex-rs" / "Cargo.lock"
    output = repository / "nix" / "cargo-git-hashes.nix"
    _atomic_write(output, _render_hashes(_locked_git_packages(lock_file)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
