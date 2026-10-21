#!/usr/bin/env python3
"""Publish Codex review via the production controller.

Pass: independently ensure receipt comment, exact-head COMMENT ping
review and assignee (retry-safe). Fail: feedback review carrying the
actual blockers/instructions, no ping, plus a deduplicated bot
engineering issue for final engine/limit/no-progress outcomes when a
``--repair-result`` is supplied. Pass behaviour is unchanged.
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


def _append_publish_summary(result: dict, *, repo: str, run_id: int,
                            run_attempt: int) -> None:
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    run_url = result.get("run_url") or controller.run_url_for(repo or None, run_id)
    lines = [f"Studio publish: ok={result.get('ok')} run={run_url} "
             f"(attempt {run_attempt})"]
    eng = result.get("engineering") if isinstance(result, dict) else None
    if isinstance(eng, dict) and eng.get("issue_number"):
        url = result.get("engineering_url") or eng.get("html_url") or \
            controller.issue_url_for(repo or None, eng["issue_number"])
        if eng.get("action") == "closed_not_reopened" or eng.get("state") == "closed":
            lines.append(
                f"Engineering issue: {url} (closed; not reopened - owner decision; "
                "no open handoff claimed)")
            lines.append(
                "Next action: owner decision on the closed engineering issue; "
                "do not rerun production until the owner decides.")
        else:
            lines.append(f"Engineering issue: {url} (open)")
            lines.append(
                f"Next action: fix via engineering issue {url}, review and merge "
                "the engineering PR, then owner re-dispatches the source workflow.")
    elif not result.get("ok"):
        lines.append(
            "Next action: address the failure feedback on the PR; "
            "no owner ping was sent for this failure.")
    try:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")
    except OSError:
        pass


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
    ap.add_argument("--repair-result", default="",
                    help="optional repair-result.json from studio_repair.py")
    ap.add_argument("--parent", type=int, default=None,
                    help="optional parent issue (defaults to repair result)")
    a = ap.parse_args()
    review = json.loads(Path(a.review).read_text())
    repair_result = None
    if a.repair_result:
        repair_result = json.loads(Path(a.repair_result).read_text())
        if not isinstance(repair_result, dict):
            print(json.dumps({"ok": False, "failures": [
                "repair result must be an object"]}))
            return 2
        rr_head = repair_result.get("final_head_sha") or repair_result.get("head_sha")
        if rr_head is not None and rr_head != a.head_sha:
            print(json.dumps({"ok": False, "failures": [
                "repair result head does not match --head-sha (stale)"]}))
            return 2
        rr_pr = repair_result.get("pr_number", repair_result.get("final_pr_number"))
        if rr_pr is not None and int(rr_pr) != int(a.pr):
            print(json.dumps({"ok": False, "failures": [
                "repair result PR does not match --pr"]}))
            return 2
    parent = a.parent
    if parent is None and isinstance(repair_result, dict):
        parent = repair_result.get("parent_issue", repair_result.get("parent"))
    gh = GitHubClient(os.environ["GITHUB_REPOSITORY"], os.environ["GH_TOKEN"])
    r = controller.publish_review_controller(
        gh, pr_number=a.pr, child=a.child, head_sha=a.head_sha,
        run_id=a.run_id, run_attempt=a.run_attempt, artifact_id=a.artifact_id,
        artifact_url=a.artifact_url, review=review,
        mechanical_pass=a.mechanical_pass,
        repair_result=repair_result,
        parent=int(parent) if parent is not None else None)
    print(json.dumps(r))
    _append_publish_summary(r, repo=os.environ.get("GITHUB_REPOSITORY", ""),
                            run_id=a.run_id, run_attempt=a.run_attempt)
    # Gate failures publish feedback and stay green for a workflow re-run;
    # stale-head / closed-PR failures are real errors.
    if not r["ok"] and any("stale" in f or "not open" in f for f in r["failures"]):
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
