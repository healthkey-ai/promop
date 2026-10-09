"""Consumer verification of the exact bytes exported by the vocabulary API."""
import hashlib
from io import StringIO
import json
import time

import pytest
from django.db import connection
from rest_framework.test import APIClient

from omop_core.management.commands.load_athena_vocabularies import Command
from omop_core.models import (
    ConceptAncestor, ConceptRelationship, ConceptSynonym, DrugStrength,
    Relationship, SourceToConceptMap, Vocabulary, VocabularyRelease,
)
from omop_core.services import vocab_snapshot
from patient_portal.models import Identity
from tests.factories import ConceptFactory, VocabularyFactory

pytestmark = pytest.mark.django_db


def publish(tables):
    command = Command(stdout=StringIO())
    command._build_start = time.time()
    command._publish_release(dict.fromkeys(tables, 999999))
    return VocabularyRelease.objects.latest('pk')


@pytest.fixture
def client():
    result = APIClient()
    result.force_authenticate(Identity.objects.create_user(email='checksum@test.com', is_staff=True))
    return result


@pytest.fixture
def vocabulary_data():
    first = ConceptFactory(concept_id=9200002, concept_name='é漢字 "quoted"\nline\\end', source='HealthKey')
    second = ConceptFactory(concept_id=9200001, source=None)
    dates = {'valid_start_date': '1970-01-01', 'valid_end_date': '2099-12-31'}
    relationship = Relationship.objects.create(
        relationship_id='checksum-link', relationship_name='Checksum link',
        is_hierarchical=0, defines_ancestry=0, reverse_relationship_id='checksum-link',
        relationship_concept_id=0,
    )
    ConceptRelationship.objects.create(concept_1=first, concept_2=second, relationship=relationship, **dates)
    ConceptAncestor.objects.create(ancestor_concept=first, descendant_concept=second, min_levels_of_separation=1, max_levels_of_separation=1)
    ConceptSynonym.objects.create(concept=first, language_concept=second, concept_synonym_name='é漢字\nSynonym')
    DrugStrength.objects.create(drug_concept=first, ingredient_concept=second, amount_value=1.2345678901234567, numerator_value=1e-20, **dates)
    SourceToConceptMap.objects.create(source_code='source', source_concept=first, source_vocabulary_id='LOINC', target_concept=second, target_vocabulary_id='LOINC', **dates)
    return first, second


@pytest.mark.parametrize('table', vocab_snapshot.TABLE_COLUMNS)
def test_manifest_hash_matches_http_bytes(table, vocabulary_data, client):
    release = publish(vocab_snapshot.TABLE_COLUMNS)
    response = client.get(f'/api/v1/vocab-releases/{release.pk}/snapshot/{table}/')
    assert response.status_code == 200
    lines = b''.join(response.streaming_content).splitlines(keepends=True)
    sentinel = json.loads(lines[-1])
    data = b''.join(lines[:-1])
    checksum = release.checksums[table]
    assert checksum['algorithm'] == 'sha256'
    assert checksum['canonicalization'] == response['X-Vocab-Checksum-Format'] == 'promop-vocab-ndjson-v1'
    assert checksum['digest'] == hashlib.sha256(data).hexdigest()
    assert checksum['count'] == release.row_counts[table] == sentinel['rows'] == len(lines) - 1
    assert sentinel['__done'] is True
    assert checksum['count'] > 0
    assert checksum['digest'] != hashlib.sha256(b''.join(lines)).hexdigest()
    assert list(json.loads(lines[0])) == list(vocab_snapshot.TABLE_COLUMNS[table])


def test_pinned_bytes_unicode_null_and_key_order():
    VocabularyFactory(vocabulary_id='golden', vocabulary_name='é漢字 "Q"\n\\', vocabulary_reference=None, vocabulary_version=None)
    line = next(line for line in vocab_snapshot.iter_data_lines('vocabulary') if json.loads(line)['vocabulary_id'] == 'golden')
    expected = ('{"vocabulary_id":"golden","vocabulary_name":"é漢字 \\"Q\\"\\n\\\\",'
                '"vocabulary_reference":null,"vocabulary_version":null,"vocabulary_concept_id":0,'
                '"is_deprecated":false,"deprecated_date":null,"deprecated_reason":null}\n').encode('utf-8')
    assert line == expected


def test_order_is_key_order_not_insertion_or_locale(vocabulary_data):
    for key in ['zz', 'Zz', 'äz']:
        VocabularyFactory(vocabulary_id=key)
    keys = [json.loads(line)['vocabulary_id'] for line in vocab_snapshot.iter_data_lines('vocabulary')]
    assert keys == sorted(keys, key=lambda key: key.encode('utf-8'))
    ids = [json.loads(line)['concept_id'] for line in vocab_snapshot.iter_data_lines('concept')]
    assert ids == sorted(ids)


def test_physical_row_rewrite_does_not_change_hash():
    VocabularyFactory(vocabulary_id='rewrite')
    before = vocab_snapshot.table_checksum('vocabulary')
    with connection.cursor() as cursor:
        cursor.execute("SELECT ctid::text FROM vocabulary WHERE vocabulary_id = %s", ['rewrite'])
        old_ctid = cursor.fetchone()[0]
        cursor.execute("UPDATE vocabulary SET vocabulary_name = vocabulary_name WHERE vocabulary_id = %s RETURNING ctid::text", ['rewrite'])
        assert cursor.fetchone()[0] != old_ctid
    assert vocab_snapshot.table_checksum('vocabulary') == before


