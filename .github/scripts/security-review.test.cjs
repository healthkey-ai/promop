const { test } = require('node:test');
const assert = require('node:assert/strict');
const { securityLabels, issueReferences, reviewDecision } = require('./security-review.cjs');
const pr = { user: { login: 'author' }, head: { sha: 'current' } };
const review = (state, commit_id = 'current', login = 'larsburgess', id = 1) => ({ id, state, commit_id, user: { login, id: login === 'larsburgess' ? 23724 : 98765 } });
const allowed = new Set(['author', 'larsburgess', 'second']);

test('security labels are explicit and case insensitive', () => {
  for (const label of ['security', 'Security', 'security:critical', 'security-fix']) assert.equal(securityLabels([{ name: label }]), true);
  for (const label of ['tests', 'security-policy-docs-only', 'not-security']) {
    assert.equal(securityLabels([{ name: label }]), label.startsWith('security-'));
  }
});

test('finds local, qualified, and URL issue references without duplicates', () => {
  assert.deepEqual(issueReferences({ title: 'Fix #141', body: 'Closes #141; other/repo#23; https://github.com/other/repo/issues/24' }, 'healthkey-ai/promop'), [
    { repository: 'other/repo', number: 24 },
    { repository: 'healthkey-ai/promop', number: 141 },
    { repository: 'other/repo', number: 23 },
  ]);
});

test('requires current independent approval by Lars', () => {
  assert.equal(reviewDecision(pr, [], allowed), false);
  assert.equal(reviewDecision(pr, [review('APPROVED')], allowed), true);
  assert.equal(reviewDecision(pr, [review('APPROVED', 'old')], allowed), false);
  assert.equal(reviewDecision(pr, [review('APPROVED', 'current', 'author')], allowed), false);
  assert.equal(reviewDecision(pr, [review('APPROVED', 'current', 'reader')], allowed), false);
  assert.equal(reviewDecision(pr, [{ ...review('APPROVED'), user: { login: 'larsburgess', id: 23724, type: 'Bot' } }], allowed), false);
});

test('dismissal and requested changes invalidate approval; comments do not', () => {
  assert.equal(reviewDecision(pr, [review('APPROVED'), review('DISMISSED', 'current', 'larsburgess', 2)], allowed), false);
  assert.equal(reviewDecision(pr, [review('APPROVED'), review('COMMENTED', 'current', 'larsburgess', 2)], allowed), true);
  assert.equal(reviewDecision(pr, [review('APPROVED'), review('CHANGES_REQUESTED', 'old', 'second', 2)], allowed), false);
  assert.equal(reviewDecision(pr, [review('CHANGES_REQUESTED'), review('APPROVED', 'current', 'larsburgess', 2)], allowed), true);
});

const { securityFiles, evaluate, run } = require('./security-review.cjs');

test('file classification covers security code and renames while excluding test-only edits', () => {
  for (const filename of ['ctomop/settings.py', 'frontend/src/components/Auth/Login.tsx', '.github/workflows/ci.yml', 'patient_portal/api/permissions.py', '.bandit-baseline.json']) assert.equal(securityFiles([{ filename }]), true, filename);
  for (const filename of ['patient_portal/tests.py', 'tests/test_browser_oauth_retirement.py', 'frontend/src/components/Auth/Login.test.tsx', 'omop_core/services/genomics.py']) assert.equal(securityFiles([{ filename }]), false, filename);
  assert.equal(securityFiles([{ filename: 'retired.py', previous_filename: 'ctomop/oauth.py' }]), true);
});

function fixture({ labels = [], files = [], issueLabels = [], reviews = [], broken = false } = {}) {
  const current = { ...pr, number: 7, labels, title: 'Update', body: '', changed_files: files.length, updated_at: 'now', base: { ref: 'dev' } };
  const statuses = [];
  const github = {
    graphql: async () => ({ repository: { pullRequest: { closingIssuesReferences: {
      nodes: [{ number: 141, repository: { nameWithOwner: 'healthkey-ai/promop' }, labels: { nodes: issueLabels, pageInfo: { hasNextPage: false } } }], pageInfo: { hasNextPage: false },
    } } } }),
    paginate: async (method) => method(),
    rest: {
      pulls: {
        list: async () => [current], listFiles: async () => files, listReviews: async () => reviews,
        get: async () => ({ data: current }),
      },
      issues: { get: async () => { throw new Error('unexpected request'); } },
      repos: {
        getCollaboratorPermissionLevel: async () => ({ data: { permission: 'write' } }),
        getCommit: async () => ({ data: { author: { login: 'author' } } }),
        createCommitStatus: async (status) => { statuses.push(status); },
      },
    },
  };
  if (broken) github.graphql = async () => { throw new Error('API unavailable'); };
  return { github, current, statuses };
}

