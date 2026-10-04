"""Mapping provenance: engine and version when proposed, the approver once approved (#1719)."""
import importlib

import pytest
from django.apps import apps
from rest_framework.test import APIClient

from omop_core.models import FieldConceptMapping
from omop_core.services.field_curation_transfer import read_payload, apply_payload
from omop_core.services.field_descriptor import get_all_field_descriptors
from omop_core.services.field_mapping_provenance import (
    FIELD_SUGGESTION_PROVENANCE, suggestion_provenance, user_provenance,
)
from patient_portal.api.serializers import FieldConceptMappingSerializer
from patient_portal.models import Identity

pytestmark = pytest.mark.django_db


def test_api_cannot_spoof_system_origin():
    serializer = FieldConceptMappingSerializer(data={
        'field_name': 'hemoglobin_g_dl', 'provenance': 'system_generated',
    })
    assert serializer.is_valid(), serializer.errors
    row = serializer.save()
    assert row.provenance == 'curator'
    assert serializer.data['provenance'] == 'curator'


@pytest.fixture
def admin():
    user = Identity.objects.create_user(email='approver@example.org', is_staff=True)
    client = APIClient()
    client.force_authenticate(user)
    return client, user


def patch(client, row, **data):
    response = client.patch(f'/api/v1/field-mappings/{row.pk}/', data, format='json')
    assert response.status_code == 200, response.data
    row.refresh_from_db()
    return response


def test_approving_a_suggestion_names_the_approver(admin):
    client, user = admin
    row = FieldConceptMapping.objects.create(
        field_name='hemoglobin_g_dl', provenance=suggestion_provenance('reviewed'),
    )
    assert row.provenance == f'{FIELD_SUGGESTION_PROVENANCE} (reviewed)'
    response = patch(client, row, status='approved')
    assert row.provenance == 'approver@example.org'
    assert response.data['provenance'] == 'approver@example.org'
    assert row.reviewer_id == user.pk


def test_an_unapproved_recipe_edit_is_curator_and_approval_then_names_the_approver(admin):
    client, _ = admin
    row = FieldConceptMapping.objects.create(
        field_name='hemoglobin_g_dl', provenance=suggestion_provenance('lexical'),
    )
    patch(client, row, notes='checked')
    assert row.provenance == suggestion_provenance('lexical'), 'notes are not the recipe'
    patch(client, row, unit='g/dL')
    assert row.provenance == 'curator'
    patch(client, row, unit='g/dL', status='approved')
    assert row.provenance == 'approver@example.org'


def test_changing_an_approved_recipe_restamps_and_unapproving_keeps_the_name(admin):
    client, _ = admin
    row = FieldConceptMapping.objects.create(
        field_name='hemoglobin_g_dl', provenance='someone-else@example.org', status='approved',
    )
    patch(client, row, notes='plain resave')
    assert row.provenance == 'someone-else@example.org'
    patch(client, row, unit='g/dL')
    assert row.provenance == 'approver@example.org'
    patch(client, row, status='proposed')
    assert row.provenance == 'approver@example.org'


def test_creating_an_approved_mapping_names_its_approver(admin):
    client, _ = admin
    response = client.post('/api/v1/field-mappings/', {
        'field_name': 'hemoglobin_g_dl', 'status': 'approved', 'provenance': 'system_generated',
    }, format='json')
    assert response.status_code == 201, response.data
    assert FieldConceptMapping.objects.get(field_name='hemoglobin_g_dl').provenance == 'approver@example.org'


def test_user_provenance_falls_back_and_fits_the_column():
    assert user_provenance(Identity(email='', name='Dr Who', pk=7)) == 'Dr Who'
    assert user_provenance(Identity(email='', name='', pk=7)) == '7'
    assert len(user_provenance(Identity(email='a' * 200 + '@x.org'))) == 100


def test_migration_names_the_reviewer_of_approved_rows_only():
    reviewer = Identity.objects.create_user(email='reviewer@example.org')
    approved = FieldConceptMapping.objects.create(
        field_name='hemoglobin_g_dl', provenance='system_generated', status='approved', reviewer=reviewer)
    seeded = FieldConceptMapping.objects.create(
        field_name='platelet_count', provenance='system_generated', status='approved')
    proposed = FieldConceptMapping.objects.create(
        field_name='wbc_count', provenance='system_generated', status='proposed', reviewer=reviewer)
    migration = importlib.import_module('omop_core.migrations.0279_field_mapping_provenance_text')
    migration.stamp_approvers(apps, None)
    for row in (approved, seeded, proposed):
        row.refresh_from_db()
    assert approved.provenance == 'reviewer@example.org'
    assert seeded.provenance == 'system_generated', 'no recorded reviewer to name'
    assert proposed.provenance == 'system_generated', 'the proposing engine was never recorded'


def test_list_and_transfer_preserve_provenance_and_unknown_legacy():
    row = FieldConceptMapping.objects.create(
        field_name='hemoglobin_g_dl', provenance='system_generated',
    )
    descriptor = next(d for d in get_all_field_descriptors() if d['field_name'] == row.field_name)
    assert descriptor['mapping']['provenance'] == 'system_generated'
    payload = read_payload('default', tables={'mappings'})
    row.delete()
    apply_payload(payload)
    assert FieldConceptMapping.objects.get(field_name='hemoglobin_g_dl').provenance == 'system_generated'
    payload['mappings'][0].pop('provenance')
    apply_payload(payload)
    assert FieldConceptMapping.objects.get(field_name='hemoglobin_g_dl').provenance == ''


def test_propose_all_names_its_engine_and_version(admin):
    from omop_core.services.mappings import LAB_FIELD_TO_LOINC
    from tests.factories import ConceptFactory
    code = LAB_FIELD_TO_LOINC['hemoglobin_g_dl'][0]
    ConceptFactory(vocabulary_id='LOINC', concept_code=code, domain_id='Measurement')
    client, _ = admin
    response = client.post('/api/v1/field-mappings/propose-all/', {}, format='json')
    assert response.status_code in (200, 201), response.data
    row = FieldConceptMapping.objects.get(field_name='hemoglobin_g_dl')
    assert (row.status, row.provenance) == ('proposed', f'{FIELD_SUGGESTION_PROVENANCE} (propose-all)')
