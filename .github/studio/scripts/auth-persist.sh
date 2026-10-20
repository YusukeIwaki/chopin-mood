#!/usr/bin/env bash
# Persist refreshed auth ONLY when it changed. Usage: auth-persist.sh codex|muse
# Outputs changed=true/false to $GITHUB_OUTPUT. Caller creates a WRITE token
# only when changed=true, then runs auth-push.sh. Push is never forced and
# conflicting remote edits abort instead of a silent credential rebase.
# Caveat: concurrent cross-repo auth writes fail safe (see docs/10).
set -euo pipefail
umask 077
kind="${1:?usage: auth-persist.sh codex|muse}"

case "$kind" in
  codex) runtime="$CODEX_RUNTIME_HOME/auth.json"; before="$CODEX_AUTH_BEFORE_HASH"
         enc_repo="$CODEX_ENC_REPO_DIR"; enc_file="codex.enc"; pass_env=CODEX_ENC_PASSPHRASE
         check='jq -e ".auth_mode == \"chatgpt\" and ((.tokens.refresh_token // \"\") | length > 0)" "$1" >/dev/null' ;;
  muse)  runtime="$MUSE_RUNTIME_HOME/muse/auth.json"; before="$MUSE_AUTH_BEFORE_HASH"
         enc_repo="$MUSE_ENC_REPO_DIR"; enc_file="muse.enc"; pass_env=MUSE_ENC_PASSPHRASE
         check='jq -e "(type == \"object\") and ((.storage // \"file\") != \"keychain\") and ((.schema_version // 1) >= 1) and (((.providers.meta.access_token // \"\") | length > 0) or ((.providers.meta.api_key // \"\") | length > 0))" "$1" >/dev/null' ;;
  *) echo "unknown kind: $kind" >&2; exit 2 ;;
esac

: "${!pass_env:?missing passphrase}"
test -f "$runtime" && test -f "$before"
bash -c "$check" _ "$runtime"
before_hash="$(cat "$before")"
after_hash="$(sha256sum "$runtime" | awk '{print $1}')"
if [ "$before_hash" = "$after_hash" ]; then
  echo "changed=false" >> "${GITHUB_OUTPUT:-/dev/stdout}"
  echo "auth unchanged ($kind)"
  exit 0
fi
tmp="$enc_repo/$enc_file.tmp"
openssl enc -aes-256-cbc -salt -pbkdf2 -base64 \
  -in "$runtime" -out "$tmp" -pass env:"$pass_env"
test -s "$tmp"
mv "$tmp" "$enc_repo/$enc_file"
echo "changed=true" >> "${GITHUB_OUTPUT:-/dev/stdout}"
echo "auth changed ($kind); re-encrypted, awaiting write-token push"
