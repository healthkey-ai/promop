"""POST /api/v1/<clinical>/upsert_if_mapped/: resolve the code, then write (#2813).

One call for what a producer otherwise does in two: ask
``code-mappings/lookup/`` for the concept, then post the row. A row whose
source code has an approved mapping is written at that mapping's concept by
the bulk upsert; one that has none is left out, or stored at concept 0.
"""
from decimal import Decimal

import pytest
from django.core.cache import cache
from django.db import connection
from django.test.utils import CaptureQueriesContext
from rest_framework.test import APIClient

from omop_core.models import (
    Concept, Measurement, Observation, ProvenanceRecord, SourceCodeAxisVariant, SourceCodeConceptMapping,
)
from patient_portal.models import Identity
from tests.factories import (
    ConceptFactory, DomainFactory, OrganizationFactory, PatientRecordFactory, PersonFactory,
)

URL = '/api/v1/measurements/upsert_if_mapped/'
OBSERVATIONS_URL = '/api/v1/observations/upsert_if_mapped/'
VOCABULARY = 'OpenWearables'
EPIC = 'http://open.epic.com/FHIR/StructureDefinition/observation-flowsheet-id'


@pytest.fixture(autouse=True)
def _fresh_throttles():
    cache.clear()


@pytest.fixture
def client(db):
    client = APIClient()
    client.force_authenticate(Identity.objects.create_user(
        email='mapped-writes@example.org', password='test', is_staff=True))
    return client


@pytest.fixture
def person(db):
    person = PersonFactory()
    PatientRecordFactory(person=person)
    return person


@pytest.fixture
def type_concept(db):
    return ConceptFactory(concept_code='TYPE-2813', concept_name='Wearable')


def _mapping(code, concept, *, status='approved', vocabulary=VOCABULARY, table='measurement',
             organization=None, **extra):
    return SourceCodeConceptMapping.objects.create(
        organization=organization, source_vocabulary_id=vocabulary, source_code=code,
        source_code_description=code, target_concept=concept,
        destination_vocabulary_id='LOINC' if concept else '',
        domain_id='Observation' if table == 'observation' else 'Measurement',
        omop_table=table, status=status, origin='import', **extra)


@pytest.fixture
def steps(db):
    concept = ConceptFactory(concept_code='55423-8', concept_name='Number of steps')
    _mapping('steps', concept)
    return concept


@pytest.fixture
def row(person, type_concept):
    def make(code='steps', day='2026-11-20', value='4200', **extra):
        return {
            'person': person.person_id, 'source_vocabulary_id': VOCABULARY,
            'measurement_source_value': code, 'measurement_date': day,
            'measurement_type_concept': type_concept.concept_id,
            'value_as_number': value, **extra,
        }
    return make


def _post(client, rows, query='?skip_refresh=true', url=URL, **headers):
    return client.post(url + query, rows, format='json', **headers)


# --- what is written ------------------------------------------------------


def test_a_mapped_row_is_written_at_the_mappings_concept(client, row, steps, person):
    resp = _post(client, [row()])

    assert resp.status_code == 201, resp.data
    assert resp.data['created'] == 1 and resp.data['resolved'] == 1
    assert resp.data['skipped'] == 0 and resp.data['unmapped'] == []
    stored = Measurement.objects.get(pk=resp.data['ids'][0])
    assert stored.measurement_concept_id == steps.concept_id
    assert stored.person_id == person.person_id
    assert stored.measurement_source_value == 'steps'
    assert stored.value_as_number == Decimal('4200')
    assert resp['Cache-Control'] == 'no-store'


def test_one_object_is_a_batch_of_one(client, row, steps):
    resp = _post(client, row())

    assert resp.status_code == 201, resp.data
    assert len(resp.data['ids']) == 1 and resp.data['created'] == 1


