#!/usr/bin/env python3
"""Publish Codex review via the production controller.

Pass: independently ensure receipt comment, exact-head COMMENT ping
review and assignee (retry-safe). Fail: feedback review, no ping.
"""
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
    ap.add_argument("--review", required=True)
    ap.add_argument("--pr", required=True, type=int)
    ap.add_argument("--child", required=True, type=int)
    ap.add_argument("--head-sha", required=True)
    ap.add_argument("--run-id", required=True, type=int)
    ap.add_argument("--run-attempt", required=True, type=int, default=1)
    ap.add_argument("--artifact-id", required=True, type=int)
    ap.add_argument("--artifact-url", default="")
    ap.add_argument("--mechanical-pass", action="store_true")
    a = ap.parse_args()
    review = json.loads(Path(a.review).read_text())
    gh = GitHubClient(os.environ["GITHUB_REPOSITORY"], os.environ["GH_TOKEN"])
    r = controller.publish_review_controller(
        gh, pr_number=a.pr, child=a.child, head_sha=a.head_sha,
        run_id=a.run_id, run_attempt=a.run_attempt, artifact_id=a.artifact_id,
        artifact_url=a.artifact_url, review=review,
        mechanical_pass=a.mechanical_pass)
    print(json.dumps(r))
    # Gate failures publish feedback and stay green for a workflow re-run;
    # stale-head / closed-PR failures are real errors.
    if not r["ok"] and any("stale" in f or "not open" in f for f in r["failures"]):
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
