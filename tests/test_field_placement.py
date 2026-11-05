"""Field placement: which patient tab a field renders on (#2328).

Placement is one value per field, the tab. A placed field renders last on its
tab, on its own row; there is no section or order to curate. A field nothing
places is ``omop``, unplaced: it keeps its mapping and derivation, and its
facts show only as raw OMOP rows.
"""
import pytest
from rest_framework.test import APIClient

from omop_core.models import (
    UNPLACED_TAB, CustomPatientField, FieldConceptMapping,
)
from omop_core.services.field_curation_transfer import apply_payload, read_payload
from omop_core.services.field_descriptor import (
    _classify_tab, get_all_field_descriptors, resolve_tab,
)
from patient_portal.models import Identity
from tests.factories import ConceptFactory

pytestmark = pytest.mark.django_db


def _descriptor(name):
    return next(d for d in get_all_field_descriptors() if d['field_name'] == name)


def test_a_field_the_code_does_not_place_is_unplaced_not_other():
    # Fields on no tab list (blood_type, red_blood_cell_count, pod24, ...)
    # used to fall to 'other', which the Field Mappings page has no tab for.
    assert _classify_tab('median_daily_steps_30d') == 'behavior'
    assert _classify_tab('red_blood_cell_count') == UNPLACED_TAB
    assert _descriptor('red_blood_cell_count')['tab'] == 'omop'
    assert _descriptor('blood_type')['tab'] == 'omop'
    assert 'other' not in {d['tab'] for d in get_all_field_descriptors()}


def test_a_curators_tab_on_the_mapping_overrides_the_codes_placement():
    assert _descriptor('hemoglobin_g_dl')['tab'] == 'blood'
    FieldConceptMapping.objects.create(field_name='hemoglobin_g_dl', tab='labs')
    assert _descriptor('hemoglobin_g_dl')['tab'] == 'labs'
    assert _descriptor('hemoglobin_g_dl')['mapping']['tab'] == 'labs'


def test_a_blank_tab_on_the_mapping_keeps_the_codes_placement():
    FieldConceptMapping.objects.create(field_name='hemoglobin_g_dl')
    assert _descriptor('hemoglobin_g_dl')['tab'] == 'blood'


def test_a_custom_field_renders_on_its_own_tab_unless_the_mapping_says_otherwise():
    concept = ConceptFactory(vocabulary_id='LOINC', concept_code='2160-0')
    mapping = FieldConceptMapping.objects.create(
        field_name='frailty_index', concept=concept, omop_table='observation',
        source_value='frailty_index', value_kind='number', status='approved',
    )
    custom = CustomPatientField.objects.create(
        field_name='frailty_index', display_name='Frailty Index', tab='general',
        field_type='number', mapping=mapping,
    )
    assert resolve_tab('frailty_index', mapping=mapping, custom=custom) == 'general'
    assert _descriptor('frailty_index')['tab'] == 'general'
    mapping.tab = 'disease'
    mapping.save()
    assert _descriptor('frailty_index')['tab'] == 'disease'


def test_a_custom_field_is_unplaced_by_default():
    concept = ConceptFactory(vocabulary_id='LOINC', concept_code='2160-0')
    mapping = FieldConceptMapping.objects.create(
        field_name='frailty_index', concept=concept, omop_table='observation',
        source_value='frailty_index', value_kind='number', status='approved',
    )
    custom = CustomPatientField.objects.create(
        field_name='frailty_index', display_name='Frailty Index',
        field_type='number', mapping=mapping,
    )
    assert custom.tab == UNPLACED_TAB
    assert _descriptor('frailty_index')['tab'] == 'omop'


# ── Transfer ──────────────────────────────────────────────────────────────

def test_placement_travels_with_the_mapping_and_an_older_payload_reads_as_blank():
    concept = ConceptFactory(vocabulary_id='LOINC', concept_code='2160-0')
    FieldConceptMapping.objects.create(
        field_name='creatinine', concept=concept, vocabulary_id='LOINC',
        concept_code='2160-0', omop_table='measurement', source_value='creatinine',
        value_kind='number', status='approved', tab='labs',
    )
    payload = read_payload('default', tables=('mappings',))
    row = next(r for r in payload['mappings'] if r['field_name'] == 'creatinine')
    assert row['tab'] == 'labs'

    FieldConceptMapping.objects.all().delete()
    apply_payload(payload)
    assert FieldConceptMapping.objects.get(field_name='creatinine').tab == 'labs'

    # A fixture written before the column existed (field_curation_v1.json).
    del row['tab']
    FieldConceptMapping.objects.all().delete()
    apply_payload(payload)
    assert FieldConceptMapping.objects.get(field_name='creatinine').tab == ''


# ── API ───────────────────────────────────────────────────────────────────

@pytest.fixture
def staff():
    user = Identity.objects.create_user(email='curator@example.org', is_staff=True)
    client = APIClient()
    client.force_authenticate(user)
    return client