def test_a_proposed_target_is_never_written(client, row, steps):
    proposed = ConceptFactory(concept_code='8867-4', concept_name='Heart rate')
    queued = _mapping('heart_rate', proposed, status='proposed')

    resp = _post(client, [row(), row('heart_rate', value='61')])

    assert resp.status_code == 201, resp.data
    assert resp.data['created'] == 1 and resp.data['skipped'] == 1
    assert resp.data['ids'][1] is None and resp.data['ids'][0] is not None
    assert not Measurement.objects.filter(measurement_source_value='heart_rate').exists()
    assert not Measurement.objects.filter(measurement_concept_id=proposed.concept_id).exists()
    assert resp.data['unmapped'] == [{
        'source_vocabulary_id': VOCABULARY, 'source_code': 'heart_rate', 'reason': 'unmapped',
        'status': 'proposed', 'stored': False, 'mapping_id': queued.pk, 'rows': [1],
    }]


def test_an_unknown_code_is_queued_and_the_call_succeeds(client, row):
    resp = _post(client, [row('never_seen', value='1'), row('never_seen', day='2026-11-21', value='2')])

    assert resp.status_code == 200, resp.data
    assert resp.data['created'] == 0 and resp.data['ids'] == [None, None]
    assert resp.data['skipped'] == 2 and resp.data['resolved'] == 0
    assert not Measurement.objects.exists()
    queued = SourceCodeConceptMapping.objects.get(
        source_vocabulary_id=VOCABULARY, source_code='never_seen')
    assert queued.status == 'proposed'
    [entry] = resp.data['unmapped']
    assert entry['rows'] == [0, 1] and entry['mapping_id'] == queued.pk
    assert entry['reason'] == 'unmapped' and entry['stored'] is False


def test_ids_keep_the_requests_positions(client, row, steps):
    resp = _post(client, [row('nope'), row(), row('nope', day='2026-11-21'), row(day='2026-11-22')])

    assert resp.status_code == 201, resp.data
    ids = resp.data['ids']
    assert ids[0] is None and ids[2] is None
    assert Measurement.objects.get(pk=ids[1]).measurement_date.isoformat() == '2026-11-20'
    assert Measurement.objects.get(pk=ids[3]).measurement_date.isoformat() == '2026-11-22'
    assert resp.data['resolved'] == 2 and resp.data['skipped'] == 2


def test_a_replay_writes_nothing_new(client, row, steps):
    rows = [row(), row(day='2026-11-21', value='5100')]
    first = _post(client, rows)
    again = _post(client, rows)

    assert again.status_code == 201, again.data
    assert again.data['created'] == 0 and again.data['ids'] == first.data['ids']
    assert Measurement.objects.count() == 2


def test_a_source_record_key_updates_the_days_row(client, row, steps):
    key = 'openwearables:u1/apple/steps/2026-11-20'
    first = _post(client, [row(source_record_key=key)])
    later = _post(client, [row(value='9000', source_record_key=key)])

    assert later.status_code == 201, later.data
    assert later.data['ids'] == first.data['ids'] and later.data['created'] == 0
    assert Measurement.objects.get().value_as_number == Decimal('9000')


def test_provenance_is_recorded_for_the_written_rows(client, row, steps):
    resp = _post(client, [row(), row('nope')], HTTP_X_PROVENANCE_SOURCE='open-wearables')

    assert resp.status_code == 201, resp.data
    record = ProvenanceRecord.objects.get(source='open-wearables')
    assert record.object_id == resp.data['ids'][0]


# --- unmapped=store ---------------------------------------------------------


def test_store_writes_an_unmapped_row_at_concept_zero(client, row, steps):
    ConceptFactory(concept_id=0, concept_code='No matching concept', concept_name='No matching concept')

    resp = _post(client, [row(), row('nope', value='7')], query='?skip_refresh=true&unmapped=store')

    assert resp.status_code == 201, resp.data
    assert resp.data['created'] == 2 and resp.data['resolved'] == 1 and resp.data['skipped'] == 0
    assert Measurement.objects.get(pk=resp.data['ids'][1]).measurement_concept_id == 0
    [entry] = resp.data['unmapped']
    assert entry['stored'] is True and entry['rows'] == [1]


