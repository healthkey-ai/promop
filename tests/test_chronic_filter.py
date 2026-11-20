"""#2512: the Other diagnoses list keeps long-term conditions (AHRQ's CCIR, then
HealthTree's overrides) and lists the rest apart with the reason."""
from datetime import date

import pytest

from omop_core.models import (
    ChronicConditionIndicator, ChronicConditionOverride, ConceptAncestor, ConceptRelationship, Relationship,
)
from omop_core.services.chronic_conditions import (
    CCIR_CSV, NO_CODE, chronic_filter, condition_icd10cm, load_indicators, read_ccir,
)
from tests.factories import ConceptFactory, PatientRecordFactory, VocabularyFactory
from tests.test_phr_read_model import condition, icd10, signed_in

pytestmark = pytest.mark.django_db

URL = '/api/v1/phr/diagnoses/'


@pytest.fixture(autouse=True)
def _inline_derivation(settings):
    settings.CELERY_BROKER_URL = ''


def indicators(**by_code):
    for code, value in by_code.items():
        ChronicConditionIndicator.objects.create(code=code.replace('_', '.'), indicator=value)


def listed(record):
    data = signed_in(record).get(URL).data
    return data, {c['name']: c['reason'] for c in data['other_hidden']}


# ---------------------------------------------------------------- the loader

CSV = (
    'Chronic Condition Indicator Refined v2027.1,,,,,,,\r\n'
    '"(0=Not chronic, 1=Chronic, and 9=No determination)",,,,,,,\r\n'
    "'ICD-10-CM CODE','ICD-10-CM CODE DESCRIPTION','CHRONIC INDICATOR',,,,, \r\n"
    "'E039',\"Hypothyroidism, unspecified\",1,,,,,\r\n"
    "'M8580',\"Other disorders of bone density, unspecified site\",0,,,,,\r\n"
    "'Z9481',Bone marrow transplant status,9,,,,,\r\n"
    "'XX',Not a code,1,,,,,\r\n"
)


def test_the_loader_reads_the_header_quotes_and_missing_decimal(tmp_path):
    path = tmp_path / 'ccir.csv'
    path.write_bytes(CSV.encode())

    release, rows = read_ccir(path)

    assert release == 'v2027.1'
    assert rows == {
        'E03.9': (1, 'Hypothyroidism, unspecified'),
        'M85.80': (0, 'Other disorders of bone density, unspecified site'),
        'Z94.81': (9, 'Bone marrow transplant status'),
    }


def test_loading_twice_changes_nothing_and_a_reload_makes_the_table_match_the_file(tmp_path):
    path = tmp_path / 'ccir.csv'
    path.write_bytes(CSV.encode())

    assert load_indicators(ChronicConditionIndicator, path) == {'created': 3, 'updated': 0, 'removed': 0, 'total': 3}
    assert load_indicators(ChronicConditionIndicator, path) == {'created': 0, 'updated': 0, 'removed': 0, 'total': 3}

    path.write_bytes(CSV.replace("'M8580'", "'M8581'").replace(',1,,,,,\r\n\'M', ',0,,,,,\r\n\'M').encode())
    counts = load_indicators(ChronicConditionIndicator, path)

    assert counts['created'] == 1 and counts['removed'] == 1 and counts['updated'] == 1
    assert set(ChronicConditionIndicator.objects.values_list('code', flat=True)) == {'E03.9', 'M85.81', 'Z94.81'}


def test_the_bundled_release_reads_in_full():
    release, rows = read_ccir(CCIR_CSV)

    assert release == 'v2027.1' and len(rows) == 75915
    assert [rows[c][0] for c in ('E03.9', 'I10', 'M81.0', 'M85.80', 'Z94.81', 'C4A.0')] == [1, 1, 1, 0, 9, 1]
    counts = [sum(1 for v in rows.values() if v[0] == n) for n in (1, 0, 9)]
    assert counts == [12981, 52303, 10631]


# ---------------------------------------------------------------- the filter on the page

def test_only_chronic_conditions_stay_in_other_and_the_rest_say_why():
    indicators(I10=1, L70_0=0, R51_9=9)
    record = PatientRecordFactory(disease='')
    condition(record, icd10('I10', 'Hypertension'), '2020-01-01')
    condition(record, icd10('L70.0', 'Acne vulgaris'), '2020-02-01')
    condition(record, icd10('R51.9', 'Headache'), '2020-03-01')

    data, hidden = listed(record)

    assert [c['name'] for c in data['other']] == ['Hypertension']
    assert hidden == {'Acne vulgaris': 'not_chronic', 'Headache': 'not_chronic'}
    assert {c['reason_label'] for c in data['other_hidden']} == {'Not a long-term condition'}


def test_an_override_wins_over_the_indicator_in_both_directions():
    indicators(M85_80=0, I10=1)
    ChronicConditionOverride.objects.create(code='M85.8', is_chronic=True)
    ChronicConditionOverride.objects.create(code='I10', is_chronic=False)
    record = PatientRecordFactory(disease='')
    condition(record, icd10('M85.80', 'Osteopenia'), '2020-01-01')
    condition(record, icd10('I10', 'Hypertension'), '2020-02-01')

    data, hidden = listed(record)

    assert [c['name'] for c in data['other']] == ['Osteopenia']
    assert hidden == {'Hypertension': 'not_chronic'}


def test_nothing_is_judged_until_the_indicator_table_is_loaded():
    record = PatientRecordFactory(disease='')
    condition(record, icd10('L70.0', 'Acne vulgaris'), '2020-01-01')

    data, hidden = listed(record)

    assert [c['name'] for c in data['other']] == ['Acne vulgaris'] and hidden == {}


