#!/usr/bin/env bash
# Prepare a disposable GitHub-hosted Ubuntu runner before restoring secrets.
# https://learn.chatgpt.com/docs/sandboxing#prerequisites
# Use the distribution's bwrap-specific AppArmor profile; keep global
# namespace restrictions and the Codex read-only permission profile intact.
set -euo pipefail
sudo apt-get update -qq
sudo apt-get install -y -qq bubblewrap apparmor-profiles apparmor-utils
profile=/etc/apparmor.d/bwrap-userns-restrict
extra=/usr/share/apparmor/extra-profiles/bwrap-userns-restrict
if [ ! -f "$profile" ] && [ -f "$extra" ]; then
  sudo install -m 0644 "$extra" "$profile"
fi
if [ -f "$profile" ]; then
  sudo apparmor_parser -r "$profile"
fi
bwrap --version
# Fail before credentials/model calls if the actual user/network namespace
# setup still cannot run. The agent's filesystem rules are applied later.
bwrap --unshare-user --unshare-net --ro-bind / / --proc /proc --dev /dev /usr/bin/true
