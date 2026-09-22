"""Authoritative evidence, fallback order, and preservation of curator decisions."""
import csv
from datetime import date
from io import StringIO
from unittest.mock import Mock
import zipfile

import pytest
import requests
from django.core.management import call_command
from django.utils import timezone

from omop_core.management.commands import scrape_unmapped_destinations as command
from omop_core.models import Concept, Domain, SourceCodeConceptMapping as Mapping, Vocabulary
from omop_core.services.athena_destinations import (
    ArchiveLookup, AthenaAPI, BrowserTransport, Evidence, LookupFailure, lookup_remote,
)
from omop_core.services.destination_recovery import apply_evidence, recoverable_mappings
from tests.factories import ConceptFactory, DomainFactory, VocabularyFactory

TODAY = date(2026, 9, 21)


def concept(cid=101, code='C91.10', vocabulary='ICD10CM', standard=''):
    return dict(concept_id=str(cid), concept_code=code, concept_name='Test diagnosis',
        vocabulary_id=vocabulary, domain_id='Condition', concept_class_id='Clinical Finding',
        standard_concept=standard, valid_start_date='19700101', valid_end_date='20991231', invalid_reason='')


def edge(source=101, target=201, **overrides):
    return dict(concept_id_1=str(source), concept_id_2=str(target), relationship_id='Maps to',
        valid_start_date='19700101', valid_end_date='20991231', invalid_reason='', **overrides)


def evidence(code='C91.10', target=201):
    return Evidence(concept(code=code), [concept(target, 'SNOMED-' + str(target), 'SNOMED', 'S')],
                    'found', 'https://athena.ohdsi.org/search-terms/terms/101')


def tsv(rows, fields=None):
    stream = StringIO()
    writer = csv.DictWriter(stream, fieldnames=fields or list(rows[0]), delimiter='\t')
    writer.writeheader()
    writer.writerows(rows)
    return stream.getvalue()


def export(tmp_path, sources=None, edges=None, *, zipped=False):
    sources = sources if sources is not None else [concept(), concept(201, 'SNOMED-201', 'SNOMED', 'S')]
    edges = edges if edges is not None else [edge()]
    files = {'CONCEPT.csv': tsv(sources, list(concept())),
             'CONCEPT_RELATIONSHIP.csv': tsv(edges, list(edge())),
             'DOMAIN.csv': tsv([dict(domain_id='Condition', domain_name='Condition', domain_concept_id=19)]),
             'VOCABULARY.csv': tsv([dict(vocabulary_id='SNOMED', vocabulary_name='SNOMED CT',
                 vocabulary_reference='https://snomed.org', vocabulary_version='test', vocabulary_concept_id=1)]),
             'CONCEPT_CLASS.csv': tsv([dict(concept_class_id='Clinical Finding',
                 concept_class_name='Clinical Finding', concept_class_concept_id=2)])}
    if zipped:
        path = tmp_path / 'athena.zip'
        with zipfile.ZipFile(path, 'w') as archive:
            for name, content in files.items():
                archive.writestr('export/' + name, content)
        return path
    tmp_path.mkdir(exist_ok=True)
    for name, content in files.items():
        (tmp_path / name).write_text(content)
    return tmp_path


def queue(code='C91.10', **kwargs):
    return Mapping.objects.create(source_vocabulary_id='ICD10CM', source_code=code, **kwargs)


def snapshot(row):
    return {key: getattr(row, key) for key in ('id', 'source_vocabulary_id', 'source_code', 'updated_at')}


def run(tmp_path, **options):
    report = tmp_path / 'report.csv'
    output = StringIO()
    call_command('scrape_unmapped_destinations', report=str(report), stdout=output,
                 skip_suggest_embeddings=True, **options)
    return list(csv.DictReader(report.open())), output.getvalue()


