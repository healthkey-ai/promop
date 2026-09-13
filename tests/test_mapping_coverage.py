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


def test_therapy_api_reports_existing_relationship_coverage_without_extra_queries(django_assert_num_queries):
    from rest_framework.test import APIRequestFactory, force_authenticate
    from patient_portal.api.views import therapy_regimen_detail
    from omop_core.models import TherapyRegimenComponent, TherapyComponentClassLink
    from django.contrib.auth import get_user_model

    regimen = TherapyRegimen.objects.create(code='review-regimen', title='Review regimen')
    component = TherapyComponent.objects.create(code='review-drug', title='Review drug',
        concept=ConceptFactory(vocabulary__vocabulary_id='RxNorm', domain__domain_id='Drug'))
    classification = TherapyClass.objects.create(code='review-class', title='Review class',
        concept=ConceptFactory(vocabulary__vocabulary_id='HemOnc',
            concept_class__concept_class_id='Component Class', standard_concept='C'))
    TherapyRegimenComponent.objects.create(regimen=regimen, component=component)
    TherapyComponentClassLink.objects.create(component=component, therapy_class=classification)
    request = APIRequestFactory().get('/')
    force_authenticate(request, user=get_user_model().objects.create_user(email='coverage-reader@example.com'))
    with django_assert_num_queries(3):
        response = therapy_regimen_detail(request, regimen.code)
    assert response.status_code == 200
    assert response.data['mapping_disposition'] == 'unmapped'
    drug = response.data['components'][0]
    assert drug['code'] == component.code
    assert drug['mapping_disposition'] == 'passes_role_screen'
    assert drug['mapped_concept']['vocabulary_id'] == 'RxNorm'
    assert drug['classes'][0]['mapping_disposition'] == 'classification_only'


def test_regimen_curation_can_reach_rows_after_first_page_and_filter_disease_round():
    from rest_framework.test import APIRequestFactory, force_authenticate
    from patient_portal.api.views import therapy_regimen_list, disease_therapy_regimen_list
    from omop_core.models import Disease, DiseaseTherapyRegimen, TherapyRound
    from django.contrib.auth import get_user_model

    TherapyRegimen.objects.bulk_create([
        TherapyRegimen(code=f'review-{n}', title=f'Review pagination {n:02}') for n in range(51)])
    regimen = TherapyRegimen.objects.get(code='review-50')
    disease = Disease.objects.create(code='review-disease', title='Review disease')
    therapy_round = TherapyRound.objects.create(code='review-round', title='Review round')
    other_round = TherapyRound.objects.create(code='review-other', title='Other round')
    link = DiseaseTherapyRegimen.objects.create(disease=disease, round=therapy_round, regimen=regimen)
    DiseaseTherapyRegimen.objects.create(disease=disease, round=other_round, regimen=regimen)
    user = get_user_model().objects.create_user(email='coverage-reader@example.com')

    def get(view, params):
        request = APIRequestFactory().get('/', params)
        force_authenticate(request, user=user)
        return view(request)

    page = get(therapy_regimen_list, {'search': 'Review pagination', 'offset': 50})
    assert [row['code'] for row in page.data] == [regimen.code]
    assert page.data[0]['mapping_disposition'] == 'unmapped'
    assert get(therapy_regimen_list, {'offset': -1}).status_code == 400
    params = {'disease': disease.code, 'round': therapy_round.code}
    assert [row['code'] for row in get(therapy_regimen_list, params).data] == [regimen.code]
    links = get(disease_therapy_regimen_list, params).data
    assert [row['id'] for row in links] == [link.pk]
    assert links[0]['regimen_mapping']['mapping_disposition'] == 'unmapped'
