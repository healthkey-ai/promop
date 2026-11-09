"""#5608: a lab result keeps its clinical date through the shared form.

`apply_patient_form_patch` used to stamp every fact with the day it was typed.
These tests anchor the dated envelope to the derivation: a supplied date is the
measurement date, a correction at the latest date updates that row, and an older
result is stored as history rather than promoted to the current value
(docs/clinical-event-time-policy.md).
"""
from datetime import date, timedelta
from decimal import Decimal

import pytest

from omop_core.models import Concept, Measurement, PatientRecord
from omop_core.services.patient_form import apply_patient_form_patch, patient_form_descriptors
from omop_core.services.pk import next_pk
from omop_core.services.patient_record_service import refresh_patient_record
from tests.factories import ConceptFactory, PatientRecordFactory, PersonFactory, VocabularyFactory

pytestmark = pytest.mark.django_db

FIELD = 'hemoglobin_g_dl'
LOINC = '718-7'


def _person_with_lab():
    person = PersonFactory()
    PatientRecordFactory(person=person)
    ConceptFactory(concept_code=LOINC, concept_name='Hemoglobin [Mass/volume] in Blood')
    type_vocab = VocabularyFactory(
        vocabulary_id='Type Concept', vocabulary_name='Type Concept')
    ConceptFactory(concept_id=32865, concept_code='PT-SELF-REPORT',
                   concept_name='Patient self-report', vocabulary=type_vocab)
    return person


def _fact(person):
    return Measurement.objects.get(person=person, measurement_concept_id__isnull=False)


def test_the_base_descriptor_marks_a_scalar_fact_as_dated():
    from omop_core.services.write_descriptor import build_writable_field_descriptor

    _person_with_lab()

    assert build_writable_field_descriptor()[FIELD]['dated'] is True


def test_a_dedicated_clinical_input_is_not_dated():
    from omop_core.services.clinical_input_contract import CLINICAL_INPUT_FIELDS
    from omop_core.services.write_descriptor import build_writable_field_descriptor

    _person_with_lab()
    descriptor = build_writable_field_descriptor()

    marked = {name for name in CLINICAL_INPUT_FIELDS
              if descriptor.get(name, {}).get('dated')}
    assert not marked, f'dedicated writers must not take a caller-supplied date: {marked}'


def test_the_descriptor_names_the_latest_recorded_date_for_a_dated_lab():
    person = _person_with_lab()
    concept = Concept.objects.get(concept_code=LOINC)
    Measurement.objects.create(
        measurement_id=next_pk(Measurement, 'measurement_id'), person=person,
        measurement_concept=concept, measurement_date=date(2026, 8, 1),
        measurement_type_concept_id=32865, value_as_number=12.5)
    record = PatientRecord.objects.get(person=person)

    entry = patient_form_descriptors(readable_fields={FIELD}, record=record)[FIELD]

    assert entry['dated'] is True
    assert entry['latest_date'] == '2026-08-01'


def test_a_newer_fact_without_a_value_does_not_move_the_correction_date():
    person = _person_with_lab()
    concept = Concept.objects.get(concept_code=LOINC)
    numeric = Measurement.objects.create(
        measurement_id=next_pk(Measurement, 'measurement_id'), person=person,
        measurement_concept=concept, measurement_date=date(2026, 7, 1),
        measurement_type_concept_id=32865, value_as_number=12.5)
    Measurement.objects.create(
        measurement_id=next_pk(Measurement, 'measurement_id'), person=person,
        measurement_concept=concept, measurement_date=date(2026, 8, 1),
        measurement_type_concept_id=32865, value_as_number=None)
    record = PatientRecord.objects.get(person=person)

    entry = patient_form_descriptors(readable_fields={FIELD}, record=record)[FIELD]

    assert entry['latest_date'] == numeric.measurement_date.isoformat()


def test_the_date_follows_a_legacy_named_fact_that_supplies_the_value():
    from tests.factories import ConceptFactory

    person = _person_with_lab()
    legacy = ConceptFactory(concept_code='HGB', concept_name='Hemoglobin').concept_id
    Measurement.objects.create(
        measurement_id=next_pk(Measurement, 'measurement_id'), person=person,
        measurement_concept_id=legacy, measurement_date=date(2026, 7, 5),
        measurement_type_concept_id=32865, value_as_number=12.0,
        unit_source_value='g/dL')
    record = PatientRecord.objects.get(person=person)

    entry = patient_form_descriptors(readable_fields={FIELD}, record=record)[FIELD]

    assert entry['latest_date'] == '2026-07-05'


