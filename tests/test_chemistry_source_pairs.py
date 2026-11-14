"""Albumin, serum calcium and total bilirubin as source pairs (#2318).

The patient records the number in the unit their lab used, and the record keeps
it. The canonical g/dL or mg/dL column is the normalised storage/projection
read-model, quantised to the column's one decimal — so the pair stays the value
matching reads, and a canonical import still propagates back into the pair.
"""
from datetime import date
from decimal import Decimal

import pytest
from rest_framework.test import APIClient

from omop_core.models import Measurement, PatientRecord, Person
from omop_core.services.chemistry_units import PAIRS, canonical_from_pair
from omop_core.services.patient_record_service import refresh_patient_record
from omop_core.services.write_descriptor import build_writable_field_descriptor
from patient_portal.api.serializers import PatientRecordSerializer
from patient_portal.models import Identity
from tests.factories import ConceptFactory, MeasurementFactory, VocabularyFactory

pytestmark = pytest.mark.django_db

PERSON_ID = 99003

PAIR_CASES = {
    'albumin': {
        'canonical': 'albumin_g_dl',
        'pair': 'albumin_level',
        'unit': 'albumin_level_units',
        'code': '1751-7',
        'canonical_unit': 'g/dL',
        'value': '40',
        'source_unit': 'g/L',
        'expected': Decimal('4.0'),
        'second_value': '8.0',
        'second_expected': Decimal('8.0'),
    },
    'calcium': {
        'canonical': 'serum_calcium_mg_dl',
        'pair': 'serum_calcium_level',
        'unit': 'serum_calcium_level_units',
        'code': '17861-6',
        'canonical_unit': 'mg/dL',
        'value': '2.50',
        'source_unit': 'mmol/L',
        'expected': Decimal('10.0'),
        'second_value': '20.0',
        'second_expected': Decimal('20.0'),
    },
    'bilirubin': {
        'canonical': 'bilirubin_total_mg_dl',
        'pair': 'serum_bilirubin_level_total',
        'unit': 'serum_bilirubin_level_total_units',
        'code': '1975-2',
        'canonical_unit': 'mg/dL',
        'value': '17.1',
        'source_unit': 'umol/L',
        'expected': Decimal('1.0'),
        'second_value': '2.0',
        'second_expected': Decimal('2.0'),
    },
}

CASES = list(PAIR_CASES.values())


def _record():
    person = Person.objects.create(person_id=PERSON_ID)
    record, _ = PatientRecord.objects.get_or_create(person=person)
    return record


@pytest.fixture
def api(settings):
    settings.CELERY_BROKER_URL = ''
    type_vocab = VocabularyFactory(
        vocabulary_id='Type Concept', vocabulary_name='Type Concept')
    ConceptFactory(
        concept_id=32865, concept_code='PT-SELF-REPORT',
        concept_name='Patient self-report', vocabulary=type_vocab,
    )
    for case in CASES:
        ConceptFactory(
            concept_code=case['code'],
            vocabulary=VocabularyFactory(vocabulary_id='LOINC'),
        )
        ConceptFactory(
            concept_code=case['canonical_unit'],
            vocabulary=VocabularyFactory(vocabulary_id='UCUM'),
        )
    client = APIClient()
    client.force_authenticate(
        Identity.objects.create_user(
            email='chemistry-pair@example.test', is_staff=True),
    )
    return client


def _edit(client, record, data):
    response = client.patch(
        f'/api/patient-info/{record.person.person_id}/', data, format='json',
    )
    assert response.status_code == 200, response.data
    record.refresh_from_db()
    return response


@pytest.mark.parametrize('case', CASES, ids=list(PAIR_CASES))
def test_the_pair_is_the_write_target_and_the_canonical_is_computed(case):
    descriptor = build_writable_field_descriptor()

    pair = descriptor[case['pair']]
    unit = descriptor[case['unit']]
    canonical = descriptor[case['canonical']]

    assert pair['kind'] == 'direct' and pair['writable'] is True
    assert unit['kind'] == 'selectable' and unit['qualifies'] == case['pair']
    assert canonical['kind'] == 'computed' and canonical['writable'] is False
    assert canonical['normalised_read_model'] is True
    assert canonical['inputs'] == [case['pair']]
    assert {option['value'] for option in pair['unit_options']} == set(
        PAIRS[case['canonical']]['units'])


@pytest.mark.parametrize('case', CASES, ids=list(PAIR_CASES))
def test_the_pair_projects_the_normalised_value_in_the_canonical_unit(api, case):
    record = _record()

    response = _edit(api, record, {
        case['pair']: case['value'], case['unit']: case['source_unit'],
    })

    assert getattr(record, case['pair']) == Decimal(case['value'])
    # The pair keeps the unit the patient chose; only the canonical normalises.
    assert getattr(record, case['unit']) == case['source_unit'].upper()
    assert getattr(record, case['canonical']) == case['expected']
    assert Decimal(str(response.data[case['canonical']])) == case['expected']

    row = Measurement.objects.get(
        person=record.person, measurement_source_value=case['code'])
    assert row.value_as_number == case['expected']
    assert row.unit_source_value == case['canonical_unit']


