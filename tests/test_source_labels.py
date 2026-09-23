"""The label grouping key: the rule, and that the database agrees with Python.

The rule is frozen -- it is the identity a review group is built from, so
changing it re-keys every group. Each case below is a measurement from the
721,620-code HealthTree extract, not a preference.
"""
import pytest
from django.db import connection

from omop_core.models import SourceCodeConceptMapping
from omop_core.services.source_labels import normalise

pytestmark = pytest.mark.django_db


def row(code, description, **kwargs):
    return SourceCodeConceptMapping.objects.create(
        source_code=code, source_code_description=description,
        **{'source_vocabulary_id': 'EPIC', 'status': 'proposed', **kwargs})


def stored(mapping):
    mapping.refresh_from_db()
    return mapping.source_label_norm


# --- the rule ---------------------------------------------------------------

@pytest.mark.parametrize('label, expected', [
    ('Albumin', 'albumin'),
    ('ALBUMIN', 'albumin'),
    ('  Albumin  ', 'albumin'),
])
def test_case_and_surrounding_space_do_not_separate_a_label(label, expected):
    assert normalise(label) == expected


@pytest.mark.parametrize('label, expected', [
    ('Neutrophils', 'neutrophils'),
    ('Neutrophils %', 'neutrophils%'),
    ('Neutrophils #', 'neutrophils#'),
    ('NRBC %', 'nrbc%'),
    ('NRBC #', 'nrbc#'),
])
def test_percent_and_hash_survive(label, expected):
    """A relative percentage and an absolute count are different LOINCs.

    Dropping these two characters merges 1,027 groups in the extract, and they
    are its largest analytes -- neutrophils, lymphocytes, monocytes, NRBC and
    immature granulocytes each arrive bare, as a percentage and as a count.
    """
    assert normalise(label) == expected


def test_percent_and_hash_forms_stay_three_distinct_keys():
    assert len({normalise('Neutrophils'), normalise('Neutrophils %'),
                normalise('Neutrophils #')}) == 3


@pytest.mark.parametrize('label, expected', [
    ('RDW (CV)', 'rdwcv'),
    ('RDW (SD)', 'rdwsd'),
    ('MCHC (calc)', 'mchccalc'),
    ('Comment (bed mobility)', 'commentbedmobility'),
])
def test_parenthesised_text_survives(label, expected):
    """Stripping (...) merges 4,343 groups and loses real distinctions.

    RDW (CV) and RDW (SD) are different LOINCs; both would become 'rdw'.
    """
    assert normalise(label) == expected


def test_parenthesised_variants_stay_apart():
    assert normalise('RDW (CV)') != normalise('RDW (SD)')
    assert normalise('RDW (CV)') != normalise('RDW')


@pytest.mark.parametrize('spellings, expected', [
    (['Gamma Globulin', 'Gammaglobulin'], 'gammaglobulin'),
    (['A/G Ratio', 'AG Ratio', 'AGRatio'], 'agratio'),
    (['e GFR', 'eGFR'], 'egfr'),
    (['M Spike', 'M-Spike', 'MSpike'], 'mspike'),
    (['ABO/Rh', 'ABO Rh', 'ABORh'], 'aborh'),
    (['Alpha 1 Globulin', 'Alpha1 Globulin'], 'alpha1globulin'),
    (['P-R Interval', 'PR Interval'], 'printerval'),
])
def test_punctuation_is_removed_not_spaced(spellings, expected):
    """Every one of the 25 largest merges this causes is one analyte.

    Replacing punctuation with a space keeps these apart instead, splitting a
    single analyte across several curator decisions.
    """
    assert {normalise(s) for s in spellings} == {expected}


@pytest.mark.parametrize('label', ['', '   ', '--', '()', '...', None])
def test_a_label_that_normalises_to_nothing_has_no_key(label):
    """NULL, not '' -- see test_rows_without_a_label_do_not_form_one_group."""
    assert normalise(label) is None


def test_a_description_at_the_storage_limit_has_no_key():
    """source_code_description is varchar(255), so a longer label is stored cut
    to fit and two different labels can share a prefix. On staging that merged
    28 groups over 76 codes -- distinct CPT procedures differing only past the
    cut, offered as one row a curator could map in a single click."""
    assert normalise('x' * 255) is None
    assert normalise('x' * 254) == 'x' * 254


def test_two_procedures_sharing_a_truncated_prefix_do_not_group():
    shared = 'Periodic comprehensive preventive medicine reevaluation ' * 5
    one, two = (shared + ' age 18-39')[:255], (shared + ' age 40-64')[:255]
    assert len(one) == len(two) == 255
    assert normalise(one) is None and normalise(two) is None


def test_distinct_analytes_are_not_merged():
    keys = {normalise(s) for s in
            ['Albumin', 'Globulin', 'Alpha 1 Globulin', 'Alpha 2 Globulin',
             'Gamma Globulin', 'Total Protein', 'A/G Ratio']}
    assert len(keys) == 7