@pytest.mark.django_db
def test_local_export_imports_missing_concept_and_approves_without_network(tmp_path, monkeypatch):
    row = queue(notes='Keep this note', suggestion_model_version='v0.2', suggested_target_concept_id=999)
    download = Mock(side_effect=AssertionError('No Drive lookup needed'))
    monkeypatch.setattr(command, '_download_gdrive_vocabulary', download)
    api = Mock()
    monkeypatch.setattr(command, 'AthenaAPI', Mock(return_value=api))
    path = export(tmp_path / 'local')
    monkeypatch.setattr(command, 'DEFAULT_LOCAL_PATH', str(path))
    records, output = run(tmp_path)
    assert records[0]['tier'] == 'local directory'
    assert records[0]['attempts'] == 'local directory'
    assert records[0]['outcome'] == 'loaded'
    row.refresh_from_db()
    assert (row.status, row.target_concept_id, row.origin_system) == ('approved', 201, 'athena')
    assert row.omop_table == 'condition'
    assert row.suggested_target_concept_id == 999 and row.suggestion_model_version == 'v0.2'
    assert row.notes.startswith('Keep this note\n')
    assert row.source_concept_id is None
    target = Concept.objects.get(pk=201)
    assert target.vocabulary_id == 'SNOMED' and target.standard_concept == 'S'
    assert target.source is None
    assert 'found_local directory=1' in output
    api.lookup.assert_not_called()
    download.assert_not_called()
    records, _ = run(tmp_path, path=str(path))
    assert records == []


@pytest.mark.django_db
def test_readable_local_export_skips_drive_even_for_codes_absent_locally(tmp_path, monkeypatch):
    for code in ('LOCAL', 'API', 'WEB', 'MISSING'):
        queue(code)
    local = export(tmp_path / 'local', [concept(code='LOCAL'), concept(201, 'SNOMED-201', 'SNOMED', 'S')])
    drive = export(tmp_path, [concept(code='DRIVE'), concept(202, 'SNOMED-202', 'SNOMED', 'S')], [edge(target=202)], zipped=True)
    download = Mock(return_value=drive)
    monkeypatch.setattr(command, '_download_gdrive_vocabulary', download)
    api, web = Mock(), Mock()
    api.lookup.side_effect = lambda vocab, code, today: evidence(code, 203) if code == 'API' else Evidence()
    web.lookup.side_effect = lambda vocab, code, today: evidence(code, 204) if code == 'WEB' else Evidence()
    monkeypatch.setattr(command, 'AthenaAPI', Mock(return_value=api))
    monkeypatch.setattr(command, 'AthenaBrowser', Mock(return_value=web))
    records, output = run(tmp_path, path=str(local))
    assert [r['tier'] for r in records] == ['local directory', 'API', 'web', '']
    assert [r['outcome'] for r in records] == ['loaded'] * 3 + ['unmapped']
    assert records[2]['attempts'] == 'local directory -> API -> web'
    assert [call.args[1] for call in api.lookup.call_args_list] == ['API', 'WEB', 'MISSING']
    assert [call.args[1] for call in web.lookup.call_args_list] == ['WEB', 'MISSING']
    download.assert_not_called()
    assert 'loaded=3' in output and 'unmapped=1' in output


@pytest.mark.django_db
def test_missing_directory_falls_back_to_drive(tmp_path, monkeypatch):
    queue()
    drive = export(tmp_path, zipped=True)
    monkeypatch.setattr(command, '_download_gdrive_vocabulary', Mock(return_value=drive))
    records, _ = run(tmp_path, path=str(tmp_path / 'absent'))
    assert records[0]['tier'] == 'gdrive' and records[0]['outcome'] == 'loaded'
    assert 'local directory:' in records[0]['errors']


@pytest.mark.django_db
def test_dry_run_validates_but_writes_nothing(tmp_path):
    row = queue()
    before = snapshot(row)
    records, _ = run(tmp_path, path=str(export(tmp_path / 'local')), offline=True, dry_run=True)
    assert records[0]['outcome'] == 'would_load'
    row.refresh_from_db()
    assert snapshot(row) == before and row.target_concept_id is None
    assert not Concept.objects.filter(pk=201).exists()
    assert not Vocabulary.objects.filter(pk='SNOMED').exists()
    assert not Domain.objects.filter(pk='Condition').exists()


