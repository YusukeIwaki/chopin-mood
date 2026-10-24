"""Production inputs are trusted, bound, and never produced by a CI worker."""
import json
import shutil
from pathlib import Path
import pytest
from studio.project import load_project
from studio.fingerprint import child_marker
from studio.fingerprint import canonical_fingerprint
ROOT=Path(__file__).resolve().parents[1]
@pytest.fixture
def project(tmp_path):
    shutil.copytree(ROOT/'studio/projects/issue-1',tmp_path/'issue-1')
    meta=json.loads((tmp_path/'issue-1/project.json').read_text())
    return tmp_path,meta['source_url']
class GitHub:
    def __init__(self, root, url):
        b=json.loads((root/'issue-1/brief.json').read_text())
        self.issues={1:{'state':'open','user':{'login':'YusukeIwaki'},'body':b['art_notes']+'\n\n'+url},
                     5:{'state':'open','user':{'login':'github-actions[bot]'},'body':child_marker(1,b['candidate_id'],b['fingerprint'])}}
        self.linked=[5]
    def get_issue(self,n):return self.issues[n]
    def get_sub_issue_numbers(self,n):return self.linked

def test_project_load_preserves_exact_timings(project):
    root,url=project;r=load_project(root,1,url,GitHub(root,url))
    assert r['candidate']==r['brief']['candidate']
    assert r['candidate']['n_frames']==1932
    assert r['matrix']=={'include':[{'child_issue':5,'candidate_id':'cand-01'}]}

@pytest.mark.parametrize('change',['author','closed','attachment','creative','marker','unlinked'])
def test_live_binding_failures(project,change):
    root,url=project;gh=GitHub(root,url)
    if change=='author':gh.issues[1]['user']['login']='stranger'
    if change=='closed':gh.issues[1]['state']='closed'
    if change=='attachment':gh.issues[1]['body']+='\nhttps://github.com/user-attachments/files/2/new.mp3'
    if change=='creative':gh.issues[1]['body']+='\nNew direction'
    if change=='marker':gh.issues[5]['body']=''
    if change=='unlinked':gh.linked=[]
    with pytest.raises(ValueError):load_project(root,1,url,gh)

def test_missing_project_cannot_spawn_planner(project):
    root,url=project
    with pytest.raises(ValueError,match='missing'):load_project(root,2,url)

def test_reference_link_is_not_a_second_recording(project):
    root,url=project;p=root/'issue-1/brief.json';brief=json.loads(p.read_text())
    brief['art_notes']+='\n\nReference: https://example.com/illustration'
    brief['fingerprint']=canonical_fingerprint(
        source_url=url,source_sha256=brief['source_hashes']['decoded_wav_sha256'],
        piece=brief['piece'],period=brief['period'],art_notes=brief['art_notes'])
    p.write_text(json.dumps(brief))
    assert load_project(root,1,url,GitHub(root,url))['brief']==brief

def test_symlink_and_storyboard_rejected(project,tmp_path):
    root,url=project;p=root/'issue-1/storyboard.json'
    sb=json.loads(p.read_text());sb['scenes'][0]['params']['figure_scale']=8
    p.write_text(json.dumps(sb))
    with pytest.raises(ValueError,match='storyboard'):load_project(root,1,url)
    p.unlink();p.symlink_to(ROOT/'studio/projects/issue-1/storyboard.json')
    with pytest.raises(ValueError,match='symlink'):load_project(root,1,url)
