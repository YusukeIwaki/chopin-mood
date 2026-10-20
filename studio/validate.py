"""Pure validation helpers: intake auth, attachment URLs, paths, IDs.

Stdlib only. Every function returns data (never raises on untrusted input
except where documented) so workflows can render a graceful job summary.
"""
from __future__ import annotations

import re
from pathlib import PurePosixPath
from urllib.parse import urlparse

from . import config

_URL_RE = re.compile(r"https://[^\s)\]<>\"']+")


def is_owner(login: str | None) -> bool:
    return bool(login) and login == config.OWNER


def validate_intake(*, author: str | None, sender: str | None = None,
                    issue_state: str | None, via_dispatch: bool = False,
                    dispatch_actor: str | None = None) -> dict:
    """Owner-only intake gate for issue events and manual dispatch.

    The source-issue author must be the owner in BOTH paths; manual
    dispatch additionally requires the dispatch operator to be the owner
    (it never waives the author check).
    """
    failures: list[str] = []
    if not is_owner(author):
        failures.append("issue author must be the repository owner")
    if via_dispatch:
        if not is_owner(dispatch_actor):
            failures.append("dispatch operator must be the repository owner")
    elif not is_owner(sender):
        failures.append("event sender must be the repository owner")
    if issue_state != "open":
        failures.append("source issue must be open")
    return {"ok": not failures, "failures": failures}


def extract_attachment_urls(body: str | None) -> list[str]:
    """All https URLs in an issue body, order-preserved, de-duplicated."""
    if not body:
        return []
    seen: list[str] = []
    for url in _URL_RE.findall(body):
        url = url.rstrip(".,;")
        if url not in seen:
            seen.append(url)
    return seen


def validate_attachment_url(url: str) -> dict:
    """Exactly one trusted GitHub user-attachment URL shape is accepted."""
    failures: list[str] = []
    if not isinstance(url, str) or not url:
        return {"ok": False, "failures": ["empty url"]}
    p = urlparse(url)
    if p.scheme != "https":
        failures.append("scheme must be https")
    if p.username or p.password:
        failures.append("credentials in URL are forbidden")
    host = (p.hostname or "").lower()
    if host in ("localhost", "127.0.0.1", "::1") or host.endswith(".local"):
        failures.append("localhost hosts are forbidden")
    if host not in config.TRUSTED_ATTACHMENT_HOSTS:
        failures.append(f"untrusted host: {host or '?'}")
    if not p.path.startswith(config.TRUSTED_ATTACHMENT_PATH_PREFIX):
        failures.append("path must be a GitHub user-attachment path")
    if ".." in p.path.split("/"):
        failures.append("path traversal segment forbidden")
    return {"ok": not failures, "failures": failures, "host": host,
            "path": p.path}


def _is_ip_literal(host: str) -> bool:
    import ipaddress
    try:
        ipaddress.ip_address(host.strip("[]"))
        return True
    except ValueError:
        return False


def _host_is_private_ip(host: str) -> bool:
    import ipaddress
    try:
        ip = ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return False
    return bool(ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_reserved or ip.is_multicast or ip.is_unspecified)


def is_trusted_redirect_host(host: str) -> bool:
    """Precise GitHub CDN allowlist; no IP literals, no arbitrary hosts."""
    h = (host or "").lower().rstrip(".")
    if not h or _is_ip_literal(h):
        return False
    if h in config.TRUSTED_REDIRECT_HOSTS:
        return True
    return any(h.startswith(pre) and h.endswith(suf)
               for pre in config.TRUSTED_REDIRECT_HOST_PREFIXES
               for suf in config.TRUSTED_REDIRECT_HOST_SUFFIXES)


def validate_redirect_url(url: str, *, hop: int = 0) -> list[str]:
    """One redirect hop: https, no credentials, port 443, allowlisted host."""
    failures: list[str] = []
    p = urlparse(url)
    if p.scheme != "https":
        failures.append(f"hop {hop}: scheme must be https")
    if p.username or p.password:
        failures.append(f"hop {hop}: credentials in URL are forbidden")
    host = (p.hostname or "").lower()
    if not host:
        failures.append(f"hop {hop}: missing host")
        return failures
    if host in ("localhost",) or host.endswith(".local") or host.endswith(".localhost"):
        failures.append(f"hop {hop}: localhost forbidden")
    if _host_is_private_ip(host):
        failures.append(f"hop {hop}: private/link-local IP forbidden: {host}")
    if _is_ip_literal(host):
        failures.append(f"hop {hop}: IP-literal hosts are forbidden: {host}")
    try:
        port = p.port
    except ValueError:
        port = -1
    if port not in (None, 443):
        failures.append(f"hop {hop}: port must be 443")
    if hop > 0 and not is_trusted_redirect_host(host):
        failures.append(f"hop {hop}: untrusted redirect host: {host}")
    return failures


