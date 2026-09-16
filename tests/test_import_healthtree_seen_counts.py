import csv
import json
from io import StringIO

import pytest
from django.core.management import call_command, CommandError
from omop_core.models import SourceCodeConceptMapping

pytestmark = pytest.mark.django_db


def snapshot(tmp_path, rows):
    path = tmp_path / 'counts.csv'
    with path.open('w', newline='', encoding='utf-8-sig') as out:
        writer = csv.writer(out)
        writer.writerow(['code', 'vocabulary', 'text', 'occurrences', 'resources'])
        writer.writerows(rows)
    return path


def run(path, **options):
    out = StringIO()
    call_command('import_healthtree_seen_counts', str(path), stdout=out, **options)
    return json.loads(out.getvalue())


def mapping(vocabulary='ICD10', code='C90.00', count=3, **kwargs):
    return SourceCodeConceptMapping.objects.create(source_vocabulary_id=vocabulary,
        source_code=code, occurrence_count=count, **kwargs)


def test_occurrences_replace_counts_and_reload_is_idempotent(tmp_path):
    row = mapping(status='approved', notes='Keep this review', origin_system='HT-One')
    untouched = mapping(code='UNKNOWN', count=12)
    before = SourceCodeConceptMapping.objects.values().get(pk=row.pk)
    path = snapshot(tmp_path, [('C90.00', 'ICD10CM', 'ignored text', 42, 17)])
    report = run(path)
    row.refresh_from_db()
    assert row.occurrence_count == 42
    after = SourceCodeConceptMapping.objects.values().get(pk=row.pk)
    before['occurrence_count'] = 42
    assert before == after
    untouched.refresh_from_db()
    assert untouched.occurrence_count == 12
    assert report['alias_matches'] == 1
    assert run(path)['changed_mappings'] == 0


def test_vocabulary_disambiguates_same_code_and_exact_match_wins(tmp_path):
    icd = mapping()
    cm = mapping('ICD10CM')
    other = mapping('SNOMED')
    path = snapshot(tmp_path, [('C90.00', 'ICD10', '', 10, 1), ('C90.00', 'ICD10CM', '', 20, 1)])
    assert run(path)['exact_matches'] == 2
    for row, expected in [(icd, 10), (cm, 20), (other, 3)]:
        row.refresh_from_db()
        assert row.occurrence_count == expected


def test_snomed_oid_and_uncoded_aliases(tmp_path):
    snomed = mapping('SNOMED', '123')
    uncoded = mapping('', 'source-text')
    path = snapshot(tmp_path, [('123', 'urn:oid:2.16.840.1.113883.6.96', '', 8, 1),
                               ('source-text', '(no system)', '', 0, 1)])
    assert run(path)['matched_mappings'] == 2
    snomed.refresh_from_db()
    uncoded.refresh_from_db()
    assert snomed.occurrence_count == 8
    assert uncoded.occurrence_count == 0


def test_unknown_codes_are_reported_without_creating_mappings(tmp_path):
    row = mapping()
    path = snapshot(tmp_path, [('OTHER', 'ICD10CM', '', 8, 1)])
    report = run(path)
    assert report['unmatched_input_codes'] == 1
    assert report['unmatched_mappings'] == 1
    assert SourceCodeConceptMapping.objects.count() == 1
    row.refresh_from_db()
    assert row.occurrence_count == 3


def test_dry_run_reports_without_writing(tmp_path):
    row = mapping()
    path = snapshot(tmp_path, [('C90.00', 'ICD10CM', '', 42, 1)])
    assert run(path, dry_run=True)['changed_mappings'] == 1
    row.refresh_from_db()
    assert row.occurrence_count == 3


@pytest.mark.parametrize('bad', ['-1', '1.5', '', 'NaN', '2147483648'])
def test_invalid_late_row_rolls_back_entire_input(tmp_path, bad):
    row = mapping()
    path = snapshot(tmp_path, [('C90.00', 'ICD10', '', 42, 1), ('BAD', 'ICD10', '', bad, 1)])
    with pytest.raises(CommandError, match='line 3'):
        run(path)
    row.refresh_from_db()
    assert row.occurrence_count == 3


def test_duplicate_identity_rejected_before_any_write(tmp_path):
    row = mapping()
    path = snapshot(tmp_path, [('C90.00', 'ICD10', '', 42, 1), ('C90.00', 'ICD10', '', 84, 1)])
    with pytest.raises(CommandError, match='Duplicate'):
        run(path)
    row.refresh_from_db()
    assert row.occurrence_count == 3


def test_required_header_validation(tmp_path):
    path = tmp_path / 'bad.csv'
    path.write_text('code,resources\nA,42\n')
    with pytest.raises(CommandError, match='occurrences'):
        run(path)


@pytest.mark.parametrize('vocabulary, alias', [
    ('CPT4', 'urn:oid:2.16.840.1.113883.6.12'),
    ('LOINC', 'http://loinc.org'),
    ('RxNorm', '2.16.840.1.113883.6.88'),
    ('ICD10', 'urn:oid:2.16.840.1.113883.6.90'),
    ('NDC', 'http://hl7.org/fhir/sid/ndc'),
])
def test_exact_standard_fhir_aliases(tmp_path, vocabulary, alias):
    row = mapping(vocabulary, '123')
    path = snapshot(tmp_path, [('123', alias, '', 42, 1)])
    assert run(path)['matched_mappings'] == 1
    row.refresh_from_db()
    assert row.occurrence_count == 42


def test_equivalent_fhir_alias_frequencies_are_combined(tmp_path):
    row = mapping('http://snomed.info/sct', '123')
    path = snapshot(tmp_path, [('123', 'SNOMED', '', 42, 1),
                               ('123', 'urn:oid:2.16.840.1.113883.6.96', '', 84, 1)])
    report = run(path)
    assert report['ambiguous_mappings'] == 0
    assert report['matched_mappings'] == 1
    row.refresh_from_db()
    assert row.occurrence_count == 126
    assert run(path)['changed_mappings'] == 0


def test_icd10_fallback_combines_equivalent_icd10cm_identifiers(tmp_path):
    row = mapping('ICD10', 'C90.00')
    path = snapshot(tmp_path, [('C90.00', 'ICD10CM', '', 42, 1),
                               ('C90.00', 'urn:oid:2.16.840.1.113883.6.90', '', 84, 1)])
    assert run(path)['matched_mappings'] == 1
    row.refresh_from_db()
    assert row.occurrence_count == 126


def test_whitespace_variants_are_combined_within_snapshot_not_across_reloads(tmp_path):
    row = mapping()
    path = snapshot(tmp_path, [('C90.00', 'ICD10', '', 42, 1), (' C90.00 ', 'ICD10', '', 8, 1)])
    run(path)
    row.refresh_from_db()
    assert row.occurrence_count == 50
    assert run(path)['changed_mappings'] == 0
