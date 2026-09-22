// A label is security-scoped if the word appears anywhere in it: 'security',
// 'Security/high', 'P0-security' and 'needs-security-review' all count. Wider
// than needed is the safe direction — it can only ask for another approval.
const SECURITY_LABEL = /security/i;
const CONTEXT = 'Security review';
const REQUIRED_REVIEWER = { login: 'larsburgess', id: 23724 };
// Referenced issues are fetched one API call each, on every scheduled run, from
// text the PR author controls. Cap the fan-out well below the hourly budget.
const MAX_ISSUE_REFERENCES = 20;
// Paths the test-file exemption must never reach: the control plane — CI, the
// policy that gates it, the scanners it runs, the change-management evidence —
// and, in the Django project packages, the modules that decide who may
// authenticate. A file here executes with the control plane's privileges
// whatever it is named (`.github/workflows/*.test.yml` is a workflow Actions
// will run; `.github/scripts/*.test.cjs` was executed by this policy's own
// workflow), and a `test_settings.py` is still a settings module.
//
// These are globs, not exact filenames: the matcher has no implicit recursion,
// so `promop/settings.py` alone stops covering the settings module the day it
// becomes `promop/settings/base.py` — the ordinary refactor at its current
// size — and covers neither `ctomop/settings_staging.py` nor a
// `settings_prod.py`. The same applies to the two modules below, which is why
// each is listed as both a file and a package. The infix form also keeps
// `test_settings.py` in scope, which is the point of listing settings here
// rather than below the test exemption. A file under a directory named
// `tests/` stays exempt at any depth.
const CONTROL_PATHS = [
  '.github/**', '**/CODEOWNERS', 'docs/soc2/**', '.bandit*', '.gitleaks*',
  'SECURITY.md', 'scripts/capture_change_management_evidence.py',
  'scripts/capture_change_management_evidence/**',
  'promop/**/*settings*', 'promop/settings/**', 'ctomop/**/*settings*', 'ctomop/settings/**',
  // OAUTH2_VALIDATOR_CLASS: refuses grant, response type and bearer token for
  // retired browser clients. Its only test is under tests/, which this list
  // exempts, so ungating it would let one change weaken the validator and
  // relax the test that proves it.
  'promop/oauth.py', 'promop/oauth/**', 'ctomop/oauth.py', 'ctomop/oauth/**',
  // The Sentry scrubber: the secret denylist and include_local_variables.
  'promop/sentry.py', 'promop/sentry/**', 'ctomop/sentry.py', 'ctomop/sentry/**',
];
// Paths that require @larsburgess approval — true security policy, auth/identity
// code, and credential handling. Operational files (Dockerfiles, infra, route
// tables, start scripts) are intentionally excluded: their security-relevant
// values are pinned by tests, and bandit and gitleaks cover the secret case.
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
  // The project route table, and only it. Of the other seven urls.py modules,
  // the DRF ones carry no permission decisions — their views do, and DRF
  // defaults to IsAuthenticated here, so a DRF route added to one fails closed.
  // That is a convention rather than a guarantee: a plain Django view added to
  // any of them would have no permission default either. What makes those
  // seven different is reachability — patient_portal/urls.py is mounted
  // nowhere, and the rest hang off the file below. promop/urls.py is the
  // exception on its own terms: it mounts admin/ and the OAuth2 provider tree,
  // carries the only permission_classes=[AllowAny] of any urls.py in the tree,
  // and ends in a catch-all, so a plain Django view added here has no
  // permission default at all. ctomop/urls.py is gated for a different reason
  // than its content, which is five lines aliasing this module: repointing that
  // alias moves every route in the project.
  'promop/urls.py', 'promop/urls/**', 'ctomop/urls.py', 'ctomop/urls/**',
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

function reviewDecision(pr, reviews, allowedReviewers) {
  const latest = new Map();
  // A deleted account leaves review.user null; such a review can never satisfy
  // the gate, and reading through it would crash the evaluation instead.
  for (const review of [...reviews].filter(review => review.user?.login).sort((a, b) => a.id - b.id)) {
    if (['APPROVED', 'CHANGES_REQUESTED', 'DISMISSED'].includes(review.state)) latest.set(review.user.login, review);
  }
  const eligible = [...latest.values()].filter(review => allowedReviewers.has(review.user.login) && review.user.login !== pr.user.login && review.user.type !== 'Bot');
  if (eligible.some(review => review.state === 'CHANGES_REQUESTED')) return false;
  return eligible.some(review => review.user.id === REQUIRED_REVIEWER.id
    && review.user.login.toLowerCase() === REQUIRED_REVIEWER.login
    && review.state === 'APPROVED' && review.commit_id === pr.head.sha);
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
    ? { state: 'success', description: 'Approval from @larsburgess covers the current commit' }
    : { state: 'failure', description: 'Security change requires @larsburgess approval of the current commit' };
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
      await publish('pending', 'Checking security scope and @larsburgess approval',
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
