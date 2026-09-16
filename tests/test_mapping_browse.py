import pytest
from rest_framework.test import APIClient, APIRequestFactory, force_authenticate

from omop_core.models import SourceCodeConceptMapping
from patient_portal.api.views import code_mapping_list
from patient_portal.models import Identity

pytestmark = pytest.mark.django_db


@pytest.fixture
def browse():
    user = Identity.objects.create_user(email='browse@example.test', is_staff=True)

    def get(**params):
        request = APIRequestFactory().get('/api/v1/code-mappings/', {'browse': '1', **params})
        force_authenticate(request, user=user)
        return code_mapping_list(request)
    return get


def row(code, **kwargs):
    return SourceCodeConceptMapping.objects.create(source_code=code, **{'source_vocabulary_id': 'ICD10', 'status': 'proposed', **kwargs})


def test_pages_sort_before_slicing_and_report_full_counts(browse):
    for i in range(105):
        row(f'C{i:03}', occurrence_count=i)
    first = browse().data
    assert len(first['results']) == 100
    assert first['results'][0]['source_code'] == 'C104'
    assert first['pages']['Unmapped'] == {'page': 1, 'page_size': 100, 'total': 105}
    assert first['tabs'][0]['proposed'] == 105
    second = browse(page_0=2).data
    assert second['results'][0]['source_code'] == 'C004'
    assert {r['mapping_id'] for r in first['results']}.isdisjoint(r['mapping_id'] for r in second['results'])
    assert browse(page_0=99).data['pages']['Unmapped']['page'] == 2
    assert browse(order_0='source_code').data['results'][0]['source_code'] == 'C000'


def test_aliases_global_search_rejected_and_sections(browse):
    row('A', source_vocabulary_id='ICD10CM', occurrence_count=50)
    row('B', status='approved')
    row('C', status='rejected')
    row('D', source_vocabulary_id='LOINC', source_code_description='distinctive phrase')
    row('E', origin_system='athena', status='approved')
    data = browse(source='ICD10').data
    assert {r['source_code'] for r in data['results']} == {'A', 'B', 'E'}
    assert data['rejected_count'] == 1
    assert len(browse(source='ICD10', show_rejected='true').data['results']) == 4
    assert [r['source_code'] for r in browse(source='ICD10', search='distinctive').data['results']] == ['D']
    assert browse(source='').data['results'] == []


def test_duplicates_include_off_page_and_rejected_members(browse):
    row('MATCH', occurrence_count=0)
    row(' match ', source_vocabulary_id='ICD10CM', status='rejected')
    for i in range(101):
        row(f'X{i}', occurrence_count=100)
    data = browse(search='X').data
    assert {r['source_code'] for r in data['duplicates']} == {'MATCH', ' match '}
    assert len(data['results']) == 100


def test_bad_paging_or_sort_is_a_validation_error(browse):
    assert browse(page_0='bad').status_code == 400
    assert browse(order_0='password').status_code == 400


@pytest.mark.parametrize('search', ['', 'CODE'])
@pytest.mark.parametrize('show_rejected', ['false', 'true'])
def test_combined_counts_preserve_all_sections_and_rejections(browse, search, show_rejected):
    row('CODE-A', source_vocabulary_id='ICD10CM')
    row('CODE-B', status='approved')
    row('CODE-C', status='rejected')
    row('CODE-D', source_vocabulary_id='LOINC', status='approved')
    row('CODE-E', origin_system='athena', status='approved')
    row('CODE-F', origin_system='athena', status='rejected')
    data = browse(source='ICD10CM', search=search, show_rejected=show_rejected).data
    assert data['pages']['Unmapped']['total'] == (2 if show_rejected == 'true' else 1)
    assert data['pages']['Mapped']['total'] == (2 if search else 1)
    assert data['pages']['Athena Mapped']['total'] == (2 if show_rejected == 'true' else 1)
    assert data['rejected_count'] == 1
    assert len(data['results']) == sum(page['total'] for page in data['pages'].values())


