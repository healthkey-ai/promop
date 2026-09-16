import importlib
import json
import sys
from io import StringIO
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import transaction

from omop_core.management.commands.precompute_suggest_embeddings import Command
from omop_core.models import (
    ConceptEmbedding, ConceptSynonym, SourceCodeConceptMapping,
    SuggestEmbeddingSnapshot,
    UmlsConcept, UmlsRelease, UmlsSourceCode,
)
from omop_core.services.embedding_jobs import dispatch_suggest_embeddings
from tests.factories import ConceptFactory


LOADERS = (
    'load_athena_vocabularies', 'load_mappings', 'sync_athena_mappings',
    'import_fhir_crossmaps', 'import_etl_cross_maps',
    'import_healthtree_crossmaps', 'import_htfhir_crossmaps', 'import_hklabs_crossmaps',
)


@pytest.fixture
def queue(db):
    concept = ConceptFactory(concept_name='Serum glucose concentration')
    mapping = SourceCodeConceptMapping.objects.create(
        source_code='glucose', source_code_description='Serum glucose concentration',
        omop_table='measurement', domain_id='Measurement', status='proposed',
        occurrence_count=10,
    )
    return concept, mapping


@pytest.fixture
def encoder(monkeypatch):
    model = Mock()
    model.encode.side_effect = lambda names, **kwargs: [
        SimpleNamespace(tolist=lambda: [0.5] * 384) for _ in names
    ]
    constructor = Mock(return_value=model)
    monkeypatch.setitem(sys.modules, 'sentence_transformers',
                        SimpleNamespace(SentenceTransformer=constructor))
    return constructor, model


def precompute(**options):
    call_command('precompute_suggest_embeddings', stdout=StringIO(), **options)


def test_precompute_embeds_missing_candidates_and_repeats_in_one_query(
        queue, encoder, django_assert_num_queries):
    concept, _ = queue
    precompute()
    assert ConceptEmbedding.objects.filter(concept=concept).exists()
    assert encoder[1].encode.call_args.args[0] == [concept.concept_name]
    encoder[0].reset_mock()
    with patch.object(Command, '_retrieve_candidates') as retrieve:
        with django_assert_num_queries(1):
            precompute()
    retrieve.assert_not_called()
    encoder[0].assert_not_called()


@pytest.mark.parametrize('change', ('concept', 'synonym', 'queue'))
def test_changed_inputs_invalidate_snapshot(queue, encoder, change):
    concept, mapping = queue
    precompute()
    if change == 'concept':
        concept.concept_name = 'Serum glucose level'
        concept.save(update_fields=['concept_name'])
    elif change == 'synonym':
        ConceptSynonym.objects.create(
            concept=concept, concept_synonym_name='Glucose in serum',
            language_concept=ConceptFactory(concept_name='English'),
        )
    else:
        mapping.source_code_description = 'Glucose in serum'
        mapping.save(update_fields=['source_code_description'])
    with patch.object(Command, '_retrieve_candidates', return_value={concept.pk}) as retrieve:
        precompute()
    retrieve.assert_called_once()


def test_deleted_embedding_is_rebuilt_without_retrieval(queue, encoder):
    concept, _ = queue
    precompute()
    ConceptEmbedding.objects.filter(concept=concept).delete()
    with patch.object(Command, '_retrieve_candidates') as retrieve:
        precompute()
    retrieve.assert_not_called()
    assert ConceptEmbedding.objects.filter(concept=concept).exists()
    assert encoder[1].encode.call_count == 2


def test_limited_snapshot_tracks_attempt_order(queue, encoder):
    from omop_core.mapping.suggestions import SUGGESTION_MODEL_VERSION

    first, mapping = queue
    second = ConceptFactory(concept_name='Unrelated candidate')
    SourceCodeConceptMapping.objects.create(
        source_code='second', source_code_description=second.concept_name,
        omop_table='measurement', domain_id='Measurement', status='proposed',
        occurrence_count=10,
    )
    precompute(limit=1)
    assert ConceptEmbedding.objects.filter(concept=first).exists()
    assert not ConceptEmbedding.objects.filter(concept=second).exists()
    mapping.last_suggest_attempt = SUGGESTION_MODEL_VERSION
    mapping.save(update_fields=['last_suggest_attempt'])
    precompute(limit=1)
    assert ConceptEmbedding.objects.filter(concept=second).exists()