def test_an_older_coded_result_does_not_replace_a_newer_display_name_result():
    person = _person_with_lab()
    coded = Concept.objects.get(concept_code=LOINC)
    display_only = ConceptFactory(concept_code='LOCAL-HGB', concept_name='Imported hemoglobin')
    Measurement.objects.create(
        measurement_id=next_pk(Measurement, 'measurement_id'), person=person,
        measurement_concept=coded, measurement_date=date(2026, 8, 1),
        measurement_type_concept_id=32865, value_as_number=12.0,
        unit_source_value='g/dL')
    Measurement.objects.create(
        measurement_id=next_pk(Measurement, 'measurement_id'), person=person,
        measurement_concept=display_only, measurement_source_value=
        'Hemoglobin [Mass/volume] in Blood', measurement_date=date(2026, 9, 1),
        measurement_type_concept_id=32865, value_as_number=13.0,
        unit_source_value='g/dL')

    record = refresh_patient_record(person)
    entry = patient_form_descriptors(
        readable_fields={FIELD}, record=record)[FIELD]

    assert float(record.hemoglobin_g_dl) == pytest.approx(13.0)
    assert entry['latest_date'] == '2026-09-01'


def test_an_older_dated_write_is_history_across_coded_and_display_sources():
    person = _person_with_lab()
    display_only = ConceptFactory(concept_code='LOCAL-HGB', concept_name='Imported hemoglobin')
    Measurement.objects.create(
        measurement_id=next_pk(Measurement, 'measurement_id'), person=person,
        measurement_concept=display_only, measurement_source_value=
        'Hemoglobin [Mass/volume] in Blood', measurement_date=date(2026, 9, 1),
        measurement_type_concept_id=32865, value_as_number=13.0,
        unit_source_value='g/dL')

    apply_patient_form_patch(
        person, {FIELD: {'value': 12.0, 'date': '2026-08-01'}}, allowed_fields={FIELD})

    assert float(refresh_patient_record(person).hemoglobin_g_dl) == pytest.approx(13.0)


def test_newer_display_name_wbc_replaces_coded_value_with_normalized_pair_and_date():
    person = _person_with_lab()
    coded = ConceptFactory(concept_code='6690-2', concept_name='Leukocytes')
    display_only = ConceptFactory(concept_code='LOCAL-WBC', concept_name='Imported leukocytes')
    Measurement.objects.create(
        measurement_id=next_pk(Measurement, 'measurement_id'), person=person,
        measurement_concept=coded, measurement_source_value='6690-2',
        measurement_date=date(2026, 8, 1), measurement_type_concept_id=32865,
        value_as_number=5, unit_source_value='10*3/uL')
    Measurement.objects.create(
        measurement_id=next_pk(Measurement, 'measurement_id'), person=person,
        measurement_concept=display_only, measurement_source_value=
        'Leukocytes [#/volume] in Blood', measurement_date=date(2026, 9, 1),
        measurement_type_concept_id=32865, value_as_number=6000,
        unit_source_value='cells/uL')

    record = refresh_patient_record(person)
    entry = patient_form_descriptors(
        readable_fields={'wbc_count_thousand_per_ul'}, record=record)[
            'wbc_count_thousand_per_ul']

    assert float(record.wbc_count_thousand_per_ul) == pytest.approx(6.0)
    assert float(record.white_blood_cell_count) == pytest.approx(6.0)
    assert entry['latest_date'] == '2026-09-01'


def test_unsupported_newer_wbc_blocks_older_display_alias():
    person = _person_with_lab()
    old_concept = ConceptFactory(concept_code='LOCAL-WBC-OLD', concept_name='Old WBC')
    new_concept = ConceptFactory(concept_code='LOCAL-WBC-NEW', concept_name='New WBC')
    Measurement.objects.create(
        measurement_id=next_pk(Measurement, 'measurement_id'), person=person,
        measurement_concept=old_concept, measurement_source_value='White blood cell count',
        measurement_date=date(2026, 8, 1), measurement_type_concept_id=32865,
        value_as_number=5, unit_source_value='10*3/uL')
    Measurement.objects.create(
        measurement_id=next_pk(Measurement, 'measurement_id'), person=person,
        measurement_concept=new_concept,
        measurement_source_value='Leukocytes [#/volume] in Blood',
        measurement_date=date(2026, 9, 1), measurement_type_concept_id=32865,
        value_as_number=6000, unit_source_value='unknown')

    record = refresh_patient_record(person)

    assert record.wbc_count_thousand_per_ul is None
    assert record.white_blood_cell_count is None


