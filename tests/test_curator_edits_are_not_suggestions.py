"""A curator's decision is never a suggestion, so Suggest never overwrites it (#1469).

Approved rows were always outside the candidate set. The two gaps this covers:
a curator who moves a destination but cannot approve left the row with
``suggest`` provenance, so Replace could re-answer it; and a batch run selects
its rows minutes before it writes, so a decision made during the run was
overwritten at write time.
"""
import pytest
from rest_framework.test import APIClient

from omop_core.mapping import suggestions
from omop_core.mapping.suggestions import (
    CURATED_DURING_RUN_MESSAGE, CURATOR_PROVENANCE, suggestable_mappings,
)
from omop_core.models import SourceCodeConceptMapping
from patient_portal.models import Identity
from tests.factories import ConceptClassFactory, ConceptFactory, DomainFactory, VocabularyFactory

pytestmark = pytest.mark.django_db


@pytest.fixture()
def concepts():
    domain = DomainFactory(domain_id='Condition', domain_name='Condition')
    vocab = VocabularyFactory(vocabulary_id='SNOMED', vocabulary_name='SNOMED')
    klass = ConceptClassFactory(concept_class_id='Clinical Finding')
    make = lambda cid, name: ConceptFactory(  # noqa: E731
        concept_id=cid, concept_name=name, concept_code=str(cid), vocabulary=vocab,
        domain=domain, concept_class=klass, standard_concept='S',
    )
    return make(9001, 'Model pick'), make(9002, 'Curator pick')


def suggested_row(target, **kwargs):
    """A proposed row a previous Suggest run answered."""
    defaults = dict(
        source_vocabulary_id='ICD10', source_code='C90.0', domain_id='Condition',
        omop_table='condition', status='proposed', origin='import',
        origin_system='suggest v0.4', suggestion_model_version='v0.4',
        target_concept=target, suggested_target_concept=target, occurrence_count=5,
    )
    defaults.update(kwargs)
    return SourceCodeConceptMapping.objects.create(**defaults)


def as_candidate(concept):
    return {
        'concept_id': concept.concept_id, 'concept_name': concept.concept_name,
        'concept_code': concept.concept_code, 'vocabulary_id': concept.vocabulary_id,
        'concept_class_id': concept.concept_class_id, 'domain_id': concept.domain_id,
        'umls_score': 1.0, 'retrieval': 'umls',
    }


@pytest.fixture()
def admin_client():
    client = APIClient()
    client.force_authenticate(Identity.objects.create_user(email='curator@example.test', is_staff=True))
    return client


# --- the edit path --------------------------------------------------------------

def test_moving_the_destination_makes_the_row_curated(admin_client, concepts):
    model_pick, curator_pick = concepts
    row = suggested_row(model_pick)

    response = admin_client.patch(f'/api/v1/code-mappings/{row.pk}/',
                                  {'destination_concept_id': curator_pick.pk}, format='json')

    assert response.status_code == 200, response.data
    row.refresh_from_db()
    assert row.status == 'proposed'
    assert row.target_concept_id == curator_pick.pk
    assert row.origin_system == CURATOR_PROVENANCE
    assert row.suggested_target_concept_id == model_pick.pk, 'the model history is kept'


def test_replace_skips_a_curated_row_but_still_reaches_untouched_suggestions(admin_client, concepts):
    model_pick, curator_pick = concepts
    curated = suggested_row(model_pick)
    untouched = suggested_row(model_pick, source_code='C90.1')
    admin_client.patch(f'/api/v1/code-mappings/{curated.pk}/',
                       {'destination_concept_id': curator_pick.pk}, format='json')

    replaceable = {m.pk for m in suggestable_mappings('condition', resuggest=True, min_occurrences=1)}

    assert untouched.pk in replaceable
    assert curated.pk not in replaceable


def test_an_edit_that_leaves_the_destination_alone_keeps_the_provenance(admin_client, concepts):
    model_pick, _ = concepts
    row = suggested_row(model_pick)

    admin_client.patch(f'/api/v1/code-mappings/{row.pk}/', {'notes': 'checked'}, format='json')

    row.refresh_from_db()
    assert row.origin_system == 'suggest v0.4'


def test_approving_a_curated_row_still_records_the_model_outcome(admin_client, concepts):
    model_pick, curator_pick = concepts
    row = suggested_row(model_pick)
    admin_client.patch(f'/api/v1/code-mappings/{row.pk}/',
                       {'destination_concept_id': curator_pick.pk}, format='json')

    admin_client.patch(f'/api/v1/code-mappings/{row.pk}/', {'status': 'approved'}, format='json')

    row.refresh_from_db()
    assert row.status == 'approved'
    assert row.suggestion_outcome == 'overridden', 'accuracy still sees the override'


def test_approved_rows_are_never_candidates(concepts):
    model_pick, _ = concepts
    suggested_row(model_pick, status='approved')
    suggested_row(model_pick, source_code='C90.1', status='approved', origin_system='')

    assert list(suggestable_mappings('condition', resuggest=True, min_occurrences=1)) == []


# --- the batch write path ---------------------------------------------------

def _run_with_decision_during_ranking(monkeypatch, row, decide):
    """Fake retrieval and a ranker that lets a curator act mid-run."""
    def rank(source_value, candidates, *args, **kwargs):
        decide()
        return candidates[0], 'ranked', []
    monkeypatch.setattr(suggestions, 'rank_candidates', rank)
    return suggestions.suggest_mappings('condition', strategies=['umls'], min_occurrences=1,
                                        resuggest=True)


def test_a_row_approved_during_the_run_is_left_alone(monkeypatch, concepts):
    model_pick, curator_pick = concepts
    row = suggested_row(model_pick)
    monkeypatch.setattr(suggestions, 'umls_candidates', lambda *a: ([as_candidate(model_pick)], 'C1'))

    def approve():
        SourceCodeConceptMapping.objects.filter(pk=row.pk).update(
            status='approved', target_concept=curator_pick,
        )

    results = _run_with_decision_during_ranking(monkeypatch, row, approve)

    row.refresh_from_db()
    assert (row.status, row.target_concept_id) == ('approved', curator_pick.pk)
    assert row.origin_system == 'suggest v0.4', 'nothing on the row was rewritten'
    assert results[0]['updated'] is False
    assert results[0]['note'] == CURATED_DURING_RUN_MESSAGE


def test_a_destination_moved_during_the_run_is_left_alone(monkeypatch, concepts):
    model_pick, curator_pick = concepts
    row = suggested_row(model_pick)
    monkeypatch.setattr(suggestions, 'umls_candidates', lambda *a: ([as_candidate(model_pick)], 'C1'))

    def move():
        SourceCodeConceptMapping.objects.filter(pk=row.pk).update(
            target_concept=curator_pick, origin_system=CURATOR_PROVENANCE,
        )

    results = _run_with_decision_during_ranking(monkeypatch, row, move)

    row.refresh_from_db()
    assert row.target_concept_id == curator_pick.pk
    assert results[0]['updated'] is False


def test_an_untouched_row_is_still_written(monkeypatch, concepts):
    model_pick, curator_pick = concepts
    row = suggested_row(model_pick)
    monkeypatch.setattr(suggestions, 'umls_candidates', lambda *a: ([as_candidate(curator_pick)], 'C1'))

    results = _run_with_decision_during_ranking(monkeypatch, row, lambda: None)

    row.refresh_from_db()
    assert row.target_concept_id == curator_pick.pk
    assert results[0]['updated'] is True
