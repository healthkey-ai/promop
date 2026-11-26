"""POST /api/v1/<clinical>/upsert_if_mapped/: resolve the code, then write (#2813).

One call for what a producer otherwise does in two: ask
``code-mappings/lookup/`` for the concept, then post the row. A row whose
source code has an approved mapping is written at that mapping's concept by
the bulk upsert; one that has none is left out and reported.
"""
from decimal import Decimal

import pytest
from django.core.cache import cache
from django.db import connection
from django.test.utils import CaptureQueriesContext
from rest_framework.test import APIClient

from omop_core.models import (
    Measurement, Observation, ProvenanceRecord, SourceCodeAxisVariant, SourceCodeConceptMapping,
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
        'status': 'proposed', 'mapping_id': queued.pk, 'rows': [1],
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
    assert entry['reason'] == 'unmapped' and entry['status'] == 'proposed'


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


# --- which table ----------------------------------------------------------


@pytest.fixture
def sleep(db):
    DomainFactory(domain_id='Observation', domain_name='Observation')
    concept = ConceptFactory(concept_code='93832-4', concept_name='Sleep duration',
                             domain=DomainFactory(domain_id='Observation'))
    _mapping('sleep', concept, table='observation')
    return concept


def test_a_code_filed_under_another_table_is_not_written_here(client, row, sleep):
    resp = _post(client, [row('sleep', value='7.5')])

    assert resp.status_code == 200, resp.data
    assert not Measurement.objects.exists()
    [entry] = resp.data['unmapped']
    assert entry['reason'] == 'other_table' and entry['omop_table'] == 'observation'
    assert entry['status'] == 'approved'


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

    provisional = row(confidence=0.9)

    resp = _post(client, [row(), with_concept, no_vocabulary, no_code, 'not a row', provisional])

    assert resp.status_code == 400, resp.data
    assert resp.data[0] == {}
    assert list(resp.data[1]) == ['measurement_concept']
    assert list(resp.data[2]) == ['source_vocabulary_id']
    assert list(resp.data[3]) == ['measurement_source_value']
    assert list(resp.data[4]) == ['non_field_errors']
    assert list(resp.data[5]) == ['confidence']
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


# --- review of #2815 ------------------------------------------------------


@pytest.mark.parametrize('path, table, prefix, date_field', [
    ('measurements', 'measurement', 'measurement', 'measurement_date'),
    ('observations', 'observation', 'observation', 'observation_date'),
    ('conditions', 'condition', 'condition', 'condition_start_date'),
    ('drug-exposures', 'drug_exposure', 'drug', 'drug_exposure_start_date'),
    ('procedures', 'procedure', 'procedure', 'procedure_date'),
])
def test_every_clinical_endpoint_has_it(client, person, type_concept, path, table, prefix, date_field):
    from omop_core.mapping.code_resolution import CLINICAL_TABLES

    model, concept_column, source_column = CLINICAL_TABLES[table]
    concept = ConceptFactory(concept_code=f'ANY-{table}', concept_name=table)
    _mapping('CODE-1', concept, vocabulary='LocalLab', table=table)
    type_field = 'drug_type_concept' if table == 'drug_exposure' else f'{prefix}_type_concept'

    resp = _post(client, [
        {'person': person.person_id, 'source_vocabulary_id': 'LocalLab', source_column: 'CODE-1',
         date_field: '2026-11-20', type_field: type_concept.concept_id},
        {'person': person.person_id, 'source_vocabulary_id': 'LocalLab', source_column: 'CODE-2',
         date_field: '2026-11-20', type_field: type_concept.concept_id},
    ], url=f'/api/v1/{path}/upsert_if_mapped/')

    assert resp.status_code == 201, resp.data
    assert resp.data['created'] == 1 and resp.data['ids'][1] is None
    stored = model.objects.get(pk=resp.data['ids'][0])
    assert getattr(stored, concept_column) == concept.concept_id
    assert [entry['source_code'] for entry in resp.data['unmapped']] == ['CODE-2']


def test_an_empty_list_is_an_empty_answer(client):
    resp = _post(client, [])

    assert resp.status_code == 200, resp.data
    assert resp.data['ids'] == [] and resp.data['created'] == 0 and resp.data['unmapped'] == []


def test_a_service_credential_writes_and_its_rows_are_attributed(db, row, steps, settings):
    settings.SERVICE_AUTH_SCOPES = 'patient/*.read system/etl.write'
    service = Identity.objects.create(issuer='urn:service', sub='mapped-writes-etl')
    client = APIClient()
    client.force_authenticate(user=service, token='service-token')

    resp = _post(client, [row(), row('nope')], HTTP_X_PROVENANCE_SOURCE='open-wearables')

    assert resp.status_code == 201, resp.data
    assert resp.data['created'] == 1 and resp.data['skipped'] == 1
    assert ProvenanceRecord.objects.filter(source='open-wearables').count() == 1


def test_a_read_only_service_credential_is_refused(db, row, steps, settings):
    settings.SERVICE_AUTH_SCOPES = 'patient/*.read'
    service = Identity.objects.create(issuer='urn:service', sub='mapped-writes-reader')
    client = APIClient()
    client.force_authenticate(user=service, token='service-token')

    resp = _post(client, [row('nope')])

    assert resp.status_code == 403
    assert not SourceCodeConceptMapping.objects.filter(source_code='nope').exists()


def test_an_admin_of_another_organization_resolves_nothing(db, row, steps):
    from omop_core.models import GroupAccess

    elsewhere = OrganizationFactory()
    admin = Identity.objects.create_user(email='other-admin-2813@example.org', password='test')
    GroupAccess.objects.create(identity=admin, org=elsewhere, role='org_admin')
    client = APIClient()
    client.force_authenticate(admin)

    resp = _post(client, [row('nope'), row()])

    assert resp.status_code == 403, resp.data
    assert not SourceCodeConceptMapping.objects.filter(source_code='nope').exists()
    assert not Measurement.objects.exists()


def test_an_approved_mapping_unusable_here_is_reported_with_its_table(client, row):
    from omop_core.mapping.code_resolution import IDENTITY_ORIGIN

    finding = ConceptFactory(
        concept_code='44054006', concept_name='Diabetes',
        vocabulary__vocabulary_id='SNOMED', domain=DomainFactory(domain_id='Condition'))
    identity = SourceCodeConceptMapping.objects.create(
        source_vocabulary_id='SNOMED', source_code='44054006', target_concept=finding,
        source_concept=finding, destination_vocabulary_id='SNOMED', domain_id='Condition',
        omop_table='condition', status='approved', origin='import', origin_system=IDENTITY_ORIGIN)

    resp = _post(client, [row('44054006', source_vocabulary_id='SNOMED')])

    assert resp.status_code == 200, resp.data
    assert not Measurement.objects.exists()
    assert resp.data['unmapped'] == [{
        'source_vocabulary_id': 'SNOMED', 'source_code': '44054006',
        'reason': 'destination_domain_or_validity_mismatch', 'status': 'approved',
        'omop_table': 'condition', 'mapping_id': identity.pk, 'rows': [0],
    }]


def test_a_curated_mappings_table_decides_whatever_the_concepts_domain(client, row, person, type_concept):
    concept = ConceptFactory(concept_code='REFILED-1', concept_name='Refiled')   # a Measurement concept
    _mapping('refiled', concept, table='observation')

    here = _post(client, [row('refiled')])
    there = _post(client, [{
        'person': person.person_id, 'source_vocabulary_id': VOCABULARY,
        'observation_source_value': 'refiled', 'observation_date': '2026-11-20',
        'observation_type_concept': type_concept.concept_id, 'value_as_number': '1',
    }], url=OBSERVATIONS_URL)

    assert here.status_code == 200 and here.data['unmapped'][0]['reason'] == 'other_table'
    assert here.data['unmapped'][0]['omop_table'] == 'observation'
    assert not Measurement.objects.exists()
    assert there.status_code == 201, there.data
    assert Observation.objects.get().observation_concept_id == concept.concept_id


def test_a_cached_direct_hit_follows_the_concepts_domain(client, row):
    concept = ConceptFactory(concept_code='718-7', concept_name='Hemoglobin')    # a Measurement concept
    _mapping('718-7', concept, vocabulary='LOINC', table='observation', origin_system='athena-direct')

    resp = _post(client, [row('718-7', source_vocabulary_id='LOINC')])

    assert resp.status_code == 201, resp.data
    assert Measurement.objects.get().measurement_concept_id == concept.concept_id


@pytest.mark.parametrize('change, field', [
    ({'source_vocabulary_id': 'Open\x00Wearables'}, 'source_vocabulary_id'),
    ({'source_vocabulary_id': 'V' * 256}, 'source_vocabulary_id'),
    ({'measurement_source_value': 'st\x00eps'}, 'measurement_source_value'),
    ({'measurement_source_value': 'C' * 101}, 'measurement_source_value'),
    ({'source_text': 'Steps\x00'}, 'source_text'),
])
def test_what_reaches_the_resolver_is_checked_first(client, row, steps, change, field):
    resp = _post(client, [{**row('fresh-code'), **change}])

    assert resp.status_code == 400, resp.data
    assert list(resp.data[0]) == [field]
    assert SourceCodeConceptMapping.objects.count() == 1    # the fixture's own


@pytest.mark.parametrize('person_value', ['--5', '²', '5.0', ' ', [], {}, True, 1.5])
def test_a_malformed_person_is_a_400(client, row, steps, person_value):
    resp = _post(client, [{**row(), 'person': person_value}])

    assert resp.status_code == 400, resp.data
    assert not Measurement.objects.exists()


def test_a_first_sighting_in_the_wrong_table_is_not_written(client, row):
    """A direct vocabulary hit is cached under the table of whoever asked first."""
    DomainFactory(domain_id='Drug', domain_name='Drug')
    drug = ConceptFactory(concept_code='1191', concept_name='aspirin',
                          vocabulary__vocabulary_id='RxNorm', domain=DomainFactory(domain_id='Drug'))

    first = _post(client, [row('1191', source_vocabulary_id='RxNorm')])
    again = _post(client, [row('1191', source_vocabulary_id='RxNorm')])

    for resp in (first, again):
        assert resp.status_code == 200, resp.data
        [entry] = resp.data['unmapped']
        assert entry['reason'] == 'other_table' and entry['omop_table'] == 'drug_exposure'
    assert not Measurement.objects.filter(measurement_concept_id=drug.concept_id).exists()


def test_a_code_longer_than_the_column_resolves_and_is_stored_cut_to_it(client, row):
    identifier = 'HKQuantityTypeIdentifierWalkingDoubleSupportPercentage'    # 54 characters
    concept = ConceptFactory(concept_code='HK-WEAR-DBL-SUPPORT', concept_name='Double support')
    _mapping(identifier, concept, vocabulary='Apple')

    resp = _post(client, [row(identifier, source_vocabulary_id='Apple', value='28')])
    again = _post(client, [row(identifier, source_vocabulary_id='Apple', value='28')])

    assert resp.status_code == 201, resp.data
    stored = Measurement.objects.get()
    assert stored.measurement_concept_id == concept.concept_id
    assert stored.measurement_source_value == identifier[:50]
    assert again.data['created'] == 0 and again.data['ids'] == resp.data['ids']


def test_key_ambiguous_names_the_requests_positions(client, row, steps):
    _post(client, [row()])                                   # one stored row, no key

    resp = _post(client, [row('nope'), row(source_record_key='ow:a'), row(source_record_key='ow:b')])

    assert resp.status_code == 201, resp.data
    assert resp.data['ids'][0] is None
    assert resp.data['key_ambiguous'] and set(resp.data['key_ambiguous']) <= {1, 2}


def test_the_unmapped_parameter_is_not_an_option(client, row, steps):
    """Nothing here stores an unresolved row: that is the bulk write's, with the inventory."""
    resp = _post(client, [row('nope')], query='?skip_refresh=true&unmapped=store')

    assert resp.status_code == 200, resp.data
    assert not Measurement.objects.exists()
