"""Frozen SCCM-based reconciliation for migration 0259 and its audit command.

Athena-provenance SCCM rows are the evidence. No export, STCM, relationship
table, network access, or instance-specific mapping IDs are required.
"""
from collections import Counter, defaultdict
from contextlib import nullcontext
from datetime import date

from django.db import transaction
from django.db.models import Q
from django.db.models.functions import Lower, Trim
from django.utils import timezone

VOCABULARIES = ('ICD10', 'ICD10CM')
TABLES = {'Condition': 'condition', 'Observation': 'observation', 'Measurement': 'measurement',
          'Procedure': 'procedure', 'Drug': 'drug_exposure'}
UPDATE_FIELDS = ['target_concept', 'destination_vocabulary_id', 'domain_id', 'omop_table',
                 'origin_system', 'source', 'notes', 'updated_at']
REPORT_FIELDS = ['mapping_id', 'vocabulary', 'code', 'prior', 'prior_provenance',
                 'targets', 'evidence_mapping_ids', 'outcome']


def _snapshot(row):
    return tuple(getattr(row, field.attname) for field in row._meta.concrete_fields)


def _group(rows):
    groups = defaultdict(list)
    for row in rows:
        groups[row.normalized_code].append(row)
    return groups


def reconcile(apps, connection, *, dry_run=False, today=None, vocabularies=VOCABULARIES,
              on_batch=None):
    Mapping = apps.get_model('omop_core', 'SourceCodeConceptMapping')
    Concept = apps.get_model('omop_core', 'Concept')
    Vocabulary = apps.get_model('omop_core', 'Vocabulary')
    alias, today = connection.alias, today or date.today()
    # Match the merged ICD-10 curation tab: both vocabularies, ignoring case
    # and surrounding whitespace, but preserving punctuation and code suffixes.
    mappings = Mapping.objects.using(alias).annotate(normalized_code=Lower(Trim('source_code')))
    originals = list(mappings.filter(source_vocabulary_id__in=vocabularies, status='approved')
                     .exclude(origin_system='athena').order_by('pk'))
    if not originals:
        return []
    evidence_filter = Q(source_vocabulary_id__in=VOCABULARIES, status='approved', origin_system='athena')
    # Freeze evidence before any writes; newly converted rows cannot supply
    # evidence for later batches in this run.
    evidence = _group(mappings.filter(evidence_filter).exclude(normalized_code='').order_by('pk'))
    receipts, applied_ids = [], set()
    for start in range(0, len(originals), 250):
        batch = _batch(mappings, Concept, Vocabulary, alias, originals[start:start + 250],
                       evidence, evidence_filter, applied_ids, today, dry_run)
        receipts.extend(batch)
        applied_ids.update(row['mapping_id'] for row in batch
                           if row['outcome'] in ('destination_changed', 'reattributed'))
        if on_batch:
            on_batch(batch)
    return receipts


def _batch(mappings, Concept, Vocabulary, alias, originals, evidence, evidence_filter,
           applied_ids, today, dry_run):
    with nullcontext() if dry_run else transaction.atomic(using=alias):
        ids = {row.pk for row in originals}
        codes = {row.normalized_code for row in originals} - {''}
        current = mappings.filter(Q(pk__in=ids) | (evidence_filter & Q(normalized_code__in=codes)))
        current = current.exclude(pk__in=applied_ids).order_by('pk')
        if not dry_run:
            current = current.select_for_update()
        current = list(current)
        current_rows = {row.pk: row for row in current}
        current_evidence = _group(row for row in current if row.origin_system == 'athena'
                                  and row.status == 'approved' and row.normalized_code
                                  and row.source_vocabulary_id in VOCABULARIES)
        target_ids = {row.target_concept_id for code in codes for row in evidence[code]}
        concepts = Concept.objects.using(alias).filter(pk__in=target_ids).order_by('pk')
        if not dry_run:
            concepts = concepts.select_for_update()
        concepts = concepts.in_bulk()
        deprecated = set(Vocabulary.objects.using(alias).filter(
            pk__in={row.vocabulary_id for row in concepts.values()}, is_deprecated=True,
        ).values_list('pk', flat=True))
        updates, receipts = [], []
        for original in originals:
            row = current_rows.get(original.pk)
            sources = evidence[original.normalized_code]
            targets = {source.target_concept_id for source in sources}
            receipt = dict(mapping_id=original.pk, vocabulary=original.source_vocabulary_id,
                           code=original.source_code, prior=original.target_concept_id,
                           prior_provenance=original.origin_system,
                           targets=sorted(targets - {None}),
                           evidence_mapping_ids=[source.pk for source in sources], outcome='')
            receipts.append(receipt)
            if row is None or _snapshot(row) != _snapshot(original):
                receipt['outcome'] = 'changed_during_audit'
            elif row.locked_by_id is not None:
                receipt['outcome'] = 'locked'
            elif [_snapshot(source) for source in sources] != [
                _snapshot(source) for source in current_evidence[original.normalized_code]
            ]:
                receipt['outcome'] = 'evidence_changed_during_audit'
            elif not sources:
                receipt['outcome'] = 'not_found'
            elif any(source.locked_by_id is not None for source in sources):
                receipt['outcome'] = 'evidence_locked'
            elif None in targets:
                receipt['outcome'] = 'incomplete_evidence'
            elif len(targets) != 1:
                receipt['outcome'] = 'ambiguous_targets'
            else:
                target = concepts.get(next(iter(targets)))
                if target is None:
                    receipt['outcome'] = 'missing_local_target'
                elif (target.source or target.standard_concept != 'S' or target.invalid_reason
                      or not target.valid_start_date <= today <= target.valid_end_date
                      or target.vocabulary_id in deprecated):
                    receipt['outcome'] = 'invalid_local_target'
                elif target.domain_id not in TABLES:
                    receipt['outcome'] = 'unsupported_domain'
                else:
                    changed = row.target_concept_id != target.pk
                    receipt['outcome'] = (('would_change_destination' if changed else 'would_reattribute')
                                          if dry_run else ('destination_changed' if changed else 'reattributed'))
                    if dry_run:
                        continue
                    row.target_concept_id = target.pk
                    row.destination_vocabulary_id = target.vocabulary_id
                    row.domain_id, row.omop_table = target.domain_id, TABLES[target.domain_id]
                    row.origin_system, row.source = 'athena', 'Athena'
                    row.updated_at = timezone.now()
                    note = (f'Athena SCCM reconciliation (0259), {today}: evidence SCCM '
                            f'{receipt["evidence_mapping_ids"]}; previous target {receipt["prior"]}, '
                            f'previous provenance {receipt["prior_provenance"]!r}; target {target.pk}.')
                    row.notes = '\n'.join(filter(None, [row.notes, note]))
                    updates.append(row)
        if updates:
            mappings.model.objects.using(alias).bulk_update(updates, UPDATE_FIELDS, batch_size=250)
    return receipts


def summarize(receipts):
    return dict(Counter(row['outcome'] for row in receipts))
