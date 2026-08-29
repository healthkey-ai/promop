"""#774 / #723 — the value concepts, not just the question concept.

Both issues asked for a single SNOMED concept: 4267143 "Language" and 4010833
"Pre-existing condition". Each is the right *question* concept but carries no
answer — 4267143 does not say which language, 4010833 does not say which
condition. Migration 0182 records both as proposed mappings and adds coded
answers for preexisting_conditions.

languages_skills gets no choices on purpose; see the migration docstring. The
test below pins that absence, because "no choices" is otherwise indistinguishable
from "someone forgot".
"""

import pytest

from omop_core.models import (
    Concept, FieldChoice, FieldChoiceCode, FieldConceptMapping,
)
from tests.factories import ConceptFactory, VocabularyFactory


pytestmark = pytest.mark.django_db


def _migration():
    from importlib import import_module
    return import_module(
        'omop_core.migrations.0185_seed_language_and_condition_value_concepts')


@pytest.fixture(autouse=True)
def seeded():
    """Run migration 0182's seed directly, against its question concepts.

    The pytest suite runs --no-migrations, so the data migration never executes
    and every assertion below would trivially fail on an empty table. Calling
    the migration's own seed function keeps the test honest about what ships
    rather than restating the values in a fixture that could drift from it.

    The two question concepts are created first because seed() skips a mapping
    whose concept is absent — without them the FieldConceptMapping half would
    never run and its tests would pass while asserting nothing.
    """
    from django.apps import apps as django_apps

    snomed = VocabularyFactory(vocabulary_id='SNOMED')
    for _field, vocabulary_id, code, _note in _migration()._QUESTION_CONCEPTS:
        assert vocabulary_id == 'SNOMED'
        ConceptFactory(vocabulary=snomed, concept_code=code,
                       concept_name=f'Question concept {code}')

    _migration().seed(django_apps, None)


def _codes_for(field_name, display):
    choice = FieldChoice.objects.get(field_name=field_name, display=display)
    return {c.code for c in FieldChoiceCode.objects.filter(choice=choice)}


# ---------------------------------------------------------------------------
# The question concepts (FieldConceptMapping)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize('field_name,code', [
    ('languages_skills', '61909002'),
    ('preexisting_conditions', '102478008'),
])
def test_question_concept_is_recorded_as_a_proposed_mapping(field_name, code):
    mapping = FieldConceptMapping.objects.get(field_name=field_name)
    assert (mapping.vocabulary_id, mapping.concept_code) == ('SNOMED', code)
    assert mapping.concept is not None
    assert mapping.status == 'proposed'


@pytest.mark.parametrize('field_name', ['languages_skills',
                                        'preexisting_conditions'])
def test_seeded_mapping_carries_no_write_details(field_name):
    """A migration proposes what a field means; it does not make it writable.

    _curated_writes requires status='approved' plus a non-empty omop_table and
    source_value before it emits a write recipe. Seeding those here would make
    both fields editable without anyone having reviewed where their facts land.
    """
    mapping = FieldConceptMapping.objects.get(field_name=field_name)
    assert (mapping.omop_table, mapping.source_value, mapping.value_kind) == \
        ('', '', '')


def test_missing_concept_leaves_no_mapping_behind(caplog):
    """seed() must skip, not half-write, when SNOMED has not been loaded.

    SNOMED loads separately from migrations, so a fresh database can reach this
    migration with neither concept present. A mapping written without its
    concept would point at nothing.
    """
    from django.apps import apps as django_apps

    FieldConceptMapping.objects.all().delete()
    Concept.objects.filter(
        concept_code__in=[c for _f, _v, c, _n in _migration()._QUESTION_CONCEPTS],
    ).delete()

    with caplog.at_level('WARNING'):
        _migration().seed(django_apps, None)

    assert not FieldConceptMapping.objects.exists()
    assert 'is missing' in caplog.text


# ---------------------------------------------------------------------------
# The answers (FieldChoice / FieldChoiceCode)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize('display,code', [
    ('Cardiac Issues', '56265001'),
    ('Renal Impairment', '236423003'),
    ('Infections', '40733004'),
])
def test_value_choices_carry_their_concept_code(display, code):
    assert code in _codes_for('preexisting_conditions', display)


