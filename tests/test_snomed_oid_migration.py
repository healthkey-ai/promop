from datetime import timedelta
from importlib import import_module
from io import StringIO

import pytest
from django.core.management import call_command
from django.db import connection
from django.db.migrations.loader import MigrationLoader
from django.test.utils import CaptureQueriesContext, override_settings
from django.utils import timezone

from omop_core.data_migrations.snomed_oid_v1 import OID, reconcile, summarize
from omop_core.models import SourceCodeConceptMapping as Mapping, MappingDestinationCandidate as Candidate, MappingSuggestionReview as Review
from tests.factories import ConceptFactory, DomainFactory, VocabularyFactory

pytestmark = pytest.mark.django_db


@pytest.fixture
def historical_apps():
    # pytest builds an isolated schema without replaying unrelated migrations.
    # Restore migration discovery solely to get the real historical model state.
    with override_settings(MIGRATION_MODULES={}):
        return MigrationLoader(connection).project_state([
            ('omop_core', '0258_reconcile_approved_icd10_from_athena_export'),
        ]).apps


@pytest.fixture
def target():
    return ConceptFactory(concept_id=4120443, concept_code='234326005',
                          concept_name='Bone marrow sampling',
                          vocabulary=VocabularyFactory(vocabulary_id='SNOMED'),
                          domain=DomainFactory(domain_id='Procedure'))


def mapping(target, vocabulary=OID, **kwargs):
    return Mapping.objects.create(**(dict(
        source_vocabulary_id=vocabulary, source_code=target.concept_code,
        target_concept=target, destination_vocabulary_id='SNOMED', domain_id='Procedure',
        omop_table='procedure', status='proposed', origin='import', origin_system='HT-FHIR',
        source='HT-FHIR', occurrence_count=7,
    ) | kwargs))


def test_migration_normalizes_and_approves_without_faking_review(historical_apps, target):
    row = mapping(target, notes='Original evidence')
    before = Mapping.objects.values().get(pk=row.pk)
    migration = import_module('omop_core.migrations.0259_normalize_snomed_oid_mappings')
    with connection.schema_editor(atomic=False) as editor:
        migration.normalize_snomed_oid_mappings(historical_apps, editor)
    row.refresh_from_db()
    assert row.source_vocabulary_id == 'SNOMED'
    assert row.status == 'approved'
    assert row.target_concept_id == target.pk
    assert row.notes.startswith('Original evidence\n')
    assert row.reviewer_id is None and row.reviewed_at is None
    after = Mapping.objects.values().get(pk=row.pk)
    assert {key for key in before if before[key] != after[key]} == {'source_vocabulary_id', 'status', 'notes', 'updated_at'}
    assert reconcile(historical_apps, connection) == []
    assert Mapping.objects.values().get(pk=row.pk) == after


def test_duplicate_merge_preserves_evidence_candidates_reviews_and_signoff(historical_apps, target):
    from patient_portal.models import Identity
    reviewer = Identity.objects.create_user(email='oid-reviewer@example.test', is_staff=True)
    now = timezone.now()
    canonical = mapping(target, 'SNOMED', status='approved', notes='Canonical note',
                        reviewer=reviewer, reviewed_at=now, occurrence_count=7,
                        first_seen=now, last_seen=now)
    alias = mapping(target, notes='Alias note', occurrence_count=7,
                    source_code_description='Bone marrow sampling',
                    first_seen=now-timedelta(days=1), last_seen=now+timedelta(days=1))
    duplicate = Candidate.objects.create(mapping=canonical, target_vocabulary_id='SNOMED',
                                         target_concept_code=target.concept_code, target_concept=target,
                                         origins=['canonical'])
    Candidate.objects.create(mapping=alias, target_vocabulary_id='SNOMED',
                             target_concept_code=target.concept_code, target_concept=target, origins=['alias'])
    alternative = Candidate.objects.create(mapping=alias, target_vocabulary_id='SNOMED',
                                          target_concept_code='unloaded-alternative', origins=['HT-FHIR'])
    review = Review.objects.create(mapping=alias, source_vocabulary_id=OID, source_code=alias.source_code,
                                  suggested_target_concept_id=target.pk, suggestion_model_version='v0.1',
                                  suggestion_outcome='accepted')
    assert summarize(reconcile(historical_apps, connection)) == {'merged': 1}
    canonical.refresh_from_db(); duplicate.refresh_from_db(); alternative.refresh_from_db(); review.refresh_from_db()
    assert not Mapping.objects.filter(pk=alias.pk).exists()
    assert canonical.occurrence_count == 7  # Duplicate imports are not 14 encounters.
    assert canonical.reviewer_id == reviewer.pk and canonical.reviewed_at == now
    assert canonical.first_seen == alias.first_seen and canonical.last_seen == alias.last_seen
    assert 'Alias note' in canonical.notes and 'Canonical note' in canonical.notes and str(alias.pk) in canonical.notes
    assert canonical.source_code_description == alias.source_code_description
    assert duplicate.origins == ['alias', 'canonical']
    assert alternative.mapping_id == review.mapping_id == canonical.pk
    assert review.source_vocabulary_id == OID  # Historical evidence is not rewritten.
    assert Candidate.objects.filter(mapping=canonical).count() == 2
    assert reconcile(historical_apps, connection) == []


