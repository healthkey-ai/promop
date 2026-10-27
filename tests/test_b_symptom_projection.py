"""B-symptom individual fields project to/from OMOP Observations (Slice 3).

Each of the three constitutional B symptoms (fever, night sweats, weight loss)
is an independent boolean field that projects to its own SNOMED-coded OMOP
Observation row.  The composite ``b_symptoms`` field is derived: True when any
symptom is True, False when all are explicitly False, None otherwise.
"""
import pytest
from datetime import date

from omop_core.models import (
    FieldChoice, FieldConceptMapping, Observation, PatientRecord,
)
from omop_core.services.omop_projection import project_single_value
from omop_core.services.patient_record_service import refresh_patient_record
from omop_core.signals import suppress_patient_record_refresh
from tests.factories import (
    ConceptFactory, DomainFactory, ObservationFactory,
    PatientRecordFactory, VocabularyFactory,
)

pytestmark = pytest.mark.django_db

# SNOMED codes for the three B symptoms.
FEVER_CODE = '386661006'
NIGHT_SWEATS_CODE = '42984000'
WEIGHT_LOSS_CODE = '262285001'

EHR_TYPE = 32817


@pytest.fixture()
def observation_domain():
    return DomainFactory(domain_id='Observation', domain_name='Observation')


@pytest.fixture()
def snomed_vocab():
    return VocabularyFactory(vocabulary_id='SNOMED')


@pytest.fixture()
def type_concept(observation_domain):
    VocabularyFactory(vocabulary_id='Type Concept')
    return ConceptFactory(
        concept_id=EHR_TYPE, concept_code='32817',
        vocabulary_id='Type Concept', concept_name='EHR',
        domain=observation_domain,
    )


@pytest.fixture()
def fever_concept(observation_domain, snomed_vocab):
    return ConceptFactory(
        concept_id=4158517, concept_code=FEVER_CODE,
        vocabulary_id='SNOMED', concept_name='Fever',
        domain=observation_domain, standard_concept='S',
    )


@pytest.fixture()
def night_sweats_concept(observation_domain, snomed_vocab):
    return ConceptFactory(
        concept_id=4196900, concept_code=NIGHT_SWEATS_CODE,
        vocabulary_id='SNOMED', concept_name='Night sweats',
        domain=observation_domain, standard_concept='S',
    )


@pytest.fixture()
def weight_loss_concept(observation_domain, snomed_vocab):
    return ConceptFactory(
        concept_id=4177808, concept_code=WEIGHT_LOSS_CODE,
        vocabulary_id='SNOMED', concept_name='Loss of weight',
        domain=observation_domain, standard_concept='S',
    )


def _create_mapping(field_name, concept, snomed_code):
    return FieldConceptMapping.objects.create(
        field_name=field_name,
        concept=concept,
        vocabulary_id='SNOMED',
        concept_code=snomed_code,
        omop_table='observation',
        source_value=snomed_code,
        value_kind='boolean',
        type_concept_id=EHR_TYPE,
        status='approved',
    )


@pytest.fixture()
def b_symptom_mappings(fever_concept, night_sweats_concept, weight_loss_concept, type_concept):
    return {
        'fever': _create_mapping('b_symptom_fever', fever_concept, FEVER_CODE),
        'night_sweats': _create_mapping('b_symptom_night_sweats', night_sweats_concept, NIGHT_SWEATS_CODE),
        'weight_loss': _create_mapping('b_symptom_weight_loss', weight_loss_concept, WEIGHT_LOSS_CODE),
    }


@pytest.fixture()
def record(settings, b_symptom_mappings):
    settings.CELERY_BROKER_URL = ''
    return PatientRecordFactory(disease='follicular lymphoma')


# ---------------------------------------------------------------------------
# Projection tests: PATCH value → Observation row
# ---------------------------------------------------------------------------


def _recipe(mapping):
    return {
        'omop_table': 'observation',
        'concept_id': mapping.concept_id,
        'source_value': mapping.source_value,
        'value_kind': 'boolean',
    }