@pytest.mark.django_db
@pytest.mark.parametrize('changes', [dict(status='approved'), dict(status='rejected'),
    dict(reviewed_at=timezone.now()), dict(target_concept_id=987654, origin='curator')])
def test_preexisting_decisions_are_excluded(tmp_path, changes):
    row = queue(**changes)
    records, _ = run(tmp_path, path=str(export(tmp_path / 'local')), offline=True)
    if changes.get('status') in ('approved', 'rejected'):
        assert not records
    else:
        assert records[0]['outcome'] == 'preserved_existing'
    row.refresh_from_db()
    for key, value in changes.items():
        assert getattr(row, key) == value


@pytest.mark.django_db
def test_unreviewed_import_proposals_are_recoverable_including_existing_candidates():
    real = ConceptFactory()
    for code, target in [('null', None), ('zero', 0), ('orphan', 7654321), ('existing', real.pk)]:
        queue(code, target_concept_id=target, origin='import')
    assert set(recoverable_mappings().values_list('source_code', flat=True)) == {'null', 'zero', 'orphan', 'existing'}


@pytest.mark.django_db
def test_concurrent_curator_change_is_preserved_before_concept_creation(tmp_path):
    row = queue()
    before = snapshot(row)
    row.notes = 'Curator changed this while lookup ran'
    row.save()
    result = apply_evidence(before, evidence(), 'API', {}, TODAY)
    assert result == 'skipped_changed'
    assert not Concept.objects.filter(pk=201).exists()


@pytest.mark.django_db
@pytest.mark.parametrize('status', ['approved', 'rejected', 'proposed'])
def test_case_variant_mapping_requires_review_before_recovery(status):
    row = queue()
    queue('c91.10', status=status)
    assert apply_evidence(snapshot(row), evidence(), 'API', {}, TODAY) == 'skipped_existing_mapping'
    assert not Concept.objects.filter(pk=201).exists()


@pytest.mark.django_db
def test_multiple_athena_destinations_stay_proposed_for_curator_selection(tmp_path, monkeypatch):
    row = queue()
    path = export(tmp_path / 'local', [concept(), concept(201, 'A', 'SNOMED', 'S'),
        concept(202, 'B', 'SNOMED', 'S')], [edge(), edge(target=202)])
    download, api = Mock(), Mock()
    monkeypatch.setattr(command, '_download_gdrive_vocabulary', download)
    monkeypatch.setattr(command, 'AthenaAPI', Mock(return_value=api))
    records, _ = run(tmp_path, path=str(path))
    assert records[0]['reason'] == 'multiple_destinations'
    assert records[0]['outcome'] == 'loaded_multiple'
    assert records[0]['target_concept_ids'] == '201;202'
    api.lookup.assert_not_called()
    download.assert_not_called()
    row.refresh_from_db()
    assert row.target_concept_id is None
    assert row.status == 'proposed' and row.origin_system == 'athena-multiple'
    assert row.reviewed_at is None
    assert set(row.destination_candidates.values_list('target_concept_id', flat=True)) == {201, 202}


@pytest.mark.django_db
@pytest.mark.parametrize('changes', [dict(standard_concept=''), dict(invalid_reason='D'),
    dict(source='HealthKey'), dict(concept_code='DIFFERENT'), dict(valid_end_date='2000-01-01')])
def test_local_destination_conflicts_are_never_overwritten(tmp_path, changes):
    target = ConceptFactory(concept_id=201, concept_code='SNOMED-201',
        vocabulary=VocabularyFactory(vocabulary_id='SNOMED'), domain=DomainFactory(domain_id='Condition'))
    for key, value in changes.items():
        setattr(target, key, value)
    target.save()
    queue()
    records, _ = run(tmp_path, path=str(export(tmp_path / 'local')), offline=True)
    assert records[0]['outcome'] == 'unmapped'
    assert 'conflicts' in records[0]['reason']
    target.refresh_from_db()
    assert target.standard_concept == changes.get('standard_concept', 'S')


