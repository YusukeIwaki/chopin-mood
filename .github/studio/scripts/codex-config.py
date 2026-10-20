#!/usr/bin/env python3
"""Generate one strict read-only profile; never print credential contents."""
import json
import os
from pathlib import Path

runtime = Path(os.environ["CODEX_RUNTIME_HOME"])
runtime.mkdir(mode=0o700, parents=True, exist_ok=True)
denied = [
    str(runtime / "auth.json"),
    os.environ["CODEX_ENC_REPO_DIR"],
    os.environ["CODEX_AUTH_BEFORE_HASH"],
]
for name in ("STUDIO_SOURCE_DIR", "MUSE_RUNTIME_HOME", "MUSE_ENC_REPO_DIR"):
    if os.environ.get(name):
        denied.append(os.environ[name])
text = (
    'cli_auth_credentials_store = "file"\n'
    'default_permissions = "studio"\n'
    '[permissions.studio]\nextends = ":read-only"\n'
    '[permissions.studio.network]\nenabled = false\n'
    '[permissions.studio.filesystem]\n'
)
text += "".join(f'{json.dumps(p)} = "deny"\n' for p in dict.fromkeys(denied))
target = runtime / "config.toml"
target.write_text(text)
target.chmod(0o600)
