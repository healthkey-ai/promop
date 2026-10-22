"""Sentry wiring: what enables it, what it sends, what it refuses to send."""

import os
from unittest import mock

import pytest
import sentry_sdk
from django.urls import path
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.exceptions import APIException, NotFound, ValidationError
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from sentry_sdk.transport import Transport

from promop import sentry
from patient_portal.api.exception_handlers import sentry_exception_handler


class TestInit:
    def test_disabled_without_dsn(self, monkeypatch):
        monkeypatch.delenv('SENTRY_DSN', raising=False)
        with mock.patch('sentry_sdk.init') as init:
            assert sentry.init_sentry(debug=False) is False
        init.assert_not_called()

    def test_blank_dsn_is_not_a_dsn(self, monkeypatch):
        monkeypatch.setenv('SENTRY_DSN', '   ')
        with mock.patch('sentry_sdk.init') as init:
            assert sentry.init_sentry(debug=False) is False
        init.assert_not_called()

    def test_enabled_with_dsn(self, monkeypatch):
        monkeypatch.setenv('SENTRY_DSN', 'https://key@example.ingest.sentry.io/1')
        with mock.patch('sentry_sdk.init') as init:
            assert sentry.init_sentry(debug=False) is True
        options = init.call_args.kwargs
        assert options['dsn'] == 'https://key@example.ingest.sentry.io/1'
        integrations = {type(i).__name__ for i in options['integrations']}
        assert integrations == {'DjangoIntegration', 'CeleryIntegration', 'LoggingIntegration'}

    def test_secret_env_vars_are_scrubbed(self, monkeypatch):
        monkeypatch.setenv('SENTRY_DSN', 'https://key@example.ingest.sentry.io/1')
        with mock.patch('sentry_sdk.init') as init:
            sentry.init_sentry(debug=False)
        denylist = init.call_args.kwargs['event_scrubber'].denylist
        for key in ('database_url', 'audit_hmac_key', 'service_auth_token',
                    'celery_result_backend', 'cache_url'):
            assert key in denylist


class TestOptions:
    def test_pii_off_and_bodies_never_sent_by_default(self, monkeypatch):
        monkeypatch.delenv('SENTRY_SEND_DEFAULT_PII', raising=False)
        options = sentry.build_options('dsn', debug=False)
        assert options['send_default_pii'] is False
        assert options['max_request_body_size'] == 'never'
        assert options['include_local_variables'] is False

    def test_environment_defaults_to_the_debug_flag(self, monkeypatch):
        monkeypatch.delenv('SENTRY_ENVIRONMENT', raising=False)
        assert sentry.build_options('dsn', debug=True)['environment'] == 'development'
        assert sentry.build_options('dsn', debug=False)['environment'] == 'production'

    def test_transactions_are_scrubbed_too(self):
        options = sentry.build_options('dsn', debug=False)
        assert options['before_send_transaction'] is options['before_send']

    def test_release_falls_back_to_the_render_commit(self, monkeypatch):
        monkeypatch.delenv('SENTRY_RELEASE', raising=False)
        monkeypatch.setenv('RENDER_GIT_COMMIT', 'abc123')
        assert sentry.build_options('dsn', debug=False)['release'] == 'abc123'

    def test_sample_rates_are_clamped_and_survive_junk(self, monkeypatch):
        monkeypatch.setenv('SENTRY_TRACES_SAMPLE_RATE', '7')
        assert sentry.build_options('dsn', debug=False)['traces_sample_rate'] == 1.0
        monkeypatch.setenv('SENTRY_TRACES_SAMPLE_RATE', 'not-a-number')
        assert sentry.build_options('dsn', debug=False)['traces_sample_rate'] == 0.0