def test_new_candidate_after_vocabulary_load_is_embedded(queue, encoder):
    precompute()
    new = ConceptFactory(concept_name='Serum glucose concentrations')
    precompute()
    assert ConceptEmbedding.objects.filter(concept=new).exists()
    assert encoder[1].encode.call_args.args[0] == [new.concept_name]


def test_imported_gap_invalidates_empty_snapshot(queue, encoder):
    concept, mapping = queue
    mapping.status = 'approved'
    mapping.save(update_fields=['status'])
    precompute()
    mapping.status = 'proposed'
    mapping.origin_system = 'hk-labs'
    mapping.save(update_fields=['status', 'origin_system'])
    precompute()
    assert ConceptEmbedding.objects.filter(concept=concept).exists()


def test_precompute_resolves_unlinked_source_concept(queue, encoder):
    concept, mapping = queue
    mapping.source_code_description = ''
    mapping.source_vocabulary_id = concept.vocabulary_id
    mapping.source_code = concept.concept_code
    mapping.save()
    precompute()
    assert ConceptEmbedding.objects.filter(concept=concept).exists()


def test_umls_preferred_name_invalidates_snapshot(queue, encoder):
    concept, mapping = queue
    mapping.source_code_description = ''
    mapping.source_vocabulary_id = 'LOINC'
    mapping.source_code = 'unloaded-source-code'
    mapping.save()
    precompute()
    release = UmlsRelease.objects.create(release_version='test')
    cui = UmlsConcept.objects.create(cui='C1234567', release=release)
    term = UmlsSourceCode.objects.create(
        concept=cui, root_source='LNC', code=mapping.source_code,
        name=concept.concept_name, is_preferred=True, term_type='PT',
    )
    precompute()
    assert ConceptEmbedding.objects.filter(concept=concept).exists()
    term.name = 'Different name'
    term.save(update_fields=['name'])
    with patch.object(Command, '_retrieve_candidates', return_value=set()) as retrieve:
        precompute()
    retrieve.assert_called_once()


def test_measure_never_writes_and_force_reencodes(queue, encoder):
    precompute(measure=True)
    assert not ConceptEmbedding.objects.exists()
    assert not SuggestEmbeddingSnapshot.objects.exists()
    encoder[0].assert_not_called()
    precompute()
    precompute(force=True)
    assert encoder[1].encode.call_count == 2


def test_umls_candidates_and_changed_bridge_invalidate_snapshot(queue, encoder,
                                                              django_assert_num_queries):
    _, mapping = queue
    mapping.source_vocabulary_id = 'LOINC'
    mapping.source_code = 'source-with-umls-bridge'
    mapping.save()
    release = UmlsRelease.objects.create(release_version='test')
    cui = UmlsConcept.objects.create(cui='C1234567', release=release)
    UmlsSourceCode.objects.create(
        concept=cui, root_source='LNC', code=mapping.source_code,
        name='Source', is_preferred=True, term_type='PT',
    )
    destinations = [ConceptFactory(concept_name=name) for name in ('Unrelated alpha', 'Unrelated beta')]
    precompute()
    assert not ConceptEmbedding.objects.filter(concept__in=destinations).exists()
    for destination in destinations:
        UmlsSourceCode.objects.create(
            concept=cui, root_source='LNC', code=destination.concept_code,
            name=destination.concept_name, is_preferred=False, term_type='SY',
        )
    precompute()
    assert ConceptEmbedding.objects.filter(concept__in=destinations).count() == 2
    with django_assert_num_queries(1):
        precompute()


def test_failed_encoding_does_not_cache_success(queue, encoder):
    encoder[1].encode.side_effect = RuntimeError('model unavailable')
    with pytest.raises(RuntimeError, match='model unavailable'):
        precompute()
    assert not SuggestEmbeddingSnapshot.objects.exists()


@pytest.mark.django_db
def test_empty_queue_repeats_in_one_query(django_assert_num_queries, encoder):
    precompute()
    with django_assert_num_queries(1):
        precompute()
    encoder[0].assert_not_called()


