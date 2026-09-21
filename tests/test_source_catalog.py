import zipfile
from io import StringIO
from unittest.mock import patch

import pytest
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import DatabaseError
from rest_framework.test import APIClient

from omop_core.models import Concept, SourceCodeConceptMapping, SourceVocabulary, SourceVocabularyTerm
from omop_core.services.source_catalog import catalog_response
from omop_core.services.source_descriptions import describe_source_code

pytestmark = pytest.mark.django_db
URL = 'https://evs.nci.nih.gov/ftp1/NCI_Thesaurus/Thesaurus_26.08e.FLAT.zip'


def archive(tmp_path, rows=None):
    rows = rows or [
        ['C141394', 'http://example.org/C141394', 'C141393', 'RISS Stage I Multiple Myeloma|R-ISS Stage I', 'Publisher staging definition.', '', '', 'Neoplastic Process', 'C116977'],
        ['C123', 'http://example.org/C123', '', 'Old Drug|Legacy name', 'Old definition', '', 'Retired_Concept', 'Pharmacologic Substance', ''],
    ]
    path = tmp_path / 'ncit.zip'
    with zipfile.ZipFile(path, 'w') as z:
        z.writestr('Thesaurus.txt', '\n'.join('\t'.join(row) for row in rows))
    return path


def load(path, **kwargs):
    call_command('load_ncit_source', archive=str(path), release_version='26.08e',
                 source_url=URL, stdout=StringIO(), **kwargs)


def test_loader_keeps_metadata_separate_and_preserves_mapping_counts(tmp_path):
    mapping = SourceCodeConceptMapping.objects.create(
        source_vocabulary_id='NCIt', source_code='C141394', source_code_description='Curator description',
        occurrence_count=17, status='approved', notes='Curator notes',
    )
    before_concepts = Concept.objects.count()
    path = archive(tmp_path)
    load(path)
    load(path)
    assert SourceVocabulary.objects.get(pk='NCIt').term_count == 2
    assert SourceVocabularyTerm.objects.count() == 2
    term = SourceVocabularyTerm.objects.get(code='C141394')
    assert term.name == 'RISS Stage I Multiple Myeloma'
    assert term.definition == 'Publisher staging definition.'
    assert term.synonyms == ['R-ISS Stage I']
    assert term.parents == ['C141393']
    assert term.semantic_types == ['Neoplastic Process']
    assert term.metadata['subsets'] == ['C116977']
    assert term.vocabulary.archive_sha256
    mapping.refresh_from_db()
    assert (mapping.source_code_description, mapping.occurrence_count, mapping.status, mapping.notes) == ('Curator description', 17, 'approved', 'Curator notes')
    assert SourceCodeConceptMapping.objects.count() == 1
    assert Concept.objects.count() == before_concepts
    assert describe_source_code('NCIt', 'C141394') == term.name


def test_dry_run_and_malformed_archive_do_not_write(tmp_path):
    load(archive(tmp_path), dry_run=True)
    assert not SourceVocabulary.objects.exists()
    with pytest.raises(CommandError, match='expected 9'):
        load(archive(tmp_path, [['C1', 'bad']]))
    assert not SourceVocabulary.objects.exists()


def test_failed_reload_preserves_previous_release(tmp_path):
    path = archive(tmp_path)
    load(path)
    before = list(SourceVocabularyTerm.objects.order_by('code').values('code', 'definition'))
    with patch.object(SourceVocabularyTerm.objects, 'bulk_create', side_effect=DatabaseError('test failure')):
        with pytest.raises(DatabaseError):
            load(path)
    assert list(SourceVocabularyTerm.objects.order_by('code').values('code', 'definition')) == before


def test_search_uses_synonyms_and_hides_retired_but_exact_lookup_reports_retirement(tmp_path):
    load(archive(tmp_path))
    data = catalog_response('NCIt', query='r-iss')
    assert [x['code'] for x in data['results']] == ['C141394']
    assert data['results'][0]['definition'] == 'Publisher staging definition.'
    assert catalog_response('NCIt', query='Legacy')['results'] == []
    assert catalog_response('NCIt', code='C123')['term']['retired'] is True
    assert len(catalog_response('NCIt', query='Legacy', include_retired=True)['results']) == 1
    assert catalog_response('NotLoaded')['available'] is False


def test_reload_replaces_removed_terms_without_changing_source_mapping(tmp_path):
    load(archive(tmp_path))
    load(archive(tmp_path, [['C456', 'http://example.org/C456', '', 'New term', '', '', '', 'Finding', '']]))
    assert list(SourceVocabularyTerm.objects.values_list('code', flat=True)) == ['C456']
    assert SourceVocabulary.objects.get(pk='NCIt').term_count == 1


def test_api_requires_mapping_access_and_returns_source_metadata(tmp_path):
    load(archive(tmp_path))
    client = APIClient()
    url = '/api/v1/code-mappings/source-catalog/'
    assert client.get(url, {'vocabulary_id': 'NCIt'}).status_code in (401, 403)
    user = get_user_model().objects.create_user(email='catalog@example.test', password='test-password')
    client.force_authenticate(user)
    assert client.get(url, {'vocabulary_id': 'NCIt'}).status_code == 403
    user.is_staff = True
    user.save()
    response = client.get(url, {'vocabulary_id': 'NCIt', 'q': 'r-iss'})
    assert response.status_code == 200
    assert response.data['results'][0]['code'] == 'C141394'
    assert client.get(url, {'vocabulary_id': 'NCIt', 'q': 'x'}).status_code == 400
    assert client.get(url, {'vocabulary_id': 'NCIt', 'q': 'x'*201}).status_code == 400


def test_ncit_available_for_drug_sources_and_umls_bridge():
    from omop_core.services.source_vocabularies import source_systems_for, VOCAB_TO_UMLS_ROOT
    assert 'NCIt' in {v['vocabulary_id'] for v in source_systems_for('Drug')}
    assert VOCAB_TO_UMLS_ROOT['NCIt'] == 'NCI'


def test_suggest_uses_publisher_name_when_athena_and_umls_lack_code(tmp_path):
    from omop_core.mapping.suggestions import _source_description
    load(archive(tmp_path))
    mapping = SourceCodeConceptMapping(source_vocabulary_id='NCIt', source_code='C141394')
    description, umls_name = _source_description(mapping, None)
    assert description == 'RISS Stage I Multiple Myeloma'
    assert umls_name == ''
