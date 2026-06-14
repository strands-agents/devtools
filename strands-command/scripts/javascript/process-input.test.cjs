// Run with: node --test strands-command/scripts/javascript/process-input.test.cjs
const { test, beforeEach } = require('node:test');
const assert = require('node:assert');
const fs = require('fs');
const os = require('os');
const path = require('path');

const processInput = require('./process-input.cjs');

// The parser reads SOP files from devtools/strands-command/agent-sops/ relative
// to cwd, and writes strands-parsed-input.json to cwd. Run from a temp dir with
// a `devtools` symlink pointing at the repo root.
const repoRoot = path.resolve(__dirname, '../../..');
const workDir = fs.mkdtempSync(path.join(os.tmpdir(), 'process-input-test-'));
fs.symlinkSync(repoRoot, path.join(workDir, 'devtools'));
process.chdir(workDir);

function makeGithub({ isPullRequest = false } = {}) {
  return {
    rest: {
      issues: {
        get: async ({ issue_number }) => ({
          data: {
            number: Number(issue_number),
            pull_request: isPullRequest ? {} : undefined,
          },
        }),
      },
      pulls: {
        get: async ({ pull_number }) => ({
          data: {
            number: Number(pull_number),
            head: { ref: 'some-branch', repo: { full_name: 'owner/repo' } },
          },
        }),
      },
      git: {
        getRef: async () => ({ data: { object: { sha: 'abc123' } } }),
        createRef: async () => ({}),
      },
    },
  };
}

function makeContext({ eventName, payload = {} }) {
  return { eventName, payload, repo: { owner: 'owner', repo: 'repo' } };
}

function makeCore() {
  const core = { failures: [] };
  core.setFailed = (msg) => core.failures.push(msg);
  return core;
}

function readOutput() {
  return JSON.parse(fs.readFileSync('strands-parsed-input.json', 'utf8'));
}

beforeEach(() => {
  fs.rmSync('strands-parsed-input.json', { force: true });
});

test('explicit inputs take precedence regardless of event name', async () => {
  const core = makeCore();
  await processInput(
    makeContext({ eventName: 'pull_request_target' }),
    makeGithub({ isPullRequest: true }),
    core,
    { issue_id: '42', command: 'dependabot-analyze', session_id: '' }
  );
  assert.deepStrictEqual(core.failures, []);
  const out = readOutput();
  assert.strictEqual(out.issue_id, '42');
  assert.strictEqual(out.mode, 'dependabot-analyze');
});

test('issue_comment payload is used when no explicit inputs', async () => {
  const core = makeCore();
  await processInput(
    makeContext({
      eventName: 'issue_comment',
      payload: { issue: { number: 7 }, comment: { body: '/strands refine please' } },
    }),
    makeGithub(),
    core,
    { issue_id: '', command: '', session_id: '' }
  );
  assert.deepStrictEqual(core.failures, []);
  const out = readOutput();
  assert.strictEqual(out.issue_id, '7');
  assert.strictEqual(out.mode, 'refiner');
});

test('dependabot-analyze mode resolves its SOP', async () => {
  const core = makeCore();
  await processInput(
    makeContext({ eventName: 'workflow_dispatch' }),
    makeGithub({ isPullRequest: true }),
    core,
    { issue_id: '42', command: 'dependabot-analyze', session_id: '' }
  );
  assert.deepStrictEqual(core.failures, []);
  const out = readOutput();
  assert.match(out.system_prompt, /Dependency Update Analyst/);
});

test('empty command with explicit issue_id defaults by issue type', async () => {
  const core = makeCore();
  await processInput(
    makeContext({ eventName: 'workflow_dispatch' }),
    makeGithub({ isPullRequest: false }),
    core,
    { issue_id: '42', command: '', session_id: '' }
  );
  assert.deepStrictEqual(core.failures, []);
  assert.strictEqual(readOutput().mode, 'refiner');
});

test('fails with clear error when issue_id is missing for non-comment events', async () => {
  const core = makeCore();
  await processInput(
    makeContext({ eventName: 'workflow_dispatch' }),
    makeGithub(),
    core,
    { issue_id: '', command: '', session_id: '' }
  );
  assert.strictEqual(core.failures.length, 1);
  assert.match(core.failures[0], /No issue_id input provided/);
});
