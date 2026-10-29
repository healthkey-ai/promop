"""Measurement values are stored as their writer sent them, and stay that way.

promop converts units only when it reads (``canonical_units.normalize``). It
used to also rewrite stored values (#2092): at bulk create, and later in
repoint and resolve. Every writer matches its next run on the value it sent,
so a rewritten value broke them all (#2104):

- the ETL's bulk upsert and its own diff no longer found their rows;
- FHIR sync and lab-results sync inserted a duplicate beside a converted row,
  and a FHIR rollup wrote the sent number back under the converted unit;
- a conversion that did not fit the column aborted the transaction, silently
  undoing the curator's concept change (and failing a bulk create, #2101).

Each test below uses a mapping whose unit differs from what was stored, so
the old code would have converted the row.
"""
from datetime import date
from decimal import Decimal
from types import SimpleNamespace

import pytest
from rest_framework.test import APIClient

from omop_core.mapping.code_resolution import (
    repoint_clinical_rows, resolve_person_source_codes,
)
from omop_core.models import (
    Concept, ConceptRelationship, Measurement, Relationship, SourceCodeConceptMapping,
)
from patient_portal.models import Identity
from tests.factories import (
    ConceptFactory, MeasurementFactory, PatientRecordFactory, PersonFactory, VocabularyFactory,
)


def _ucum(code):
    existing = Concept.objects.filter(vocabulary_id='UCUM', concept_code=code).first()
    return existing or ConceptFactory(
        vocabulary=VocabularyFactory(vocabulary_id='UCUM'), concept_code=code,
        concept_name=code, standard_concept='S')


def _no_match():
    return Concept.objects.filter(pk=0).first() or ConceptFactory(
        concept_id=0, concept_code='0', concept_name='No matching concept')


def _mapping(source_code, unit, target=None):
    """An approved mapping that names a destination unit."""
    target = target or ConceptFactory(concept_code=f'T-{source_code}', concept_name=source_code)
    return SourceCodeConceptMapping.objects.create(
        source_code=source_code, target_concept=target, omop_table='measurement',
        status='approved', destination_unit_concept=_ucum(unit))


def _loinc(code, name, property_name):
    """A LOINC concept whose axes name a property and a quantitative scale."""
    concept = ConceptFactory(concept_code=code, concept_name=name, vocabulary_id='LOINC',
                             domain_id='Measurement', standard_concept='S')
    for relationship_id, axis_name in (('Has property', property_name),
                                       ('Has scale type', 'Quantitative')):
        Relationship.objects.get_or_create(relationship_id=relationship_id, defaults={
            'relationship_name': relationship_id, 'is_hierarchical': 0,
            'defines_ancestry': 0, 'reverse_relationship_id': '', 'relationship_concept_id': 0})
        ConceptRelationship.objects.create(
            concept_1=concept, concept_2=ConceptFactory(concept_name=axis_name,
                                                        concept_code=f'LP-{code}-{axis_name}'),
            relationship_id=relationship_id,
            valid_start_date=date(1970, 1, 1), valid_end_date=date(2099, 12, 31))
    return concept


def _stored(person):
    return [(r.value_as_number, r.unit_concept and r.unit_concept.concept_code)
            for r in Measurement.objects.filter(person=person).order_by('pk')]


@pytest.fixture
def staff_client(db):
    client = APIClient()
    client.force_authenticate(Identity.objects.create_user(
        email='values-as-sent@example.org', password='test', is_staff=True))
    return client


# ---------------------------------------------------------------------------
# Bulk OMOP write (the ETL)
# ---------------------------------------------------------------------------

def _bulk_row(person, concept, type_concept, *, unit_concept=None,
              value='140.00000', unit='mmol/L', source_value='Sodium Level'):
    row = {
        'person': person.person_id,
        'measurement_concept': concept.concept_id,
        'measurement_date': '2026-10-01',
        'measurement_type_concept': type_concept.concept_id,
        'value_as_number': value,
        'unit_source_value': unit,
        'measurement_source_value': source_value,
        'value_source_value': value,
    }
    if unit_concept is not None:
        row['unit_concept'] = unit_concept.concept_id
    return row


