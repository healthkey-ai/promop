"""Deployment must load complete catalogs offline and preserve curator work."""
import gzip
import hashlib
import importlib
import json
from types import SimpleNamespace

import pytest
from django.db import connection
from django.db.migrations.loader import MigrationLoader
from django.test import override_settings

from omop_core.models import SourceCodeConceptMapping, SourceVocabulary, SourceVocabularyTerm
from omop_core.services.source_release_files import mesh_records

migration = importlib.import_module('omop_core.migrations.0251_load_publisher_source_catalogs')


def fixture_snapshot(tmp_path, monkeypatch, *, expected_count=1):
    record = dict(code='C141394', name='RISS Stage I Multiple Myeloma', definition='Publisher definition',
                  synonyms=['R-ISS Stage I'], parents=['C141393'], semantic_types=['Neoplastic Process'],
                  status='', retired=False, metadata={})
    payload = tmp_path / 'ncit.jsonl.gz'
    payload.write_bytes(gzip.compress((json.dumps(record) + '\n').encode()))
    manifest = {'catalogs': [dict(vocabulary_id='NCIt', name='NCI Thesaurus', release_version='26.08e',
        filename=payload.name, sha256=hashlib.sha256(payload.read_bytes()).hexdigest(),
        term_count=expected_count, source_url='https://example.org/ncit.zip')]}
    manifest_path = tmp_path / 'manifest.json'
    manifest_path.write_text(json.dumps(manifest))
    monkeypatch.setattr(migration, 'SNAPSHOT_DIR', tmp_path)
    monkeypatch.setattr(migration, 'MANIFEST_SHA256', hashlib.sha256(manifest_path.read_bytes()).hexdigest())
    return payload


def run_migration():
    # Supply historical models, exactly as migrate does; never import the live
    # command from a migration. The connection alias comes from schema_editor.
    with override_settings(MIGRATION_MODULES={}):
        apps = MigrationLoader(connection).project_state([
            ('omop_core', '0250_source_vocabulary_catalog'),
        ]).apps
    migration.load_catalogs(apps, SimpleNamespace(connection=connection))


@pytest.mark.django_db
def test_offline_migration_is_idempotent_and_preserves_curator_work(tmp_path, monkeypatch):
    fixture_snapshot(tmp_path, monkeypatch)
    mapping = SourceCodeConceptMapping.objects.create(source_vocabulary_id='NCIt', source_code='C141394',
        source_code_description='Curator wording', occurrence_count=2011, status='approved', notes='Keep this')
    run_migration()
    term = SourceVocabularyTerm.objects.get(code='C141394')
    assert term.definition == 'Publisher definition'
    assert term.search_text == 'C141394\nRISS Stage I Multiple Myeloma\nR-ISS Stage I'
    loaded_at = SourceVocabulary.objects.get(pk='NCIt').loaded_at
    run_migration()
    assert SourceVocabulary.objects.get(pk='NCIt').loaded_at == loaded_at
    assert SourceVocabularyTerm.objects.get(code='C141394').pk == term.pk
    mapping.refresh_from_db()
    assert (mapping.source_code_description, mapping.occurrence_count, mapping.status, mapping.notes) == (
        'Curator wording', 2011, 'approved', 'Keep this')
    assert SourceCodeConceptMapping.objects.count() == 1


@pytest.mark.django_db
def test_corrupt_payload_fails_before_writing(tmp_path, monkeypatch):
    payload = fixture_snapshot(tmp_path, monkeypatch)
    payload.write_bytes(b'corrupted')
    with pytest.raises(ValueError, match='checksum mismatch'):
        run_migration()
    assert not SourceVocabulary.objects.exists()


@pytest.mark.django_db
def test_incomplete_reload_rolls_back_catalog_and_release(tmp_path, monkeypatch):
    fixture_snapshot(tmp_path, monkeypatch)
    run_migration()
    original = SourceVocabularyTerm.objects.get(code='C141394').pk
    fixture_snapshot(tmp_path, monkeypatch, expected_count=2)
    with pytest.raises(ValueError, match='row count mismatch'):
        run_migration()
    assert SourceVocabulary.objects.get(pk='NCIt').term_count == 1
    assert SourceVocabularyTerm.objects.get(code='C141394').pk == original


