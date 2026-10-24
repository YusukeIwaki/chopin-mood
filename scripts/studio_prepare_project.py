#!/usr/bin/env python3
"""Prepare a saved Codex project for deterministic production."""
import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from studio.project import load_project
from studio.github_client import GitHubClient


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--issue", type=int, required=True)
    ap.add_argument("--url", required=True)
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    gh = GitHubClient(os.environ["GITHUB_REPOSITORY"], os.environ["GH_TOKEN"])
    data = load_project(Path("studio/projects"), a.issue, a.url, gh)
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    for name, value in data.items():
        (out / f"{name}.json").write_text(json.dumps(value, indent=2))
    with open(os.environ["GITHUB_OUTPUT"], "a") as f:
        f.write("matrix=" + json.dumps(data["matrix"], separators=(",", ":")) + "\n")
        f.write("fingerprint=" + data["brief"]["fingerprint"] + "\n")
    print("Validated saved Codex project; no child issues or content were generated.")


if __name__ == "__main__":
    main()
