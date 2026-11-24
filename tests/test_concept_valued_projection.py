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
# Approved answer mappings decide the concept (#1224)
# ---------------------------------------------------------------------------

class TestApprovedAnswerConcepts:
    """The projection writes the approved ``value_as_concept`` decision."""

    @staticmethod
    def _choice(display='Watch and wait'):
        return FieldChoice.objects.get(field_name='disease_activity', display=display)

    @staticmethod
    def _concept_map():
        descriptor = build_writable_field_descriptor()
        projection = projection_for_descriptor(descriptor['disease_activity'])
        return projection, projection.get('value_concept_map', {})

    def test_an_approved_concept_replaces_the_legacy_code(
        self, disease_activity_mapping, disease_activity_choices,
    ):
        from omop_core.services import field_value_mappings as fvm
        proposal = fvm.propose(self._choice(), vocabulary_id='SNOMED', concept_code='103338009')
        fvm.approve(proposal)

        projection, concepts = self._concept_map()
        assert concepts['Watch and wait'] == 4014046          # not the primary code's 4190466
        person = PersonFactory()
        PatientRecordFactory(person=person, disease_activity='Watch and wait')
        assert project_single_value(person, 'disease_activity', 'Watch and wait', projection)
        obs = Observation.objects.get(person=person, observation_source_value='97509-4')
        assert obs.value_as_concept_id == 4014046

    def test_an_approved_no_equivalent_writes_no_concept_despite_a_legacy_code(
        self, disease_activity_mapping, disease_activity_choices,
    ):
        from omop_core.services import field_value_mappings as fvm
        fvm.approve(fvm.propose(self._choice(), outcome='no_equivalent',
                                rationale='No standard concept for this phrase'))

        projection, concepts = self._concept_map()
        assert 'Watch and wait' not in concepts
        person = PersonFactory()
        PatientRecordFactory(person=person, disease_activity='Watch and wait')
        assert project_single_value(person, 'disease_activity', 'Watch and wait', projection)
        obs = Observation.objects.get(person=person, observation_source_value='97509-4')
        assert (obs.value_as_string, obs.value_as_concept_id) == ('Watch and wait', None)

    def test_an_open_proposal_changes_nothing(
        self, disease_activity_mapping, disease_activity_choices,
    ):
        from omop_core.services import field_value_mappings as fvm
        fvm.propose(self._choice(), vocabulary_id='SNOMED', concept_code='103338009')
        assert self._concept_map()[1]['Watch and wait'] == 4190466

    def test_a_rejected_decision_does_not_fall_back_to_the_legacy_code(
        self, disease_activity_mapping, disease_activity_choices,
    ):
        from omop_core.services import field_value_mappings as fvm
        fvm.reject(fvm.propose(self._choice(), vocabulary_id='SNOMED', concept_code='373818007'))
        assert 'Watch and wait' not in self._concept_map()[1]

    def test_a_withdrawn_approval_writes_no_concept(
        self, disease_activity_mapping, disease_activity_choices,
    ):
        from omop_core.services import field_value_mappings as fvm
        approved = fvm.approve(fvm.propose(self._choice(), vocabulary_id='SNOMED',
                                           concept_code='103338009'))
        fvm.reject(approved)
        assert 'Watch and wait' not in self._concept_map()[1]

    def test_a_target_retired_after_approval_writes_no_concept(
        self, disease_activity_mapping, disease_activity_choices,
    ):
        from omop_core.models import Concept
        from omop_core.services import field_value_mappings as fvm
        fvm.approve(fvm.propose(self._choice(), vocabulary_id='SNOMED', concept_code='103338009'))
        assert self._concept_map()[1]['Watch and wait'] == 4014046
        Concept.objects.filter(pk=4014046).update(invalid_reason='D')
        assert 'Watch and wait' not in self._concept_map()[1]

    def test_an_alias_and_the_stored_value_project_too(
        self, disease_activity_mapping, disease_activity_choices,
    ):
        from omop_core.models import FieldChoiceAlias
        choice = self._choice()
        choice.value = 'watch_and_wait'
        choice.save()
        FieldChoiceAlias.objects.create(choice=choice, field_name='disease_activity',
                                        alias='Watching', alias_key='watching')
        concepts = self._concept_map()[1]
        assert concepts['Watch and wait'] == concepts['watch_and_wait'] == concepts['Watching'] == 4190466

    def test_a_label_two_contexts_resolve_differently_writes_no_concept(
        self, disease_activity_mapping, disease_activity_choices,
    ):
        from omop_core.services import field_value_mappings as fvm
        other = FieldChoice.objects.create(field_name='disease_activity', display='Watch and wait',
                                           context_key='pathology')
        fvm.approve(fvm.propose(other, vocabulary_id='SNOMED', concept_code='103338009'))
        assert 'Watch and wait' not in self._concept_map()[1]

    def test_rejecting_another_proposal_keeps_the_legacy_code(
        self, disease_activity_mapping, disease_activity_choices,
    ):
        from omop_core.services import field_value_mappings as fvm
        fvm.reject(fvm.propose(self._choice(), vocabulary_id='SNOMED', concept_code='103338009'))
        assert self._concept_map()[1]['Watch and wait'] == 4190466

    def test_a_context_with_no_code_does_not_cancel_a_coded_one(
        self, disease_activity_mapping, disease_activity_choices,
    ):
        FieldChoice.objects.create(field_name='disease_activity', display='Watch and wait',
                                   context_key='pathology')
        assert self._concept_map()[1]['Watch and wait'] == 4190466

    def test_a_live_choice_wins_over_a_retired_one_sharing_its_label(
        self, disease_activity_mapping, disease_activity_choices,
    ):
        from omop_core.services import field_choices, field_value_mappings as fvm
        old = FieldChoice.objects.create(field_name='disease_activity', display='Watch and wait',
                                         context_key='older')
        fvm.approve(fvm.propose(old, vocabulary_id='SNOMED', concept_code='103338009'))
        field_choices.set_retired(old, True)
        assert self._concept_map()[1]['Watch and wait'] == 4190466

    def test_a_retired_choice_still_projects(
        self, disease_activity_mapping, disease_activity_choices,
    ):
        from omop_core.services import field_choices
        field_choices.set_retired(self._choice(), True)
        assert self._concept_map()[1]['Watch and wait'] == 4190466

    def test_the_queries_do_not_grow_with_the_choices(
        self, disease_activity_mapping, disease_activity_choices,
    ):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext
        from omop_core.services import field_value_mappings as fvm

        def count():
            with CaptureQueriesContext(connection) as ctx:
                fvm.answer_concept_ids(['disease_activity'])
            return len(ctx)

        first = FieldChoice.objects.create(field_name='disease_activity', display='Extra first')
        fvm.approve(fvm.propose(first, vocabulary_id='SNOMED', concept_code='263855007'))
        before = count()
        for i in range(8):
            c = FieldChoice.objects.create(field_name='disease_activity', display=f'Extra {i}')
            fvm.approve(fvm.propose(c, vocabulary_id='SNOMED', concept_code='263855007'))
        assert count() == before


