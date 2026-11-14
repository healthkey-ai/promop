"""Two things a response body must not do, both found by CodeQL.

* `auth_test` sits behind `AllowAny` and only on DEBUG; its failure branch used
  to return `traceback.format_exc()` and the exception's text
  (`py/stack-trace-exposure`).
* The assessment reader accepted any `Coding.system` that contained
  `'loinc.org'` (`py/incomplete-url-substring-sanitization`), so
  `https://example.test/loinc.org` read as LOINC. The system is matched on its
  parsed host now.
"""
import pytest
from rest_framework.test import APIClient

from patient_portal.api.views import _is_loinc_system

# The probe request traverses AuditLogMiddleware, which writes an AuditEvent.
pytestmark = pytest.mark.django_db


@pytest.mark.parametrize('system', [
    'http://loinc.org',
    'https://loinc.org',
    'HTTPS://LOINC.ORG',
    'http://loinc.org/',
    'loinc.org',
    'loinc.org/x',
])
def test_a_loinc_system_is_recognised(system):
    assert _is_loinc_system(system) is True


@pytest.mark.parametrize('system', [
    '',
    None,
    42,
    'http://snomed.info/sct',
    # The value the substring test accepted and this one must not.
    'https://example.test/loinc.org',
    'https://example.test/?next=http://loinc.org',
    'http://loinc.org.example.test',
    'https://notloinc.org',
    # Upload input: an authority urlsplit refuses must decline, not raise.
    'http://[x',
])
def test_a_system_that_merely_names_loinc_is_not_loinc(system):
    assert _is_loinc_system(system) is False


def test_auth_test_returns_no_traceback(settings, monkeypatch):
    settings.DEBUG = True

    def boom(*args, **kwargs):
        raise RuntimeError('password=hunter2')

    monkeypatch.setattr('django.contrib.auth.authenticate', boom)
    response = APIClient().post('/api/v1/auth/test/', {'username': 'nobody'}, format='json')

    assert response.status_code == 500
    assert response.data == {'status': 'error', 'step': 'imported authenticate'}
    assert 'hunter2' not in str(response.data)
