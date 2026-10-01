// A label is security-scoped if the word appears anywhere in it: 'security',
// 'Security/high', 'P0-security' and 'needs-security-review' all count. Wider
// than needed is the safe direction — it can only ask for another approval.
const SECURITY_LABEL = /security/i;
const CONTEXT = 'Security review';
// Referenced issues are fetched one API call each, on every scheduled run, from
// text the PR author controls. Cap the fan-out well below the hourly budget.
const MAX_ISSUE_REFERENCES = 20;
// Paths the test-file exemption must never reach. Two groups:
//   the control plane — CI, the policy that gates it, the scanners it runs, the
//   change-management evidence: a file here executes with the control plane's
//   privileges whatever it is named (`.github/workflows/*.test.yml` is a
//   workflow Actions will run; `.github/scripts/*.test.cjs` was executed by this
//   policy's own workflow), and
//   the two Django project packages, whole.
//
// The packages are taken whole rather than as a list of the interesting modules
// inside them, because the interesting set is not knowable from the filenames.
// `settings.py`, `oauth.py` (OAUTH2_VALIDATOR_CLASS), `sentry.py` (the secret
// denylist) and `urls.py` (the only AllowAny in the tree, the admin mount and a
// plain-Django catch-all) are the ones that read as security today. But
// `wsgi.py`, `asgi.py` and `celery.py` each carry
// `os.environ.setdefault('DJANGO_SETTINGS_MODULE', ...)`, and start.sh runs
// `gunicorn promop.wsgi:application` with nothing setting that variable — so
// the line in wsgi.py is what chooses the settings module in production, and
// no test pins it. tests/test_project_package_compatibility.py and
// tests/test_render_production_settings.py both set the variable themselves
// before asserting anything, and pytest.ini sets it for the suite; none of
// them constrains the setdefault. Repointing it at a module named anything
// else would swap production's settings, and a per-module list would have to
// have anticipated that name to catch it.
//
// Taking them whole costs nothing measurable. The two packages are 20 files
// and 1228 lines. Replaying all 1970 commits of the last 12 months through
// both spellings of this list: four of them touch a module a per-module list
// would have missed (a0ff2d2c, the rename that made promop canonical, plus
// fbdf8d46, 01d80b6a and 148c9d59), and all four also touch settings.py, so
// zero commits are sent for approval by the directory form that were not
// already sent by the per-module one. The precise rule buys no review time and
// fails open on the twenty-first file.
//
// This list is matched before the test exemption, so inside these two packages
// the exemption does not apply at all: a `test_settings.py` is still a settings
// module, and a `promop/tests/urls.py` is still a route table. That reaches
// further than the two examples — `promop/tests/test_views.py` is gated too,
// and neither directory exists today. It is the deliberate trade: a 20-file
// configuration package has nothing in it that a test-shaped name makes safe,
// and the alternative is a rule that turns on whether a settings module was
// parked under `tests/`. Everywhere else in the tree, a file under a directory
// named `tests/` stays exempt at any depth.
const CONTROL_PATHS = [
  '.github/**', '**/CODEOWNERS', 'docs/soc2/**', '.bandit*', '.gitleaks*',
  'SECURITY.md', 'scripts/capture_change_management_evidence.py',
  'scripts/capture_change_management_evidence/**',
  'promop/**', 'ctomop/**',
  // The tests that pin the operational files SECURITY_PATHS deliberately omits.
  // That omission rests on those values being pinned by tests, which only holds
  // while the pins cannot be relaxed in the same ungated change:
  // `test_web_startup` asserts the exact command sequence of start.sh, so
  // deleting its `check --deploy` line turns the suite red, and the render
  // blueprint tests pin DEBUG, CORS_ALLOWED_ORIGINS, SERVICE_AUTH_SCOPES and
  // the broker's empty ipAllowList. This list is matched before TEST_PATHS, so
  // naming them here is what overrides the test exemption.
  'tests/test_web_startup.py', 'tests/test_deployment_startup_contract.py',
  'tests/test_render_*.py',
  // The sole pin on ops/artemis/Dockerfile: USER artemis, no EXPOSE, and the
  // pinned base image and ARTEMIS_REF. The Dockerfile itself stays ungated, so
  // without this one PR could drop `USER artemis`, add EXPOSE, float the base
  // image, and delete the assertions that say otherwise.
  'tests/test_artemis_runtime.py',
];
// Paths that require @larsburgess approval — true security policy, auth/identity
// code, and credential handling. Operational files (Dockerfiles, infra, app
// route tables, start scripts) are intentionally excluded, on the grounds that
// their security-relevant values are pinned by tests. Three things make that
// hold rather than merely sound true. The pinning tests are named in
// CONTROL_PATHS above, so a file and the assertion constraining it cannot both
// move in one ungated change. The render blueprint's pins are stated over the
// whole document (tests/test_render_blueprint_invariants.py) rather than per
// named service, so appending a service does not arrive unpinned — the
// per-service assertions reached only the services they named, which left the
// production broker's ipAllowList, production DEBUG, and any new service
// uncovered. And that file pins what invokes start.sh, not only its contents:
// a web service whose startCommand stopped calling it would have left every
// assertion in tests/test_web_startup.py green while none of the deploy checks
// ran in production.
//
// What the argument still does not reach, and is accepted risk: the root
// Dockerfile and Dockerfile.gcp carry no security pin of their own (only the
// wsgi entrypoint and build command are asserted), .dockerignore has none, and
// no test constrains a service's buildCommand. Bandit and gitleaks cover the
// secret case. The root URLconf is the one path the pinning argument cannot
// cover at all; it is gated above with the rest of its package.
//
// Both dependency manifests are here, because the scanners answer a different
// question than review does. `pip-audit -r requirements.txt` and Dependabot
// alerts both fire on a known advisory against a dependency that is already
// present; neither has anything to say about one being *added* — a new direct
// dependency, or a name one character away from a real one, raises nothing
// from either. That is the reviewable event, and it is the same event in both
// ecosystems, so gating one and not the other would be a distinction with no
// reason behind it.
//
// Every entry below that names a single file is listed as a directory too. An
// exact filename stops covering its own subject the day the module becomes a
// package, and the largest files here — permissions.py at 407 lines,
// middleware.py 322, authentication.py 314 — are likelier to be split than the
// small ones for which this was first written. It applies to the TypeScript
// entries as much as the Python ones; `index.ts` is the same move by another
// name.
const SECURITY_PATHS = [
  ...CONTROL_PATHS,
  'frontend/package*.json', 'requirements*.txt', 'requirements/**',
  'omop_core/authorization.py', 'omop_core/authorization/**',
  'patient_portal/services.py', 'patient_portal/services/**',
  'patient_portal/api/break_glass.py', 'patient_portal/api/break_glass/**',
  'patient_portal/checks.py', 'patient_portal/checks/**',
  'patient_portal/api/authentication.py', 'patient_portal/api/authentication/**',
  'patient_portal/api/permissions.py', 'patient_portal/api/permissions/**',
  'patient_portal/api/middleware.py', 'patient_portal/api/middleware/**',
  'patient_portal/api/providers/**',
  // The seven app URLconfs are deliberately out of scope, and promop/urls.py
  // is not one of them: it is covered by `promop/**` above. The app ones carry
  // no permission decisions — their views do, and DEFAULT_PERMISSION_CLASSES
  // is ['IsAuthenticated'] here, so a DRF route added to one fails closed.
  // That is a convention rather than a guarantee, since a plain Django view
  // added to any of them would have no permission default either; what makes
  // them different is reachability — patient_portal/urls.py is mounted
  // nowhere, and the rest hang off promop/urls.py, which is gated.
  // The settings-module selector that makes DEFAULT_PERMISSION_CLASSES true at
  // all lives in promop/wsgi.py, and is gated there for the same reason.
  //
  // manage.py is the third copy of that selector and the only one outside both
  // packages. start.sh runs `manage.py check --deploy --fail-level ERROR` with
  // no DJANGO_SETTINGS_MODULE in the environment, so this file decides which
  // settings the deploy check reads — and a check that passes against the
  // wrong settings module is the failure the check exists to prevent.
  //
  // start.sh itself stays ungated, and one `export DJANGO_SETTINGS_MODULE=`
  // line in it would override all four selectors from outside this list. Like
  // the other operational files it is left out on the strength of a pin, and
  // that pin had to be written: tests/test_web_startup.py already fails on a
  // `--settings=` appended to any manage.py line, because it asserts the exact
  // argv sequence, and now also asserts the variable is unset in the
  // environment start.sh hands its commands. That test is in CONTROL_PATHS.
  'manage.py',
  // Service principals: the credential definitions and the two commands that
  // issue and import them.
  'patient_portal/service_tokens.py', 'patient_portal/service_tokens/**',
  'patient_portal/service_applications.py', 'patient_portal/service_applications/**',
  'patient_portal/management/commands/*service_token*.py',
  'frontend/src/utils/oauth.ts', 'frontend/src/utils/oauth/**',
  'frontend/src/hooks/useAuth.ts', 'frontend/src/hooks/useAuth/**',
  // The transports that attach (or deliberately omit) a credential — not the
  // data-fetching modules built on top of them. Directory forms for the same
  // reason as the Python ones: `axios.ts` becoming `axios/index.ts` plus
  // `axios/interceptors.ts` is an ordinary move, and it would take the
  // credential-attaching transport out of scope on its way past.
  'frontend/src/api/axios.ts', 'frontend/src/api/axios/**',
  'frontend/src/api/publicAxios.ts', 'frontend/src/api/publicAxios/**',
  'frontend/src/api/clinicalTransport.ts', 'frontend/src/api/clinicalTransport/**',
  'frontend/src/federation/assertLabsTokens.ts',
  'frontend/src/federation/assertLabsTokens/**',
  'frontend/src/components/Auth/**',
  'docs/*security*.md',
];
const TEST_PATHS = ['tests/**', '**/tests/**', '**/tests.py', '**/test_*.py', '**/*.test.*', '**/*.itest.*'];

