import pytest
from rest_framework.test import APIClient

from omop_core.models import SourceCodeConceptMapping, SourceSystemFacility
from patient_portal.models import Identity


pytestmark = pytest.mark.django_db
URL = '/api/v1/code-mappings/facility-browse/'
SYS_A, SYS_B, SYS_Q, SYS_LOOSE = 'urn:oid:a', 'urn:oid:b', 'urn:oid:quest', 'urn:oid:loose'


def assoc(system, name, n, **kw):
    return SourceSystemFacility.objects.create(
        source_vocabulary_id=system, facility_name=name,
        fingerprint=f'{n:064d}', provenance='test', **kw)


def code(system, value, desc='', **kw):
    return SourceCodeConceptMapping.objects.create(
        source_vocabulary_id=system, source_code=value,
        source_code_description=desc, **kw)


@pytest.fixture
def client():
    c = APIClient()
    c.force_authenticate(Identity.objects.create_user(email='c@example.test', is_staff=True))
    return c


@pytest.fixture
def data():
    assoc(SYS_A, 'Hopkins', 1)
    assoc(SYS_B, 'hopkins', 2)
    assoc(SYS_Q, 'Hopkins', 3)
    assoc(SYS_Q, 'Providence', 4)
    assoc(SYS_A, 'Rejected Place', 5, review_status='rejected')
    for v in ('MSQ2', 'MSQ10', 'MSQ1'):
        code(SYS_A, v, 'M-spike quantitation ' + v[3:])
    code(SYS_B, '7761', 'Monoclonal band 2', status='approved')
    code(SYS_Q, 'ALB', 'Albumin')
    code(SYS_LOOSE, 'X1', 'Orphan')


def test_facility_list_groups_case_insensitively_and_adds_unattributed(client, data):
    body = client.get(URL).json()['facilities']
    by_name = {f['facility'].casefold(): f for f in body}
    assert by_name['hopkins']['system_count'] == 3
    assert by_name['hopkins']['code_count'] == 5
    assert 'rejected place' not in by_name
    assert by_name['__unattributed__']['code_count'] == 1


def test_facility_search_filters_names(client, data):
    body = client.get(URL, {'search': 'prov'}).json()['facilities']
    assert [f['facility'] for f in body] == ['Providence']


def test_codes_sorted_by_number_and_shared_systems_flagged(client, data):
    body = client.get(URL, {'facility': 'HOPKINS', 'system': SYS_A}).json()
    assert [r['source_code'] for r in body['results']] == ['MSQ1', 'MSQ2', 'MSQ10']
    shared = {s['source_vocabulary_id']: s['shared_with_facilities'] for s in body['systems']}
    assert shared[SYS_Q] == 1 and shared[SYS_A] == 0


def test_search_across_facility_and_status_filter(client, data):
    body = client.get(URL, {'facility': 'Hopkins', 'q': 'band'}).json()
    assert [r['source_code'] for r in body['results']] == ['7761']
    body = client.get(URL, {'facility': 'Hopkins', 'status': 'approved'}).json()
    assert body['total'] == 0  # approved needs a destination
    body = client.get(URL, {'facility': 'Hopkins', 'status': 'unmapped'}).json()
    assert body['total'] == 5


def test_unattributed_and_validation(client, data):
    body = client.get(URL, {'facility': '__unattributed__'}).json()
    assert [r['source_code'] for r in body['results']] == ['X1']
    assert client.get(URL, {'facility': 'Hopkins', 'sort': 'bogus'}).status_code == 400
    assert client.get(URL, {'facility': 'Hopkins', 'status': 'x'}).status_code == 400
    assert client.get(URL, {'facility': 'Hopkins', 'page': '0'}).status_code == 400


def test_requires_mapping_read_access(data):
    c = APIClient()
    assert c.get(URL).status_code in (401, 403)
    c.force_authenticate(Identity.objects.create_user(email='p@example.test'))
    assert c.get(URL).status_code == 403
