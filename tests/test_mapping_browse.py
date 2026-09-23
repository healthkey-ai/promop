import pytest
from rest_framework.test import APIClient, APIRequestFactory, force_authenticate

from omop_core.models import MappingDestinationCandidate, SourceCodeConceptMapping
from patient_portal.api.views import code_mapping_detail, code_mapping_list
from patient_portal.models import Identity
from tests.factories import ConceptFactory

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


@pytest.mark.parametrize('params', [{}, {'source': '__overall__'}, {'search': 'SORT-'}])
def test_unmapped_defaults_to_seen_descending_before_pagination(browse, params):
    """Unmapped defaults to Seen, like every other section (#1575).

    It grouped by provenance first until then. That buried the rows a Suggest
    run had just answered: enqueue writes ``origin_system=''`` and a run
    rewrites it to ``suggest v0.4``, which sorts last ascending.
    """
    curated = [row(f'SORT-C{i:03}', origin_system='curator', occurrence_count=i // 2) for i in range(101)]
    suggested = row('SORT-S', origin_system='suggest v0.4', occurrence_count=1000)
    expected = sorted([suggested] + curated, key=lambda m: (-m.occurrence_count, m.source_code, m.pk))
    first = browse(**params).data
    second = browse(page_0=2, **params).data
    assert first['pages']['Unmapped']['total'] == 102
    # The highest-Seen row leads regardless of its provenance.
    assert first['results'][0]['mapping_id'] == suggested.pk
    assert [r['mapping_id'] for r in first['results'] + second['results']] == [m.pk for m in expected]
    # Provenance remains available as an explicit sort.
    explicit = browse(order_0='origin_system', **params).data
    assert explicit['results'][0]['origin_system'] == 'curator'


def test_every_section_shares_the_seen_default(browse):
    for status in ['proposed', 'approved', 'rejected']:
        row(f'{status}-low', status=status, origin_system='curator', occurrence_count=1)
        row(f'{status}-high', status=status, origin_system='suggest v0.4', occurrence_count=500)
    row('athena-low', origin_system='athena', occurrence_count=1)
    row('athena-high', origin_system='athena', occurrence_count=500)
    data = browse().data
    by_section = {}
    for r in data['results']:
        section = ('Athena Mapped' if r['origin_system'] == 'athena'
                   else {'approved': 'Mapped', 'rejected': 'Rejected'}.get(r['status'], 'Unmapped'))
        by_section.setdefault(section, []).append(r['source_code'])
    assert by_section['Unmapped'] == ['proposed-high', 'proposed-low']
    assert by_section['Mapped'] == ['approved-high', 'approved-low']
    assert by_section['Rejected'] == ['rejected-high', 'rejected-low']
    assert by_section['Athena Mapped'] == ['athena-high', 'athena-low']


def test_provenance_filter_narrows_rows_totals_and_pagination(browse):
    for i in range(3):
        row(f'CUR-{i}', origin_system='curator', occurrence_count=10)
    for i in range(7):
        row(f'SUG-{i}', origin_system='suggest v0.4', occurrence_count=99)
    unfiltered = browse().data
    assert unfiltered['pages']['Unmapped']['total'] == 10
    assert unfiltered['selected_provenance'] == ''

    filtered = browse(provenance='curator').data
    assert [r['source_code'] for r in filtered['results']] == ['CUR-0', 'CUR-1', 'CUR-2']
    # Totals follow the filter, or pagination would offer pages with no rows.
    assert filtered['pages']['Unmapped']['total'] == 3
    assert filtered['selected_provenance'] == 'curator'
    # The tab strip keeps counting the whole tab -- it is how a curator sees
    # what is there before filtering.
    assert unfiltered['tabs'][0]['proposed'] == filtered['tabs'][0]['proposed'] == 10


def test_provenance_filter_lists_every_value_on_the_tab_with_counts(browse):
    row('A', origin_system='curator', occurrence_count=1)
    row('B', origin_system='suggest v0.4', occurrence_count=1)
    row('C', origin_system='suggest v0.4', occurrence_count=1)
    row('D', origin_system='', occurrence_count=1)
    data = browse().data
    assert data['provenances'] == [
        {'origin_system': 'suggest v0.4', 'count': 2},
        {'origin_system': '', 'count': 1},
        {'origin_system': 'curator', 'count': 1},
    ]
    # Choosing one must not drop the others from the control.
    assert browse(provenance='curator').data['provenances'] == data['provenances']


def test_blank_provenance_is_filterable_through_a_sentinel(browse):
    """'' is a real provenance -- enqueue_unmapped_source_codes writes it for
    every newly queued code -- but '' on the wire already means "no filter"."""
    row('QUEUED-1', origin_system='', occurrence_count=9)
    row('QUEUED-2', origin_system='', occurrence_count=8)
    row('CURATED', origin_system='curator', occurrence_count=7)
    assert browse().data['pages']['Unmapped']['total'] == 3
    filtered = browse(provenance='__blank__').data
    assert [r['source_code'] for r in filtered['results']] == ['QUEUED-1', 'QUEUED-2']
    assert filtered['pages']['Unmapped']['total'] == 2
    assert filtered['selected_provenance'] == '__blank__'
    # An empty value is still "no filter", not "the blank provenance".
    assert browse(provenance='').data['pages']['Unmapped']['total'] == 3


def test_athena_rows_are_not_offered_as_a_filter_value(browse):
    """Athena is reference data in a section that starts collapsed, and on the
    ICD-10 tab it outnumbers everything -- offering it reads as an empty page."""
    for i in range(5):
        row(f'ATH-{i}', origin_system='athena', status='approved')
    row('WORK', origin_system='HT-One')
    assert browse().data['provenances'] == [{'origin_system': 'HT-One', 'count': 1}]


def test_search_rebuilds_the_options_from_the_rows_the_filter_will_act_on(browse):
    """A search reaches across tabs, so the active tab's counts would describe
    a different set of rows than the filter applies to."""
    row('HIT-A', source_vocabulary_id='ICD10', origin_system='curator')
    row('HIT-B', source_vocabulary_id='RxNorm', origin_system='fhir-sync')
    row('MISS', source_vocabulary_id='ICD10', origin_system='hk-labs')
    assert browse(source='ICD10').data['provenances'] == [
        {'origin_system': 'curator', 'count': 1}, {'origin_system': 'hk-labs', 'count': 1},
    ]
    # Searching widens to every tab, so the off-tab provenance must be offered.
    assert browse(source='ICD10', search='HIT-').data['provenances'] == [
        {'origin_system': 'curator', 'count': 1}, {'origin_system': 'fhir-sync', 'count': 1},
    ]


def test_provenance_options_cost_no_extra_query(browse):
    """origin_system carries no index, so a GROUP BY of its own is a sequential
    scan of the tab on every browse and every post-approve refresh. The options
    ride on the aggregate browse already runs."""
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    def query_count():
        with CaptureQueriesContext(connection) as captured:
            data = browse().data
        return len(captured.captured_queries), data

    for i in range(30):
        row(f'Q{i:03}', origin_system='curator' if i % 2 else '', occurrence_count=i)
    baseline, first = query_count()
    for i in range(30, 90):
        row(f'Q{i:03}', origin_system='hk-labs', occurrence_count=i)
    grown, second = query_count()
    assert grown == baseline, f'query count grew {baseline} -> {grown}'
    # The options really are being produced, so the count above is not flat
    # because the feature quietly did nothing.
    assert {o['origin_system'] for o in first['provenances']} == {'curator', ''}
    assert {o['origin_system'] for o in second['provenances']} == {'curator', '', 'hk-labs'}


def test_provenance_filter_composes_with_search_and_leaves_duplicates_alone(browse):
    # A duplicate is one code twice after Upper(Trim(...)); the unique
    # constraint forbids two rows with the identical spelling.
    row('DUP', origin_system='curator', occurrence_count=5)
    row(' dup ', origin_system='suggest v0.4', occurrence_count=5)
    row('OTHER', origin_system='curator', occurrence_count=5)
    data = browse(search='DUP', provenance='curator').data
    assert [r['source_code'] for r in data['results']] == ['DUP']
    # Both halves of the duplicate still surface: hiding one would turn the
    # duplicate warning into a puzzle.
    assert sorted(r['origin_system'] for r in data['duplicates']) == ['curator', 'suggest v0.4']


def test_reversing_provenance_keeps_seen_descending_and_other_defaults(browse):
    for status in ['proposed', 'approved', 'rejected']:
        row(f'{status}-C', status=status, origin_system='curator', occurrence_count=100)
        row(f'{status}-SA', status=status, origin_system='suggest v0.4', occurrence_count=1)
        row(f'{status}-SZ', status=status, origin_system='suggest v0.4', occurrence_count=200)
    data = browse(order_0='-origin_system').data
    assert [r['source_code'] for r in data['results'] if r['status'] == 'proposed'] == [
        'proposed-SZ', 'proposed-SA', 'proposed-C',
    ]
    for status in ['approved', 'rejected']:
        assert [r['source_code'] for r in data['results'] if r['status'] == status] == [
            f'{status}-SZ', f'{status}-C', f'{status}-SA',
        ]


def test_aliases_global_search_rejected_and_sections(browse):
    row('A', source_vocabulary_id='ICD10CM', occurrence_count=50)
    row('B', status='approved')
    row('C', status='rejected')
    row('D', source_vocabulary_id='LOINC', source_code_description='distinctive phrase')
    row('E', origin_system='athena', status='approved')
    data = browse(source='ICD10').data
    # Rejected rows now appear in their own section, always visible.
    assert {r['source_code'] for r in data['results']} == {'A', 'B', 'C', 'E'}
    assert data['rejected_count'] == 1
    assert data['pages']['Rejected']['total'] == 1
    assert [r['source_code'] for r in browse(source='ICD10', search='distinctive').data['results']] == ['D']
    assert browse(source='').data['results'] == []


def test_duplicates_include_off_page_and_rejected_members(browse):
    row('MATCH', occurrence_count=0)
    row(' match ', status='rejected')
    for i in range(101):
        row(f'X{i}', occurrence_count=100)
    data = browse(search='X').data
    assert {r['source_code'] for r in data['duplicates']} == {'MATCH', ' match '}
    assert len(data['results']) == 100


@pytest.mark.parametrize('source', ['OpenWearables', '__overall__'])
def test_same_code_in_different_vocabularies_on_one_tab_is_not_a_duplicate(browse, source):
    # Wearables consolidates three vocabularies; ingest resolves on the exact one.
    for vocabulary in ('Apple', 'Garmin', 'OpenWearables'):
        row('heart_rate', source_vocabulary_id=vocabulary)
    row('A02.0')
    row('A02.0', source_vocabulary_id='ICD10CM')
    assert browse(source=source).data['duplicates'] == []
    assert browse(source='ICD10').data['duplicates'] == []


def test_duplicate_within_one_vocabulary_on_a_shared_tab_is_still_reported(browse):
    row('steps', source_vocabulary_id='Garmin')
    row(' STEPS ', source_vocabulary_id='Garmin', status='rejected')
    row('steps', source_vocabulary_id='Apple')
    data = browse(source='OpenWearables').data
    assert {(r['source_vocabulary_id'], r['source_code']) for r in data['duplicates']} == {
        ('Garmin', 'steps'), ('Garmin', ' STEPS ')}


def test_oid_spelling_of_a_vocabulary_is_the_same_vocabulary(browse):
    row('123', source_vocabulary_id='SNOMED')
    row('123', source_vocabulary_id='urn:oid:2.16.840.1.113883.6.96')
    assert len(browse(source='SNOMED').data['duplicates']) == 2


def test_bad_paging_or_sort_is_a_validation_error(browse):
    assert browse(page_0='bad').status_code == 400
    assert browse(order_0='password').status_code == 400


@pytest.mark.parametrize('search', ['', 'CODE'])
def test_combined_counts_preserve_all_sections_and_rejections(browse, search):
    row('CODE-A', source_vocabulary_id='ICD10CM')
    row('CODE-B', status='approved')
    row('CODE-C', status='rejected')
    row('CODE-D', source_vocabulary_id='LOINC', status='approved')
    row('CODE-E', origin_system='athena', status='approved')
    row('CODE-F', origin_system='athena', status='rejected')
    data = browse(source='ICD10CM', search=search).data
    # Rejected rows always appear in their own section now.
    assert data['pages']['Unmapped']['total'] == 1
    assert data['pages']['Mapped']['total'] == (2 if search else 1)
    assert data['pages']['Rejected']['total'] == 1
    assert data['pages']['Athena Mapped']['total'] == 2
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


# ── DELETE clears destination instead of removing ──────────────────────


@pytest.fixture
def staff_user():
    return Identity.objects.create_user(email='staff-delete@example.test', is_staff=True)


def _delete(user, mapping_id):
    request = APIRequestFactory().delete(f'/api/v1/code-mappings/{mapping_id}/')
    force_authenticate(request, user=user)
    return code_mapping_detail(request, mapping_id=mapping_id)


def test_delete_with_destination_clears_instead_of_removing(staff_user):
    """DELETE on a mapping with a destination clears the destination, keeping the row."""
    concept = ConceptFactory(concept_id=999999, concept_name='Test Concept', concept_code='12345')
    mapping = row('TEST-CODE', status='approved', target_concept=concept,
                  destination_vocabulary_id='SNOMED')
    MappingDestinationCandidate.objects.create(
        mapping=mapping, target_vocabulary_id='SNOMED',
        target_concept_code='12345', target_concept=concept,
    )
    response = _delete(staff_user, mapping.id)
    assert response.status_code == 200
    mapping.refresh_from_db()
    assert mapping.target_concept_id is None
    assert mapping.status == 'proposed'
    assert mapping.destination_vocabulary_id == ''
    assert mapping.reviewer_id is None
    assert mapping.suggestion_model_version == ''
    assert mapping.last_suggest_attempt == ''
    assert not MappingDestinationCandidate.objects.filter(mapping=mapping).exists()


def test_delete_without_destination_truly_deletes(staff_user):
    """DELETE on a proposed mapping with no destination removes the row."""
    mapping = row('JUNK-CODE')
    response = _delete(staff_user, mapping.id)
    assert response.status_code == 204
    assert not SourceCodeConceptMapping.objects.filter(id=mapping.id).exists()


def test_delete_rejected_mapping_clears_destination(staff_user):
    """DELETE on a rejected mapping with a destination clears it."""
    concept = ConceptFactory(concept_id=999998, concept_name='Rejected Concept', concept_code='99998')
    mapping = row('REJECTED-CODE', status='rejected', target_concept=concept,
                  destination_vocabulary_id='SNOMED')
    response = _delete(staff_user, mapping.id)
    assert response.status_code == 200
    mapping.refresh_from_db()
    assert mapping.target_concept_id is None
    assert mapping.status == 'proposed'