def test_store_without_concept_zero_is_refused_before_anything_is_written(client, row, steps):
    Concept.objects.filter(concept_id=0).delete()

    resp = _post(client, [row(), row('nope')], query='?skip_refresh=true&unmapped=store')

    assert resp.status_code == 400, resp.data
    assert 'unmapped' in resp.data
    assert not Measurement.objects.exists()


def test_an_unknown_policy_is_a_400(client, row, steps):
    resp = _post(client, [row()], query='?unmapped=zero')

    assert resp.status_code == 400
    assert not Measurement.objects.exists()


# --- which table ----------------------------------------------------------


@pytest.fixture
def sleep(db):
    DomainFactory(domain_id='Observation', domain_name='Observation')
    concept = ConceptFactory(concept_code='93832-4', concept_name='Sleep duration',
                             domain=DomainFactory(domain_id='Observation'))
    _mapping('sleep', concept, table='observation')
    return concept


def test_a_code_filed_under_another_table_is_not_written_here(client, row, sleep):
    ConceptFactory(concept_id=0, concept_code='No matching concept', concept_name='No matching concept')

    for query in ('?skip_refresh=true', '?skip_refresh=true&unmapped=store'):
        resp = _post(client, [row('sleep', value='7.5')], query=query)

        assert resp.status_code == 200, resp.data
        assert not Measurement.objects.exists()
        [entry] = resp.data['unmapped']
        assert entry['reason'] == 'other_table' and entry['omop_table'] == 'observation'
        assert entry['stored'] is False and entry['status'] == 'approved'


def test_the_named_table_takes_it(client, person, type_concept, sleep):
    resp = _post(client, [{
        'person': person.person_id, 'source_vocabulary_id': VOCABULARY,
        'observation_source_value': 'sleep', 'observation_date': '2026-11-20',
        'observation_type_concept': type_concept.concept_id, 'value_as_number': '7.5',
    }], url=OBSERVATIONS_URL)

    assert resp.status_code == 201, resp.data
    assert Observation.objects.get(pk=resp.data['ids'][0]).observation_concept_id == sleep.concept_id


def test_a_unit_routes_a_row_to_its_axis_variant(client, row):
    mass = ConceptFactory(concept_code='2345-7', concept_name='Glucose [Mass/volume]')
    moles = ConceptFactory(concept_code='14749-6', concept_name='Glucose [Moles/volume]')
    mapping = _mapping('GLU', mass, axis_variant_count=2)
    SourceCodeAxisVariant.objects.create(
        mapping=mapping, axis_key='property=SCnc|scale_type=Qn',
        axis_values={'property': 'SCnc', 'scale_type': 'Qn'}, units=['mmol/L'],
        status='approved', target_concept=moles)

    resp = _post(client, [
        row('GLU', value='99', unit_source_value='mg/dL'),
        row('GLU', day='2026-11-21', value='5.5', unit_source_value='mmol/L'),
    ])

    assert resp.status_code == 201, resp.data
    by_unit = dict(Measurement.objects.values_list('unit_source_value', 'measurement_concept_id'))
    assert by_unit == {'mg/dL': mass.concept_id, 'mmol/L': moles.concept_id}


# --- hospital codes -------------------------------------------------------


def test_a_hospital_code_needs_an_organization(client, row):
    resp = _post(client, [row('10627', source_vocabulary_id=EPIC)])

    assert resp.status_code == 400, resp.data
    assert 'organization_id' in resp.data
    assert not SourceCodeConceptMapping.objects.filter(source_code='10627').exists()


def test_a_hospital_code_resolves_through_that_hospitals_mapping(client, row):
    ours, theirs = OrganizationFactory(), OrganizationFactory()
    mine = ConceptFactory(concept_code='OURS-1', concept_name='Ours')
    other = ConceptFactory(concept_code='THEIRS-1', concept_name='Theirs')
    _mapping('10627', mine, vocabulary=EPIC, organization=ours)
    _mapping('10627', other, vocabulary=EPIC, organization=theirs)

    resp = _post(client, [row('10627', source_vocabulary_id=EPIC)],
                 HTTP_X_PROVENANCE_SOURCE='etl',
                 HTTP_X_PROVENANCE_ORGANIZATION_ID=str(ours.pk))

    assert resp.status_code == 201, resp.data
    assert Measurement.objects.get().measurement_concept_id == mine.concept_id


