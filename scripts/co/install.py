"""Validate one released CLI, update ni with CAS, build, and explicitly switch."""

import json
import re
from pathlib import Path
from typing import Any

from common import (
    LifecycleError,
    git,
    git_flake,
    head,
    require_clean,
    require_repo,
    run,
    sha256,
    timestamp,
    write_json,
)
from build import verify_static_elf
from host_integration import (
    build_official_host,
    official_host_lock,
    test_candidate_binaries,
)
from release_asset import (
    TAG_RE,
    ReleaseBundle,
    fetch_release_bundle,
    resolve_release_tag,
    verified_cli,
)

HOST_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")
CODEX_SOURCE_RE = re.compile(
    r"codex\s*=\s*\{(?:(?!\};).)*?url\s*=\s*\"([^\"]+)\"", re.DOTALL
)


def _locked_input(lock_path: Path, *input_path: str) -> dict[str, Any]:
    try:
        lock = json.loads(lock_path.read_text(encoding="utf-8"))
        node = lock["nodes"][lock["root"]]
        for input_name in input_path:
            node_name = node["inputs"][input_name]
            if not isinstance(node_name, str):
                raise TypeError(f"{input_name} is not a direct node")
            node = lock["nodes"][node_name]
        locked = node["locked"]
    except (OSError, KeyError, TypeError, json.JSONDecodeError) as error:
        raise LifecycleError(
            f"无法解析 consumer lock input: {lock_path}#{'/'.join(input_path)}"
        ) from error
    if not isinstance(locked, dict):
        raise LifecycleError(
            f"consumer locked input 不是 object: {'/'.join(input_path)}"
        )
    return locked


def _verify_codex_lock(
    lock_path: Path,
    reference: str,
    source_rev: str,
    input_path: tuple[str, ...] = ("codex",),
) -> None:
    locked = _locked_input(lock_path, *input_path)
    if locked.get("ref") != reference or locked.get("rev") != source_rev:
        raise LifecycleError(f"consumer Codex lock 未固定目标 tag+SHA: {lock_path}")
    url = str(locked.get("url", ""))
    if "github.com/loiang/co.git" not in url:
        raise LifecycleError(f"consumer Codex lock 不是 loiang/co: {lock_path}")


def _resolve_remote(root: Path, reference: str) -> str:
    if reference != "main" and TAG_RE.fullmatch(reference) is None:
        raise LifecycleError(f"Codex ref 必须是 main 或安全的 co-* tag: {reference}")
    direct = "refs/heads/main" if reference == "main" else f"refs/tags/{reference}"
    output = git(
        root,
        "ls-remote",
        "--exit-code",
        "https://github.com/loiang/co.git",
        direct,
        f"{direct}^{{}}",
    )
    resolved = None
    for line in output.splitlines():
        fields = line.split()
        if len(fields) == 2 and fields[1] in (direct, f"{direct}^{{}}"):
            if fields[1].endswith("^{}") or resolved is None:
                resolved = fields[0]
    if resolved is None or not re.fullmatch(r"[0-9a-f]{40,64}", resolved):
        raise LifecycleError(f"远端 Codex ref 未解析为完整 SHA: {reference}")
    return resolved


def _preflight(
    repository: Path, ni_repository: Path, host: str, tag: str | None
) -> tuple[Path, Path, str]:
    root = require_repo(repository)
    ni_root = require_repo(ni_repository)
    if (
        HOST_RE.fullmatch(host) is None
        or not (ni_root / "hosts" / host / "flake.nix").is_file()
    ):
        raise LifecycleError(f"ni host 无效或不存在: {host}")
    require_clean(root)
    require_clean(ni_root)
    reference = tag or "main"
    source_rev = _resolve_remote(root, reference)
    return root, ni_root, source_rev


def _ni_command(ni_root: Path, *args: str) -> list[str]:
    return ["nix", "run", git_flake(ni_root, "ni"), "--", *args]


def _verify_source_url(ni_root: Path, reference: str, source_rev: str) -> None:
    urls = CODEX_SOURCE_RE.findall((ni_root / "flake.nix").read_text(encoding="utf-8"))
    suffix = f"?ref={reference}"
    if reference != "main":
        suffix += f"&rev={source_rev}"
    expected = f"git+ssh://git@github.com/loiang/co.git{suffix}"
    if urls != [expected]:
        raise LifecycleError("ni flake.nix Codex source 未精确切换到目标 reference")


