// Execute the workflow guards against GitHub API doubles, without credentials.
const assert = require('node:assert/strict');
const { readFileSync } = require('node:fs');
const { resolve } = require('node:path');
const { test } = require('node:test');

const workflow = readFileSync(resolve(__dirname, '../../.github/workflows/coverage-reporting.yml'), 'utf8');
const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor;
function step(name) {
  const body = workflow.split(`      - name: ${name}\n`)[1].split('      - name:')[0];
  return new AsyncFunction('context', 'github', 'core', 'process',
    body.split('          script: |\n')[1].split('\n').map(line => line.slice(12)).join('\n'));
}
const select = step('Resolve report artifact and source attribution');
const recheck = step('Recheck producer and PR before uploading');
const sha = 'a'.repeat(40);
const trustedSha = 'b'.repeat(40);

function fixture(manual = false) {
  const repository = { id: 1, default_branch: 'main' };
  const run = {
    id: 123, run_attempt: 2, status: 'completed', conclusion: 'success',
    event: manual ? 'workflow_dispatch' : 'pull_request', workflow_id: 321,
    repository, head_repository: { id: manual ? 1 : 2, owner: { login: 'contributor' } },
    path: '.github/workflows/tests.yml', head_sha: sha, head_branch: 'feature/speech',
    run_started_at: '2026-11-09T09:00:00Z', updated_at: '2026-11-09T09:10:00Z',
    actor: { login: 'maintainer' }, triggering_actor: { login: 'maintainer' },
    pull_requests: [],
  };
  const pr = {
    number: 78, state: 'open', draft: false, base: { repo: repository, ref: 'main' },
    head: { sha, ref: run.head_branch, repo: { id: run.head_repository.id } },
  };
  const context = {
    eventName: manual ? 'workflow_dispatch' : 'workflow_run', actor: 'maintainer',
    sha: manual ? sha : trustedSha, ref: manual ? 'refs/heads/feature/speech' : 'refs/heads/main',
    repo: { owner: 'maintainer', repo: 'integration' },
    payload: { repository, ...(manual ? { inputs: {
      producer_run_id: '123', producer_run_attempt: '2', pull_request_number: '78', expected_head_sha: sha,
    } } : { workflow_run: run }) },
  };
  return {
    run, pr, context, workflow: { id: 321, path: '.github/workflows/tests.yml' },
    env: { REPORTER_WORKFLOW_SHA: manual ? sha : trustedSha, REPORTER_TRIGGERING_ACTOR: 'maintainer' },
    artifacts: [{ name: 'coverage-evidence-2', id: 456, size_in_bytes: 25712, expired: false,
      created_at: '2026-11-09T09:09:00Z',
      workflow_run: { id: 123, repository_id: 1, head_repository_id: run.head_repository.id,
        head_sha: sha, head_branch: run.head_branch } }],
    calls: [],
  };
}

function github(f) {
  const record = (endpoint, params, data) => {
    f.calls.push({ endpoint, params });
    assert.equal(params.owner, 'maintainer');
    assert.equal(params.repo, 'integration');
    return { data };
  };
  return {
    rest: {
      pulls: { list: 'pulls', get: async params => record('pr', params, f.pr) },
      actions: {
        listWorkflowRunArtifacts: 'artifacts',
        getWorkflowRun: async params => record('run', params, f.run),
        getWorkflowRunAttempt: async params => record('attempt', params, f.attempt ?? f.run),
        getWorkflow: async params => record('workflow', params, f.workflow),
      },
    },
    paginate: async (endpoint, params) => {
      record(endpoint, params);
      if (endpoint === 'pulls') return f.prs ?? [f.pr];
      assert.equal(endpoint, 'artifacts');
      assert.equal(params.run_id, 123);
      assert.equal(params.per_page, 100);
      return f.artifacts;
    },
  };
}

async function resolveSource(f) {
  const outputs = {}, notices = [];
  await select(f.context, github(f), {
    setOutput: (key, value) => { outputs[key] = value; }, notice: text => notices.push(text),
  }, { env: f.env });
  return { outputs, notices };
}
function expected(pr = '78', headRepo = '2') {
  return { artifact_id: '456', sha, branch: 'feature/speech', pr,
    run_id: '123', run_attempt: '2', run_url: 'https://github.com/maintainer/integration/actions/runs/123',
    head_repository_id: headRepo };
}
async function confirm(f, outputs) {
  await recheck(f.context, github(f), {}, { env: {
    SOURCE_RUN_ID: outputs.run_id, SOURCE_RUN_ATTEMPT: outputs.run_attempt,
    SOURCE_SHA: outputs.sha, SOURCE_BRANCH: outputs.branch, SOURCE_PR: outputs.pr,
    SOURCE_HEAD_REPOSITORY_ID: outputs.head_repository_id,
  } });
}

