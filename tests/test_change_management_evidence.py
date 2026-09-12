"""Evidence must not claim enforcement when a bypass or required gate is missing."""

from copy import deepcopy

from scripts.capture_change_management_evidence import evaluate_branch


RULESET = {
    'id': 1,
    'enforcement': 'active',
    'bypass_actors': [],
    'rules': [
        {'type': 'pull_request', 'parameters': {
            'required_approving_review_count': 1,
            'require_code_owner_review': True,
        }},
        {'type': 'required_status_checks', 'parameters': {
            'required_status_checks': [{'context': name} for name in (
                'Backend tests', 'Frontend lint & build', 'Security gates',
            )],
        }},
    ],
}


def assess(ruleset):
    effective = [{**rule, 'ruleset_id': ruleset['id']} for rule in ruleset['rules']]
    return evaluate_branch(effective, [ruleset])


def test_required_review_ci_and_no_direct_pushes_are_reported():
    result = assess(RULESET)
    assert result['independent_review_enforced']
    assert result['required_ci_enforced']
    assert result['direct_pushes_blocked']


def test_admin_always_bypass_is_not_reported_as_enforcement():
    ruleset = deepcopy(RULESET)
    ruleset['bypass_actors'] = [{'actor_type': 'RepositoryRole', 'actor_id': 5, 'bypass_mode': 'always'}]
    result = assess(ruleset)
    assert not result['independent_review_enforced']
    assert not result['required_ci_enforced']
    assert not result['direct_pushes_blocked']


def test_pr_only_bypass_blocks_direct_push_but_can_skip_review_and_ci():
    ruleset = deepcopy(RULESET)
    ruleset['bypass_actors'] = [{'actor_type': 'RepositoryRole', 'actor_id': 5, 'bypass_mode': 'pull_request'}]
    result = assess(ruleset)
    assert result['direct_pushes_blocked']
    assert not result['independent_review_enforced']
    assert not result['required_ci_enforced']


def test_zero_approvals_does_not_count_as_required_review():
    ruleset = deepcopy(RULESET)
    ruleset['rules'][0]['parameters']['required_approving_review_count'] = 0
    assert not assess(ruleset)['independent_review_enforced']


def test_missing_security_gate_is_reported():
    ruleset = deepcopy(RULESET)
    ruleset['rules'][1]['parameters']['required_status_checks'].pop()
    assert not assess(ruleset)['required_ci_enforced']


def test_uncaptured_inherited_rule_sources_fail_closed():
    effective = [{**rule, 'ruleset_id': 99} for rule in RULESET['rules']]
    result = evaluate_branch(effective, [RULESET])
    assert not result['all_ruleset_sources_captured']
    assert not result['independent_review_enforced']
    assert not result['required_ci_enforced']
    assert not result['direct_pushes_blocked']


def test_disabled_rule_does_not_count_as_enforced():
    ruleset = deepcopy(RULESET)
    ruleset['enforcement'] = 'disabled'
    result = assess(ruleset)
    assert not result['independent_review_enforced']
    assert not result['required_ci_enforced']
    assert not result['direct_pushes_blocked']


def test_collector_combines_paginated_arrays_without_new_gh_flags(monkeypatch):
    from types import SimpleNamespace
    from scripts.capture_change_management_evidence import github_json

    def run(command, **kwargs):
        assert '--paginate' in command
        assert '--slurp' not in command
        return SimpleNamespace(returncode=0, stdout='[{"id": 1}]\n[{"id": 2}]', stderr='')

    monkeypatch.setattr('scripts.capture_change_management_evidence.subprocess.run', run)
    assert github_json('repos/example/repository/rulesets') == [{'id': 1}, {'id': 2}]


def test_collector_retains_all_paginated_environments(monkeypatch):
    from types import SimpleNamespace
    from scripts.capture_change_management_evidence import github_json
    monkeypatch.setattr(
        'scripts.capture_change_management_evidence.subprocess.run',
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stderr='', stdout=(
            '{"total_count": 2, "environments": [{"name": "staging"}]}\n'
            '{"total_count": 2, "environments": [{"name": "production"}]}'
        )),
    )
    assert github_json('repos/example/repository/environments')['environments'] == [
        {'name': 'staging'}, {'name': 'production'},
    ]
