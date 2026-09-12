#!/usr/bin/env python3
"""Capture GitHub CC8.1 settings without reading secrets or service credentials."""

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess


REQUIRED_CHECKS = {'Backend tests', 'Frontend lint & build', 'Security gates'}


def github_json(endpoint):
    result = subprocess.run(
        ['gh', 'api', '--paginate', endpoint],
        capture_output=True, text=True,
    )
    if result.returncode:
        raise RuntimeError(f'GitHub API read failed for {endpoint}: {result.stderr.strip()}')
    # Older gh versions emit adjacent JSON documents for --paginate and have
    # no --slurp option. Decode each complete response without a shell pipeline.
    decoder = json.JSONDecoder()
    remaining = result.stdout.strip()
    pages = []
    while remaining:
        page, end = decoder.raw_decode(remaining)
        pages.append(page)
        remaining = remaining[end:].strip()
    if pages and isinstance(pages[0], list):
        return [item for page in pages for item in page]
    if len(pages) > 1:
        for collection in ('environments', 'workflows'):
            if all(collection in page for page in pages):
                return {**pages[0], collection: [item for page in pages for item in page[collection]]}
    if len(pages) != 1:
        raise ValueError(f'Expected one object response from {endpoint}')
    return pages[0]


def evaluate_branch(effective_rules, rulesets):
    pr_rules = [rule['parameters'] for rule in effective_rules if rule['type'] == 'pull_request']
    required_checks = {
        check['context']
        for rule in effective_rules if rule['type'] == 'required_status_checks'
        for check in rule['parameters']['required_status_checks']
    }
    source_ids = {rule['ruleset_id'] for rule in effective_rules}
    sources = [ruleset for ruleset in rulesets if ruleset['id'] in source_ids]
    # A flattened effective-rules response omits bypass actors. Inspect every
    # contributing ruleset before treating the requirements as non-bypassable.
    all_sources_captured = source_ids == {ruleset['id'] for ruleset in sources}
    review_enforced = any(
        rule['parameters']['required_approving_review_count'] >= 1
        for source in sources if source['enforcement'] == 'active' and not source['bypass_actors']
        for rule in source['rules'] if rule['type'] == 'pull_request'
    )
    direct_push_blocked = any(
        source['enforcement'] == 'active'
        and not any(actor['bypass_mode'] == 'always' for actor in source['bypass_actors'])
        and any(rule['type'] == 'pull_request' for rule in source['rules'])
        for source in sources
    )
    ci_enforced = {
        check['context']
        for source in sources if source['enforcement'] == 'active' and not source['bypass_actors']
        for rule in source['rules'] if rule['type'] == 'required_status_checks'
        for check in rule['parameters']['required_status_checks']
    }
    return {
        'required_approvals': max((rule['required_approving_review_count'] for rule in pr_rules), default=0),
        'code_owner_review_required': any(rule['require_code_owner_review'] for rule in pr_rules),
        'required_checks': sorted(required_checks),
        'all_ruleset_sources_captured': all_sources_captured,
        'independent_review_enforced': all_sources_captured and review_enforced,
        'required_ci_enforced': all_sources_captured and REQUIRED_CHECKS <= ci_enforced,
        'direct_pushes_blocked': all_sources_captured and direct_push_blocked,
    }


def capture(repository):
    prefix = f'repos/{repository}'
    repo = github_json(prefix)
    summaries = github_json(f'{prefix}/rulesets?includes_parents=true')
    rulesets = []
    for summary in summaries:
        # Follow GitHub's source link so inherited organization rules are
        # captured too. A failed read must fail the capture, not omit a rule.
        rulesets.append(github_json(summary['_links']['self']['href']))
    branches = {}
    for branch in ('dev', 'main'):
        effective = github_json(f'{prefix}/rules/branches/{branch}')
        head = github_json(f'{prefix}/branches/{branch}')['commit']['sha']
        tree = github_json(f'{prefix}/git/trees/{head}?recursive=1')
        if tree.get('truncated'):
            raise ValueError(f'Cannot establish CODEOWNERS from truncated {branch} tree')
        blobs = {entry['path']: entry['sha'] for entry in tree['tree'] if entry['type'] == 'blob'}
        owner_path = next((path for path in ('.github/CODEOWNERS', 'CODEOWNERS', 'docs/CODEOWNERS') if path in blobs), None)
        branches[branch] = {
            'head': head,
            'codeowners': {'path': owner_path, 'blob_sha': blobs[owner_path]} if owner_path else None,
            'effective_rules': effective,
            'codeowners_errors': github_json(f'{prefix}/codeowners/errors?ref={head}')['errors'],
            'assessment': evaluate_branch(effective, rulesets),
        }
    environments = github_json(f'{prefix}/environments?per_page=100')
    workflows = github_json(f'{prefix}/actions/workflows?per_page=100')
    return {
        'captured_at': datetime.now(timezone.utc).isoformat(),
        'repository': repository,
        'default_branch': repo['default_branch'],
        'rulesets': rulesets,
        'branches': branches,
        'environments': environments,
        'workflows': workflows,
        'scope': {
            'github_settings': 'Observed through authenticated read-only GitHub API calls.',
            'render_settings': 'Not captured. GitHub environments do not prove Render approval gates.',
            'historical_operation': 'Not established by this configuration snapshot; retain PR and deployment records.',
        },
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo', default='healthkey-ai/promop')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--require-enforced', action='store_true')
    args = parser.parse_args()
    evidence = capture(args.repo)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(evidence, indent=2, sort_keys=True) + '\n')
    failures = []
    for branch, data in evidence['branches'].items():
        for control in ('independent_review_enforced', 'required_ci_enforced', 'direct_pushes_blocked'):
            if not data['assessment'][control]:
                failures.append(f'{branch}: {control}')
        if not data['codeowners']:
            failures.append(f'{branch}: CODEOWNERS missing')
        if data['codeowners_errors']:
            failures.append(f'{branch}: CODEOWNERS validation errors')
    print(f'Captured {args.repo} settings to {args.output}')
    for failure in failures:
        print(f'NOT ENFORCED: {failure}')
    if args.require_enforced and failures:
        raise SystemExit(1)


if __name__ == '__main__':
    main()
