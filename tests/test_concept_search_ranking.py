"""The curator concept search puts the concept in common use first (#2923).

The stub vocabulary mirrors LOINC and SNOMED as loaded on staging: the same
codes and names, so the cases that went wrong there are the cases asserted here.
"""
from datetime import date

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from rest_framework.request import Request
from rest_framework.test import APIClient, APIRequestFactory

from omop_core.api.concepts import (
    ConceptPagination,
    ConceptSearchPagination,
    ranked_concept_ordering,
    search_queryset,
)
from omop_core.models import (
    Concept,
    ConceptMappingPriority,
    FieldConceptMapping,
    SourceCodeConceptMapping,
    SuggestSynonymTerm,
    VocabularyRelease,
)
from django.utils import timezone

from patient_portal.models import Identity
from tests.factories import ConceptClassFactory, DomainFactory, VocabularyFactory

pytestmark = pytest.mark.django_db

URL = '/api/v1/concepts/search/'

PSA_DBS = (1761735, '100716-0', 'Prostate specific Ag [Mass/volume] in DBS')
PSA_SEMEN = (646824, '109230-3', 'Prostate specific Ag [Measurement] in Semen')
PSA_SERUM = (3013603, '2857-1', 'Prostate specific Ag [Mass/volume] in Serum or Plasma')
# The same length as 2857-1: only the code tells them apart.
PSA_SERUM_NEW = (646862, '110671-5', 'Prostate specific Ag [Measurement] in Serum or Plasma')
PSA_SERUM_MOLES = (3037249, '19197-3', 'Prostate specific Ag [Moles/volume] in Serum or Plasma')
PSA_SNOMED = (4272032, '63476009', 'Prostate specific antigen measurement')
TESTO_DIALYSIS = (3012699, '17685-9', 'Testosterone Free [Mass/volume] in Dialysis fluid')
TESTO_SERUM = (3016049, '2991-8', 'Testosterone Free [Mass/volume] in Serum or Plasma')
TESTO_SERUM_MOLES = (3018301, '14914-6', 'Testosterone Free [Moles/volume] in Serum or Plasma')


def reference_rows(vocabulary_id, domain_id, concept_class_id):
    """The rows a concept's foreign keys name; the database checks them at teardown."""
    VocabularyFactory(vocabulary_id=vocabulary_id, vocabulary_name=vocabulary_id)
    DomainFactory(domain_id=domain_id, domain_name=domain_id)
    ConceptClassFactory(concept_class_id=concept_class_id, concept_class_name=concept_class_id)


def make_concept(spec, vocabulary_id='LOINC', domain_id='Measurement', **extra):
    concept_id, code, name = spec
    reference_rows(vocabulary_id, domain_id, 'Lab Test')
    return Concept.objects.create(
        concept_id=concept_id, concept_code=code, concept_name=name,
        vocabulary_id=vocabulary_id, domain_id=domain_id, concept_class_id='Lab Test',
        standard_concept='S', valid_start_date=date(1970, 1, 1),
        valid_end_date=date(2099, 12, 31), **extra,
    )


def synonym(spec, term, domain_id='Measurement'):
    SuggestSynonymTerm.objects.create(concept_id=spec[0], domain_id=domain_id, term=term.upper())


@pytest.fixture()
def api():
    client = APIClient()
    client.force_authenticate(
        Identity.objects.create_user(email='curator@example.com', password='x', is_staff=True))
    return client


@pytest.fixture()
def vocabulary():
    for spec in (PSA_DBS, PSA_SEMEN, PSA_SERUM, PSA_SERUM_NEW, PSA_SERUM_MOLES,
                 TESTO_DIALYSIS, TESTO_SERUM, TESTO_SERUM_MOLES):
        make_concept(spec)
    make_concept(PSA_SNOMED, vocabulary_id='SNOMED')
    # LOINC says "Ag"; its synonyms and SNOMED say "antigen".
    synonym(PSA_SERUM, 'Prostate specific antigen, Blood')
    synonym(PSA_SERUM_NEW, 'Prostate specific antigen [Measurement], Serum or Plasma')
    synonym(PSA_DBS, 'Prostate specific antigen, Dried blood spot')
    synonym(PSA_SERUM, 'Antigen; Chemistry; PSA; Serum; Tumor marker')


