"""Ship publisher terminology to every instance, including HealthTree GCP.

Frozen data migration: uses historical models and repository-contained payloads,
never a live management command, network download, credential or environment flag.
Do not modify the payload directory after release; add a new migration instead.
Each catalog commits atomically. If the second load fails, retry safely reuses
the first. Reverse is a no-op: removing reference content can break later work.
"""
import gzip
import hashlib
import json
from pathlib import Path

from django.db import migrations, transaction
from django.utils import timezone

SNAPSHOT_DIR = Path(__file__).resolve().parent.parent / 'data' / 'source_catalog_20260920'
MANIFEST_SHA256 = '820aeb56963dca45e4fcfdbe7179d18e28e714dbac93a375f38692a83683ed8e'
FIELDS = ('code', 'name', 'definition', 'synonyms', 'parents', 'semantic_types',
          'status', 'retired', 'metadata')


def _digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def load_catalogs(apps, schema_editor):
    manifest_path = SNAPSHOT_DIR / 'manifest.json'
    if _digest(manifest_path) != MANIFEST_SHA256:
        raise ValueError('Publisher source catalog manifest checksum mismatch.')
    manifest = json.loads(manifest_path.read_text())
    # Validate every artifact before changing either catalog. A missing/corrupt
    # image artifact must fail deployment, not silently mark the load applied.
    for entry in manifest['catalogs']:
        if _digest(SNAPSHOT_DIR / entry['filename']) != entry['sha256']:
            raise ValueError(f"Publisher source catalog checksum mismatch: {entry['vocabulary_id']}")
    alias = schema_editor.connection.alias
    Vocabulary = apps.get_model('omop_core', 'SourceVocabulary')
    Term = apps.get_model('omop_core', 'SourceVocabularyTerm')
    vocabularies = Vocabulary.objects.using(alias)
    terms = Term.objects.using(alias)
    for entry in manifest['catalogs']:
        vocabulary_id = entry['vocabulary_id']
        with transaction.atomic(using=alias):
            vocabulary = vocabularies.select_for_update().filter(pk=vocabulary_id).first()
            if (vocabulary and vocabulary.archive_sha256 == entry['sha256']
                    and vocabulary.term_count == entry['term_count']
                    and terms.filter(vocabulary_id=vocabulary_id).count() == entry['term_count']):
                continue
            vocabularies.update_or_create(pk=vocabulary_id, defaults={
                'name': entry['name'], 'release_version': entry['release_version'],
                'source_url': entry['source_url'], 'archive_sha256': entry['sha256'],
                'term_count': entry['term_count'], 'loaded_at': timezone.now(),
            })
            terms.filter(vocabulary_id=vocabulary_id).delete()
            batch, count = [], 0
            with gzip.open(SNAPSHOT_DIR / entry['filename'], 'rt', encoding='utf-8') as stream:
                for line in stream:
                    record = json.loads(line)
                    batch.append(Term(
                        vocabulary_id=vocabulary_id,
                        **{field: record[field] for field in FIELDS},
                        search_text='\n'.join([record['code'], record['name'], *record['synonyms']]),
                    ))
                    count += 1
                    if len(batch) == 500:
                        terms.bulk_create(batch, batch_size=500)
                        batch.clear()
                if batch:
                    terms.bulk_create(batch, batch_size=500)
            if count != entry['term_count']:
                raise ValueError(f'Publisher source catalog row count mismatch: {vocabulary_id}')
    with schema_editor.connection.cursor() as cursor:
        cursor.execute('ANALYZE source_vocabulary_term')


class Migration(migrations.Migration):

    atomic = False

    dependencies = [
        ('omop_core', '0250_source_vocabulary_catalog'),
    ]

    operations = [
        migrations.RunPython(load_catalogs, migrations.RunPython.noop),
    ]
