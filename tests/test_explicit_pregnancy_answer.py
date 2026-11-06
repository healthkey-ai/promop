"""The pregnancy/lactation self-assessment is an explicit answer (#2343)."""
import pytest
from django.db import IntegrityError, transaction

from omop_core.models import FieldFormula
from omop_core.services.patient_record_service import (
    recompute_patient_record_fields,
    refresh_patient_record,
)
from omop_core.services.write_descriptor import build_writable_field_descriptor
from tests.factories import PatientRecordFactory

pytestmark = pytest.mark.django_db

FIELD = 'no_pregnancy_or_lactation_status'


def test_false_survives_recompute_and_refresh():
    record = PatientRecordFactory(no_pregnancy_or_lactation_status=True)
    record.no_pregnancy_or_lactation_status = False
    record.user_edited_fields = [FIELD]
    record.save()

    recompute_patient_record_fields(record)
    record.refresh_from_db()
    assert record.no_pregnancy_or_lactation_status is False

    refreshed = refresh_patient_record(record.person)
    assert refreshed.no_pregnancy_or_lactation_status is False


def test_unknown_is_storable_and_survives_recompute():
    record = PatientRecordFactory(no_pregnancy_or_lactation_status=True)
    record.no_pregnancy_or_lactation_status = None
    record.user_edited_fields = [FIELD]
    record.save()

    recompute_patient_record_fields(record)
    record.refresh_from_db()
    assert record.no_pregnancy_or_lactation_status is None

    refreshed = refresh_patient_record(record.person)
    assert refreshed.no_pregnancy_or_lactation_status is None


def test_the_seeded_formula_is_inactive():
    row = FieldFormula.objects.filter(field_name=FIELD).first()
    if row is None:
        # pytest.ini runs with --no-migrations, so data migrations (0167/0168)
        # do not seed this row; the Django runner applies them and runs this.
        pytest.skip('seed data migrations do not run under --no-migrations')
    assert row.is_active is False


def test_an_active_formula_for_the_field_is_refused():
    with pytest.raises(IntegrityError):
        with transaction.atomic():
            FieldFormula.objects.create(
                field_name=FIELD,
                formula='@not(pregnancy_test_result)',
                is_active=True,
            )


def test_the_descriptor_still_offers_the_answer_as_writable():
    entry = build_writable_field_descriptor()[FIELD]
    assert entry['kind'] == 'direct'
    assert entry['writable'] is True
    assert entry['value_kind'] == 'boolean'


def test_the_shared_form_writes_false_and_clears_to_unknown():
    from omop_core.services.patient_form import apply_patient_form_patch

    record = PatientRecordFactory(no_pregnancy_or_lactation_status=True)

    saved, result = apply_patient_form_patch(record.person, {FIELD: False})
    assert FIELD in result.applied
    assert saved.no_pregnancy_or_lactation_status is False

    saved, result = apply_patient_form_patch(record.person, {FIELD: None})
    assert FIELD in result.applied
    assert saved.no_pregnancy_or_lactation_status is None