@pytest.mark.parametrize('display', [
    'Neurological and Psychiatric Conditions',
    'Autoimmune and Inflammatory Disorders',
    'Pregnancy or Breastfeeding',
])
def test_two_axis_categories_carry_both_codes(display):
    """These categories name two clinical axes, so one code cannot cover them."""
    assert len(_codes_for('preexisting_conditions', display)) == 2


@pytest.mark.parametrize('display', ['Performance Status', 'Prior Therapies'])
def test_deliberately_uncoded_choices_stay_uncoded(display):
    """Coding these would assert something clinically untrue.

    They are not conditions but other eligibility axes sharing the category
    list. Pinned rather than left implicit: a later bulk-suggestion pass would
    otherwise attach a plausible-looking code and nobody would notice.
    """
    assert _codes_for('preexisting_conditions', display) == set()


def test_languages_skills_gets_no_choices():
    """#774 reads as though it wants a language value set. It cannot have one.

    patient_record_service derives the field as a composite over two
    vocabularies — "English language: speak, Spanish language: write" — so a
    bare language name could never match a stored value. Offering English and
    Spanish as choices would put options in front of a curator that no record
    can hold.
    """
    assert not FieldChoice.objects.filter(field_name='languages_skills').exists()


# ---------------------------------------------------------------------------
# Standardness
# ---------------------------------------------------------------------------

def test_every_seeded_code_is_a_standard_snomed_concept():
    """The property that separates these from #803's MeSH concept.

    A non-standard concept cannot be written to a *_concept_id column
    (docs/vocabularies.md), so a choice coded against one would be unusable
    exactly where it matters.

    This can only be checked where SNOMED is actually loaded. The pytest
    database is built with --no-migrations and holds no vocabulary, so rather
    than iterating over concepts that are all absent and passing vacuously, the
    test skips and says so. It does real work against a database loaded from
    Athena, which is where the claim can be falsified.
    """
    rows = list(FieldChoiceCode.objects.filter(
        choice__field_name='preexisting_conditions'))
    assert rows, 'migration 0182 seeded nothing'

    checked = 0
    for row in rows:
        concept = Concept.objects.filter(
            vocabulary_id=row.vocabulary_id, concept_code=row.code,
        ).first()
        if concept is None:
            continue
        checked += 1
        assert concept.standard_concept == 'S', (
            f'{row.vocabulary_id}:{row.code} ({concept.concept_name}) is not '
            f'standard, so it cannot be written to a *_concept_id column'
        )

    if checked == 0:
        pytest.skip(
            f'none of the {len(rows)} seeded codes are present: this database '
            f'has no SNOMED load, so standardness cannot be checked here')


def test_mixed_domain_category_keeps_the_condition_primary():
    """"Pregnancy or Breastfeeding" spans two OMOP domains.

    77386006 "Pregnancy" is Condition-domain; 413712001 "Breastfeeding
    (mother)" is Observation-domain, because breastfeeding is a state rather
    than a disorder and SNOMED offers no Condition concept for it. A consumer
    that takes the primary code and writes it to condition_occurrence must get
    the Condition one, so the ordering is pinned here rather than left to the
    order the list happens to be written in.
    """
    choice = FieldChoice.objects.get(field_name='preexisting_conditions',
                                     display='Pregnancy or Breastfeeding')
    primary = FieldChoiceCode.objects.get(choice=choice, is_primary=True)
    assert primary.code == '77386006'


@pytest.mark.parametrize('field_name', ['languages_skills',
                                        'preexisting_conditions'])
def test_curator_suggestion_agrees_with_the_seeded_mapping(field_name):
    """The mapping page shows both, so they must not contradict each other.

    _build_suggestion runs unconditionally — it is not suppressed once a field
    has a mapping — so a curator opening either field would otherwise see a
    recorded mapping and a suggested code that disagree, with nothing on screen
    saying which is intended. Both fields fall through to SUGGESTED_FIELD_CODES
    (they are in neither LAB_FIELD_TO_LOINC nor DERIVED_FIELD_TO_CODE and have
    no provenance entry), which is the dict this pins.
    """
    from omop_core.services.mappings import SUGGESTED_FIELD_CODES

    mapping = FieldConceptMapping.objects.get(field_name=field_name)
    assert SUGGESTED_FIELD_CODES[field_name] == \
        (mapping.concept_code, mapping.vocabulary_id)