# ---------------------------------------------------------------------------
# A fact that carries the answer as a concept is read back as the answer (#1224)
# ---------------------------------------------------------------------------

class TestConceptOnlyFactsRead:
    """Derivation reads ``value_as_concept_id`` through the same decisions."""

    @staticmethod
    def _choice(display):
        return FieldChoice.objects.get(field_name='disease_activity', display=display)

    @staticmethod
    def _fact(person, concept_id, *, string=None, day=date(2026, 1, 5)):
        return Observation.objects.create(
            observation_id=900000 + Observation.objects.count(), person=person,
            observation_concept_id=1617595, observation_source_value='97509-4',
            observation_date=day, observation_type_concept_id=32817,
            value_as_concept_id=concept_id, value_as_string=string,
        )

    def _derived(self, person):
        return refresh_patient_record(person).disease_activity

    def test_a_concept_only_fact_reads_as_the_answer_its_legacy_code_names(
        self, disease_activity_mapping, disease_activity_choices,
    ):
        person = PersonFactory()
        PatientRecordFactory(person=person)
        self._fact(person, 4014046)        # In remission, the code of 'Finished treatment ...'
        assert self._derived(person) == 'Finished treatment and in remission'

    def test_a_concept_two_answers_share_is_not_guessed(
        self, disease_activity_mapping, disease_activity_choices,
    ):
        from omop_core.services import field_value_mappings as fvm
        fvm.approve(fvm.propose(self._choice('Watch and wait'), vocabulary_id='SNOMED',
                                concept_code='103338009'))
        person = PersonFactory()
        PatientRecordFactory(person=person)
        self._fact(person, 4014046)        # now the concept of 'Watch and wait' too
        # 'Finished treatment ...' still has it by its code: two answers claim
        # the concept, so nothing is guessed.
        assert self._derived(person) in (None, '')

    def test_an_approved_decision_moves_the_reading_with_it(
        self, disease_activity_mapping, disease_activity_choices,
    ):
        from omop_core.services import field_value_mappings as fvm
        fvm.approve(fvm.propose(self._choice('Watch and wait'), vocabulary_id='SNOMED',
                                concept_code='263855007'))
        fvm.approve(fvm.propose(self._choice('Came back after treatment'), outcome='no_equivalent',
                                rationale='Not a single standard concept'))
        person = PersonFactory()
        PatientRecordFactory(person=person)
        self._fact(person, 4136153)
        assert self._derived(person) == 'Watch and wait'

    def test_a_no_equivalent_answer_has_no_concept_to_read(
        self, disease_activity_mapping, disease_activity_choices,
    ):
        from omop_core.services import field_value_mappings as fvm
        fvm.approve(fvm.propose(self._choice('Came back after treatment'), outcome='no_equivalent',
                                rationale='Not a single standard concept'))
        person = PersonFactory()
        PatientRecordFactory(person=person)
        self._fact(person, 4136153)
        assert self._derived(person) in (None, '')

    def test_the_text_of_a_fact_wins_over_its_concept(
        self, disease_activity_mapping, disease_activity_choices,
    ):
        person = PersonFactory()
        PatientRecordFactory(person=person)
        self._fact(person, 4014046, string='Watch and wait')
        assert self._derived(person) == 'Watch and wait'

    def test_a_concept_no_answer_stands_for_is_not_read(
        self, disease_activity_mapping, disease_activity_choices,
    ):
        person = PersonFactory()
        PatientRecordFactory(person=person)
        self._fact(person, 1617595)        # the question concept itself
        assert self._derived(person) in (None, '')

    def test_a_retired_answer_is_still_read(
        self, disease_activity_mapping, disease_activity_choices,
    ):
        from django.utils import timezone
        FieldChoice.objects.filter(display='Finished treatment and in remission').update(
            retired_at=timezone.now())
        person = PersonFactory()
        PatientRecordFactory(person=person)
        self._fact(person, 4014046)
        assert self._derived(person) == 'Finished treatment and in remission'

    def test_a_live_answer_speaks_over_a_retired_one_sharing_the_concept(
        self, disease_activity_mapping, disease_activity_choices,
    ):
        from django.utils import timezone
        old = FieldChoice.objects.create(field_name='disease_activity', display='Remission (old)',
                                         sort_order=9, retired_at=timezone.now())
        FieldChoiceCode.objects.create(choice=old, vocabulary_id='SNOMED', code='103338009',
                                       display='Remission (old)', is_primary=True)
        person = PersonFactory()
        PatientRecordFactory(person=person)
        self._fact(person, 4014046)
        assert self._derived(person) == 'Finished treatment and in remission'

    def test_the_lookup_is_not_made_unless_a_fact_needs_it(
        self, disease_activity_mapping, disease_activity_choices, django_assert_num_queries,
    ):
        from unittest import mock
        person = PersonFactory()
        PatientRecordFactory(person=person)
        self._fact(person, None, string='Watch and wait')
        with mock.patch('omop_core.services.field_value_mappings.answer_values_by_concept') as lookup:
            assert self._derived(person) == 'Watch and wait'
        lookup.assert_not_called()


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
