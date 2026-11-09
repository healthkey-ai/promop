"""relocate_clinical_rows: a code's rows follow its mapping to another table (#2386)."""
from datetime import date
from decimal import Decimal

import pytest
from django.contrib.contenttypes.models import ContentType

from omop_core.mapping.table_relocation import relocatable, relocate_clinical_rows
from omop_core.models import (
    Measurement, MeasurementOwnership, Observation, Organization, PatientRecord,
    PatientSourceCode, ProvenanceRecord, SourceCodeConceptMapping,
)
from tests.factories import ConceptFactory, MeasurementFactory, PersonFactory

pytestmark = pytest.mark.django_db


def _mapping(**overrides):
    return SourceCodeConceptMapping.objects.create(**{
        'source_vocabulary_id': 'EPIC', 'source_code': 'GLU', 'source_code_description': 'Glucose',
        'domain_id': 'Observation', 'omop_table': 'observation', 'status': 'approved',
        'target_concept': ConceptFactory(domain_id='Measurement'), **overrides,
    })


def test_relocatable_needs_two_different_clinical_tables():
    assert relocatable('observation', 'measurement')
    assert relocatable('condition_occurrence', 'procedure')
    assert not relocatable('measurement', 'measurement')
    assert not relocatable('', 'measurement')
    assert not relocatable('measurement', 'note')


def test_measurements_become_observations_with_their_facts_provenance_and_inventory():
    mapping = _mapping(domain_id='Observation', omop_table='observation')
    person = PersonFactory()
    PatientRecord.objects.create(person=person, derivation_version=3)
    unit = ConceptFactory(concept_name='mg/dL')
    row = MeasurementFactory(
        person=person, measurement_source_value='GLU', measurement_date='2026-01-02',
        value_as_number=Decimal('5.5'), unit_concept=unit, unit_source_value='mg/dL',
        value_source_value='5.5', range_low=Decimal('3'), range_high=Decimal('6'),
    )
    unrelated = MeasurementFactory(person=person, measurement_source_value='HGB')
    provenance = ProvenanceRecord.objects.create(
        source='etl', content_type=ContentType.objects.get_for_model(Measurement),
        object_id=row.pk, source_user_id='etl',
    )
    MeasurementOwnership.objects.create(measurement_id=row.pk, visit_occurrence_id=1)
    PatientSourceCode.objects.create(person=person, source_value='GLU', source_vocabulary_id='EPIC',
                                     omop_table='measurement', occurrence_count=1)
    # The patient already has the code under the destination table too:
    # the inventory rows merge rather than collide on the unique key.
    PatientSourceCode.objects.create(person=person, source_value='GLU', source_vocabulary_id='EPIC',
                                     omop_table='observation', occurrence_count=2)

    outcome = relocate_clinical_rows(mapping=mapping, from_table='measurement', to_table='observation')

    assert (outcome['rows_relocated'], outcome['persons_marked_stale'], outcome['source_codes_refiled']) == (1, 1, 1)
    assert not Measurement.objects.filter(pk=row.pk).exists()
    assert Measurement.objects.filter(pk=unrelated.pk).exists()
    moved = Observation.objects.get(person=person, observation_source_value='GLU')
    assert moved.observation_concept_id == mapping.target_concept_id
    assert (str(moved.observation_date), moved.value_as_number, moved.unit_concept_id,
            moved.unit_source_value, moved.value_source_value) == ('2026-01-02', Decimal('5.5'), unit.pk, 'mg/dL', '5.5')
    assert moved.observation_type_concept_id == row.measurement_type_concept_id
    provenance.refresh_from_db()
    assert (provenance.content_type_id, provenance.object_id) == (ContentType.objects.get_for_model(Observation).pk, moved.pk)
    assert not MeasurementOwnership.objects.filter(measurement_id=row.pk).exists()
    inventory = PatientSourceCode.objects.get(person=person, source_value='GLU')
    assert (inventory.omop_table, inventory.occurrence_count) == ('observation', 3)
    assert PatientRecord.objects.get(person=person).derivation_version == 0


def test_a_proposed_mapping_keeps_the_rows_concept_and_records_it_for_the_sweep():
    old = ConceptFactory(concept_name='Old glucose')
    mapping = _mapping(status='proposed')
    row = MeasurementFactory(measurement_source_value='GLU', measurement_concept=old)
    relocate_clinical_rows(mapping=mapping, from_table='measurement', to_table='observation')
    assert Observation.objects.get(person=row.person).observation_concept_id == old.pk
    mapping.refresh_from_db()
    assert old.pk in mapping.deferred_repoint_concept_ids and 0 in mapping.deferred_repoint_concept_ids


def test_person_scope_and_dry_run():
    mapping = _mapping()
    mine = MeasurementFactory(measurement_source_value='GLU')
    theirs = MeasurementFactory(measurement_source_value='GLU')
    preview = relocate_clinical_rows(mapping=mapping, from_table='measurement', to_table='observation',
                                     apply_changes=False)
    assert preview['rows_relocated'] == 2 and Measurement.objects.filter(pk=mine.pk).exists()
    outcome = relocate_clinical_rows(mapping=mapping, from_table='measurement', to_table='observation',
                                     person_id=mine.person_id)
    assert outcome['rows_relocated'] == 1
    assert not Measurement.objects.filter(pk=mine.pk).exists()
    assert Measurement.objects.filter(pk=theirs.pk).exists()


def test_an_organization_scoped_mapping_moves_only_its_hospitals_rows():
    hospital = Organization.objects.create(name='Hospital A', slug='hospital-a')
    mapping = _mapping(organization=hospital)
    attributed = MeasurementFactory(measurement_source_value='GLU')
    ProvenanceRecord.objects.create(
        source='etl', content_type=ContentType.objects.get_for_model(Measurement),
        object_id=attributed.pk, organization=hospital, source_user_id='etl',
    )
    unattributed = MeasurementFactory(measurement_source_value='GLU')
    outcome = relocate_clinical_rows(mapping=mapping, from_table='measurement', to_table='observation')
    assert outcome['rows_relocated'] == 1
    assert not Measurement.objects.filter(pk=attributed.pk).exists()
    assert Measurement.objects.filter(pk=unattributed.pk).exists()


def test_the_same_table_or_a_non_clinical_table_moves_nothing():
    mapping = _mapping()
    MeasurementFactory(measurement_source_value='GLU')
    assert relocate_clinical_rows(mapping=mapping, from_table='measurement', to_table='measurement')['rows_relocated'] == 0
    assert relocate_clinical_rows(mapping=mapping, from_table='measurement', to_table='')['rows_relocated'] == 0
    assert Measurement.objects.filter(measurement_source_value='GLU').count() == 1
