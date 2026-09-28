#!/usr/bin/env bash
set -euo pipefail

if [[ -n "${CODEX_BAZEL_BIN:-}" ]]; then
  exec "$CODEX_BAZEL_BIN" "$@"
fi

bazelisk="@bazelisk@"
patchelf="@patchelf@"
loader="@loader@"
rpath="@rpath@"
bazelisk_home="${BAZELISK_HOME:-${XDG_CACHE_HOME:-$HOME/.cache}/bazelisk}"
bazel_output_root="${BAZEL_OUTPUT_USER_ROOT:-${XDG_CACHE_HOME:-$HOME/.cache}/bazel}"
bazel_startup_args=("--output_user_root=$bazel_output_root")
if [[ "${1:-}" == "--version" ]]; then
  preflight_args=(--version)
else
  preflight_args=("${bazel_startup_args[@]}" info output_user_root)
fi

patch_binary() {
  local binary="$1" interpreter
  if ! interpreter="$($patchelf --print-interpreter "$binary" 2>/dev/null)"; then
    return 1
  fi
  if [[ "$interpreter" == "$loader" ]]; then
    return 1
  fi

  "$patchelf" \
    --set-interpreter "$loader" \
    --set-rpath "$rpath" \
    "$binary"
  return 0
}

patch_cached_bazel() {
  local binary
  local patched=1

  if [[ -d "$bazelisk_home/downloads" ]]; then
    while IFS= read -r -d '' binary; do
      if patch_binary "$binary"; then
        patched=0
      fi
    done < <(find "$bazelisk_home/downloads" -type f -path '*/bin/bazel' -print0)
  fi

  if [[ -d "$bazel_output_root" ]]; then
    while IFS= read -r -d '' binary; do
      if patch_binary "$binary"; then
        patched=0
      fi
    done < <(find "$bazel_output_root" -type f -perm /111 -path '*/install/*' -print0)
  fi

  return "$patched"
}

patch_cached_bazel || true

preflight_output="$(mktemp)"
trap 'rm -f "$preflight_output"' EXIT
preflight_status=1
for _ in {1..8}; do
  if "$bazelisk" "${preflight_args[@]}" >"$preflight_output" 2>&1; then
    cat "$preflight_output"
    preflight_status=0
    break
  fi
  preflight_status=$?
  if ! patch_cached_bazel; then
    cat "$preflight_output" >&2
    exit "$preflight_status"
  fi
done
if (( preflight_status != 0 )); then
  cat "$preflight_output" >&2
  exit "$preflight_status"
fi

if [[ "${1:-}" == "--version" ]]; then
  exec "$bazelisk" "$@"
fi
exec "$bazelisk" "${bazel_startup_args[@]}" "$@"
