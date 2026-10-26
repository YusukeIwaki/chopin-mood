#!/usr/bin/env python3
"""Release via the production controller (trusted main code only).

Non-studio PR closures skip gracefully (exit 0); dispatch by a
non-owner fails. Nothing is regenerated; mismatched assets fail.
"""
from __future__ import annotations
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import argparse
import json
import os

from studio import candidate_release, controller
from studio.github_client import GitHubClient


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pr", required=True, type=int)
    ap.add_argument("--stage", action="store_true",
                    help="Verify/upload the selected assets, keeping drafts private and Issues open")
    a = ap.parse_args()
    via_dispatch = os.environ.get("EVENT_NAME") == "workflow_dispatch"
    gh = GitHubClient(os.environ["GITHUB_REPOSITORY"], os.environ["GH_TOKEN"])
    pr = gh.request("GET", f"/pulls/{a.pr}")
    branch = (pr.get("head") or {}).get("ref", "")
    release = (candidate_release.release_controller
               if branch.startswith("codex/studio-issue-")
               else controller.release_controller)
    r = release(
        gh, pr_number=a.pr, actor=os.environ.get("DISPATCH_ACTOR"),
        via_dispatch=via_dispatch, stage_only=a.stage)
    print(json.dumps(r))
    if r.get("skipped"):
        return 0
    if r.get("ok") and os.environ.get("GITHUB_OUTPUT"):
        from studio.publication import TAG_RE
        if not TAG_RE.fullmatch(r.get("tag", "")):
            raise ValueError("unexpected release tag")
        with open(os.environ["GITHUB_OUTPUT"], "a") as out:
            out.write(f"tag={r['tag']}\n")
    return 0 if r.get("ok") else 2


if __name__ == "__main__":
    raise SystemExit(main())