class TestScrubbing:
    def test_query_string_and_body_are_dropped(self):
        event = {
            'request': {
                'url': 'https://promop.example/api/v1/patient-records/?person_id=42',
                'query_string': 'person_id=42',
                'data': {'birthDate': '1970-01-01'},
            },
        }
        scrubbed = sentry._scrub_event(event, hint={})
        assert scrubbed['request']['url'] == 'https://promop.example/api/v1/patient-records/'
        assert 'query_string' not in scrubbed['request']
        assert 'data' not in scrubbed['request']

    @pytest.mark.parametrize('url, expected', [
        ('https://promop.example/api/v1/patient-records/123/',
         'https://promop.example/api/v1/patient-records/:id/'),
        ('/api/v1/patients/9f1c2b34-aaaa-bbbb-cccc-ddddeeeeffff/invite/',
         '/api/v1/patients/:id/invite/'),
        ('/api/v1/measurements/bulk_delete/', '/api/v1/measurements/bulk_delete/'),
        ('https://promop.example/', 'https://promop.example/'),
    ])
    def test_ids_in_the_path_are_redacted(self, url, expected):
        event = {'request': {'url': url}}
        assert sentry._scrub_event(event, hint={})['request']['url'] == expected

    def test_emails_are_redacted_from_a_log_event(self):
        event = {'logentry': {
            'message': 'Failed to send invitation to %s: %s',
            'params': ['jane.doe@example.com', 'smtp down'],
            'formatted': 'Failed to send invitation to jane.doe@example.com: smtp down',
        }}
        logentry = sentry._scrub_event(event, hint={})['logentry']
        assert logentry['params'] == ['[email]', 'smtp down']
        assert logentry['formatted'] == 'Failed to send invitation to [email]: smtp down'

    def test_connection_strings_in_argv_are_filtered(self):
        event = {'extra': {'sys.argv': ['manage.py', 'migrate', 'postgresql://u:p@host/db']}}
        assert sentry._scrub_event(event, hint={})['extra']['sys.argv'] == [
            'manage.py', 'migrate', '[Filtered]',
        ]

    def test_an_event_without_a_request_is_left_alone(self):
        event = {'exception': {'values': []}}
        assert sentry._scrub_event(event, hint={}) == event


class TestExceptionHandler:
    def test_server_error_is_reported(self):
        exc = APIException('backend unavailable')
        with mock.patch('patient_portal.api.exception_handlers.sentry_sdk.capture_exception') as capture:
            response = sentry_exception_handler(exc, {})
        assert response.status_code == status.HTTP_500_INTERNAL_SERVER_ERROR
        capture.assert_called_once_with(exc)

    @pytest.mark.parametrize('exc', [ValidationError('bad'), NotFound('gone')])
    def test_client_errors_are_not_reported(self, exc):
        with mock.patch('patient_portal.api.exception_handlers.sentry_sdk.capture_exception') as capture:
            response = sentry_exception_handler(exc, {})
        assert 400 <= response.status_code < 500
        capture.assert_not_called()

    def test_unhandled_exception_is_left_to_django(self):
        with mock.patch('patient_portal.api.exception_handlers.sentry_sdk.capture_exception') as capture:
            assert sentry_exception_handler(RuntimeError('boom'), {}) is None
        capture.assert_not_called()


def test_drf_uses_the_handler(settings):
    assert settings.REST_FRAMEWORK['EXCEPTION_HANDLER'] == (
        'patient_portal.api.exception_handlers.sentry_exception_handler'
    )


@pytest.mark.urls('tests.test_sentry')
class TestOneEventPerFailure:
    """django.request is ignored by DjangoIntegration, so nothing reports twice."""

    @pytest.fixture
    def events(self, settings):
        settings.ALLOWED_HOSTS = ['*']
        captured: list[dict] = []

        class Capture(Transport):
            def capture_envelope(self, envelope):
                captured.extend(
                    item.payload.json for item in envelope.items
                    if item.headers.get('type') == 'event'
                )

        with mock.patch.dict(os.environ, {'SENTRY_DSN': 'https://key@example.ingest.sentry.io/1'}):
            sentry.init_sentry(debug=False)
        sentry_sdk.get_client().transport = Capture()
        yield captured
        sentry_sdk.get_client().close()

    def test_drf_server_error_reports_once(self, client, events):
        response = client.get('/sentry-test/api-exception/')
        assert response.status_code == 500
        assert len(events) == 1

    def test_unhandled_exception_reports_once(self, client, events):
        with pytest.raises(RuntimeError):
            client.get('/sentry-test/crash/')
        assert len(events) == 1

    def test_direct_server_error_response_reports_once(self, client, events):
        assert client.get('/sentry-test/unavailable/42/').status_code == 503
        assert len(events) == 1
        assert events[0]['message'] == '503 GET /sentry-test/unavailable/:id/'

    def test_client_error_reports_nothing(self, client, events):
        assert client.get('/sentry-test/not-found/').status_code == 404
        assert events == []


@api_view(['GET'])
@permission_classes([AllowAny])
def _api_exception_view(request):
    raise APIException('backend unavailable')


@api_view(['GET'])
@permission_classes([AllowAny])
def _crash_view(request):
    raise RuntimeError('boom')


@api_view(['GET'])
@permission_classes([AllowAny])
def _not_found_view(request):
    raise NotFound('gone')


@api_view(['GET'])
@permission_classes([AllowAny])
def _unavailable_view(request, person_id):
    return Response({'detail': 'concepts missing'}, status=status.HTTP_503_SERVICE_UNAVAILABLE)


urlpatterns = [
    path('sentry-test/unavailable/<int:person_id>/', _unavailable_view),
    path('sentry-test/api-exception/', _api_exception_view),
    path('sentry-test/crash/', _crash_view),
    path('sentry-test/not-found/', _not_found_view),
]
