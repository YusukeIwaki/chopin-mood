#!/usr/bin/env bash
# Only the prompt/schema workspace and this provider's dedicated auth are
# mounted. The agent cannot change the trusted host checkout or GitHub files.
set -euo pipefail
work="${1:?worker directory}"
container_name="${MUSE_REPAIR_CONTAINER_NAME:-}"
if [ -z "$container_name" ]; then
  container_name="studio-muse-$$-${RANDOM:-0}"
fi
docker build -t studio-muse-worker -f .github/studio/Muse.Dockerfile .github/studio >&2
docker run --rm studio-muse-worker --version >&2
cleanup() {
  original_code=$?
  trap - EXIT INT TERM
  cleanup_code=0
  python3 - "$container_name" <<'PY_CLEANUP' || cleanup_code=$?
import sys
from studio.repair import cleanup_muse_container
if not cleanup_muse_container(sys.argv[1]):
    print("::error::Muse container cleanup unconfirmed; credential persistence is disabled.", file=sys.stderr)
    raise SystemExit(2)
PY_CLEANUP
  if [ "$original_code" -ne 0 ]; then exit "$original_code"; fi
  exit "$cleanup_code"
}
trap cleanup EXIT
trap 'exit 143' TERM
trap 'exit 130' INT
python3 - <<'PY_START'
from studio.repair import record_muse_cleanup
record_muse_cleanup(False)
PY_START
docker run --rm --name "$container_name" --cap-drop=ALL --security-opt=no-new-privileges \
  --user "$(id -u):$(id -g)" \
  --mount "type=bind,source=$work,target=/workspace" \
  --mount "type=bind,source=$MUSE_RUNTIME_HOME,target=/agent-config" \
  -e XDG_CONFIG_HOME=/agent-config -e HOME=/tmp \
  studio-muse-worker exec --yolo --model muse-wonder-1.0-contributor \
  --reasoning-effort max --max-model-steps 80 --disable-web-tools \
  --disable-reminders --no-foreign-personal-context --prompt-file prompt.md --json
