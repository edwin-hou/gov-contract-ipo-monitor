'use strict';

// Artifact IDs identify objects; they do not establish upload chronology.
// Keep the selector independently testable with actual API-shaped metadata.
function timestamp(value, now) {
  if (typeof value !== 'string' || !/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,3})?Z$/.test(value)) {
    throw new Error('Checkpoint metadata requires an explicit UTC creation timestamp');
  }
  const parsed = Date.parse(value);
  if (!Number.isFinite(parsed) || new Date(parsed).toISOString().slice(0, 19) !== value.slice(0, 19) || parsed > now + 300000) {
    throw new Error('Checkpoint creation timestamp is invalid or in the future');
  }
  return parsed;
}

function identifier(value) {
  return Number.isSafeInteger(value) && value > 0;
}

async function selectCheckpoint(artifacts, getRun, options) {
  const {repository, currentRunId, branch = 'main', workflowPath = '.github/workflows/monitor.yml', now = Date.now()} = options;
  if (!Array.isArray(artifacts) || typeof repository !== 'string' || !repository.includes('/') || !Number.isFinite(now)) {
    throw new Error('Checkpoint selector requires repository metadata and an artifact list');
  }
  const eligible = artifacts.filter(a => a?.name === 'ipo-monitor-state' && a.expired === false
    && a.workflow_run?.head_branch === branch && String(a.workflow_run.id) !== String(currentRunId));
  const ordered = eligible.map(artifact => {
    if (!identifier(artifact.id) || !identifier(artifact.workflow_run.id)
      || !Number.isSafeInteger(artifact.size_in_bytes) || artifact.size_in_bytes <= 0 || artifact.size_in_bytes > 250000000) {
      throw new Error('Checkpoint artifact identity or size is invalid');
    }
    return {artifact, created: timestamp(artifact.created_at, now)};
  }).sort((a, b) => b.created - a.created);
  let chosen = null;
  for (const item of ordered) {
    if (chosen && item.created < chosen.artifactCreated) break;
    // A read failure is not evidence that an older checkpoint is current.
    const run = await getRun(item.artifact.workflow_run.id);
    const reported = item.artifact.workflow_run;
    if (!run || run.id !== reported.id || run.path !== workflowPath || run.head_branch !== branch
      || run.status !== 'completed' || !['push', 'schedule', 'workflow_dispatch'].includes(run.event)
      || run.repository?.full_name?.toLowerCase() !== repository.toLowerCase()
      || run.head_repository?.full_name?.toLowerCase() !== repository.toLowerCase()
      || !identifier(run.repository?.id) || run.repository.id !== run.head_repository?.id
      || reported.repository_id !== run.repository.id || reported.head_repository_id !== run.head_repository.id
      || !/^[a-f0-9]{40}$/i.test(run.head_sha || '') || run.head_sha !== reported.head_sha) continue;
    const runCreated = timestamp(run.created_at, now);
    if (runCreated > item.created) throw new Error('Checkpoint upload predates its verified workflow run');
    const candidate = {artifact: item.artifact, run, artifactCreated: item.created, runCreated};
    if (!chosen || runCreated > chosen.runCreated) chosen = candidate;
    else if (runCreated === chosen.runCreated) {
      throw new Error('Checkpoint creation chronology is ambiguous; refusing an artifact-ID tiebreak');
    }
  }
  return chosen ? {artifact: chosen.artifact, run: chosen.run} : null;
}

module.exports = {selectCheckpoint};
