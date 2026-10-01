import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from rest_framework.test import APIClient

from omop_core.models import CodeMappingUpload, Organization, SourceCodeConceptMapping
from patient_portal.models import Identity
from tests.factories import ConceptFactory


URL = '/api/v1/code-mappings/upload/'


def csv_file(content, name='codes.csv'):
    return SimpleUploadedFile(name, content.encode(), content_type='text/csv')


@pytest.fixture
def staff_client():
    user = Identity.objects.create_user(
        email='uploader@example.com', password='x', is_staff=True,
    )
    client = APIClient()
    client.force_authenticate(user=user)
    return client, user


@pytest.mark.django_db
def test_two_column_upload_defaults_seen_to_zero_and_provenance_to_email(staff_client):
    client, user = staff_client
    response = client.post(URL, {
        'file': csv_file(' Source Code , SOURCE DESCRIPTION \nA01,Alpha\nB02,\n'),
        'source_vocabulary_id': 'VendorLab',
    }, format='multipart')

    assert response.status_code == 201, response.data
    assert {key: response.data[key] for key in (
        'duplicate', 'total', 'inserted', 'updated', 'unchanged',
    )} == {'duplicate': False, 'total': 2, 'inserted': 2, 'updated': 0, 'unchanged': 0}
    rows = list(SourceCodeConceptMapping.objects.filter(
        source_vocabulary_id='VendorLab').order_by('source_code'))
    assert [(row.source_code, row.source_code_description, row.occurrence_count)
            for row in rows] == [('A01', 'Alpha', 0), ('B02', '', 0)]
    assert all(row.origin_system == user.email for row in rows)
    assert all(row.created_by == user and row.status == 'proposed' for row in rows)


@pytest.mark.django_db
@pytest.mark.parametrize(('state_header', 'state_value'), [
    ('', ''),
    (',state', ','),
])
def test_destination_without_or_with_blank_state_creates_proposed_mapping(
    staff_client, state_header, state_value,
):
    client, _ = staff_client
    destination = ConceptFactory(domain_id='Measurement', vocabulary_id='LOINC')

    response = client.post(URL, {
        'file': csv_file(
            f'source code,source description,seen count,destination concept ID{state_header}\n'
            f'A01,Albumin,8,{destination.pk}{state_value}\n'
            f'B02,Needs review,2,{state_value}\n'
        ),
        'source_vocabulary_id': 'VendorLab',
        'provenance': 'reviewed-catalogue',
    }, format='multipart')

    assert response.status_code == 201, response.data
    proposed = SourceCodeConceptMapping.objects.get(source_code='A01')
    assert proposed.target_concept == destination
    assert proposed.destination_vocabulary_id == 'LOINC'
    assert proposed.domain_id == 'Measurement'
    assert proposed.omop_table == 'measurement'
    assert proposed.status == 'proposed'
    assert proposed.reviewer is None
    assert proposed.reviewed_at is None
    assert SourceCodeConceptMapping.objects.get(source_code='B02').status == 'proposed'


@pytest.mark.django_db
def test_approved_state_creates_approved_mapping(staff_client):
    client, user = staff_client
    destination = ConceptFactory(domain_id='Measurement', vocabulary_id='LOINC')

    response = client.post(URL, {
        'file': csv_file(
            'source code,source description,seen count,destination concept ID,state\n'
            f'A01,Albumin,8,{destination.pk}, Approved \n'
        ),
        'source_vocabulary_id': 'VendorLab',
        'provenance': 'reviewed-catalogue',
    }, format='multipart')

    assert response.status_code == 201, response.data
    approved = SourceCodeConceptMapping.objects.get(source_code='A01')
    assert approved.target_concept == destination
    assert approved.status == 'approved'
    assert approved.reviewer == user
    assert approved.reviewed_at is not None


@pytest.mark.django_db
def test_destination_ids_are_validated_before_any_rows_are_imported(staff_client):
    client, _ = staff_client
    response = client.post(URL, {
        'file': csv_file(
            'source code,source description,destination concept ID\n'
            'GOOD,Would otherwise import,\nBAD,Missing destination,999999999\n'
        ),
        'source_vocabulary_id': 'VendorLab',
        'provenance': 'reviewed-catalogue',
    }, format='multipart')

    assert response.status_code == 400
    assert response.data['errors'][0]['row'] == 3
    assert 'was not found' in response.data['errors'][0]['detail']
    assert not SourceCodeConceptMapping.objects.exists()


@pytest.mark.django_db
def test_destination_upload_repairs_metadata_without_reassigning_reviewer(staff_client):
    client, _ = staff_client
    destination = ConceptFactory(domain_id='Measurement', vocabulary_id='LOINC')
    original_reviewer = Identity.objects.create_user(
        email='original-reviewer@example.com', password='x', is_staff=True,
    )
    mapping = SourceCodeConceptMapping.objects.create(
        source_vocabulary_id='VendorLab', source_code='A01',
        target_concept=destination, status='approved', reviewer=original_reviewer,
    )

    response = client.post(URL, {
        'file': csv_file(
            'source code,source description,destination concept ID,state\n'
            f'A01,Albumin,{destination.pk},Approved\n'
        ),
        'source_vocabulary_id': 'VendorLab',
        'provenance': 'reviewed-catalogue',
    }, format='multipart')

    assert response.status_code == 201, response.data
    mapping.refresh_from_db()
    assert mapping.destination_vocabulary_id == 'LOINC'
    assert mapping.domain_id == 'Measurement'
    assert mapping.omop_table == 'measurement'
    assert mapping.reviewer == original_reviewer


