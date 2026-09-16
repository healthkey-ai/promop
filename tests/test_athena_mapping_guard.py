import pytest
from rest_framework.test import APIRequestFactory, force_authenticate

from omop_core.mapping import suggestions
from omop_core.models import SourceCodeConceptMapping
from omop_core.services.athena_mapping_guard import ATHENA_DUPLICATE_MESSAGE, athena_supplies_mapping
from patient_portal.api.views import code_mapping_detail, code_mapping_list
from patient_portal.models import Identity
from tests.factories import ConceptFactory

pytestmark = pytest.mark.django_db


@pytest.fixture
def athena():
    return SourceCodeConceptMapping.objects.create(
        source_vocabulary_id='ICD10CM', source_code='A02.0',
        target_concept=ConceptFactory(), status='approved', origin_system='athena',
    )


def request_mapping(method, payload, mapping_id=None):
    request = getattr(APIRequestFactory(), method)('/', payload, format='json')
    user = Identity.objects.filter(email='mapping-admin@example.test').first()
    if user is None:
        user = Identity.objects.create_user(email='mapping-admin@example.test', is_staff=True)
    force_authenticate(request, user=user)
    return code_mapping_list(request) if mapping_id is None else code_mapping_detail(request, mapping_id)


@pytest.mark.parametrize('source_vocabulary', ['ICD10', 'ICD10CM'])
@pytest.mark.parametrize('status', ['proposed', 'approved'])
def test_create_rejects_athena_pair_before_any_write(athena, source_vocabulary, status):
    response = request_mapping('post', {
        'source_vocabulary_id': source_vocabulary, 'source_code': ' a02.0 ',
        'destination_concept_id': athena.target_concept_id, 'status': status,
    })
    assert response.status_code == 400
    assert str(response.data['detail']) == ATHENA_DUPLICATE_MESSAGE
    assert SourceCodeConceptMapping.objects.count() == 1


@pytest.mark.parametrize('status', ['proposed', 'approved'])
@pytest.mark.parametrize('method', ['patch', 'post'])
def test_edit_and_approval_cannot_duplicate_athena(athena, status, method, monkeypatch):
    other = ConceptFactory()
    mapping = SourceCodeConceptMapping.objects.create(
        source_vocabulary_id='ICD10', source_code='A02.0', target_concept=other,
    )
    def unexpected_repoint(**kwargs):
        pytest.fail('Rejected duplicates must not repoint clinical rows')
    monkeypatch.setattr('patient_portal.api.views.repoint_clinical_rows', unexpected_repoint)
    response = request_mapping(method, {
        'mapping_id': mapping.pk, 'source_vocabulary_id': 'ICD10', 'source_code': 'A02.0',
        'destination_concept_id': athena.target_concept_id, 'status': status,
    }, mapping.pk if method == 'patch' else None)
    assert response.status_code == 400
    assert str(response.data['detail']) == ATHENA_DUPLICATE_MESSAGE
    mapping.refresh_from_db()
    assert mapping.target_concept_id == other.pk
    assert mapping.status == 'proposed'


def test_status_only_approval_is_blocked_but_rejection_is_allowed(athena):
    mapping = SourceCodeConceptMapping.objects.create(
        source_vocabulary_id='ICD10', source_code='A02.0', target_concept=athena.target_concept,
    )
    response = request_mapping('patch', {'status': 'approved'}, mapping.pk)
    assert response.status_code == 400
    assert str(response.data['detail']) == ATHENA_DUPLICATE_MESSAGE
    response = request_mapping('patch', {'status': 'rejected'}, mapping.pk)
    assert response.status_code == 200
    mapping.refresh_from_db()
    assert mapping.status == 'rejected'


def test_athena_metadata_edit_does_not_conflict_with_itself(athena):
    response = request_mapping('patch', {'notes': 'Reviewed original Athena mapping'}, athena.pk)
    assert response.status_code == 200


@pytest.mark.parametrize('different', ['source_code', 'vocabulary'])
def test_nonidentical_pairs_are_allowed(athena, different):
    payload = {
        'source_vocabulary_id': 'ICD10', 'source_code': 'A02.0',
        'destination_concept_id': athena.target_concept_id,
    }
    if different == 'destination':
        payload['destination_concept_id'] = ConceptFactory().pk
    elif different == 'source_code':
        payload['source_code'] = 'A020'
    else:
        payload['source_vocabulary_id'] = 'LOINC'
    assert request_mapping('post', payload).status_code == 201


def test_alias_guard_is_symmetric_and_not_a_global_code_match(athena):
    athena.source_vocabulary_id = 'ICD10'
    athena.source_code = ' A02.0 '
    athena.save()
    assert athena_supplies_mapping('ICD10CM', 'a02.0', athena.target_concept_id)
    assert not athena_supplies_mapping('LOINC', 'A02.0', athena.target_concept_id)
    assert athena_supplies_mapping('ICD10', 'A02.0', None)


