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


@pytest.mark.parametrize('scope', ['other_person', 'other_parent'])
def test_overflow_note_reference_cannot_escape_patient_and_variant(setup, scope):
    from tests.factories import PersonFactory
    from omop_core.services.genomics import list_variants
    person, _, _ = setup
    result = save_variant(person, {'gene': 'BRCA1', 'variant': 'Original variant'})
    note = Note.objects.create(note_id=987654321, person=PersonFactory() if scope == 'other_person' else person,
        note_date='2026-09-01', note_type_concept_id=0, note_text='Unrelated private narrative',
        note_source_value=f'genomics:overflow:{result["id"] if scope == "other_person" else result["id"] + 1}')
    pointer = f'[note:{note.pk}]'
    Measurement.objects.filter(pk=result['id']).update(value_as_string=pointer)
    assert next(v for v in list_variants(person) if v['id'] == result['id'])['variant'] == pointer


def _maps_to(source, target, **kwargs):
    from omop_core.models import ConceptRelationship, Relationship
    relationship, _ = Relationship.objects.get_or_create(relationship_id='Maps to', defaults={
        'relationship_name': 'Maps to', 'is_hierarchical': 0, 'defines_ancestry': 0,
        'reverse_relationship_id': 'Mapped from', 'relationship_concept_id': 0})
    return ConceptRelationship.objects.create(concept_1=source, concept_2=target, relationship=relationship,
        valid_start_date=kwargs.get('valid_start_date', '1970-01-01'),
        valid_end_date=kwargs.get('valid_end_date', '2099-12-31'))


def test_genomic_question_maps_to_ambiguity_retains_source_without_picking_a_target():
    from omop_core.services.genomics import _resolve
    source = ConceptFactory(concept_code='ambiguous-genomic-question', standard_concept=None)
    first, second = ConceptFactory(), ConceptFactory()
    _maps_to(source, first)
    assert _resolve(source.concept_code, 'Measurement') == (first.pk, source.pk, 'Measurement')
    _maps_to(source, second)
    assert _resolve(source.concept_code, 'Measurement') == (0, source.pk, 'Measurement')


@pytest.mark.parametrize('expired', ['source', 'relationship', 'target'])
def test_genomic_question_resolution_rejects_expired_evidence(expired):
    from omop_core.services.genomics import _resolve
    source = ConceptFactory(concept_code='expired-genomic-question', standard_concept=None,
        **({'valid_end_date': '2000-01-01'} if expired == 'source' else {}))
    target = ConceptFactory(**({'valid_end_date': '2000-01-01'} if expired == 'target' else {}))
    _maps_to(source, target, **({'valid_end_date': '2000-01-01'} if expired == 'relationship' else {}))
    assert _resolve(source.concept_code, 'Measurement') == (0, source.pk, 'Measurement')


def test_expired_approved_question_does_not_become_a_current_standard_fact(setup):
    from omop_core.models import FieldConceptMapping
    from omop_core.services.genomics import mapped_concept
    mapping = FieldConceptMapping.objects.select_related('concept').get(field_name='genetic_mutations.origin')
    mapping.concept = ConceptFactory(valid_end_date='2000-01-01')
    mapping.vocabulary_id = mapping.concept.vocabulary_id
    mapping.concept_code = mapping.concept.concept_code
    mapping.omop_table = 'measurement'
    target, source = mapped_concept(mapping)
    assert target == 0
    assert source == mapping.concept_id
