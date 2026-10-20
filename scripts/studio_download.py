#!/usr/bin/env python3
"""Safe GitHub-attachment download + probe (trusted controller step).

HTTPS trusted-host only, every redirect re-validated, no auth forwarded,
512 MB cap, ffprobe stream check (reject no-audio before full decode).
Inputs come from env/argv set by the workflow from validated data only.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from studio import validate  # noqa: E402
from studio import decode as decmod  # noqa: E402


class ValidatingRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        chain = validate.validate_redirect_chain([req.full_url, newurl])
        if not chain["ok"]:
            raise ValueError(f"redirect rejected: {chain['failures']}")
        # Never forward Authorization to a redirected host.
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is not None:
            new.remove_header("Authorization")
        return new


def download(url: str, dest: Path) -> int:
    v = validate.validate_attachment_url(url)
    if not v["ok"]:
        raise SystemExit(f"attachment URL rejected: {v['failures']}")
    opener = urllib.request.build_opener(ValidatingRedirectHandler())
    req = urllib.request.Request(url, headers={"User-Agent": "studio-controller"})
    total = 0
    with opener.open(req, timeout=validate.config.DOWNLOAD_TIMEOUT_SEC) as resp, \
            open(dest, "wb") as f:
        while True:
            chunk = resp.read(1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > validate.config.MAX_SOURCE_BYTES:
                raise SystemExit("source exceeds 512 MB controller cap")
            f.write(chunk)
    return total


def probe(path: Path) -> dict:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_streams", "-show_format",
         "-of", "json", str(path)], capture_output=True, text=True,
        timeout=120, check=True).stdout
    return json.loads(out)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--meta-out", required=True)
    ap.add_argument("--decode-out", default="",
                    help="also decode losslessly to this WAV (native subtype kept)")
    a = ap.parse_args()
    dest = Path(a.out)
    dest.parent.mkdir(parents=True, exist_ok=True)
    size = download(a.url, dest)
    info = probe(dest)
    audios = [s for s in info.get("streams", []) if s.get("codec_type") == "audio"]
    if not audios:
        print(json.dumps({"ok": False, "failures": ["no audio stream"]}))
        return 2
    s0 = audios[0]
    dur = float(info.get("format", {}).get("duration", 0) or 0)
    lim = validate.validate_media_limits(
        size_bytes=size, duration_sec=dur,
        sample_rate=int(s0.get("sample_rate", 0) or 0),
        channels=int(s0.get("channels", 0) or 0))
    meta = {"ok": lim["ok"], "failures": lim["failures"], "size_bytes": size,
            "duration_sec": dur, "sample_rate": s0.get("sample_rate"),
            "channels": s0.get("channels"), "codec": s0.get("codec_name"),
            "attachment_sha256": decmod.sha256_file(dest)}
    if a.decode_out and lim["ok"]:
        dec = decmod.decode_lossless(dest, a.decode_out)
        meta["decode"] = dec
        meta["decoded_wav_sha256"] = decmod.sha256_file(a.decode_out)
    Path(a.meta_out).write_text(json.dumps(meta, indent=2))
    print(json.dumps(meta))
    return 0 if lim["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
