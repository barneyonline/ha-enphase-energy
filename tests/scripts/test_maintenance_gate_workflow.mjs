import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { test } from 'node:test';
import vm from 'node:vm';

const workflow = await readFile(new URL('../../.github/workflows/maintenance-gate.yml', import.meta.url), 'utf8');
const marker = '          script: |\n';
const script = workflow.slice(workflow.lastIndexOf(marker) + marker.length)
  .split('\n').map(line => line.startsWith('            ') ? line.slice(12) : line).join('\n');
const head = 'a'.repeat(40), base = 'b'.repeat(40), advanced = 'c'.repeat(40);
const repository = { id: 10, default_branch: 'main' };
const makePR = (number, ref = 'codex/issue-1', labels = []) => ({
  number, state: 'open', draft: false, merged: false, labels,
  head: { sha: head, ref }, base: { sha: base, ref: 'main' },
});

async function run(eventName, payload, options = {}) {
  const open = options.open || [makePR(1), makePR(2, 'human-feature', [{ name: 'automation/hal' }]), makePR(3, 'human-feature')];
  const associated = options.associated || [makePR(99, 'unrelated')];
  const checks = [], updates = [], evaluated = [], associatedReads = [];
  let reads = 0;
  const list = () => {}, associatedList = () => {}, listForRef = () => {};
  const github = {
    rest: {
      pulls: { list, get: async ({ pull_number }) => {
        reads++;
        const pr = structuredClone(open.find(item => item.number === pull_number) || associated.find(item => item.number === pull_number));
        assert(pr, 'Only known PRs may be fetched');
        if (options.changedPR && reads % 2 === 0) Object.assign(pr, options.changedPR);
        return { data: pr };
      } },
      repos: { listPullRequestsAssociatedWithCommit: associatedList },
      checks: { listForRef, create: async input => { const check = { ...input, id: checks.length + 1 }; checks.push(check); return { data: check }; }, update: async input => { updates.push(input); } },
      git: { getRef: async () => ({ data: { ref: options.baseName || 'refs/heads/main', object: { type: options.baseType || 'commit', sha: options.baseTip || base } } }) },
      actions: { getWorkflowRun: async () => ({ data: options.observed }) },
    },
    paginate: async (endpoint, args) => {
      if (endpoint === list) return open;
      if (endpoint === associatedList) { associatedReads.push(args); return associated; }
      if (endpoint === listForRef) return [];
      throw new Error('Unexpected paginated operation');
    },
  };
  const child = { spawnSync: (_command, args) => {
    const number = Number(args[args.indexOf('--pull-number') + 1]);
    evaluated.push(number);
    return { status: 0, stdout: JSON.stringify({ allow: true, merge_authority: true, head_sha: head, base_sha: base }) };
  } };
  const process = { env: { GITHUB_SERVER_URL: 'https://github.com', REQUESTED_PULL_NUMBER: options.requested || '' } };
  const context = { eventName, payload: { repository, ...payload }, repo: { owner: 'example', repo: 'integration' }, runId: 7 };
  await vm.runInNewContext('(async () => {\n' + script + '\n})()', {
    github, context, process, require: name => { assert.equal(name, 'node:child_process'); return child; }, core: { info() {} },
  });
  return { evaluated, updates, associatedReads };
}

test('main push explicitly triggers reconciliation of all managed open PRs, including unrelated heads', async () => {
  assert.match(workflow, /on:\n  push:\n    branches: \[main\]/);
  const result = await run('push', { ref: 'refs/heads/main', after: advanced });
  assert.deepEqual(result.evaluated, [1, 2]);
  assert.equal(result.associatedReads.length, 0);
  assert(result.updates.every(check => check.conclusion === 'success'));
});

test('completed main-branch CI also refreshes all managed open PRs', async () => {
  const result = await run('workflow_run', { workflow_run: { name: 'Tests', head_branch: 'main', head_sha: advanced } });
  assert.deepEqual(result.evaluated, [1, 2]);
  assert.equal(result.associatedReads.length, 0);
});

test('feature-branch CI retains its associated-PR routing', async () => {
  const result = await run('workflow_run', { workflow_run: { name: 'Tests', head_branch: 'codex/issue-99', head_sha: head } });
  assert.deepEqual(result.evaluated, [99]);
  assert.equal(result.associatedReads.length, 1);
});

test('review observer is authenticated before routing, even with a main branch name', async () => {
  const observed = { name: 'Autonomous Maintenance Review Events', event: 'pull_request_review', repository: { id: 10 }, head_sha: head, pull_requests: [] };
  const payload = { workflow_run: { id: 8, name: observed.name, head_branch: 'main' } };
  const result = await run('workflow_run', payload, { observed });
  assert.deepEqual(result.evaluated, [99]);
  await assert.rejects(run('workflow_run', payload, { observed: { ...observed, repository: { id: 11 } } }), /Invalid review observer provenance/);
});

test('direct PR and explicit manual requests still select their one PR', async () => {
  assert.deepEqual((await run('pull_request_target', { pull_request: { number: 1 } })).evaluated, [1]);
  assert.deepEqual((await run('workflow_dispatch', {}, { requested: '2' })).evaluated, [2]);
  await assert.rejects(run('workflow_dispatch', {}, { requested: '2;exit' }), /Invalid pull number/);
});

test('base movement, malformed base refs and final PR changes prevent successful publication', async () => {
  for (const options of [
    { baseTip: advanced }, { baseType: 'tag' }, { baseName: 'refs/heads/other' },
    { changedPR: { base: { sha: advanced, ref: 'main' } } },
    { changedPR: { base: { sha: base, ref: 'other' } } },
    { changedPR: { head: { sha: advanced, ref: 'codex/issue-1' } } },
    { changedPR: { state: 'closed' } }, { changedPR: { draft: true } }, { changedPR: { merged: true } },
  ]) {
    const result = await run('push', { ref: 'refs/heads/main' }, { ...options, open: [makePR(1)] });
    assert.equal(result.updates.length, 1);
    assert.equal(result.updates[0].conclusion, 'failure');
    assert.match(result.updates[0].output.summary, /fresh evidence is required/);
  }
});