test('fork PR resolves with empty workflow and base-commit associations', async () => {
  const f = fixture();
  assert.deepEqual(await resolveSource(f), { outputs: expected(), notices: [] });
  assert.deepEqual(f.calls[0], { endpoint: 'pulls', params: {
    owner: 'maintainer', repo: 'integration', state: 'open', head: 'contributor:feature/speech', per_page: 100,
  } });
});
for (const mismatch of ['sha', 'base', 'repository', 'branch', 'closed', 'deleted', 'draft']) {
  test(`automatic path skips ${mismatch} PR before downloading reports`, async () => {
    const f = fixture();
    if (mismatch === 'sha') f.pr.head.sha = trustedSha;
    if (mismatch === 'base') f.pr.base.repo = { id: 3 };
    if (mismatch === 'repository') f.pr.head.repo.id = 3;
    if (mismatch === 'branch') f.pr.head.ref = 'another-branch';
    if (mismatch === 'closed') f.pr.state = 'closed';
    if (mismatch === 'deleted') f.pr.head.repo = null;
    if (mismatch === 'draft') f.pr.draft = true;
    const result = await resolveSource(f);
    assert.deepEqual(result.outputs, {});
    assert.equal(result.notices.length, 1);
    assert.ok(f.calls.every(call => call.endpoint !== 'artifacts'));
  });
}
for (const count of [0, 2]) {
  test(`skips ${count} matching PRs`, async () => {
    const f = fixture(); f.prs = Array(count).fill(f.pr);
    assert.deepEqual((await resolveSource(f)).outputs, {});
  });
}
test('same-repository automatic PR is also attributed exactly', async () => {
  const f = fixture();
  f.run.head_repository.id = 1; f.pr.head.repo.id = 1;
  f.artifacts[0].workflow_run.head_repository_id = 1;
  assert.deepEqual((await resolveSource(f)).outputs, expected('78', '1'));
});
for (const branch of ['main', 'release/1.0']) {
  test(`trusted ${branch} push keeps exact attribution`, async () => {
    const f = fixture();
    Object.assign(f.run, { event: 'push', head_branch: branch, head_repository: { id: 1 } });
    Object.assign(f.artifacts[0].workflow_run, { head_repository_id: 1, head_branch: branch });
    const { outputs } = await resolveSource(f);
    assert.deepEqual(outputs, { ...expected('', '1'), branch });
    await confirm(f, outputs);
    assert.ok(f.calls.every(call => call.endpoint !== 'pr'));
  });
}
for (const branch of ['feature/untrusted', 'main']) {
  test(`rejects untrusted push (${branch})`, async () => {
    const f = fixture(); Object.assign(f.run, { event: 'push', head_branch: branch });
    await assert.rejects(resolveSource(f), /Unexpected push report source/);
  });
}
test('automatic reporting retains failure evidence when a report exists', async () => {
  const f = fixture(); f.run.conclusion = 'failure';
  const { outputs } = await resolveSource(f);
  await confirm(f, outputs);
  assert.deepEqual(outputs, expected());
});
test('automatic successful producer may intentionally have no reports', async () => {
  const f = fixture(); f.artifacts = [];
  const { outputs, notices } = await resolveSource(f);
  assert.deepEqual(outputs, {}); assert.equal(notices.length, 1);
});
test('automatic failed producer without reports fails', async () => {
  const f = fixture(); f.artifacts = []; f.run.conclusion = 'failure';
  await assert.rejects(resolveSource(f), /did not preserve reports/);
});