def test_the_mapping_api_sets_and_reports_the_tab(staff):
    row = FieldConceptMapping.objects.create(field_name='hemoglobin_g_dl')
    response = staff.patch(f'/api/v1/field-mappings/{row.pk}/', {'tab': 'labs'}, format='json')
    assert response.status_code == 200, response.data
    assert response.data['tab'] == 'labs'
    row.refresh_from_db()
    assert row.tab == 'labs'

    listed = {d['field_name']: d for d in staff.get('/api/v1/field-mappings/').data}
    assert listed['hemoglobin_g_dl']['tab'] == 'labs'


def test_the_mapping_api_refuses_a_tab_that_is_not_a_patient_tab(staff):
    row = FieldConceptMapping.objects.create(field_name='hemoglobin_g_dl')
    response = staff.patch(f'/api/v1/field-mappings/{row.pk}/', {'tab': 'sidebar'}, format='json')
    assert response.status_code == 400
    assert 'tab' in response.data


def test_creating_a_custom_field_without_a_tab_leaves_it_unplaced(staff):
    concept = ConceptFactory(vocabulary_id='LOINC', concept_code='2160-0')
    payload = {
        'confirm_patient_record': True, 'field_name': 'frailty_index',
        'display_name': 'Frailty Index', 'field_type': 'number',
        'concept': concept.concept_id, 'omop_table': 'observation',
    }
    response = staff.post('/api/v1/custom-patient-fields/', payload, format='json')
    assert response.status_code == 201, response.data
    custom = CustomPatientField.objects.get(field_name='frailty_index')
    assert custom.tab == UNPLACED_TAB
    assert custom.mapping.tab == UNPLACED_TAB
    assert _descriptor('frailty_index')['tab'] == 'omop'

    response = staff.post('/api/v1/custom-patient-fields/', {
        **payload, 'field_name': 'frailty_note', 'display_name': 'Frailty note',
        'field_type': 'text', 'tab': 'disease',
    }, format='json')
    assert response.status_code == 201, response.data
    assert CustomPatientField.objects.get(field_name='frailty_note').mapping.tab == 'disease'


# ── Review follow-ups ─────────────────────────────────────────────────────

def test_patching_a_custom_fields_mapping_tab_moves_the_custom_field_too(staff):
    concept = ConceptFactory(vocabulary_id='LOINC', concept_code='2160-0')
    mapping = FieldConceptMapping.objects.create(
        field_name='frailty_index', concept=concept, omop_table='observation',
        source_value='frailty_index', value_kind='number', status='approved', tab='general',
    )
    CustomPatientField.objects.create(
        field_name='frailty_index', display_name='Frailty Index', tab='general',
        field_type='number', mapping=mapping,
    )
    response = staff.patch(f'/api/v1/field-mappings/{mapping.pk}/', {'tab': 'disease'}, format='json')
    assert response.status_code == 200, response.data
    listed = {d['field_name']: d for d in staff.get('/api/v1/custom-patient-fields/').data}
    assert listed['frailty_index']['tab'] == 'disease'
    assert _descriptor('frailty_index')['tab'] == 'disease'

    response = staff.patch(f'/api/v1/field-mappings/{mapping.pk}/', {'tab': ''}, format='json')
    assert response.status_code == 200, response.data
    assert CustomPatientField.objects.get(field_name='frailty_index').tab == UNPLACED_TAB


def test_an_older_payload_leaves_a_curated_placement_alone_on_update():
    concept = ConceptFactory(vocabulary_id='LOINC', concept_code='2160-0')
    FieldConceptMapping.objects.create(
        field_name='creatinine', concept=concept, vocabulary_id='LOINC',
        concept_code='2160-0', omop_table='measurement', source_value='creatinine',
        value_kind='number', status='approved', tab='labs',
    )
    payload = read_payload('default', tables=('mappings',))
    row = next(r for r in payload['mappings'] if r['field_name'] == 'creatinine')
    del row['tab']
    row['notes'] = 'from the older instance'
    apply_payload(payload)
    mapping = FieldConceptMapping.objects.get(field_name='creatinine')
    assert mapping.notes == 'from the older instance'
    assert mapping.tab == 'labs'


def test_disease_relevance_counts_a_field_where_the_curator_placed_it():
    from omop_core.services.disease_relevance import _common_fields

    concept = ConceptFactory(vocabulary_id='LOINC', concept_code='882-1')
    # blood_type is unplaced by the code; placed on General it becomes common.
    FieldConceptMapping.objects.create(
        field_name='blood_type', concept=concept, omop_table='observation',
        status='approved', tab='general',
    )
    # hemoglobin_g_dl is on Blood by the code; moved to unplaced it drops out.
    FieldConceptMapping.objects.create(
        field_name='hemoglobin_g_dl', concept=concept, omop_table='measurement',
        status='approved', tab=UNPLACED_TAB,
    )
    common = _common_fields(['blood_type', 'hemoglobin_g_dl', 'serum_creatinine_level'])
    assert 'blood_type' in common
    assert 'hemoglobin_g_dl' not in common
    assert 'serum_creatinine_level' in common