def test_a_code_the_table_lacks_takes_what_the_codes_under_it_agree_on():
    indicators(E11_9=1, E11_65=1, J30_1=0, J30_2=1)
    record = PatientRecordFactory(disease='')
    condition(record, icd10('E11', 'Type 2 diabetes mellitus'), '2020-01-01')
    condition(record, icd10('J30', 'Allergic rhinitis'), '2020-02-01')

    data, hidden = listed(record)

    assert [c['name'] for c in data['other']] == ['Type 2 diabetes mellitus']
    assert hidden == {'Allergic rhinitis': 'not_chronic'}


def test_the_source_text_is_read_as_a_code_when_the_concept_is_not_icd10cm():
    indicators(E03_9=1)
    record = PatientRecordFactory(disease='')
    condition(record, ConceptFactory(concept_name='Hypothyroidism'), '2020-01-01', condition_source_value='E039')

    data, _ = listed(record)

    assert [c['name'] for c in data['other']] == ['Hypothyroidism']


def snomed(concept_id, name):
    return ConceptFactory(concept_id=concept_id, concept_name=name, concept_code=str(concept_id),
                          vocabulary=VocabularyFactory(vocabulary_id='SNOMED', vocabulary_name='SNOMED'))


def map_back(snomed_concept, icd_concept):
    relationship, _ = Relationship.objects.get_or_create(
        relationship_id='Mapped from',
        defaults=dict(relationship_name='Mapped from', is_hierarchical=0, defines_ancestry=0,
                      reverse_relationship_id='Maps to', relationship_concept_id=0))
    ConceptRelationship.objects.create(concept_1=snomed_concept, concept_2=icd_concept, relationship=relationship,
                                       valid_start_date='2000-01-01', valid_end_date='2099-12-31')


def test_a_snomed_condition_is_judged_by_the_icd10cm_code_athena_maps_to_it():
    indicators(E03_9=1, L70_0=0)
    hypo, acne = snomed(40001, 'Hypothyroidism'), snomed(40002, 'Acne')
    map_back(hypo, icd10('E03.9', 'Hypothyroidism, unspecified'))
    map_back(acne, icd10('L70.0', 'Acne vulgaris'))
    record = PatientRecordFactory(disease='')
    condition(record, hypo, '2020-01-01')
    condition(record, acne, '2020-02-01')

    data, hidden = listed(record)

    assert [c['name'] for c in data['other']] == ['Hypothyroidism']
    assert hidden == {'Acne': 'not_chronic'}


def test_when_several_codes_map_to_one_concept_the_most_specific_is_used():
    thyroid = snomed(40003, 'Thyroid disorder')
    map_back(thyroid, icd10('E07', 'Other disorders of thyroid'))
    map_back(thyroid, icd10('E07.89', 'Other specified disorders of thyroid'))
    record = PatientRecordFactory(disease='')
    row = condition(record, thyroid, '2020-01-01')

    assert condition_icd10cm([row]) == {row.pk: 'E07.89'}


def test_a_condition_with_no_code_that_maps_anywhere_is_not_chronic():
    indicators(I10=1)
    record = PatientRecordFactory(disease='')
    condition(record, snomed(40004, 'Mystery disorder'), '2020-01-01')

    data, hidden = listed(record)

    assert data['other'] == [] and hidden == {'Mystery disorder': 'not_chronic'}


def test_a_condition_with_nothing_coded_is_no_code_and_the_patients_own_entry_is_kept():
    indicators(I10=1)
    record = PatientRecordFactory(disease='')
    row = condition(record, ConceptFactory(concept_id=0, concept_code='0', concept_name='No matching concept'),
                    '2020-01-01', condition_source_value='')

    reasons, _ = chronic_filter([row])
    assert reasons == {row.pk: NO_CODE}
    assert chronic_filter([row], self_reported={row.pk}) == ({}, {})


def test_the_more_specific_of_two_related_conditions_is_kept_with_the_earlier_date():
    indicators(E11_9=1, E11_65=1)
    general = snomed(40010, 'Diabetes mellitus type 2')
    specific = snomed(40011, 'Type 2 diabetes with hyperglycemia')
    map_back(general, icd10('E11.9', 'Type 2 diabetes without complications'))
    map_back(specific, icd10('E11.65', 'Type 2 diabetes with hyperglycemia'))
    ConceptAncestor.objects.create(ancestor_concept=general, descendant_concept=specific,
                                   min_levels_of_separation=1, max_levels_of_separation=1)
    record = PatientRecordFactory(disease='')
    condition(record, general, '2018-03-01')
    condition(record, specific, '2021-05-01')

    data, hidden = listed(record)

    assert [(c['name'], c['date']) for c in data['other']] == [('Type 2 diabetes with hyperglycemia', '2018-03-01')]
    assert hidden == {'Diabetes mellitus type 2': 'less_specific'}


def test_the_status_reasons_still_come_first_and_cancer_and_transplant_are_not_judged():
    indicators(I10=1)
    record = PatientRecordFactory(disease='')
    condition(record, icd10('I10', 'Hypertension'), '2020-01-01', condition_verification_status='refuted')
    condition(record, icd10('Z94.81', 'Bone marrow transplant status'), '2020-02-01')

    data, hidden = listed(record)

    assert hidden == {'Hypertension': 'refuted'}
    assert [c['name'] for c in data['historical_procedures']] == ['Bone marrow transplant status']
    assert date(2020, 1, 1)
