#!/usr/bin/env python3
"""Create/reuse sub-issues via the production controller (GITHUB_TOKEN).

The authentic bot planning record on the parent issue wins on retry:
original candidates+briefs are reused; a changed fingerprint fails.
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
    ap.add_argument("--candidates", required=True)
    ap.add_argument("--selection", required=True)
    ap.add_argument("--parent", required=True, type=int)
    ap.add_argument("--fingerprint", required=True)
    ap.add_argument("--creative", default="")
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    cands = json.loads(Path(a.candidates).read_text())["candidates"]
    sel = json.loads(Path(a.selection).read_text())
    creative = json.loads(Path(a.creative).read_text()) if a.creative else {}
    gh = GitHubClient(os.environ["GITHUB_REPOSITORY"], os.environ["GH_TOKEN"])
    r = controller.ensure_children_controller(
        gh, parent=a.parent, candidates=cands, planner_output=sel,
        creative=creative, fp=a.fingerprint)
    if not r["ok"]:
        print(json.dumps(r))
        return 2
    Path(a.out).write_text(json.dumps({"include": r["matrix"]}, indent=2))
    print(json.dumps({"children": [m["child_issue"] for m in r["matrix"]],
                      "reused_plan": r["reused_plan"]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
