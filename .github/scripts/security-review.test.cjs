const { test } = require('node:test');
const assert = require('node:assert/strict');
const { securityLabels, issueReferences, reviewDecision, securityPath } = require('./security-review.cjs');
const pr = { user: { login: 'author' }, head: { sha: 'current' } };
const review = (state, commit_id = 'current', login = 'larsburgess', id = 1) => ({ id, state, commit_id, user: { login, id: login === 'larsburgess' ? 23724 : 98765 } });
const allowed = new Set(['author', 'larsburgess', 'second']);

test('any label carrying the word security requires review', () => {
  for (const label of ['security', 'Security', 'security:critical', 'security-fix', 'Security/high',
                       'P0-security', 'needs-security-review', 'security-policy-docs-only']) {
    assert.equal(securityLabels([{ name: label }]), true, label);
  }
  for (const label of ['tests', 'documentation', 'sec']) assert.equal(securityLabels([{ name: label }]), false, label);
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
  for (const filename of ['patient_portal/tests.py', 'tests/test_browser_oauth_retirement.py', 'frontend/src/components/Auth/Login.test.tsx', 'omop_core/services/genomics.py', 'Dockerfile', 'render.yaml', 'start.sh', 'patient_portal/api/v1_urls.py']) assert.equal(securityFiles([{ filename }]), false, filename);
  assert.equal(securityFiles([{ filename: 'retired.py', previous_filename: 'patient_portal/api/permissions.py' }]), true);
  // Renaming the OAuth validator out of the way is still a gated change.
  assert.equal(securityFiles([{ filename: 'retired.py', previous_filename: 'ctomop/oauth.py' }]), true);
});

function fixture({ labels = [], files = [], issueLabels = [], reviews = [], broken = false, existingStatuses = [] } = {}) {
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
        listCommitStatusesForRef: async () => ({ data: existingStatuses }),
        createCommitStatus: async (status) => { statuses.push(status); },
      },
    },
  };
  if (broken) github.graphql = async () => { throw new Error('API unavailable'); };
  return { github, current, statuses };
}

