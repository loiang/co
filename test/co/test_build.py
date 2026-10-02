"""Exercise stamped single-CLI builds without invoking the Rust compiler."""

import io
import json
import subprocess
import sys
import tarfile
import tomllib
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).parents[2]
sys.path.insert(0, str(ROOT / "scripts" / "co"))

from build import _official_version, build  # noqa: E402
from common import LifecycleError, git, run, sha256  # noqa: E402

REAL_SUBPROCESS_RUN = subprocess.run


def _gh_run(
    stdout: bytes = b'[{"ref":"refs/tags/rust-v0.154.0","object":{"type":"commit","sha":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}}]',
    *,
    returncode: int = 0,
    stderr: bytes = b"",
    runtime_release: bytes | None = None,
) -> object:
    """Return a subprocess boundary fake that delegates non-gh commands."""

    def execute(
        command: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[bytes]:
        if command[:2] == ["gh", "api"]:
            if "/releases/tags/" in command[2]:
                return subprocess.CompletedProcess(command, 1, b"", b"HTTP 404")
            if (
                command[2] == "repos/openai/codex/releases/latest"
                and runtime_release is not None
            ):
                return subprocess.CompletedProcess(command, 0, runtime_release, b"")
            return subprocess.CompletedProcess(command, returncode, stdout, stderr)
        return REAL_SUBPROCESS_RUN(command, **kwargs)

    return execute


@pytest.fixture
def source_repo(tmp_path: Path) -> Path:
    """Keep source identity real while isolating every generated artifact."""
    (tmp_path / "scripts").symlink_to(ROOT / "scripts", target_is_directory=True)
    (tmp_path / "codex-rs").mkdir()
    (tmp_path / "codex-rs/Cargo.toml").write_text(
        '[workspace.package]\nversion = "0.0.0"\n', encoding="utf-8"
    )
    (tmp_path / "codex-rs/Cargo.lock").write_text(
        'version = 4\n[[package]]\nname = "codex-cli"\nversion = "0.0.0"\n',
        encoding="utf-8",
    )
    (tmp_path / "flake.lock").write_text("{}", encoding="utf-8")
    (tmp_path / ".gitignore").write_text(".states/\n", encoding="utf-8")
    git(tmp_path, "init", "--quiet")
    git(tmp_path, "config", "user.name", "co-test")
    git(tmp_path, "config", "user.email", "co-test@localhost")
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "--quiet", "-m", "native build fixture")
    return tmp_path


def test_official_version_resolves_official_git_tags() -> None:
    metadata = io.BytesIO(
        b'[{"ref":"refs/tags/rust-v0.154.0","object":{"type":"commit","sha":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}}]'
    )
    with patch(
        "upstream_version.subprocess.run",
        return_value=subprocess.CompletedProcess(
            ["gh", "api"], 0, metadata.getvalue(), b""
        ),
    ) as gh_run:
        assert _official_version() == "0.154.0"
    command = gh_run.call_args.args[0]
    assert command[:3] == [
        "gh",
        "api",
        "repos/openai/codex/git/matching-refs/tags/rust-v",
    ]
    assert "token" not in " ".join(command).lower()


def test_linux_gnu_native_tls_uses_vendored_openssl() -> None:
    """Keep native Codex builds independent of a host OpenSSL installation."""
    manifest = tomllib.loads(
        (ROOT / "codex-rs/http-client/Cargo.toml").read_text(encoding="utf-8")
    )
    dependency = manifest["dependencies"]["native-tls"]
    assert dependency == {"version": "0.2", "features": ["vendored"]}

    tree = run(
        [
            "cargo",
            "tree",
            "--locked",
            "--manifest-path",
            "codex-rs/Cargo.toml",
            "--package",
            "codex-http-client",
            "--target",
            "x86_64-unknown-linux-gnu",
            "-e",
            "features",
        ],
        cwd=ROOT,
    )
    assert 'native-tls feature "vendored"' in tree.stdout
    assert 'openssl-sys feature "vendored"' in tree.stdout