def test_a_restricted_readable_set_does_not_name_an_excluded_count():
    from tests.factories import ConceptFactory

    person = _person_with_lab()
    ConceptFactory(concept_code='751-8', concept_name='Absolute neutrophil count')
    ConceptFactory(concept_code='777-3', concept_name='Platelet count')
    for code, value in (('751-8', 4.2), ('777-3', 150)):
        Measurement.objects.create(
            measurement_id=next_pk(Measurement, 'measurement_id'), person=person,
            measurement_concept=Concept.objects.get(concept_code=code),
            measurement_date=date(2026, 8, 1), measurement_type_concept_id=32865,
            value_as_number=value)
    record = PatientRecord.objects.get(person=person)

    entry = patient_form_descriptors(
        readable_fields={'anc_thousand_per_ul'}, record=record)['anc_thousand_per_ul']

    assert entry['latest_date'] == '2026-08-01'


def test_a_dated_source_pair_names_the_canonical_results_date():
    """The cells/µL count is dated but has no projection of its own (#5700)."""
    person = _person_with_lab()
    ConceptFactory(concept_code='751-8', concept_name='Absolute neutrophil count')
    Measurement.objects.create(
        measurement_id=next_pk(Measurement, 'measurement_id'), person=person,
        measurement_concept=Concept.objects.get(concept_code='751-8'),
        measurement_date=date(2026, 8, 1), measurement_type_concept_id=32865,
        value_as_number=4.2, unit_source_value='10*3/uL')
    record = PatientRecord.objects.get(person=person)

    descriptor = patient_form_descriptors(record=record)

    source = descriptor['absolute_neutrophile_count']
    assert source['dated'] is True
    assert source['latest_date'] == '2026-08-01'
    assert descriptor['anc_thousand_per_ul']['latest_date'] == '2026-08-01'


def test_a_dated_edit_records_the_supplied_clinical_date():
    person = _person_with_lab()

    record, result = apply_patient_form_patch(
        person, {FIELD: {'value': 12.5, 'date': '2026-07-04'}}, allowed_fields={FIELD})

    assert FIELD in result.applied
    fact = _fact(person)
    assert fact.measurement_date == date(2026, 7, 4)
    assert fact.measurement_datetime is None
    assert float(refresh_patient_record(person).hemoglobin_g_dl) == pytest.approx(12.5)


def test_a_future_date_is_refused_before_anything_is_written():
    person = _person_with_lab()
    tomorrow = (date.today() + timedelta(days=1)).isoformat()

    with pytest.raises(ValueError, match='future'):
        apply_patient_form_patch(
            person, {FIELD: {'value': 12.5, 'date': tomorrow}}, allowed_fields={FIELD})

    assert not Measurement.objects.filter(person=person).exists()


@pytest.mark.parametrize('payload', [
    {'value': 12.5},
    {'value': 12.5, 'date': '2026-13-01'},
    {'value': 12.5, 'date': '2026-07-04', 'extra': True},
])
def test_a_malformed_dated_envelope_is_refused(payload):
    person = _person_with_lab()

    with pytest.raises(ValueError):
        apply_patient_form_patch(person, {FIELD: payload}, allowed_fields={FIELD})

    assert not Measurement.objects.filter(person=person).exists()


def test_a_correction_at_the_same_date_updates_that_result():
    person = _person_with_lab()
    apply_patient_form_patch(
        person, {FIELD: {'value': 12.5, 'date': '2026-07-04'}}, allowed_fields={FIELD})
    apply_patient_form_patch(
        person, {FIELD: {'value': 13.0, 'date': '2026-07-04'}}, allowed_fields={FIELD})

    assert Measurement.objects.filter(person=person).count() == 1
    assert float(refresh_patient_record(person).hemoglobin_g_dl) == pytest.approx(13.0)


def test_an_older_result_is_history_not_the_current_value():
    person = _person_with_lab()
    apply_patient_form_patch(
        person, {FIELD: {'value': 12.0, 'date': '2026-08-01'}}, allowed_fields={FIELD})
    apply_patient_form_patch(
        person, {FIELD: {'value': 14.0, 'date': '2026-07-01'}}, allowed_fields={FIELD})

    dates = sorted(Measurement.objects.filter(person=person).values_list(
        'measurement_date', flat=True))
    assert dates == [date(2026, 7, 1), date(2026, 8, 1)]
    # The later result still wins derivation even though the older row was edited last.
    assert float(refresh_patient_record(person).hemoglobin_g_dl) == pytest.approx(12.0)