@pytest.mark.parametrize('changes,reason', [
    ({'standard_concept': None}, 'nonstandard_or_invalid'),
    ({'standard_concept': 'C'}, 'nonstandard_or_invalid'),
    ({'invalid_reason': 'D'}, 'nonstandard_or_invalid'),
    ({'valid_end_date': '2000-01-01'}, 'outside_valid_dates'),
    ({'valid_start_date': '2090-01-01'}, 'outside_valid_dates'),
    ({'concept_code': 'different'}, 'not_identity'),
])
def test_invalid_targets_normalize_but_remain_proposed(historical_apps, target, changes, reason):
    row = mapping(target)
    type(target).objects.filter(pk=target.pk).update(**changes)
    receipt, = reconcile(historical_apps, connection)
    row.refresh_from_db()
    assert receipt['reason'] == reason
    assert row.source_vocabulary_id == 'SNOMED' and row.status == 'proposed'
    assert row.target_concept_id == target.pk


@pytest.mark.parametrize('changes', [
    {'target_concept_id': None}, {'target_concept_id': 999999999},
    {'domain_id': 'Condition'}, {'omop_table': 'condition'},
    {'status': 'rejected'}, {'origin': 'curator'}, {'origin_system': 'curator'},
    {'suggested_target_concept_id': 4120443}, {'destination_vocabulary_id': 'RxNorm'},
])
def test_exceptions_preserve_decisions_and_destinations(historical_apps, target, changes):
    row = mapping(target, **changes)
    receipt, = reconcile(historical_apps, connection)
    row.refresh_from_db()
    assert receipt['outcome'] == 'normalized_unapproved'
    assert row.source_vocabulary_id == 'SNOMED'
    assert row.status == changes.get('status', 'proposed')
    assert row.target_concept_id == changes.get('target_concept_id', target.pk)


@pytest.mark.parametrize('canonical_changes,alias_changes,expected', [
    ({'status': 'rejected'}, {}, 'skipped_canonical_conflict'),
    ({'target_concept_id': None}, {}, 'skipped_canonical_conflict'),
    ({'domain_id': 'Condition'}, {}, 'skipped_canonical_metadata_conflict'),
    ({}, {'origin': 'curator'}, 'skipped_curator_or_suggestion'),
    ({}, {'status': 'rejected'}, 'skipped_curator_or_suggestion'),
])
def test_duplicate_conflicts_are_reported_without_writes(historical_apps, target, canonical_changes, alias_changes, expected):
    mapping(target, 'SNOMED', **({'status': 'approved'} | canonical_changes))
    mapping(target, **alias_changes)
    before = list(Mapping.objects.order_by('pk').values())
    assert summarize(reconcile(historical_apps, connection)) == {expected: 1}
    assert list(Mapping.objects.order_by('pk').values()) == before


def test_audit_is_read_only_and_other_populations_untouched(historical_apps, target):
    mapping(target)
    mapping(target, 'SNOMED', source_code='rxnorm-crossmap', domain_id='Drug',
            omop_table='drug_exposure', target_concept=None, origin_system='HT-One')
    before = list(Mapping.objects.order_by('pk').values())
    out = StringIO()
    with CaptureQueriesContext(connection) as queries:
        call_command('audit_snomed_oid_mappings', stdout=out)
    assert 'normalized_approved' in out.getvalue()
    assert not any(q['sql'].lstrip().upper().startswith(('UPDATE', 'INSERT', 'DELETE')) or 'FOR UPDATE' in q['sql'] for q in queries)
    assert list(Mapping.objects.order_by('pk').values()) == before
    reconcile(historical_apps, connection)
    assert Mapping.objects.values().get(source_code='rxnorm-crossmap') == before[1]


def test_ambiguous_alias_only_row_is_not_auto_approved(historical_apps, target):
    row = mapping(target)
    Candidate.objects.create(mapping=row, target_vocabulary_id='RxNorm', target_concept_code='other')
    receipt, = reconcile(historical_apps, connection)
    row.refresh_from_db()
    assert receipt['reason'] == 'alternative_destination'
    assert row.source_vocabulary_id == 'SNOMED' and row.status == 'proposed'
    assert Candidate.objects.filter(mapping=row).exists()


def test_locked_rows_are_reported_and_untouched(historical_apps, target):
    from patient_portal.models import Identity
    user = Identity.objects.create_user(email='oid-lock@example.test')
    row = mapping(target, locked_by=user, locked_at=timezone.now())
    before = Mapping.objects.values().get(pk=row.pk)
    assert summarize(reconcile(historical_apps, connection)) == {'skipped_locked': 1}
    assert Mapping.objects.values().get(pk=row.pk) == before


def test_candidate_link_conflict_prevents_merge(historical_apps, target):
    canonical = mapping(target, 'SNOMED', status='approved')
    alias = mapping(target)
    for row, concept_id in [(canonical, target.pk), (alias, 0)]:
        Candidate.objects.create(mapping=row, target_vocabulary_id='SNOMED',
                                 target_concept_code=target.concept_code, target_concept_id=concept_id)
    assert summarize(reconcile(historical_apps, connection)) == {'skipped_candidate_conflict': 1}
    assert Mapping.objects.filter(pk=alias.pk).exists()


def test_duplicate_candidate_zero_id_is_preserved(historical_apps, target):
    canonical = mapping(target, 'SNOMED', status='approved')
    alias = mapping(target)
    existing = Candidate.objects.create(mapping=canonical, target_vocabulary_id='SNOMED',
                                        target_concept_code='unresolved', target_concept_id=0)
    Candidate.objects.create(mapping=alias, target_vocabulary_id='SNOMED', target_concept_code='unresolved')
    assert summarize(reconcile(historical_apps, connection)) == {'merged': 1}
    existing.refresh_from_db()
    assert existing.target_concept_id == 0


def test_value_domain_identity_needs_no_fact_table(historical_apps, target):
    target.domain = DomainFactory(domain_id='Meas Value')
    target.save(update_fields=['domain'])
    row = mapping(target, domain_id='Meas Value', omop_table='')
    assert summarize(reconcile(historical_apps, connection)) == {'normalized_approved': 1}
    row.refresh_from_db()
    assert row.status == 'approved' and row.omop_table == ''
