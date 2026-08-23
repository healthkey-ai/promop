"""Suggestions are suggestions.

`suggest_field_concept_mappings` narrows several million concepts to a shortlist
and records a choice between them. The choice is a judgement about meaning, made
without examining a patient, so the property that makes it safe is that it cannot
act: everything lands as `proposed`, which the descriptor ignores.

These pin that property, and the shape of a suggestion that would be safe to
approve.
"""
import pytest

from omop_core.models import Concept, FieldConceptMapping
from omop_core.services.write_descriptor import build_writable_field_descriptor
from omop_core.suggested_mappings import REVIEWED_SUGGESTIONS

pytestmark = pytest.mark.django_db


class TestASuggestionCannotActOnItsOwn:
    def test_a_proposed_mapping_leaves_the_field_unwritable(self):
        """The whole safety property.

        A wrong suggestion costs a reviewer a click. A wrong *approval* would
        write clinical facts against the wrong concept, which is why nothing here
        approves anything.
        """
        concept = Concept.objects.first()
        assert concept is not None, 'expected seeded concepts'
        # Whichever field is awaiting a concept, rather than a named one — a
        # field's classification can change, and this is not a test about which.
        subject = next(
            f for f, e in sorted(build_writable_field_descriptor().items())
            if e['kind'] == 'unmapped'
        )
        FieldConceptMapping.objects.create(
            field_name=subject,
            concept=concept,
            omop_table='observation',
            source_value='whatever',
            status='proposed',
        )

        entry = build_writable_field_descriptor()[subject]

        assert entry['writable'] is False, subject
        assert entry['kind'] == 'unmapped', subject

    def test_no_seeded_suggestion_claims_a_source_value(self):
        """Derivation matches on source_value, and a guess is worse than a blank.

        A suggestion that filled it in would look complete, approve cleanly, and
        write rows nothing reads back.
        """
        for field, choice in REVIEWED_SUGGESTIONS.items():
            assert 'source_value' not in choice, field


class TestTheSuggestionsAreWellFormed:
    """A suggestion a reviewer cannot act on wastes their time instead of saving it."""

    @pytest.mark.parametrize('field', sorted(REVIEWED_SUGGESTIONS))
    def test_each_names_a_table_the_editor_can_write(self, field):
        assert REVIEWED_SUGGESTIONS[field]['omop_table'] in {'measurement', 'observation'}

    @pytest.mark.parametrize('field', sorted(REVIEWED_SUGGESTIONS))
    def test_each_carries_a_rationale(self, field):
        # The rationale is the point: it is what a reviewer checks, and several
        # of these reject the top lexical match for a reason worth stating.
        rationale = REVIEWED_SUGGESTIONS[field]['rationale']
        assert len(rationale) > 40, field

    @pytest.mark.parametrize('field', sorted(REVIEWED_SUGGESTIONS))
    def test_each_names_a_vocabulary_and_code(self, field):
        choice = REVIEWED_SUGGESTIONS[field]
        assert choice['vocabulary_id'] in {'LOINC', 'SNOMED'}
        assert choice['concept_code']

    def test_none_is_suggested_for_a_field_that_is_already_mapped(self):
        """Suggesting where a mapping already exists would invite overwriting one."""
        descriptor = build_writable_field_descriptor()
        for field in REVIEWED_SUGGESTIONS:
            assert descriptor[field]['kind'] == 'unmapped', (
                f'{field} is {descriptor[field]["kind"]}, not awaiting a concept'
            )
