from io import StringIO
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from django.core.management import call_command
from rest_framework.test import APIRequestFactory, force_authenticate

from omop_core.models import PatientRecord, Person
from omop_core.services.treating_institutions import institution_directory, sample_institution
from patient_portal.api.treating_institutions import TreatingInstitutionListView, InstitutionDirectorySerializer
from tests.factories import PatientRecordFactory, OrganizationFactory


def test_directory_contains_clinical_centers_and_traceable_locations():
    directory = institution_directory()
    serializer = InstitutionDirectorySerializer(data=directory)
    assert serializer.is_valid(), serializer.errors
    centers = directory['institutions']
    assert len(centers) >= 60
    assert len({c['id'] for c in centers}) == len(centers)
    assert len({c['label'] for c in centers}) == len(centers)
    assert all(c['designation'] in {'Clinical', 'Comprehensive'} for c in centers)
    assert all(len(c['label']) <= 255 and c['source_url'].startswith('https://www.cancer.gov/') for c in centers)
    assert sample_institution(1, 'MA') == sample_institution(1, 'Massachusetts')
    assert sample_institution(1, 'MA').endswith('Massachusetts')
    assert all('St. Jude' not in sample_institution(i, 'TN') for i in range(100))


def test_directory_requires_authentication():
    factory = APIRequestFactory()
    view = TreatingInstitutionListView.as_view()
    request = factory.get('/api/treating-institutions/')
    assert view(request).status_code in {401, 403}
    request = factory.get('/api/treating-institutions/')
    force_authenticate(request, user=SimpleNamespace(is_authenticated=True, pk=1))
    response = view(request)
    assert response.status_code == 200
    assert response.data == institution_directory()


@pytest.mark.django_db
def test_backfill_preview_scope_preservation_and_idempotence():
    org = OrganizationFactory(slug='synthea-fl')
    sample = PatientRecordFactory(organization=org, facility_name=None, region='MA')
    existing = PatientRecordFactory(organization=org, facility_name='Community Oncology')
    recovered = PatientRecordFactory(organization=org, facility_name=None)
    cleared = PatientRecordFactory(organization=org, facility_name='', user_edited_fields=['facility_name'])
    conflict = PatientRecordFactory(organization=org, facility_name='Recorded center')
    other = PatientRecordFactory(facility_name=None)
    Person.objects.filter(pk__in=[r.person_id for r in [sample, existing, recovered, cleared, conflict, other]]).update(facility_name=None)
    Person.objects.filter(pk=recovered.person_id).update(facility_name='Imported hospital')
    Person.objects.filter(pk=conflict.person_id).update(facility_name='Different hospital')
    call_command('backfill_sample_treating_institutions', stdout=StringIO())
    sample.refresh_from_db()
    assert sample.facility_name is None
    with patch('omop_core.services.patient_record_service.refresh_patient_record') as refresh:
        call_command('backfill_sample_treating_institutions', confirm=True, batch_size=2, stdout=StringIO())
        refresh.assert_not_called()
    for record in [sample, existing, recovered]:
        record.refresh_from_db(); record.person.refresh_from_db()
        assert record.facility_name == record.person.facility_name
    assert sample.facility_name.endswith('Massachusetts')
    assert existing.facility_name == 'Community Oncology'
    assert recovered.facility_name == 'Imported hospital'
    cleared.refresh_from_db(); other.refresh_from_db(); conflict.refresh_from_db()
    assert cleared.facility_name == ''
    assert other.facility_name is None
    assert conflict.facility_name == 'Recorded center'
    before = list(PatientRecord.objects.values_list('pk', 'facility_name'))
    output = StringIO()
    call_command('backfill_sample_treating_institutions', confirm=True, stdout=output)
    assert '"patient_record_updates": 0' in output.getvalue()
    assert '"person_updates": 0' in output.getvalue()
    assert before == list(PatientRecord.objects.values_list('pk', 'facility_name'))
