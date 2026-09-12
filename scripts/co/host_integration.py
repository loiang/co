"""Verify candidate app-server against the official host pinned by ni."""

import json
import os
from pathlib import Path
from typing import Any

from common import (
    LifecycleError,
    git_flake,
    require_repo,
    run,
    sha256,
    timestamp,
    write_json,
)
from evidence import read_build_record, source_identity


def build_official_host(root: Path, ni_repository: Path) -> str:
    result = run(
        [
            "nix",
            "build",
            git_flake(ni_repository, "codex-code-mode-host"),
            "--no-link",
            "--print-out-paths",
            "--no-write-lock-file",
        ],
        cwd=root,
    )
    outputs = tuple(line for line in (result.stdout or "").splitlines() if line)
    if len(outputs) != 1:
        raise LifecycleError("official Code Mode host build 未返回单一 store path")
    return outputs[0]


def official_host_lock(ni_repository: Path) -> dict[str, Any]:
    lock_path = ni_repository.resolve() / "flake.lock"
    try:
        lock = json.loads(lock_path.read_text(encoding="utf-8"))
        node_name = lock["nodes"]["root"]["inputs"]["codeModeHostRelease"]
        if not isinstance(node_name, str):
            raise TypeError("codeModeHostRelease input is not a direct node")
        locked = lock["nodes"][node_name]["locked"]
    except (OSError, KeyError, TypeError, json.JSONDecodeError) as error:
        raise LifecycleError("无法解析 ni official host lock identity") from error
    if not isinstance(locked, dict) or "narHash" not in locked:
        raise LifecycleError("ni official host lock 缺少 narHash")
    return {"flakeLockSha256": sha256(lock_path), "locked": locked}


def test_host_integration(repository: Path, ni_repository: Path) -> Path:
    """Run candidate app-server through a real separately built official host."""
    root = require_repo(repository)
    resolved_ni = require_repo(ni_repository)
    identity = source_identity(root)
    build_record = read_build_record(root, identity)
    host_store = build_official_host(root, resolved_ni)
    host_binary = Path(host_store) / "bin/codex-code-mode-host"
    codex_binary = Path(str(build_record["storePath"])) / "bin/codex"
    command = test_candidate_binaries(root, codex_binary, host_binary)
    record = {
        "schemaVersion": 1,
        **identity,
        "completedAt": timestamp(),
        "officialHostStorePath": host_store,
        "officialHostSha256": sha256(host_binary),
        "officialHostLock": official_host_lock(resolved_ni),
        "transports": ["grpc", "stdio"],
        "command": command,
    }
    latest = root / ".states/co/host-integration/latest.json"
    write_json(latest, record)
    return latest


def test_candidate_binaries(
    root: Path, codex_binary: Path, host_binary: Path
) -> list[str]:
    """Exercise exact candidate and official-host binaries through app-server."""
    environment = os.environ.copy()
    environment["CODEX_BIN"] = str(codex_binary)
    environment["CODEX_CODE_MODE_HOST_BIN"] = str(host_binary)
    command = [
        "nix",
        "develop",
        git_flake(root),
        "--no-update-lock-file",
        "--command",
        "python3",
        "-m",
        "pytest",
        "-q",
        "test/co/test_code_mode_host_integration.py",
    ]
    run(command, cwd=root, capture=False, env=environment)
    return command