def validate_redirect_chain(urls: list[str]) -> dict:
    """Hop 0 must be a trusted attachment URL; every later hop must stay
    on the precise GitHub CDN allowlist (https/443, no IPs, no credentials).

    Auth is never forwarded: callers must send no Authorization header on
    redirects (enforced by the downloader, asserted here by contract).
    """
    failures: list[str] = []
    if not urls:
        return {"ok": False, "failures": ["empty redirect chain"]}
    first = validate_attachment_url(urls[0])
    if not first["ok"]:
        failures.extend(f"hop 0: {f}" for f in first["failures"])
    for i, u in enumerate(urls):
        failures.extend(validate_redirect_url(u, hop=i))
    return {"ok": not failures, "failures": failures}


def validate_artifact_storage_url(url: str) -> dict:
    """Signed storage URL reached after dropping Authorization: https,
    port 443, no credentials, no IP literals / private addresses."""
    failures = validate_redirect_url(url, hop=1)
    # Storage hosts are signed and vendor-specific; the IP/port/scheme
    # checks above are the boundary, host allowlist is CDN-specific only
    # for attachment redirects, so drop only the allowlist failure here.
    failures = [f for f in failures if "untrusted redirect host" not in f]
    return {"ok": not failures, "failures": failures}


def validate_media_limits(*, size_bytes: int, duration_sec: float,
                          sample_rate: int, channels: int) -> dict:
    failures: list[str] = []
    if size_bytes > config.MAX_SOURCE_BYTES:
        failures.append(f"size {size_bytes} exceeds {config.MAX_SOURCE_BYTES}")
    if size_bytes <= 0:
        failures.append("empty source")
    if duration_sec > config.MAX_DURATION_SEC:
        failures.append(f"duration {duration_sec} exceeds {config.MAX_DURATION_SEC}")
    if duration_sec <= 0:
        failures.append("no audio duration (reject before full decode)")
    if sample_rate > config.MAX_SAMPLE_RATE or sample_rate <= 0:
        failures.append(f"sample rate {sample_rate} out of bounds")
    if channels > config.MAX_CHANNELS or channels < 1:
        failures.append(f"channels {channels} out of bounds")
    return {"ok": not failures, "failures": failures}


def is_numeric_id(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def validate_issue_number(value) -> bool:
    return is_numeric_id(value) and value < 10_000_000


def safe_relative_path(rel: str) -> dict:
    """Repo-relative path containment: no absolute/traversal/backslash."""
    failures: list[str] = []
    if not isinstance(rel, str) or not rel:
        return {"ok": False, "failures": ["empty path"]}
    if "\\" in rel:
        failures.append("backslash forbidden")
    p = PurePosixPath(rel)
    if p.is_absolute():
        failures.append("absolute path forbidden")
    if any(part in ("..", "") for part in p.parts):
        failures.append("traversal/empty segment forbidden")
    if rel.startswith("/") or "://" in rel:
        failures.append("absolute/URL path forbidden")
    return {"ok": not failures, "failures": failures, "parts": list(p.parts)}


def validate_production_file(rel: str, size_bytes: int) -> dict:
    """Allowlist for files committed under production/<child-id>/."""
    base = safe_relative_path(rel)
    failures = list(base["failures"])
    parts = base.get("parts", [])
    if len(parts) != 3 or parts[0] != "production" or not parts[1].isdigit():
        failures.append("must be production/<child-id>/<file>")
    name = parts[-1] if parts else ""
    if name not in config.ALLOWED_PRODUCTION_NAMES:
        failures.append(f"file not allowlisted: {name}")
    if PurePosixPath(name).suffix not in config.ALLOWED_PRODUCTION_SUFFIXES:
        failures.append("suffix not allowlisted")
    if size_bytes > config.MAX_PRODUCTION_FILE_BYTES:
        failures.append("production file too large")
    if size_bytes < 0:
        failures.append("negative size")
    return {"ok": not failures, "failures": failures}
