"""Creatinine's source pair and its normalised read-model (#2318 follow-up).

The patient records the number in the unit their lab used; the matcher reads the
normalised mg/dL value through `serum_creatinine_mg_dl`. The divisor is 88.42 —
the one the installed CKD-EPI calculator uses — so an edit here and the matcher
agree on the same eGFR.

Direct bilirubin is deliberately not in this slice: it has no separate canonical
column yet.
"""
from decimal import Decimal

import pytest
from rest_framework.test import APIClient

from omop_core.models import Measurement, PatientRecord, Person
from omop_core.services.creatinine_units import (
    CANONICAL_FIELD,
    UNIT_DIVISORS,
    canonical_from_pair,
    canonical_in_range,
    normalize_unit,
)
from omop_core.services.write_descriptor import build_writable_field_descriptor
from patient_portal.api.serializers import PatientRecordSerializer
from patient_portal.models import Identity
from tests.factories import ConceptFactory, VocabularyFactory

pytestmark = pytest.mark.django_db

PERSON_ID = 99002
LOINC_CREATININE = '2160-0'


def _record():
    person = Person.objects.create(person_id=PERSON_ID)
    record, _ = PatientRecord.objects.get_or_create(person=person)
    return record


def _patch(record, data):
    return PatientRecordSerializer(record, data=data, partial=True)


@pytest.fixture
def api(settings):
    settings.CELERY_BROKER_URL = ''
    type_vocab = VocabularyFactory(vocabulary_id='Type Concept', vocabulary_name='Type Concept')
    ConceptFactory(
        concept_id=32865, concept_code='PT-SELF-REPORT',
        concept_name='Patient self-report', vocabulary=type_vocab,
    )
    ConceptFactory(concept_code=LOINC_CREATININE, vocabulary=VocabularyFactory(vocabulary_id='LOINC'))
    unit = ConceptFactory(concept_code='mg/dL', vocabulary=VocabularyFactory(vocabulary_id='UCUM'))
    client = APIClient()
    client.force_authenticate(
        Identity.objects.create_user(email='creatinine-pair@example.test', is_staff=True),
    )
    return client, unit


def _edit(api, record, data):
    client, _unit = api
    response = client.patch(
        f'/api/patient-info/{record.person.person_id}/', data, format='json',
    )
    assert response.status_code == 200, response.data
    record.refresh_from_db()
    return response


def _projected(record):
    return Measurement.objects.get(
        person=record.person, measurement_source_value=LOINC_CREATININE,
    )


@pytest.mark.parametrize('value,unit,expected', [
    # Golden numbers, written independently of the registry: a 10x or 1000x slip
    # in the divisor has to fail here.
    (Decimal('88.42'), 'UMOL/L', Decimal('1.00')),
    (Decimal('176.84'), 'UMOL/L', Decimal('2.00')),
    (Decimal('100'), 'UMOL/L', Decimal('1.13')),
    (Decimal('1'), 'MG/DL', Decimal('1.00')),
    (Decimal('1.25'), 'MG/DL', Decimal('1.25')),
    (Decimal('1.1'), None, Decimal('1.10')),
])
def test_the_pair_normalises_to_the_matchers_unit(value, unit, expected):
    recognised, canonical = canonical_from_pair(value, unit)

    assert recognised is True
    assert canonical == expected


def test_an_unknown_unit_is_not_converted():
    assert canonical_from_pair(Decimal('100'), 'MMOL/L') == (False, None)


def test_a_clear_is_recognised_whatever_the_unit_says():
    assert canonical_from_pair(None, 'UMOL/L') == (True, None)


@pytest.mark.parametrize('spelling,expected', [
    ('umol/L', 'UMOL/L'), ('UMOL/L', 'UMOL/L'), ('mg/dL', 'MG/DL'), ('MG/DL', 'MG/DL'),
])
def test_the_unit_label_is_case_insensitive(spelling, expected):
    assert normalize_unit(spelling) == expected


