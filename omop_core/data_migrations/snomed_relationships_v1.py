"""Frozen local-table repair of untouched SNOMED/RxNorm crossmap proposals.

Reads each instance's SCCM, concepts and outgoing Maps to relationships. Writes
only SCCM and its candidate rows; old definitions remain in SCCM audit notes.
No network, exports, fixed mapping IDs or clinical records are involved.
"""
import json
from collections import Counter, defaultdict
from contextlib import nullcontext
from datetime import date

from django.db import transaction
from django.db.models import Exists, OuterRef, Q
from django.utils import timezone

from .snomed_crossmap_v1 import IMPORT_ORIGINS, TABLES, standard_concepts

SINGLE_ORIGIN = 'athena-local-maps-to'
MULTIPLE_ORIGIN = 'athena-local-multiple'
REPAIRED_ORIGINS = (SINGLE_ORIGIN, MULTIPLE_ORIGIN)
UPDATE_FIELDS = ['source_concept', 'target_concept', 'destination_vocabulary_id',
                 'domain_id', 'omop_table', 'status', 'origin_system', 'notes', 'updated_at']


def eligible_mappings(apps, connection):
    Mapping = apps.get_model('omop_core', 'SourceCodeConceptMapping')
    Concept = apps.get_model('omop_core', 'Concept')
    Candidate = apps.get_model('omop_core', 'MappingDestinationCandidate')
    Review = apps.get_model('omop_core', 'MappingSuggestionReview')
    alias = connection.alias
    candidates = Candidate.objects.using(alias).filter(mapping_id=OuterRef('pk'))
    return Mapping.objects.using(alias).filter(
        source_vocabulary_id='SNOMED', status='proposed', origin='import',
        origin_system__in=IMPORT_ORIGINS, created_by__isnull=True, updated_by__isnull=True,
        reviewer__isnull=True, reviewed_at__isnull=True, locked_by__isnull=True,
        suggested_target_concept__isnull=True, suggestion_model_version='',
        suggestion_outcome='', suggest_strategy='', last_suggest_attempt='',
    ).annotate(
        valid_destination=Exists(standard_concepts(Concept, alias).filter(pk=OuterRef('target_concept_id'))),
        rx_candidate=Exists(candidates.filter(target_vocabulary_id='RxNorm')),
        other_candidate=Exists(candidates.exclude(target_vocabulary_id='RxNorm')),
        prior_review=Exists(Review.objects.using(alias).filter(mapping_id=OuterRef('pk'))),
    ).filter(valid_destination=False, other_candidate=False, prior_review=False).filter(
        Q(target_concept__vocabulary_id='RxNorm') |
        (Q(target_concept__isnull=True) & (Q(destination_vocabulary_id='RxNorm') | Q(rx_candidate=True)))
    )


def _snapshot(row):
    return {f.attname: getattr(row, f.attname) for f in row._meta.concrete_fields}


def _active(row, today):
    return not row.invalid_reason and row.valid_start_date <= today <= row.valid_end_date