# --- the database agrees with Python ----------------------------------------

LABELS = ['Albumin', 'ALBUMIN', '  Albumin  ', 'Neutrophils %', 'Neutrophils #',
          'RDW (CV)', 'RDW (SD)', 'Gamma Globulin', 'Gammaglobulin', 'A/G Ratio',
          'eGFR', 'M-Spike', 'ABO/Rh', 'Alpha 1 Globulin', '', '   ', '--', '()',
          'x' * 255, 'y' * 254]


def test_the_generated_column_matches_normalise_for_every_case():
    """The column is generated by the database, so the SQL and the Python are
    two statements of one rule. This is what stops them drifting."""
    for i, label in enumerate(LABELS):
        mapping = row(f'C{i:03}', label)
        assert stored(mapping) == normalise(label), f'disagreed on {label!r}'


def test_the_column_really_is_database_generated():
    with connection.cursor() as cursor:
        cursor.execute("""
            SELECT is_generated, generation_expression
            FROM information_schema.columns
            WHERE table_name = 'source_code_concept_mapping'
              AND column_name = 'source_label_norm'
        """)
        is_generated, expression = cursor.fetchone()
    assert is_generated == 'ALWAYS'
    assert 'regexp_replace' in expression


def test_editing_the_description_re_keys_the_row():
    mapping = row('C1', 'Albumin')
    assert stored(mapping) == 'albumin'
    mapping.source_code_description = 'Gamma Globulin'
    mapping.save()
    assert stored(mapping) == 'gammaglobulin'


def test_bulk_create_and_queryset_update_cannot_skip_it():
    """The paths a save() override or a signal would miss.

    omop_core/signals.py receivers do not fire for bulk_create, and
    queryset.update() bypasses save() entirely -- both are used on this table.
    """
    SourceCodeConceptMapping.objects.bulk_create([
        SourceCodeConceptMapping(source_vocabulary_id='EPIC', source_code='B1',
                                 source_code_description='Gamma Globulin', status='proposed'),
        SourceCodeConceptMapping(source_vocabulary_id='EPIC', source_code='B2',
                                 source_code_description='Gammaglobulin', status='proposed'),
    ])
    assert set(SourceCodeConceptMapping.objects
               .filter(source_code__in=['B1', 'B2'])
               .values_list('source_label_norm', flat=True)) == {'gammaglobulin'}

    SourceCodeConceptMapping.objects.filter(source_code='B1').update(
        source_code_description='NRBC %')
    assert SourceCodeConceptMapping.objects.get(source_code='B1').source_label_norm == 'nrbc%'


def test_the_column_is_read_only():
    """Postgres refuses a direct write, which is the guarantee."""
    from django.db import IntegrityError, ProgrammingError
    with pytest.raises((IntegrityError, ProgrammingError)):
        with connection.cursor() as cursor:
            cursor.execute(
                "INSERT INTO source_code_concept_mapping "
                "(source_vocabulary_id, source_code, source_code_description, "
                " source_label_norm, status, omop_table) "
                "VALUES ('EPIC', 'X1', 'Albumin', 'something-else', 'proposed', 'measurement')")


# --- what the key is for ----------------------------------------------------

def test_one_key_gathers_the_vendor_codes_that_share_a_label():
    """The point of the whole thing: albumin arrives under 2,557 codes."""
    for i, spelling in enumerate(['Albumin', 'ALBUMIN', 'albumin ', 'Albumin']):
        row(f'EPIC#{i}', spelling)
    from django.db.models import Count
    groups = dict(SourceCodeConceptMapping.objects
                  .exclude(source_label_norm=None)
                  .values_list('source_label_norm')
                  .annotate(n=Count('pk')))
    assert groups == {'albumin': 4}


def test_truncated_descriptions_do_not_group_in_the_database_either():
    long_one = ('Periodic comprehensive preventive medicine ' * 8)[:255]
    long_two = (('Periodic comprehensive preventive medicine ' * 8)[:250] + 'XXXXX')[:255]
    assert long_one[:250] == long_two[:250] and long_one != long_two
    a, b = row('CPT-A', long_one), row('CPT-B', long_two)
    assert stored(a) is None and stored(b) is None


def test_rows_without_a_label_do_not_form_one_group():
    """1,925 staging rows carry no description. Grouped together they would
    offer a curator one row standing for 1,925 unrelated codes, mappable in a
    single click. SQL groups NULLs, so the exclusion has to be explicit."""
    for i in range(5):
        row(f'BLANK{i}', ['', '   ', '--', '()', '...'][i])
    assert SourceCodeConceptMapping.objects.filter(source_label_norm=None).count() == 5
    assert not SourceCodeConceptMapping.objects.exclude(source_label_norm=None).exists()
