"""Patient editing must honor the same scoped admin rights as the patient list."""
from datetime import timedelta
from unittest.mock import patch

import pytest
from django.utils import timezone
from rest_framework.test import APIClient

from omop_core.authorization import can_access_patient, can_write_patient, get_actor_role
from omop_core.models import GroupAccess, Location, OrgTrust
from patient_portal.models import Identity
from tests.factories import OrganizationFactory, PatientRecordFactory

pytestmark = pytest.mark.django_db


@pytest.fixture
def setup():
    actor = Identity.objects.create_user(email='trusted-admin@example.test', email_verified_at=timezone.now())
    source, target = OrganizationFactory(), OrganizationFactory()
    record = PatientRecordFactory(organization=target)
    client = APIClient()
    client.force_authenticate(actor)
    return actor, source, target, record, client


@pytest.mark.parametrize('access', ['direct', 'organization_trust', 'domain_trust'])
def test_org_admin_can_edit_profile_without_reverse_derivation(setup, access):
    actor, source, target, record, client = setup
    if access == 'direct':
        GroupAccess.objects.create(identity=actor, org=target, role='org_admin')
    elif access == 'organization_trust':
        GroupAccess.objects.create(identity=actor, org=source, role='org_admin')
        OrgTrust.objects.create(granting_org=target, trusted_org=source)
    else:
        OrgTrust.objects.create(granting_org=target, trusted_domain='example.test')

    assert can_access_patient(actor, record.person_id)
    assert can_write_patient(actor, record.person_id)
    assert get_actor_role(actor, record.person_id) == 'org_admin'
    descriptor = client.get('/api/v1/patient-records/writable-fields/',
                            {'person_id': record.person_id})
    assert descriptor.status_code == 200
    payload = {'email': 'corrected@example.test', 'phone_number': '555-0100',
               'date_of_birth': '1985-03-15', 'gender': 'Female',
               'race': 'Asian', 'ethnicity': 'Not Hispanic or Latino',
               'city': 'Portland', 'region': 'OR', 'country': 'United States',
               'postal_code': '97201', 'facility_name': 'Example clinic'}
    for field in payload:
        assert descriptor.data[field]['writable'], field
        assert descriptor.data[field]['target'] == 'patient_record', field
    assert not descriptor.data['bmi']['writable']

    with patch('omop_core.services.patient_record_service.refresh_patient_record') as refresh, \
         patch('patient_portal.api.views.refresh_patient_record') as view_refresh:
        response = client.patch(f'/api/patient-info/{record.person_id}/', payload, format='json')
    assert response.status_code == 200, response.data
    refresh.assert_not_called()
    view_refresh.assert_not_called()
    record.refresh_from_db()
    person = record.person
    assert record.email == person.email == payload['email']
    assert record.phone_number == person.phone_number == payload['phone_number']
    assert record.facility_name == person.facility_name == payload['facility_name']
    assert record.date_of_birth.isoformat() == payload['date_of_birth']
    assert (person.year_of_birth, person.month_of_birth, person.day_of_birth) == (1985, 3, 15)
    assert record.gender == 'F' and person.gender_source_value == 'Female'
    assert record.race == person.race_source_value == payload['race']
    assert record.ethnicity == person.ethnicity_source_value == payload['ethnicity']
    location = Location.objects.get(location_id=person.location_id)
    assert record.city == location.city == 'Portland'
    assert record.region == location.state == 'OR'
    assert record.postal_code == location.zip == '97201'


@pytest.mark.parametrize('restriction', ['expired', 'revoked', 'unrelated', 'patient_only',
                                         'inactive_target', 'recursive', 'analyst'])
def test_trust_fix_does_not_grant_out_of_scope_patient_writes(setup, restriction):
    actor, source, target, record, client = setup
    grant = GroupAccess.objects.create(identity=actor, org=source, role='org_admin')
    trust = OrgTrust.objects.create(granting_org=target, trusted_org=source)
    if restriction == 'expired':
        grant.expires_at = timezone.now() - timedelta(seconds=1)
        grant.save()
    elif restriction == 'revoked':
        trust.delete()
    elif restriction == 'unrelated':
        record.organization = OrganizationFactory()
        record.save()
    elif restriction == 'patient_only':
        grant.role = 'patient'
        grant.save()
    elif restriction == 'inactive_target':
        target.is_active = False
        target.save()
    elif restriction == 'recursive':
        third = OrganizationFactory()
        OrgTrust.objects.create(granting_org=third, trusted_org=target)
        record.organization = third
        record.save()
    elif restriction == 'analyst':
        trust.delete()
        grant.org = target
        grant.role = 'analyst'
        grant.save()

    assert not can_write_patient(actor, record.person_id)
    assert can_access_patient(actor, record.person_id) == (restriction == 'analyst')
    descriptor = client.get('/api/v1/patient-records/writable-fields/',
                            {'person_id': record.person_id})
    assert descriptor.status_code == 200
    assert descriptor.data['email']['read_only_for_caller']
    previous = record.email
    response = client.patch(f'/api/patient-info/{record.person_id}/',
                            {'email': 'must-not-save@example.test'}, format='json')
    assert response.status_code in {403, 404}
    record.refresh_from_db()
    assert record.email == previous
