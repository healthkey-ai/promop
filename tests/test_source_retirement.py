from datetime import date, timedelta

import pytest
from django.utils import timezone
from rest_framework.test import APIRequestFactory, force_authenticate

from omop_core.models import SourceCodeConceptMapping
from omop_core.services.source_retirement import mapping_source_retirement
from patient_portal.api.views import _serialize_code_mapping_row, code_mapping_list
from patient_portal.models import Identity
from tests.factories import ConceptFactory, VocabularyFactory

pytestmark = pytest.mark.django_db


def source(code='O35.0XX0', **kwargs):
    vocab = VocabularyFactory(vocabulary_id='ICD10CM')
    return ConceptFactory(vocabulary=vocab, concept_code=code, source='Athena', **kwargs)


def mapping(code='O35.0XX0', **kwargs):
    return SourceCodeConceptMapping.objects.create(source_vocabulary_id='ICD10', source_code=code, **kwargs)


@pytest.mark.parametrize('reason', ['D', 'U'])
def test_retired_alias_without_source_fk_or_destination_is_exposed(reason):
    concept = source(invalid_reason=reason, valid_end_date=date(2022, 9, 30))
    row = mapping()
    payload = _serialize_code_mapping_row(None, row)
    assert payload['source_retired'] is True
    assert payload['source_concept_id'] is None  # Read metadata; do not rewrite mappings.
    assert 'Athena ICD10CM' in payload['source_retirement_evidence'][0]
    assert f'concept {concept.pk}' in payload['source_retirement_evidence'][0]
    assert f'invalid reason {reason}' in payload['source_retirement_evidence'][0]


def test_expired_validity_is_evidence_even_without_invalid_reason():
    source(invalid_reason=None, valid_end_date=timezone.localdate() - timedelta(days=1))
    row = mapping()
    payload = mapping_source_retirement([row])[row.pk]
    assert payload['source_retired'] is True
    assert 'validity ended' in payload['source_retirement_evidence'][0]


def test_retired_destination_is_not_source_retirement():
    active = source(invalid_reason=None, valid_end_date=date(2099, 12, 31))
    target = ConceptFactory(invalid_reason='D')
    row = mapping(source_concept=active, target_concept=target)
    payload = _serialize_code_mapping_row(target, row)
    assert payload['source_retired'] is False
    assert payload['source_retirement_evidence'] == []
    assert payload['destination_invalid_reason'] == 'D'


def test_unknown_source_does_not_mean_active_or_retired():
    row = mapping()
    payload = _serialize_code_mapping_row(None, row)
    assert payload['source_retired'] is None
    assert payload['source_retirement_evidence'] == []


def test_same_code_in_unrelated_vocabulary_is_not_evidence():
    ConceptFactory(concept_code='O35.0XX0', vocabulary=VocabularyFactory(vocabulary_id='LOINC'), invalid_reason='D')
    row = mapping()
    assert mapping_source_retirement([row])[row.pk]['source_retired'] is None


def test_any_linked_source_retirement_evidence_is_kept():
    active = ConceptFactory(vocabulary=VocabularyFactory(vocabulary_id='ICD10'), concept_code='O35.0XX0',
                            invalid_reason=None, valid_end_date=date(2099, 12, 31))
    retired = source(invalid_reason='U', valid_end_date=date(2020, 1, 1))
    row = mapping(source_concept=active)
    payload = mapping_source_retirement([row])[row.pk]
    assert payload['source_retired'] is True
    assert any(str(retired.pk) in evidence for evidence in payload['source_retirement_evidence'])


def test_batch_uses_one_query_regardless_of_mapping_count(django_assert_num_queries):
    source(invalid_reason='D')
    rows = [mapping()] + [mapping(code=f'OTHER-{index}') for index in range(30)]
    with django_assert_num_queries(1):
        metadata = mapping_source_retirement(rows)
    assert len(metadata) == 31
    assert metadata[rows[0].pk]['source_retired'] is True


def test_list_returns_source_metadata_for_all_sections():
    source(invalid_reason='D')
    rows = [mapping(status='proposed')]
    rows.append(SourceCodeConceptMapping.objects.create(
        source_vocabulary_id='ICD10CM', source_code='O35.0XX0', status='approved', origin_system='athena'))
    request = APIRequestFactory().get('/')
    force_authenticate(request, user=Identity.objects.create_user(email='retirement-admin@example.test', is_staff=True))
    response = code_mapping_list(request)
    assert response.status_code == 200
    assert {r['mapping_id'] for r in response.data} == {r.pk for r in rows}
    assert all(r['source_retired'] is True for r in response.data)
