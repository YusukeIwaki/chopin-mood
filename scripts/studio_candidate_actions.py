#!/usr/bin/env python3
"""Actions adapter for direct-source candidate PRs; execute from trusted main."""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from studio import candidate_pipeline as pipeline, config, repair
from studio.github_client import GitHubClient


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("command", choices=["prepare", "seal", "review", "check", "publish"])
    ap.add_argument("--work", required=True, type=Path)
    ap.add_argument("--pr", type=int)
    a = ap.parse_args()
    work = a.work
    if a.command == "prepare":
        if os.environ.get("GITHUB_REF") != "refs/heads/main" or os.environ.get("GITHUB_EVENT_NAME") != "workflow_dispatch":
            raise ValueError("candidate reproduction requires manual dispatch on main")
        if os.environ.get("GITHUB_TRIGGERING_ACTOR") != config.OWNER:
            raise ValueError("candidate rerun operator must be the owner")
        gh = GitHubClient(os.environ["GITHUB_REPOSITORY"], os.environ["GH_TOKEN"])
        prepared = pipeline.prepare_candidate(gh, pr_number=a.pr, actor=os.environ["GITHUB_ACTOR"], engine_sha=os.environ["GITHUB_SHA"])
        ctx = pipeline.write_prepared(prepared, work)
        project = json.loads((work / "project.json").read_text())
        with open(os.environ["GITHUB_OUTPUT"], "a") as f:
            for key in ("pr", "head_sha", "source_issue", "candidate_id"):
                f.write(f"{key}={ctx[key]}\n")
            f.write(f"source_url={project['source_url']}\n")
        print(json.dumps({"ok": True, **ctx}))
    elif a.command == "seal":
        pipeline.seal_candidate(work, run_id=int(os.environ["GITHUB_RUN_ID"]), run_attempt=int(os.environ["GITHUB_RUN_ATTEMPT"]))
        print('{"ok":true}')
    elif a.command == "review":
        bundle = work / "bundle"
        load = lambda name: json.loads((bundle / f"{name}.json").read_text())
        receipt = load("receipt")
        gh = GitHubClient(os.environ["GITHUB_REPOSITORY"], os.environ["GH_TOKEN"])
        pipeline.verify_candidate_guard(gh, receipt)
        prompt = repair.build_codex_review_prompt(
            (ROOT / ".github/studio/prompts/codex-review.md").read_text(),
            brief=load("brief"), storyboard=load("storyboard"),
            production_files=receipt["input_sha256"], expected_head_sha=receipt["head_sha"],
            plan=load("plan"), qa=load("qa"), hashes=load("hashes"),
            provenance=load("provenance"), receipt=receipt)
        prompt += "\nLocal reviewers' conclusions are intentionally omitted. Independently judge this output. Prefer simple, purposeful movement; novelty alone does not justify extra effects. No musical-listening claim is permitted.\n"
        temp = Path(os.environ["RUNNER_TEMP"]) / "candidate-final-review"
        temp.mkdir(parents=True, exist_ok=True)
        prompt_path = temp / "prompt.md"
        prompt_path.write_text(prompt)
        repair.run_codex_review(contact_sheet=bundle / "contact-sheet.jpg",
            schema_path=ROOT / ".github/studio/schemas/review.schema.json",
            output_path=bundle / "review.json", prompt_path=prompt_path,
            runner_temp=temp, report_script=ROOT / ".github/studio/scripts/report-agent-error.py")
        pipeline.refresh_manifest(bundle)
        result = pipeline.validate_bundle(bundle)
        print(json.dumps({"ok": True, "verdict": result["review"]["verdict"]}))
    elif a.command == "check":
        pipeline.validate_bundle(work / "bundle")
        print('{"ok":true}')
    elif a.command == "publish":
        gh = GitHubClient(os.environ["GITHUB_REPOSITORY"], os.environ["GH_TOKEN"])
        result = pipeline.publish_candidate(gh, work=work, artifact_id=int(os.environ["ARTIFACT_ID"]), artifact_url=os.environ["ARTIFACT_URL"])
        print(json.dumps(result))
        return 0 if result["ok"] else 2
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except repair.RepairInfrastructureError:
        # The runner already emitted a safe category; never print provider logs.
        print('{"ok":false,"error":"candidate reviewer infrastructure failure"}')
        raise SystemExit(2)
    except (ValueError, KeyError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}))
        raise SystemExit(2)