function matches(path, pattern) {
  const escaped = pattern.replace(/[.+?^${}()|[\]\\]/g, '\\$&');
  const regex = escaped.replace(/\*\*\//g, '\u0001').replace(/\*\*/g, '\u0002').replace(/\*/g, '[^/]*')
    .replace(/\u0001/g, '(?:.*/)?').replace(/\u0002/g, '.*');
  return new RegExp(`^${regex}$`).test(path);
}

function securityPath(path) {
  // Trim first: an unanchored match on a path carrying stray whitespace would
  // silently classify a gated file as exempt.
  const candidate = String(path).trim();
  if (CONTROL_PATHS.some(pattern => matches(candidate, pattern))) return true;
  if (TEST_PATHS.some(pattern => matches(candidate, pattern))) return false;
  return SECURITY_PATHS.some(pattern => matches(candidate, pattern));
}

function securityFiles(files) {
  return files.some(file => [file.filename, file.previous_filename].filter(Boolean).some(securityPath));
}

function securityLabels(labels = []) {
  return labels.some(label => SECURITY_LABEL.test(typeof label === 'string' ? label : label.name));
}

function issueReferences(pr, repository) {
  const text = `${pr.title || ''}\n${pr.body || ''}`;
  const refs = new Map();
  const add = (repo, number) => {
    // Key on the parsed number: '#12' and '#00012' are the same issue, and a
    // '#0' reference can only produce a guaranteed 404.
    const issue = Number(number);
    if (!Number.isInteger(issue) || issue <= 0) return;
    refs.set(`${repo}#${issue}`, { repository: repo, number: issue });
  };
  for (const match of text.matchAll(/https:\/\/github\.com\/([\w.-]+\/[\w.-]+)\/issues\/(\d+)/g)) add(match[1], match[2]);
  for (const match of text.matchAll(/(?:^|[^\w/])(?:(\w[\w.-]*\/[\w.-]+))?#(\d+)\b/g)) add(match[1] || repository, match[2]);
  if (refs.size > MAX_ISSUE_REFERENCES) {
    throw new Error(`More than ${MAX_ISSUE_REFERENCES} linked issues to verify safely`);
  }
  return [...refs.values()];
}

// Security changes need one approval from a writer other than the PR's author. The
// approval covers the whole PR, so commits pushed after it (a reviewer's own fix
// included) do not void it. Dismissing it, or an outstanding request for changes
// from another writer, does.
function reviewDecision(pr, reviews, allowedReviewers) {
  const latest = new Map();
  // A deleted account leaves review.user null; such a review can never satisfy
  // the gate, and reading through it would crash the evaluation instead.
  for (const review of [...reviews].filter(review => review.user?.login).sort((a, b) => a.id - b.id)) {
    if (['APPROVED', 'CHANGES_REQUESTED', 'DISMISSED'].includes(review.state)) latest.set(review.user.login, review);
  }
  const eligible = [...latest.values()].filter(review => allowedReviewers.has(review.user.login)
    && review.user.id !== pr.user.id && review.user.login !== pr.user.login && review.user.type !== 'Bot');
  if (eligible.some(review => review.state === 'CHANGES_REQUESTED')) return false;
  return eligible.some(review => review.state === 'APPROVED');
}

async function evaluate(github, owner, repo, pr) {
  const reasons = [];
  if (pr.changed_files > 3000) throw new Error('Too many changed files to verify safely');
  const files = await github.paginate(github.rest.pulls.listFiles, { owner, repo, pull_number: pr.number, per_page: 100 });
  if (securityFiles(files)) reasons.push('security-sensitive files');
  if (securityLabels(pr.labels)) reasons.push('security label on PR');
  const linked = await github.graphql(`query($owner:String!, $repo:String!, $number:Int!) {
    repository(owner:$owner,name:$repo) { pullRequest(number:$number) {
      closingIssuesReferences(first:100) { nodes { number repository { nameWithOwner } labels(first:100) { nodes { name } pageInfo { hasNextPage } } } pageInfo { hasNextPage } }
    } }
  }`, { owner, repo, number: pr.number });
  const closing = linked.repository.pullRequest.closingIssuesReferences;
  if (closing.pageInfo.hasNextPage) throw new Error('Too many closing issues to verify safely');
  const verified = new Set();
  for (const issue of closing.nodes) {
    if (issue.labels.pageInfo.hasNextPage) throw new Error('Too many issue labels to verify safely');
    verified.add(`${issue.repository.nameWithOwner}#${issue.number}`);
    if (securityLabels(issue.labels.nodes)) reasons.push(`security issue ${issue.repository.nameWithOwner}#${issue.number}`);
  }
  for (const ref of issueReferences(pr, `${owner}/${repo}`)) {
    if (verified.has(`${ref.repository}#${ref.number}`)) continue;
    const [issueOwner, issueRepo] = ref.repository.split('/');
    let issue;
    try {
      ({ data: issue } = await github.rest.issues.get({ owner: issueOwner, repo: issueRepo, issue_number: ref.number }));
    } catch (error) {
      // `#4a90e2` in a body parses as a reference. A number that resolves to
      // nothing carries no label, so it cannot make the PR security-scoped —
      // but throwing here would pin the PR at `failure` permanently.
      if (error.status === 404) continue;
      throw error;
    }
    if (securityLabels(issue.labels)) reasons.push(`security issue ${ref.repository}#${ref.number}`);
  }
  if (!reasons.length) return { state: 'success', description: 'No security-sensitive files or security-labelled PR/issues' };
  const reviews = await github.paginate(github.rest.pulls.listReviews, { owner, repo, pull_number: pr.number, per_page: 100 });
  const allowed = new Set();
  for (const login of new Set(reviews.map(review => review.user?.login).filter(Boolean))) {
    const { data } = await github.rest.repos.getCollaboratorPermissionLevel({ owner, repo, username: login });
    if (['admin', 'maintain', 'write'].includes(data.permission) || data.user?.permissions?.push) allowed.add(login);
  }
  return reviewDecision(pr, reviews, allowed)
    ? { state: 'success', description: 'Approved by a writer other than the author' }
    : { state: 'failure', description: 'Security change requires approval from a writer other than the author' };
}

async function run({ github, context, core }) {
  const { owner, repo } = context.repo;
  const groups = new Map();
  try {
    const prs = await github.paginate(github.rest.pulls.list, { owner, repo, state: 'open', per_page: 100 });
    for (const pr of prs.filter(pr => ['dev', 'main'].includes(pr.base.ref))) {
      if (!groups.has(pr.head.sha)) groups.set(pr.head.sha, []);
      groups.get(pr.head.sha).push(pr);
    }
  } catch (error) {
    // No commit is known yet, so no status can be written. Fail the run itself
    // rather than returning quietly and leaving every existing status stale.
    core.setFailed(`Unable to list open pull requests: ${error.message}`);
    return;
  }
  const unwritten = [];
  for (const [sha, group] of groups) {
    // Writing `pending` plus a final state on every scheduled run burns through
    // GitHub's 1000-statuses-per-SHA-and-context limit in about a day, and the
    // 422 that follows would abort the whole run. Read the current status once
    // and write only when it actually changes.
    let latest;
    const currentStatus = async () => {
      if (latest === undefined) {
        const { data } = await github.rest.repos.listCommitStatusesForRef({ owner, repo, ref: sha, per_page: 100 });
        latest = data.find(status => status.context === CONTEXT) || null;
      }
      return latest;
    };
    const publish = async (state, description, { onlyIfMissing = false } = {}) => {
      const current = await currentStatus();
      if (onlyIfMissing && current) return;
      if (current && current.state === state && current.description === description) return;
      await github.rest.repos.createCommitStatus({
        owner, repo, sha, context: CONTEXT, state, description,
        target_url: `${context.serverUrl}/${owner}/${repo}/actions/runs/${context.runId}`,
      });
      latest = { context: CONTEXT, state, description };
    };
    try {
      // An event means something that feeds the verdict may just have changed
      // (a label, a review, the PR body), so the old verdict is marked pending
      // while it is rechecked. The five-minute reconciliation changes nothing by
      // itself, so it only needs `pending` on a commit that has no status yet.
      await publish('pending', 'Checking security scope and required approval',
        { onlyIfMissing: context.eventName === 'schedule' });
      const results = [];
      for (const pr of group) {
        const { data: snapshot } = await github.rest.pulls.get({ owner, repo, pull_number: pr.number });
        const result = await evaluate(github, owner, repo, snapshot);
        const { data: current } = await github.rest.pulls.get({ owner, repo, pull_number: pr.number });
        if (current.head.sha !== sha || current.updated_at !== snapshot.updated_at) throw new Error('PR changed during evaluation; awaiting the next check');
        results.push(result);
      }
      const failed = results.find(result => result.state !== 'success');
      await publish(failed ? 'failure' : 'success', failed?.description || results[0].description);
    } catch (error) {
      core.warning(`Security review for ${sha}: ${error.message}`);
      try {
        await publish('failure', 'Unable to verify security review; inspect workflow logs');
      } catch (statusError) {
        // A failed status write must not skip the remaining commits — but it
        // leaves this commit carrying whatever verdict it already had, which
        // may now be wrong. Keep going, and fail the run so the gap is visible.
        core.warning(`Security review status for ${sha}: ${statusError.message}`);
        unwritten.push(sha);
      }
    }
  }
  if (unwritten.length) {
    core.setFailed(`Could not publish a security review status for: ${unwritten.join(', ')}`);
  }
}

module.exports = { securityPath, securityFiles, securityLabels, issueReferences, reviewDecision, evaluate, run };
