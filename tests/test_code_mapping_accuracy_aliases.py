from importlib import import_module

import pytest
from django.apps import apps
from rest_framework.test import APIRequestFactory, force_authenticate

from omop_core.models import SourceCodeConceptMapping
from patient_portal.api.views import code_mapping_accuracy
from patient_portal.models import Identity

pytestmark = pytest.mark.django_db


def test_legacy_approved_suggestions_are_backfilled_as_model_acceptances():
    from tests.factories import ConceptFactory

    suggested = ConceptFactory()
    replacement = ConceptFactory()
    eligible = SourceCodeConceptMapping.objects.create(
        source_code='accepted-suggestion', origin_system='suggest v0.1',
        suggestion_model_version='v0.1', status='approved',
        target_concept=suggested, suggested_target_concept=suggested,
    )
    excluded = [
        SourceCodeConceptMapping.objects.create(
            source_code='approved-code-import', origin_system='code',
            suggestion_model_version='v0.1', status='approved',
            target_concept=suggested, suggested_target_concept=suggested,
        ),
        SourceCodeConceptMapping.objects.create(
            source_code='unreviewed-suggestion', origin_system='suggest v0.1',
            suggestion_model_version='v0.1', status='proposed',
            target_concept=suggested, suggested_target_concept=suggested,
        ),
        SourceCodeConceptMapping.objects.create(
            source_code='changed-suggestion', origin_system='suggest v0.1',
            suggestion_model_version='v0.1', status='approved',
            target_concept=replacement, suggested_target_concept=suggested,
        ),
        SourceCodeConceptMapping.objects.create(
            source_code='unversioned-suggestion', origin_system='suggest',
            status='approved', target_concept=suggested,
            suggested_target_concept=suggested,
        ),
    ]

    migration = import_module(
        'omop_core.migrations.0223_backfill_approved_suggestion_outcomes'
    )
    migration.backfill_approved_suggestion_outcomes(apps, None)

    eligible.refresh_from_db()
    assert eligible.suggestion_outcome == 'accepted'
    for mapping in excluded:
        mapping.refresh_from_db()
        assert mapping.suggestion_outcome == ''


def test_icd10_accuracy_combines_aliases_for_latest_model():
    SourceCodeConceptMapping.objects.bulk_create([
        SourceCodeConceptMapping(
            source_vocabulary_id=vocab, source_code=code,
            suggestion_model_version=version, suggestion_outcome=outcome,
        )
        for vocab, code, version, outcome in [
            ('ICD10', 'A01', '0.2', 'accepted'),
            ('ICD10CM', 'A02', '0.2', 'rejected'),
            ('ICD10CM', 'A03', '0.2', 'overridden'),
            ('ICD10CM', 'A04', '0.1', 'accepted'),
        ]
    ])
    user = Identity.objects.create_user(email='accuracy-admin@example.test', is_staff=True)
    request = APIRequestFactory().get('/api/code-mappings/accuracy/')
    force_authenticate(request, user=user)

    response = code_mapping_accuracy(request)

    assert response.status_code == 200
    assert set(response.data['by_source_vocabulary']) == {'ICD10'}
    metrics = response.data['by_source_vocabulary']['ICD10']
    assert metrics['model_version'] == '0.2'
    assert metrics['suggestions'] == 3
    assert metrics['approved'] == 1
    assert metrics['rejected'] == 1
    assert metrics['overridden'] == 1
    assert metrics['precision'] == pytest.approx(1 / 3)
    assert metrics['recall'] == pytest.approx(1 / 2)
    assert metrics['f1'] == pytest.approx(0.4)


