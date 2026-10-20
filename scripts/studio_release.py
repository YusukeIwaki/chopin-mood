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

from studio import controller
from studio.github_client import GitHubClient


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pr", required=True, type=int)
    a = ap.parse_args()
    via_dispatch = os.environ.get("EVENT_NAME") == "workflow_dispatch"
    gh = GitHubClient(os.environ["GITHUB_REPOSITORY"], os.environ["GH_TOKEN"])
    r = controller.release_controller(
        gh, pr_number=a.pr, actor=os.environ.get("DISPATCH_ACTOR"),
        via_dispatch=via_dispatch)
    print(json.dumps(r))
    if r.get("skipped"):
        return 0
    return 0 if r.get("ok") else 2


if __name__ == "__main__":
    raise SystemExit(main())
