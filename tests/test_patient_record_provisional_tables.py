"""patient_record_provisional and patient_record_confidence mirror patient_record (#2528).

Both tables are generated from PatientRecord when the models load, so the
tests pin the contract rather than the generator: a column added to
PatientRecord must reach the provisional table, a field derivation owns must
reach the confidence table, and neither table may grow a column of its own.
"""
from decimal import Decimal

import pytest
from django.db import models

from omop_core.derived_fields import _OMOP_DERIVED_FIELDS
from omop_core.models import (
    PROVISIONAL_EXCLUDED_COLUMNS, PatientRecord, PatientRecordConfidence,
    PatientRecordProvisional,
)
from tests.factories import PatientRecordFactory

pytestmark = pytest.mark.django_db


def _columns(model):
    return {f.name: f for f in model._meta.concrete_fields}


def test_the_exclusions_are_real_patient_record_columns():
    # A renamed lifecycle column would silently start being cloned.
    assert PROVISIONAL_EXCLUDED_COLUMNS <= set(_columns(PatientRecord))


def test_provisional_has_every_patient_record_column_and_nothing_else():
    real = set(_columns(PatientRecord)) - PROVISIONAL_EXCLUDED_COLUMNS
    provisional = set(_columns(PatientRecordProvisional)) - {'person'}
    assert provisional == real


def test_provisional_columns_keep_their_type_and_nullability():
    real = _columns(PatientRecord)
    for name, field in _columns(PatientRecordProvisional).items():
        if name == 'person':
            continue
        assert type(field) is type(real[name]), name
        assert field.null == real[name].null, name
        assert field.default == real[name].default, name


def test_provisional_adds_no_index_or_unique_of_its_own():
    # Tenancy reads organization; everything else is read by person.
    for name, field in _columns(PatientRecordProvisional).items():
        if name in ('person', 'organization'):
            continue
        assert not field.unique and not field.db_index, name
    assert not PatientRecordProvisional._meta.indexes
    assert not PatientRecordProvisional._meta.constraints
    assert PatientRecordProvisional._meta.get_field('organization').remote_field.related_name == '+'


def test_confidence_is_exactly_the_derived_non_json_columns_and_the_covered_json_lists():
    from omop_core.derived_fields import PROVISIONAL_JSON_FIELDS
    expected = set()
    for name in _OMOP_DERIVED_FIELDS:
        try:
            field = PatientRecord._meta.get_field(name)
        except Exception:  # noqa: BLE001 -- a derived name that is not a column
            continue
        if not isinstance(field, models.JSONField) or name in PROVISIONAL_JSON_FIELDS:
            expected.add(name)
    assert set(_columns(PatientRecordConfidence)) - {'person'} == expected
    assert len(expected) > 250  # the plan counts 274 today; a collapse to a few is a bug
    assert {'prior_procedures', 'genetic_mutations', 'active_malignancies', 'disease_slugs'} <= expected


def test_confidence_columns_are_nullable_percentages_named_like_the_field():
    for name, field in _columns(PatientRecordConfidence).items():
        if name == 'person':
            continue
        assert isinstance(field, models.PositiveSmallIntegerField) and field.null, name
        assert name in _columns(PatientRecordProvisional), name


def test_patient_record_itself_gets_no_confidence_column():
    assert 'confidence' not in _columns(PatientRecord)


def test_a_row_round_trips_and_follows_its_person():
    record = PatientRecordFactory()
    PatientRecordProvisional.objects.create(person=record.person, hemoglobin_g_dl=11.2)
    PatientRecordConfidence.objects.create(person=record.person, hemoglobin_g_dl=82)

    assert PatientRecordProvisional.objects.get(person=record.person).hemoglobin_g_dl == Decimal('11.2')
    assert record.person.patient_record_confidence.hemoglobin_g_dl == 82
    record.person.delete()
    assert not PatientRecordProvisional.objects.exists()
    assert not PatientRecordConfidence.objects.exists()
