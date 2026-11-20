"""Bulk resolve totals and provisional measurement units (#2530 follow-ups)."""
from decimal import Decimal

import pytest

from omop_core.mapping.code_resolution import NO_MATCHING_CONCEPT_ID
from omop_core.models import PatientSourceCode, SourceCodeConceptMapping, SourceCodeResolveRun
from omop_core.services.patient_record_service import _build_snapshot
from omop_core.services.resolve_jobs import create_and_dispatch_resolve_run
from tests.factories import (
    ConceptFactory, MeasurementFactory, PatientRecordFactory, PersonFactory,
)
from tests.test_canonical_units import _ucum_concept, setup_units  # noqa: F401 (fixture)

pytestmark = pytest.mark.django_db

CODE = 'GLU-1'


@pytest.fixture()
def world(settings):
    settings.PROVISIONAL_DATA_ENABLED = True
    settings.PROVISIONAL_CONFIDENCE_FLOOR = 70
    settings.CELERY_BROKER_URL = ''
    ConceptFactory(concept_id=NO_MATCHING_CONCEPT_ID, concept_code='0',
                   concept_name='No matching concept', standard_concept=None)
    ConceptFactory(concept_id=32817, concept_code='32817', concept_name='EHR')
    return ConceptFactory(concept_name='Proposed glucose')


def _person_with_code():
    person = PersonFactory()
    PatientRecordFactory(person=person)
    PatientSourceCode.objects.create(
        person=person, source_value=CODE, omop_table='measurement', source_vocabulary_id='Local')
    return person


def _proposal(target, status='proposed'):
    return SourceCodeConceptMapping.objects.create(
        source_code=CODE, source_vocabulary_id='Local', source_code_description=CODE,
        domain_id='Measurement', omop_table='measurement', target_concept=target,
        status=status, suggestion_confidence=0.85)


def test_bulk_run_counts_provisional_rows_written(world):
    people = [_person_with_code(), _person_with_code()]
    _proposal(world)

    run = create_and_dispatch_resolve_run([p.person_id for p in people])

    run.refresh_from_db()
    assert run.state == SourceCodeResolveRun.COMPLETED
    assert (run.created, run.provisional, run.reverted) == (2, 2, 0)


def test_bulk_run_counts_provisional_rows_reverted(world):
    person = _person_with_code()
    mapping = _proposal(world)
    first = create_and_dispatch_resolve_run([person.person_id])
    first.refresh_from_db()
    assert first.provisional == 1
    # The proposal is rejected; the next run takes its row back to unresolved.
    mapping.status = 'rejected'
    mapping.save()

    second = create_and_dispatch_resolve_run([person.person_id])

    second.refresh_from_db()
    assert (second.provisional, second.reverted) == (0, 1)


def test_run_status_endpoint_reports_the_new_counts(world):
    from django.contrib.auth import get_user_model
    from rest_framework.test import APIClient
    person = _person_with_code()
    _proposal(world)
    run = create_and_dispatch_resolve_run([person.person_id])
    user = get_user_model().objects.create_user(email='bulk-status@example.test', is_staff=True)
    client = APIClient()
    client.force_authenticate(user)

    body = client.get(f'/api/v1/resolve-runs/{run.id}/').json()

    assert (body['provisional'], body['reverted']) == (1, 0)


def _measurement(person, source_unit, stored_unit, value, sent, confidence):
    return MeasurementFactory(
        person=person, measurement_concept=ConceptFactory(concept_code='GLU'),
        unit_source_value=source_unit, value_as_number=Decimal(value), value_source_value=sent,
        unit_concept=_ucum_concept(stored_unit) if stored_unit else None, confidence=confidence)


def test_provisional_measurements_are_read_in_their_source_unit(setup_units, settings):  # noqa: F811
    settings.PROVISIONAL_DATA_ENABLED = True
    person = PersonFactory()
    PatientRecordFactory(person=person)
    # Stored in g/L but sent as mg/dL, and the sent number proves neither unit.
    unproven = _measurement(person, 'mg/dL', 'g/L', '0.00014', '14.4', confidence=80)
    # Stored converted from the sent number: proven, read in the source unit.
    proven = _measurement(person, 'K/uL', '/L', '3900000000', '3.9', confidence=80)

    snapshot = _build_snapshot(person)

    by_pk = {m.pk: m for m in snapshot.provisional_measurements}
    assert by_pk[unproven.pk].value_as_number is None
    assert by_pk[proven.pk].value_as_number == Decimal('3.9')
    assert snapshot.measurements == []