test('owner dispatch selects exact latest producer without deriving executable code refs', async () => {
  const f = fixture(true);
  assert.deepEqual(await resolveSource(f), { outputs: expected('78', '1'), notices: [] });
  assert.deepEqual(f.calls.map(call => call.endpoint), ['pr', 'run', 'workflow', 'attempt', 'artifacts']);
  assert.equal(f.calls.find(call => call.endpoint === 'run').params.run_id, 123);
  assert.equal(f.calls.find(call => call.endpoint === 'workflow').params.workflow_id, 'tests.yml');
  assert.equal(f.calls.find(call => call.endpoint === 'attempt').params.run_id, 123);
  assert.equal(f.calls.find(call => call.endpoint === 'attempt').params.attempt_number, 2);
});
test('owner dispatch may also select a successful PR event producer', async () => {
  const f = fixture(true); f.run.event = 'pull_request';
  assert.deepEqual((await resolveSource(f)).outputs, expected('78', '1'));
});
for (const field of ['producer_run_id', 'producer_run_attempt', 'pull_request_number']) {
  for (const invalid of ['', '0', '-1', '1.2', '1e2', '01', '9007199254740992', '1;exit']) {
    test(`rejects malformed ${field} (${invalid}) before API access`, async () => {
      const f = fixture(true); f.context.payload.inputs[field] = invalid;
      await assert.rejects(resolveSource(f), /Invalid/);
      assert.equal(f.calls.length, 0);
    });
  }
}
const manualRejects = {
  'foreign dispatcher': f => { f.context.actor = 'contributor'; },
  'foreign reporter rerunner': f => { f.env.REPORTER_TRIGGERING_ACTOR = 'contributor'; },
  'invalid expected SHA': f => { f.context.payload.inputs.expected_head_sha = 'a'.repeat(39); },
  'stale reporter checkout': f => { f.context.sha = trustedSha; },
  'stale reporter definition': f => { f.env.REPORTER_WORKFLOW_SHA = trustedSha; },
  'tag dispatch': f => { f.context.ref = 'refs/tags/feature/speech'; },
  'other branch dispatch': f => { f.context.ref = 'refs/heads/other'; },
  'wrong PR number': f => { f.pr.number = 79; },
  'closed PR': f => { f.pr.state = 'closed'; },
  'draft PR': f => { f.pr.draft = true; },
  'foreign base': f => { f.pr.base.repo = { id: 3 }; },
  'other base branch': f => { f.pr.base.ref = 'release/1.0'; },
  'fork source': f => { f.pr.head.repo.id = 2; },
  'deleted source': f => { f.pr.head.repo = null; },
  'stale PR head': f => { f.pr.head.sha = trustedSha; },
  'wrong run ID': f => { f.run.id = 456; },
  'stale attempt': f => { f.run.run_attempt = 3; },
  'incomplete run': f => { f.run.status = 'in_progress'; },
  'failed run': f => { f.run.conclusion = 'failure'; },
  'cancelled run': f => { f.run.conclusion = 'cancelled'; },
  'skipped run': f => { f.run.conclusion = 'skipped'; },
  'wrong workflow ID': f => { f.run.workflow_id = 999; },
  'wrong workflow path': f => { f.run.path = '.github/workflows/other.yml'; },
  'wrong registered workflow': f => { f.workflow.path = '.github/workflows/other.yml'; },
  'push event': f => { f.run.event = 'push'; },
  'schedule event': f => { f.run.event = 'schedule'; },
  'foreign producer repository': f => { f.run.repository = { id: 3 }; },
  'foreign producer head repository': f => { f.run.head_repository.id = 2; },
  'wrong producer SHA': f => { f.run.head_sha = trustedSha; },
  'wrong producer branch': f => { f.run.head_branch = 'other'; },
  'foreign producer dispatcher': f => { f.run.actor.login = 'contributor'; },
  'foreign producer rerunner': f => { f.run.triggering_actor.login = 'contributor'; },
};
for (const [name, change] of Object.entries(manualRejects)) {
  test(`manual selection rejects ${name} before artifacts`, async () => {
    const f = fixture(true); change(f);
    await assert.rejects(resolveSource(f));
    assert.ok(f.calls.every(call => call.endpoint !== 'artifacts'));
  });
}
const invalidAttempt = {
  'wrong run ID': attempt => { attempt.id = 456; },
  'wrong attempt': attempt => { attempt.run_attempt = 1; },
  'wrong workflow ID': attempt => { attempt.workflow_id = 999; },
  'wrong workflow path': attempt => { attempt.path = '.github/workflows/other.yml'; },
  'wrong repository': attempt => { attempt.repository = { id: 3 }; },
  'wrong source repository': attempt => { attempt.head_repository.id = 3; },
  'wrong source SHA': attempt => { attempt.head_sha = trustedSha; },
  'wrong source branch': attempt => { attempt.head_branch = 'other'; },
  'wrong event': attempt => { attempt.event = 'push'; },
  'incomplete attempt': attempt => { attempt.status = 'in_progress'; },
  'unsuccessful attempt': attempt => { attempt.conclusion = 'failure'; },
  'missing start': attempt => { delete attempt.run_started_at; },
  'invalid start': attempt => { attempt.run_started_at = 'invalid'; },
  'missing finish': attempt => { delete attempt.updated_at; },
  'invalid finish': attempt => { attempt.updated_at = 'invalid'; },
  'reversed interval': attempt => { attempt.updated_at = '2026-11-09T08:00:00Z'; },
};
for (const [name, change] of Object.entries(invalidAttempt)) {
  test(`rejects attempt metadata with ${name} before artifacts`, async () => {
    const f = fixture(true); f.attempt = structuredClone(f.run); change(f.attempt);
    await assert.rejects(resolveSource(f), /attempt identity or execution interval/);
    assert.ok(f.calls.every(call => call.endpoint !== 'artifacts'));
  });
}
for (const bound of ['run_started_at', 'updated_at']) {
  test(`accepts artifact creation at the attempt's ${bound} boundary`, async () => {
    const f = fixture(true); f.artifacts[0].created_at = f.run[bound];
    assert.deepEqual((await resolveSource(f)).outputs, expected('78', '1'));
  });
}
test('automatic reporting also rejects earlier-attempt artifacts with a future name', async () => {
  const f = fixture(); f.artifacts[0].created_at = '2026-11-09T08:00:00Z';
  await assert.rejects(resolveSource(f), /not created during the selected attempt/);
});
const badArtifacts = {
  'missing artifact': f => { f.artifacts = []; },
  'expired artifact': f => { f.artifacts[0].expired = true; },
  'duplicate artifacts': f => { f.artifacts.push(structuredClone(f.artifacts[0])); },
  'other attempt': f => { f.artifacts[0].name = 'coverage-evidence-1'; },
  'artifact from an earlier attempt with the selected name': f => { f.artifacts[0].created_at = '2026-11-09T08:00:00Z'; },
  'artifact after the selected attempt': f => { f.artifacts[0].created_at = '2026-11-09T09:11:00Z'; },
  'missing creation timestamp': f => { delete f.artifacts[0].created_at; },
  'invalid creation timestamp': f => { f.artifacts[0].created_at = 'invalid'; },
  'empty archive': f => { f.artifacts[0].size_in_bytes = 0; },
  'oversized archive': f => { f.artifacts[0].size_in_bytes = 33554433; },
  'invalid artifact ID': f => { f.artifacts[0].id = -1; },
  'missing provenance': f => { delete f.artifacts[0].workflow_run; },
  'other run': f => { f.artifacts[0].workflow_run.id = 456; },
  'other repository': f => { f.artifacts[0].workflow_run.repository_id = 3; },
  'other head repository': f => { f.artifacts[0].workflow_run.head_repository_id = 3; },
  'other source SHA': f => { f.artifacts[0].workflow_run.head_sha = trustedSha; },
  'other source branch': f => { f.artifacts[0].workflow_run.head_branch = 'other'; },
};
for (const [name, change] of Object.entries(badArtifacts)) {
  test(`manual selection rejects ${name}`, async () => {
    const f = fixture(true); change(f);
    await assert.rejects(resolveSource(f));
  });
}