test('either a file, PR label, or linked issue label requires review', async () => {
  for (const options of [{ files: [{ filename: 'ctomop/oauth.py' }] }, { labels: [{ name: 'security' }] }, { issueLabels: [{ name: 'security' }] }]) {
    const { github, current } = fixture(options);
    assert.equal((await evaluate(github, 'healthkey-ai', 'promop', current)).state, 'failure');
    const approved = fixture({ ...options, reviews: [review('APPROVED')] });
    assert.equal((await evaluate(approved.github, 'healthkey-ai', 'promop', approved.current)).state, 'success');
  }
  const ordinary = fixture();
  assert.equal((await evaluate(ordinary.github, 'healthkey-ai', 'promop', ordinary.current)).state, 'success');
});

test('API errors publish failure, never a passing status', async () => {
  const { github, statuses } = fixture({ broken: true });
  await run({ github, context: { repo: { owner: 'healthkey-ai', repo: 'promop' }, serverUrl: 'https://github.com', runId: 1 }, core: { warning() {} } });
  assert.deepEqual(statuses.map(status => status.state), ['pending', 'failure']);
});

test('a security label on a referenced non-closing issue also requires review', async () => {
  const { github, current } = fixture();
  current.body = 'Related to #141';
  github.graphql = async () => ({ repository: { pullRequest: { closingIssuesReferences: { nodes: [], pageInfo: { hasNextPage: false } } } } });
  github.rest.issues.get = async () => ({ data: { labels: [{ name: 'security' }] } });
  assert.equal((await evaluate(github, 'healthkey-ai', 'promop', current)).state, 'failure');
});

test('an ordinary PR cannot overwrite a security failure on a shared head', async () => {
  const { github, current, statuses } = fixture();
  const security = { ...current, number: 8, labels: [{ name: 'security' }] };
  github.rest.pulls.list = async () => [security, current];
  github.rest.pulls.get = async p => ({ data: p.pull_number === 8 ? security : current });
  await run({ github, context: { repo: { owner: 'healthkey-ai', repo: 'promop' }, serverUrl: 'https://github.com', runId: 1 }, core: { warning() {} } });
  assert.deepEqual(statuses.map(status => status.state), ['pending', 'failure']);
});

test('a PR changed during evaluation cannot receive a passing status', async () => {
  const { github, current, statuses } = fixture();
  let reads = 0;
  github.rest.pulls.get = async () => ({ data: { ...current, updated_at: ++reads === 1 ? 'now' : 'later' } });
  await run({ github, context: { repo: { owner: 'healthkey-ai', repo: 'promop' }, serverUrl: 'https://github.com', runId: 1 }, core: { warning() {} } });
  assert.deepEqual(statuses.map(status => status.state), ['pending', 'failure']);
});


test('only Lars can approve security changes with write access and no team requirement', async () => {
  for (const permission of ['write', 'maintain', 'admin']) {
    const { github, current } = fixture({ issueLabels: [{ name: 'security' }], reviews: [review('APPROVED', 'current', 'larsburgess')] });
    github.rest.repos.getCollaboratorPermissionLevel = async () => ({ data: { permission } });
    assert.equal((await evaluate(github, 'healthkey-ai', 'promop', current)).state, 'success');
  }
  for (const permission of ['read', 'triage']) {
    const { github, current } = fixture({ labels: [{ name: 'security' }], reviews: [review('APPROVED')] });
    github.rest.repos.getCollaboratorPermissionLevel = async () => ({ data: { permission } });
    assert.equal((await evaluate(github, 'healthkey-ai', 'promop', current)).state, 'failure');
  }
});

test('security control and existing protected paths require review', () => {
  for (const filename of ['omop_core/authorization.py', 'patient_portal/services.py', 'patient_portal/api/break_glass.py', 'patient_portal/checks.py', 'start.sh', 'CODEOWNERS', '.github/CODEOWNERS', 'docs/soc2/change-management.md', 'scripts/capture_change_management_evidence.py']) {
    assert.equal(securityFiles([{ filename }]), true, filename);
  }
});


test('another admin cannot substitute for the named reviewer', async () => {
  const { github, current } = fixture({ labels: [{ name: 'security' }], reviews: [review('APPROVED', 'current', 'another-developer')] });
  github.rest.repos.getCollaboratorPermissionLevel = async () => ({ data: { permission: 'admin' } });
  assert.equal((await evaluate(github, 'healthkey-ai', 'promop', current)).state, 'failure');
});

test('reviewer identity and independent authorship are required', () => {
  const wrongIdentity = { ...review('APPROVED'), user: { login: 'larsburgess', id: 98765 } };
  assert.equal(reviewDecision(pr, [wrongIdentity], allowed), false);
  const larsAsAuthor = { ...pr, user: { login: 'larsburgess', id: 23724 } };
  assert.equal(reviewDecision(larsAsAuthor, [review('APPROVED')], allowed), false);
});