@pytest.mark.django_db
@pytest.mark.parametrize('state,destination,message', [
    ('Rejected', 'destination', 'Approved, Proposed, or blank'),
    ('Approved', '', 'requires a destination concept ID'),
])
def test_invalid_state_rejects_the_entire_upload(
    staff_client, state, destination, message,
):
    client, _ = staff_client
    concept = ConceptFactory(domain_id='Measurement', vocabulary_id='LOINC')
    destination_value = str(concept.pk) if destination else ''
    response = client.post(URL, {
        'file': csv_file(
            'source code,source description,destination concept ID,state\n'
            f'GOOD,Would otherwise import,,Proposed\n'
            f'BAD,Invalid state,{destination_value},{state}\n'
        ),
        'source_vocabulary_id': 'VendorLab',
        'provenance': 'reviewed-catalogue',
    }, format='multipart')

    assert response.status_code == 400
    assert response.data['errors'][0]['row'] == 3
    assert response.data['errors'][0]['field'] == 'state'
    assert message in response.data['errors'][0]['detail']
    assert not SourceCodeConceptMapping.objects.exists()


@pytest.mark.django_db
def test_non_approver_can_import_proposed_destination_but_not_approved(
    monkeypatch,
):
    user = Identity.objects.create_user(email='analyst@example.com', password='x')
    client = APIClient()
    client.force_authenticate(user=user)
    destination = ConceptFactory(domain_id='Measurement', vocabulary_id='LOINC')
    protected = SourceCodeConceptMapping.objects.create(
        source_vocabulary_id='VendorLab', source_code='C03',
        target_concept=destination, status='approved', reviewer=user,
    )
    monkeypatch.setattr(
        'patient_portal.api.views._can_manage_field_mappings', lambda _user: True,
    )
    monkeypatch.setattr(
        'patient_portal.api.views._can_approve_mappings', lambda _user: False,
    )

    proposed = client.post(URL, {
        'file': csv_file(
            'source code,source description,destination concept ID,state\n'
            f'A01,Albumin,{destination.pk},Proposed\n'
        ),
        'source_vocabulary_id': 'VendorLab', 'provenance': 'analyst-feed',
    }, format='multipart')
    approved = client.post(URL, {
        'file': csv_file(
            'source code,source description,destination concept ID,state\n'
            f'B02,Albumin,{destination.pk},Approved\n'
        ),
        'source_vocabulary_id': 'VendorLab', 'provenance': 'analyst-feed',
    }, format='multipart')
    demotion = client.post(URL, {
        'file': csv_file(
            'source code,source description,destination concept ID,state\n'
            f'C03,Albumin,{destination.pk},Proposed\n'
        ),
        'source_vocabulary_id': 'VendorLab', 'provenance': 'analyst-feed',
    }, format='multipart')

    assert proposed.status_code == 201, proposed.data
    assert SourceCodeConceptMapping.objects.get(source_code='A01').status == 'proposed'
    assert approved.status_code == 400
    assert 'Only org admins and staff' in approved.data['detail']
    assert not SourceCodeConceptMapping.objects.filter(source_code='B02').exists()
    assert demotion.status_code == 400
    assert 'change an approved mapping' in demotion.data['errors'][0]['detail']
    protected.refresh_from_db()
    assert protected.status == 'approved'


@pytest.mark.django_db
def test_upload_updates_metadata_without_overwriting_curation(staff_client):
    client, user = staff_client
    mapping = SourceCodeConceptMapping.objects.create(
        source_vocabulary_id='VendorLab', source_code='A01',
        source_code_description='Old label', occurrence_count=4,
        origin_system='original-feed', status='approved', notes='curator decision',
        destination_vocabulary_id='SNOMED', origin='curator',
    )

    response = client.post(URL, {
        'file': csv_file('source code,source description,seen count\nA01,New label,3\n'),
        'source_vocabulary_id': 'VendorLab',
        'provenance': 'new-feed',
    }, format='multipart')

    assert response.status_code == 201, response.data
    assert response.data['updated'] == 1
    mapping.refresh_from_db()
    assert mapping.source_code_description == 'New label'
    assert mapping.occurrence_count == 7
    assert mapping.origin_system == 'original-feed'
    assert mapping.status == 'approved'
    assert mapping.notes == 'curator decision'
    assert mapping.destination_vocabulary_id == 'SNOMED'
    assert mapping.origin == 'curator'
    assert mapping.updated_by == user