def search(api, query, **params):
    response = api.get(URL, {'q': query, **params})
    assert response.status_code == 200, response.content
    return [row['concept_code'] for row in response.json()['results']]


def test_the_usual_specimen_comes_before_a_dried_blood_spot(api, vocabulary):
    codes = search(api, 'prostate specific Ag')
    assert codes[0] == '2857-1'
    assert codes.index('2857-1') < codes.index('100716-0')
    # Every Serum or Plasma sibling is ahead of the specimen variants.
    for serum in ('110671-5', '19197-3'):
        assert codes.index(serum) < codes.index('100716-0')
        assert codes.index(serum) < codes.index('109230-3')


def test_a_shorter_code_breaks_a_tie_between_equal_names(api, vocabulary):
    # "[Measurement]" and "[Mass/volume]" are the same length; 2857-1 is the older code.
    codes = search(api, 'prostate specific Ag')
    assert codes.index('2857-1') < codes.index('110671-5')


def test_free_testosterone_in_serum_comes_before_dialysis_fluid(api, vocabulary):
    codes = search(api, 'testosterone free')
    assert codes[0] == '2991-8'
    assert codes.index('2991-8') < codes.index('17685-9')
    assert codes.index('14914-6') < codes.index('17685-9')


def test_a_loinc_test_is_found_by_the_antigen_spelling_before_snomed(api, vocabulary):
    """LOINC writes "Ag", so only its synonym "Prostate specific antigen, Blood" matches."""
    codes = search(api, 'prostate specific antigen')
    assert '2857-1' in codes[:25]
    assert codes.index('2857-1') < codes.index('63476009')
    # Name and synonym matches are one list, each concept once.
    assert len(codes) == len(set(codes))


def test_a_concept_a_curator_chose_comes_first_then_one_a_mapping_reaches(api, vocabulary):
    order = search(api, 'testosterone free')
    assert order.index('2991-8') < order.index('14914-6')
    # An approved code mapping lifts the Moles test over the Mass one ...
    SourceCodeConceptMapping.objects.create(
        source_vocabulary_id='Epic', source_code='TF', source_code_description='Free testosterone',
        target_concept_id=TESTO_SERUM_MOLES[0], status='approved', origin='import',
        domain_id='Measurement', omop_table='measurement',
    )
    assert search(api, 'testosterone free')[0] == '14914-6'
    # ... and an approved field mapping lifts the Mass one over a code mapping's.
    FieldConceptMapping.objects.create(
        field_name='free_testosterone', concept_id=TESTO_SERUM[0], status='approved',
        omop_table='measurement',
    )
    assert search(api, 'testosterone free')[:2] == ['2991-8', '14914-6']


def test_a_concept_a_curator_ranked_counts_as_chosen(api, vocabulary):
    ConceptMappingPriority.objects.create(concept_id=PSA_SERUM_MOLES[0], position=1)
    codes = search(api, 'prostate specific Ag')
    assert codes[0] == '19197-3'


def test_a_proposed_or_rejected_mapping_does_not_count_as_use(api, vocabulary):
    for status in ('proposed', 'rejected'):
        SourceCodeConceptMapping.objects.create(
            source_vocabulary_id='Epic', source_code=f'TF-{status}', source_code_description='x',
            target_concept_id=TESTO_SERUM_MOLES[0], status=status, origin='import',
            domain_id='Measurement', omop_table='measurement',
        )
    assert search(api, 'testosterone free')[0] == '2991-8'