@pytest.mark.parametrize(
    "metadata",
    [
        b'{"tag_name":"rust-v0.154.0-alpha.1","draft":false,"prerelease":true}',
        b'{"tag_name":"v0.154.0","draft":false,"prerelease":false}',
        b'{"tag_name":"rust-v0.154.0","draft":true,"prerelease":false}',
    ],
)
def test_official_version_rejects_release_metadata_as_tag_input(
    metadata: bytes,
) -> None:
    with (
        patch(
            "upstream_version.subprocess.run",
            return_value=subprocess.CompletedProcess(["gh", "api"], 0, metadata, b""),
        ),
        pytest.raises(LifecycleError, match="JSON array"),
    ):
        _official_version()


def test_official_version_rejects_malformed_json() -> None:
    with (
        patch(
            "upstream_version.subprocess.run",
            return_value=subprocess.CompletedProcess(
                ["gh", "api"], 0, b"not JSON", b""
            ),
        ),
        pytest.raises(LifecycleError, match="不是有效 JSON"),
    ):
        _official_version()


def test_official_version_rejects_empty_tags() -> None:
    with (
        patch(
            "upstream_version.subprocess.run",
            return_value=subprocess.CompletedProcess(["gh", "api"], 0, b"[]", b""),
        ),
        pytest.raises(LifecycleError, match="没有有效完整"),
    ):
        _official_version()


def test_official_version_rejects_missing_gh() -> None:
    with (
        patch("upstream_version.subprocess.run", side_effect=FileNotFoundError),
        pytest.raises(LifecycleError, match="gh CLI 不可用"),
    ):
        _official_version()


@pytest.mark.parametrize(
    ("stderr", "message"),
    [
        (b"not logged into any GitHub hosts", "gh CLI 未认证"),
        (b"API rate limit exceeded", "gh api 查询官方 tags 失败"),
    ],
)
def test_official_version_rejects_gh_failure(stderr: bytes, message: str) -> None:
    with (
        patch(
            "upstream_version.subprocess.run",
            return_value=subprocess.CompletedProcess(["gh", "api"], 1, b"", stderr),
        ),
        pytest.raises(LifecycleError, match=message),
    ):
        _official_version()


@pytest.fixture
def cargo_boundary(source_repo: Path, monkeypatch) -> Path:
    """Simulate only Cargo, running the real stamping driver and archive writer."""
    tools = source_repo / ".states/tools"
    tools.mkdir(parents=True)
    cargo = tools / "cargo"
    cargo.write_text(
        f"#!{sys.executable}\n"
        "import json, os, pathlib, sys, tomllib\n"
        "args = sys.argv[1:]\n"
        "version = os.environ['CODEX_CLI_VERSION']\n"
        "log = pathlib.Path(os.environ['CO_TEST_CARGO_LOG'])\n"
        "with log.open('a') as stream: stream.write(json.dumps(args) + '\\n')\n"
        "workspace = pathlib.Path.cwd()\n"
        "assert tomllib.loads((workspace / 'Cargo.toml').read_text())['workspace']['package']['version'] == version\n"
        "if args == ['update', '--workspace']:\n"
        "    lock = workspace / 'Cargo.lock'\n"
        "    lock.write_text(lock.read_text().replace('0.0.0', version))\n"
        "elif args == ['build', '--release', '--locked', '-p', 'codex-cli', '--bin', 'codex']:\n"
        "    log.with_suffix('.env').write_text(json.dumps({key: os.environ.get(key) for key in ['CARGO_TARGET_DIR', 'CO_BUILD_WORKTREE', 'CO_BUILD_LOCK_FD', 'CARGO_BUILD_JOBS']}))\n"
        "    binary = pathlib.Path(os.environ['CARGO_TARGET_DIR']) / 'release/codex'\n"
        "    binary.parent.mkdir(parents=True, exist_ok=True)\n"
        "    binary.write_text('#!' + sys.executable + '\\nprint(' + repr('codex-cli ' + version) + ')\\n')\n"
        "    binary.chmod(0o755)\n"
        "else: raise RuntimeError(args)\n",
        encoding="utf-8",
    )
    cargo.chmod(0o755)
    import os

    monkeypatch.setenv("PATH", str(tools) + os.pathsep + os.environ["PATH"])
    monkeypatch.setenv("CO_TEST_CARGO_LOG", str(source_repo / ".states/cargo-log"))
    return cargo


