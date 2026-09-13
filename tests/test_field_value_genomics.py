import pytest

from omop_core.models import FieldChoice, Measurement, Note, Observation
from omop_core.services.field_values import save_mapping
from omop_core.services.genomics import FIELDS, save_variant
from tests.factories import ConceptFactory
from tests.test_genomics_crud import setup  # noqa: F401 — shared legacy/Athena fixtures

pytestmark = pytest.mark.django_db


def test_genomic_answer_override_routes_to_its_domain_and_preserves_source(setup):
    person, _, _ = setup
    source = 'Original source laboratory germline interpretation alias ' * 3
    choice = FieldChoice.objects.create(field_name='genetic_mutations.origin',
        display='Germline', canonical_value='Germline', aliases=[source])
    answer = ConceptFactory(domain__domain_id='Meas Value')
    domain = 'Observation' if FIELDS['origin'][1] == 'Measurement' else 'Measurement'
    question = ConceptFactory(domain__domain_id=domain)
    save_mapping(choice, {'status': 'approved', 'outcome': 'mapped', 'target_concept': answer,
        'question_concept': question, 'notes': 'Reviewed laboratory source answer and question.',
        'vocabulary_release': 'Test release'})
    result = save_variant(person, {'gene': 'BRCA1', 'origin': source})
    model = Measurement if domain == 'Measurement' else Observation
    row = model.objects.get(person=person, **{f'{domain.lower()}_concept': question})
    assert row.value_as_concept_id == answer.pk
    assert row.value_source_value.startswith('[note:')
    note_id = int(row.value_source_value.removeprefix('[note:').removesuffix(']'))
    assert Note.objects.get(pk=note_id).note_text == source.strip()
    assert result['origin'] == 'Germline'