@pytest.mark.parametrize(('status', 'change_destination', 'outcome', 'counter'), [
    ('approved', False, 'accepted', 'approved'),
    ('rejected', False, 'rejected', 'rejected'),
    ('approved', True, 'overridden', 'overridden'),
])
def test_uncoded_reviews_of_older_models_are_counted(status, change_destination, outcome, counter):
    from rest_framework.test import APIClient
    from tests.factories import ConceptFactory, DomainFactory

    domain = DomainFactory(domain_id='Measurement')
    original = ConceptFactory(domain=domain, standard_concept='S')
    replacement = ConceptFactory(domain=domain, standard_concept='S')
    old = SourceCodeConceptMapping.objects.create(
        source_vocabulary_id='', source_code='Uncoded lab', domain_id='Measurement',
        omop_table='measurement', target_concept=original, suggested_target_concept=original,
        origin_system='suggest v0.1', suggestion_model_version='v0.1', status='proposed',
    )
    SourceCodeConceptMapping.objects.create(
        source_vocabulary_id='', source_code='New unreviewed lab',
        origin_system='suggest v0.2', suggestion_model_version='v0.2', status='proposed',
    )
    SourceCodeConceptMapping.objects.create(
        source_vocabulary_id='LOINC', source_code='external',
        suggestion_model_version='v0.2', suggestion_outcome='accepted',
    )
    client = APIClient()
    client.force_authenticate(Identity.objects.create_user(email='uncoded@example.test', is_staff=True))
    before = client.get('/api/v1/code-mappings/accuracy/').data
    assert before['by_source_vocabulary']['']['review_totals'] == dict(approved=0, rejected=0, overridden=0)
    response = client.patch(f'/api/v1/code-mappings/{old.pk}/', {
        'status': status,
        'destination_concept_id': replacement.pk if change_destination else original.pk,
    }, format='json')
    assert response.status_code == 200, response.data
    old.refresh_from_db()
    assert old.source_vocabulary_id == ''
    assert old.suggestion_outcome == outcome
    after = client.get('/api/v1/code-mappings/accuracy/').data
    uncoded = after['by_source_vocabulary']['']
    assert uncoded['review_totals'] == {key: int(key == counter) for key in ('approved', 'rejected', 'overridden')}
    # Keep the current model's precision/recall and historical model records
    # separate from the cumulative review counters.
    assert uncoded['model_version'] == 'v0.2'
    assert uncoded['reviewed'] == 0
    assert uncoded['precision'] is None
    assert after['overall']['review_totals']['approved'] == 1 + int(counter == 'approved')
    history = client.get('/api/v1/code-mappings/accuracy/dashboard/').data['models']
    assert next(model for model in history if model['model_version'] == 'v0.1')[counter] == 1


def test_accuracy_uses_one_query_for_all_vocabularies():
    from django.db import connection
    from django.test.utils import CaptureQueriesContext
    for source in ['', 'ICD10', 'ICD10CM', 'LOINC', 'SNOMED', 'Apple', 'Garmin']:
        SourceCodeConceptMapping.objects.create(
            source_vocabulary_id=source, source_code='one',
            suggestion_model_version='v0.2', suggestion_outcome='rejected',
        )
    request = APIRequestFactory().get('/api/v1/code-mappings/accuracy/')
    force_authenticate(request, user=Identity.objects.create_user(email='query-count@example.test', is_staff=True))
    with CaptureQueriesContext(connection) as queries:
        response = code_mapping_accuracy(request)
    assert len(queries) == 1
    assert response.data['overall']['review_totals']['rejected'] == 7
    assert response.data['by_source_vocabulary']['']['review_totals']['rejected'] == 1
    assert response.data['by_source_vocabulary']['ICD10']['review_totals']['rejected'] == 2
    assert response.data['by_source_vocabulary']['OpenWearables']['review_totals']['rejected'] == 2


@pytest.mark.parametrize(('outcomes', 'expected'), [
    ([''], (None, None, None)),
    (['rejected'], (0, 0, 0)),
    (['overridden'], (0, 0, 0)),
    (['rejected', 'overridden', ''], (0, 0, 0)),
    (['accepted'], (1, 1, 1)),
    (['accepted', 'rejected', 'overridden', ''], (1 / 3, 1 / 2, 0.4)),
])
def test_current_model_metrics_match_history(outcomes, expected):
    from rest_framework.test import APIClient
    from omop_core.mapping.suggestions import SUGGESTION_MODEL_VERSION

    for index, outcome in enumerate(outcomes):
        SourceCodeConceptMapping.objects.create(
            source_vocabulary_id='LOINC', source_code=f'metric-{index}',
            suggestion_model_version=SUGGESTION_MODEL_VERSION,
            suggestion_outcome=outcome,
        )
    client = APIClient()
    client.force_authenticate(Identity.objects.create_user(email='metrics@example.test', is_staff=True))
    main = client.get('/api/v1/code-mappings/accuracy/').data
    history = client.get('/api/v1/code-mappings/accuracy/dashboard/').data['models']
    model = next(row for row in history if row['model_version'] == SUGGESTION_MODEL_VERSION)
    for metrics in (main['overall'], main['by_source_vocabulary']['LOINC'], model):
        assert metrics['suggestions'] == len(outcomes)
        assert metrics['reviewed'] == sum(bool(outcome) for outcome in outcomes)
        for key, value in zip(('precision', 'recall', 'f1'), expected):
            if value is None:
                assert metrics[key] is None
            else:
                assert metrics[key] == pytest.approx(value)


