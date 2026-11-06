// Exercise the actual github-script step without credentials or contributor code.
const assert = require('node:assert/strict');
const { readFileSync } = require('node:fs');
const { resolve } = require('node:path');
const { test } = require('node:test');

const workflow = readFileSync(resolve(__dirname, '../../.github/workflows/coverage-reporting.yml'), 'utf8');
const script = workflow.split('          script: |\n')[1].split('      - name:')[0]
  .split('\n').map(line => line.slice(12)).join('\n');
const execute = new (Object.getPrototypeOf(async function () {}).constructor)(
  'context', 'github', 'core', script,
);
const sha = 'a'.repeat(40);

function fixture() {
  const run = {
    id: 123, run_attempt: 2, conclusion: 'success', event: 'pull_request',
    repository: { id: 1 }, head_repository: { id: 2, owner: { login: 'contributor' } },
    path: '.github/workflows/tests.yml', head_sha: sha, head_branch: 'feature/speech',
    pull_requests: [],
  };
  const pr = {
    number: 78, state: 'open', base: { repo: { id: 1 } },
    head: { sha, ref: run.head_branch, repo: { id: 2 } },
  };
  return { run, pr };
}

async function resolveSource({ run, prs, associated = [] }) {
  const outputs = {}, notices = [], calls = [];
  const rest = {
    repos: { listPullRequestsAssociatedWithCommit: 'associated' },
    pulls: { list: 'pulls' }, actions: { listWorkflowRunArtifacts: 'artifacts' },
  };
  await execute(
    { payload: { workflow_run: run, repository: { id: 1 } }, repo: { owner: 'maintainer', repo: 'integration' } },
    { rest, paginate: async (endpoint, params) => {
      calls.push({ endpoint, params });
      if (endpoint === 'associated') return associated;
      if (endpoint === 'pulls') return prs;
      assert.equal(endpoint, 'artifacts');
      assert.equal(params.run_id, 123);
      return [{ name: 'coverage-evidence-2', id: 456, size_in_bytes: 25712, expired: false }];
    } },
    { setOutput: (key, value) => { outputs[key] = value; }, notice: text => notices.push(text) },
  );
  return { outputs, notices, calls };
}

test('fork PR resolves with empty workflow and base-commit associations', async () => {
  const { run, pr } = fixture();
  const result = await resolveSource({ run, prs: [pr] });
  assert.deepEqual(result.outputs, { artifact_id: '456', sha, branch: 'feature/speech', pr: '78' });
  assert.deepEqual(result.notices, []);
  assert.deepEqual(result.calls[0], { endpoint: 'pulls', params: {
    owner: 'maintainer', repo: 'integration', state: 'open', head: 'contributor:feature/speech', per_page: 100,
  } });
});

for (const mismatch of ['sha', 'base', 'repository', 'branch', 'closed', 'deleted']) {
  test(`rejects ${mismatch} mismatch before downloading reports`, async () => {
    const { run, pr } = fixture();
    if (mismatch === 'sha') pr.head.sha = 'b'.repeat(40);
    if (mismatch === 'base') pr.base.repo.id = 3;
    if (mismatch === 'repository') pr.head.repo.id = 3;
    if (mismatch === 'branch') pr.head.ref = 'another-branch';
    if (mismatch === 'closed') pr.state = 'closed';
    if (mismatch === 'deleted') pr.head.repo = null;
    const result = await resolveSource({ run, prs: [pr], associated: [pr] });
    assert.deepEqual(result.outputs, {});
    assert.equal(result.notices.length, 1);
    assert.ok(result.calls.every(call => call.endpoint !== 'artifacts'));
  });
}

for (const count of [0, 2]) {
  test(`skips ${count} matching PRs`, async () => {
    const { run, pr } = fixture();
    const result = await resolveSource({ run, prs: Array(count).fill(pr), associated: Array(count).fill(pr) });
    assert.deepEqual(result.outputs, {});
    assert.equal(result.notices.length, 1);
  });
}

test('same-repository PR is also attributed exactly', async () => {
  const { run, pr } = fixture();
  run.head_repository = { id: 1, owner: { login: 'maintainer' } };
  pr.head.repo.id = 1;
  const result = await resolveSource({ run, prs: [pr], associated: [pr] });
  assert.equal(result.outputs.pr, '78');
});

for (const branch of ['main', 'release/1.0']) {
  test(`trusted ${branch} push keeps exact attribution`, async () => {
    const { run } = fixture();
    Object.assign(run, { event: 'push', head_branch: branch, head_repository: { id: 1 } });
    const result = await resolveSource({ run });
    assert.deepEqual(result.outputs, { artifact_id: '456', sha, branch, pr: '' });
    assert.equal(result.calls[0].endpoint, 'artifacts');
  });
}

for (const branch of ['feature/untrusted', 'main']) {
  test(`rejects untrusted push (${branch})`, async () => {
    const { run } = fixture();
    Object.assign(run, { event: 'push', head_branch: branch });
    await assert.rejects(resolveSource({ run }), /Unexpected push report source/);
  });
}
