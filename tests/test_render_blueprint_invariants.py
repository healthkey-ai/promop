"""Blueprint-wide invariants, so a new service cannot arrive unpinned.

The security-review gate leaves render.yaml out of scope on the grounds that its
security-relevant values are pinned by tests. The per-service assertions in
tests/test_render_staging_blueprint.py only reach the services they name, so the
production broker could be opened to the world, production DEBUG flipped, or an
entire wildcard web service appended, with every test still green. These are
stated over the whole document — every service, every database, and the
top-level envVarGroups a service can inherit instead of declaring its own —
so they hold for services that do not exist yet.
"""
from pathlib import Path

import pytest
import yaml

from patient_portal.api.permissions import _WRITE_SCOPES

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope='module')
def blueprint():
    return yaml.safe_load((ROOT / 'render.yaml').read_text())


def environments(blueprint):
    """Every (owner, key, entry) the blueprint can put into a process.

    A service may declare envVars inline or inherit them from a top-level
    envVarGroup by `- fromGroup: <name>`, which carries no `key` of its own.
    Reading only the inline entries would let one PR move every value below
    into a group and pass all of this.
    """
    for service in blueprint.get('services', []):
        for entry in service.get('envVars', []):
            if 'key' in entry:
                yield service.get('name', '<unnamed service>'), entry['key'], entry
    for group in blueprint.get('envVarGroups', []):
        for entry in group.get('envVars', []):
            if 'key' in entry:
                yield f"envVarGroup {group.get('name', '<unnamed>')}", entry['key'], entry


def test_no_service_or_group_in_any_environment_enables_debug(blueprint):
    for owner, key, entry in environments(blueprint):
        if key != 'DEBUG':
            continue
        # Deliberately stricter than the wildcard check below: DEBUG has no
        # legitimate reason to be dashboard-supplied, so a value-less entry
        # fails here rather than being skipped.
        assert entry.get('value') in ('False', 'false'), f'{owner}.DEBUG = {entry.get("value")!r}'


def test_every_broker_stays_closed_to_the_public_internet(blueprint):
    brokers = [s for s in blueprint.get('services', []) if s['type'] in ('redis', 'keyvalue')]
    assert brokers, 'expected at least one broker to constrain'
    for broker in brokers:
        # Present and empty: an omitted key would leave the allow list to
        # whatever the dashboard holds, which this file cannot see.
        assert broker.get('ipAllowList') == [], broker.get('name')


def test_no_database_declares_a_non_empty_allow_list(blueprint):
    # Weaker than the broker rule on purpose: promop-db declares no ipAllowList
    # at all today, so requiring one here would assert a state this repository
    # does not have. Adding the key is a production change and belongs in its
    # own PR; this catches the blueprint growing an open list in the meantime.
    for database in blueprint.get('databases', []):
        declared = database.get('ipAllowList')
        assert declared in (None, []), f'{database.get("name")}: {declared!r}'


def test_nothing_declares_a_host_or_origin_wildcard(blueprint):
    for owner, key, entry in environments(blueprint):
        if key not in ('ALLOWED_HOSTS', 'CORS_ALLOWED_ORIGINS') or 'value' not in entry:
            continue  # sync: false — supplied in the dashboard, not here.
        assert '*' not in str(entry['value']), f'{owner}.{key}'


def test_nothing_grants_a_resource_wide_write_scope(blueprint):
    # _WRITE_SCOPES, taken from the permission class rather than copied, so a
    # scope added there does not arrive unguarded here. Deliberately NOT the
    # whole of "authorizes writes": _ETL_WRITE_SCOPE ('system/etl.write') also
    # permits writes on approved endpoints and is already granted to staging,
    # so it cannot be denied here. This rule covers the resource-wide SMART
    # write scopes only; the ETL capability is pinned by
    # tests/test_render_staging_blueprint.py against the service that has it.
    for owner, key, entry in environments(blueprint):
        if key != 'SERVICE_AUTH_SCOPES':
            continue
        granted = set(str(entry.get('value', '')).split())
        assert not granted & _WRITE_SCOPES, f'{owner}: {sorted(granted & _WRITE_SCOPES)}'


def test_every_web_service_starts_through_the_deploy_check_entrypoint(blueprint):
    """start.sh's contents are pinned; what invokes it was not.

    tests/test_web_startup.py asserts the exact command sequence inside
    start.sh, including `manage.py check --deploy --fail-level ERROR`. That
    says nothing about whether the deployment still runs it: replacing a web
    service's startCommand with a bare gunicorn line leaves every one of those
    assertions green while none of the checks run in production. Workers are
    excluded deliberately — the production worker invokes celery directly.
    """
    web = [s for s in blueprint.get('services', []) if s['type'] == 'web']
    assert web, 'expected at least one web service to constrain'
    for service in web:
        command = service.get('startCommand', '')
        # The last command in the chain is the one the container goes on
        # running, so `chmod +x start.sh && ./start.sh` passes while
        # `echo start.sh && gunicorn ...` — which mentions it without running
        # it — does not. Substring matching would accept both.
        final = command.split('&&')[-1].strip().strip('"\'').split()
        assert final and final[0] in ('./start.sh', 'sh', 'bash') and (
            final[0] == './start.sh' or (len(final) > 1 and final[1].endswith('start.sh'))
        ), f'{service.get("name")}: {command!r}'
