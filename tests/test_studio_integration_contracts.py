"""Boundary regressions: real form labels, codecs, auth lifecycle, schemas."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tomllib

import pytest
from studio import candidates, creative, fingerprint

ROOT = Path(__file__).resolve().parents[1]


def test_real_issue_form_labels_preserve_owner_direction():
    form = (ROOT / ".github/ISSUE_TEMPLATE/studio-source.yml").read_text()
    labels = [s.split("label:", 1)[1].strip() for s in form.splitlines() if "label:" in s]
    values = ["https://github.com/user-attachments/files/1/piano.wav",
              "Owner supplied work title", "late (healing)", "一行目\n二行目"]
    body = "\n\n".join(f"### {label}\n\n{value}" for label, value in zip(labels, values))
    assert creative.parse_issue_body(body) == {
        "piece": values[1], "period": "late", "art_notes": values[3]}


def test_plain_source_issue_preserves_direction_without_inventing_piece():
    direction = 'Expresses "Tired... tired..." with abstract image, with dimly reminiscing past.'
    attachment = "https://github.com/user-attachments/files/33725169/chopin_waltz9.mp3"
    body = f"{direction}\n\n[chopin_waltz9.mp3]({attachment})"
    assert creative.parse_issue_body(body) == {
        "piece": None, "period": None, "art_notes": direction}
    assert creative.parse_issue_body(attachment)["art_notes"] == ""
    assert creative.parse_issue_body("<" + attachment + ">")["art_notes"] == ""
    reference = "[palette](https://example.com/palette)"
    assert creative.parse_issue_body(reference)["art_notes"] == reference


def test_m4a_decode_when_libsndfile_cannot_open(tmp_path):
    from studio.decode import decode_lossless
    import soundfile as sf
    src = tmp_path / "piano.m4a"
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i",
                    "sine=frequency=440:duration=1", "-c:a", "aac", str(src)], check=True)
    result = decode_lossless(src, tmp_path / "decoded.wav")
    assert result["subtype"] == "FLOAT"
    assert result["sample_rate"] == 44100
    assert sf.info(tmp_path / "decoded.wav").duration >= 1.0


def test_nested_schemas_are_closed_with_explicit_saved_data_options():
    def check(node, optional=frozenset()):
        if isinstance(node, dict):
            if node.get("type") == "object":
                assert node.get("additionalProperties") is False
                assert set(node.get("properties", {})) == set(node.get("required", [])) | optional
            for value in node.values():
                check(value)
        elif isinstance(node, list):
            for value in node:
                check(value)
    for name in ("planner-output", "review", "visual-review", "storyboard"):
        # Storyboards are saved production inputs, not model output. Only
        # their root asset selector is optional; nested contracts stay closed.
        optional = {"asset_bundle"} if name == "storyboard" else frozenset()
        check(json.loads((ROOT / f".github/studio/schemas/{name}.schema.json").read_text()), optional)


def test_planning_record_round_trip_with_code_fence_in_direction():
    record = {"fingerprint": "a" * 64, "briefs": {"cand-01": {"world": "```night```"}}}
    assert fingerprint.parse_planning_comment(fingerprint.build_planning_comment(record)) == record


def test_auth_restore_and_unchanged_persist_execute_real_scripts(tmp_path):
    for binary in ("openssl", "jq", "git", "sha256sum"):
        if not shutil.which(binary):
            pytest.skip(f"{binary} needed for the actual Linux auth lifecycle")
    repo, runtime = tmp_path / "enc", tmp_path / "runtime"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=Test", "-c",
                    "user.email=test@example.invalid", "commit", "--allow-empty", "-qm", "init"], check=True)
    env = dict(os.environ, CODEX_RUNTIME_HOME=str(runtime), CODEX_ENC_REPO_DIR=str(repo),
               CODEX_AUTH_BEFORE_HASH=str(tmp_path / "before"), STUDIO_SOURCE_DIR=str(tmp_path / "source"),
               CODEX_ENC_PASSPHRASE="synthetic-test-only", GITHUB_OUTPUT=str(tmp_path / "gh-output"))
    plain = json.dumps({"auth_mode": "chatgpt", "tokens": {"refresh_token": "synthetic"}}).encode()
    subprocess.run(["openssl", "enc", "-aes-256-cbc", "-salt", "-pbkdf2", "-base64",
                    "-pass", "env:CODEX_ENC_PASSPHRASE", "-out", str(repo / "codex.enc")],
                   input=plain, env=env, check=True)
    scripts = ROOT / ".github/studio/scripts"
    # Restoring twice also tests that a pre-existing staged config is replaced
    # rather than appending a duplicate TOML table.
    for _ in range(2):
        subprocess.run(["bash", str(scripts / "auth-restore.sh"), "codex"],
                       cwd=ROOT, env=env, check=True, capture_output=True)
    cfg = tomllib.loads((runtime / "config.toml").read_text())
    assert cfg["default_permissions"] == "studio"
    assert cfg["permissions"]["studio"]["extends"] == ":read-only"
    assert cfg["permissions"]["studio"]["network"]["enabled"] is False
    assert cfg["permissions"]["studio"]["filesystem"][str(runtime / "auth.json")] == "deny"
    subprocess.run(["bash", str(scripts / "auth-persist.sh"), "codex"],
                   cwd=ROOT, env=env, check=True, capture_output=True)
    assert (tmp_path / "gh-output").read_text().strip() == "changed=false"
