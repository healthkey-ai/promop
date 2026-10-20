"""Source-owned inputs on the frozen integration schema; no modern-dev dependencies."""
from datetime import date, timedelta
from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError

from omop_core.models import Concept, Observation, PatientRecord
from omop_core.services.clinical_input_contract import (
    CLINICAL_INPUT_FIELDS, RECIPES, write_clinical_inputs,
)
from omop_core.services.field_write_service import apply_field_writes
from omop_core.services.patient_record_service import refresh_patient_record
from omop_core.services.write_descriptor import build_writable_field_descriptor
from tests.factories import ConceptFactory, PatientRecordFactory, PersonFactory, VocabularyFactory

pytestmark = pytest.mark.django_db


@pytest.fixture
def patient():
    person = PersonFactory()
    PatientRecordFactory(person=person)
    if not Concept.objects.filter(pk=0).exists():
        ConceptFactory(concept_id=0, concept_code='0', concept_name='No matching concept')
    if not Concept.objects.filter(pk=32865).exists():
        vocab, _ = VocabularyFactory._meta.model.objects.get_or_create(
            vocabulary_id='Type Concept', defaults={'vocabulary_name': 'Type Concept'},
        )
        ConceptFactory(concept_id=32865, concept_code='32865', vocabulary=vocab)
    return person


@pytest.mark.parametrize('field,value', [
    ('prothrombin_time_uln', 1.12345),
    ('activated_partial_thromboplastin_time_uln', Decimal('1.5')),
    ('able_to_swallow_tablets_capsules', False),
    ('able_to_swallow_tablets_capsules', True),
    ('sct_date', date(2026, 1, 2)),
])
def test_host_and_field_applier_round_trip_and_clear(patient, field, value):
    d = build_writable_field_descriptor()[field]
    assert d['writable'] is True
    assert d['source_value'] == RECIPES[field][0]
    result = apply_field_writes(patient, {field: value})
    assert result.applied == [field]
    expected = Decimal(str(value)) if RECIPES[field][1] == 'number' else value
    assert getattr(refresh_patient_record(patient), field) == expected
    fact = Observation.objects.get(person=patient)
    assert fact.observation_type_concept_id == 32865
    assert fact.observation_concept_id == 0
    write_clinical_inputs(patient, {field: None})
    assert getattr(refresh_patient_record(patient), field) is None
    assert Observation.objects.filter(person=patient).count() == 2
    fact.refresh_from_db()
    assert fact.value_source_value is None


def test_missing_fact_is_unknown_not_default_true(patient):
    record = refresh_patient_record(patient)
    assert record.able_to_swallow_tablets_capsules is None
    assert record.prothrombin_time_uln is None


@pytest.mark.parametrize('invalid', [0, -1, 'NaN', 'Infinity', 1.123456, True])
def test_invalid_ratios_rollback_entire_mixed_input_batch(patient, invalid):
    with pytest.raises((ValueError, ValidationError)):
        write_clinical_inputs(patient, {
            'able_to_swallow_tablets_capsules': False, 'prothrombin_time_uln': invalid,
        })
    assert Observation.objects.filter(person=patient).count() == 0
    assert PatientRecord.objects.get(person=patient).able_to_swallow_tablets_capsules is None


def test_same_day_import_survives_correction_and_clear(patient):
    imported = Observation(
        observation_id=9900001, person=patient, observation_concept_id=0,
        observation_type_concept_id=32817, observation_date=date.today(),
        observation_source_value='mm-sct-date', value_as_string='2026-01-01',
    )
    imported._skip_patient_record_refresh = True
    imported.save()
    assert refresh_patient_record(patient).sct_date == date(2026, 1, 1)
    record = write_clinical_inputs(patient, {'sct_date': date(2026, 1, 2)})
    assert record.sct_date == date(2026, 1, 2)
    assert write_clinical_inputs(patient, {'sct_date': None}).sct_date is None
    imported.refresh_from_db()
    assert imported.value_as_string == '2026-01-01'
    assert imported.observation_type_concept_id == 32817
    assert Observation.objects.filter(person=patient).count() == 3


def test_bad_type_vocabulary_cannot_be_used_as_self_report(patient):
    vocab = VocabularyFactory(vocabulary_id='Wrong type')
    Concept.objects.filter(pk=32865).update(vocabulary=vocab)
    assert not build_writable_field_descriptor()['sct_date']['writable']
    with pytest.raises(ValueError, match='Type Concept'):
        write_clinical_inputs(patient, {'able_to_swallow_tablets_capsules': False})
    assert Observation.objects.filter(person=patient).count() == 0


def test_equal_value_retry_writes_a_fact_not_just_projection(patient):
    PatientRecord.objects.filter(person=patient).update(prothrombin_time_uln=Decimal('1.2'))
    write_clinical_inputs(patient, {'prothrombin_time_uln': 1.2})
    assert Observation.objects.filter(person=patient).count() == 1
    assert refresh_patient_record(patient).prothrombin_time_uln == Decimal('1.2')


def test_future_dated_fact_cannot_turn_write_into_false_ack(patient):
    fact = Observation(
        observation_id=9900002, person=patient, observation_concept_id=0,
        observation_type_concept_id=32817, observation_date=date.today() + timedelta(days=2),
        observation_source_value='cb:swallow-tablets', value_as_string='true',
    )
    fact._skip_patient_record_refresh = True
    fact.save()
    from django.core.exceptions import ValidationError
    with pytest.raises(ValidationError, match='round-trip'):
        write_clinical_inputs(patient, {'able_to_swallow_tablets_capsules': False})
    assert Observation.objects.filter(person=patient).count() == 1


