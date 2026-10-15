import pytest
from rest_framework.test import APIClient

from omop_core.models import HospitalCodeImport, SourceCodeConceptMapping, SourceSystemFacility
from patient_portal.models import Identity


pytestmark = pytest.mark.django_db
URL = '/api/v1/code-mappings/source-systems/'
EPIC = 'urn:oid:1.2.840.114350.1.13.90.2.7.2.707679'


@pytest.fixture
def staff_client():
    client = APIClient()
    client.force_authenticate(Identity.objects.create_user(
        email='registry-curator@example.test', is_staff=True,
    ))
    return client


def association(system, name, fingerprint, **fields):
    return SourceSystemFacility.objects.create(
        source_vocabulary_id=system, facility_name=name,
        fingerprint=fingerprint, provenance='healthtree-hospital-seed-20261005-v2',
        **fields,
    )


def test_exact_system_and_facility_filters_keep_ambiguity_visible(staff_client):
    association(EPIC, 'University Hospital', 'a' * 64,
                facility_id='H1', level='site', confidence='high')
    association(EPIC, 'Unverified Alias', 'b' * 64,
                level='unknown', confidence='low')
    association('urn:oid:other', 'University Hospital', 'c' * 64,
                level='site', confidence='high')
    SourceCodeConceptMapping.objects.create(
        source_vocabulary_id=EPIC, source_code='ALB',
        source_metadata={'facilities': [{'name': 'University Hospital'}]},
    )
    SourceCodeConceptMapping.objects.create(
        source_vocabulary_id=EPIC, source_code='HGB',
    )
    SourceCodeConceptMapping.objects.create(
        source_vocabulary_id=EPIC, source_code='WBC',
        source_metadata={'facilities': []},
    )
    HospitalCodeImport.objects.create(
        source_url='https://example.test', artifact_filename='seed.zip',
        artifact_identity='healthtree-hospital-seed-20261005-v2',
        artifact_sha256='d' * 64, expected_rows=691_800, outcome='queued',
    )

    response = staff_client.get(URL, {'system': EPIC})
    assert response.status_code == 200
    assert response.data['total'] == 2
    assert {item['facility_name'] for item in response.data['results']} == {
        'University Hospital', 'Unverified Alias',
    }
    assert response.data['system_diagnostics'] == {
        'source_code_rows': 3, 'codes_with_facility_evidence': 1,
    }
    assert response.data['seed_import']['outcome'] == 'queued'

    filtered = staff_client.get(URL, {'system': EPIC, 'facility': 'university'})
    assert filtered.data['total'] == 1
    assert filtered.data['results'][0]['facility_id'] == 'H1'


def test_empty_system_still_reports_code_evidence_and_seed_failure(staff_client):
    SourceCodeConceptMapping.objects.create(
        source_vocabulary_id=EPIC, source_code='ALB',
        source_metadata={'facilities': [{'name': 'University Hospital'}]},
    )
    HospitalCodeImport.objects.create(
        source_url='https://example.test', artifact_filename='seed.zip',
        artifact_identity='healthtree-hospital-seed-20261005-v2',
        artifact_sha256='e' * 64, expected_rows=691_800, outcome='failed',
        failure_reason='Download failed',
    )
    response = staff_client.get(URL, {'system': EPIC})
    assert response.data['results'] == []
    assert response.data['system_diagnostics']['codes_with_facility_evidence'] == 1
    assert response.data['seed_import']['outcome'] == 'failed'


def test_bounded_pagination_and_permissions(staff_client):
    SourceSystemFacility.objects.bulk_create([
        SourceSystemFacility(
            source_vocabulary_id=EPIC, facility_name=f'Hospital {index:02}',
            fingerprint=f'{index:064x}', provenance='seed',
        ) for index in range(51)
    ])
    first = staff_client.get(URL)
    second = staff_client.get(URL, {'page': '2'})
    assert first.data['total'] == 51
    assert len(first.data['results']) == 50
    assert len(second.data['results']) == 1
    assert staff_client.get(URL, {'page': '0'}).status_code == 400

    patient_client = APIClient()
    patient_client.force_authenticate(Identity.objects.create_user(
        email='registry-patient@example.test',
    ))
    assert patient_client.get(URL).status_code == 403
