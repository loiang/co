#!/usr/bin/env bash
set -euo pipefail

if [[ -n "${CODEX_BAZEL_BIN:-}" ]]; then
  exec "$CODEX_BAZEL_BIN" "$@"
fi

proot="@proot@"
bazelisk="@bazelisk@"
loader="@loader@"
glibc_lib="@glibc_lib@"
gcc_lib="@gcc_lib@"
runtime_library_path="@runtime_library_path@"
bazel_output_root="${BAZEL_OUTPUT_USER_ROOT:-${XDG_CACHE_HOME:-$HOME/.cache}/bazel}"
proot_tmpdir="$(mktemp -d)"
trap 'rm -rf "$proot_tmpdir"' EXIT
export PROOT_TMPDIR="$proot_tmpdir"

export LD_LIBRARY_PATH="$runtime_library_path${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
proot_args=(
  -b "$loader:/lib64/ld-linux-x86-64.so.2"
  -b "$glibc_lib:/lib"
  -b "$gcc_lib:/usr/lib"
)

if [[ "${1:-}" == "--version" ]]; then
  "$proot" "${proot_args[@]}" "$bazelisk" "$@"
  exit $?
fi

"$proot" "${proot_args[@]}" "$bazelisk" \
  "--output_user_root=$bazel_output_root" "$@"
