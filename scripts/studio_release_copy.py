#!/usr/bin/env python3
"""Prepare, generate and save text for a verified Studio Release (no media changes)."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from studio import publication
from studio.github_client import GitHubClient, GitHubError
from studio.repair import RepairInfrastructureError


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("prepare", "generate", "publish"))
    parser.add_argument("--tag", help="Exact published Studio release tag (prepare only)")
    parser.add_argument("--work", type=Path, required=True, help="Private runner work directory")
    args = parser.parse_args()
    args.work.mkdir(parents=True, exist_ok=True)
    context_path = args.work / "prepared.json"
    try:
        if args.command == "prepare":
            if not args.tag:
                parser.error("prepare requires --tag")
            gh = GitHubClient(os.environ["GITHUB_REPOSITORY"], os.environ["GH_TOKEN"])
            prepared = publication.prepare(gh, args.tag)
            context_path.write_text(json.dumps(prepared, ensure_ascii=False, indent=2))
            if os.environ.get("GITHUB_OUTPUT"):
                with open(os.environ["GITHUB_OUTPUT"], "a") as out:
                    out.write(f"needed={str(prepared['needed']).lower()}\n")
            result = {k: prepared[k] for k in ("needed", "tag", "reason") if k in prepared}
        else:
            prepared = json.loads(context_path.read_text())
            if args.command == "generate":
                publication.generate(prepared, args.work)
                result = {"ok": True, "generated": True}
            else:
                gh = GitHubClient(os.environ["GITHUB_REPOSITORY"], os.environ["GH_TOKEN"])
                result = publication.publish(gh, prepared, json.loads((args.work / "copy.json").read_text()))
        print(json.dumps(result))
        return 0
    except GitHubError as exc:
        print(f"GitHub operation failed ({exc.status}); no media was changed", file=sys.stderr)
    except (ValueError, OSError, KeyError, RepairInfrastructureError) as exc:
        # Model stdout/stderr and untrusted GitHub response bodies stay off logs.
        print(f"Publication copy failed ({type(exc).__name__}); inspect runner state or rerun the failed job",
              file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