def test_use_does_not_beat_a_better_name_match(api, vocabulary):
    """A used concept only reorders concepts whose names match equally well."""
    exact = make_concept((9100001, 'X-1', 'Free testosterone'))
    FieldConceptMapping.objects.create(
        field_name='used_elsewhere', concept_id=TESTO_DIALYSIS[0], status='approved',
        omop_table='measurement',
    )
    codes = search(api, 'free testosterone')
    assert codes[0] == exact.concept_code


def test_an_exact_code_or_omop_id_still_ranks_first(api, vocabulary):
    assert search(api, '2857-1')[0] == '2857-1'
    assert search(api, '2991-8')[0] == '2991-8'
    assert search(api, str(TESTO_SERUM[0]))[0] == '2991-8'
    FieldConceptMapping.objects.create(
        field_name='psa_value', concept_id=PSA_SERUM_NEW[0], status='approved',
        omop_table='measurement',
    )
    assert search(api, '19197-3')[0] == '19197-3'


def test_naming_a_specimen_turns_the_specimen_preference_off(api, vocabulary):
    make_concept((9500001, 'U-1', 'Testosterone Free [Mass/volume] in Urine and Serum or Plasma'))
    make_concept((9500002, 'U-2', 'Testosterone Free [Mass/volume] in Urine'))
    # No specimen typed: the usual one leads, though its name is longer.
    plain = search(api, 'testosterone free')
    assert plain.index('U-1') < plain.index('U-2')
    # A specimen typed: the shorter name wins, as the preference no longer applies.
    typed = search(api, 'testosterone free urine')
    assert typed.index('U-2') < typed.index('U-1')


def test_synonyms_are_read_in_one_statement(api, vocabulary):
    with CaptureQueriesContext(connection) as small:
        search(api, 'prostate specific antigen')
    with CaptureQueriesContext(connection) as other:
        search(api, 'testosterone free', page_size=100)
    assert len(small) == len(other)
    assert sum('suggest_synonym_term' in q['sql'] for q in small.captured_queries) == 1


def test_a_domain_filter_narrows_the_synonyms_read(api, vocabulary):
    finding = make_concept((9300001, 'OBS-1', 'Prostate specific Ag finding'), domain_id='Observation')
    synonym((finding.concept_id,), 'Prostate specific antigen finding', domain_id='Observation')
    assert search(api, 'prostate specific antigen', domain_id='Observation') == ['OBS-1']
    assert 'OBS-1' not in search(api, 'prostate specific antigen', domain_id='Measurement')
    assert '2857-1' in search(api, 'prostate specific antigen', domain_id='Measurement')
    # A domain Suggest keeps no synonyms for reads none.
    with CaptureQueriesContext(connection) as queries:
        search(api, 'prostate specific antigen', domain_id='Device')
    assert not [q for q in queries.captured_queries if 'suggest_synonym_term' in q['sql']]


def synonym_statements(queries):
    return [q['sql'] for q in queries.captured_queries if 'suggest_synonym_term' in q['sql']]


def test_text_with_no_token_of_three_characters_reads_no_synonyms(api, vocabulary):
    """A trigram index cannot serve a shorter LIKE pattern; the read would scan a partition."""
    for text in ('t 4', 'b 12', 't(4;14)', 'il 6'):
        with CaptureQueriesContext(connection) as queries:
            search(api, text)
        assert not synonym_statements(queries), text
    with CaptureQueriesContext(connection) as queries:
        search(api, 'psa b 12')
    assert len(synonym_statements(queries)) == 1


def test_the_synonym_read_follows_the_callers_vocabulary_and_standing(api, vocabulary):
    with CaptureQueriesContext(connection) as queries:
        search(api, 'prostate specific antigen', vocabulary_id='LOINC')
    assert 'vocabulary_id' in synonym_statements(queries)[0]
    # Synonyms belong to standard concepts, so a non-standard filter reads none.
    with CaptureQueriesContext(connection) as queries:
        search(api, 'prostate specific antigen', standard_concept='C')
    assert not synonym_statements(queries)
    # The other vocabulary's synonyms cannot crowd the asked-for one out of the cap.
    assert '2857-1' in search(api, 'prostate specific antigen', vocabulary_id='LOINC')
    assert search(api, 'prostate specific antigen', vocabulary_id='SNOMED') == ['63476009']


