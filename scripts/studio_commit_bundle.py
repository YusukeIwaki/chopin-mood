#!/usr/bin/env python3
"""Atomic Git-tree commit of the production bundle + PR create/reuse."""
from __future__ import annotations
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import argparse
import json
import os
from pathlib import Path

from studio import controller
from studio.github_client import GitHubClient


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--child", required=True, type=int)
    ap.add_argument("--bundle-dir", required=True)
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    bundle = Path(a.bundle_dir)
    files: dict[str, bytes] = {}
    for p in sorted(bundle.iterdir()):
        if p.is_symlink():
            print(json.dumps({"ok": False, "failures": [f"symlink in bundle: {p.name}"]}))
            return 2
        if p.is_file():
            files[p.name] = p.read_bytes()
    gh = GitHubClient(os.environ["GITHUB_REPOSITORY"], os.environ["GH_TOKEN"])
    r = controller.commit_bundle_controller(
        gh, child=a.child, files=files,
        repo_owner=os.environ["GITHUB_REPOSITORY_OWNER"],
        engine_base_sha=os.environ.get("GITHUB_SHA"))
    if not r.get("ok"):
        print(json.dumps(r))
        return 2
    Path(a.out).write_text(json.dumps(r, indent=2))
    print(json.dumps(r))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