@pytest.mark.parametrize('loader', (*LOADERS, 'load_umls_release', 'sync_umls_release'))
@pytest.mark.parametrize('mode', ('success', 'dry_run', 'skip', 'failure'))
def test_loader_completion_hook(loader, mode):
    command = importlib.import_module(f'omop_core.management.commands.{loader}').Command
    options = {'stdout': StringIO(), 'skip_checks': True}
    if loader == 'import_fhir_crossmaps':
        options.update(type='cpt-to-snomed', file='unused.json')
    if loader == 'load_umls_release':
        options.update(archive='unused.zip', release_version='test', release_url='https://example.invalid')
    if mode == 'dry_run':
        if loader not in LOADERS:
            pytest.skip('Standalone UMLS loaders do not offer dry runs.')
        options['dry_run'] = True
    if mode == 'skip':
        options['skip_suggest_embeddings'] = True
    error = CommandError('load failed') if mode == 'failure' else None
    with patch.object(command, 'handle', side_effect=error, return_value=None), patch(
            'omop_core.management.embedding_command.dispatch_suggest_embeddings') as dispatch:
        if error:
            with pytest.raises(CommandError, match='load failed'):
                call_command(loader, **options)
        else:
            call_command(loader, **options)
    assert dispatch.call_count == (1 if mode == 'success' else 0)


@pytest.mark.parametrize('skip', (True, False))
def test_nested_loaders_dispatch_only_once(skip):
    from omop_core.management.commands.load_athena_vocabularies import Command as Athena
    from omop_core.management.commands.load_mappings import Command as Mappings

    def nested(*args, **kwargs):
        call_command('load_mappings', stdout=StringIO(), skip_checks=True)

    with patch.object(Athena, 'handle', side_effect=nested), patch.object(
            Mappings, 'handle', return_value=None), patch(
            'omop_core.management.embedding_command.dispatch_suggest_embeddings') as dispatch:
        call_command('load_athena_vocabularies', stdout=StringIO(), skip_checks=True,
                     skip_suggest_embeddings=skip)
    assert dispatch.call_count == (0 if skip else 1)


@pytest.mark.django_db
@pytest.mark.parametrize('broker', ('', 'redis://localhost:6379/0'))
def test_dispatch_runs_after_commit(settings, broker, django_capture_on_commit_callbacks):
    settings.CELERY_BROKER_URL = broker
    with patch('omop_core.tasks.precompute_suggest_embeddings_task.delay') as queued, patch(
            'omop_core.services.embedding_jobs.run_suggest_embeddings') as inline:
        with django_capture_on_commit_callbacks(execute=True):
            dispatch_suggest_embeddings()
            queued.assert_not_called()
            inline.assert_not_called()
    assert queued.call_count == bool(broker)
    assert inline.call_count == (not broker)


@pytest.mark.django_db
def test_rollback_discards_dispatch(django_capture_on_commit_callbacks):
    with django_capture_on_commit_callbacks(execute=True) as callbacks:
        with pytest.raises(RuntimeError):
            with transaction.atomic():
                dispatch_suggest_embeddings()
                raise RuntimeError('rollback')
    assert callbacks == []


def test_real_mapping_load_warms_queue(queue, encoder, tmp_path, settings,
                                     django_capture_on_commit_callbacks):
    settings.CELERY_BROKER_URL = ''
    artifact = tmp_path / 'mappings.json'
    artifact.write_text(json.dumps({'mappings': []}))
    with django_capture_on_commit_callbacks(execute=True):
        call_command('load_mappings', artifact=str(artifact), stdout=StringIO())
    assert ConceptEmbedding.objects.filter(concept=queue[0]).exists()


def test_task_uses_full_candidate_configuration():
    from omop_core.mapping.suggestions import LEXICAL_LIMIT_MAX
    from omop_core.tasks import precompute_suggest_embeddings_task

    with patch('omop_core.services.embedding_jobs.call_command') as command:
        precompute_suggest_embeddings_task.run()
    command.assert_called_once_with('precompute_suggest_embeddings',
                                    min_occurrences=1, lexical_limit=LEXICAL_LIMIT_MAX)
