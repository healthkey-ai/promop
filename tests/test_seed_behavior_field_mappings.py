"""Seeded BehaviorTab mappings write facts derivation already reads."""

from datetime import date
import importlib

import pytest
from django.apps import apps as global_apps

from omop_core.models import FieldConceptMapping, Measurement, Observation
from omop_core.services.patient_record_service import refresh_patient_record
from omop_core.services.pk import next_pk
from omop_core.services.write_descriptor import build_writable_field_descriptor
from tests.factories import ConceptFactory, PatientRecordFactory, PersonFactory

pytestmark = pytest.mark.django_db


def _run_seed():
    migration = importlib.import_module(
        'omop_core.migrations.0160_seed_behavior_field_mappings',
    )
    migration.seed(global_apps, None)


def _type_concepts():
    ConceptFactory(
        concept_id=32856,
        vocabulary_id='Type Concept',
        concept_code='OMOP4976929',
        concept_name='Lab',
    )
    ConceptFactory(
        concept_id=32817,
        vocabulary_id='Type Concept',
        concept_code='OMOP4976890',
        concept_name='EHR',
    )


def test_behavior_seed_makes_simple_registered_fields_writable():
    _type_concepts()
    pack_years = ConceptFactory(
        vocabulary_id='LOINC',
        concept_code='63640-7',
        concept_name='Pack years',
    )
    contraception = ConceptFactory(
        vocabulary_id='LOINC',
        concept_code='8659-8',
        concept_name='Contraceptive use',
    )

    _run_seed()

    descriptor = build_writable_field_descriptor()
    pack_entry = descriptor['pack_years']
    assert pack_entry['writable'] is True
    assert pack_entry['target'] == 'measurement'
    assert pack_entry['concept_id'] == pack_years.concept_id
    assert pack_entry['source_value'] == '63640-7'
    assert pack_entry['value_kind'] == 'number'
    assert pack_entry['type_concept_id'] == 32856

    contraception_entry = descriptor['contraceptive_use']
    assert contraception_entry['writable'] is True
    assert contraception_entry['target'] == 'observation'
    assert contraception_entry['concept_id'] == contraception.concept_id
    assert contraception_entry['source_value'] == '8659-8'
    assert contraception_entry['value_kind'] == 'boolean'
    assert contraception_entry['type_concept_id'] == 32817


def test_behavior_seeded_numeric_mapping_round_trips():
    _type_concepts()
    ConceptFactory(
        vocabulary_id='LOINC',
        concept_code='63640-7',
        concept_name='Pack years',
    )
    _run_seed()
    entry = build_writable_field_descriptor()['pack_years']
    person = PersonFactory()
    PatientRecordFactory(person=person)

    Measurement.objects.create(
        measurement_id=next_pk(Measurement, 'measurement_id'),
        person=person,
        measurement_concept_id=entry['concept_id'],
        measurement_date=date(2026, 2, 1),
        measurement_type_concept_id=entry['type_concept_id'],
        measurement_source_value=entry['source_value'],
        value_as_number=12.5,
    )

    record = refresh_patient_record(person)

    assert record.pack_years == 12.5


def test_behavior_seeded_boolean_mapping_round_trips():
    _type_concepts()
    ConceptFactory(
        vocabulary_id='LOINC',
        concept_code='8659-8',
        concept_name='Contraceptive use',
    )
    _run_seed()
    entry = build_writable_field_descriptor()['contraceptive_use']
    person = PersonFactory()
    PatientRecordFactory(person=person)

    Observation.objects.create(
        observation_id=next_pk(Observation, 'observation_id'),
        person=person,
        observation_concept_id=entry['concept_id'],
        observation_date=date(2026, 2, 1),
        observation_type_concept_id=entry['type_concept_id'],
        observation_source_value=entry['source_value'],
        value_as_string='true',
    )

    record = refresh_patient_record(person)

    assert record.contraceptive_use is True


def test_behavior_seed_does_not_make_companion_fields_independently_writable():
    ConceptFactory(
        vocabulary_id='LOINC',
        concept_code='74204-0',
        concept_name='Non-prescription drug use',
    )
    ConceptFactory(
        vocabulary_id='LOINC',
        concept_code='82593-5',
        concept_name='Environmental exposure risk',
    )

    _run_seed()

    mapped = set(FieldConceptMapping.objects.values_list('field_name', flat=True))
    assert 'substance_use_details' not in mapped
    assert 'geographic_exposure_risk_details' not in mapped
    assert 'pregnancy_test_date' not in mapped
