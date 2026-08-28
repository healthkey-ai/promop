"""#774 / #723 — the value concepts, not just the question concept.

Both issues asked for a single SNOMED concept: 4267143 "Language" and 4010833
"Pre-existing condition". Each is the right *question* concept but carries no
answer — 4267143 does not say which language, 4010833 does not say which
condition. Migration 0182 adds the answers as coded choices.

These tests pin the parts that are easy to regress silently: that the coded
choices exist, that the deliberately-uncoded ones stay uncoded, and that every
code resolves to a standard SNOMED concept — the property that separates this
from the MeSH case in #803.
"""

import pytest

from omop_core.models import Concept, FieldChoice, FieldChoiceCode


pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def seeded():
    """Run migration 0182's seed directly.

    The pytest suite runs --no-migrations, so the data migration never executes
    and every assertion below would trivially fail on an empty table. Calling
    the migration's own seed function keeps the test honest about what ships
    rather than restating the values in a fixture that could drift from it.
    """
    from importlib import import_module

    from django.apps import apps as django_apps

    mig = import_module(
        'omop_core.migrations.0182_seed_language_and_condition_value_concepts')
    mig.seed(django_apps, None)


def _codes_for(field_name, display):
    choice = FieldChoice.objects.get(field_name=field_name, display=display)
    return {c.code for c in FieldChoiceCode.objects.filter(choice=choice)}


@pytest.mark.parametrize('field_name,display,code', [
    ('languages_skills', 'English', '297487008'),
    ('languages_skills', 'Spanish', '297510001'),
    ('preexisting_conditions', 'Cardiac Issues', '56265001'),
    ('preexisting_conditions', 'Renal Impairment', '236423003'),
    ('preexisting_conditions', 'Infections', '40733004'),
])
def test_value_choices_carry_their_concept_code(field_name, display, code):
    assert code in _codes_for(field_name, display)


@pytest.mark.parametrize('display', [
    'Neurological and Psychiatric Conditions',
    'Autoimmune and Inflammatory Disorders',
    'Pregnancy or Breastfeeding',
])
def test_two_axis_categories_carry_both_codes(display):
    """These categories name two clinical axes, so one code cannot cover them."""
    assert len(_codes_for('preexisting_conditions', display)) == 2


@pytest.mark.parametrize('field_name,display', [
    # Not conditions: other eligibility axes that share the category list, and
    # already modelled by ecog_performance_status and the therapy-line fields.
    ('preexisting_conditions', 'Performance Status'),
    ('preexisting_conditions', 'Prior Therapies'),
    # The absence of a specific language, not a language.
    ('languages_skills', 'Other'),
])
def test_deliberately_uncoded_choices_stay_uncoded(field_name, display):
    """Coding these would assert something clinically untrue.

    Pinned rather than left implicit: a later bulk-suggestion pass would
    otherwise attach a plausible-looking code and nobody would notice.
    """
    assert _codes_for(field_name, display) == set()


def test_every_seeded_code_is_a_standard_snomed_concept():
    """The property that separates these from #803's MeSH concept.

    A non-standard concept cannot be written to a *_concept_id column
    (docs/vocabularies.md), so a choice coded against one would be unusable
    exactly where it matters.
    """
    codes = FieldChoiceCode.objects.filter(
        choice__field_name__in=['languages_skills', 'preexisting_conditions'],
    )
    assert codes.exists(), 'migration 0182 seeded nothing'

    for row in codes:
        concept = Concept.objects.filter(
            vocabulary_id=row.vocabulary_id, concept_code=row.code,
        ).first()
        if concept is None:
            # SNOMED loads separately; absence is a fixture gap, not a defect.
            continue
        assert concept.standard_concept == 'S', (
            f'{row.vocabulary_id}:{row.code} ({concept.concept_name}) is not '
            f'standard, so it cannot be written to a *_concept_id column'
        )
