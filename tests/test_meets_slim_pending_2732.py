"""A pending edit of a SLiM input still moves the verdict on refresh (#2732).

`refresh_patient_record` derives `meets_slim` from OMOP, then restores pending
edits. Before this fix the restored input never reached the final recompute, so
a patient who edited clonal plasma cells saw the stale OMOP verdict.
"""
from decimal import Decimal

import pytest

from omop_core.models import FieldConceptMapping, PatientRecord
from omop_core.services.patient_record_service import refresh_patient_record
from tests.factories import (
    ConceptFactory,
    MeasurementFactory,
    PatientRecordFactory,
    PersonFactory,
    VocabularyFactory,
)

pytestmark = pytest.mark.django_db


def _measure(person, code, value, date, unit=None):
    """A coded lab row, the shape the SLiM scan reads."""
    return MeasurementFactory(
        person=person,
        measurement_concept=ConceptFactory(
            concept_code=code,
            vocabulary=VocabularyFactory(vocabulary_id='LOINC'),
        ),
        measurement_source_value=f'Lab {code}',
        measurement_date=date,
        value_as_number=value,
        unit_source_value=unit,
    )


def _mapped_text_marrow(person):
    """A marrow result only an approved mapping can read: text, no number."""
    concept = ConceptFactory(
        concept_code='11118-7', vocabulary=VocabularyFactory(vocabulary_id='LOINC'),
    )
    FieldConceptMapping.objects.create(
        field_name='clonal_plasma_cells', concept=concept,
        omop_table='measurement', source_value='Marrow plasma cells',
        status='approved',
    )
    return MeasurementFactory(
        person=person, measurement_concept=concept,
        measurement_source_value='Marrow plasma cells',
        measurement_date='2024-06-01', unit_source_value='%',
        value_as_string='65', value_as_number=None,
    )


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


def test_a_pending_marrow_value_keeps_the_scans_light_chain_assessment():
    """Chains from two draws must not become a criterion because the marrow
    edit made the refresh recompute the verdict (#2876)."""
    person, record = _pending(clonal_plasma_cells=Decimal('59'))
    _measure(person, '36916-5', 300.0, '2023-01-01', 'mg/L')
    _measure(person, '33944-0', 1.0, '2024-06-01', 'mg/L')

    refreshed = refresh_patient_record(person)

    # The record holds both chains, but from different draws. Only the scan's
    # same-event pair may assess the light criterion, and it has none, so the
    # verdict is unknown instead of the ratio True that dividing them gives.
    assert refreshed.kappa_flc == Decimal('300')
    assert refreshed.lambda_flc == Decimal('1')
    assert refreshed.meets_slim is None


def test_a_pending_chain_without_a_counterpart_from_its_draw_is_unknown():
    """A reader-supplied chain beside a counterpart another draw happened to
    supply is not a pair, so the light criterion stays unknown (#2876)."""
    person, record = _pending(kappa_flc=Decimal('300'))
    _measure(person, '36916-5', 2.0, '2023-01-01', 'mg/L')
    _measure(person, '33944-0', 1.0, '2024-06-01', 'mg/L')
    _measure(person, '11118-7', 15.0, '2024-06-01')

    refreshed = refresh_patient_record(person)

    # Dividing the pending 300 by the derived 1 would reach the ratio boundary
    # and assert the defining event; the scan has no same-event pair, and one
    # chain is not one.
    assert refreshed.kappa_flc == Decimal('300')
    assert refreshed.meets_slim is None
    assert refreshed.user_edited_fields == ['kappa_flc']


def test_two_pending_chains_are_a_pair():
    """Both chains supplied by the reader form the pair the criterion reads."""
    person, record = _pending(kappa_flc=Decimal('300'), lambda_flc=Decimal('2'))
    _measure(person, '11118-7', 10.0, '2024-06-01')

    refreshed = refresh_patient_record(person)

    assert refreshed.meets_slim is True


def test_a_pending_chain_keeps_an_older_qualifying_marrow():
    """The scan counts any qualifying marrow result; editing a chain must not
    shrink that to the newest result (#2876)."""
    person, record = _pending(kappa_flc=Decimal('300'))
    _measure(person, '11118-7', 65.0, '2023-01-01')
    _measure(person, '11118-7', 15.0, '2024-06-01')

    refreshed = refresh_patient_record(person)

    # The record's display value is the newest marrow result, which fails
    # Sixty; the scan saw the older 65 %, and that criterion is not pending.
    assert float(refreshed.clonal_plasma_cells) == 15.0
    assert refreshed.meets_slim is True


def test_a_confirmed_marrow_result_below_threshold_reads_unknown():
    """The scan agrees with an edit: a criterion with no evidence is unknown,
    not "not met" (#2876)."""
    person = PersonFactory()
    PatientRecordFactory(person=person)
    _measure(person, '11118-7', 59.0, '2024-06-01')

    assert refresh_patient_record(person).meets_slim is None


def test_a_mapped_text_marrow_result_survives_a_pending_chain_edit():
    """An approved mapping can carry the marrow percentage as text. The scan
    reads it too, so the qualifying result is not lost (#2876)."""
    person, record = _pending(kappa_flc=Decimal('300'))
    _mapped_text_marrow(person)

    refreshed = refresh_patient_record(person)

    assert refreshed.clonal_plasma_cells == Decimal('65')
    assert refreshed.meets_slim is True


def test_a_mapped_text_marrow_result_assesses_sixty_without_any_edit():
    """The scan reads the text value an approved mapping wrote, so a record is
    not left unknown while its own marrow result says 65 % (#2876)."""
    person = PersonFactory()
    PatientRecordFactory(person=person)
    _mapped_text_marrow(person)

    refreshed = refresh_patient_record(person)

    assert refreshed.clonal_plasma_cells == Decimal('65')
    assert refreshed.meets_slim is True


def test_a_direct_save_answers_provisionally_until_the_next_refresh():
    """A direct edit does not read OMOP history, so it cannot see that the two
    chains come from different draws: it may answer True where the next refresh
    answers unknown. #2885 tracks closing that gap; this pins the difference so
    it stays deliberate."""
    from omop_core.services.patient_record_service import (
        recompute_patient_record_fields,
    )

    person = PersonFactory()
    PatientRecordFactory(person=person)
    _measure(person, '36916-5', 300.0, '2023-01-01', 'mg/L')
    _measure(person, '33944-0', 1.0, '2024-06-01', 'mg/L')
    refresh_patient_record(person)

    # The PATCH path loads its own instance, as the API does.
    record = PatientRecord.objects.get(person=person)
    record.clonal_plasma_cells = Decimal('10')
    recompute_patient_record_fields(record, changed_fields={'clonal_plasma_cells'})
    assert record.meets_slim is True

    assert refresh_patient_record(person).meets_slim is None