def test_two_approved_suggestions_remain_perfect_when_new_model_has_no_reviews():
    from rest_framework.test import APIClient

    SourceCodeConceptMapping.objects.bulk_create([
        SourceCodeConceptMapping(
            source_vocabulary_id='ICD10', source_code=f'staging-{index}',
            suggestion_model_version=version, suggestion_outcome=outcome,
            status='approved' if outcome else 'proposed',
        )
        for index, (version, outcome) in enumerate([
            ('v0.2', 'accepted'), ('v0.2', 'accepted'),
            ('v0.2', ''), ('v0.3', ''),
        ])
    ])
    client = APIClient()
    client.force_authenticate(Identity.objects.create_user(email='staging-metrics@example.test', is_staff=True))
    main = client.get('/api/v1/code-mappings/accuracy/').data
    for snapshot in (main['overall'], main['by_source_vocabulary']['ICD10']):
        assert snapshot['model_version'] == 'v0.3'
        latest_reviewed = snapshot['latest_reviewed']
        assert latest_reviewed['model_version'] == 'v0.2'
        assert latest_reviewed['reviewed'] == 2
        assert (latest_reviewed['precision'], latest_reviewed['recall'], latest_reviewed['f1']) == (1, 1, 1)
    history = client.get('/api/v1/code-mappings/accuracy/dashboard/').data['models']
    reviewed = next(model for model in history if model['model_version'] == 'v0.2')
    assert reviewed['approved'] == reviewed['reviewed'] == 2
    assert (reviewed['precision'], reviewed['recall'], reviewed['f1']) == (1, 1, 1)
    current = next(model for model in history if model['model_version'] == 'v0.3')
    assert current['reviewed'] == 0
    assert (current['precision'], current['recall'], current['f1']) == (None, None, None)


def test_all_models_snapshot_scores_every_version_together():
    """The mapping page's strip reads this, so it must span model versions.

    The newest reviewed version scores 100%; every version together is 3
    accepted of 5 reviews.
    """
    from rest_framework.test import APIClient

    SourceCodeConceptMapping.objects.bulk_create([
        SourceCodeConceptMapping(
            source_vocabulary_id='ICD10', source_code=f'all-models-{index}',
            suggestion_model_version=version, suggestion_outcome=outcome,
        )
        for index, (version, outcome) in enumerate([
            ('v0.1', 'accepted'), ('v0.1', 'rejected'), ('v0.1', 'overridden'),
            ('v0.2', 'accepted'), ('v0.2', 'accepted'),
        ])
    ])
    client = APIClient()
    client.force_authenticate(Identity.objects.create_user(email='all-models@example.test', is_staff=True))
    main = client.get('/api/v1/code-mappings/accuracy/').data

    for snapshot in (main['overall'], main['by_source_vocabulary']['ICD10']):
        all_models = snapshot['all_models']
        assert all_models['model_versions'] == 2
        assert all_models['suggestions'] == 5
        assert all_models['reviewed'] == 5
        assert (all_models['approved'], all_models['rejected'], all_models['overridden']) == (3, 1, 1)
        assert all_models['precision'] == pytest.approx(3 / 5)
        assert all_models['recall'] == pytest.approx(3 / 4)
        assert all_models['f1'] == pytest.approx(2 / 3)
        # The three counts stay consistent with the review_totals beside them.
        assert {key: all_models[key] for key in ('approved', 'rejected', 'overridden')} == snapshot['review_totals']
        # A single version's own numbers are unchanged and still perfect.
        assert snapshot['latest_reviewed']['model_version'] == 'v0.2'
        assert snapshot['latest_reviewed']['precision'] == 1