def test_the_etag_follows_what_the_ranking_reads(api, vocabulary):
    VocabularyRelease.objects.create(
        build_timestamp=timezone.now(), status='published', published_at=timezone.now(),
        scope=['LOINC'], vocab_versions={'LOINC': '2.78'}, row_counts={'concept': 1},
        checksums={'concept': {'count': 1}})
    first = api.get(URL, {'q': 'testosterone free'})
    assert first.status_code == 200 and first['ETag']
    again = api.get(URL, {'q': 'testosterone free'}, HTTP_IF_NONE_MATCH=first['ETag'])
    assert again.status_code == 304
    ConceptMappingPriority.objects.create(concept_id=TESTO_SERUM_MOLES[0], position=1)
    after = api.get(URL, {'q': 'testosterone free'}, HTTP_IF_NONE_MATCH=first['ETag'])
    assert after.status_code == 200
    assert after['ETag'] != first['ETag']
    assert after.json()['results'][0]['concept_code'] == '14914-6'
    # An approved code mapping counts, and so does deleting it.
    mapping = SourceCodeConceptMapping.objects.create(
        source_vocabulary_id='Epic', source_code='TF2', source_code_description='x',
        target_concept_id=TESTO_SERUM[0], status='approved', origin='import',
        domain_id='Measurement', omop_table='measurement',
    )
    approved = api.get(URL, {'q': 'testosterone free'})
    assert approved['ETag'] != after['ETag']
    mapping.delete()
    assert api.get(URL, {'q': 'testosterone free'})['ETag'] != approved['ETag']


# ---- plain-language searches (the demo's cases) ----------------------------------------

TESTO_TOTAL = (3008893, '2986-8', 'Testosterone [Mass/volume] in Serum or Plasma')
GRADE_GROUP_QUESTION = (36660206, '94734-1',
                        'Prostate cancer grade group [Score] in Prostate tumor Qualitative')
GRADE_ANSWERS = {
    1: (36662377, 'LA30794-4', 'ISUP Grade (Grade Group) 1 (Gleason score <=6)'),
    2: (36662565, 'LA30795-1', 'ISUP Grade (Grade Group) 2 (Gleason score 3+4=7)'),
    3: (36662476, 'LA30796-9', 'ISUP Grade (Grade Group) 3 (Gleason score 4+3=7)'),
    4: (36662300, 'LA30797-7', 'ISUP Grade (Grade Group) 4 (Gleason score 8)'),
}
GLEASON_SNOMED = {n: (3655243 + n, f'86074{n + 1}00{n}', f'Gleason grade group {n}')
                  for n in range(1, 5)}


@pytest.fixture()
def plain_language(vocabulary):
    """What else a curator's plain words match on staging: a HealthKey lab, an ingredient."""
    reference_rows('HK-Labs', 'Measurement', 'Lab Test')
    Concept.objects.create(
        concept_id=9400001, concept_code='hkl:psa', concept_name='PSA', vocabulary_id='HK-Labs',
        domain_id='Measurement', concept_class_id='Lab Test', standard_concept=None,
        source='HealthKey', valid_start_date=date(1970, 1, 1), valid_end_date=date(2099, 12, 31))
    synonym(PSA_SERUM, 'PSA SERPL-MCNC')
    make_concept(TESTO_TOTAL)
    synonym(TESTO_TOTAL, 'Testosterone, Blood')
    reference_rows('RxNorm', 'Drug', 'Ingredient')
    make_concept((9400002, '10379', 'testosterone'), vocabulary_id='RxNorm', domain_id='Drug')
    for spec in GRADE_ANSWERS.values():
        make_concept(spec, domain_id='Meas Value')
    for spec in GLEASON_SNOMED.values():
        make_concept(spec, vocabulary_id='SNOMED', domain_id='Observation')


