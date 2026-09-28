---
name: upstream-merge-preserve-custom
description: Fetch and merge openai/codex upstream into loiang/co while preserving repository-specific commits, custom behavior, and docs/pacth. Use for recurring upstream synchronization of this fork.
---

# Merge upstream while preserving co customizations

Use `$git-workflow:branch-merge`, `$git-workflow:commit`, and
`$git-workflow:push` for their respective lifecycle stages. Never rewrite or
force-push `main`.

## Preconditions

1. Work in `/repo/co`; verify `origin` is the fork and `upstream` is
   `https://github.com/openai/codex.git`.
2. Read `docs/pacth` completely. Treat it as the preservation contract for
   local behavior, tests, and packaging—not as an executable patch.
3. Inspect `git status`, commit existing work with `$git-workflow:commit`, and
   fetch both `origin` and `upstream`.
4. Record the pre-merge `main`, `upstream/main`, merge-base, ahead/behind
   counts, and create a `backup/pre-merge-*` tag.

## Integration protocol

1. Create an integration branch from the current `main`.
2. Before changing files, use `git merge-tree` and the ledger to identify
   overlapping paths and classify semantic risks as SAFE, CAUTION, or DANGER.
   Stop and ask the user only for DANGER conflicts that require a product or
   architecture decision.
3. Merge `upstream/main` into the integration branch. Resolve conflicts there;
   never resolve conflicts directly on `main`.
4. Preserve local behavior documented in `docs/pacth`. For every conflict,
   compare the merge-base, local side, and upstream side; integrate upstream
   API or dependency changes without silently dropping the documented custom
   behavior.
5. Keep `docs/pacth` in the result. Update its upstream baseline and commit
   references only from verified Git history, and add or revise protection and
   test paths when the merged implementation changes.

## Validation and publication

1. Run `python3 scripts/format.py` after code changes.
2. Run focused tests for every affected custom subsystem and every resolved
   conflict. Follow `AGENTS.md` for Rust nextest, schema, snapshot, Clippy, and
   full-suite gates; request approval before the complete Rust suite when it is
   required.
3. Re-read every ledger protection path against the merged tree and inspect
   `git diff main...HEAD`. No documented customization may disappear without
   explicit user approval.
4. Commit the resolved integration branch. Fast-forward `main` to it through
   `$git-workflow:branch-merge`; delete the temporary branch and backup tag only
   after validation succeeds.
5. Atomically publish `main`, tags, and `refs/notes/commits` with
   `$git-workflow:push`. Verify the remote refs.

Report the old and new upstream OIDs, preserved custom areas, conflict
resolutions, tests, final `main` OID, and publication verification.