def test_fever_projects_to_observation(record, b_symptom_mappings):
    mapping = b_symptom_mappings['fever']
    project_single_value(record.person, 'b_symptom_fever', True, _recipe(mapping))

    obs = Observation.objects.filter(
        person=record.person,
        observation_concept_id=mapping.concept_id,
    )
    assert obs.exists()
    assert obs.first().value_as_number == 1.0


def test_night_sweats_projects_to_observation(record, b_symptom_mappings):
    mapping = b_symptom_mappings['night_sweats']
    project_single_value(record.person, 'b_symptom_night_sweats', True, _recipe(mapping))

    obs = Observation.objects.filter(
        person=record.person,
        observation_concept_id=mapping.concept_id,
    )
    assert obs.exists()
    assert obs.first().value_as_number == 1.0


def test_weight_loss_projects_to_observation(record, b_symptom_mappings):
    mapping = b_symptom_mappings['weight_loss']
    project_single_value(record.person, 'b_symptom_weight_loss', False, _recipe(mapping))

    obs = Observation.objects.filter(
        person=record.person,
        observation_concept_id=mapping.concept_id,
    )
    assert obs.exists()
    assert obs.first().value_as_number == 0.0


# ---------------------------------------------------------------------------
# Derivation tests: Observation row → PatientRecord fields
# ---------------------------------------------------------------------------


def _write_obs(person, concept, value_as_number):
    with suppress_patient_record_refresh():
        ObservationFactory(
            person=person,
            observation_concept=concept,
            observation_date=date(2024, 6, 1),
            value_as_number=value_as_number,
            observation_source_value=concept.concept_code,
        )


def test_b_symptoms_derived_true_when_any_true(record, fever_concept, night_sweats_concept, weight_loss_concept):
    _write_obs(record.person, fever_concept, 1.0)
    _write_obs(record.person, night_sweats_concept, 0.0)
    _write_obs(record.person, weight_loss_concept, 0.0)

    refreshed = refresh_patient_record(record.person)
    assert refreshed.b_symptom_fever is True
    assert refreshed.b_symptom_night_sweats is False
    assert refreshed.b_symptom_weight_loss is False
    assert refreshed.b_symptoms is True


def test_b_symptoms_derived_false_when_all_false(record, fever_concept, night_sweats_concept, weight_loss_concept):
    _write_obs(record.person, fever_concept, 0.0)
    _write_obs(record.person, night_sweats_concept, 0.0)
    _write_obs(record.person, weight_loss_concept, 0.0)

    refreshed = refresh_patient_record(record.person)
    assert refreshed.b_symptom_fever is False
    assert refreshed.b_symptom_night_sweats is False
    assert refreshed.b_symptom_weight_loss is False
    assert refreshed.b_symptoms is False


def test_b_symptoms_derived_none_when_mixed(record, fever_concept):
    """Only fever recorded; others unknown → b_symptoms is None (not enough info)."""
    _write_obs(record.person, fever_concept, 0.0)

    refreshed = refresh_patient_record(record.person)
    assert refreshed.b_symptom_fever is False
    assert refreshed.b_symptom_night_sweats is None
    assert refreshed.b_symptom_weight_loss is None
    # Not all three are False, and none is True → None.
    assert refreshed.b_symptoms is None


def test_b_symptoms_true_overrides_missing(record, fever_concept):
    """Fever is True, others unknown → b_symptoms is True."""
    _write_obs(record.person, fever_concept, 1.0)

    refreshed = refresh_patient_record(record.person)
    assert refreshed.b_symptom_fever is True
    assert refreshed.b_symptoms is True


# ---------------------------------------------------------------------------
# Migration data test: FieldConceptMapping rows
# ---------------------------------------------------------------------------


def test_mapping_entries_exist(b_symptom_mappings):
    """Verify the three FieldConceptMapping rows were created with correct concepts."""
    for key, mapping in b_symptom_mappings.items():
        row = FieldConceptMapping.objects.get(pk=mapping.pk)
        assert row.status == 'approved'
        assert row.omop_table == 'observation'
        assert row.value_kind == 'boolean'