def test_an_undated_patch_keeps_todays_date_for_compatibility():
    person = _person_with_lab()

    apply_patient_form_patch(person, {FIELD: 12.5}, allowed_fields={FIELD})

    assert _fact(person).measurement_date == date.today()


def test_a_non_dated_field_refuses_a_result_date():
    person = _person_with_lab()

    with pytest.raises(ValueError, match='does not accept a result date'):
        apply_patient_form_patch(
            person, {'systolic_blood_pressure': {'value': 120, 'date': '2026-07-04'}},
            allowed_fields={'systolic_blood_pressure'})


def test_an_older_dated_correction_is_transient_until_refresh():
    """A historical correction lands on PatientRecord first; a refresh then
    restores the newer fact as the current answer. This is the documented
    PatientRecord-first trade-off, not a promise that history becomes current."""
    person = _person_with_lab()
    concept = Concept.objects.get(concept_code=LOINC)
    Measurement.objects.create(
        measurement_id=next_pk(Measurement, 'measurement_id'), person=person,
        measurement_concept=concept, measurement_date=date(2026, 8, 1),
        measurement_type_concept_id=32865, value_as_number=14.0,
    )
    record = PatientRecord.objects.get(person=person)
    record.hemoglobin_g_dl = 14.0
    record.save(update_fields=['hemoglobin_g_dl'])

    saved, result = apply_patient_form_patch(
        person, {FIELD: {'value': 12.5, 'date': '2026-07-04'}}, allowed_fields={FIELD},
    )

    assert FIELD in result.applied
    saved.refresh_from_db()
    assert float(saved.hemoglobin_g_dl) == 12.5
    july = Measurement.objects.get(
        person=person, measurement_concept=concept, measurement_date=date(2026, 7, 4),
    )
    august = Measurement.objects.get(
        person=person, measurement_concept=concept, measurement_date=date(2026, 8, 1),
    )
    assert july.value_as_number == 12.5
    assert august.value_as_number == 14.0
    refreshed = refresh_patient_record(person)
    assert float(refreshed.hemoglobin_g_dl) == 14.0


def test_every_source_pair_value_is_dated_when_its_canonical_is():
    """The pair is the editable half of a dated result (#2397).

    A patient records the number and unit their lab used; the date belongs to
    the same result as the canonical read-model it derives.
    """
    from omop_core.services import direct_bilirubin_units
    from omop_core.services.blood_count_source_pairs import PAIRS as COUNT_PAIRS
    from omop_core.services.chemistry_units import PAIRS as CHEMISTRY_PAIRS
    from omop_core.services.write_descriptor import (
        LAB_FIELD_TO_LOINC, build_writable_field_descriptor)

    _person_with_lab()

    pairs = {
        'hemoglobin_level': 'hemoglobin_g_dl',
        'serum_creatinine_level': 'serum_creatinine_mg_dl',
        **{spec['value']: canonical for canonical, spec in CHEMISTRY_PAIRS.items()},
        **{source: spec['canonical'] for source, spec in COUNT_PAIRS.items()},
    }
    # A dated canonical needs a resolved projection, so seed the LOINC rows the
    # descriptor resolves. The fixture already has hemoglobin's 718-7.
    seeded = {Concept.objects.get(concept_code=LOINC).concept_code}
    for field in (*pairs.values(), direct_bilirubin_units.PAIR_FIELD):
        code = LAB_FIELD_TO_LOINC[field][0]
        if code in seeded:
            continue
        ConceptFactory(concept_code=code, concept_name=field)
        seeded.add(code)

    descriptor = build_writable_field_descriptor()
    assert pairs
    undated = [
        f'{pair} (canonical {canonical})' for pair, canonical in pairs.items()
        if descriptor[pair].get('dated') is not True
        or descriptor[canonical].get('dated') is not True
    ]
    assert not undated, undated
    assert descriptor[direct_bilirubin_units.PAIR_FIELD].get('dated') is True