@pytest.mark.parametrize('dry_run', [False, True])
def test_batch_suggest_skips_athena_supplied_candidate(athena, monkeypatch, dry_run):
    # The queue row Suggest reads: same code as the Athena mapping, under the
    # merged alias vocabulary, still waiting for a destination.
    queued = SourceCodeConceptMapping.objects.create(
        source_vocabulary_id='ICD10', source_code='A02.0', omop_table='condition',
        domain_id='Condition', status='proposed', origin_system='', occurrence_count=12,
    )
    monkeypatch.setattr(suggestions, 'umls_candidates', lambda *args: ([{'concept_id': athena.target_concept_id}], 'C1'))
    result = suggestions.suggest_mappings('condition', strategies=['umls'], dry_run=dry_run)
    assert result[0]['suggested'] is None
    assert result[0]['note'] == ATHENA_DUPLICATE_MESSAGE
    queued.refresh_from_db()
    assert queued.target_concept_id is None, 'the duplicate must not be written'
    assert SourceCodeConceptMapping.objects.count() == 2
    # A dry run writes nothing at all. A real one records that it tried, so the
    # code is not re-retrieved and re-ranked on every later run -- it has no
    # destination, and filtering on that alone would pin it to the front of the
    # queue for ever.
    assert result[0]['updated'] is (not dry_run)
    assert queued.last_suggest_attempt == (
        '' if dry_run else suggestions.SUGGESTION_MODEL_VERSION
    )
    # Never suggestion_model_version: nothing was proposed, so the accuracy
    # figures must not count this code as a suggestion the curator overrode.
    assert queued.suggestion_model_version == ''
    assert queued.origin_system == '', 'the row keeps the provenance that raised it'


def test_single_suggest_explains_athena_duplicate(athena, monkeypatch):
    monkeypatch.setattr(suggestions, 'umls_candidates', lambda *args: ([{'concept_id': athena.target_concept_id}], 'C1'))
    result = suggestions.suggest_one_mapping('A02.0', 'ICD10', 'condition', strategies=['umls'])
    assert result['suggested'] is None
    assert result['note'] == ATHENA_DUPLICATE_MESSAGE


@pytest.mark.parametrize('destination', ['different', 'missing'])
def test_icd10_source_precedence_regardless_of_destination(athena, destination):
    target_id = ConceptFactory().pk if destination == 'different' else None
    response = request_mapping('post', {
        'source_vocabulary_id': 'ICD10', 'source_code': ' a02.0 ',
        'destination_concept_id': target_id,
    })
    assert response.status_code == 400
    if destination == 'different':
        assert str(response.data['detail']) == ATHENA_DUPLICATE_MESSAGE
    assert athena_supplies_mapping('ICD10', ' a02.0 ', target_id)
    assert SourceCodeConceptMapping.objects.count() == 1


def test_cleanup_removes_only_non_athena_icd10_duplicates(athena):
    from importlib import import_module
    from django.apps import apps
    from django.db import connection
    cleanup = import_module('omop_core.migrations.0216_remove_icd10_athena_duplicates')
    def row(vocab, code, **kwargs):
        return SourceCodeConceptMapping.objects.create(
            source_vocabulary_id=vocab, source_code=code, **kwargs,
        )
    duplicates = [
        row('ICD10', 'A02.0', target_concept=ConceptFactory(), status='approved'),
        row('ICD10', ' a02.0 ', status='proposed'),
        row('ICD10CM', 'a02.0', status='rejected'),
    ]
    survivors = [athena,
        row('ICD10', 'a02.0', origin_system='athena', target_concept=ConceptFactory()),
        row('LOINC', 'A02.0'), row('ICD10', 'A020'), row('ICD10', 'B01'),
        row('ICD10CM', 'B01', origin_system='athena'),
        row('ICD10CM', ' ', origin_system='athena', target_concept=ConceptFactory()),
        row('ICD10', ' '),
    ]
    with connection.schema_editor() as editor:
        cleanup.remove_duplicates(apps, editor)
        cleanup.remove_duplicates(apps, editor)
    assert not SourceCodeConceptMapping.objects.filter(pk__in=[r.pk for r in duplicates]).exists()
    assert set(SourceCodeConceptMapping.objects.values_list('pk', flat=True)) == {r.pk for r in survivors}


@pytest.mark.parametrize('query', [{}, {'source': 'ICD10'}, {'status': 'proposed'}, {'search': 'Only duplicate'}])
def test_list_excludes_athena_owned_codes_before_filters(athena, query):
    duplicate = SourceCodeConceptMapping.objects.create(
        source_vocabulary_id='ICD10', source_code=' a02.0 ',
        target_concept=ConceptFactory(concept_name='Only duplicate'),
    )
    request = APIRequestFactory().get('/', query)
    force_authenticate(request, user=Identity.objects.create_user(
        email='list-admin@example.test', is_staff=True,
    ))
    response = code_mapping_list(request)
    assert response.status_code == 200
    assert duplicate.pk not in {r['mapping_id'] for r in response.data}
    if not query:
        assert {r['mapping_id'] for r in response.data} == {athena.pk}


def test_other_vocabularies_still_allow_different_destinations(athena):
    athena.source_vocabulary_id = 'LOINC'
    athena.save()
    other = ConceptFactory()
    assert not athena_supplies_mapping('LOINC', 'A02.0', other.pk)
    assert athena_supplies_mapping('LOINC', 'A02.0', athena.target_concept_id)
