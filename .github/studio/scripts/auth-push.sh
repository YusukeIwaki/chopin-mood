#!/usr/bin/env bash
# Push re-encrypted auth with the short-lived WRITE token (GH_TOKEN env).
# Never --force. Aborts when the remote moved since clone (conflicting
# auth edit) instead of rebasing changed credentials.
set -euo pipefail
kind="${1:?usage: auth-push.sh codex|muse}"
case "$kind" in
  codex) enc_repo="$CODEX_ENC_REPO_DIR"; enc_file="codex.enc" ;;
  muse)  enc_repo="$MUSE_ENC_REPO_DIR";  enc_file="muse.enc" ;;
  *) echo "unknown kind: $kind" >&2; exit 2 ;;
esac
clone_head="$(cat "$enc_repo/.clone-head")"
git -C "$enc_repo" config user.name "github-actions[bot]"
git -C "$enc_repo" config user.email "41898282+github-actions[bot]@users.noreply.github.com"
git -C "$enc_repo" add -- "$enc_file"
if git -C "$enc_repo" diff --cached --quiet; then
  echo "auth unchanged at push stage ($kind); nothing to push"
  exit 0
fi
git -C "$enc_repo" commit -m "chore(auth): persist refreshed authentication"
gh auth setup-git
git -C "$enc_repo" fetch origin main
remote_head="$(git -C "$enc_repo" rev-parse origin/main)"
if [ "$remote_head" != "$clone_head" ]; then
  echo "::error::auth repo remote moved since clone ($clone_head -> $remote_head); refusing to rebase changed credentials — re-run the workflow" >&2
  exit 1
fi
git -C "$enc_repo" push origin HEAD:main
echo "auth pushed ($kind)"
