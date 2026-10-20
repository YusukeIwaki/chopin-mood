"""Studio controller CLI (stdlib only). Subcommands print JSON verdicts.

Exit 0 = gate passed, 2 = gate failed (failures listed), 1 = usage error.
Workflows call these with inputs via files/env, never shell interpolation
of untrusted issue/model text.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import artifacts, candidates, config, fingerprint, gating, storyboard, validate


def _out(obj: dict, ok: bool) -> int:
    print(json.dumps(obj, ensure_ascii=False, indent=2))
    return 0 if ok else 2


def _load(path: str):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="studio")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("fingerprint")
    p.add_argument("--source-url", required=True)
    p.add_argument("--source-sha256", required=True)
    p.add_argument("--piece", default="")
    p.add_argument("--period", default="")
    p.add_argument("--art-notes", default="")

    p = sub.add_parser("validate-url")
    p.add_argument("--url", required=True)

    p = sub.add_parser("validate-planner")
    p.add_argument("--candidates", required=True)
    p.add_argument("--selection", required=True)

    p = sub.add_parser("validate-storyboard")
    p.add_argument("--storyboard", required=True)
    p.add_argument("--scenes", required=True)

    p = sub.add_parser("validate-candidates")
    p.add_argument("--candidates", required=True)

    p = sub.add_parser("check-release")
    p.add_argument("--context", required=True)

    p = sub.add_parser("check-review")
    p.add_argument("--review", required=True)
    p.add_argument("--mechanical-pass", action="store_true")
    p.add_argument("--head-sha", default="")

    p = sub.add_parser("validate-zip")
    p.add_argument("--zip", required=True)
    p.add_argument("--manifest", default="")

    p = sub.add_parser("intake")
    p.add_argument("--author", default="")
    p.add_argument("--sender", default="")
    p.add_argument("--state", default="")
    p.add_argument("--dispatch-actor", default="")

    p = sub.add_parser("creative")
    p.add_argument("--body-file", required=True)
    p.add_argument("--out", required=True)

    p = sub.add_parser("make-receipt")
    p.add_argument("--pr", required=True, type=int)
    p.add_argument("--child", required=True, type=int)
    p.add_argument("--parent", required=True, type=int)
    p.add_argument("--head-sha", required=True)
    p.add_argument("--run-id", required=True, type=int)
    p.add_argument("--run-attempt", required=True, type=int)
    p.add_argument("--engine-sha", required=True)
    p.add_argument("--attachment-sha256", required=True)
    p.add_argument("--decoded-sha256", required=True)
    p.add_argument("--fingerprint", required=True)
    p.add_argument("--production-map", required=True,
                   help="JSON file {repo/path: sha256} from commit step")
    p.add_argument("--out", required=True)

    p = sub.add_parser("check-bundle")
    p.add_argument("--dir", required=True)

    p = sub.add_parser("timeline-html")
    p.add_argument("--timeline", required=True)
    p.add_argument("--out", required=True)

    args = ap.parse_args(argv)

    if args.cmd == "fingerprint":
        fp = fingerprint.canonical_fingerprint(
            source_url=args.source_url, source_sha256=args.source_sha256,
            piece=args.piece, period=args.period, art_notes=args.art_notes)
        return _out({"fingerprint": fp, "marker": fingerprint.fingerprint_marker(fp)}, True)
    if args.cmd == "validate-url":
        r = validate.validate_attachment_url(args.url)
        return _out(r, r["ok"])
    if args.cmd == "validate-planner":
        cands = _load(args.candidates)
        sel = _load(args.selection)
        r = candidates.validate_planner_output(sel, cands.get("candidates", cands))
        return _out({"ok": r["ok"], "failures": r["failures"],
                     "selected_ids": [c["id"] for c in r["selected"]]}, r["ok"])
    if args.cmd == "validate-storyboard":
        r = storyboard.validate_storyboard(_load(args.storyboard),
                                           _load(args.scenes).get("scenes", []))
        return _out(r, r["ok"])
    if args.cmd == "validate-candidates":
        cands = _load(args.candidates)
        errs: dict[str, list[str]] = {}
        for c in cands.get("candidates", []):
            e = candidates.validate_candidate(c)
            if e:
                errs[c.get("id", "?")] = e
        return _out({"ok": not errs, "errors": errs}, not errs)
    if args.cmd == "check-release":
        r = gating.evaluate_release_gate(_load(args.context))
        return _out(r, r["ok"])
    if args.cmd == "check-review":
        r = gating.evaluate_review_gate(
            _load(args.review), mechanical_pass=args.mechanical_pass,
            expected_head_sha=args.head_sha or None)
        return _out(r, r["ok"])
    if args.cmd == "validate-zip":
        data = Path(args.zip).read_bytes()
        r = artifacts.read_and_validate_zip(
            data, expected=set(config.EXPECTED_ARTIFACT_FILES) if not args.manifest else None)
        if r["ok"] and args.manifest:
            m = _load(args.manifest)
            v = artifacts.verify_hash_manifest(m, r["files"])
            return _out(v, v["ok"])
        return _out({"ok": r["ok"], "failures": r["failures"],
                     "files": sorted(r["files"])}, r["ok"])
    if args.cmd == "creative":
        from . import creative as _creative
        body = Path(args.body_file).read_text(encoding="utf-8")
        result = _creative.parse_issue_body(body)
        Path(args.out).write_text(json.dumps(result, ensure_ascii=False, indent=2))
        return _out(result, True)
    if args.cmd == "make-receipt":
        prod = _load(args.production_map)
        receipt = artifacts.build_receipt(
            pr=args.pr, child=args.child, parent=args.parent,
            head_sha=args.head_sha, run_id=args.run_id,
            run_attempt=args.run_attempt, engine_sha=args.engine_sha,
            source_hashes={"attachment_sha256": args.attachment_sha256,
                           "decoded_wav_sha256": args.decoded_sha256},
            planning_fingerprint=args.fingerprint, production_files=prod)
        Path(args.out).write_text(json.dumps(receipt, indent=2))
        return _out(receipt, True)
    if args.cmd == "check-bundle":
        d = Path(args.dir)
        got = {p.name for p in d.iterdir() if p.is_file()}
        want = set(config.EXPECTED_ARTIFACT_FILES)
        failures = []
        if got != want:
            failures.append(f"bundle file set mismatch: got {sorted(got)} want {sorted(want)}")
        for p in d.iterdir():
            if p.is_symlink():
                failures.append(f"symlink in bundle: {p.name}")
        return _out({"ok": not failures, "failures": failures,
                     "files": sorted(got)}, not failures)
    if args.cmd == "timeline-html":
        html = artifacts.build_timeline_html(_load(args.timeline))
        Path(args.out).write_text(html, encoding="utf-8")
        return _out({"ok": True, "out": args.out}, True)
    if args.cmd == "intake":
        r = validate.validate_intake(
            author=args.author or None, sender=args.sender or None,
            issue_state=args.state or None,
            via_dispatch=bool(args.dispatch_actor),
            dispatch_actor=args.dispatch_actor or None)
        return _out(r, r["ok"])
    return 1


if __name__ == "__main__":
    sys.exit(main())
