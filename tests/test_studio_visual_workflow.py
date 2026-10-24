"""Codex owns content; Actions only renders and independently reviews it."""
from pathlib import Path
import yaml
ROOT=Path(__file__).resolve().parents[1]
TEXT=(ROOT/'.github/workflows/studio-production.yml').read_text()
WF=yaml.safe_load(TEXT)
STEPS=WF['jobs']['build']['steps']
def step(fragment):
    return next(s for s in STEPS if fragment in s.get('name',''))

def test_no_muse_or_content_generation_in_actions():
    for p in [*(ROOT/'.github/workflows').glob('*.yml'),ROOT/'.github/actions/studio-auth/action.yml']:
        assert 'muse' not in p.read_text().lower(), p
    for token in ('studio_repair.py','studio_ensure_children.py','Run Codex planner','issues:\n    types:'):
        assert token not in TEXT
    assert 'studio_prepare_project.py' in TEXT
    assert 'persist-credentials: false' in TEXT
    assert "github.actor == 'YusukeIwaki'" in TEXT
    assert "github.ref == 'refs/heads/main'" in TEXT

def test_preflight_is_review_only_with_no_nested_worker():
    s=step('Staged visual preflight')
    assert s['uses']=='actions/github-script@v8'
    script=s['with']['script']
    assert "'--codex-led'" in script
    assert '${{' not in script
    assert s['timeout-minutes']==40
    assert s['env']['ENGINE_SHA']=='${{ github.sha }}'

def test_full_stages_preserve_visual_and_exact_head_gates():
    for name in ('Validate storyboard and render','Commit production bundle','Generate artifact receipt',
                 'Run Codex evidence review','Validate bundle allowlist','Upload production artifact','Publish review'):
        assert step(name)['if']=="steps.visual.outputs.proceed == 'true'"
    render=step('Validate storyboard and render')['run']
    assert '--visual-approval output/studio/work/visual-approval.json' in render
    assert '--engine-sha ${{ github.sha }}' in render
    assert 'steps.commit.outputs.head_sha' in str(step('Upload production artifact'))
    assert 'steps.commit.outputs.head_sha' in str(step('Publish review'))
    assert 'python3 -m studio check-bundle' in TEXT

def test_artifact_sdk_and_review_history_stay():
    assert '@actions/artifact@2.3.2' in step('Install ffmpeg')['run']
    assert '--validate-history' in step('Build aggregate visual history')['run']
    assert step('Upload aggregate visual history')['if']=="always() && steps.validate_visual_history.outcome == 'success'"
    assert step('Upload aggregate visual history')['with']['retention-days']==90
    assert "owner-ready: NO" in step('Summarize early visual block')['run']

def test_codex_auth_persist_and_cleanup_after_last_model_call():
    names=[s.get('name','') for s in STEPS]
    assert names.index(step('Persist refreshed Codex auth')['name'])>names.index(step('Run Codex evidence review')['name'])
    for name in ('Persist refreshed Codex auth','Cleanup credentials'):
        assert 'always()' in step(name)['if']
    assert WF['jobs']['build']['timeout-minutes']==120