def test_default_browse_reuses_counts_and_loads_sections_together(browse):
    from django.db import connection
    from django.test.utils import CaptureQueriesContext
    row('A')
    row('B', status='approved')
    row('C', origin_system='athena', status='approved')
    with CaptureQueriesContext(connection) as queries:
        response = browse(source='ICD10')
    assert response.status_code == 200
    assert len(response.data['results']) == 3
    sql = [q['sql'] for q in queries]
    assert not any('COUNT(*) AS "__count"' in query for query in sql)
    assert sum('"source_code_concept_mapping"."id",' in query and 'LEFT OUTER JOIN "concept"' in query for query in sql) <= 2


def test_spa_shell_is_not_cached_across_default_changes():
    from django.urls import resolve
    from django.test import RequestFactory
    response = resolve('/code-mappings/').func(RequestFactory().get('/code-mappings/'))
    assert 'no-store' in response['Cache-Control']


@pytest.mark.parametrize('status_filter', [None, 'approved'])
def test_plain_list_pages_before_serialization_and_filters_in_database(status_filter):
    from unittest.mock import patch
    from patient_portal.api import views

    SourceCodeConceptMapping.objects.bulk_create([
        SourceCodeConceptMapping(source_vocabulary_id='ICD10', source_code=f'P{i:03}', status='approved')
        for i in range(105)
    ] + [SourceCodeConceptMapping(source_vocabulary_id='ICD10', source_code='A-proposed')])
    user = Identity.objects.create_user(email='list-pages@example.test', is_staff=True)

    def get(**params):
        if status_filter:
            params['status'] = status_filter
        request = APIRequestFactory().get('/api/v1/code-mappings/', params)
        force_authenticate(request, user=user)
        return code_mapping_list(request)

    with patch.object(views, '_serialize_code_mapping_row', wraps=views._serialize_code_mapping_row) as serialize:
        first = get(page_size=100000)
        assert first.status_code == 200
        assert len(first.data) == serialize.call_count == 100
    assert first['X-Page-Size'] == '100'
    assert first['X-Total-Count'] == ('105' if status_filter else '106')
    assert 'page=2' in first['Link']
    assert 'rel="next"' in first['Link']
    second = get(page=2)
    assert len(second.data) == (5 if status_filter else 6)
    assert 'rel="prev"' in second['Link']
    assert 'rel="next"' not in second['Link']
    assert {r['mapping_id'] for r in first.data}.isdisjoint(r['mapping_id'] for r in second.data)
    if status_filter:
        assert all(r['status'] == 'approved' for r in first.data + second.data)
    assert get(page='bad').status_code == 404
    assert get(page=999).status_code == 404


@pytest.mark.parametrize('origin', ['https://curation.example', 'https://blocked.example'])
def test_plain_list_exposes_pagination_headers_only_to_allowed_origins(settings, origin):
    settings.CORS_ALLOW_ALL_ORIGINS = False
    settings.CORS_ALLOWED_ORIGINS = ['https://curation.example']
    SourceCodeConceptMapping.objects.bulk_create([
        SourceCodeConceptMapping(source_vocabulary_id='ICD10', source_code=f'C{i:03}')
        for i in range(105)
    ])
    client = APIClient()
    client.force_authenticate(user=Identity.objects.create_user(email='cors-pages@example.test', is_staff=True))

    response = client.get('/api/v1/code-mappings/', {'source': 'ICD10'}, HTTP_ORIGIN=origin, secure=True)

    assert response.status_code == 200
    assert len(response.data) == 100
    assert response['X-Total-Count'] == '105'
    assert response['X-Page'] == '1'
    assert response['X-Page-Size'] == '100'
    assert 'page=2' in response['Link']
    if origin == 'https://curation.example':
        assert response['Access-Control-Allow-Origin'] == origin
        exposed = {name.strip().lower() for name in response['Access-Control-Expose-Headers'].split(',')}
        assert {'link', 'x-total-count', 'x-page', 'x-page-size'} <= exposed
    else:
        assert 'Access-Control-Allow-Origin' not in response
        assert 'Access-Control-Expose-Headers' not in response
