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
        source_url='https://example.test', artifact_filename='old.zip',
        artifact_identity='healthtree-hospital-seed-20261002-v1',
        artifact_sha256='f' * 64, expected_rows=691_800, outcome='applied',
    )
    HospitalCodeImport.objects.create(
        source_url='https://example.test', artifact_filename='seed.zip',
        artifact_identity='healthtree-hospital-seed-20261005-v2',
        artifact_sha256='d' * 64, expected_rows=691_800, outcome='queued',
    )

    response = staff_client.get(URL, {'system': EPIC})
    assert response.status_code == 200
    assert response.data['total'] == 1
    assert {item['facility_name'] for item in response.data['results'][0]['facilities']} == {
        'University Hospital', 'Unverified Alias',
    }
    assert response.data['system_diagnostics'] == {
        'source_code_rows': 3, 'codes_with_facility_evidence': 1,
    }
    assert response.data['seed_import']['outcome'] == 'queued'
    assert response.data['seed_import']['artifact_identity'] == 'healthtree-hospital-seed-20261005-v2'

    filtered = staff_client.get(URL, {'system': EPIC, 'facility': 'university'})
    assert filtered.data['total'] == 1
    assert filtered.data['results'][0]['facilities'][0]['facility_id'] == 'H1'


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
    assert response.data['results'] == [{
        'source_vocabulary_id': EPIC, 'facilities': [],
    }]
    assert response.data['system_diagnostics']['codes_with_facility_evidence'] == 1
    assert response.data['seed_import']['outcome'] == 'failed'


def test_bounded_pagination_and_permissions(staff_client):
    SourceSystemFacility.objects.bulk_create([
        SourceSystemFacility(
            source_vocabulary_id=f'urn:oid:1.2.840.114350.{index:03}',
            facility_name=f'Hospital {index:02}',
            fingerprint=f'{index:064x}', provenance='seed',
        ) for index in range(51)
    ])
    first = staff_client.get(URL)
    second = staff_client.get(URL, {'page': '2'})
    assert first.data['total'] == 51
    assert len(first.data['results']) == 50
    assert len(second.data['results']) == 1
    assert first.data['results'][0]['source_vocabulary_id'] == 'urn:oid:1.2.840.114350.000'
    descending = staff_client.get(URL, {'sort': 'system_desc'})
    assert descending.data['results'][0]['source_vocabulary_id'] == 'urn:oid:1.2.840.114350.050'
    assert staff_client.get(URL, {'page': '0'}).status_code == 400
    assert staff_client.get(URL, {'sort': 'random'}).status_code == 400

    patient_client = APIClient()
    patient_client.force_authenticate(Identity.objects.create_user(
        email='registry-patient@example.test',
    ))
    assert patient_client.get(URL).status_code == 403
    assert patient_client.post(URL, {'source_vocabulary_id': EPIC,
                                     'facility_name': 'Hospital'}).status_code == 403


def test_system_groups_all_facilities_and_includes_mapping_only_oid(staff_client):
    association(EPIC, 'Hospital A', 'a' * 64)
    association(EPIC, 'Hospital B', 'b' * 64)
    missing_registry = 'urn:oid:1.2.840.114350.999'
    SourceCodeConceptMapping.objects.create(
        source_vocabulary_id=missing_registry, source_code='LOCAL',
    )
    response = staff_client.get(URL)
    assert response.data['total'] == 2
    by_system = {group['source_vocabulary_id']: group for group in response.data['results']}
    assert len(by_system[EPIC]['facilities']) == 2
    assert by_system[missing_registry]['facilities'] == []
    assert response.data['association_total'] == 2


def test_curator_can_add_confirm_and_reject_without_erasing_imported_evidence(staff_client):
    imported = association(EPIC, 'Imported Hospital', 'a' * 64,
                           confidence='high')
    created = staff_client.post(URL, {
        'source_vocabulary_id': EPIC, 'facility_name': 'Second Hospital',
        'facility_id': 'H2',
    }, format='json')
    assert created.status_code == 201
    curated = SourceSystemFacility.objects.get(id=created.data['id'])
    assert curated.review_status == 'confirmed'
    assert curated.provenance == 'curator-added'
    assert curated.reviewed_by == 'registry-curator@example.test'
    assert staff_client.post(URL, {
        'source_vocabulary_id': EPIC, 'facility_name': 'Second Hospital',
        'facility_id': 'H2',
    }, format='json').status_code == 409

    rejected = staff_client.patch(URL, {
        'id': imported.id, 'review_status': 'rejected',
    }, format='json')
    assert rejected.status_code == 200
    imported.refresh_from_db()
    assert imported.review_status == 'rejected'
    assert imported.provenance == 'healthtree-hospital-seed-20261005-v2'
    assert imported.confidence == 'high'
    response = staff_client.get(URL, {'system': EPIC})
    assert {row['review_status'] for row in response.data['results'][0]['facilities']} == {
        'confirmed', 'rejected',
    }
    assert staff_client.patch(URL, {
        'id': imported.id, 'review_status': 'invalid',
    }, format='json').status_code == 400