@pytest.mark.parametrize('query, code, rank', [
    ('PSA', '2857-1', 3),
    ('prostate specific antigen', '2857-1', 1),
    ('prostate specific Ag', '2857-1', 1),
    ('testosterone', '2986-8', 3),
    ('free testosterone', '2991-8', 1),
    ('ISUP grade group 3', 'LA30796-9', 1),
    ('grade group 3', 'LA30796-9', 3),
    ('grade group 1', 'LA30794-4', 3),
    ('grade group 2', 'LA30795-1', 3),
    ('grade group 4', 'LA30797-7', 3),
])
def test_plain_language_finds_the_intended_concept_in_the_first_results(
        api, plain_language, query, code, rank):
    codes = search(api, query)
    assert code in codes[:rank], codes[:10]


# ---- the result limit ---------------------------------------------------------------

@pytest.fixture()
def many(db):
    reference_rows('SNOMED', 'Observation', 'Clinical Finding')
    Concept.objects.bulk_create([
        Concept(
            concept_id=9200000 + n, concept_code=f'LP-{n}', concept_name=f'Limit probe finding {n:03d}',
            vocabulary_id='SNOMED', domain_id='Observation', concept_class_id='Clinical Finding',
            standard_concept='S', valid_start_date=date(1970, 1, 1), valid_end_date=date(2099, 12, 31),
        ) for n in range(120)
    ])


def results(api, **params):
    response = api.get(URL, {'q': 'limit probe', **params})
    assert response.status_code == 200
    return response.json()


def test_limit_is_an_alias_of_page_size(api, many):
    assert len(results(api)['results']) == 25
    assert len(results(api, limit=8)['results']) == 8
    assert len(results(api, limit=50)['results']) == 50
    assert len(results(api, page_size=8)['results']) == 8


def test_limit_is_capped_and_page_size_wins(api, many):
    assert len(results(api, limit=500)['results']) == 100
    assert len(results(api, limit=50, page_size=10)['results']) == 10
    assert len(results(api, limit=50, page_size=500)['results']) == 100


@pytest.mark.parametrize('bad', ['0', '-3', 'abc', '', '2.5', '1_0', '\u0663'])
def test_an_unusable_limit_gets_the_default_page(api, many, bad):
    assert len(results(api, limit=bad)['results']) == 25


def test_an_unusable_page_size_is_not_replaced_by_limit(api, many):
    assert len(results(api, page_size=0, limit=8)['results']) == 25
    assert len(results(api, page_size='abc', limit=8)['results']) == 25


def test_the_envelope_is_unchanged_and_pages_follow_the_limit(api, many):
    body = results(api, limit=50)
    assert set(body) == {'count', 'next', 'previous', 'results'}
    assert body['count'] == 120
    assert 'limit=50' in body['next']
    assert len(results(api, limit=50, page=3)['results']) == 20


def test_other_concept_endpoints_ignore_limit():
    request = Request(APIRequestFactory().get('/x', {'limit': '8'}))
    assert ConceptPagination().get_page_size(request) == 25
    assert ConceptSearchPagination().get_page_size(request) == 8


# ---- what must not change -----------------------------------------------------------

def test_the_embedders_search_keeps_its_own_ordering(vocabulary):
    """The drug and regimen search shares the filters and the match tiers, not the new ranking."""
    queryset, _ = search_queryset('prostate specific Ag', {})
    codes = list(queryset.values_list('concept_code', flat=True))
    # Name length alone, as before: the DBS variant is shorter than the serum one.
    assert codes.index('100716-0') < codes.index('2857-1')
    # And no synonym recall: "antigen" does not reach LOINC's "Ag".
    queryset, _ = search_queryset('prostate specific antigen', {})
    assert '2857-1' not in set(queryset.values_list('concept_code', flat=True))