@pytest.mark.django_db
def test_api_missing_reference_metadata_is_reported_without_fake_rows(tmp_path, monkeypatch):
    queue()
    api = Mock()
    api.lookup.return_value = evidence()
    monkeypatch.setattr(command, 'AthenaAPI', Mock(return_value=api))
    records, _ = run(tmp_path, path=str(tmp_path / 'absent'), skip_download=True, no_web=True)
    assert records[0]['tier'] == 'API' and records[0]['outcome'] == 'unmapped'
    assert 'Missing reference metadata' in records[0]['reason']
    assert not Concept.objects.filter(pk=201).exists()


def test_archive_rejects_expired_reversed_nonstandard_and_wrong_vocabulary(tmp_path):
    sources = [concept(), concept(102, vocabulary='ICD10'), concept(201, 'OK', 'SNOMED', 'S'),
               concept(202, 'NONSTANDARD', 'SNOMED'), concept(203, 'EXPIRED', 'SNOMED', 'S')]
    sources[-1]['valid_end_date'] = '20200101'
    edges = [edge(), edge(target=202), edge(target=203), edge(target=204), edge(source=201, target=101)]
    edges[0]['valid_end_date'] = '20200101'
    sources.append(concept(204, 'REVERSE', 'SNOMED', 'S'))
    edges[3]['relationship_id'] = 'Mapped from'
    path = export(tmp_path, sources, edges)
    provider = ArchiveLookup(lambda name: (path / name).open(), {('ICD10CM', 'c91.10')}, TODAY, str(path))
    assert provider.lookup('ICD10CM', 'C91.10', TODAY).reason == 'not_found'
    assert provider.lookup('ICD10', 'C91.10', TODAY).source is None


def api_concept(row):
    return dict(id=int(row['concept_id']), name=row['concept_name'], domainId=row['domain_id'],
        vocabularyId=row['vocabulary_id'], conceptClassId=row['concept_class_id'],
        conceptCode=row['concept_code'], standardConcept='Standard' if row['standard_concept'] == 'S' else 'Non-standard',
        invalidReason='Valid', validStart='1970-01-01', validEnd='2099-12-31')


def remote_fixture():
    replies = {'': dict(content=[dict(id=101, code='C91.10', vocabulary='ICD10CM'),
        dict(id=999, code='C91.10', vocabulary='ICD10'), dict(id=888, code='C91.11', vocabulary='ICD10CM')], totalPages=1),
        '/101': api_concept(concept()), '/201': api_concept(concept(201, 'DEST', 'SNOMED', 'S')),
        '/101/relationships': dict(count=2, items=[dict(relationships=[
            dict(targetConceptId=201, relationshipId='Maps to'),
            dict(targetConceptId=888, relationshipId='Mapped from')])])}
    return replies


def test_api_only_uses_exact_identity_and_outgoing_maps_to():
    replies = remote_fixture()
    get = Mock(side_effect=lambda path, params=None: replies[path])
    result = lookup_remote(get, 'ICD10CM', 'C91.10', TODAY)
    assert result.reason == 'found' and result.targets[0]['concept_id'] == 201
    assert [call.args[0] for call in get.call_args_list] == ['', '/101', '/101/relationships', '/201']


def test_api_search_is_paginated():
    replies = remote_fixture()
    replies['']['totalPages'] = 2
    def get(path, params=None):
        if path == '' and params['page'] == 1:
            return dict(content=[], totalPages=2)
        return replies[path]
    result = lookup_remote(get, 'ICD10CM', 'C91.10', TODAY)
    assert result.reason == 'found'


@pytest.mark.parametrize('part, key, value', [('/101', 'conceptCode', 'DIFFERENT'),
    ('/201', 'id', 999), ('/101/relationships', 'count', 3), ('', 'totalPages', 101)])
def test_incomplete_or_changed_api_evidence_cannot_be_used(part, key, value):
    replies = remote_fixture()
    replies[part][key] = value
    with pytest.raises(LookupFailure):
        lookup_remote(lambda path, params=None: replies[path], 'ICD10CM', 'C91.10', TODAY)


