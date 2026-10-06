"""Execute the production publication script with API/registry-shaped receipts."""
import json
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

WORKFLOW = Path(__file__).resolve().parents[1] / ".github/workflows/publish-container.yml"
NODE = shutil.which("node") or str(Path.home() / ".cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node.exe")
SOURCE = "a" * 40
ADVANCED = "b" * 40
DIGEST = "sha256:" + "c" * 64
IMAGE = "ghcr.io/edwin-hou/gov-contract-ipo-monitor"


def publication_script():
    body = WORKFLOW.read_text().split("          script: |\n", 1)[1]
    lines = []
    for line in body.splitlines():
        if line.strip() and not line.startswith("            "):
            break
        lines.append(line)
    return textwrap.dedent("\n".join(lines))


def execute(**changes):
    fixture = {
        "script": publication_script(),
        "env": {"SOURCE_SHA": SOURCE, "BUILD_DIGEST": DIGEST, "IMAGE": IMAGE, "GITHUB_SHA": ADVANCED},
        "main": [SOURCE, SOURCE],
    }
    fixture.update(changes)
    driver = r"""
let input=''; process.stdin.on('data', chunk=>input+=chunk);
process.stdin.on('end', async()=>{
 const fixture=JSON.parse(input), commands=[], refs=[], infos=[], warnings=[], files={};
 const github={rest:{git:{getRef:async args=>{
  refs.push(args);
  if(fixture.failMainRead===refs.length) throw new Error('live main read failed');
  const sha=fixture.main[Math.min(refs.length-1,fixture.main.length-1)];
  return {data:{ref:fixture.refName||'refs/heads/main',object:{type:fixture.objectType||'commit',sha}}};
 }}}};
 const exec={
  getExecOutput:async(tool,args,options)=>{
   commands.push({tool,args,options});
   if(tool!=='docker'||args[0]!=='buildx'||args[1]!=='imagetools'||args[2]!=='inspect') throw new Error('unexpected inspection');
   if(fixture.invalidManifest) return {stdout:'invalid JSON'};
   const key=args[3].endsWith(':latest')?'latestDigest':'immutableDigest';
   return {stdout:JSON.stringify({schemaVersion:2,mediaType:'application/vnd.oci.image.index.v1+json',
     digest:fixture[key]===undefined?fixture.env.BUILD_DIGEST:fixture[key]})};
  },
  exec:async(tool,args)=>{
   commands.push({tool,args});
   if(fixture.failPromotion) throw new Error('registry write failed');
   return 0;
  }
 };
 const core={info:text=>infos.push(text),warning:text=>warnings.push(text)};
 const requireStub=name=>{
  if(name!=='fs') throw new Error('unexpected module');
  return {mkdirSync:()=>{},writeFileSync:(path,body)=>{files[path]=body;}};
 };
 const AsyncFunction=Object.getPrototypeOf(async function(){}).constructor;
 let error=null;
 try {await new AsyncFunction('github','context','core','exec','process','require',fixture.script)(
  github,{repo:{owner:'edwin-hou',repo:'gov-contract-ipo-monitor'}},core,exec,{env:fixture.env},requireStub);
 }catch(exc){error=exc.message;}
 process.stdout.write(JSON.stringify({error,commands,refs,infos,warnings,files}));
});
"""
    result = subprocess.run([NODE, "-e", driver], input=json.dumps(fixture), capture_output=True, text=True,
                            shell=False, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), timeout=15, check=True)
    return json.loads(result.stdout)


def receipt(result):
    return json.loads(result["files"]["deployments/container.json"])


def test_delayed_old_ci_uses_source_identity_and_does_not_promote_latest():
    result = execute(main=[ADVANCED, ADVANCED])
    assert result["error"] is None
    assert len(result["commands"]) == 1
    assert result["commands"][0]["args"][3] == f"{IMAGE}:sha-{SOURCE}"
    record = receipt(result)
    assert record["source_sha"] == SOURCE
    assert record["image"] == f"{IMAGE}:sha-{SOURCE}"
    assert record["digest"] == DIGEST
    assert record["digest_reference"] == f"{IMAGE}@{DIGEST}"
    assert record["latest_promoted"] is False and record["latest_digest"] is None
    assert record["main_sha_before_promotion"] == ADVANCED


