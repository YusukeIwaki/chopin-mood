"""Exercise the new Actions adapter with exact bytes and offline GitHub state."""
import base64
import copy
import json
import subprocess
import sys
from pathlib import Path

import pytest

from studio import artifacts, candidate_pipeline as pipe, config
from studio.candidate_project import QA_CHECK_NAMES
from test_candidate_release import Transport, ENGINE, URL, add_candidate, add_artifact, encoded


class API(Transport):
    def _dispatch(self, method, seg, body, query):
        result = super()._dispatch(method, seg, body, query)
        # Real GitHub Contents and Pull Files API fields omitted by the older fake.
        if seg[:1] == ['contents'] and method == 'GET':
            result['size'] = len(base64.b64decode(result['content']))
        if len(seg) == 3 and seg[0] == 'pulls' and seg[2] == 'files':
            result = [{**x, 'status': 'added'} for x in result]
        return result


@pytest.fixture
def prepared(tmp_path):
    gh = API()
    gh.seed_commit({'README.md': b'engine'})
    issue = gh.seed_issue(body=f'### Recording attachment\n{URL}\n\n### Art / direction notes\nquiet recollection')
    pr, raw = add_candidate(gh, issue['number'], merged=False)
    gh.calls.clear()
    result = pipe.prepare_candidate(gh, pr_number=pr['number'], actor=config.OWNER, engine_sha=ENGINE)
    work = tmp_path / 'work'
    ctx = pipe.write_prepared(result, work)
    return gh, pr, raw, work, ctx


def test_prepare_keeps_exact_input_bytes_without_child_issue(prepared):
    gh, pr, raw, work, ctx = prepared
    assert ctx['head_sha'] == pr['head']['sha']
    for name, data in raw.items():
        assert (work / 'inputs' / name).read_bytes() == data
    assert not any('sub_issues' in path for _, path in gh.calls)
    assert not any(method != 'GET' for method, _ in gh.calls)
    pipe.verify_candidate_guard(gh, ctx)


@pytest.mark.parametrize('change', ['head', 'closed', 'author', 'creative', 'input_hash', 'engine'])
def test_live_guard_refuses_drift_before_side_effects(prepared, change):
    gh, pr, _, _, ctx = prepared
    if change == 'head': gh.branches[pr['head']['ref']] = 'f' * 40
    if change == 'closed': pr['state'] = 'closed'
    if change == 'author': gh.issues[ctx['source_issue']]['user']['login'] = 'outsider'
    if change == 'creative': gh.issues[ctx['source_issue']]['body'] += '\nnew direction'
    if change == 'input_hash': ctx['input_sha256']['editorial.json'] = 'f' * 64
    if change == 'engine': ctx['engine_sha'] = 'f' * 40
    gh.calls.clear()
    with pytest.raises(ValueError): pipe.verify_candidate_guard(gh, ctx)
    assert all(method == 'GET' for method, _ in gh.calls)


def test_owner_and_engine_must_match_before_prepare(prepared):
    gh, pr, *_ = prepared
    with pytest.raises(ValueError, match='owner'):
        pipe.prepare_candidate(gh, pr_number=pr['number'], actor='outsider', engine_sha=ENGINE)
    with pytest.raises(ValueError, match='engine'):
        pipe.prepare_candidate(gh, pr_number=pr['number'], actor=config.OWNER, engine_sha='f' * 40)


def stage_bundle(prepared):
    gh, pr, raw, work, ctx = prepared
    files = add_artifact(gh, pr, raw)
    gh.comments[pr['number']] = []
    bundle = work / 'bundle'
    bundle.mkdir()
    for name, data in files.items():
        if name not in {'project.json', 'editorial.json', 'receipt.json', 'review.json'}:
            (bundle / name).write_bytes(data)
    pipe.seal_candidate(work, run_id=11, run_attempt=1)
    (bundle / 'review.json').write_bytes(files['review.json'])
    pipe.refresh_manifest(bundle)
    return bundle


