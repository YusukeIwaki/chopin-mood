#!/usr/bin/env python3
"""Classify a captured agent stderr file into a fixed, safe summary.

Reads only allowlisted patterns internally. Stdout is always one of the
fixed classification blocks below: it never echoes input lines, exception
contents, paths, credentials, prompts, or provider JSON. Any unreadable,
missing, or unrecognised input yields only the generic classification.
Best-effort by design: diagnostics must never fail the calling step.
"""
import re
import sys

_MAX_BYTES = 2_000_000

_EXPIRED = (
    "expired_authentication",
    "expired authentication (invalid_refresh_token/token_revoked)",
    "Re-login to the agent account, refresh the encrypted auth, then run Studio auth smoke.",
)
_UNSUPPORTED = (
    "unsupported_model",
    "unsupported model",
    "Check the logged CLI version and that the configured model is available "
    "to this account, then run Studio auth smoke.",
)
_USAGE = (
    "usage_limit",
    "usage limit/quota",
    "Wait for the account usage limit/quota to reset or be raised, then run Studio auth smoke.",
)
_SANDBOX = (
    "sandbox_setup_failure",
    "sandbox setup failure",
    "Check the runner sandbox setup and permissions, then run Studio auth smoke.",
)
_INPUT = (
    "input_limit",
    "request or image input size limit",
    "Check image count and encoded payload size; preserve native evidence and review bindings.",
)
_REQUEST = (
    "invalid_model_request",
    "invalid model request or output schema",
    "Validate the CLI flags and structured-output schema with the logged CLI version.",
)
_CONNECTION = (
    "provider_connection",
    "provider connection or upstream service failure",
    "Check provider availability; retry only a bounded number of times without bypassing review.",
)
_GENERIC = (
    "generic",
    "generic",
    "Run Studio auth smoke to isolate the failure; do not share raw provider output.",
)

_PATTERNS = (
    (_EXPIRED, (
        r"invalid_refresh_token",
        r"token_revoked",
        r"refresh[ _-]?token[^a-z0-9]{0,10}(invalid|expired|revoked)",
        r"(invalid|expired|revoked)[^a-z0-9]{0,10}refresh[ _-]?token",
    )),
    (_UNSUPPORTED, (
        r"unsupported model",
        r"model_not_supported",
        r"model\s+is\s+not\s+supported",
        r"model[^a-z0-9]{0,80}(not supported|unsupported|not available|does not exist)",
        r"(not supported|unsupported)[^a-z0-9]{0,40}model",
        r"invalid model",
    )),
    (_USAGE, (
        r"usage[ _-]?limit",
        r"insufficient_quota",
        r"quota",
        r"rate[ _-]?limit",
        r"too many requests",
        r"limit (reached|exceeded)",
        r"\b429\b",
    )),
    (_INPUT, (
        r"\b413\b", r"payload too large", r"request entity too large",
        r"request body.{0,60}(too large|exceed)",
        r"too many images", r"image.{0,60}(size limit|too large)",
        r"context_length_exceeded", r"maximum context length",
    )),
    (_REQUEST, (
        r"invalid_json_schema", r"invalid schema for response",
        r"unrecognized arguments?", r"unexpected argument",
    )),
    (_CONNECTION, (
        r"\b(502|503|504)\b", r"stream disconnected",
        r"connection (reset|refused|timed out)",
        r"error sending request", r"websocket.{0,60}(failed|error)",
    )),
    (_SANDBOX, (
        r"sandbox[ _-]?setup",
        r"sandbox setup failure",
        r"(failed|failure|error|unable|cannot|could not)[^a-z0-9]{0,60}sandbox",
        r"sandbox[^a-z0-9]{0,60}(failed|failure|error|denied|not permitted)",
        r"landlock[^a-z0-9]{0,40}(failed|failure|error|denied|not permitted)",
        r"no permissions to create a new namespace",
        r"bubblewrap.{0,60}(failed|failure|error|denied|not permitted)",
        r"(failed|failure|error|unable|cannot).{0,60}bubblewrap",
        r"\bbwrap\b.{0,60}(failed|failure|error|denied|not permitted)",
        r"(failed|failure|error|unable|cannot).{0,60}\bbwrap\b",
        r"namespace.{0,40}(failed|failure|error|denied|not permitted)",
        r"(failed|failure|error).{0,40}namespace",
        r"seccomp[^a-z0-9]{0,40}(failed|error|denied)",
    )),
)


def classify(text):
    lowered = text.lower()
    for block, patterns in _PATTERNS:
        for pattern in patterns:
            if re.search(pattern, lowered):
                return block
    return _GENERIC


def _read_input(argv):
    path = None
    args = argv[1:]
    i = 0
    while i < len(args):
        arg = args[i]
        if arg in ("--file", "--stderr", "--path", "--input"):
            if i + 1 < len(args):
                path = args[i + 1]
                i += 2
                continue
            return ""
        if arg.startswith("--file=") or arg.startswith("--path="):
            path = arg.split("=", 1)[1]
        elif arg == "-":
            path = "-"
        elif not arg.startswith("-"):
            path = arg
        i += 1
    if path is None or path == "-":
        try:
            return sys.stdin.buffer.read(_MAX_BYTES).decode("utf-8", "ignore")
        except Exception:
            return ""
    try:
        with open(path, "rb") as handle:
            return handle.read(_MAX_BYTES).decode("utf-8", "ignore")
    except Exception:
        return ""


def main(argv=None):
    try:
        block = classify(_read_input(list(sys.argv if argv is None else argv)))
    except Exception:
        block = _GENERIC
    slug, label, next_step = block
    sys.stdout.write(
        "classification: %s\nlabel: %s\nnext_step: %s\n" % (slug, label, next_step)
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