def test_the_descriptor_points_the_editor_at_the_source_pair():
    descriptor = build_writable_field_descriptor()

    assert descriptor['serum_creatinine_level']['kind'] == 'direct'
    assert descriptor['serum_creatinine_level']['writable'] is True
    assert descriptor['serum_creatinine_level_units']['kind'] == 'selectable'
    assert descriptor['serum_creatinine_level_units']['qualifies'] == 'serum_creatinine_level'
    assert descriptor[CANONICAL_FIELD]['kind'] == 'computed'
    assert descriptor[CANONICAL_FIELD]['writable'] is False
    assert descriptor[CANONICAL_FIELD]['inputs'] == ['serum_creatinine_level']
    assert descriptor[CANONICAL_FIELD]['normalised_read_model'] is True


def test_a_pair_in_umol_per_l_is_stored_and_normalised():
    record = _record()
    serializer = _patch(record, {
        'serum_creatinine_level': '88.42', 'serum_creatinine_level_units': 'umol/L',
    })
    assert serializer.is_valid(), serializer.errors
    serializer.save()

    record.refresh_from_db()
    assert Decimal(str(record.serum_creatinine_level)) == Decimal('88.42')
    assert record.serum_creatinine_level_units == 'UMOL/L'
    assert record.serum_creatinine_mg_dl == Decimal('1.00')


def test_a_pair_in_mg_per_dl_keeps_the_number():
    record = _record()
    serializer = _patch(record, {
        'serum_creatinine_level': '1.25', 'serum_creatinine_level_units': 'mg/dL',
    })
    assert serializer.is_valid(), serializer.errors
    serializer.save()

    record.refresh_from_db()
    assert record.serum_creatinine_mg_dl == Decimal('1.25')


def test_a_unit_without_its_value_is_refused():
    record = _record()
    serializer = _patch(record, {'serum_creatinine_level_units': 'umol/L'})

    assert not serializer.is_valid()
    assert 'serum_creatinine_level_units' in serializer.errors


def test_an_unknown_unit_is_refused():
    record = _record()
    serializer = _patch(record, {
        'serum_creatinine_level': '1', 'serum_creatinine_level_units': 'mmol/L',
    })

    assert not serializer.is_valid()
    assert 'serum_creatinine_level_units' in serializer.errors


def test_a_pair_outside_the_canonical_range_is_refused():
    record = _record()
    assert canonical_in_range(Decimal('1000')) is False
    serializer = _patch(record, {
        'serum_creatinine_level': '1000', 'serum_creatinine_level_units': 'mg/dL',
    })

    assert not serializer.is_valid()
    assert 'serum_creatinine_level' in serializer.errors


def test_recompute_keeps_the_pair_and_derives_the_canonical():
    from omop_core.services.patient_record_service import recompute_patient_record_fields

    record = _record()
    record.serum_creatinine_level = Decimal('88.42')
    record.serum_creatinine_level_units = 'UMOL/L'
    record.serum_creatinine_mg_dl = Decimal('1.00')
    record.user_edited_fields = ['serum_creatinine_level', 'serum_creatinine_level_units']
    record.save()

    recompute_patient_record_fields(record)

    record.refresh_from_db()
    assert record.serum_creatinine_level == Decimal('88.42')
    assert record.serum_creatinine_level_units == 'UMOL/L'
    assert record.serum_creatinine_mg_dl == Decimal('1.00')


def test_a_new_canonical_import_retires_the_pending_pair():
    from omop_core.services.patient_record_service import recompute_patient_record_fields

    record = _record()
    record.serum_creatinine_level = Decimal('88.42')
    record.serum_creatinine_level_units = 'UMOL/L'
    record.serum_creatinine_mg_dl = Decimal('1.00')
    record.user_edited_fields = ['serum_creatinine_level', 'serum_creatinine_level_units']
    record.save()

    record.serum_creatinine_mg_dl = Decimal('2.00')
    recompute_patient_record_fields(record, changed_fields=('serum_creatinine_mg_dl',))

    record.refresh_from_db()
    assert record.serum_creatinine_mg_dl == Decimal('2.00')
    assert record.serum_creatinine_level_units == 'MG/DL'
    assert record.serum_creatinine_level == Decimal('2.00')