# --- refused --------------------------------------------------------------


def test_what_a_row_must_and_must_not_carry(client, row, steps):
    with_concept = row(measurement_concept=steps.concept_id)
    no_vocabulary = row()
    del no_vocabulary['source_vocabulary_id']
    no_code = row(code='  ')

    resp = _post(client, [row(), with_concept, no_vocabulary, no_code, 'not a row'])

    assert resp.status_code == 400, resp.data
    assert resp.data[0] == {}
    assert list(resp.data[1]) == ['measurement_concept']
    assert list(resp.data[2]) == ['source_vocabulary_id']
    assert list(resp.data[3]) == ['measurement_source_value']
    assert list(resp.data[4]) == ['non_field_errors']
    assert not Measurement.objects.exists()


def test_a_row_the_serializer_refuses_is_named_by_its_request_position(client, row, steps):
    resp = _post(client, [row('nope'), row(), row(day='not-a-date')])

    assert resp.status_code == 400, resp.data
    assert resp.data[0] == {} and resp.data[1] == {}
    assert list(resp.data[2]) == ['measurement_date']
    assert not Measurement.objects.exists()


def test_one_batch_is_one_person(client, row, steps):
    other = PersonFactory()
    PatientRecordFactory(person=other)

    mixed = _post(client, [row(), {**row(), 'person': other.person_id}])
    missing = _post(client, [{**row(), 'person': None}])
    unknown = _post(client, [{**row(), 'person': 987654321}])

    assert mixed.status_code == 400 and 'exactly one person' in mixed.data['detail']
    assert missing.status_code == 400 and unknown.status_code == 400
    assert not Measurement.objects.exists()


def test_the_row_limit_is_the_bulk_writes(client, row, steps, monkeypatch):
    monkeypatch.setattr('patient_portal.api.views.OMOP_BULK_MAX_ROWS', 2)

    resp = _post(client, [row(), row(day='2026-11-21'), row(day='2026-11-22')])

    assert resp.status_code == 413
    assert resp.data['max_rows'] == 2


def test_the_duplicate_report_flags_are_refused(client, row, steps):
    resp = _post(client, [row()], query='?warn_concept_duplicates=true')

    assert resp.status_code == 400
    assert not Measurement.objects.exists()


# --- who ------------------------------------------------------------------


def test_a_signed_in_patient_cannot_use_it(db, row, steps):
    client = APIClient()
    client.force_authenticate(Identity.objects.create_user(
        email='patient-2813@example.org', password='test'))

    resp = _post(client, [row('nope')])

    assert resp.status_code == 403
    assert not SourceCodeConceptMapping.objects.filter(source_code='nope').exists()


def test_nobody_signed_in_cannot_use_it(db, row, steps):
    resp = _post(APIClient(), [row()])

    assert resp.status_code in (401, 403)
    assert not Measurement.objects.exists()


def test_the_legacy_prefix_does_not_have_it(client, row, steps):
    resp = client.post('/api/measurements/upsert_if_mapped/', [row()], format='json')

    assert resp.status_code in (404, 405)
    assert not Measurement.objects.exists()


# --- cost -----------------------------------------------------------------


def test_queries_do_not_grow_with_the_rows(client, row, steps):
    def cost(first_day, count):
        rows = [row(day=f'2026-{first_day:02d}-{day:02d}', value=str(day)) for day in range(1, count + 1)]
        rows += [row('nope', day=f'2026-{first_day:02d}-{day:02d}') for day in range(1, count + 1)]
        with CaptureQueriesContext(connection) as queries:
            resp = _post(client, rows)
        assert resp.status_code == 201, resp.data
        assert resp.data['created'] == count and resp.data['skipped'] == count
        return len(queries)

    cost(8, 2)   # the first call queues 'nope' and loads caches
    assert cost(9, 3) == cost(10, 25)
