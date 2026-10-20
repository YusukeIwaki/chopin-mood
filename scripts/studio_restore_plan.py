#!/usr/bin/env python3
"""Reuse the original bot plan before spending another planner call."""
import argparse
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from studio.fingerprint import find_bot_planning_record
from studio.github_client import GitHubClient


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--parent", required=True, type=int)
    ap.add_argument("--fingerprint", required=True)
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    gh = GitHubClient(os.environ["GITHUB_REPOSITORY"], os.environ["GH_TOKEN"])
    record = find_bot_planning_record(gh.paginated(f"/issues/{a.parent}/comments"))
    if record is not None:
        if record["fingerprint"] != a.fingerprint:
            raise SystemExit("Source/direction changed after planning. Create a new source Issue.")
        selected = {"selected_candidate_ids": record["selected_candidate_ids"],
                    "briefs": record["briefs"], "rationale": record["rationale"]}
        out = Path(a.out)
        (out / "selection.json").write_text(json.dumps(selected, ensure_ascii=False))
        if "candidates" not in record:
            raise SystemExit("Stored plan predates reproducible candidates; create a new source Issue.")
        (out / "candidates.json").write_text(json.dumps({"candidates": record["candidates"]}))
    with open(os.environ["GITHUB_OUTPUT"], "a") as f:
        print(f"reused={'true' if record is not None else 'false'}", file=f)


if __name__ == "__main__":
    main()