def test_same_count_content_corruption_is_detectable(client):
    VocabularyFactory(vocabulary_id='corrupt', vocabulary_name='before')
    release = publish(['vocabulary'])
    Vocabulary.objects.filter(pk='corrupt').update(vocabulary_name='after!')
    response = client.get(f'/api/v1/vocab-releases/{release.pk}/snapshot/vocabulary/')
    lines = b''.join(response.streaming_content).splitlines(keepends=True)
    assert json.loads(lines[-1])['rows'] == release.row_counts['vocabulary']
    assert hashlib.sha256(b''.join(lines[:-1])).hexdigest() != release.checksums['vocabulary']['digest']


def test_empty_table_hash_excludes_sentinel():
    SourceToConceptMap.objects.all().delete()
    checksum = vocab_snapshot.table_checksum('source_to_concept_map')
    assert checksum['count'] == 0
    assert checksum['digest'] == 'e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855'
    assert list(vocab_snapshot.stream_ndjson('source_to_concept_map')) == [b'{"__done": true, "rows": 0}\n']


def test_schema_addition_does_not_change_v1_bytes():
    VocabularyFactory(vocabulary_id='schema')
    before = vocab_snapshot.table_checksum('vocabulary')
    with connection.cursor() as cursor:
        cursor.execute("ALTER TABLE vocabulary ADD COLUMN future_metadata text DEFAULT 'new'")
    assert vocab_snapshot.table_checksum('vocabulary') == before


def test_session_numeric_and_date_settings_do_not_change_bytes(vocabulary_data):
    before = vocab_snapshot.table_checksum('drug_strength')
    with connection.cursor() as cursor:
        cursor.execute("SET LOCAL extra_float_digits = -3")
        cursor.execute("SET LOCAL DateStyle = 'SQL, DMY'")
    assert vocab_snapshot.table_checksum('drug_strength') == before
    row = json.loads(next(vocab_snapshot.iter_data_lines('drug_strength')))
    assert row['amount_value'] == 1.2345678901234567
    assert row['numerator_value'] == 1e-20
    assert row['valid_start_date'] == '1970-01-01'


def test_hash_failure_does_not_publish_partial_manifest(monkeypatch):
    previous = publish(['vocabulary'])
    original = vocab_snapshot.table_checksum
    def fail(table):
        if table == 'concept':
            raise RuntimeError('scan failed')
        return original(table)
    monkeypatch.setattr(vocab_snapshot, 'table_checksum', fail)
    with pytest.raises(RuntimeError, match='scan failed'):
        publish(['vocabulary', 'concept'])
    assert list(VocabularyRelease.objects.values_list('pk', flat=True)) == [previous.pk]


def test_unknown_table_is_rejected_before_sql():
    with pytest.raises(KeyError):
        list(vocab_snapshot.iter_data_lines('vocabulary; DROP TABLE concept'))


def test_filtered_snapshot_does_not_reuse_full_table_etag(vocabulary_data, client):
    release = publish(['concept'])
    url = f'/api/v1/vocab-releases/{release.pk}/snapshot/concept/'
    unfiltered = client.get(url)
    b''.join(unfiltered.streaming_content)
    filtered = client.get(url + '?source=HealthKey', HTTP_IF_NONE_MATCH=unfiltered['ETag'])
    assert filtered.status_code == 200
    assert filtered['ETag'] != unfiltered['ETag']
    lines = b''.join(filtered.streaming_content).splitlines(keepends=True)
    assert json.loads(lines[-1])['rows'] == 1
    assert hashlib.sha256(b''.join(lines[:-1])).hexdigest() != release.checksums['concept']['digest']
    unchanged = client.get(url + '?source=HealthKey', HTTP_IF_NONE_MATCH=filtered['ETag'])
    assert unchanged.status_code == 304


def test_loader_publishes_after_mapping_updates(monkeypatch, tmp_path):
    command = Command(stdout=StringIO())
    for method in (
        '_load_relationships', '_load_vocabularies', '_load_domains',
        '_load_concept_classes', '_load_concepts',
    ):
        monkeypatch.setattr(command, method, lambda dry_run: 0)
    for method in ('_seed_concept_zero', '_sync_cdm_source_metadata', '_load_raw_umls', '_record_version_history'):
        monkeypatch.setattr(command, method, lambda *args: None)
    monkeypatch.setattr(command, '_load_code_mappings', lambda verbosity: VocabularyFactory(vocabulary_id='post-load'))
    options = vars(command.create_parser('manage.py', 'load_athena_vocabularies').parse_args([
        '--path', str(tmp_path), '--concepts-only', '--skip-umls-cache',
        '--skip-clinical-vocabulary-verification',
    ]))
    command.handle(**options)
    release = VocabularyRelease.objects.latest('pk')
    assert 'post-load' in release.vocab_versions
    assert release.checksums['vocabulary'] == vocab_snapshot.table_checksum('vocabulary')


def test_stream_verification_across_cursor_batches(client):
    before = Vocabulary.objects.count()
    Vocabulary.objects.bulk_create([
        Vocabulary(vocabulary_id=f'batch-{i:04d}', vocabulary_name='Batch', vocabulary_concept_id=0)
        for i in range(1005)
    ])
    release = publish(['vocabulary'])
    response = client.get(f'/api/v1/vocab-releases/{release.pk}/snapshot/vocabulary/')
    lines = b''.join(response.streaming_content).splitlines(keepends=True)
    assert json.loads(lines[-1])['rows'] == before + 1005
    assert hashlib.sha256(b''.join(lines[:-1])).hexdigest() == release.checksums['vocabulary']['digest']
