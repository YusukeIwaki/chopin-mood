#!/usr/bin/env python3
"""Carry the last automated critique into an explicit production retry."""
import argparse
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from studio import config
from studio.github_client import GitHubClient


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--child", required=True, type=int)
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    gh = GitHubClient(os.environ["GITHUB_REPOSITORY"], os.environ["GH_TOKEN"])
    owner = gh.repo.split("/")[0]
    prs = gh.paginated(f"/pulls?head={owner}:{config.PRODUCTION_BRANCH_PREFIX}{a.child}&state=open")
    feedback = "No previous automated critique."
    if prs:
        reviews = gh.paginated(f"/pulls/{prs[0]['number']}/reviews")
        critiques = [r for r in reviews if (r.get("user") or {}).get("login") in config.BOT_LOGINS
                     and r.get("state") == "COMMENTED"
                     and (r.get("body") or "").startswith("Automated review did not pass;")]
        if critiques:
            feedback = critiques[-1]["body"][:12000]
    Path(a.out).write_text(feedback)


if __name__ == "__main__":
    main()
