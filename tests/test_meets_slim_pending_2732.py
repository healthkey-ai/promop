"""A pending edit of a SLiM input still moves the verdict on refresh (#2732).

`refresh_patient_record` derives `meets_slim` from OMOP, then restores pending
edits. Before this fix the restored input never reached the final recompute, so
a patient who edited clonal plasma cells saw the stale OMOP verdict.
"""
from decimal import Decimal

import pytest

from omop_core.models import PatientRecord
from omop_core.services.patient_record_service import refresh_patient_record
from tests.factories import PatientRecordFactory, PersonFactory

pytestmark = pytest.mark.django_db


def _pending(**fields):
    person = PersonFactory()
    record = PatientRecordFactory(person=person, **fields)
    record.user_edited_fields = sorted(fields)
    record.save()
    return person, record


def test_a_pending_slim_input_decides_the_verdict_and_survives_two_refreshes():
    person, record = _pending(clonal_plasma_cells=Decimal('65'))

    first = refresh_patient_record(person)
    second = refresh_patient_record(person)
    record = PatientRecord.objects.get(pk=record.pk)

    assert (first.meets_slim, second.meets_slim) == (True, True)
    assert record.meets_slim is True
    assert record.clonal_plasma_cells == Decimal('65.00')
    assert record.user_edited_fields == ['clonal_plasma_cells']


def test_a_pending_input_below_the_threshold_is_not_a_positive_verdict():
    person, record = _pending(clonal_plasma_cells=Decimal('59'))

    refresh_patient_record(person)
    record.refresh_from_db()

    # 59% fails the >=60% criterion, and the other SLiM criteria are unknown,
    # so the three-valued rule answers None rather than asserting "not met".
    assert record.meets_slim is None
    assert record.user_edited_fields == ['clonal_plasma_cells']
