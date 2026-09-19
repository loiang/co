"""Resolve the native build tools used by the source package builder.

The native package path intentionally does not invoke Nix.  On a NixOS host,
an already-installed GNU Make in the read-only store is still a valid build
input when the interactive environment did not expose ``make`` in ``PATH``.
"""

import os
import platform
import re
import shutil
import stat
import subprocess
from pathlib import Path
from typing import Mapping

from common import LifecycleError

_GNU_MAKE_NAME = re.compile(
    r"^[^-]+-gnumake-(?P<version>\d+(?:\.\d+){1,2})(?:[-+].*)?$"
)
_DEFAULT_STORE_ROOT = Path("/nix/store")


def resolve_make_bin(
    env: Mapping[str, str] | None = None,
    *,
    store_root: Path = _DEFAULT_STORE_ROOT,
    platform_name: str | None = None,
) -> Path:
    """Find a verified GNU Make executable for the native builder.

    The caller supplies the child environment so tests and the builder use
    the same lookup rules.  An executable already exposed by ``PATH`` wins;
    only on Linux is the existing read-only Nix store searched as a fallback.
    ``store_root`` is injectable to keep tests independent of the host store.

    Args:
        env: Child environment whose ``PATH`` should be searched.
        store_root: Nix store root used for the Linux fallback.
        platform_name: Optional platform name override for isolated tests.

    Returns:
        The verified ``make`` executable path.

    Raises:
        LifecycleError: No verified GNU Make executable is available.
    """
    child_env = dict(env or os.environ)
    path_make = shutil.which("make", path=child_env.get("PATH"))
    if path_make is not None:
        candidate = Path(path_make).resolve()
        if _is_executable(candidate) and _is_gnu_make(candidate, child_env):
            return candidate

    if (platform_name or platform.system()) != "Linux":
        raise LifecycleError(
            "native Codex build 需要 GNU Make；PATH 中未找到可用的 make"
        )

    candidates = _store_candidates(store_root)
    for candidate in sorted(
        candidates,
        key=lambda path: (_version_key(path), str(path.resolve())),
        reverse=True,
    ):
        if _is_executable(candidate) and _is_gnu_make(candidate, child_env):
            return candidate.resolve()

    raise LifecycleError(
        "native Codex build 需要 GNU Make；PATH 和 Nix store 中均未找到"
        "可验证的 make（请提供 PATH 中的 GNU Make）"
    )


def prepend_tool_path(env: dict[str, str], executable: Path) -> None:
    """Prepend a verified tool directory to a child process environment.

    This mutates only the environment dictionary owned by the caller, which
    keeps the selected tool visible to both the official builder and Cargo.

    Args:
        env: Complete child environment to update.
        executable: Verified executable whose parent should be preferred.
    """
    current = env.get("PATH", "")
    env["PATH"] = os.pathsep.join(
        part for part in (str(executable.parent), current) if part
    )


def _store_candidates(store_root: Path) -> list[Path]:
    if not store_root.is_absolute():
        raise LifecycleError(f"Nix store root 必须是绝对路径: {store_root}")
    if not store_root.is_dir() or store_root.is_symlink():
        return []
    root = store_root.resolve()
    return [
        path
        for path in root.glob("*-gnumake-*/bin/make")
        if _inside_store(path, root) and _version_key(path) is not None
    ]


def _inside_store(path: Path, store_root: Path) -> bool:
    try:
        resolved = path.resolve(strict=True)
        return resolved.is_relative_to(store_root) and not any(
            part.is_symlink() for part in _parents_between(resolved, store_root)
        )
    except OSError:
        return False


def _parents_between(path: Path, root: Path) -> list[Path]:
    parents: list[Path] = []
    current = path.parent
    while current != root and root in current.parents:
        parents.append(current)
        current = current.parent
    return parents


def _version_key(path: Path) -> tuple[int, ...] | None:
    match = _GNU_MAKE_NAME.fullmatch(path.parent.parent.name)
    if match is None:
        return None
    version = tuple(int(part) for part in match.group("version").split("."))
    return version + (0,) * (3 - len(version))


def _is_executable(path: Path) -> bool:
    try:
        mode = path.stat().st_mode
        return path.is_file() and bool(
            mode & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        )
    except OSError:
        return False


def _is_gnu_make(path: Path, env: Mapping[str, str]) -> bool:
    try:
        result = subprocess.run(
            [str(path), "--version"],
            check=False,
            capture_output=True,
            text=True,
            env=dict(env),
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    first_line = (result.stdout or "").splitlines()[:1]
    return result.returncode == 0 and bool(first_line) and first_line[0].startswith(
        "GNU Make "
    )
