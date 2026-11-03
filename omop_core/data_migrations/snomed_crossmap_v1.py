"""Frozen policy for replacing unreviewed RxNorm crossmap proposals with SNOMED identities.

Also used by the resolver for the same narrowly scoped promotion. Candidates,
review evidence, encounter counts and clinical rows are never rewritten.
"""
import json
from collections import Counter
from datetime import date

from django.db import transaction
from django.db.models import Exists, OuterRef, Q, Subquery
from django.utils import timezone

IDENTITY_ORIGIN = 'athena-standard-self'
IMPORT_ORIGINS = ('HT-One', 'HT-Next', 'HK-ETL', 'etl-cross-map', 'artifact')
TABLES = {'Condition': 'condition', 'Drug': 'drug_exposure', 'Measurement': 'measurement',
          'Observation': 'observation', 'Procedure': 'procedure'}


def standard_concepts(Concept, alias):
    today = date.today()
    return Concept.objects.using(alias).filter(
        standard_concept='S', valid_start_date__lte=today, valid_end_date__gte=today,
    ).filter(Q(invalid_reason__isnull=True) | Q(invalid_reason=''))


def identity_values(concept):
    return dict(source_concept_id=concept.pk, target_concept_id=concept.pk,
                destination_vocabulary_id='SNOMED', domain_id=concept.domain_id,
                omop_table=TABLES.get(concept.domain_id, ''), status='approved',
                origin_system=IDENTITY_ORIGIN)


def eligible_mappings(apps, connection):
    Mapping = apps.get_model('omop_core', 'SourceCodeConceptMapping')
    Concept = apps.get_model('omop_core', 'Concept')
    Candidate = apps.get_model('omop_core', 'MappingDestinationCandidate')
    alias = connection.alias
    standard = standard_concepts(Concept, alias)
    candidates = Candidate.objects.using(alias).filter(mapping_id=OuterRef('pk'))
    return Mapping.objects.using(alias).filter(
        source_vocabulary_id='SNOMED', status='proposed', origin='import',
        origin_system__in=IMPORT_ORIGINS,
        created_by__isnull=True, updated_by__isnull=True, reviewer__isnull=True,
        reviewed_at__isnull=True, locked_by__isnull=True,
        suggested_target_concept__isnull=True, suggestion_model_version='',
        suggestion_outcome='', suggest_strategy='', last_suggest_attempt='',
    ).annotate(
        identity_id=Subquery(standard.filter(vocabulary_id='SNOMED', concept_code=OuterRef('source_code')).values('pk')[:1]),
        valid_destination=Exists(standard.filter(pk=OuterRef('target_concept_id'))),
        rxnorm_candidate=Exists(candidates.filter(target_vocabulary_id='RxNorm')),
        other_candidate=Exists(candidates.exclude(target_vocabulary_id='RxNorm')),
    ).filter(identity_id__isnull=False, valid_destination=False, other_candidate=False).filter(
        Q(target_concept__vocabulary_id='RxNorm') |
        (Q(target_concept__isnull=True) & (Q(destination_vocabulary_id='RxNorm') | Q(rxnorm_candidate=True)))
    )


def reconcile(apps, connection, *, dry_run=False, mapping_ids=None):
    Mapping = apps.get_model('omop_core', 'SourceCodeConceptMapping')
    Concept = apps.get_model('omop_core', 'Concept')
    alias = connection.alias
    query = eligible_mappings(apps, connection)
    if mapping_ids is not None:
        query = query.filter(pk__in=mapping_ids)
    receipts = []
    ids = list(query.order_by('pk').values_list('pk', flat=True))
    for start in range(0, len(ids), 500):
        # Writes re-evaluate the eligibility predicate while locking the rows.
        # The preview uses the same predicate but no locks or transactions.
        if dry_run:
            rows = list(query.filter(pk__in=ids[start:start + 500]))
            for row in rows:
                receipts.append(dict(mapping_id=row.pk, source_code=row.source_code,
                                     previous_target_id=row.target_concept_id,
                                     target_concept_id=row.identity_id, outcome='self_mapped'))
            continue
        with transaction.atomic(using=alias):
            rows = list(query.filter(pk__in=ids[start:start + 500]).select_for_update(of=('self',)))
            concepts = Concept.objects.using(alias).in_bulk([r.identity_id for r in rows])
            for row in rows:
                concept = concepts[row.identity_id]
                snapshot = {field.attname: getattr(row, field.attname) for field in Mapping._meta.concrete_fields}
                note = 'Standard SNOMED identity replaces unreviewed RxNorm crossmap (#1584): ' + json.dumps(snapshot, default=str, sort_keys=True)
                receipts.append(dict(mapping_id=row.pk, source_code=row.source_code,
                                     previous_target_id=row.target_concept_id,
                                     target_concept_id=concept.pk, outcome='self_mapped'))
                for key, value in identity_values(concept).items():
                    setattr(row, key, value)
                row.notes = '\n'.join(filter(None, (row.notes, note)))
                row.updated_at = timezone.now()
            Mapping.objects.using(alias).bulk_update(rows, [
                'source_concept', 'target_concept', 'destination_vocabulary_id',
                'domain_id', 'omop_table', 'status', 'origin_system', 'notes', 'updated_at',
            ], batch_size=250)
    return receipts


def summarize(receipts):
    return dict(Counter(row['outcome'] for row in receipts))