@pytest.mark.parametrize('case', CASES, ids=list(PAIR_CASES))
def test_a_canonical_unit_write_is_the_identity(api, case):
    record = _record()

    _edit(api, record, {
        case['pair']: str(case['expected']),
        case['unit']: case['canonical_unit'],
    })

    assert getattr(record, case['canonical']) == case['expected']


@pytest.mark.parametrize('case', CASES, ids=list(PAIR_CASES))
def test_the_shared_form_normalises_the_source_pair(case):
    from omop_core.services.patient_form import apply_patient_form_patch

    record = _record()
    saved, result = apply_patient_form_patch(
        record.person,
        {case['pair']: case['value'], case['unit']: case['source_unit']},
    )

    assert {case['pair'], case['canonical']} <= set(result.applied)
    assert getattr(saved, case['canonical']) == case['expected']


@pytest.mark.parametrize('case', CASES, ids=list(PAIR_CASES))
def test_recompute_keeps_the_pair_and_derives_the_canonical(case):
    from omop_core.services.patient_record_service import (
        recompute_patient_record_fields,
    )

    record = _record()
    setattr(record, case['pair'], Decimal(case['value']))
    setattr(record, case['unit'], case['source_unit'])
    setattr(record, case['canonical'], case['expected'])
    record.user_edited_fields = [case['pair'], case['unit']]
    record.save()

    recompute_patient_record_fields(record)

    record.refresh_from_db()
    assert getattr(record, case['pair']) == Decimal(case['value'])
    assert getattr(record, case['unit']) == case['source_unit']
    assert getattr(record, case['canonical']) == case['expected']


@pytest.mark.parametrize('case', CASES, ids=list(PAIR_CASES))
def test_a_new_canonical_import_retires_the_pending_pair(case):
    from omop_core.services.patient_record_service import (
        recompute_patient_record_fields,
    )

    record = _record()
    setattr(record, case['pair'], Decimal(case['value']))
    setattr(record, case['unit'], case['source_unit'])
    setattr(record, case['canonical'], case['expected'])
    record.user_edited_fields = [case['pair'], case['unit']]
    record.save()

    setattr(record, case['canonical'], case['second_expected'])
    recompute_patient_record_fields(record, changed_fields=(case['canonical'],))

    record.refresh_from_db()
    assert getattr(record, case['canonical']) == case['second_expected']
    assert getattr(record, case['pair']) == Decimal(case['second_value'])
    assert getattr(record, case['unit']) == PAIRS[case['canonical']]['default_unit']


@pytest.mark.parametrize('case', CASES, ids=list(PAIR_CASES))
def test_a_read_only_canonical_refuses_the_pair(case):
    from omop_core.services.patient_form import (
        apply_patient_form_patch, patient_form_descriptors,
    )

    record = _record()
    setattr(record, case['canonical'], case['expected'])
    record.save()
    descriptor = patient_form_descriptors(
        build_writable_field_descriptor(), record=None)
    descriptor[case['canonical']] = {
        **descriptor[case['canonical']], 'read_only_for_caller': True,
    }

    saved, result = apply_patient_form_patch(
        record.person,
        {case['pair']: case['value'], case['unit']: case['source_unit']},
        descriptor=descriptor,
    )

    assert result.rejected.get(case['pair'])
    assert getattr(saved, case['pair']) is None
    assert getattr(saved, case['canonical']) == case['expected']


@pytest.mark.parametrize('case', CASES, ids=list(PAIR_CASES))
def test_an_unknown_stored_unit_is_refused_not_defaulted(case):
    from omop_core.services.patient_form import apply_patient_form_patch

    record = _record()
    PatientRecord.objects.filter(pk=record.pk).update(
        **{case['pair']: None, case['unit']: 'mg/L'},
    )

    saved, result = apply_patient_form_patch(
        record.person, {case['pair']: case['value']})

    assert result.rejected.get(case['unit'])
    assert not result.applied
    assert getattr(saved, case['canonical']) is None


@pytest.mark.parametrize('case', CASES, ids=list(PAIR_CASES))
def test_a_unit_without_its_value_is_refused(api, case):
    record = _record()
    serializer = PatientRecordSerializer(
        record, data={case['unit']: case['source_unit']}, partial=True,
    )

    assert not serializer.is_valid()
    assert case['unit'] in serializer.errors