test('either a file, PR label, or linked issue label requires review', async () => {
  for (const options of [{ files: [{ filename: 'patient_portal/api/permissions.py' }] }, { labels: [{ name: 'security' }] }, { issueLabels: [{ name: 'security' }] }]) {
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
  await run({ github, context: { repo: { owner: 'healthkey-ai', repo: 'promop' }, serverUrl: 'https://github.com', runId: 1, eventName: 'pull_request_target' }, core: { warning() {}, setFailed() {} } });
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
  await run({ github, context: { repo: { owner: 'healthkey-ai', repo: 'promop' }, serverUrl: 'https://github.com', runId: 1, eventName: 'pull_request_target' }, core: { warning() {}, setFailed() {} } });
  assert.deepEqual(statuses.map(status => status.state), ['pending', 'failure']);
});

test('a PR changed during evaluation cannot receive a passing status', async () => {
  const { github, current, statuses } = fixture();
  let reads = 0;
  github.rest.pulls.get = async () => ({ data: { ...current, updated_at: ++reads === 1 ? 'now' : 'later' } });
  await run({ github, context: { repo: { owner: 'healthkey-ai', repo: 'promop' }, serverUrl: 'https://github.com', runId: 1, eventName: 'pull_request_target' }, core: { warning() {}, setFailed() {} } });
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
  for (const filename of ['omop_core/authorization.py', 'patient_portal/services.py', 'patient_portal/api/break_glass.py', 'patient_portal/checks.py', 'CODEOWNERS', '.github/CODEOWNERS', 'docs/soc2/change-management.md', 'scripts/capture_change_management_evidence.py']) {
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

test('a test-named file inside the control plane is still gated', () => {
  for (const filename of [
    '.github/scripts/security-review.test.cjs', '.github/workflows/deploy.test.yml',
    '.github/workflows/security.test.yml', '.github/actions/tests/action.yml',
    '.github/tests/policy.yml', 'docs/soc2/tests/evidence.json',
    'tests/CODEOWNERS', '.gitleaks.test.toml',
  ]) assert.equal(securityPath(filename), true, filename);
});

test('application test files stay exempt', () => {
  for (const filename of [
    'patient_portal/tests.py', 'tests/test_browser_oauth_retirement.py',
    'frontend/src/components/Auth/Login.test.tsx', 'patient_portal/api/tests/test_permissions.py',
  ]) assert.equal(securityPath(filename), false, filename);
});

test('auth/identity, settings and credential handling are gated', () => {
  for (const filename of [
    // Both project packages whole, in every spelling: the settings module as a
    // file and as a package, the OAuth validator, the Sentry scrubber, and any
    // module that does not exist yet.
    'promop/settings.py', 'ctomop/settings.py', 'promop/settings/base.py',
    'promop/settings_prod.py', 'ctomop/settings_staging.py', 'promop/settings/__init__.py',
    'ctomop/settings/base.py', 'promop/config/settings.py',
    'promop/oauth.py', 'ctomop/oauth.py', 'promop/sentry.py', 'ctomop/sentry.py',
    'promop/oauth/validators.py', 'promop/sentry/scrubber.py',
    'frontend/src/api/publicAxios.ts', 'frontend/src/api/clinicalTransport.ts',
    'frontend/src/federation/assertLabsTokens.ts', 'patient_portal/service_tokens.py',
    'patient_portal/management/commands/import_service_tokens.py',
    'docs/promop-security-soc2-remediation-plan.md',
    // Both manifests: the scanners fire on an advisory against a dependency
    // already present, not on one being added, which is the reviewable event
    // and is the same event in both ecosystems.
    'frontend/package.json', 'frontend/package-lock.json',
    'requirements.txt', 'requirements/base.txt',
    // The project route table — the only urls.py carrying AllowAny, and the
    // one that mounts admin/ and the OAuth2 provider tree.
    'promop/urls.py', 'ctomop/urls.py',
    // One path per pattern, for every pattern on either list: a pattern that no
    // path here exercises can be deleted without a test noticing, and eight of
    // them could be when this list was written. Every single-file entry has a
    // directory form beside it, so splitting a module into a package cannot
    // ungate its own subject.
    'patient_portal/api/permissions/base.py', 'patient_portal/api/middleware/audit.py',
    'omop_core/authorization/rules.py', 'patient_portal/service_tokens/mint.py',
    'ctomop/oauth/validators.py', 'ctomop/sentry/scrubber.py',
    'patient_portal/services/consent.py', 'patient_portal/api/break_glass/views.py',
    'patient_portal/checks/settings_checks.py', 'patient_portal/api/authentication/jwt.py',
    'patient_portal/service_applications/models.py',
    'promop/urls/__init__.py', 'ctomop/urls/api.py',
    'manage.py',
    'frontend/src/api/axios/interceptors.ts', 'frontend/src/api/publicAxios/index.ts',
    'frontend/src/api/clinicalTransport/index.ts',
    'frontend/src/federation/assertLabsTokens/index.ts',
    'frontend/src/utils/oauth/pkce.ts', 'frontend/src/hooks/useAuth/context.ts',
    'scripts/capture_change_management_evidence/api.py',
    // Entries that predate this list and were exercised by nothing: deleting
    // any of them left the suite green.
    'SECURITY.md', 'patient_portal/api/authentication.py',
    'patient_portal/api/middleware.py', 'patient_portal/api/providers/base.py',
    'patient_portal/service_applications.py', 'frontend/src/utils/oauth.ts',
    'frontend/src/hooks/useAuth.ts', 'frontend/src/api/axios.ts',
  ]) assert.equal(securityPath(filename), true, filename);
});

test('auth, credential lifecycle and privileged-account paths are gated', () => {
  // Added after review of #1496 found them outside the gate on dev as well as
  // on that branch. One path per pattern, file form and directory form, so a
  // pattern nothing exercises cannot be deleted without a test noticing.
  for (const filename of [
    // The viewset that issues service tokens, beside the definitions module of
    // the same name that was already gated.
    'patient_portal/api/service_applications.py',
    'patient_portal/api/service_applications/views.py',
    // authenticate(), the lockout threshold, the enumeration-resistant timing.
    'patient_portal/backends.py', 'patient_portal/backends/email.py',
    // Org-admin authority.
    'omop_core/services/access.py', 'omop_core/services/access/orgs.py',
    // The credential lifecycle of a patient account.
    'patient_portal/api/password_reset.py', 'patient_portal/api/password_reset/views.py',
    'patient_portal/api/email_verification.py', 'patient_portal/api/email_verification/views.py',
    'patient_portal/api/patient_signup.py', 'patient_portal/api/patient_signup/views.py',
    'patient_portal/api/patient_invitations.py', 'patient_portal/api/patient_invitations/accept.py',
    // The writer of the GroupAccess rows access.py reads, and proxy auth.
    'patient_portal/api/org_views.py', 'patient_portal/api/org_views/invitations.py',
    'patient_portal/api/representatives.py', 'patient_portal/api/representatives/views.py',
    // The audit trail's read API.
    'patient_portal/api/audit_views.py', 'patient_portal/api/audit_views/filters.py',
    // Credential-minting commands the *service_token* glob misses on the name.
    'patient_portal/management/commands/create_service_client.py',
    'patient_portal/management/commands/create_smart_app.py',
    // Commands that create standing privilege.
    'omop_core/management/commands/create_staff_user.py',
    'omop_core/management/commands/setup_admin.py',
  ]) assert.equal(securityPath(filename), true, filename);
  // Shapes that must NOT be swept in. Some of these are real files and some are
  // near-miss spellings that do not exist; either way the point is the glob,
  // not the file. Gating by proximity is what #1496 was undoing.
  for (const filename of [
    'patient_portal/api/views.py', 'patient_portal/api/serializers.py',
    'omop_core/services/genomics.py', 'omop_core/services/access_log_export.py',
    'omop_core/management/commands/setup_admin_docs.py',
    'patient_portal/api/patient_search.py',
  ]) assert.equal(securityPath(filename), false, filename);
  // The test exemption still applies to their tests, as it does everywhere
  // outside the two project packages.
  for (const filename of [
    'patient_portal/tests/test_backends.py', 'tests/test_patient_invitations.py',
  ]) assert.equal(securityPath(filename), false, filename);
});

// Deliberately not gated. Shaped as one list of whatever the classifier does
// gate, rather than as 24 assertions that each path MUST be false. It fails on
// the same inputs — re-gating render.yaml still turns this red — so the gain is
// in what the failure says, not in when it happens: the name and the message
// read as "this list changed", and they name every newly gated path at once
// instead of stopping at the first.
test('the narrowed scope leaves operational paths out', () => {
  const out = [
    'patient_portal/api/v1_urls.py',
    'patient_portal/urls.py', 'start.sh', 'start-worker.sh',
    'ops/artemis/Dockerfile', 'deploy/Dockerfile',
    'Procfile', 'nixpacks.toml', 'frontend/.env.production', 'render.yaml',
    'docker-compose.yml', 'Dockerfile', 'Dockerfile.gcp', '.env.example',
  ].filter(securityPath);
  assert.deepEqual(out, [], `unexpectedly gated: ${out.join(', ')}`);
});

test('the root URLconf is gated while app route tables are not', () => {
  // promop/urls.py is not DRF: AllowAny, the admin mount, the OAuth2 AS mount
  // and a plain-Django catch-all all live there, so IsAuthenticated-by-default
  // does not cover a path() added to it. App URLconfs do get that default.
  for (const filename of [
    'promop/urls.py', 'ctomop/urls.py', 'promop/urls/__init__.py', 'ctomop/urls/api.py',
  ]) assert.equal(securityPath(filename), true, filename);
  // And the module that decides which settings define that default, in each of
  // the three places it is spelled. Nothing pins these: the tests that name
  // DJANGO_SETTINGS_MODULE set it in the environment before asserting anything,
  // so a change to the setdefault would leave every one of them green while
  // production loaded a different settings module.
  for (const filename of [
    'promop/wsgi.py', 'promop/asgi.py', 'promop/celery.py',
    'ctomop/wsgi.py', 'ctomop/asgi.py', 'ctomop/celery.py', 'manage.py',
  ]) assert.equal(securityPath(filename), true, filename);
  // A module that does not exist yet is the case a per-filename list cannot
  // reach: repointing wsgi.py at `promop.conf` needs promop/conf.py to be
  // gated on the strength of its directory alone.
  for (const filename of [
    'promop/conf.py', 'promop/whitenoise.py', 'promop/frontend_paths.py',
    'ctomop/conf.py',
  ]) assert.equal(securityPath(filename), true, filename);
  for (const filename of [
    'patient_portal/urls.py', 'patient_portal/api/urls.py', 'patient_portal/api/v1_urls.py',
    'omop_core/urls.py',
  ]) assert.equal(securityPath(filename), false, filename);
});

test('the tests that pin the ungated operational files are themselves gated', () => {
  // start.sh and render.yaml stay out of scope on the grounds that their
  // security-relevant values are pinned by tests. That holds only while the
  // pins cannot be relaxed in the same ungated change, so the pins are gated:
  // test_web_startup asserts start.sh's exact command sequence including
  // `check --deploy --fail-level ERROR`, and the render tests pin DEBUG,
  // CORS_ALLOWED_ORIGINS, SERVICE_AUTH_SCOPES and the broker ipAllowList.
  for (const filename of [
    'tests/test_web_startup.py', 'tests/test_deployment_startup_contract.py',
    'tests/test_render_staging_blueprint.py', 'tests/test_render_production_settings.py',
    'tests/test_render_blueprint_invariants.py', 'tests/test_artemis_runtime.py',
  ]) assert.equal(securityPath(filename), true, filename);
  // The exemption still covers ordinary application tests.
  for (const filename of ['tests/test_webhooks.py', 'tests/test_suggest_strategies.py']) {
    assert.equal(securityPath(filename), false, filename);
  }
});

test('the test exemption does not reach inside the project packages', () => {
  // A test_settings.py is a settings module whatever it is named, and wherever
  // it sits — ctomop/ carried one until 0ba8665d. CONTROL_PATHS is matched
  // before TEST_PATHS, so inside these two packages the exemption is off
  // entirely: that is what reaches a settings module parked under tests/.
  for (const filename of [
    'promop/test_settings.py', 'promop/settings/test_base.py', 'ctomop/test_settings.py',
    'promop/tests/test_settings.py',
  ]) assert.equal(securityPath(filename), true, filename);
  // It reaches an ordinary-looking test in there too, which is the cost of the
  // rule and is deliberate rather than an oversight: neither directory exists,
  // and a 20-file configuration package holds nothing a test-shaped name makes
  // safe. Asserted so that ungating it has to be a decision someone writes down.
  for (const filename of [
    'promop/tests/test_views.py', 'promop/tests/urls.py', 'ctomop/tests/test_urls.py',
  ]) assert.equal(securityPath(filename), true, filename);
  // Outside them, the exemption is untouched at any depth.
  for (const filename of [
    'patient_portal/api/tests/test_permissions.py', 'omop_core/tests/test_access.py',
  ]) assert.equal(securityPath(filename), false, filename);
});

test('a path carrying stray whitespace cannot slip past classification', () => {
  assert.equal(securityPath('omop_core/authorization.py\n'), true);
  assert.equal(securityPath(' promop/settings.py '), true);
});

test('issue references are deduplicated numerically and bounded', () => {
  assert.deepEqual(issueReferences({ title: 'Fix #12', body: 'also #00012 and #0' }, 'healthkey-ai/promop'),
    [{ repository: 'healthkey-ai/promop', number: 12 }]);
  const many = { title: '', body: Array.from({ length: 21 }, (unused, index) => `#${index + 1}`).join(' ') };
  assert.throws(() => issueReferences(many, 'healthkey-ai/promop'), /More than 20 linked issues/);
  const atLimit = { title: '', body: Array.from({ length: 20 }, (unused, index) => `#${index + 1}`).join(' ') };
  assert.equal(issueReferences(atLimit, 'healthkey-ai/promop').length, 20);
});

test('a review from a deleted account is ignored, not fatal', () => {
  const deleted = { id: 5, state: 'APPROVED', commit_id: 'current', user: null };
  assert.equal(reviewDecision(pr, [deleted], allowed), false);
  assert.equal(reviewDecision(pr, [deleted, review('APPROVED', 'current', 'larsburgess', 6)], allowed), true);
});

test('a scheduled run rewrites nothing when the verdict is unchanged', async () => {
  const existing = [{ context: 'Security review', state: 'success', description: 'No security-sensitive files or security-labelled PR/issues' }];
  const { github, statuses } = fixture({ existingStatuses: existing });
  await run({ github, context: { repo: { owner: 'healthkey-ai', repo: 'promop' }, serverUrl: 'https://github.com', runId: 1, eventName: 'schedule' }, core: { warning() {}, setFailed() {} } });
  assert.deepEqual(statuses, []);
});

test('an event marks an existing verdict pending while it is rechecked', async () => {
  const existing = [{ context: 'Security review', state: 'success', description: 'No security-sensitive files or security-labelled PR/issues' }];
  const { github, statuses } = fixture({ labels: [{ name: 'security' }], existingStatuses: existing });
  await run({ github, context: { repo: { owner: 'healthkey-ai', repo: 'promop' }, serverUrl: 'https://github.com', runId: 1, eventName: 'pull_request_target' }, core: { warning() {}, setFailed() {} } });
  assert.deepEqual(statuses.map(status => status.state), ['pending', 'failure']);
});

test('a scheduled run publishes a changed verdict over an existing status', async () => {
  const existing = [{ context: 'Security review', state: 'success', description: 'No security-sensitive files or security-labelled PR/issues' }];
  const { github, statuses } = fixture({ files: [{ filename: 'promop/settings.py' }], existingStatuses: existing });
  await run({ github, context: { repo: { owner: 'healthkey-ai', repo: 'promop' }, serverUrl: 'https://github.com', runId: 1, eventName: 'schedule' }, core: { warning() {}, setFailed() {} } });
  assert.deepEqual(statuses.map(status => status.state), ['failure']);
});

test('a failure to list pull requests fails the run instead of leaving stale statuses', async () => {
  const { github, statuses } = fixture();
  github.paginate = async (method) => { if (method === github.rest.pulls.list) throw new Error('rate limited'); return method(); };
  const failures = [];
  await run({ github, context: { repo: { owner: 'healthkey-ai', repo: 'promop' }, serverUrl: 'https://github.com', runId: 1, eventName: 'pull_request_target' }, core: { warning() {}, setFailed: message => failures.push(message) } });
  assert.deepEqual(statuses, []);
  assert.match(failures[0], /Unable to list open pull requests/);
});

test('a status write that fails for one commit does not skip the next', async () => {
  const { github, statuses } = fixture({ labels: [{ name: 'security' }] });
  const one = { ...pr, number: 7, labels: [{ name: 'security' }], title: 'Update', body: '', changed_files: 0, updated_at: 'now', base: { ref: 'dev' }, head: { sha: 'sha-one' } };
  const two = { ...one, number: 8, head: { sha: 'sha-two' } };
  github.rest.pulls.list = async () => [one, two];
  github.rest.pulls.get = async ({ pull_number }) => ({ data: pull_number === 7 ? one : two });
  github.rest.repos.createCommitStatus = async (status) => {
    if (status.sha === 'sha-one') throw new Error('422 status limit reached');
    statuses.push(status);
  };
  const warnings = [];
  await run({ github, context: { repo: { owner: 'healthkey-ai', repo: 'promop' }, serverUrl: 'https://github.com', runId: 1, eventName: 'pull_request_target' }, core: { warning: message => warnings.push(message), setFailed() {} } });
  assert.deepEqual(statuses.map(status => [status.sha, status.state]), [['sha-two', 'pending'], ['sha-two', 'failure']]);
  assert.ok(warnings.some(message => message.includes('sha-one')));
});

test('an unpublishable status fails the run rather than leaving it green', async () => {
  const { github } = fixture({ labels: [{ name: 'security' }] });
  github.rest.repos.listCommitStatusesForRef = async () => { throw new Error('502 Bad Gateway'); };
  const failures = [];
  await run({ github, context: { repo: { owner: 'healthkey-ai', repo: 'promop' }, serverUrl: 'https://github.com', runId: 1, eventName: 'pull_request_target' }, core: { warning() {}, setFailed: message => failures.push(message) } });
  assert.match(failures[0], /Could not publish a security review status/);
});

test('a reference to an issue that does not exist cannot pin a PR at failure', async () => {
  const { github, current } = fixture();
  current.body = 'Matches the brand colour #123456';
  github.graphql = async () => ({ repository: { pullRequest: { closingIssuesReferences: { nodes: [], pageInfo: { hasNextPage: false } } } } });
  github.rest.issues.get = async () => { const error = new Error('Not Found'); error.status = 404; throw error; };
  assert.equal((await evaluate(github, 'healthkey-ai', 'promop', current)).state, 'success');
});

test('an issue lookup failing for any other reason still fails closed', async () => {
  const { github, current } = fixture();
  current.body = 'Closes #141';
  github.graphql = async () => ({ repository: { pullRequest: { closingIssuesReferences: { nodes: [], pageInfo: { hasNextPage: false } } } } });
  github.rest.issues.get = async () => { const error = new Error('Server Error'); error.status = 500; throw error; };
  await assert.rejects(() => evaluate(github, 'healthkey-ai', 'promop', current), /Server Error/);
});
