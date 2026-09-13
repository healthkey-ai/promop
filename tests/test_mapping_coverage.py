import pytest

from omop_core.models import FieldChoice, TherapyRegimen, TherapyComponent, TherapyClass
from omop_core.services.mapping_coverage import answer_coverage, therapy_coverage
from omop_core.services.field_values import save_mapping
from tests.factories import ConceptFactory

pytestmark = pytest.mark.django_db


def test_coverage_reads_existing_therapy_tables_and_preserves_classification(django_assert_num_queries):
    regimen = TherapyRegimen.objects.create(code='local', title='Unmapped regimen')
    component = TherapyComponent.objects.create(code='invalid', title='Old ingredient',
        concept=ConceptFactory(invalid_reason='D'))
    classification = TherapyClass.objects.create(code='class', title='Component class',
        concept=ConceptFactory(vocabulary__vocabulary_id='HemOnc', concept_class__concept_class_id='Component Class',
                               standard_concept='C'))
    with django_assert_num_queries(3):
        result = therapy_coverage()
    assert result['regimens']['unmapped'] >= 1
    assert result['components']['invalid_target'] >= 1
    assert result['classes']['classification_only'] >= 1
    for reference in (regimen, component, classification):
        assert type(reference).objects.filter(pk=reference.pk, code=reference.code).exists()


def test_answer_coverage_distinguishes_reviewed_local_and_unreviewed_choices():
    choice = FieldChoice.objects.create(field_name='her2_status', display='Local result')
    save_mapping(choice, {'status': 'approved', 'outcome': 'no_equivalent', 'notes': 'Reviewed exact local meaning.'})
    FieldChoice.objects.create(field_name='her2_status', display='Pending review')
    result = answer_coverage()
    assert result['reviewed_no_equivalent'] == 1
    assert result['needs_review'] >= 1
    assert result.get('approved_mapped', 0) == 0
