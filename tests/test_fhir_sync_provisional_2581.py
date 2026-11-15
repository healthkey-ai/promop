"""#2581: a FHIR sync keeps an approved row over an earlier provisional one.

The bulk planner has the same rule (#2543). The sync paths had kept the earliest
row and collapsed the rest, so a later approved fact was deleted behind a
provisional proposal and the survivor stayed invisible to derivation.
"""
from datetime import date, datetime, timezone as dt_timezone
from types import SimpleNamespace

import pytest

from omop_core.models import Measurement
from patient_portal.api.fhir.sync import AGGREGATION_EXT_URL, FhirSyncView
from tests.factories import ConceptFactory, PersonFactory

pytestmark = pytest.mark.django_db

DAY = date(2025, 10, 24)


def _stored(person, concept, type_concept, pk, confidence, source='2823-3', value='4.0'):
    return Measurement.objects.create(
        measurement_id=pk,
        person=person,
        measurement_concept=concept,
        measurement_type_concept=type_concept,
        measurement_date=DAY,
        measurement_datetime=datetime(2025, 10, 24, 8, 0, tzinfo=dt_timezone.utc),
        measurement_source_value=source,
        value_as_number=value,
        confidence=confidence,
    )


def _daily(value='5.0'):
    return {
        'resourceType': 'Observation',
        'status': 'final',
        'effectiveDateTime': '2025-10-24T08:00:00Z',
        'code': {'coding': [{'system': 'http://loinc.org', 'code': '2823-3'}]},
        'valueQuantity': {'value': value, 'unit': 'mmol/L'},
        'extension': [{'url': AGGREGATION_EXT_URL, 'valueCode': 'daily'}],
    }


@pytest.fixture
def sync():
    person = PersonFactory()
    ehr = ConceptFactory(concept_name='EHR', concept_code='EHR-2581')
    stored = ConceptFactory(concept_name='Potassium (stored)', concept_code='LOCAL-2581')
    incoming = ConceptFactory(concept_name='Potassium (sync)', concept_code='NEW-2581')
    view = FhirSyncView()

    def rollup(observations, cache=None):
        return view._ingest_observations(
            person, observations, ehr, cache or {}, 'iss|sub', None, {})

    def clinical(instance):
        return view._upsert_clinical(
            Measurement, 'measurement_id', 'measurement_concept_id',
            'measurement_date', 'measurement_source_value', person,
            [instance], 'iss|sub', None)

    return SimpleNamespace(
        person=person, ehr=ehr, stored=stored, incoming=incoming,
        rollup=rollup, clinical=clinical,
    )


def test_the_daily_rollup_keeps_a_later_real_row(sync):
    _stored(sync.person, sync.stored, sync.ehr, 9101, 50)
    _stored(sync.person, sync.stored, sync.ehr, 9102, None)

    sync.rollup([_daily('5.0')])

    rows = list(Measurement.objects.filter(person=sync.person).order_by('measurement_id'))
    assert [row.measurement_id for row in rows] == [9102]
    assert rows[0].confidence is None
    assert rows[0].value_as_number == 5.0


def test_the_daily_rollup_promotes_a_lone_provisional_row(sync):
    _stored(sync.person, sync.stored, sync.ehr, 9103, 50)

    sync.rollup([_daily('5.5')], {('*', '2823-3'): sync.incoming})

    row = Measurement.objects.get(pk=9103)
    assert row.confidence is None
    assert row.measurement_concept_id == sync.incoming.concept_id
    assert row.value_as_number == 5.5


def test_the_daily_rollup_keeps_an_unresolved_proposal_provisional(sync):
    """A code the sync cannot resolve does not confirm the proposed mapping."""
    _stored(sync.person, sync.stored, sync.ehr, 9104, 50)

    sync.rollup([_daily('5.5')])

    row = Measurement.objects.get(pk=9104)
    assert row.confidence == 50
    assert row.measurement_concept_id == sync.stored.concept_id


def test_the_generic_upsert_keeps_a_later_real_row(sync):
    _stored(sync.person, sync.stored, sync.ehr, 9111, 50, source='FHIR-2581')
    _stored(sync.person, sync.stored, sync.ehr, 9112, None, source='FHIR-2581')
    incoming = Measurement(
        person=sync.person,
        measurement_concept=sync.incoming,
        measurement_type_concept=sync.ehr,
        measurement_date=DAY,
        measurement_datetime=datetime(2025, 10, 24, 8, 0, tzinfo=dt_timezone.utc),
        measurement_source_value='FHIR-2581',
        value_as_number=4.0,
    )

    sync.clinical(incoming)

    rows = list(Measurement.objects.filter(person=sync.person).order_by('measurement_id'))
    assert [row.measurement_id for row in rows] == [9112]
    assert rows[0].confidence is None
    assert rows[0].measurement_concept_id == sync.incoming.concept_id


def test_the_daily_rollup_promotes_when_only_confidence_changes(sync):
    _stored(sync.person, sync.incoming, sync.ehr, 9105, 50, value='5.0')

    sync.rollup([_daily('5.0')], {('*', '2823-3'): sync.incoming})

    assert Measurement.objects.get(pk=9105).confidence is None


def test_the_generic_upsert_promotes_when_only_confidence_changes(sync):
    _stored(sync.person, sync.incoming, sync.ehr, 9115, 50, source='FHIR-2581')
    incoming = Measurement(
        person=sync.person,
        measurement_concept=sync.incoming,
        measurement_type_concept=sync.ehr,
        measurement_date=DAY,
        measurement_datetime=datetime(2025, 10, 24, 8, 0, tzinfo=dt_timezone.utc),
        measurement_source_value='FHIR-2581',
        value_as_number=4.0,
    )

    sync.clinical(incoming)

    assert Measurement.objects.get(pk=9115).confidence is None


