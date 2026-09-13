const SECURITY_LABEL = /^security(?:$|[- :])/i;
const CONTEXT = 'Security review';
const REQUIRED_REVIEWER = { login: 'larsburgess', id: 23724 };
const SECURITY_PATHS = [
  'omop_core/authorization.py', 'patient_portal/services.py',
  'patient_portal/api/break_glass.py', 'patient_portal/checks.py', 'start.sh',
  '**/CODEOWNERS', 'docs/soc2/**', 'scripts/capture_change_management_evidence.py',
  'ctomop/settings.py', 'ctomop/oauth.py', 'ctomop/security*.py',
  'patient_portal/api/authentication.py', 'patient_portal/api/permissions.py',
  'patient_portal/api/middleware.py', 'patient_portal/api/providers/**',
  'frontend/src/utils/oauth.ts', 'frontend/src/api/axios.ts',
  'frontend/src/hooks/useAuth.ts', 'frontend/src/components/Auth/**',
  '.github/**', '.bandit*', '.gitleaks*', 'SECURITY.md', 'docs/security*.md',
  'requirements*.txt', 'frontend/package*.json', '.env*',
  'Dockerfile*', 'docker-compose*.yml', 'render.yaml',
];
const TEST_PATHS = ['tests/**', '**/tests/**', '**/tests.py', '**/test_*.py', '**/*.test.*', '**/*.itest.*'];

function matches(path, pattern) {
  const escaped = pattern.replace(/[.+?^${}()|[\]\\]/g, '\\$&');
  const regex = escaped.replace(/\*\*\//g, '\u0001').replace(/\*\*/g, '\u0002').replace(/\*/g, '[^/]*')
    .replace(/\u0001/g, '(?:.*/)?').replace(/\u0002/g, '.*');
  return new RegExp(`^${regex}$`).test(path);
}

function securityFiles(files) {
  return files.some(file => [file.filename, file.previous_filename].filter(Boolean).some(path =>
    !TEST_PATHS.some(pattern => matches(path, pattern)) && SECURITY_PATHS.some(pattern => matches(path, pattern))));
}

function securityLabels(labels = []) {
  return labels.some(label => SECURITY_LABEL.test(typeof label === 'string' ? label : label.name));
}

function issueReferences(pr, repository) {
  const text = `${pr.title || ''}\n${pr.body || ''}`;
  const refs = new Map();
  const add = (repo, number) => refs.set(`${repo}#${number}`, { repository: repo, number: Number(number) });
  for (const match of text.matchAll(/https:\/\/github\.com\/([\w.-]+\/[\w.-]+)\/issues\/(\d+)/g)) add(match[1], match[2]);
  for (const match of text.matchAll(/(?:^|[^\w/])(?:(\w[\w.-]*\/[\w.-]+))?#(\d+)\b/g)) add(match[1] || repository, match[2]);
  if (refs.size > 100) throw new Error('Too many linked issues to verify safely');
  return [...refs.values()];
}

function reviewDecision(pr, reviews, allowedReviewers) {
  const latest = new Map();
  for (const review of [...reviews].sort((a, b) => a.id - b.id)) {
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
    const { data: issue } = await github.rest.issues.get({ owner: issueOwner, repo: issueRepo, issue_number: ref.number });
    if (securityLabels(issue.labels)) reasons.push(`security issue ${ref.repository}#${ref.number}`);
  }
  if (!reasons.length) return { state: 'success', description: 'No security-sensitive files or security-labelled PR/issues' };
  const reviews = await github.paginate(github.rest.pulls.listReviews, { owner, repo, pull_number: pr.number, per_page: 100 });
  const allowed = new Set();
  for (const login of new Set(reviews.map(review => review.user.login))) {
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
  const prs = await github.paginate(github.rest.pulls.list, { owner, repo, state: 'open', per_page: 100 });
  for (const pr of prs.filter(pr => ['dev', 'main'].includes(pr.base.ref))) {
    if (!groups.has(pr.head.sha)) groups.set(pr.head.sha, []);
    groups.get(pr.head.sha).push(pr);
  }
  for (const [sha, group] of groups) {
    const status = async (state, description) => github.rest.repos.createCommitStatus({
      owner, repo, sha, context: CONTEXT, state, description,
      target_url: `${context.serverUrl}/${owner}/${repo}/actions/runs/${context.runId}`,
    });
    await status('pending', 'Checking security scope and @larsburgess approval');
    try {
      const results = [];
      for (const pr of group) {
        const { data: snapshot } = await github.rest.pulls.get({ owner, repo, pull_number: pr.number });
        const result = await evaluate(github, owner, repo, snapshot);
        const { data: current } = await github.rest.pulls.get({ owner, repo, pull_number: pr.number });
        if (current.head.sha !== sha || current.updated_at !== snapshot.updated_at) throw new Error('PR changed during evaluation; awaiting the next check');
        results.push(result);
      }
      const failed = results.find(result => result.state !== 'success');
      await status(failed ? 'failure' : 'success', failed?.description || results[0].description);
    } catch (error) {
      await status('failure', 'Unable to verify security review; inspect workflow logs');
      core.warning(`Security review for ${sha}: ${error.message}`);
    }
  }
}

module.exports = { securityFiles, securityLabels, issueReferences, reviewDecision, evaluate, run };