@pytest.mark.parametrize(("cores", "version"), [(0, "0.154.0"), (4, "0.162.0-alpha.4")])
def test_build_emits_only_stamped_cli_without_runtime_downloads(
    source_repo: Path, cargo_boundary: Path, cores: int, version: str
) -> None:
    originals = [
        (source_repo / "codex-rs" / name).read_bytes()
        for name in ("Cargo.toml", "Cargo.lock")
    ]

    def capture(command: list[str], **kwargs: object):
        if command[0] == "rustc":
            return subprocess.CompletedProcess(
                command, 0, "host: x86_64-unknown-linux-gnu\n", ""
            )
        return run(command, **kwargs)

    metadata = json.dumps(
        [
            {
                "ref": f"refs/tags/rust-v{version}",
                "object": {"type": "commit", "sha": "a" * 40},
            }
        ]
    ).encode()
    with (
        patch("native_package.run", side_effect=capture),
        patch("upstream_version.subprocess.run", side_effect=_gh_run(metadata)) as gh,
    ):
        latest = build(source_repo, cores)
    record = json.loads(latest.read_text())
    assert record["schemaVersion"] == 3
    assert "package" not in record and "packageDir" not in record
    expected = {
        "kind": "standalone-cli",
        "version": version,
        "target": "x86_64-unknown-linux-gnu",
        "entrypoint": "codex",
    }
    assert record["artifact"] == expected
    directory = source_repo / record["artifactDir"]
    assert [path.name for path in directory.iterdir()] == ["codex"]
    assert (
        run([str(directory / "codex"), "--version"], cwd=source_repo).stdout.strip()
        == f"codex-cli {version}"
    )
    archive, manifest, sums = [source_repo / name for name in record["assets"]]
    with tarfile.open(archive) as bundle:
        assert [(member.name, member.isreg()) for member in bundle] == [("codex", True)]
    payload = json.loads(manifest.read_text())
    assert payload["schemaVersion"] == 3 and payload["sourceVersion"] == version
    assert payload["artifact"] == expected and "package" not in payload
    assert payload["checksums"] == {
        "codex": sha256(directory / "codex"),
        archive.name: sha256(archive),
    }
    assert record["manifestSha256"] == sha256(manifest)
    assert (
        sums.read_text()
        == f"{sha256(archive)}  {archive.name}\n{sha256(manifest)}  {manifest.name}\n"
    )
    calls = [
        json.loads(line)
        for line in (source_repo / ".states/cargo-log").read_text().splitlines()
    ]
    assert calls == [
        ["update", "--workspace"],
        ["build", "--release", "--locked", "-p", "codex-cli", "--bin", "codex"],
    ]
    env = json.loads((source_repo / ".states/cargo-log.env").read_text())
    assert env["CARGO_TARGET_DIR"] == str(source_repo / "codex-rs/target")
    assert env["CO_BUILD_WORKTREE"] == str(source_repo / ".states/build")
    assert env["CO_BUILD_LOCK_FD"].isdigit()
    assert env["CARGO_BUILD_JOBS"] == (str(cores) if cores else None)
    assert [
        call.args[0][2]
        for call in gh.call_args_list
        if call.args[0][:2] == ["gh", "api"]
    ] == ["repos/openai/codex/git/matching-refs/tags/rust-v"]
    assert [
        (source_repo / "codex-rs" / name).read_bytes()
        for name in ("Cargo.toml", "Cargo.lock")
    ] == originals
    assert not (source_repo / ".states/build").exists()
    assert git(source_repo, "branch", "--list", "build/co-*") == ""