def test_smallest_positive_ratio_keeps_decimal_precision(patient):
    assert write_clinical_inputs(patient, {'prothrombin_time_uln': 0.00001}).prothrombin_time_uln == Decimal('0.00001')


def test_only_explicit_contract_fields_are_writable_here(patient):
    with pytest.raises(ValueError, match='Unsupported'):
        write_clinical_inputs(patient, {'hepatic_adequacy_status': True})
    assert len(CLINICAL_INPUT_FIELDS) == 4


def test_new_inputs_have_source_provenance(patient):
    from omop_core.services.provenance_registry import get_registry
    registry = get_registry()
    for field in CLINICAL_INPUT_FIELDS - {'sct_date'}:
        provenance = registry[field]
        assert provenance.omop_table == 'Observation'
        assert provenance.source_values == [RECIPES[field][0]]


@pytest.mark.parametrize('field,value', [
    ('prothrombin_time_uln', Decimal('1.25')),
    ('activated_partial_thromboplastin_time_uln', Decimal('1.75')),
    ('able_to_swallow_tablets_capsules', True),
])
def test_new_inputs_are_traceable_to_their_observation(patient, field, value):
    from omop_core.services.provenance_service import get_field_provenance

    write_clinical_inputs(patient, {field: value})
    provenance = get_field_provenance(patient, field)

    assert provenance is not None
    assert len(provenance['source_rows']) == 1
    row = provenance['source_rows'][0]
    assert row['table'] == 'Observation'
    assert row['id'] == Observation.objects.get(person=patient).observation_id


def test_provenance_selected_row_is_the_latest_same_day_input(patient):
    from omop_core.services.provenance_service import get_field_provenance

    write_clinical_inputs(patient, {'prothrombin_time_uln': Decimal('1.25')})
    write_clinical_inputs(patient, {'prothrombin_time_uln': Decimal('1.30')})

    provenance = get_field_provenance(patient, 'prothrombin_time_uln')

    assert len(provenance['source_rows']) == 2
    assert provenance['source_rows'][0]['value'] == Decimal('1.30')
    assert provenance['source_rows'][0]['selected'] is True
    assert provenance['source_rows'][1]['selected'] is False


def test_clinical_write_is_rejected_when_descriptor_does_not_list_the_field(patient):
    result = apply_field_writes(
        patient, {'prothrombin_time_uln': Decimal('1.25')}, descriptor={},
    )

    assert result.applied == []
    assert 'prothrombin_time_uln' in result.rejected
    assert not Observation.objects.filter(person=patient).exists()


def test_clinical_write_is_rejected_when_descriptor_marks_it_not_writable(patient):
    descriptor = {
        'prothrombin_time_uln': {
            'kind': 'editable',
            'writable': False,
            'reason': 'Not editable in this context.',
        },
    }

    result = apply_field_writes(
        patient, {'prothrombin_time_uln': Decimal('1.25')}, descriptor=descriptor,
    )

    assert result.applied == []
    assert result.rejected == {'prothrombin_time_uln': 'Not editable in this context.'}
    assert not Observation.objects.filter(person=patient).exists()


def test_clinical_write_is_rejected_when_descriptor_kind_is_not_editable(patient):
    descriptor = {
        'able_to_swallow_tablets_capsules': {'kind': 'profile', 'writable': True},
    }

    result = apply_field_writes(
        patient, {'able_to_swallow_tablets_capsules': True}, descriptor=descriptor,
    )

    assert result.applied == []
    assert 'able_to_swallow_tablets_capsules' in result.rejected
    assert not Observation.objects.filter(person=patient).exists()


def test_clinical_write_accepts_a_descriptor_that_marks_it_writable(patient):
    descriptor = {'prothrombin_time_uln': {'kind': 'editable', 'writable': True}}

    result = apply_field_writes(
        patient, {'prothrombin_time_uln': Decimal('1.25')}, descriptor=descriptor,
    )

    assert result.applied == ['prothrombin_time_uln']
    assert not result.rejected
    assert Observation.objects.filter(person=patient).count() == 1


def test_mixed_batch_rolls_back_the_nonclinical_part_when_a_clinical_value_is_invalid(patient):
    patient.gender_source_value = 'Male'
    patient.save(update_fields=['gender_source_value'])

    with pytest.raises(ValueError, match='explicit boolean'):
        apply_field_writes(
            patient,
            {'gender': 'Female', 'able_to_swallow_tablets_capsules': 'true'},
        )

    patient.refresh_from_db()
    assert patient.gender_source_value == 'Male'
    assert not Observation.objects.filter(person=patient).exists()


def test_mixed_batch_applies_the_nonclinical_and_clinical_parts(patient):
    result = apply_field_writes(
        patient,
        {'gender': 'Female', 'prothrombin_time_uln': Decimal('1.25')},
    )

    assert set(result.applied) == {'gender', 'prothrombin_time_uln'}
    assert not result.rejected
    assert Observation.objects.filter(person=patient).count() == 1


@pytest.mark.parametrize('invalid', ['true', 0, 1, [], {}])
def test_swallowing_requires_an_explicit_boolean_not_a_truthy_value(patient, invalid):
    with pytest.raises(ValueError, match='explicit boolean'):
        write_clinical_inputs(patient, {'able_to_swallow_tablets_capsules': invalid})
    assert not Observation.objects.filter(person=patient).exists()
