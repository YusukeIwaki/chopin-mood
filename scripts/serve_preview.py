#!/usr/bin/env python3
"""Local-only preview server with single-range HTTP Range support (stdlib only).

Why this exists:
    The stdlib ``python -m http.server`` ignores ``Range`` and always
    returns ``200`` with the full body and no ``Accept-Ranges``. Browsers
    then report ``seekable=[0,0]`` for MP4 and seeking snaps back to 0.
    This server keeps ``SimpleHTTPRequestHandler`` path handling and adds
    the minimal single-range support browsers need for ``<video>`` seeks.

Security:
    Binds to 127.0.0.1 only. No LAN exposure. Serves static files only.

Usage:
    .venv/bin/python scripts/serve_preview.py --port 8765
    .venv/bin/python scripts/serve_preview.py --port 8765 --directory output
"""

from __future__ import annotations

import argparse
import datetime
import email.utils
import os
from functools import partial
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


def parse_single_range(value: str, size: int):
    """Parse a single ``bytes=`` range. Returns (start, end) inclusive.

    Returns:
        tuple (start, end) on success,
        "unsatisfiable" when the range cannot be served (416),
        None when the header should be ignored (serve 200).
    """
    if size <= 0:
        return "unsatisfiable"
    if not value.startswith("bytes="):
        return None
    spec = value[len("bytes="):].strip()
    if not spec or "," in spec:
        # Multiple ranges are not supported; serve 200 per RFC 7233 3.1.
        return None
    if "-" not in spec:
        return None
    start_s, _, end_s = spec.partition("-")
    start_s = start_s.strip()
    end_s = end_s.strip()
    try:
        if start_s == "" and end_s == "":
            return None
        if start_s == "":
            # Suffix: last N bytes.
            suffix = int(end_s)
            if suffix <= 0:
                return "unsatisfiable"
            if suffix >= size:
                return (0, size - 1)
            return (size - suffix, size - 1)
        if end_s == "":
            start = int(start_s)
            if start < 0 or start >= size:
                return "unsatisfiable"
            return (start, size - 1)
        start = int(start_s)
        end = int(end_s)
        if start < 0 or end < start or start >= size:
            return "unsatisfiable"
        return (start, min(end, size - 1))
    except ValueError:
        return None


class _BoundedFile:
    """File wrapper exposing only ``remaining`` bytes from current offset."""

    def __init__(self, raw, remaining: int):
        self._raw = raw
        self._remaining = remaining

    def read(self, size: int = -1):
        if self._remaining <= 0:
            return b""
        if size is None or size < 0:
            size = self._remaining
        else:
            size = min(size, self._remaining)
        data = self._raw.read(size)
        self._remaining -= len(data)
        return data

    def close(self):
        try:
            self._raw.close()
        except Exception:
            pass

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
        return False


class PreviewHandler(SimpleHTTPRequestHandler):
    """SimpleHTTPRequestHandler + single byte-range support."""

    def send_head(self):
        path = self.translate_path(self.path)
        if os.path.isdir(path):
            # Reuse directory handling (redirect / listing / index).
            # Directory responses never carry partial content.
            return super().send_head()
        ctype = self.guess_type(path)
        if path.endswith("/"):
            self.send_error(HTTPStatus.NOT_FOUND, "File not found")
            return None
        try:
            f = open(path, "rb")
        except OSError:
            self.send_error(HTTPStatus.NOT_FOUND, "File not found")
            return None
        try:
            fs = os.fstat(f.fileno())
            size = fs[6]
            # Keep browser-cache behaviour for unconditional full GETs.
            if (
                "If-Modified-Since" in self.headers
                and "If-None-Match" not in self.headers
                and "Range" not in self.headers
            ):
                try:
                    ims = email.utils.parsedate_to_datetime(
                        self.headers["If-Modified-Since"]
                    )
                except (TypeError, IndexError, OverflowError, ValueError):
                    pass
                else:
                    if ims.tzinfo is None:
                        ims = ims.replace(tzinfo=datetime.timezone.utc)
                    if ims.tzinfo is datetime.timezone.utc:
                        last_modif = datetime.datetime.fromtimestamp(
                            fs.st_mtime, datetime.timezone.utc
                        ).replace(microsecond=0)
                        if last_modif <= ims:
                            self.send_response(HTTPStatus.NOT_MODIFIED)
                            self.end_headers()
                            f.close()
                            return None
            range_value = self.headers.get("Range")
            if range_value is not None:
                parsed = parse_single_range(range_value.strip(), size)
                if parsed == "unsatisfiable":
                    self.send_response(
                        HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE
                    )
                    self.send_header("Content-Range", f"bytes */{size}")
                    self.send_header("Accept-Ranges", "bytes")
                    self.send_header("Content-Length", "0")
                    self.send_header(
                        "Last-Modified", self.date_time_string(fs.st_mtime)
                    )
                    self.end_headers()
                    f.close()
                    return None
                if parsed is not None:
                    start, end = parsed
                    length = end - start + 1
                    self.send_response(HTTPStatus.PARTIAL_CONTENT)
                    self.send_header("Content-type", ctype)
                    self.send_header(
                        "Content-Range", f"bytes {start}-{end}/{size}"
                    )
                    self.send_header("Content-Length", str(length))
                    self.send_header("Accept-Ranges", "bytes")
                    self.send_header(
                        "Last-Modified", self.date_time_string(fs.st_mtime)
                    )
                    self.end_headers()
                    f.seek(start)
                    return _BoundedFile(f, length)
                # Invalid/multiple ranges: fall through to 200.
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-type", ctype)
            self.send_header("Content-Length", str(size))
            self.send_header("Accept-Ranges", "bytes")
            self.send_header(
                "Last-Modified", self.date_time_string(fs.st_mtime)
            )
            self.end_headers()
            return f
        except Exception:
            f.close()
            raise


def build_parser() -> argparse.ArgumentParser:
    repo_root = Path(__file__).resolve().parent.parent
    default_dir = repo_root / "output"
    parser = argparse.ArgumentParser(
        description="127.0.0.1-only static preview server with Range support."
    )
    parser.add_argument(
        "--port", type=int, default=8765, help="Local port (default: 8765)."
    )
    parser.add_argument(
        "--directory",
        type=str,
        default=str(default_dir),
        help="Directory to serve (default: repo output/).",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    directory = os.path.abspath(args.directory)
    if not os.path.isdir(directory):
        raise SystemExit(f"not a directory: {directory}")
    if not (1 <= args.port <= 65535):
        raise SystemExit(f"invalid port: {args.port}")
    handler = partial(PreviewHandler, directory=directory)
    # 127.0.0.1 only: never expose preview files to the LAN.
    with ThreadingHTTPServer(("127.0.0.1", args.port), handler) as httpd:
        print(
            f"serving {directory} on http://127.0.0.1:{args.port}/ "
            "(local only, Range supported)"
        )
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
