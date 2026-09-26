"""Encryption-at-rest evidence must name every PHI store and never claim coverage it lacks."""
import io
import json

import pytest
from django.core.management import call_command
from django.core.files.storage import Storage
from django.core.management.base import CommandError

from patient_portal.management.commands import capture_encryption_evidence as cmd

pytestmark = pytest.mark.django_db


class RemoteStorage(Storage):
    """Stands in for S3-style storage: no local filesystem location."""

BROKER = 'redis://red-abc123:s3cret-password@red-abc123:6379'


def capture(*args):
    out, err = io.StringIO(), io.StringIO()
    call_command('capture_encryption_evidence', *args, stdout=out, stderr=err)
    return json.loads(out.getvalue()), err.getvalue()


class FakeRender:
    def __init__(self, responses):
        self.responses = responses
        self.paths = []

    def get(self, path):
        self.paths.append(path)
        if path not in self.responses:
            raise CommandError(f'Render API read failed for {path}: HTTP 404')
        return self.responses[path]


def render_responses(recovery='AVAILABLE', disk=None, kv_owner='tea-1'):
    return {
        '/services/srv-1': {'id': 'srv-1', 'name': 'promop', 'type': 'web_service',
                            'ownerId': 'tea-1',
                            'serviceDetails': {'region': 'oregon', 'plan': 'standard',
                                               'disk': disk}},
        '/postgres/dpg-abc123': {'id': 'dpg-abc123', 'plan': 'pro_4gb', 'region': 'oregon',
                                 'owner': {'id': 'tea-1'}, 'ipAllowList': [{}]},
        '/postgres/dpg-abc123/recovery': {'recoveryStatus': recovery},
        '/key-value/red-abc123': {'id': 'red-abc123', 'plan': 'starter',
                                  'owner': {'id': kv_owner}, 'options': {}},
        '/owners/tea-1': {'id': 'tea-1', 'name': 'HealthKey', 'type': 'team',
                          'email': 'not-captured@example.test'},
        '/owners/tea-2': {'id': 'tea-2', 'name': 'Other', 'type': 'team'},
    }


@pytest.mark.parametrize('url,kind,expected', [
    ('//dpg-abc123-a', 'postgres', 'dpg-abc123'),
    ('//dpg-abc123-a.oregon-postgres.render.com', 'postgres', 'dpg-abc123'),
    ('//localhost', 'postgres', None),
    ('redis://red-abc123:6379', 'key_value', 'red-abc123'),
    ('rediss://red-abc123:pw@oregon-keyvalue.render.com:6379', 'key_value', 'red-abc123'),
    ('redis://cache.internal:6379', 'key_value', None),
    ('', 'key_value', None),
])
def test_render_resource_id_is_derived_from_the_url(url, kind, expected):
    assert cmd.render_resource_id(url, kind) == expected


def test_every_phi_store_is_reported_without_credentials(settings):
    settings.CELERY_BROKER_URL = BROKER
    settings.CELERY_RESULT_BACKEND = BROKER
    evidence, _ = capture()
    stores = {s['store']: s for s in evidence['stores']}
    assert set(stores) == {'database', 'files', 'key_value'}
    assert stores['database']['engine'] == 'postgresql'
    assert stores['database']['server_version']
    assert stores['files']['backend'] == 'django.core.files.storage.filesystem.FileSystemStorage'
    assert stores['key_value']['render_key_value_ids'] == ['red-abc123']
    body = json.dumps(evidence)
    assert 's3cret-password' not in body
    host = settings.DATABASES['default'].get('HOST')
    assert not host or host not in body
    assert evidence['manual_attestations_required']


def test_local_file_storage_is_a_gap_only_on_a_deployment(settings):
    settings.IS_DEPLOYED = False
    evidence, _ = capture()
    assert evidence['gaps'] == []

    settings.IS_DEPLOYED = True
    evidence, err = capture()
    assert any(g.startswith('files: ') for g in evidence['gaps'])
    assert 'GAP: files:' in err


def test_require_covered_exits_nonzero_on_a_gap(settings):
    settings.IS_DEPLOYED = True
    with pytest.raises(SystemExit) as exc:
        capture('--require-covered')
    assert exc.value.code == 1


def test_render_flag_needs_an_api_key(monkeypatch):
    monkeypatch.delenv('RENDER_API_KEY', raising=False)
    with pytest.raises(CommandError, match='RENDER_API_KEY'):
        capture('--render')


def _assess(api, settings):
    settings.IS_DEPLOYED = True
    stores = [
        {'store': 'database', 'gaps': []},
        {'store': 'files', 'local_path': str(settings.MEDIA_ROOT),
         'gaps': ['service filesystem']},
        {'store': 'key_value', 'configured': True, 'unidentified_backends': 0, 'gaps': []},
    ]
    render = cmd.render_capture(api, 'srv-1', 'dpg-abc123', ['red-abc123'])
    return cmd.assess({'stores': stores, 'render': render}), render