class TestBulkWrite:
    """POST /api/v1/measurements/ with a list, as the ETL sends it."""

    @pytest.fixture
    def sodium(self, db):
        person = PersonFactory()
        PatientRecordFactory(person=person)
        mapping = _mapping('Sodium Level', 'mol/L')
        return SimpleNamespace(
            person=person, concept=mapping.target_concept, mapping=mapping,
            mmol=_ucum('mmol/L'),
            type_concept=ConceptFactory(concept_code='EHR-BULK', concept_name='EHR'))

    def _post(self, client, rows, query='skip_refresh=true'):
        resp = client.post(f'/api/v1/measurements/?{query}', rows, format='json')
        assert resp.status_code == 201, resp.content
        return resp.json()

    @pytest.mark.parametrize('with_unit_concept', [True, False])
    def test_stored_as_sent(self, staff_client, sodium, with_unit_concept):
        # Without a unit concept is what the ETL sends when it refused to
        # convert (overflow, precision loss, no canonical unit).
        unit_concept = sodium.mmol if with_unit_concept else None
        body = self._post(staff_client, [_bulk_row(
            sodium.person, sodium.concept, sodium.type_concept, unit_concept=unit_concept)],
            query='skip_refresh=true&upsert=false')

        row = Measurement.objects.get(pk=body['ids'][0])
        assert row.value_as_number == Decimal('140')
        assert row.unit_concept_id == (unit_concept.concept_id if unit_concept else None)
        assert row.unit_source_value == 'mmol/L'

    def test_rerun_with_upsert_matches_instead_of_duplicating(self, staff_client, sodium):
        body = [_bulk_row(sodium.person, sodium.concept, sodium.type_concept,
                          unit_concept=sodium.mmol)]

        first = self._post(staff_client, body)
        second = self._post(staff_client, body)

        assert second['created'] == 0
        assert second['ids'] == first['ids']
        assert _stored(sodium.person) == [(Decimal('140'), 'mmol/L')]

    def test_import_repoint_rerun(self, staff_client, sodium):
        body = [_bulk_row(sodium.person, sodium.concept, sodium.type_concept,
                          unit_concept=sodium.mmol)]
        first = self._post(staff_client, body)
        new_concept = ConceptFactory(concept_code='SOD-NEW', concept_name='Sodium new')

        result = repoint_clinical_rows(
            mapping=sodium.mapping, old_concept_id=sodium.concept.concept_id,
            new_concept_id=new_concept.concept_id)

        assert result['rows_updated'] == 1
        row = Measurement.objects.get(pk=first['ids'][0])
        assert row.measurement_concept_id == new_concept.concept_id
        assert _stored(sodium.person) == [(Decimal('140'), 'mmol/L')]

        assert self._post(staff_client, body)['created'] == 0
        assert len(_stored(sodium.person)) == 1

    def test_import_resolve_rerun(self, staff_client, sodium):
        body = [_bulk_row(sodium.person, _no_match(), sodium.type_concept,
                          unit_concept=sodium.mmol)]
        first = self._post(staff_client, body)

        resolve_person_source_codes(sodium.person)

        row = Measurement.objects.get(pk=first['ids'][0])
        assert row.measurement_concept_id == sodium.concept.concept_id
        assert _stored(sodium.person) == [(Decimal('140'), 'mmol/L')]

        assert self._post(staff_client, body)['created'] == 0
        assert len(_stored(sodium.person)) == 1


def test_bulk_create_does_not_overflow_on_a_conversion_2101(staff_client):
    """Regression for #2101.

    The bulk write used to convert a count with no mapping to its LOINC
    property's base unit: 250 10*3/uL became 250000000000 /L, which does not fit
    Decimal(15, 5), so the whole batch failed with "numeric field overflow".
    #2101's rows are masked, so this reproduces that failure mode, not
    necessarily its exact rows. The batch also carries #2101's own row shape.
    """
    person = PersonFactory()
    PatientRecordFactory(person=person)
    platelets = _loinc('777-3', 'Platelets [#/volume] in Blood', 'Number Concentration')
    type_concept = ConceptFactory(concept_code='EHR-2101', concept_name='EHR')
    protein = ConceptFactory(concept_code='2888-6', concept_name='Protein')
    rows = [
        _bulk_row(person, platelets, type_concept, value='250', unit='10*3/uL',
                  source_value='PLT'),
        _bulk_row(person, platelets, type_concept, value='251', unit='10*3/uL',
                  source_value='PLT', unit_concept=_ucum('10*3/uL')),
        _bulk_row(person, protein, type_concept, value='0.00100', unit='mg/dL',
                  source_value='2888-6', unit_concept=_ucum('g/dL')),
    ]

    resp = staff_client.post('/api/v1/measurements/?skip_refresh=true&upsert=false',
                             rows, format='json')

    assert resp.status_code == 201, resp.content
    assert sorted(v for v, _ in _stored(person)) == [
        Decimal('0.001'), Decimal('250'), Decimal('251')]


