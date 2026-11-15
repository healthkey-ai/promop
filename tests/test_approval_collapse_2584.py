"""#2584: approval collapse keeps the approved row over a repointed proposal.

`_collapse_duplicates` runs after approval repoints a provisional row onto the
approved concept. If the approved fact for the same event already exists, the
earliest-row rule deleted it and kept the provisional row, which derivation
ignores. The approved row is the survivor.
"""
from datetime import date, datetime, timezone as dt_timezone

import pytest

from omop_core.mapping.code_resolution import repoint_clinical_rows
from omop_core.models import Measurement, SourceCodeConceptMapping
from tests.factories import ConceptFactory, MeasurementFactory, PersonFactory

pytestmark = pytest.mark.django_db


def _mapping(concept):
    return SourceCodeConceptMapping.objects.create(
        source_code='CODE-2584', target_concept=concept, omop_table='measurement',
        status='approved')


def _row(person, concept, confidence):
    return MeasurementFactory(
        person=person,
        measurement_concept=concept,
        measurement_source_value='CODE-2584',
        measurement_date=date(2024, 1, 1),
        measurement_datetime=datetime(2024, 1, 1, 8, 0, tzinfo=dt_timezone.utc),
        value_as_number=40,
        value_as_string='',
        confidence=confidence,
    )


def test_approval_keeps_a_later_approved_row_over_the_repointed_proposal():
    person = PersonFactory()
    proposed = ConceptFactory(concept_code='PROPOSED-2584')
    approved = ConceptFactory(concept_code='APPROVED-2584', concept_name='Albumin [Mass/volume]')
    mapping = _mapping(approved)
    proposal = _row(person, proposed, 50)
    real = _row(person, approved, None)

    result = repoint_clinical_rows(
        mapping=mapping, old_concept_id=proposed.pk, new_concept_id=approved.pk)

    assert result['rows_collapsed'] == 1
    assert not Measurement.objects.filter(pk=proposal.pk).exists()
    real.refresh_from_db()
    assert real.measurement_concept_id == approved.pk
    assert real.confidence is None


def test_approval_collapses_only_proposals_onto_the_earliest_when_none_is_approved():
    person = PersonFactory()
    proposed = ConceptFactory(concept_code='PROPOSED-2584B')
    approved = ConceptFactory(concept_code='APPROVED-2584B')
    mapping = _mapping(approved)
    first = _row(person, proposed, 50)
    # A second proposal at the target concept, no approved row.
    second = _row(person, approved, 40)

    result = repoint_clinical_rows(
        mapping=mapping, old_concept_id=proposed.pk, new_concept_id=approved.pk)

    # Two proposals are still one event: the earliest survives (the behaviour
    # before #2584, pinned here so the approved-row preference does not change it).
    assert result['rows_collapsed'] == 1
    assert not Measurement.objects.filter(pk=second.pk).exists()
    first.refresh_from_db()
    assert (first.measurement_concept_id, first.confidence) == (approved.pk, 50)