@pytest.mark.parametrize('case', CASES, ids=list(PAIR_CASES))
def test_an_unsupported_unit_is_refused_on_write(api, case):
    record = _record()
    serializer = PatientRecordSerializer(
        record,
        data={case['pair']: case['value'], case['unit']: 'mg/L'},
        partial=True,
    )

    assert not serializer.is_valid()
    assert case['unit'] in serializer.errors


@pytest.mark.parametrize('case', CASES, ids=list(PAIR_CASES))
def test_a_value_outside_the_canonical_range_is_refused(api, case):
    record = _record()
    serializer = PatientRecordSerializer(
        record,
        data={case['pair']: '100000', case['unit']: case['source_unit']},
        partial=True,
    )

    assert not serializer.is_valid()
    assert case['pair'] in serializer.errors


@pytest.mark.parametrize('case', CASES, ids=list(PAIR_CASES))
def test_a_legacy_name_import_writes_the_pair_and_derives_the_canonical(case):
    names = {
        'albumin_level': 'albumin',
        'serum_calcium_level': 'calcium',
        'serum_bilirubin_level_total': 'bilirubin total',
    }
    person = Person.objects.create(person_id=PERSON_ID + 1)
    MeasurementFactory(
        person=person,
        measurement_concept=ConceptFactory(concept_name=names[case['pair']]),
        value_as_number=case['value'],
        unit_source_value=case['source_unit'],
    )

    record = refresh_patient_record(person)

    # The record keeps what the lab reported; the canonical is the read-model.
    assert getattr(record, case['pair']) == Decimal(case['value'])
    assert getattr(record, case['unit']) == case['source_unit'].upper()
    assert getattr(record, case['canonical']) == case['expected']


@pytest.mark.parametrize('case', CASES, ids=list(PAIR_CASES))
def test_a_newer_legacy_pair_is_not_downgraded_by_an_older_coded_result(case):
    names = {
        'albumin_level': 'albumin',
        'serum_calcium_level': 'calcium',
        'serum_bilirubin_level_total': 'bilirubin total',
    }
    person = Person.objects.create(person_id=PERSON_ID + 2)
    MeasurementFactory(
        person=person,
        measurement_concept=ConceptFactory(concept_code=case['code']),
        measurement_source_value=case['code'],
        measurement_date=date(2024, 1, 1),
        value_as_number=case['second_value'],
        unit_source_value=case['source_unit'],
    )
    MeasurementFactory(
        person=person,
        measurement_concept=ConceptFactory(concept_name=names[case['pair']]),
        measurement_date=date(2024, 2, 1),
        value_as_number=case['value'],
        unit_source_value=case['source_unit'],
    )

    record = refresh_patient_record(person)

    # The newer legacy fact wins: the canonical is 4.0, not the older coded 8.0.
    assert getattr(record, case['canonical']) == case['expected']
    assert getattr(record, case['pair']) == Decimal(case['value'])


@pytest.mark.parametrize('case,pair_value,unit_code,quantised', [
    ('albumin', Decimal('34.94'), 'G/L', Decimal('3.5')),
    ('calcium', Decimal('2.74'), 'MMOL/L', Decimal('11.0')),
    ('bilirubin', Decimal('25.64'), 'UMOL/L', Decimal('1.5')),
], ids=['albumin', 'calcium', 'bilirubin'])
def test_the_canonical_is_quantised_so_the_pair_stays_the_threshold_source(
    case, pair_value, unit_code, quantised,
):
    spec = PAIR_CASES[case]

    recognised, canonical = canonical_from_pair(
        spec['canonical'], pair_value, unit_code)

    assert recognised
    assert canonical == quantised
    # The pair converts to a value the one-decimal column cannot hold. A
    # threshold at the quantised boundary must read the pair, not this column.
    assert pair_value / PAIRS[spec['canonical']]['divisors'][unit_code] != canonical


def test_a_pending_pair_survives_a_later_import():
    person = Person.objects.create(person_id=PERSON_ID + 3)
    record, _ = PatientRecord.objects.get_or_create(person=person)
    record.albumin_level = Decimal('40')
    record.albumin_level_units = 'G/L'
    record.albumin_g_dl = Decimal('4.0')
    record.user_edited_fields = ['albumin_level', 'albumin_level_units']
    record.save()
    MeasurementFactory(
        person=person,
        measurement_concept=ConceptFactory(concept_code='1751-7'),
        measurement_source_value='1751-7',
        value_as_number=30,
        unit_source_value='g/L',
    )

    record = refresh_patient_record(person)

    # The patient's 40 g/L is still the answer; the import does not replace it.
    assert record.albumin_level == Decimal('40')
    assert record.albumin_level_units == 'G/L'
    assert record.albumin_g_dl == Decimal('4.0')
    assert {'albumin_level', 'albumin_level_units'} <= set(
        record.user_edited_fields or [])