def reconcile(apps, connection, *, dry_run=False, batch_size=250, on_batch=None):
    Mapping = apps.get_model('omop_core', 'SourceCodeConceptMapping')
    Concept = apps.get_model('omop_core', 'Concept')
    Relationship = apps.get_model('omop_core', 'ConceptRelationship')
    Candidate = apps.get_model('omop_core', 'MappingDestinationCandidate')
    Vocabulary = apps.get_model('omop_core', 'Vocabulary')
    alias, today = connection.alias, date.today()
    query = eligible_mappings(apps, connection)
    originals = list(query.order_by('pk'))
    receipts = []
    for start in range(0, len(originals), batch_size):
        batch = originals[start:start + batch_size]
        with nullcontext() if dry_run else transaction.atomic(using=alias):
            # Recheck eligibility under row locks, protecting concurrent curation.
            current = query.filter(pk__in=[r.pk for r in batch]).order_by('pk')
            sources = Concept.objects.using(alias).filter(
                vocabulary_id='SNOMED', concept_code__in=[r.source_code for r in batch]).order_by('pk')
            if not dry_run:
                current = current.select_for_update(of=('self',))
                sources = sources.select_for_update()
            current = current.in_bulk()
            sources = {s.concept_code: s for s in sources}
            edges = Relationship.objects.using(alias).filter(
                concept_1_id__in=[s.pk for s in sources.values()], relationship_id='Maps to',
                valid_start_date__lte=today, valid_end_date__gte=today,
            ).filter(Q(invalid_reason__isnull=True) | Q(invalid_reason='')).order_by('pk')
            if not dry_run:
                edges = edges.select_for_update()
            edges = list(edges)
            grouped = defaultdict(list)
            for edge in edges:
                grouped[edge.concept_1_id].append(edge)
            concepts = Concept.objects.using(alias).filter(pk__in={e.concept_2_id for e in edges}).order_by('pk')
            candidates = Candidate.objects.using(alias).filter(mapping_id__in=current).order_by('pk')
            if not dry_run:
                concepts = concepts.select_for_update()
                candidates = candidates.select_for_update()
            concepts = concepts.in_bulk()
            prior = defaultdict(list)
            for candidate in candidates:
                prior[candidate.mapping_id].append(candidate)
            deprecated = set(Vocabulary.objects.using(alias).filter(
                pk__in={c.vocabulary_id for c in concepts.values()}, is_deprecated=True,
            ).values_list('pk', flat=True))
            updates, additions, batch_receipts = [], [], []
            for original in batch:
                row = current.get(original.pk)
                source = sources.get(original.source_code)
                evidence = grouped[source.pk] if source else []
                receipt = dict(mapping_id=original.pk, source_code=original.source_code,
                               previous_target_id=original.target_concept_id, target_ids=[], outcome='')
                batch_receipts.append(receipt)
                if row is None or _snapshot(row) != _snapshot(original):
                    receipt['outcome'] = 'changed_or_protected'
                    continue
                if any(c.target_vocabulary_id != 'RxNorm' for c in prior[row.pk]):
                    receipt['outcome'] = 'changed_or_protected'
                    continue
                if not source or source.source:
                    receipt['outcome'] = 'missing_or_local_source'
                    continue
                if source.standard_concept == 'S' and _active(source, today):
                    receipt['outcome'] = 'standard_identity'
                    continue
                # Retired source codes may still have current Maps to edges.
                # Missing destinations make the evidence incomplete; never
                # approve a seemingly unique target from a partial installation.
                if any(e.concept_2_id not in concepts for e in evidence):
                    receipt['outcome'] = 'incomplete_relationship_targets'
                    continue
                targets = {e.concept_2_id: concepts[e.concept_2_id] for e in evidence
                           if concepts[e.concept_2_id].standard_concept == 'S'
                           and not concepts[e.concept_2_id].source
                           and _active(concepts[e.concept_2_id], today)
                           and concepts[e.concept_2_id].vocabulary_id not in deprecated}
                receipt['target_ids'] = sorted(targets)
                if not targets:
                    receipt['outcome'] = 'no_standard_destination'
                    continue
                multiple = len(targets) > 1
                receipt['outcome'] = 'proposed_multiple' if multiple else 'approved_single'
                if dry_run:
                    continue
                audit = dict(mapping=_snapshot(row), candidates=[_snapshot(c) for c in prior[row.pk]],
                             relationships=[_snapshot(e) for e in evidence], target_ids=sorted(targets))
                row.notes = '\n'.join(filter(None, [row.notes,
                    'Local SNOMED Maps to reconciliation (#1584): ' + json.dumps(audit, default=str, sort_keys=True)]))
                row.source_concept_id = source.pk
                row.target_concept_id = None if multiple else next(iter(targets))
                vocabularies = {t.vocabulary_id for t in targets.values()}
                domains = {t.domain_id for t in targets.values()}
                row.destination_vocabulary_id = next(iter(vocabularies)) if len(vocabularies) == 1 else ''
                row.domain_id = next(iter(domains)) if len(domains) == 1 else ''
                row.omop_table = TABLES.get(row.domain_id, '')
                row.status = 'proposed' if multiple else 'approved'
                row.origin_system = MULTIPLE_ORIGIN if multiple else SINGLE_ORIGIN
                row.updated_at = timezone.now()
                updates.append(row)
                additions.extend(Candidate(mapping_id=row.pk, target_concept_id=t.pk,
                    target_vocabulary_id=t.vocabulary_id, target_concept_code=t.concept_code,
                    origins=[SINGLE_ORIGIN]) for t in targets.values())
            if updates:
                Mapping.objects.using(alias).bulk_update(updates, UPDATE_FIELDS, batch_size=batch_size)
                Candidate.objects.using(alias).filter(mapping_id__in=[r.pk for r in updates]).delete()
                Candidate.objects.using(alias).bulk_create(additions, batch_size=500)
            receipts.extend(batch_receipts)
        if on_batch:
            on_batch(batch_receipts)
    return receipts


def summarize(receipts):
    return dict(Counter(row['outcome'] for row in receipts))