@pytest.mark.parametrize('status', [429, 500])
def test_api_retries_transient_errors_with_timeout_and_rate_limit(status, monkeypatch):
    session = Mock()
    first = Mock(status_code=status, headers={'Retry-After': '0'})
    second = Mock(status_code=200)
    second.json.return_value = {'content': [], 'totalPages': 0}
    session.get.side_effect = [first, second]
    monkeypatch.setattr('omop_core.services.athena_destinations.time.sleep', Mock())
    api = AthenaAPI(session=session, interval=0, timeout=7, retries=1)
    assert api.lookup('ICD10CM', 'NONE', TODAY).reason == 'not_found'
    assert session.get.call_count == 2
    assert session.get.call_args.kwargs['timeout'] == 7


@pytest.mark.django_db
def test_unavailable_download_and_api_fall_back_to_web_and_report_errors(tmp_path, monkeypatch):
    queue()
    ConceptFactory(concept_id=201, concept_code='SNOMED-201',
        vocabulary=VocabularyFactory(vocabulary_id='SNOMED'), domain=DomainFactory(domain_id='Condition'))
    local = tmp_path / 'absent'
    monkeypatch.setattr(command, '_download_gdrive_vocabulary', Mock(side_effect=OSError('Drive unavailable')))
    api, web = Mock(), Mock()
    api.lookup.side_effect = requests.HTTPError('403 Forbidden')
    web.lookup.return_value = evidence()
    monkeypatch.setattr(command, 'AthenaAPI', Mock(return_value=api))
    monkeypatch.setattr(command, 'AthenaBrowser', Mock(return_value=web))
    records, _ = run(tmp_path, path=str(local))
    assert records[0]['tier'] == 'web' and records[0]['outcome'] == 'loaded'
    assert 'gdrive: Drive unavailable' in records[0]['errors']
    assert '403 Forbidden' in records[0]['errors']


def test_browser_navigates_public_ui_and_reads_its_response():
    page = Mock()
    response = Mock(status=200)
    response.json.return_value = {'count': 0, 'items': []}
    from unittest.mock import MagicMock
    context = MagicMock()
    context.__enter__.return_value.value = response
    page.expect_response.return_value = context
    result = BrowserTransport(page, 0).get('/101/relationships')
    assert result == {'count': 0, 'items': []}
    page.goto.assert_called_once_with('https://athena.ohdsi.org/search-terms/terms/101', wait_until='domcontentloaded')
    predicate = page.expect_response.call_args.args[0]
    assert predicate(Mock(url='https://athena.ohdsi.org/api/v1/concepts/101/relationships?std=false'))
    assert not predicate(Mock(url='https://athena.ohdsi.org/api/v1/concepts/101'))


@pytest.mark.django_db
def test_first_api_403_disables_it_for_remaining_codes_and_omits_report_noise(tmp_path, monkeypatch):
    for code in ('FIRST', 'SECOND', 'THIRD'):
        queue(code)
    local = export(tmp_path / 'local', sources=[], edges=[])
    api, web = Mock(), Mock()
    response = requests.Response()
    response.status_code = 403
    api.lookup.side_effect = requests.HTTPError('Forbidden', response=response)
    web.lookup.side_effect = lambda vocab, code, today: evidence(code)
    monkeypatch.setattr(command, 'AthenaAPI', Mock(return_value=api))
    monkeypatch.setattr(command, 'AthenaBrowser', Mock(return_value=web))
    records, output = run(tmp_path, path=str(local), skip_download=True)
    api.lookup.assert_called_once()
    assert web.lookup.call_count == 3
    assert output.count('disabling API lookups') == 1
    assert 'found_API' not in output
    for row in records:
        assert row['tier'] == 'web' and row['outcome'] == 'loaded'
        assert row['attempts'] == 'local directory -> web'
        assert 'API' not in row['errors'] and '403' not in row['errors']