def test_the_pair_names_the_latest_recorded_date_for_its_canonical():
    person = _person_with_lab()
    concept = Concept.objects.get(concept_code=LOINC)
    Measurement.objects.create(
        measurement_id=next_pk(Measurement, 'measurement_id'), person=person,
        measurement_concept=concept, measurement_date=date(2026, 8, 1),
        measurement_type_concept_id=32865, value_as_number=12.5,
        unit_source_value='g/dL')
    record = PatientRecord.objects.get(person=person)

    entry = patient_form_descriptors(
        readable_fields={'hemoglobin_level'}, record=record)['hemoglobin_level']

    assert entry['dated'] is True
    assert entry['latest_date'] == '2026-08-01'


def test_a_dated_source_pair_lands_on_the_day_of_the_draw():
    person = _person_with_lab()

    apply_patient_form_patch(
        person,
        {'hemoglobin_level': {'value': 120, 'date': '2026-07-04'},
         'hemoglobin_level_units': 'G/L'},
        allowed_fields={'hemoglobin_level', 'hemoglobin_level_units',
                        'hemoglobin_g_dl'})

    record = PatientRecord.objects.get(person=person)
    assert record.hemoglobin_level == Decimal('120')
    assert record.hemoglobin_level_units == 'G/L'
    assert record.hemoglobin_g_dl == Decimal('12.0')
    fact = _fact(person)
    assert fact.measurement_date == date(2026, 7, 4)
    assert float(fact.value_as_number) == 12.0
    assert fact.unit_source_value == 'g/dL'


def test_an_undated_pair_refuses_a_result_date():
    """No resolved projection means no fact to carry the date (#2397)."""
    person = _person_with_lab()
    Concept.objects.filter(concept_code=LOINC).delete()

    with pytest.raises(ValueError, match='does not accept a result date'):
        apply_patient_form_patch(
            person,
            {'hemoglobin_level': {'value': 120, 'date': '2026-07-04'},
             'hemoglobin_level_units': 'G/L'},
            allowed_fields={'hemoglobin_level', 'hemoglobin_level_units',
                            'hemoglobin_g_dl'})

    assert not Measurement.objects.filter(person=person).exists()
    assert PatientRecord.objects.get(person=person).hemoglobin_level is None


def test_an_older_dated_pair_stays_the_current_answer():
    """The pair is retained pending, so an older draw is still the answer (#2318).

    The fact history shows the older draw; the record keeps the reader's pair
    until an explicit canonical event, so the editor must not call it history.
    """
    person = _person_with_lab()
    concept = Concept.objects.get(concept_code=LOINC)
    type_vocab = VocabularyFactory(
        vocabulary_id='Type Concept', vocabulary_name='Type Concept')
    ConceptFactory(concept_id=32817, concept_code='EHR', concept_name='EHR',
                   vocabulary=type_vocab)
    Measurement.objects.create(
        measurement_id=next_pk(Measurement, 'measurement_id'), person=person,
        measurement_concept=concept, measurement_date=date(2026, 8, 1),
        measurement_type_concept_id=32817, value_as_number=12.0,
        unit_source_value='g/dL')

    apply_patient_form_patch(
        person,
        {'hemoglobin_level': {'value': 140, 'date': '2026-07-01'},
         'hemoglobin_level_units': 'G/L'},
        allowed_fields={'hemoglobin_level', 'hemoglobin_level_units',
                        'hemoglobin_g_dl'})

    record = PatientRecord.objects.get(person=person)
    assert record.hemoglobin_level == Decimal('140')
    assert record.hemoglobin_g_dl == Decimal('14.0')
    assert {'hemoglobin_level', 'hemoglobin_level_units'} <= set(
        record.user_edited_fields or [])

    refresh_patient_record(person)

    record.refresh_from_db()
    assert record.hemoglobin_level == Decimal('140')
    assert record.hemoglobin_g_dl == Decimal('14.0')
    dates = sorted(Measurement.objects.filter(person=person).values_list(
        'measurement_date', flat=True))
    assert dates == [date(2026, 7, 1), date(2026, 8, 1)]


def test_a_future_date_on_the_pair_is_refused():
    person = _person_with_lab()
    tomorrow = (date.today() + timedelta(days=1)).isoformat()

    with pytest.raises(ValueError, match='future'):
        apply_patient_form_patch(
            person,
            {'hemoglobin_level': {'value': 120, 'date': tomorrow},
             'hemoglobin_level_units': 'G/L'},
            allowed_fields={'hemoglobin_level', 'hemoglobin_level_units',
                            'hemoglobin_g_dl'})

    assert not Measurement.objects.filter(person=person).exists()