def test_seal_review_check_publish_full_adapter_path(prepared):
    bundle = stage_bundle(prepared)
    # This roundtrip catches manifest-self inclusion and original-byte loss.
    checked = pipe.validate_bundle(bundle)
    assert checked['receipt']['input_sha256'] == prepared[-1]['input_sha256']
    gh, pr, *_ = prepared
    result = pipe.publish_candidate(gh, work=prepared[3], artifact_id=42, artifact_url='https://github.com/example/artifact')
    assert result['ok'], result
    assert pr['assignees'] == [{'login': config.OWNER}]
    review_count = len(gh.reviews[pr['number']])
    comment_count = len(gh.comments[pr['number']])
    assert pipe.publish_candidate(gh, work=prepared[3], artifact_id=42, artifact_url='https://github.com/example/artifact')['ok']
    assert len(gh.reviews[pr['number']]) == review_count
    assert len(gh.comments[pr['number']]) == comment_count


def test_failed_review_posts_feedback_without_release_receipt_or_ping(prepared):
    bundle = stage_bundle(prepared)
    review = json.loads((bundle / 'review.json').read_text())
    review.update(verdict='fail', blockers=['The figure disappears.'], repair={'route':'engine','instructions':['Make figure visible.']})
    review['checks']['aesthetic_approval'] = False
    (bundle / 'review.json').write_bytes(encoded(review)); pipe.refresh_manifest(bundle)
    gh, pr, *_ = prepared
    result = pipe.publish_candidate(gh, work=prepared[3], artifact_id=42, artifact_url='https://github.com/example/artifact')
    assert not result['ok']
    assert not gh.reviews.get(pr['number'])
    assert all('studio:candidate-receipt' not in c['body'] for c in gh.comments[pr['number']])


def test_stale_head_cannot_publish(prepared):
    stage_bundle(prepared)
    gh, pr, *_ = prepared
    gh.branches[pr['head']['ref']] = 'f' * 40
    gh.calls.clear()
    with pytest.raises(ValueError, match='head changed'):
        pipe.publish_candidate(gh, work=prepared[3], artifact_id=42, artifact_url='https://github.com/example/artifact')
    assert all(method == 'GET' for method, _ in gh.calls)


@pytest.mark.parametrize('kind', ['media', 'extra', 'input', 'symlink'])
def test_modified_or_extra_artifact_rejected(prepared, kind):
    bundle = stage_bundle(prepared)
    if kind == 'media': (bundle / 'video.mp4').write_bytes(b'replaced')
    if kind == 'extra': (bundle / 'source.wav').write_bytes(b'full recording')
    if kind == 'input':
        (bundle / 'editorial.json').write_bytes(b'{}'); pipe.refresh_manifest(bundle)
    if kind == 'symlink':
        target = prepared[3] / 'elsewhere'; target.write_bytes((bundle / 'video.mp4').read_bytes())
        (bundle / 'video.mp4').unlink(); (bundle / 'video.mp4').symlink_to(target)
    with pytest.raises(ValueError): pipe.validate_bundle(bundle)


def test_candidate_intake_is_standard_library_only():
    p = subprocess.run([sys.executable, '-S', 'scripts/studio_candidate_actions.py', '--help'], capture_output=True, text=True)
    assert p.returncode == 0, p.stderr


def test_rehashed_incomplete_qa_cannot_be_notified(prepared):
    bundle = stage_bundle(prepared)
    (bundle / 'qa.json').write_bytes(encoded({'pass': True, 'checks': []}))
    pipe.refresh_manifest(bundle)
    gh = prepared[0]; gh.calls.clear()
    with pytest.raises(ValueError, match='mechanical QA'):
        pipe.publish_candidate(gh, work=prepared[3], artifact_id=42, artifact_url='https://github.com/example/artifact')
    assert not gh.calls
