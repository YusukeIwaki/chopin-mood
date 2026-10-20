#!/usr/bin/env python3
"""Check the final answer, not an 'OK' appearing in the input/event log."""
import json
from pathlib import Path
import sys

records = [json.loads(line) for line in Path(sys.argv[1]).read_text().splitlines() if line.strip()]
completed = [r["payload"].get("text", "").strip() for r in records
             if r.get("payload_type") == "run.terminal.completed"]
if not completed or completed[-1] != "OK":
    raise SystemExit("Muse did not complete the exact-model authentication smoke with OK")
