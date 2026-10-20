#!/usr/bin/env bash
# Always-run cleanup: remove runtime credential state under RUNNER_TEMP.
set -euo pipefail
for d in "${CODEX_RUNTIME_HOME:-}" "${MUSE_RUNTIME_HOME:-}" \
         "${CODEX_ENC_REPO_DIR:-}" "${MUSE_ENC_REPO_DIR:-}"; do
  [ -n "$d" ] || continue
  case "$d" in
    "$RUNNER_TEMP"/*) rm -rf -- "$d" ;;
    *) echo "refusing cleanup outside RUNNER_TEMP: $d" >&2; exit 1 ;;
  esac
done
rm -f -- "${CODEX_AUTH_BEFORE_HASH:-/nonexistent}" "${MUSE_AUTH_BEFORE_HASH:-/nonexistent}" \
  "$RUNNER_TEMP/codex.stdout" "$RUNNER_TEMP/codex.stderr" \
  "$RUNNER_TEMP/muse.stdout" "$RUNNER_TEMP/muse.stderr" 2>/dev/null || true
echo "credential runtime cleaned"
