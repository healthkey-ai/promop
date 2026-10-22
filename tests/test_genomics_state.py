"""Source-preserving finding state across legacy facts and interactive routes."""
import pytest
from rest_framework.exceptions import ValidationError
from rest_framework.test import APIClient

from omop_core.models import Measurement, Observation
from omop_core.services.genomics import list_variants, save_variant
from omop_core.services.patient_record_service import refresh_patient_record
from tests.test_genomics_crud import setup  # noqa: F401

pytestmark = pytest.mark.django_db


@pytest.mark.parametrize('assessment,expected', [
    ('present', 'present'), ('absent', 'absent'), ('no_call', 'indeterminate'),
    ('not_tested', 'indeterminate'), ('indeterminate', 'indeterminate'),
])
def test_legacy_assessment_supplies_state_without_changing_source_facts(setup, assessment, expected):
    person, record, _ = setup
    saved = save_variant(person, {'gene': 'BRCA1', 'variant': 'source text', 'assessment': assessment})
    # Emulate the historical shape with only the assessment component.
    Measurement.objects.filter(person=person, measurement_source_value='genomics:status').update(is_erroneous=True)
    before = list(Observation.objects.filter(person=person).values('observation_id', 'value_as_string', 'is_erroneous'))
    refresh_patient_record(person)
    actual = list_variants(person)[0]
    assert actual['id'] == saved['id']
    assert actual['assessment'] == assessment
    assert actual['status'] == expected
    assert list(Observation.objects.filter(person=person).values('observation_id', 'value_as_string', 'is_erroneous')) == before
    record.refresh_from_db()
    assert record.genomics_brca1[0]['status'] == expected
    assert bool(record.molecular_markers) == (expected == 'present')


@pytest.mark.parametrize('route', ['dedicated', 'general', 'named'])
def test_state_transitions_clear_inherited_components_and_detected_summary(setup, route):
    person, record, staff = setup
    saved = save_variant(person, {
        'gene': 'BRCA1', 'variant': 'original finding', 'assessment': 'present',
        'transcript_dna_change': 'c.123A>G', 'amino_acid_change_type': 'missense',
        'zygosity': 'Heterozygous', 'coverage_depth': 200,
    })
    record.refresh_from_db()
    assert record.molecular_markers
    client = APIClient()
    client.force_authenticate(staff)
    payload = {'id': saved['id'], 'gene': 'BRCA1', 'status': 'absent'}
    if route == 'dedicated':
        response = client.patch(f'/api/v1/patient-records/{person.pk}/genomics/{saved["id"]}/', payload, format='json')
    else:
        field = 'genetic_mutations' if route == 'general' else 'genomics_brca1'
        response = client.patch(f'/api/v1/patient-records/{person.pk}/', {field: [payload]}, format='json')
    assert response.status_code == 200, response.data
    current = list_variants(person)[0]
    assert current['status'] == 'absent'
    for key in ('assessment', 'transcript_dna_change', 'amino_acid_change_type', 'zygosity'):
        assert not current.get(key)
    assert current['coverage_depth'] == 200  # Scope/quality context remains applicable.
    record.refresh_from_db()
    assert not record.molecular_markers
    assert Observation.objects.filter(person=person, observation_source_value='genomics:assessment',
                                      value_as_string='present', is_erroneous=True).exists()
    assert Measurement.objects.filter(person=person, measurement_source_value='genomics:transcript_dna_change',
                                      value_as_string='c.123A>G', is_erroneous=True).exists()


@pytest.mark.parametrize('status,assessment', [
    ('present', 'absent'), ('present', 'no_call'), ('absent', 'not_tested'), ('indeterminate', 'present'),
])
def test_contradictory_new_states_are_rejected_atomically(setup, status, assessment):
    person, _, _ = setup
    with pytest.raises(ValidationError, match='contradicts'):
        save_variant(person, {'gene': 'BRCA1', 'status': status, 'assessment': assessment})
    assert not Measurement.objects.filter(person=person).exists()
    assert list_variants(person) == []


@pytest.mark.parametrize('field,value', [
    ('transcript_dna_change', 'c.123A>G'), ('amino_acid_change_type', 'missense'), ('zygosity', 'Heterozygous'),
])
def test_new_variant_specific_components_are_inapplicable_to_absence(setup, field, value):
    person, _, _ = setup
    with pytest.raises(ValidationError, match='absent finding'):
        save_variant(person, {'gene': 'BRCA1', 'status': 'absent', field: value})
    assert list_variants(person) == []


def test_explicit_state_wins_in_legacy_conflict_but_edits_require_resolution(setup):
    person, record, _ = setup
    saved = save_variant(person, {'gene': 'BRCA1', 'status': 'absent', 'assessment': 'absent'})
    Measurement.objects.filter(person=person, measurement_source_value='genomics:status').update(value_as_string='present')
    refresh_patient_record(person)
    legacy = list_variants(person)[0]
    assert legacy['status'] == 'present'
    assert legacy['assessment'] == 'absent'
    with pytest.raises(ValidationError, match='contradicts'):
        save_variant(person, legacy, variant_id=saved['id'])
    changed = save_variant(person, {'status': 'absent'}, variant_id=saved['id'])
    assert changed['status'] == 'absent'
    record.refresh_from_db()
    assert not record.molecular_markers


def test_unrecognized_legacy_assessment_is_indeterminate_and_preserved(setup):
    person, _, _ = setup
    save_variant(person, {'gene': 'BRCA1', 'assessment': 'no_call'})
    Measurement.objects.filter(person=person, measurement_source_value='genomics:status').update(is_erroneous=True)
    Observation.objects.filter(person=person, observation_source_value='genomics:assessment').update(value_as_string='below source reporting threshold')
    actual = list_variants(person)[0]
    assert actual['status'] == 'indeterminate'
    assert actual['assessment'] == 'below source reporting threshold'
    edited = save_variant(person, {'laboratory': 'Source lab'}, variant_id=actual['id'])
    assert edited['assessment'] == actual['assessment']
    assert edited['status'] == 'indeterminate'
