"""Thin command-line boundary for repository-owned ``just co-*`` recipes.

Argument parsing stays separate from lifecycle modules so behavior tests can
exercise real temporary Git repositories without mocking command dispatch.
"""

import argparse
import sys
from pathlib import Path

from common import LifecycleError
from build import build
from git_lifecycle import Candidate, finalize_candidate, upgrade, validate_candidate
from host_integration import test_host_integration
from install import install
from promotion import promote
from publication import publish
from testing import run_tests


def _non_negative_int(value: str) -> int:
    """Parse resource limits before any lifecycle mutation can begin."""
    try:
        parsed = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("必须是整数") from error
    if parsed < 0:
        raise argparse.ArgumentTypeError("必须是非负整数")
    return parsed


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="co-lifecycle")
    subcommands = parser.add_subparsers(dest="command", required=True)

    upgrade_parser = subcommands.add_parser("upgrade")
    upgrade_parser.add_argument("--repo", type=Path, required=True)
    upgrade_parser.add_argument("--ni-repo", type=Path, default=Path("/repo/ni"))
    upgrade_parser.add_argument("--cores", type=_non_negative_int, default=0)
    upgrade_parser.add_argument("revision", nargs="?", default="upstream/main")

    finalize = subcommands.add_parser("upgrade-finalize")
    finalize.add_argument("--repo", type=Path, required=True)
    finalize.add_argument("--upstream-rev", required=True)
    finalize.add_argument("--ni-repo", type=Path, default=Path("/repo/ni"))
    finalize.add_argument("--cores", type=_non_negative_int, default=0)

    promote_parser = subcommands.add_parser("promote")
    promote_parser.add_argument("--repo", type=Path, required=True)
    promote_parser.add_argument("candidate")

    test_parser = subcommands.add_parser("test")
    test_parser.add_argument("--repo", type=Path, required=True)

    build_parser = subcommands.add_parser("build")
    build_parser.add_argument("--repo", type=Path, required=True)
    build_parser.add_argument("--cores", type=_non_negative_int, default=0)

    host_parser = subcommands.add_parser("test-host")
    host_parser.add_argument("--repo", type=Path, required=True)
    host_parser.add_argument("--ni-repo", type=Path, default=Path("/repo/ni"))

    publish_parser = subcommands.add_parser("publish")
    publish_parser.add_argument("--repo", type=Path, required=True)
    publish_parser.add_argument("--dry-run", action="store_true")

    install_parser = subcommands.add_parser("install")
    install_parser.add_argument("--repo", type=Path, required=True)
    install_parser.add_argument("host")
    install_parser.add_argument("tag", nargs="?")
    install_parser.add_argument("--ni-repo", type=Path, default=Path("/repo/ni"))
    install_parser.add_argument("--dry-run", action="store_true")
    return parser


def _execute(args: argparse.Namespace) -> str:
    if args.command == "upgrade":
        candidate = upgrade(args.repo, args.revision, args.ni_repo, args.cores)
        status = "created" if candidate.changed else "noop"
        return f"candidate {status}: {candidate.branch} ({candidate.root})"
    if args.command == "upgrade-finalize":
        branch = subprocess_branch(args.repo)
        candidate = Candidate(
            args.repo.resolve(), branch, args.upstream_rev, changed=True
        )
        finalize_candidate(candidate)
        validate_candidate(candidate, args.ni_repo, args.cores)
        return f"candidate finalized: {branch} ({candidate.root})"
    if args.command == "promote":
        revision = promote(args.repo, args.candidate)
        return f"local main promoted: {revision}"
    if args.command == "test":
        return f"test record: {run_tests(args.repo)}"
    if args.command == "build":
        return f"build record: {build(args.repo, args.cores)}"
    if args.command == "test-host":
        return (
            f"host integration record: {test_host_integration(args.repo, args.ni_repo)}"
        )
    if args.command == "publish":
        return f"published tag: {publish(args.repo, dry_run=args.dry_run)}"
    if args.command == "install":
        switch = install(
            args.repo, args.ni_repo, args.host, args.tag, dry_run=args.dry_run
        )
        status = "preflight complete" if args.dry_run else "consumer installed"
        return f"{status}:\n{switch}"
    raise LifecycleError(f"未知 command: {args.command}")


def subprocess_branch(repository: Path) -> str:
    """Read the continuation worktree branch without broadening CLI state.

    Args:
        repository: Candidate worktree retained after a conflict.

    Returns:
        Current branch name.
    """
    from common import git, require_repo

    root = require_repo(repository)
    branch = git(root, "branch", "--show-current")
    if not branch.startswith("upgrade/"):
        raise LifecycleError(f"candidate branch 必须是 upgrade/*: {branch}")
    return branch


def main() -> int:
    """Execute one lifecycle command and preserve actionable failure text.

    Returns:
        Process exit status suitable for ``just``.
    """
    try:
        print(_execute(_parser().parse_args()))
        return 0
    except (LifecycleError, OSError) as error:
        print(f"co-lifecycle: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