def test_render_capture_ties_attestation_to_the_resources(settings):
    api = FakeRender(render_responses())
    gaps, render = _assess(api, settings)
    assert render['postgres']['recovery']['recoveryStatus'] == 'AVAILABLE'
    assert render['attestation']['key_custody'].startswith('Provider-managed')
    assert render['owners'] == [{'id': 'tea-1', 'name': 'HealthKey', 'type': 'team',
                                 'twoFactorAuthEnabled': None}]
    # Only allowlisted fields leave the API response.
    assert 'not-captured@example.test' not in json.dumps(render)
    assert gaps == ['files: service filesystem']


def test_a_disk_mounted_over_media_root_resolves_the_file_gap(settings):
    disk = {'id': 'dsk-1', 'mountPath': str(settings.MEDIA_ROOT.parent), 'sizeGB': 10}
    gaps, _ = _assess(FakeRender(render_responses(disk=disk)), settings)
    assert gaps == []


def test_a_disk_elsewhere_does_not_resolve_the_file_gap(settings):
    disk = {'id': 'dsk-1', 'mountPath': '/var/data', 'sizeGB': 10}
    gaps, _ = _assess(FakeRender(render_responses(disk=disk)), settings)
    assert gaps == ['files: service filesystem']


def test_unavailable_recovery_and_split_workspaces_are_gaps(settings):
    api = FakeRender(render_responses(recovery='NOT_AVAILABLE', kv_owner='tea-2'))
    gaps, _ = _assess(api, settings)
    assert 'database: point-in-time recovery is not available.' in gaps
    assert 'owners: resources span more than one Render workspace.' in gaps


def test_a_failed_render_read_fails_the_capture(settings):
    responses = render_responses()
    del responses['/postgres/dpg-abc123/recovery']
    with pytest.raises(CommandError, match='recovery'):
        cmd.render_capture(FakeRender(responses), 'srv-1', 'dpg-abc123', [])


def test_render_api_treats_any_non_200_as_a_failed_capture():
    class Session:
        headers = {}

        def get(self, url, timeout):
            return type('Response', (), {'status_code': 403, 'json': lambda self: {}})()

    with pytest.raises(CommandError, match='HTTP 403'):
        cmd.RenderAPI('key', session=Session()).get('/postgres/dpg-abc123')


def test_a_backend_outside_render_is_a_gap_even_beside_a_render_broker(settings):
    settings.CELERY_BROKER_URL = BROKER
    settings.CELERY_RESULT_BACKEND = 'rediss://user:pw@results.example.test:6380/0'
    store = cmd.broker_store()
    assert store['render_key_value_ids'] == ['red-abc123']
    assert store['unidentified_backends'] == 1
    assert 'results.example.test' not in json.dumps(store)
    render = cmd.render_capture(FakeRender(render_responses()), None, 'dpg-abc123', ['red-abc123'])
    gaps = cmd.assess({'stores': [{'store': 'files', 'gaps': []}, store], 'render': render})
    assert any(g.startswith('key_value: ') for g in gaps)


def test_render_ids_come_from_the_running_deployment_only(settings, monkeypatch):
    api = FakeRender(render_responses())
    monkeypatch.setattr(cmd, 'RenderAPI', lambda key: api)
    monkeypatch.setenv('RENDER_API_KEY', 'key')
    monkeypatch.setenv('RENDER_SERVICE_ID', 'srv-1')
    evidence, _ = capture('--render')
    assert '/services/srv-1' in api.paths
    # The test database is not a Render database, so nothing is attested for it.
    assert not any(path.startswith('/postgres/') for path in api.paths)
    assert any(g.startswith('database: no Render Postgres') for g in evidence['gaps'])
    for flag in ('--render-postgres-id=dpg-abc123', '--render-service-id=srv-2'):
        with pytest.raises(CommandError):
            capture('--render', flag)


def test_a_remote_file_storage_backend_is_a_gap_on_a_deployment(settings):
    settings.STORAGES = {**settings.STORAGES, 'default': {
        'BACKEND': 'tests.test_encryption_evidence.RemoteStorage'}}
    settings.IS_DEPLOYED = False
    assert cmd.file_store()['gaps'] == []
    settings.IS_DEPLOYED = True
    store = cmd.file_store()
    assert store['backend'] == 'tests.test_encryption_evidence.RemoteStorage'
    assert 'local_path' not in store
    assert len(store['gaps']) == 1 and 'cannot inspect' in store['gaps'][0]


def test_a_separate_redis_cache_joins_the_key_value_inventory(settings):
    settings.CELERY_BROKER_URL = BROKER
    settings.CELERY_RESULT_BACKEND = BROKER
    settings.CACHES = {'default': {
        'BACKEND': 'django.core.cache.backends.redis.RedisCache',
        'LOCATION': 'rediss://user:cache-secret@red-cache999:6379'}}
    store = cmd.broker_store()
    assert store['render_key_value_ids'] == ['red-abc123', 'red-cache999']
    assert 'cache-secret' not in json.dumps(store)
    settings.CACHES = {'default': {
        'BACKEND': 'django.core.cache.backends.redis.RedisCache',
        'LOCATION': 'redis://cache.example.test:6379'}}
    assert cmd.broker_store()['unidentified_backends'] == 1
