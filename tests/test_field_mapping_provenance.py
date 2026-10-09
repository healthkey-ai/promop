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


# --- review fixes -------------------------------------------------------------

def test_approved_recipe_edit_signs_off_reviewer_with_provenance(admin):
    """Provenance, reviewer and reviewed_at all name the editor who signs off."""
    client, user = admin
    original = Identity.objects.create_user(email='first-approver@example.org', is_staff=True)
    row = FieldConceptMapping.objects.create(
        field_name='hemoglobin_g_dl', status='approved', reviewer=original,
        provenance='first-approver@example.org',
    )
    patch(client, row, unit='g/dL')
    assert (row.provenance, row.reviewer_id) == ('approver@example.org', user.pk)
    assert row.reviewed_at is not None


def test_a_non_approvers_recipe_edit_is_curator_and_keeps_the_reviewer():
    from types import SimpleNamespace
    approver = Identity.objects.create_user(email='first-approver@example.org', is_staff=True)
    editor = Identity.objects.create_user(email='analyst@example.org')
    row = FieldConceptMapping.objects.create(
        field_name='hemoglobin_g_dl', status='approved', reviewer=approver,
        provenance='first-approver@example.org',
    )
    serializer = FieldConceptMappingSerializer(
        row, data={'unit': 'g/dL'}, partial=True, context={'request': SimpleNamespace(user=editor)},
    )
    assert serializer.is_valid(), serializer.errors
    serializer.save()
    row.refresh_from_db()
    assert (row.provenance, row.reviewer_id) == ('curator', approver.pk)


@pytest.mark.parametrize('stored, portable', [
    ('approver@example.org', 'curator'),
    ('Dr Who', 'curator'),
    ('curator', 'curator'),
    ('system_generated', 'system_generated'),
    ('', ''),
    ('field-suggest v1 (reviewed)', 'field-suggest v1 (reviewed)'),
    ('genomics-catalog v2', 'genomics-catalog v2'),
])
def test_portable_provenance_drops_people_only(stored, portable):
    from omop_core.services.field_mapping_provenance import portable_provenance
    assert portable_provenance(stored) == portable


def test_transfer_carries_no_approver_identity():
    from omop_core.models import SourceCodeConceptMapping
    FieldConceptMapping.objects.create(field_name='hemoglobin_g_dl', status='approved',
                                       provenance='approver@example.org')
    FieldConceptMapping.objects.create(field_name='platelet_count',
                                       provenance=suggestion_provenance('reviewed'))
    SourceCodeConceptMapping.objects.create(source_vocabulary_id='LOCAL', source_code='A',
                                            origin_system='approver@example.org', status='approved')
    SourceCodeConceptMapping.objects.create(source_vocabulary_id='LOCAL', source_code='B',
                                            origin_system='suggest v0.4')
    payload = read_payload('default', tables=('mappings', 'code_mappings'))
    provenance = {m['field_name']: m['provenance'] for m in payload['mappings']}
    assert provenance == {'hemoglobin_g_dl': 'curator', 'platelet_count': 'field-suggest v1 (reviewed)'}
    origins = {m['source_code']: m['origin_system'] for m in payload['code_mappings']}
    assert origins == {'A': 'curator', 'B': 'suggest v0.4'}
    assert 'approver@example.org' not in repr(payload)


def test_inventory_export_carries_no_approver_identity():
    from omop_core.management.commands.export_field_mapping_inventory import collect_reference_tables
    FieldConceptMapping.objects.create(field_name='hemoglobin_g_dl', status='approved',
                                       provenance='approver@example.org')
    tables, _ = collect_reference_tables()
    rows = {r['field_name']: r for r in tables['field_concept_mapping']}
    assert rows['hemoglobin_g_dl']['provenance'] == 'curator'
    assert 'approver@example.org' not in repr(tables)


def test_migration_leaves_a_curator_edit_unattributed():
    reviewer = Identity.objects.create_user(email='reviewer@example.org')
    edited = FieldConceptMapping.objects.create(
        field_name='hemoglobin_g_dl', provenance='curator', status='approved', reviewer=reviewer)
    blank = FieldConceptMapping.objects.create(
        field_name='platelet_count', provenance='', status='approved', reviewer=reviewer)
    migration = importlib.import_module('omop_core.migrations.0279_field_mapping_provenance_text')
    migration.stamp_approvers(apps, None)
    edited.refresh_from_db(); blank.refresh_from_db()
    assert edited.provenance == 'curator', 'the reviewer may not be who made the later edit'
    assert blank.provenance == 'reviewer@example.org'


def test_transfer_drops_any_code_mapping_approver_label():
    """#1708 stamps email, else issuer|sub, else a name; none may travel."""
    from omop_core.models import SourceCodeConceptMapping
    Identity.objects.create(email='', name='Ada Lovelace', issuer='https://idp.example', sub='ada-1')
    for code, origin in (('A', 'https://idp.example|ada-1'), ('B', 'Ada Lovelace'),
                         ('C', 'HT-One'), ('D', 'athena')):
        SourceCodeConceptMapping.objects.create(source_vocabulary_id='LOCAL', source_code=code,
                                                origin_system=origin)
    payload = read_payload('default', tables=('code_mappings',))
    origins = {m['source_code']: m['origin_system'] for m in payload['code_mappings']}
    assert origins == {'A': 'curator', 'B': 'curator', 'C': 'HT-One', 'D': 'athena'}


def test_migration_reverse_folds_back_to_the_two_old_labels():
    rows = {provenance: FieldConceptMapping.objects.create(field_name=field, provenance=provenance)
            for field, provenance in (('hemoglobin_g_dl', 'field-suggest v1 (propose-all)'),
                                      ('platelet_count', 'genomics-catalog v2'),
                                      ('wbc_count', 'approver@example.org'),
                                      ('albumin', 'curator'), ('creatinine', 'system_generated'),
                                      ('calcium', ''))}
    migration = importlib.import_module('omop_core.migrations.0279_field_mapping_provenance_text')
    migration.restore_two_labels(apps, None)
    after = {before: FieldConceptMapping.objects.get(pk=row.pk).provenance for before, row in rows.items()}
    assert after == {
        'field-suggest v1 (propose-all)': 'system_generated', 'genomics-catalog v2': 'system_generated',
        'approver@example.org': 'curator', 'curator': 'curator', 'system_generated': 'system_generated', '': '',
    }
    assert all(len(value) <= 20 for value in after.values())