def test_current_verified_source_promotes_exact_digest_and_reads_back_latest():
    result = execute()
    assert result["error"] is None
    assert result["commands"][1] == {"tool": "docker", "args": [
        "buildx", "imagetools", "create", "--prefer-index=false", "--tag", f"{IMAGE}:latest", f"{IMAGE}@{DIGEST}"]}
    assert result["commands"][2]["args"][3] == f"{IMAGE}:latest"
    assert all(args == {"owner": "edwin-hou", "repo": "gov-contract-ipo-monitor", "ref": "heads/main"} for args in result["refs"])
    record = receipt(result)
    assert record["latest_promoted"] is True and record["latest_digest"] == DIGEST
    assert record["main_advanced_after_check"] is False


def test_new_push_during_promotion_is_recorded_without_claiming_atomicity_or_rollback():
    result = execute(main=[SOURCE, ADVANCED])
    assert result["error"] is None
    record = receipt(result)
    assert record["main_advanced_after_check"] is True
    assert record["main_sha_after_publication"] == ADVANCED
    assert record["promotion_strategy"] == "live-main-check-before-registry-write"
    assert len(result["commands"]) == 3 and len(result["warnings"]) == 1


@pytest.mark.parametrize("field,value", [("SOURCE_SHA", ""), ("SOURCE_SHA", ADVANCED + "\n"),
    ("BUILD_DIGEST", ""), ("BUILD_DIGEST", "sha256:not-a-digest"), ("IMAGE", "other.invalid/repo")])
def test_invalid_publication_identity_never_reaches_registry_or_live_main(field, value):
    env = {"SOURCE_SHA": SOURCE, "BUILD_DIGEST": DIGEST, "IMAGE": IMAGE}
    env[field] = value
    result = execute(env=env)
    assert "identity" in result["error"]
    assert result["commands"] == result["refs"] == [] and result["files"] == {}


@pytest.mark.parametrize("change", [{"immutableDigest": "sha256:" + "d" * 64}, {"invalidManifest": True},
    {"failMainRead": 1}, {"objectType": "tag"}, {"refName": "refs/heads/feature"}, {"main": ["", ""]}])
def test_unverified_registry_or_main_does_not_promote_or_write_success_receipt(change):
    result = execute(**change)
    assert result["error"]
    assert len(result["commands"]) == 1 and result["files"] == {}


@pytest.mark.parametrize("change", [{"latestDigest": "sha256:" + "d" * 64}, {"failPromotion": True}, {"failMainRead": 2}])
def test_failed_or_unverifiable_promotion_never_records_success(change):
    result = execute(**change)
    assert result["error"] and result["files"] == {}
    assert sum(command["args"][2] == "create" for command in result["commands"]) == 1


def test_workflow_wires_build_tags_labels_and_digest_to_exact_source():
    workflow = WORKFLOW.read_text()
    assert "ref: ${{ env.SOURCE_SHA }}" in workflow
    assert 'test "$(git rev-parse HEAD)" = "$SOURCE_SHA"' in workflow
    assert "tags: type=raw,value=sha-${{ env.SOURCE_SHA }}" in workflow
    assert "labels: org.opencontainers.image.revision=${{ env.SOURCE_SHA }}" in workflow
    assert "type=sha" not in workflow and "type=raw,value=latest" not in workflow
    assert "BUILD_DIGEST: ${{ steps.build.outputs.digest }}" in workflow
    assert "github.event.workflow_run.event == 'push'" in workflow
    assert "github.event.workflow_run.head_repository.full_name == github.repository" in workflow
    assert "group: container-publication" in workflow and "cancel-in-progress: false" in workflow