def test_packaged_artifacts_match_frozen_migration_manifest():
    directory = migration.SNAPSHOT_DIR
    assert migration._digest(directory / 'manifest.json') == migration.MANIFEST_SHA256
    manifest = json.loads((directory / 'manifest.json').read_text())
    expected = {'NCIt': 213083, 'MeSH': 355156}
    for catalog in manifest['catalogs']:
        path = directory / catalog['filename']
        assert migration._digest(path) == catalog['sha256']
        with gzip.open(path, 'rt') as stream:
            assert sum(1 for _ in stream) == catalog['term_count'] == expected[catalog['vocabulary_id']]


def test_build_rejects_unresolved_lfs_pointer(tmp_path):
    from scripts.verify_source_catalog_snapshots import verify
    directory = tmp_path / 'omop_core' / 'data' / 'source_catalog_test'
    directory.mkdir(parents=True)
    content = b'actual archive content'
    payload = directory / 'ncit.jsonl.gz'
    (directory / 'manifest.json').write_text(json.dumps({'catalogs': [{
        'filename': payload.name, 'sha256': hashlib.sha256(content).hexdigest(),
    }]}))
    payload.write_text('version https://git-lfs.github.com/spec/v1\noid sha256:example\nsize 22\n')
    with pytest.raises(ValueError, match='Fetch Git LFS'):
        verify(tmp_path)
    payload.write_bytes(content)
    verify(tmp_path)


def test_mesh_parser_keeps_substance_metadata_and_heading_semantics(tmp_path):
    descriptors = tmp_path / 'desc.gz'
    descriptors.write_bytes(gzip.compress(b'''<DescriptorRecordSet>
      <DescriptorRecord><DescriptorUI>D01</DescriptorUI><DescriptorName><String>Parent</String></DescriptorName>
        <TreeNumberList><TreeNumber>D01</TreeNumber></TreeNumberList></DescriptorRecord>
      <DescriptorRecord><DescriptorUI>D02</DescriptorUI><DescriptorName><String>Drug</String></DescriptorName>
        <TreeNumberList><TreeNumber>D01.123</TreeNumber></TreeNumberList>
        <ConceptList><Concept PreferredConceptYN="Y"><ScopeNote>Drug scope.</ScopeNote>
          <TermList><Term><String>Brand</String></Term></TermList>
        </Concept></ConceptList></DescriptorRecord></DescriptorRecordSet>'''))
    supplementary = tmp_path / 'supp.gz'
    supplementary.write_bytes(gzip.compress(b'''<SupplementalRecordSet><SupplementalRecord>
      <SupplementalRecordUI>C01</SupplementalRecordUI><SupplementalRecordName><String>Trial agent</String></SupplementalRecordName>
      <Note>Investigational compound.</Note><HeadingMappedToList><HeadingMappedTo><DescriptorReferredTo>
        <DescriptorUI>*D02</DescriptorUI><DescriptorName><String>Drug</String></DescriptorName>
      </DescriptorReferredTo></HeadingMappedTo></HeadingMappedToList>
      <ConceptList><Concept PreferredConceptYN="Y"><RegistryNumberList><RegistryNumber>ABC123</RegistryNumber>
      </RegistryNumberList><TermList><Term><String>Study alias</String></Term></TermList></Concept></ConceptList>
      </SupplementalRecord></SupplementalRecordSet>'''))
    parent, drug, agent = list(mesh_records(descriptors, supplementary))
    assert parent['parents'] == []
    assert drug['parents'] == ['D01']
    assert drug['definition'] == 'Drug scope.'
    assert drug['synonyms'] == ['Brand']
    assert agent['parents'] == []  # A mapped heading is not an asserted parent.
    assert agent['metadata']['mapped_headings'] == [{'code': 'D02', 'name': 'Drug'}]
    assert agent['metadata']['registry_numbers'] == ['ABC123']
    assert agent['definition'] == 'Investigational compound.'


def test_mesh_is_available_as_a_source_and_has_umls_bridge():
    from omop_core.services.source_vocabularies import source_systems_for, VOCAB_TO_UMLS_ROOT
    assert 'MeSH' in {v['vocabulary_id'] for v in source_systems_for('Drug')}
    assert VOCAB_TO_UMLS_ROOT['MeSH'] == 'MSH'