@pytest.mark.parametrize("change", ["tracked", "staged", "untracked"])
def test_build_rejects_mutable_source_before_resolving_release(
    source_repo: Path, change: str
) -> None:
    target = source_repo / ("new-file" if change == "untracked" else "flake.lock")
    target.write_text("changed")
    if change == "staged":
        git(source_repo, "add", str(target))
    with (
        patch("build._official_version") as release,
        pytest.raises(LifecycleError, match="工作树存在"),
    ):
        build(source_repo)
    release.assert_not_called()


@pytest.mark.parametrize("failure", [RuntimeError, KeyboardInterrupt])
def test_interrupted_build_preserves_latest_and_removes_partial_output(
    source_repo: Path, failure: type[BaseException]
) -> None:
    latest = source_repo / ".states/co/build/latest.json"
    latest.parent.mkdir(parents=True)
    latest.write_text('{"previous":"verified"}')

    def interrupt(request):
        (request.output_dir / "partial-output").write_text("partial")
        raise failure("interrupted")

    with (
        patch("build._official_version", return_value="0.154.0"),
        patch("build.build_package", side_effect=interrupt),
        pytest.raises(failure),
    ):
        build(source_repo)
    assert latest.read_text() == '{"previous":"verified"}'
    assert not list(latest.parent.glob("20*"))
    assert not (source_repo / ".states/build").exists()


@pytest.mark.parametrize("output", ["missing", "symlink", "nonexec", "wrong-version"])
def test_invalid_cli_output_never_emits_evidence(
    source_repo: Path, output: str
) -> None:
    def capture(command: list[str], **kwargs: object):
        if command[0] == "rustc":
            return subprocess.CompletedProcess(
                command, 0, "host: x86_64-unknown-linux-gnu\n", ""
            )
        if (
            command[:2] == [sys.executable, "-c"]
            and "versioned_workspace" in command[2]
        ):
            binary = source_repo / "codex-rs/target/release/codex"
            binary.parent.mkdir(parents=True)
            if output != "missing":
                binary.write_text("cli")
                binary.chmod(0o644 if output == "nonexec" else 0o755)
            if output == "symlink":
                actual = binary.with_name("actual")
                binary.rename(actual)
                binary.symlink_to(actual)
            return subprocess.CompletedProcess(command, 0)
        if command[-1] == "--version":
            return subprocess.CompletedProcess(command, 0, "codex-cli 0.0.0\n", "")
        return run(command, **kwargs)

    with (
        patch("build._official_version", return_value="0.154.0"),
        patch("native_package.run", side_effect=capture),
        pytest.raises(LifecycleError),
    ):
        build(source_repo)
    assert not (source_repo / ".states/co/build/latest.json").exists()
    assert not (source_repo / ".states/build").exists()


def test_build_rejects_negative_core_limit_before_commands(source_repo: Path) -> None:
    with (
        patch("native_package.run") as command,
        pytest.raises(LifecycleError, match="cores"),
    ):
        build(source_repo, -1)
    command.assert_not_called()


@pytest.mark.parametrize(
    "host",
    ["", "host: \n", "host: a\nhost: b\n", "host: riscv64gc-unknown-linux-gnu\n"],
)
def test_invalid_host_never_compiles(source_repo: Path, host: str) -> None:
    def capture(command: list[str], **kwargs: object):
        if command[0] == "rustc":
            return subprocess.CompletedProcess(command, 0, host, "")
        assert "versioned_workspace" not in " ".join(command)
        return run(command, **kwargs)

    with (
        patch("build._official_version", return_value="0.154.0"),
        patch("native_package.run", side_effect=capture),
        pytest.raises(LifecycleError),
    ):
        build(source_repo)