@pytest.mark.django_db
def test_api_success_before_403_retains_provenance_and_count(tmp_path, monkeypatch):
    for code in ('SUCCESS', 'FORBIDDEN', 'LATER'):
        queue(code)
    local = export(tmp_path / 'local', sources=[], edges=[])
    api, web = Mock(), Mock()
    response = requests.Response()
    response.status_code = 403
    api.lookup.side_effect = [evidence('SUCCESS'), requests.HTTPError(response=response)]
    web.lookup.side_effect = lambda vocab, code, today: evidence(code)
    monkeypatch.setattr(command, 'AthenaAPI', Mock(return_value=api))
    monkeypatch.setattr(command, 'AthenaBrowser', Mock(return_value=web))
    records, output = run(tmp_path, path=str(local), skip_download=True)
    assert api.lookup.call_count == 2
    assert [r['tier'] for r in records] == ['API', 'web', 'web']
    assert 'found_API=1' in output


@pytest.mark.django_db
@pytest.mark.parametrize('status', [401, 429, 500])
def test_non_403_api_errors_do_not_disable_the_next_code(tmp_path, monkeypatch, status):
    queue('FIRST')
    queue('SECOND')
    local = export(tmp_path / 'local', sources=[], edges=[])
    api, web = Mock(), Mock()
    response = requests.Response()
    response.status_code = status
    api.lookup.side_effect = [requests.HTTPError(response=response), evidence('SECOND')]
    web.lookup.return_value = evidence('FIRST')
    monkeypatch.setattr(command, 'AthenaAPI', Mock(return_value=api))
    monkeypatch.setattr(command, 'AthenaBrowser', Mock(return_value=web))
    records, _ = run(tmp_path, path=str(local), skip_download=True)
    assert api.lookup.call_count == 2
    assert [r['tier'] for r in records] == ['web', 'API']


@pytest.mark.django_db
def test_bad_metadata_rolls_back_reference_and_mapping_changes(tmp_path):
    row = queue()
    path = export(tmp_path / 'local')
    (path / 'CONCEPT_CLASS.csv').unlink()
    records, _ = run(tmp_path, path=str(path), offline=True)
    assert records[0]['outcome'] == 'unmapped'
    assert 'Missing reference metadata' in records[0]['reason']
    assert not Domain.objects.filter(pk='Condition').exists()
    assert not Vocabulary.objects.filter(pk='SNOMED').exists()
    row.refresh_from_db()
    assert row.status == 'proposed' and row.target_concept_id is None


@pytest.mark.django_db
def test_vocabulary_filter_and_limit_are_applied_before_lookup(tmp_path):
    queue(status='none')
    queue('OTHER')
    Mapping.objects.create(source_vocabulary_id='ICD10', source_code='C91.10')
    records, _ = run(tmp_path, path=str(export(tmp_path / 'local')), offline=True,
                     vocabulary=['ICD10CM'], limit=1)
    assert len(records) == 1 and records[0]['vocabulary'] == 'ICD10CM'
    assert records[0]['outcome'] == 'loaded'
    assert Mapping.objects.filter(status='approved').count() == 1


@pytest.mark.django_db
def test_stdout_csv_is_not_polluted_by_summary(tmp_path):
    queue()
    out, err = StringIO(), StringIO()
    call_command('scrape_unmapped_destinations', path=str(export(tmp_path / 'local')),
        report='-', offline=True, dry_run=True, stdout=out, stderr=err, skip_suggest_embeddings=True)
    records = list(csv.DictReader(StringIO(out.getvalue())))
    assert len(records) == 1 and records[0]['outcome'] == 'would_load'
    assert 'Unapproved queue rows to audit: 1' in err.getvalue()
    assert 'would_load=1' in err.getvalue()


@pytest.mark.django_db
def test_report_path_is_checked_before_any_mapping_changes(tmp_path):
    row = queue()
    with pytest.raises(OSError):
        call_command('scrape_unmapped_destinations', path=str(export(tmp_path / 'local')),
            report=str(tmp_path / 'absent' / 'report.csv'), offline=True, skip_suggest_embeddings=True)
    row.refresh_from_db()
    assert row.target_concept_id is None


@pytest.mark.django_db
@pytest.mark.parametrize('field', ['reviewer_id', 'updated_by_id', 'locked_by_id'])
def test_reviewed_edited_and_locked_queue_rows_are_excluded(field):
    from patient_portal.models import Identity
    user = Identity.objects.create(issuer='local', sub='recovery-reviewer', uid='recovery-reviewer')
    row = queue(**{field: user.pk})
    assert not recoverable_mappings().filter(pk=row.pk).exists()


