"""Columns whose answer set belongs to the disease, not to the field.

`stage` is Ann Arbor for follicular lymphoma and ISS/R-ISS for myeloma;
`tumor_grade` is 1-3 for breast cancer and the centroblast grades for FL. Each
tab names the system and supplies its own list, while the single `FieldChoice`
set is the follicular one. Attaching it to the descriptor made the federated
editor override the tab — a myeloma record offered Ann Arbor stages. The
exclusion must hold on both attach points: the curated-mapping path and the
fallback loop.
"""
import pytest

from omop_core.models import FieldChoice, FieldConceptMapping
from omop_core.services.write_descriptor import build_writable_field_descriptor
from tests.factories import ConceptFactory

pytestmark = pytest.mark.django_db

EHR_TYPE = 32817


def _seed_choices(field_name, displays):
    for order, display in enumerate(displays):
        FieldChoice.objects.create(
            field_name=field_name, display=display, sort_order=order)


@pytest.mark.parametrize('field_name', ['stage', 'tumor_grade'])
def test_the_disease_specific_set_is_not_attached(field_name):
    _seed_choices(field_name, ('I', 'II', 'III'))

    entry = build_writable_field_descriptor()[field_name]

    assert entry['writable'] is True
    assert entry.get('options') is None


def test_the_set_does_not_come_back_through_an_approved_mapping():
    # A curator re-saving the mapping is a normal action; the curated path
    # attaches options on its own, so the exclusion has to happen before it.
    _seed_choices('stage', ('I', 'II', 'III'))
    FieldConceptMapping.objects.create(
        field_name='stage',
        concept=ConceptFactory(),
        vocabulary_id='SNOMED',
        concept_code='399390009',
        omop_table='observation',
        source_value='stage',
        value_kind='string',
        type_concept_id=EHR_TYPE,
        status='approved',
    )

    entry = build_writable_field_descriptor()['stage']

    assert entry.get('options') is None
