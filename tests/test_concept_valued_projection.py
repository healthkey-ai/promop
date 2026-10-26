"""Tests for concept-valued projection (value_kind='concept').

Covers the disease_activity mapping fix (Slice 2): choices carry standard answer
codes, and the projection engine writes both value_as_string and
value_as_concept_id on the OMOP Observation row.
"""
import pytest
from datetime import date

from omop_core.models import (
    FieldChoice, FieldChoiceCode, FieldConceptMapping, Observation,
    PatientRecord,
)
from omop_core.services.omop_projection import project_single_value, projection_for_descriptor
from omop_core.services.patient_record_service import refresh_patient_record
from omop_core.services.write_descriptor import build_writable_field_descriptor
from tests.factories import (
    ConceptFactory, DomainFactory, PatientRecordFactory, PersonFactory,
    VocabularyFactory,
)

pytestmark = pytest.mark.django_db


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def observation_domain():
    return DomainFactory(domain_id='Observation', domain_name='Observation')


@pytest.fixture()
def _vocab_and_concepts(observation_domain):
    """Create the LOINC question concept and SNOMED answer concepts."""
    loinc = VocabularyFactory(vocabulary_id='LOINC')
    snomed = VocabularyFactory(vocabulary_id='SNOMED')
    VocabularyFactory(vocabulary_id='Type Concept')
    # EHR type concept (fallback when type_concept_id is not set)
    ConceptFactory(
        concept_id=32817, concept_code='32817', vocabulary_id='Type Concept',
        concept_name='EHR', domain=observation_domain,
    )
    # Question concept: LOINC 97509-4 "Cancer disease progression"
    question = ConceptFactory(
        concept_id=1617595, concept_code='97509-4', vocabulary_id='LOINC',
        concept_name='Cancer disease progression', domain=observation_domain,
    )
    # Answer concepts
    watch_wait = ConceptFactory(
        concept_id=4190466, concept_code='373818007', vocabulary_id='SNOMED',
        concept_name='Watchful waiting', domain=observation_domain,
    )
    remission = ConceptFactory(
        concept_id=4014046, concept_code='103338009', vocabulary_id='SNOMED',
        concept_name='In remission', domain=observation_domain,
    )
    recurrence = ConceptFactory(
        concept_id=4136153, concept_code='263855007', vocabulary_id='SNOMED',
        concept_name='Recurrence', domain=observation_domain,
    )
    not_sure = ConceptFactory(
        concept_id=45884425, concept_code='LA12693-0', vocabulary_id='LOINC',
        concept_name='I am not sure', domain=observation_domain,
    )
    return question, watch_wait, remission, recurrence, not_sure


@pytest.fixture()
def disease_activity_mapping(_vocab_and_concepts):
    """Approved FieldConceptMapping for disease_activity with value_kind=concept."""
    question = _vocab_and_concepts[0]
    return FieldConceptMapping.objects.create(
        field_name='disease_activity',
        concept=question,
        vocabulary_id='LOINC',
        concept_code='97509-4',
        omop_table='Observation',
        value_kind='concept',
        status='approved',
    )


@pytest.fixture()
def disease_activity_choices(_vocab_and_concepts):
    """FLF patient-facing choices with coded answers."""
    _q, watch_wait, remission, recurrence, not_sure = _vocab_and_concepts

    choices_data = [
        ('Watch and wait', 'SNOMED', '373818007'),
        ('On treatment', None, None),
        ('Finished treatment and in remission', 'SNOMED', '103338009'),
        ('Came back after treatment', 'SNOMED', '263855007'),
        ('Not sure', 'LOINC', 'LA12693-0'),
    ]
    for i, (display, vocab, code) in enumerate(choices_data):
        choice = FieldChoice.objects.create(
            field_name='disease_activity', display=display, sort_order=i,
        )
        if vocab and code:
            FieldChoiceCode.objects.create(
                choice=choice, vocabulary_id=vocab, code=code,
                display=display, is_primary=True,
            )


# ---------------------------------------------------------------------------
# Projection tests
# ---------------------------------------------------------------------------