@pytest.mark.django_db
def test_global_upload_does_not_overwrite_hospital_scoped_code(staff_client):
    client, _ = staff_client
    organization = Organization.objects.create(name='Upload Hospital', slug='upload-hospital')
    scoped = SourceCodeConceptMapping.objects.create(
        organization=organization,
        source_vocabulary_id='VendorLab', source_code='A01',
        source_code_description='Hospital label', occurrence_count=4,
        origin_system='hospital-feed', status='proposed',
    )

    response = client.post(URL, {
        'file': csv_file('source code,source description,seen count\nA01,Global label,3\n'),
        'source_vocabulary_id': 'VendorLab',
        'provenance': 'global-feed',
    }, format='multipart')

    assert response.status_code == 201, response.data
    scoped.refresh_from_db()
    assert (scoped.source_code_description, scoped.occurrence_count) == ('Hospital label', 4)
    global_mapping = SourceCodeConceptMapping.objects.get(
        organization__isnull=True, source_vocabulary_id='VendorLab', source_code='A01',
    )
    assert (global_mapping.source_code_description, global_mapping.occurrence_count) == (
        'Global label', 3,
    )


@pytest.mark.django_db
def test_identical_upload_is_idempotent_and_returns_prior_receipt(staff_client):
    client, _ = staff_client
    content = 'source code,source description,seen count\nA01,Alpha,3\n'
    payload = {'source_vocabulary_id': 'VendorLab', 'provenance': 'feed'}
    first = client.post(URL, {'file': csv_file(content), **payload}, format='multipart')
    second = client.post(URL, {'file': csv_file(content), **payload}, format='multipart')

    assert first.status_code == 201
    assert second.status_code == 200
    assert second.data['duplicate'] is True
    assert second.data['upload_id'] == first.data['upload_id']
    assert SourceCodeConceptMapping.objects.get(source_code='A01').occurrence_count == 3
    assert CodeMappingUpload.objects.count() == 1


@pytest.mark.django_db
@pytest.mark.parametrize('bad_row, message', [
    ('A01,Alpha,-1', 'non-negative integer'),
    (',Alpha,1', 'Source code is required'),
    ('A01,Alpha,nope', 'non-negative integer'),
])
def test_validation_reports_row_and_imports_nothing(staff_client, bad_row, message):
    client, _ = staff_client
    response = client.post(URL, {
        'file': csv_file(
            'source code,source description,seen count\nGOOD,Valid,2\n' + bad_row + '\n'
        ),
        'source_vocabulary_id': 'VendorLab',
        'provenance': 'feed',
    }, format='multipart')

    assert response.status_code == 400
    assert response.data['errors'][0]['row'] == 3
    assert message in response.data['errors'][0]['detail']
    assert not SourceCodeConceptMapping.objects.filter(source_vocabulary_id='VendorLab').exists()
    assert not CodeMappingUpload.objects.exists()


@pytest.mark.django_db
def test_upload_rejects_duplicate_codes_and_hk_source_vocabulary(staff_client):
    client, _ = staff_client
    duplicate = client.post(URL, {
        'file': csv_file('source code,source description\nA01,One\nA01,Two\n'),
        'source_vocabulary_id': 'VendorLab', 'provenance': 'feed',
    }, format='multipart')
    assert duplicate.status_code == 400
    assert 'first appears on row 2' in duplicate.data['errors'][0]['detail']

    hk_source = client.post(URL, {
        'file': csv_file('source code,source description\nA01,One\n'),
        'source_vocabulary_id': 'HK-Labs', 'provenance': 'feed',
    }, format='multipart')
    assert hk_source.status_code == 400
    assert 'destinations' in hk_source.data['detail']


@pytest.mark.django_db
def test_upload_requires_mapping_access():
    user = Identity.objects.create_user(email='outsider@example.com', password='x')
    client = APIClient()
    client.force_authenticate(user=user)
    response = client.post(URL, {
        'file': csv_file('source code,source description\nA01,Alpha\n'),
        'source_vocabulary_id': 'VendorLab', 'provenance': 'feed',
    }, format='multipart')
    assert response.status_code == 403


@pytest.mark.django_db
def test_upload_limits_are_atomic(staff_client, monkeypatch):
    client, _ = staff_client
    monkeypatch.setattr('omop_core.services.code_mapping_upload.MAX_UPLOAD_ROWS', 1)
    too_many = client.post(URL, {
        'file': csv_file('source code,source description\nA01,Alpha\nB02,Beta\n'),
        'source_vocabulary_id': 'VendorLab', 'provenance': 'feed',
    }, format='multipart')
    assert too_many.status_code == 400
    assert '1-row limit' in too_many.data['detail']
    assert not SourceCodeConceptMapping.objects.exists()

    monkeypatch.setattr('omop_core.services.code_mapping_upload.MAX_UPLOAD_BYTES', 10)
    too_large = client.post(URL, {
        'file': csv_file('source code,source description\nA01,Alpha\n'),
        'source_vocabulary_id': 'VendorLab', 'provenance': 'feed',
    }, format='multipart')
    assert too_large.status_code == 400
    assert 'upload limit' in too_large.data['detail']
    assert not SourceCodeConceptMapping.objects.exists()