@pytest.mark.parametrize('part, key, value', [('', 'content', ['invalid']),
    ('', 'totalPages', True), ('/101/relationships', 'items', ['invalid']),
    ('/101/relationships', 'items', [{'relationships': ['invalid']}])])
def test_malformed_remote_records_fail_as_lookup_errors(part, key, value):
    replies = remote_fixture()
    replies[part][key] = value
    with pytest.raises(LookupFailure):
        lookup_remote(lambda path, params=None: replies[path], 'ICD10CM', 'C91.10', TODAY)


def test_browser_error_does_not_return_unverified_content():
    from unittest.mock import MagicMock
    page, context = Mock(), MagicMock()
    context.__enter__.return_value.value = Mock(status=403)
    page.expect_response.return_value = context
    with pytest.raises(LookupFailure, match='HTTP 403'):
        BrowserTransport(page, 0).get('/101')


@pytest.mark.django_db
def test_missing_browser_is_reported_and_command_finishes(tmp_path, monkeypatch):
    queue()
    api, browser = Mock(), Mock()
    api.lookup.return_value = Evidence()
    browser.lookup.side_effect = LookupFailure('Browser dependency unavailable')
    monkeypatch.setattr(command, 'AthenaAPI', Mock(return_value=api))
    monkeypatch.setattr(command, 'AthenaBrowser', Mock(return_value=browser))
    records, _ = run(tmp_path, path=str(tmp_path / 'absent'), skip_download=True)
    assert records[0]['reason'] == 'lookup_errors'
    assert records[0]['outcome'] == 'unmapped'
    assert 'web: LookupFailure: Browser dependency unavailable' in records[0]['errors']


@pytest.mark.django_db
def test_explicit_ht_one_vocabulary_correction_is_reported_and_used(tmp_path):
    row = Mapping.objects.create(source_vocabulary_id='ICD10', source_code='C91.10', origin='import')
    records, _ = run(tmp_path, path=str(export(tmp_path / 'local')), offline=True,
                     lookup_vocabulary=['ICD10=ICD10CM'])
    assert records[0]['lookup_vocabulary'] == 'ICD10CM'
    assert records[0]['vocabulary'] == 'ICD10' and records[0]['outcome'] == 'loaded'
    row.refresh_from_db()
    assert row.source_vocabulary_id == 'ICD10' and row.target_concept_id == 201
    assert 'ICD10CM:C91.10' in row.notes


@pytest.mark.django_db
def test_definitive_missing_counts_exclude_lookup_failures_and_proposals_are_audited(tmp_path, monkeypatch):
    for code in ('ABSENT', 'FAILED'):
        queue(code)
    # This existing curator destination is preserved but still counted in coverage.
    queue('FOUND', target_concept=ConceptFactory())
    local = export(tmp_path / 'local', sources=[], edges=[])
    api, web = Mock(), Mock()
    api.lookup.return_value = Evidence()
    def lookup(vocab, code, today):
        if code == 'FAILED': raise LookupFailure('Timed out')
        return evidence(code) if code == 'FOUND' else Evidence()
    web.lookup.side_effect = lookup
    monkeypatch.setattr(command, 'AthenaAPI', Mock(return_value=api))
    monkeypatch.setattr(command, 'AthenaBrowser', Mock(return_value=web))
    records, output = run(tmp_path, path=str(local))
    assert [r['coverage'] for r in records] == ['not_found_any_method', 'incomplete_lookup', 'found']
    assert records[2]['outcome'] == 'preserved_existing'
    assert 'checked=3, found=1, multiple_destinations=0, ambiguous=0, not_found_any_method=1, incomplete_lookup=1' in output


@pytest.mark.django_db
def test_offline_absence_does_not_claim_all_methods_checked(tmp_path):
    queue()
    records, output = run(tmp_path, path=str(export(tmp_path / 'local', sources=[], edges=[])), offline=True)
    assert records[0]['coverage'] == 'incomplete_lookup'
    assert 'not_found_any_method=0' in output
