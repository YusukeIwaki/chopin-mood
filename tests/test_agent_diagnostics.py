"""Offline tests for the allowlisted agent-error diagnostic.

Synthetic logs embed dummy secrets/paths/prompts; the fixed output must
never echo them. No real models, no network, no real credentials.
"""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / ".github/studio/scripts/report-agent-error.py"

DUMMY_SECRET = "DUMMY-SECRET-9f8e7d6c5b"
DUMMY_TOKEN = "dummy-refresh-token-VALUE-12345"
DUMMY_PATH = "/home/runner/DUMMY_PRIVATE_DIR/auth.json"
DUMMY_PROMPT = "DUMMY PROMPT TEXT do not echo this sentence"


def run_diag(tmp_path, log_text, name="stderr.log"):
    log = tmp_path / name
    log.write_text(log_text, encoding="utf-8")
    result = subprocess.run(
        [sys.executable, str(SCRIPT), str(log)],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout


def assert_clean(output, *forbidden):
    for value in forbidden:
        assert value not in output
    assert DUMMY_SECRET not in output
    assert DUMMY_TOKEN not in output
    assert DUMMY_PATH not in output
    assert DUMMY_PROMPT not in output


def test_expired_authentication_invalid_refresh_token(tmp_path):
    out = run_diag(tmp_path, (
        f"Error: invalid_refresh_token token={DUMMY_TOKEN} secret={DUMMY_SECRET}\n"
        f"at {DUMMY_PATH}\n{DUMMY_PROMPT}\n"
        '{"error": {"code": "invalid_refresh_token", "detail": "DUMMY-SECRET-9f8e7d6c5b"}}\n'
    ))
    assert "classification: expired_authentication" in out
    assert "expired authentication" in out
    assert "Studio auth smoke" in out
    assert_clean(out)


def test_expired_authentication_token_revoked(tmp_path):
    out = run_diag(tmp_path, f"auth failed: token_revoked value={DUMMY_TOKEN} {DUMMY_SECRET}")
    assert "classification: expired_authentication" in out
    assert_clean(out)


def test_unsupported_model(tmp_path):
    out = run_diag(tmp_path, (
        f"HTTP 400 model not supported for this account secret={DUMMY_SECRET}\n"
        f'{{"error": {{"message": "model gpt-DUMMY is not supported", "leak": "{DUMMY_TOKEN}"}}}}\n'
        f"{DUMMY_PROMPT} at {DUMMY_PATH}\n"
    ))
    assert "classification: unsupported_model" in out
    assert "unsupported model" in out
    assert_clean(out, "gpt-DUMMY")


def test_usage_limit_quota(tmp_path):
    out = run_diag(tmp_path, (
        f"You have hit your usage limit quota exceeded secret={DUMMY_SECRET} "
        f"token={DUMMY_TOKEN} path={DUMMY_PATH} prompt={DUMMY_PROMPT}"
    ))
    assert "classification: usage_limit" in out
    assert "usage limit/quota" in out
    assert_clean(out)


def test_usage_limit_insufficient_quota_phrase(tmp_path):
    out = run_diag(tmp_path, f"insufficient_quota {DUMMY_SECRET}")
    assert "classification: usage_limit" in out
    assert_clean(out)


def test_sandbox_setup_failure(tmp_path):
    out = run_diag(tmp_path, (
        f"sandbox setup failure: failed to create sandbox secret={DUMMY_SECRET} "
        f"at {DUMMY_PATH} {DUMMY_PROMPT} token={DUMMY_TOKEN}"
    ))
    assert "classification: sandbox_setup_failure" in out
    assert "sandbox setup failure" in out
    assert_clean(out)


def test_real_bwrap_namespace_failure(tmp_path):
    out = run_diag(tmp_path, (
        "fs sandbox helper failed with status exit status: 1: bwrap: "
        "No permissions to create a new namespace, likely because the kernel "
        f"does not allow non-privileged user namespaces. {DUMMY_SECRET}"
    ))
    assert "classification: sandbox_setup_failure" in out
    assert_clean(out)


def test_generic_unknown_never_echoes_and_points_to_auth_smoke(tmp_path):
    out = run_diag(tmp_path, (
        f"Something entirely unexpected exploded secret={DUMMY_SECRET}\n"
        f"Traceback: ValueError({DUMMY_TOKEN}) at {DUMMY_PATH}\n{DUMMY_PROMPT}\n"
        '{"provider": "DUMMY", "raw": "DUMMY-SECRET-9f8e7d6c5b"}\n'
    ))
    assert "classification: generic" in out
    assert "Studio auth smoke" in out
    # Only the fixed generic block: no other classification may appear.
    for other in ("expired_authentication", "unsupported_model",
                  "usage_limit", "sandbox_setup_failure"):
        assert other not in out
    assert_clean(out, "Something entirely unexpected", "ValueError", '"provider":')


def test_missing_file_is_generic_and_does_not_echo_path(tmp_path):
    missing = tmp_path / "DUMMY_PRIVATE_DIR" / "nope.log"
    result = subprocess.run(
        [sys.executable, str(SCRIPT), str(missing)],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0
    assert "classification: generic" in result.stdout
    assert "Studio auth smoke" in result.stdout
    assert str(missing) not in result.stdout
    assert str(missing) not in result.stderr
    assert DUMMY_SECRET not in result.stdout


def test_empty_and_binary_input_are_generic(tmp_path):
    log = tmp_path / "empty.log"
    log.write_bytes(b"\x00\xff\xfe binary \x00 DUMMY-SECRET-9f8e7d6c5b")
    result = subprocess.run(
        [sys.executable, str(SCRIPT), str(log)],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0
    assert "classification: generic" in result.stdout
    assert DUMMY_SECRET not in result.stdout


BENIGN_BUBBLEWRAP = (
    "Codex could not find bubblewrap on PATH. "
    "Codex will use the bundled bubblewrap in the meantime\n"
)
ACTUAL_UNSUPPORTED = (
    "The 'gpt-6.1-sol' model is not supported when using Codex "
    "with a ChatGPT account."
)


def test_unsupported_model_actual_provider_phrase(tmp_path):
    out = run_diag(tmp_path, f"{ACTUAL_UNSUPPORTED} secret={DUMMY_SECRET}\n")
    assert "classification: unsupported_model" in out
    assert_clean(out, "gpt-6.1-sol")


def test_benign_bubblewrap_then_unsupported_model(tmp_path):
    out = run_diag(tmp_path, BENIGN_BUBBLEWRAP + ACTUAL_UNSUPPORTED + "\n")
    assert "classification: unsupported_model" in out
    assert "classification: sandbox_setup_failure" not in out
    assert_clean(out, "gpt-6.1-sol", "bubblewrap")


def test_benign_bubblewrap_then_usage_limit(tmp_path):
    out = run_diag(tmp_path, (
        BENIGN_BUBBLEWRAP + f"You have hit your usage limit secret={DUMMY_SECRET}\n"
    ))
    assert "classification: usage_limit" in out
    assert "classification: sandbox_setup_failure" not in out
    assert_clean(out, "bubblewrap")


def test_benign_bubblewrap_then_unknown_is_generic(tmp_path):
    out = run_diag(tmp_path, (
        BENIGN_BUBBLEWRAP
        + f"Something entirely unexpected exploded secret={DUMMY_SECRET}\n"
    ))
    assert "classification: generic" in out
    for other in ("expired_authentication", "unsupported_model",
                  "usage_limit", "sandbox_setup_failure"):
        assert other not in out
    assert_clean(out, "bubblewrap", "Something entirely unexpected")


def test_output_is_only_fixed_lines(tmp_path):
    out = run_diag(tmp_path, f"quota secret={DUMMY_SECRET}")
    lines = [line for line in out.splitlines() if line]
    assert len(lines) == 3
    assert lines[0].startswith("classification: ")
    assert lines[1].startswith("label: ")
    assert lines[2].startswith("next_step: ")
