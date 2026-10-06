"""Execute the exact production Node selector with GitHub API-shaped fixtures."""
import json
import shutil
import subprocess
from copy import deepcopy
from pathlib import Path

import pytest

REPO = 'edwin-hou/gov-contract-ipo-monitor'
SHA = '8c590b24146384e6803dd70b311c64ad8462cfbd'
MODULE = Path(__file__).resolve().parents[1]/'scripts/select_checkpoint.cjs'
NODE = shutil.which('node') or str(Path.home()/'.cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node.exe')


def artifact(identifier, run, created):
    return {'id':identifier,'name':'ipo-monitor-state','size_in_bytes':15_000_000,'expired':False,
            'created_at':created,'workflow_run':{'id':run,'repository_id':1,'head_repository_id':1,'head_branch':'main','head_sha':SHA}}


def run(identifier, created, **changes):
    return {'id':identifier,'path':'.github/workflows/monitor.yml','head_branch':'main','head_sha':SHA,
            'status':'completed','event':'workflow_dispatch','conclusion':'success','created_at':created,
            'repository':{'id':1,'full_name':REPO},'head_repository':{'id':1,'full_name':REPO}} | changes


def choose(artifacts,runs,**options):
    driver = r'''
const {selectCheckpoint}=require(process.argv[1]);
let input=''; process.stdin.on('data',chunk=>input+=chunk);
process.stdin.on('end',async()=>{
 const fixture=JSON.parse(input); const requested=[];
 try {
  const selected=await selectCheckpoint(fixture.artifacts,async id=>{
   requested.push(id); if(fixture.errorRun===id)throw new Error('readback failed');return fixture.runs[id];
  },{repository:fixture.repository,currentRunId:99,now:Date.parse('2026-10-06T02:00:00Z'),...fixture.options});
  process.stdout.write(JSON.stringify({selected,requested}));
 }catch(error){process.stdout.write(JSON.stringify({error:error.message,requested}));}
});
'''
    fixture={'artifacts':artifacts,'runs':runs,'repository':REPO,'options':options}
    if 'errorRun' in options: fixture['errorRun']=options.pop('errorRun')
    result=subprocess.run([NODE,'-e',driver,str(MODULE)],input=json.dumps(fixture),capture_output=True,text=True,
                          shell=False,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0),timeout=15,check=True)
    return json.loads(result.stdout)


def fixtures():
    return [artifact(11383877120,37397889999,'2026-10-06T01:12:30Z'),artifact(11383691702,37398067866,'2026-10-06T01:14:30Z')], {
        37397889999:run(37397889999,'2026-10-06T01:10:00Z'),37398067866:run(37398067866,'2026-10-06T01:13:00Z')}


def test_reversed_real_artifact_ids_choose_later_upload_not_numeric_id():
    artifacts,runs=fixtures()
    result=choose(artifacts,runs)
    assert result['selected']['artifact']['id']==11383691702
    assert result['selected']['run']['id']==37398067866
    assert result['requested']==[37398067866]


@pytest.mark.parametrize('changes',[{'path':'.github/workflows/test.yml'},{'head_branch':'feature'}, {'status':'in_progress'},
    {'event':'pull_request'},{'head_sha':'a'*40},{'repository':{'id':1,'full_name':'other/repo'}},
    {'head_repository':{'id':2,'full_name':'fork/repo'}},{'id':99}])
def test_unrelated_unverified_or_running_run_cannot_supply_checkpoint(changes):
    artifacts,runs=fixtures();runs[37398067866].update(changes)
    result=choose(artifacts,runs)
    assert result['selected']['artifact']['id']==11383877120


def test_expired_current_and_other_named_artifacts_are_excluded():
    artifacts,runs=fixtures();artifacts[1]['expired']=True
    artifacts += [artifact(5,99,'2026-10-06T01:50:00Z'),dict(artifact(6,88,'2026-10-06T01:55:00Z'),name='ipo-monitor-report')]
    assert choose(artifacts,runs)['selected']['artifact']['id']==11383877120


@pytest.mark.parametrize('created',['2026-10-06','2026-02-30T01:00:00Z','2026-10-06T03:00:00Z',None])
def test_invalid_ambiguous_or_future_metadata_does_not_reset_or_guess(created):
    artifacts,runs=fixtures();artifacts[1]['created_at']=created
    assert 'error' in choose(artifacts,runs)


def test_timestamp_tie_uses_verified_run_creation_and_identical_times_fail_closed():
    artifacts,runs=fixtures();artifacts[0]['created_at']=artifacts[1]['created_at']
    assert choose(artifacts,runs)['selected']['artifact']['id']==11383691702
    runs[37397889999]['created_at']=runs[37398067866]['created_at']
    assert 'ambiguous' in choose(artifacts,runs)['error']


def test_run_upload_chronology_and_readback_errors_fail_closed():
    artifacts,runs=fixtures();runs[37398067866]['created_at']='2026-10-06T01:59:00Z'
    assert 'predates' in choose(artifacts,runs)['error']
    artifacts,runs=fixtures()
    assert 'readback failed' in choose(artifacts,runs,errorRun=37398067866)['error']


def test_completed_degraded_run_can_preserve_valid_collection_state():
    artifacts,runs=fixtures();runs[37398067866]['conclusion']='failure'
    assert choose(artifacts,runs)['selected']['artifact']['id']==11383691702
    assert choose([], {})['selected'] is None


def test_workflow_uses_exact_selector_and_preserves_restore_validation():
    root=MODULE.parent.parent
    workflow=(root/'.github/workflows/monitor.yml').read_text()
    assert "require(process.env.GITHUB_WORKSPACE + '/scripts/select_checkpoint.cjs')" in workflow
    assert 'b.id-a.id' not in workflow and 'python scripts/restore_checkpoint.py' in workflow
    assert "'scripts/select_checkpoint.cjs'" in workflow
