#!/usr/bin/env bash
# Only the prompt/schema workspace and this provider's dedicated auth are
# mounted. The agent cannot change the trusted host checkout or GitHub files.
set -euo pipefail
work="${1:?worker directory}"
docker build -t studio-muse-worker -f .github/studio/Muse.Dockerfile .github/studio >&2
docker run --rm studio-muse-worker --version >&2
docker run --rm --cap-drop=ALL --security-opt=no-new-privileges \
  --user "$(id -u):$(id -g)" \
  --mount "type=bind,source=$work,target=/workspace" \
  --mount "type=bind,source=$MUSE_RUNTIME_HOME,target=/agent-config" \
  -e XDG_CONFIG_HOME=/agent-config -e HOME=/tmp \
  studio-muse-worker exec --yolo --model muse-wonder-1.0-contributor \
  --reasoning-effort max --max-model-steps 80 --disable-web-tools \
  --disable-reminders --no-foreign-personal-context --prompt-file prompt.md --json
