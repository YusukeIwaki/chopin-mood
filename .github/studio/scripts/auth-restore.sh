#!/usr/bin/env bash
# Restore Codex and/or Muse auth from encrypted repos into RUNNER_TEMP.
# Usage: auth-restore.sh codex|muse
# Env in:  *_ENC_REPO_DIR, *_RUNTIME_HOME, *_AUTH_BEFORE_HASH, passphrase env.
# Caller clones the .enc repo with a READ token and revokes it BEFORE this
# script's agent steps run. No token/passphrase is printed.
set -euo pipefail
umask 077
kind="${1:?usage: auth-restore.sh codex|muse}"

case "$kind" in
  codex)
    : "${CODEX_ENC_PASSPHRASE:?missing CODEX_ENC_PASSPHRASE}"
    install -d -m 0700 "$CODEX_RUNTIME_HOME"
    python3 .github/studio/scripts/codex-config.py
    openssl enc -d -aes-256-cbc -pbkdf2 -base64 \
      -in "$CODEX_ENC_REPO_DIR/codex.enc" \
      -out "$CODEX_RUNTIME_HOME/auth.json" -pass env:CODEX_ENC_PASSPHRASE
    chmod 600 "$CODEX_RUNTIME_HOME/auth.json"
    jq -e '.auth_mode == "chatgpt" and ((.tokens.refresh_token // "") | length > 0)' \
      "$CODEX_RUNTIME_HOME/auth.json" >/dev/null
    git -C "$CODEX_ENC_REPO_DIR" rev-parse HEAD > "$CODEX_ENC_REPO_DIR/.clone-head"
    sha256sum "$CODEX_RUNTIME_HOME/auth.json" | awk '{print $1}' > "$CODEX_AUTH_BEFORE_HASH"
    ;;
  muse)
    : "${MUSE_ENC_PASSPHRASE:?missing MUSE_ENC_PASSPHRASE}"
    # Linux Muse auth: $XDG_CONFIG_HOME/muse/auth.json, schema-v1 inline
    # tokens. macOS keychain pointers (storage: keychain) are NOT portable.
    install -d -m 0700 "$MUSE_RUNTIME_HOME/muse"
    openssl enc -d -aes-256-cbc -pbkdf2 -base64 \
      -in "$MUSE_ENC_REPO_DIR/muse.enc" \
      -out "$MUSE_RUNTIME_HOME/muse/auth.json" -pass env:MUSE_ENC_PASSPHRASE
    chmod 600 "$MUSE_RUNTIME_HOME/muse/auth.json"
    jq -e '
      (type == "object") and ((.storage // "file") != "keychain") and
      (((.providers.meta.access_token // "") | length > 0) or
       ((.providers.meta.api_key // "") | length > 0))
    ' "$MUSE_RUNTIME_HOME/muse/auth.json" >/dev/null
    git -C "$MUSE_ENC_REPO_DIR" rev-parse HEAD > "$MUSE_ENC_REPO_DIR/.clone-head"
    sha256sum "$MUSE_RUNTIME_HOME/muse/auth.json" | awk '{print $1}' > "$MUSE_AUTH_BEFORE_HASH"
    ;;
  *) echo "unknown kind: $kind" >&2; exit 2 ;;
esac
echo "auth restored ($kind); contents not printed"