def test_a_patient_self_service_sync_does_not_clear_confidence(sync):
    from patient_portal.api.fhir.sync import FhirPatientSyncView

    _stored(sync.person, sync.stored, sync.ehr, 9116, 50, source='FHIR-2581')
    incoming = Measurement(
        person=sync.person,
        measurement_concept=sync.incoming,
        measurement_type_concept=sync.ehr,
        measurement_date=DAY,
        measurement_datetime=datetime(2025, 10, 24, 8, 0, tzinfo=dt_timezone.utc),
        measurement_source_value='FHIR-2581',
        value_as_number=4.0,
    )

    FhirPatientSyncView()._upsert_clinical(
        Measurement, 'measurement_id', 'measurement_concept_id',
        'measurement_date', 'measurement_source_value', sync.person,
        [incoming], 'iss|sub', None)

    assert Measurement.objects.get(pk=9116).confidence == 50


def test_an_incoming_proposal_does_not_clear_confidence_of_the_survivor(sync):
    _stored(sync.person, sync.stored, sync.ehr, 9117, 50, source='FHIR-2581')
    incoming = Measurement(
        person=sync.person,
        measurement_concept=sync.incoming,
        measurement_type_concept=sync.ehr,
        measurement_date=DAY,
        measurement_datetime=datetime(2025, 10, 24, 8, 0, tzinfo=dt_timezone.utc),
        measurement_source_value='FHIR-2581',
        value_as_number=4.0,
        confidence=50,
    )

    sync.clinical(incoming)

    assert Measurement.objects.get(pk=9117).confidence == 50


def test_promotion_marks_the_patient_stale(sync):
    from omop_core.models import PatientRecord
    from tests.factories import PatientRecordFactory

    record = PatientRecordFactory(person=sync.person)
    PatientRecord.objects.filter(pk=record.pk).update(derivation_version=7)
    _stored(sync.person, sync.stored, sync.ehr, 9118, 50, source='FHIR-2581')
    incoming = Measurement(
        person=sync.person,
        measurement_concept=sync.incoming,
        measurement_type_concept=sync.ehr,
        measurement_date=DAY,
        measurement_datetime=datetime(2025, 10, 24, 8, 0, tzinfo=dt_timezone.utc),
        measurement_source_value='FHIR-2581',
        value_as_number=4.0,
    )

    sync.clinical(incoming)

    record.refresh_from_db()
    assert record.derivation_version == 0


def test_the_generic_upsert_keeps_an_unresolved_proposal_provisional(sync):
    _stored(sync.person, sync.stored, sync.ehr, 9114, 50, source='FHIR-2581')
    incoming = Measurement(
        person=sync.person,
        measurement_concept_id=0,
        measurement_type_concept=sync.ehr,
        measurement_date=DAY,
        measurement_datetime=datetime(2025, 10, 24, 8, 0, tzinfo=dt_timezone.utc),
        measurement_source_value='FHIR-2581',
        value_as_number=4.0,
    )

    sync.clinical(incoming)

    row = Measurement.objects.get(pk=9114)
    assert row.confidence == 50
    assert row.measurement_concept_id == sync.stored.concept_id


def test_the_generic_upsert_promotes_a_lone_provisional_row(sync):
    _stored(sync.person, sync.stored, sync.ehr, 9113, 50, source='FHIR-2581')
    incoming = Measurement(
        person=sync.person,
        measurement_concept=sync.incoming,
        measurement_type_concept=sync.ehr,
        measurement_date=DAY,
        measurement_datetime=datetime(2025, 10, 24, 8, 0, tzinfo=dt_timezone.utc),
        measurement_source_value='FHIR-2581',
        value_as_number=4.0,
    )

    sync.clinical(incoming)

    row = Measurement.objects.get(pk=9113)
    assert row.confidence is None
    assert row.measurement_concept_id == sync.incoming.concept_id


class _Candidate:
    def __init__(self, pk, confidence, concept_id, attribution):
        self.pk = pk
        self.confidence = confidence
        self.measurement_concept_id = concept_id
        self.attribution = attribution


def test_a_real_row_from_another_hospital_is_not_the_survivor():
    from patient_portal.api.fhir.sync import _preferred_keep, _same_attribution_extras

    provisional = _Candidate(1, 50, 5, ('A',))
    other_hospital = _Candidate(2, None, 5, ('B',))

    keep = _preferred_keep([provisional, other_hospital], 'measurement_concept_id')

    assert keep is provisional
    assert _same_attribution_extras([provisional, other_hospital], keep) == []


def test_the_callers_own_row_is_preferred_over_an_earlier_foreign_one():
    from types import SimpleNamespace

    from patient_portal.api.fhir.sync import _preferred_keep

    foreign_real = _Candidate(1, None, 5, (2,))
    own_provisional = _Candidate(2, 50, 5, (1,))

    keep = _preferred_keep(
        [foreign_real, own_provisional], 'measurement_concept_id',
        SimpleNamespace(pk=1))

    assert keep is own_provisional


def test_a_real_row_from_the_same_hospital_is_the_survivor():
    from patient_portal.api.fhir.sync import _preferred_keep, _same_attribution_extras

    provisional = _Candidate(1, 50, 5, ('A',))
    real = _Candidate(2, None, 6, ('A',))

    keep = _preferred_keep([provisional, real], 'measurement_concept_id')

    assert keep is real
    assert _same_attribution_extras([provisional, real], keep) == [provisional]