def test_the_pair_projects_the_normalised_value_with_its_unit(api):
    record = _record()
    response = _edit(api, record, {
        'serum_creatinine_level': '88.42', 'serum_creatinine_level_units': 'umol/L',
    })

    assert Decimal(str(record.serum_creatinine_level)) == Decimal('88.42')
    assert record.serum_creatinine_level_units == 'UMOL/L'
    assert record.serum_creatinine_mg_dl == Decimal('1.00')
    assert Decimal(str(response.data['serum_creatinine_mg_dl'])) == Decimal('1.00')

    row = _projected(record)
    assert row.value_as_number == Decimal('1.00')
    assert row.unit_source_value == 'mg/dL'


def test_the_shared_form_normalises_the_source_pair(api):
    from omop_core.services.patient_form import apply_patient_form_patch

    record = _record()
    saved, result = apply_patient_form_patch(
        record.person, {'serum_creatinine_level': '176.84', 'serum_creatinine_level_units': 'umol/L'},
    )
    assert {'serum_creatinine_level', CANONICAL_FIELD} <= set(result.applied)
    # The companion unit is persisted with the value; whether the writer lists it
    # in `applied` is an implementation detail of the pair path.
    assert saved.serum_creatinine_level_units == 'UMOL/L'
    assert saved.serum_creatinine_mg_dl == Decimal('2.00')
    assert _projected(saved).value_as_number == Decimal('2.00')


def test_the_divisor_is_the_matchers():
    # 88.42, not 88.4: the CKD-EPI calculator uses that factor.
    assert UNIT_DIVISORS['UMOL/L'] == Decimal('88.42')


def test_a_read_only_canonical_refuses_the_pair():
    """A host-level restriction must not be bypassed by the pair path (#2366)."""
    from omop_core.services.patient_form import patient_form_descriptors

    record = _record()
    record.serum_creatinine_mg_dl = Decimal('1.00')
    record.save()
    descriptor = patient_form_descriptors(build_writable_field_descriptor(), record=None)
    descriptor[CANONICAL_FIELD] = {
        **descriptor[CANONICAL_FIELD], 'read_only_for_caller': True,
    }

    from omop_core.services.patient_form import apply_patient_form_patch
    saved, result = apply_patient_form_patch(
        record.person,
        {'serum_creatinine_level': '2', 'serum_creatinine_level_units': 'mg/dL'},
        descriptor=descriptor,
    )

    assert result.rejected.get('serum_creatinine_level')
    assert saved.serum_creatinine_level is None
    assert saved.serum_creatinine_mg_dl == Decimal('1.00')


def test_an_unknown_stored_unit_is_refused_not_defaulted():
    """`MMOL/L` in legacy data is not permission to read the number as mg/dL."""
    from omop_core.services.patient_form import apply_patient_form_patch

    record = _record()
    PatientRecord.objects.filter(pk=record.pk).update(
        serum_creatinine_level=None, serum_creatinine_level_units='MMOL/L',
    )

    saved, result = apply_patient_form_patch(record.person, {'serum_creatinine_level': '2'})

    assert result.rejected.get('serum_creatinine_level_units')
    assert not result.applied
    assert saved.serum_creatinine_mg_dl is None


def test_the_ordinary_alias_follows_the_pair():
    """The pair is protected from the canonical copy; its sibling alias is not."""
    from omop_core.services.patient_record_service import recompute_patient_record_fields

    record = _record()
    record.serum_creatinine_level = Decimal('88.42')
    record.serum_creatinine_level_units = 'UMOL/L'
    record.serum_creatinine_mg_dl = Decimal('1.00')
    record.creatinine_mg_dl = Decimal('1.00')
    record.user_edited_fields = ['serum_creatinine_level', 'serum_creatinine_level_units']
    record.save()

    record.serum_creatinine_level = Decimal('176.84')
    recompute_patient_record_fields(record)

    record.refresh_from_db()
    assert record.serum_creatinine_mg_dl == Decimal('2.00')
    assert record.serum_creatinine_level == Decimal('176.84')
    assert record.serum_creatinine_level_units == 'UMOL/L'
    assert record.creatinine_mg_dl == Decimal('2.00')