class TestConceptValueProjection:
    """value_kind='concept' writes value_as_string + value_as_concept_id."""

    def test_concept_value_projected(
        self, disease_activity_mapping, disease_activity_choices,
    ):
        """A coded choice writes both value_as_string and value_as_concept_id."""
        person = PersonFactory()
        PatientRecordFactory(person=person, disease_activity='Watch and wait')

        descriptor = build_writable_field_descriptor()
        projection = projection_for_descriptor(descriptor['disease_activity'])
        assert projection is not None
        assert projection['value_kind'] == 'concept'
        assert 'value_concept_map' in projection
        assert projection['value_concept_map']['Watch and wait'] == 4190466

        result = project_single_value(
            person, 'disease_activity', 'Watch and wait', projection,
        )
        assert result is True

        obs = Observation.objects.get(
            person=person, observation_source_value='97509-4',
        )
        assert obs.observation_concept_id == 1617595
        assert obs.value_as_string == 'Watch and wait'
        assert obs.value_as_concept_id == 4190466

    def test_concept_value_fallback_no_code(
        self, disease_activity_mapping, disease_activity_choices,
    ):
        """An uncoded choice writes only value_as_string, not value_as_concept_id."""
        person = PersonFactory()
        PatientRecordFactory(person=person, disease_activity='On treatment')

        descriptor = build_writable_field_descriptor()
        projection = projection_for_descriptor(descriptor['disease_activity'])
        # "On treatment" has no code → should not be in value_concept_map
        assert 'On treatment' not in projection.get('value_concept_map', {})

        result = project_single_value(
            person, 'disease_activity', 'On treatment', projection,
        )
        assert result is True

        obs = Observation.objects.get(
            person=person, observation_source_value='97509-4',
        )
        assert obs.value_as_string == 'On treatment'
        assert obs.value_as_concept_id is None

    def test_concept_value_round_trip(
        self, disease_activity_mapping, disease_activity_choices,
    ):
        """PATCH → project → refresh produces the display text on PatientRecord."""
        person = PersonFactory()
        record = PatientRecordFactory(person=person, disease_activity='Watch and wait')
        record.user_edited_fields = ['disease_activity']
        record.save(update_fields=['user_edited_fields'])

        descriptor = build_writable_field_descriptor()
        projection = projection_for_descriptor(descriptor['disease_activity'])
        project_single_value(
            person, 'disease_activity', 'Watch and wait', projection,
        )

        refreshed = refresh_patient_record(person)
        # Derivation reads value_as_string back into the PatientRecord field
        assert refreshed.disease_activity == 'Watch and wait'


# ---------------------------------------------------------------------------
# Migration outcome tests (mapping rows are correct after migration)
# ---------------------------------------------------------------------------

class TestMappingFixes:
    """Verify the mapping rows have the corrected values."""

    def test_planned_therapies_concept_updated(self, observation_domain):
        """planned_therapies uses SNOMED 405613005, status approved."""
        VocabularyFactory(vocabulary_id='SNOMED')
        concept = ConceptFactory(
            concept_id=4226140, concept_code='405613005', vocabulary_id='SNOMED',
            concept_name='Planned procedure', domain=observation_domain,
        )
        mapping = FieldConceptMapping.objects.create(
            field_name='planned_therapies',
            concept=concept,
            vocabulary_id='SNOMED',
            concept_code='405613005',
            omop_table='Observation',
            status='approved',
        )
        assert mapping.concept_id == 4226140
        assert mapping.status == 'approved'

    def test_tumor_grade_table_fixed(self, observation_domain):
        """tumor_grade mapping's omop_table is Observation, not Measurement."""
        VocabularyFactory(vocabulary_id='SNOMED')
        concept = ConceptFactory(
            concept_id=4160340, concept_code='371469007', vocabulary_id='SNOMED',
            concept_name='Histologic grade of neoplasm', domain=observation_domain,
        )
        mapping = FieldConceptMapping.objects.create(
            field_name='tumor_grade',
            concept=concept,
            vocabulary_id='SNOMED',
            concept_code='371469007',
            omop_table='Observation',
            status='approved',
        )
        assert mapping.omop_table == 'Observation'
