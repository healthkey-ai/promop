"""Two things a response body must not do, both found by CodeQL.

* `auth_test` sits behind `AllowAny` and only on DEBUG; its failure branch used
  to return `traceback.format_exc()` and the exception's text
  (`py/stack-trace-exposure`).
* The assessment reader accepted any `Coding.system` that contained
  `'loinc.org'` (`py/incomplete-url-substring-sanitization`), so
  `https://example.test/loinc.org` read as LOINC. The system is matched on its
  parsed host now, and the codings it reads are normalised: the loop sits
  inside the per-patient try, so a shape FHIR does not prescribe would drop the
  patient rather than skip the coding.
"""
import pytest
from rest_framework.test import APIClient

from patient_portal.api.views import _assessment_codings, _coding_strings, _is_loinc_system

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
    # A host read from text that is not a host: query, fragment, opaque URI.
    'example.test?next=www.loinc.org',
    'loinc.org.example.test?next=.loinc.org',
    'urn:example.loinc.org',
    'mailto:loinc.org',
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


def test_auth_test_reports_the_step_it_reached(settings, monkeypatch):
    """The label is the only diagnostic left, so it must follow the work it names."""
    from rest_framework.request import Request

    settings.DEBUG = True

    def unreadable(self):
        raise RuntimeError('unreadable body')

    # Reading the body is the first thing the probe does; the answer must not
    # claim it got past that step.
    monkeypatch.setattr(Request, 'data', property(unreadable))
    response = APIClient().post('/api/v1/auth/test/', {'username': 'nobody'}, format='json')

    assert response.status_code == 500
    assert response.data['step'] == 'start'


@pytest.mark.parametrize('observation', [
    {}, {'code': None}, {'code': 'LOINC'}, {'code': {}}, {'code': {'coding': None}},
    {'code': {'coding': {}}}, {'code': {'coding': [None, 1, 'x']}},
])
def test_a_malformed_code_yields_no_codings(observation):
    assert _assessment_codings(observation) == []


def test_a_code_with_a_bad_coding_keeps_the_good_one():
    codings = [None, {'system': None, 'code': None}, {'system': 'http://loinc.org', 'code': '89247-1'}]
    assert _assessment_codings({'code': {'coding': codings}}) == [
        {'system': None, 'code': None}, {'system': 'http://loinc.org', 'code': '89247-1'}]


@pytest.mark.parametrize('coding,expected', [
    ({'system': 'http://loinc.org', 'code': '89247-1'}, ('http://loinc.org', '89247-1')),
    ({'system': None, 'code': None}, ('', '')),
    ({'system': {}, 'code': ['89247-1']}, ('', '')),
    ({'system': 42, 'code': 718}, ('', '')),
    ({}, ('', '')),
])
def test_a_coding_is_read_as_strings(coding, expected):
    assert _coding_strings(coding) == expected