# ---------------------------------------------------------------------------
# promop's own ingest: FHIR sync and lab-results sync
# ---------------------------------------------------------------------------

def _sodium_obs():
    return {
        'resourceType': 'Observation', 'status': 'final',
        'code': {'coding': [{'system': 'http://loinc.org', 'code': '2951-2',
                             'display': 'Sodium'}], 'text': 'Sodium'},
        'effectiveDateTime': '2026-10-01T08:00:00Z',
        'valueQuantity': {'value': 140, 'unit': 'mmol/L', 'code': 'mmol/L'},
    }


@pytest.mark.parametrize('method', ['_insert_discrete_observations',
                                    '_upsert_rollup_observations'])
def test_fhir_resync_after_repoint(db, method):
    from omop_core.services.source_vocabularies import fhir_source_vocabulary
    from patient_portal.api.fhir.sync import FhirSyncView
    person = PersonFactory()
    PatientRecordFactory(person=person)
    ehr = ConceptFactory(concept_code='EHR-FHIR', concept_name='EHR')
    imported = ConceptFactory(concept_code='2951-2', concept_name='Sodium')
    view = FhirSyncView()
    key = (fhir_source_vocabulary('http://loinc.org'), '2951-2')

    def sync(concept):
        # The full sync preloads this cache from the approved mappings.
        getattr(view, method)(person, [_sodium_obs()], ehr, {key: concept},
                              'resync-test', None, {})

    sync(imported)
    mapping = _mapping('Sodium', 'mol/L')
    result = repoint_clinical_rows(mapping=mapping, old_concept_id=imported.concept_id,
                                   new_concept_id=mapping.target_concept_id)
    assert result['rows_updated'] == 1

    sync(mapping.target_concept)

    assert _stored(person) == [(Decimal('140'), None)]
    assert Measurement.objects.get(person=person).measurement_concept_id == mapping.target_concept_id


def test_lab_results_resync_after_resolve(db, staff_client):
    # A lab name with no LOINC code lands on concept 0 under a minted source
    # concept, which is what resolve picks up once a curator maps the name.
    from patient_portal.api.lab_results.tests import _setup_vocab
    _setup_vocab()
    person = PersonFactory()
    PatientRecordFactory(person=person)
    body = {
        'person_id': person.person_id,
        'measurements': [{'test_name': 'Hemoglobin', 'value': '13.5', 'unit': 'g/dL',
                          'measured_at': '2026-05-15'}],
        'lab_name': 'Quest Diagnostics', 'lab_date': '2026-05-15',
        'report_filename': 'bloodwork.pdf', 'source_type': 'document_extraction',
    }
    assert staff_client.post('/api/lab-results/sync/', body, format='json').status_code == 201
    assert Measurement.objects.get(person=person).measurement_concept_id == 0
    mapping = _mapping('Hemoglobin', 'g/L')

    resolve_person_source_codes(person)
    assert Measurement.objects.get(person=person).measurement_concept_id == mapping.target_concept_id
    second = staff_client.post('/api/lab-results/sync/', body, format='json')

    assert second.status_code == 201, second.content
    assert [v for v, _ in _stored(person)] == [Decimal('13.5')]


# ---------------------------------------------------------------------------
# Curation survives a unit that would not have fit
# ---------------------------------------------------------------------------

def test_repoint_keeps_the_concept_change(db):
    # 250 10*3/uL is 2.5e11 /L, which does not fit Decimal(15, 5). Converting
    # it used to abort the transaction and silently undo the repoint.
    person = PersonFactory()
    old = ConceptFactory(concept_code='OLD-PLT', concept_name='Old')
    row = MeasurementFactory(person=person, measurement_concept=old,
                             measurement_source_value='PLT',
                             value_as_number=Decimal('250'), unit_source_value='10*3/uL')
    mapping = _mapping('PLT', '/L')

    result = repoint_clinical_rows(mapping=mapping, old_concept_id=old.concept_id,
                                   new_concept_id=mapping.target_concept_id)

    assert result['rows_updated'] == 1
    row.refresh_from_db()
    assert row.measurement_concept_id == mapping.target_concept_id
    assert row.value_as_number == Decimal('250')


def test_resolve_keeps_the_concept_change(db):
    person = PersonFactory()
    row = MeasurementFactory(person=person, measurement_concept=_no_match(),
                             measurement_source_value='PLT',
                             value_as_number=Decimal('250'), unit_source_value='10*3/uL')
    mapping = _mapping('PLT', '/L')

    resolve_person_source_codes(person)

    row.refresh_from_db()
    assert row.measurement_concept_id == mapping.target_concept_id
    assert row.value_as_number == Decimal('250')