def test_a_pending_clear_survives_a_refresh_of_the_imported_fact():
    person = Person.objects.create(person_id=PERSON_ID + 4)
    MeasurementFactory(
        person=person,
        measurement_concept=ConceptFactory(concept_code='1751-7'),
        measurement_source_value='1751-7',
        value_as_number=40,
        unit_source_value='g/L',
    )
    record = refresh_patient_record(person)
    assert record.albumin_g_dl == Decimal('4.0')

    record.albumin_level = None
    record.albumin_level_units = None
    record.albumin_g_dl = None
    record.user_edited_fields = ['albumin_level', 'albumin_level_units']
    record.save()
    record = refresh_patient_record(person)

    assert record.albumin_level is None
    assert record.albumin_g_dl is None


def test_a_pending_canonical_wins_over_a_stale_pair():
    from omop_core.services.patient_record_service import (
        recompute_patient_record_fields,
    )

    record = _record()
    record.albumin_level = Decimal('40')
    record.albumin_level_units = 'G/L'
    record.albumin_g_dl = Decimal('5.0')
    record.user_edited_fields = ['albumin_g_dl']
    record.save()

    recompute_patient_record_fields(record, changed_fields=('albumin_g_dl',))

    record.refresh_from_db()
    assert record.albumin_g_dl == Decimal('5.0')
    assert record.albumin_level == Decimal('5.0')
    assert record.albumin_level_units == 'G/DL'


def test_a_read_only_companion_unit_uses_the_stored_unit():
    from omop_core.services.patient_form import (
        apply_patient_form_patch, patient_form_descriptors,
    )

    record = _record()
    record.albumin_level = Decimal('40')
    record.albumin_level_units = 'G/L'
    record.albumin_g_dl = Decimal('4.0')
    record.save()
    descriptor = patient_form_descriptors(
        build_writable_field_descriptor(), record=None)
    descriptor['albumin_level_units'] = {
        **descriptor['albumin_level_units'], 'read_only_for_caller': True,
    }

    saved, result = apply_patient_form_patch(
        record.person, {'albumin_level': '50'}, descriptor=descriptor)

    assert {'albumin_level', 'albumin_g_dl'} <= set(result.applied)
    assert saved.albumin_level == Decimal('50')
    assert saved.albumin_level_units == 'G/L'
    assert saved.albumin_g_dl == Decimal('5.0')


@pytest.mark.parametrize('case', CASES, ids=list(PAIR_CASES))
def test_a_newer_dated_import_retires_the_pending_pair(api, case):
    from omop_core.signals import suppress_patient_record_refresh

    record = _record()
    draw = date(2024, 1, 1)
    _edit(api, record, {
        case['pair']: {'value': case['value'], 'date': draw.isoformat()},
        case['unit']: case['source_unit'],
    })
    assert record.pending_source_pair_dates == {case['pair']: draw.isoformat()}

    with suppress_patient_record_refresh():
        MeasurementFactory(
            person=record.person,
            measurement_concept=ConceptFactory(concept_code=case['code']),
            measurement_source_value=case['code'],
            measurement_date=date(2024, 2, 1),
            value_as_number=case['value'],
            unit_source_value=case['source_unit'],
        )
    refresh_patient_record(record.person)
    record.refresh_from_db()

    # The import is strictly newer, so the pair yields to it.
    assert getattr(record, case['canonical']) == case['expected']
    assert getattr(record, case['pair']) == Decimal(case['expected'])
    assert case['pair'] not in (record.user_edited_fields or [])
    assert record.pending_source_pair_dates == {}


@pytest.mark.parametrize('case', CASES, ids=list(PAIR_CASES))
def test_a_same_day_import_keeps_the_dated_pair(case):
    from omop_core.signals import suppress_patient_record_refresh

    record = _record()
    draw = date(2024, 3, 1)
    record.user_edited_fields = [case['pair'], case['unit']]
    record.pending_source_pair_dates = {case['pair']: draw.isoformat()}
    setattr(record, case['pair'], Decimal(case['value']))
    setattr(record, case['unit'], case['source_unit'])
    setattr(record, case['canonical'], case['expected'])
    record.save()

    with suppress_patient_record_refresh():
        MeasurementFactory(
            person=record.person,
            measurement_concept=ConceptFactory(concept_code=case['code']),
            measurement_source_value=case['code'],
            measurement_date=draw,
            value_as_number=case['value'],
            unit_source_value=case['source_unit'],
        )
    refresh_patient_record(record.person)
    record.refresh_from_db()

    assert getattr(record, case['canonical']) == case['expected']
    assert getattr(record, case['pair']) == Decimal(case['value'])
    assert record.pending_source_pair_dates == {case['pair']: draw.isoformat()}
