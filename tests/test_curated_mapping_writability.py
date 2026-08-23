"""An approved concept mapping is what makes a field writable.

The curation interface records a decision per PatientRecord field. Until now
nothing acted on it: `FieldConceptMapping` said in its own docstring that it
"does NOT make the field writable", so a curator could approve a mapping, see it
listed as approved, and find the field exactly as read-only as before.

These pin the other half — the descriptor reads approved mappings and emits an
editable entry — and the bar a row has to clear before it counts. A concept says
what a fact means; it does not say where the fact goes or how to find it again.
"""
import pytest

from omop_core.models import FieldConceptMapping
from omop_core.services.write_descriptor import build_writable_field_descriptor
from tests.factories import ConceptFactory, VocabularyFactory

pytestmark = pytest.mark.django_db

FIELD = 'planned_therapies'   # unmapped by default, so a clean subject


def _concept():
    VocabularyFactory(vocabulary_id='SNOMED')
    return ConceptFactory(
        concept_code='313059006', vocabulary_id='SNOMED',
        concept_name='Planned therapy',
    )


def _mapping(**overrides):
    defaults = {
        'field_name': FIELD,
        'concept': _concept(),
        'omop_table': 'observation',
        'source_value': 'planned-therapies',
        'value_kind': 'string',
        'status': 'approved',
    }
    defaults.update(overrides)
    return FieldConceptMapping.objects.create(**defaults)


class TestAnApprovedMappingMakesTheFieldWritable:
    def test_the_field_becomes_editable(self):
        mapping = _mapping()

        entry = build_writable_field_descriptor()[FIELD]

        assert entry['kind'] == 'editable'
        assert entry['writable'] is True
        assert entry['target'] == 'observation'
        assert entry['concept_id'] == mapping.concept_id
        assert entry['source_value'] == 'planned-therapies'

    def test_it_carries_everything_a_write_needs(self):
        _mapping()

        entry = build_writable_field_descriptor()[FIELD]

        required = {'target', 'concept_id', 'value_kind', 'type_concept_id',
                    'source_value'}
        assert required <= set(entry)

    def test_it_is_marked_as_curated(self):
        # Distinguishes a field made writable by a reviewer from one hardcoded
        # in the mapping tables, which matters when explaining where a write
        # path came from.
        _mapping()
        assert build_writable_field_descriptor()[FIELD]['curated'] is True


class TestAnIncompleteMappingIsStillAdvisory:
    """Short of a full recipe, a mapping records a decision and nothing more.

    Offering a box that writes a row derivation cannot find is worse than
    offering none: the save succeeds and the value never comes back.
    """

    def test_a_proposed_mapping_does_not_count(self):
        _mapping(status='proposed')
        assert build_writable_field_descriptor()[FIELD]['writable'] is False

    def test_a_rejected_mapping_does_not_count(self):
        _mapping(status='rejected')
        assert build_writable_field_descriptor()[FIELD]['writable'] is False

    def test_a_mapping_without_a_source_value_does_not_count(self):
        # Derivation matches on source_value. Without one the written row is
        # unfindable, so the field would appear to save and never change.
        _mapping(source_value='')
        assert build_writable_field_descriptor()[FIELD]['writable'] is False

    def test_a_mapping_without_a_concept_does_not_count(self):
        _mapping(concept=None)
        assert build_writable_field_descriptor()[FIELD]['writable'] is False

    def test_a_mapping_to_a_table_this_cannot_write_does_not_count(self):
        # Conditions and procedures are real OMOP tables, but the editor has no
        # write path for them yet. Claiming otherwise would 400 on save.
        _mapping(omop_table='condition_occurrence')
        assert build_writable_field_descriptor()[FIELD]['writable'] is False


class TestBoundedAnswers:
    def test_a_value_vocabulary_becomes_the_offered_options(self):
        # The vocabulary tables are seeded by their own migrations; this reads
        # whatever is there rather than inventing rows, because ingest filters
        # against exactly the same set.
        from omop_core.models import SctEligibility

        expected = list(
            SctEligibility.objects.order_by('title').values_list('title', flat=True)
        )
        assert expected, 'expected the SctEligibility vocabulary to be seeded'
        _mapping(value_vocabulary='SctEligibility', multiple=True)

        entry = build_writable_field_descriptor()[FIELD]

        assert [o['value'] for o in entry['options']] == expected
        assert entry['multiple'] is True

    def test_an_unknown_vocabulary_name_is_ignored_not_fatal(self):
        # A curator can mistype. The field stays writable as free text rather
        # than the whole descriptor failing to build.
        _mapping(value_vocabulary='NoSuchVocabulary')

        entry = build_writable_field_descriptor()[FIELD]

        assert entry['writable'] is True
        assert 'options' not in entry
