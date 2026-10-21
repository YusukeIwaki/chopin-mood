#!/usr/bin/env python3
"""Carry the last automated critique into an explicit production retry.

Includes the COMPLETE stored bot critique (blockers + repair
instructions) and the previous storyboard from the exact current open
PR head as bounded, data-only JSON when available. Legacy summary-only
critiques are marked explicitly incomplete; missing blockers are never
invented. User-crafted text is never trusted as bot critique.
"""
import argparse
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from studio import controller
from studio.github_client import GitHubClient


def build_feedback(gh, *, child: int) -> str:
    """Reusable retry-feedback text (see controller.build_retry_feedback)."""
    return controller.build_retry_feedback(gh, child=child)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--child", required=True, type=int)
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    gh = GitHubClient(os.environ["GITHUB_REPOSITORY"], os.environ["GH_TOKEN"])
    feedback = build_feedback(gh, child=a.child)
    out = Path(a.out)
    if out.parent and str(out.parent) not in ("", "."):
        out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(feedback)


if __name__ == "__main__":
    main()