def _update_consumer(
    ni_root: Path,
    host: str,
    reference: str,
    source_rev: str,
    release_tag: str,
) -> None:
    host_lock_before = _locked_input(ni_root / "flake.lock", "codeModeHostRelease")
    consumer_lock = ni_root / "hosts" / host / "flake.lock"
    consumer_host_before = _locked_input(consumer_lock, "root", "codeModeHostRelease")
    run(
        _ni_command(
            ni_root,
            "update",
            host,
            "--input",
            "codex",
            "--ref",
            reference,
            "--expected-rev",
            source_rev,
            "--release-tag",
            release_tag,
        ),
        cwd=ni_root,
        capture=False,
    )
    _verify_source_url(ni_root, reference, source_rev)
    _verify_codex_lock(ni_root / "flake.lock", reference, source_rev)
    _verify_codex_lock(consumer_lock, reference, source_rev, ("root", "codex"))
    if _locked_input(ni_root / "flake.lock", "codeModeHostRelease") != host_lock_before:
        raise LifecycleError("更新 Codex 时 official host lock 发生意外变化")
    if (
        _locked_input(consumer_lock, "root", "codeModeHostRelease")
        != consumer_host_before
    ):
        raise LifecycleError("更新 Codex 时 consumer official host lock 发生意外变化")


def _record_install(root: Path, values: dict[str, Any]) -> None:
    write_json(root / ".states/co/install/latest.json", {"schemaVersion": 1, **values})


def _validate_candidate_source(
    root: Path, ni_root: Path, bundle: ReleaseBundle
) -> Path:
    host_store = build_official_host(root, ni_root)
    host_binary = Path(host_store) / "bin/codex-code-mode-host"
    with verified_cli(bundle) as codex_binary:
        verify_static_elf(codex_binary)
        command = test_candidate_binaries(root, codex_binary, host_binary)
    record = root / ".states/co/install/candidate-validation.json"
    write_json(
        record,
        {
            "schemaVersion": 1,
            "completedAt": timestamp(),
            "sourceRev": bundle.source_rev,
            "harnessRev": head(root),
            "release": bundle.evidence(),
            "cliArchiveStorePath": str(bundle.cli.path),
            "officialHostStorePath": host_store,
            "officialHostSha256": sha256(host_binary),
            "officialHostLock": official_host_lock(ni_root),
            "transports": ["grpc", "stdio"],
            "command": command,
        },
    )
    return record


def install(
    repository: Path,
    ni_repository: Path,
    host: str,
    tag: str | None = None,
    dry_run: bool = False,
) -> str:
    """Prevalidate one released CLI, then install it through ni's CAS transaction.

    Args:
        repository: Clean co checkout providing the release validation harness.
        ni_repository: Clean ni checkout that owns the target host locks.
        host: Existing ni host name.
        tag: Optional immutable co-* pin; omitted means tracking published main.
        dry_run: Stop after local preflight without changing ni or the system.

    Returns:
        Exact switch command that was executed, or would run in dry-run mode.
    """
    root, ni_root, source_rev = _preflight(repository, ni_repository, host, tag)
    reference = tag or "main"
    release_tag = resolve_release_tag(root, reference, source_rev)
    bundle = fetch_release_bundle(root, release_tag, source_rev)
    switch_command = _ni_command(ni_root, "rebuild", host, "switch", "--no-update")
    integration_record = _validate_candidate_source(root, ni_root, bundle)
    if dry_run:
        return " ".join(switch_command)
    ni_before = head(ni_root)
    _update_consumer(ni_root, host, reference, source_rev, release_tag)
    run(_ni_command(ni_root, "build", host), cwd=ni_root, capture=False)
    run(switch_command, cwd=ni_root, capture=False)
    switch = " ".join(switch_command)
    _record_install(
        root,
        {
            "completedAt": timestamp(),
            "sourceRev": source_rev,
            "reference": reference,
            "releaseTag": release_tag,
            "host": host,
            "niRepo": str(ni_root),
            "niRevBefore": ni_before,
            "niRevAfter": head(ni_root),
            "hostIntegrationRecord": str(integration_record.relative_to(root)),
            "switchCommand": switch,
            "switched": True,
        },
    )
    return switch