test('pre-upload recheck accepts the selected exact manual producer', async () => {
  const f = fixture(true); const { outputs } = await resolveSource(f);
  await confirm(f, outputs);
});
const races = {
  'new run attempt': f => { f.run.run_attempt = 3; },
  'run in progress': f => { f.run.status = 'in_progress'; },
  'failed manual run': f => { f.run.conclusion = 'failure'; },
  'different run': f => { f.run.id = 456; },
  'foreign run repository': f => { f.run.repository = { id: 3 }; },
  'different workflow': f => { f.run.path = '.github/workflows/other.yml'; },
  'different run source': f => { f.run.head_sha = trustedSha; },
  'different run branch': f => { f.run.head_branch = 'other'; },
  'different run head repository': f => { f.run.head_repository.id = 3; },
  'PR closed': f => { f.pr.state = 'closed'; },
  'PR number changed': f => { f.pr.number = 79; },
  'PR became draft': f => { f.pr.draft = true; },
  'PR moved base repository': f => { f.pr.base.repo = { id: 3 }; },
  'PR retargeted': f => { f.pr.base.ref = 'release/1.0'; },
  'PR changed source repository': f => { f.pr.head.repo.id = 3; },
  'PR lost source repository': f => { f.pr.head.repo = null; },
  'PR advanced': f => { f.pr.head.sha = trustedSha; },
  'PR changed branch': f => { f.pr.head.ref = 'other'; },
};
for (const [name, change] of Object.entries(races)) {
  test(`pre-upload recheck rejects ${name}`, async () => {
    const f = fixture(true); const { outputs } = await resolveSource(f);
    change(f); await assert.rejects(confirm(f, outputs), /changed after artifact selection/);
  });
}
