"""Frozen reconciliation for #1584; use historical models, never clinical writes.

Duplicate imports share encounter evidence: retain max(Seen), not their sum.
The removed row is snapshotted in the survivor's notes, with candidates and
archived reviews reparented. Reversal requires a backup/manual reconciliation;
we cannot recreate duplicate IDs safely after later curation.
"""
import json
import logging
from collections import Counter, defaultdict
from contextlib import nullcontext
from datetime import date

from django.db import transaction
from django.utils import timezone

OID = 'urn:oid:2.16.840.1.113883.6.96'
TABLES = {'Condition': 'condition', 'Drug': 'drug_exposure', 'Measurement': 'measurement',
          'Observation': 'observation', 'Procedure': 'procedure'}
logger = logging.getLogger(__name__)


def _identity_problem(row, target, today):
    if target is None:
        return 'missing_target'
    if target.vocabulary_id != 'SNOMED' or target.concept_code != row.source_code:
        return 'not_identity'
    if target.standard_concept != 'S' or target.invalid_reason:
        return 'nonstandard_or_invalid'
    if not target.valid_start_date <= today <= target.valid_end_date:
        return 'outside_valid_dates'
    # Value/Route/etc concepts are legitimate identities but have no fact table.
    if row.domain_id != target.domain_id or row.omop_table != TABLES.get(target.domain_id, ''):
        return 'domain_table_conflict'
    if row.destination_vocabulary_id not in ('', 'SNOMED'):
        return 'destination_vocabulary_conflict'
    return ''


def _untouched_import(row):
    return (row.origin == 'import' and row.origin_system == 'HT-FHIR'
            and row.status == 'proposed'
            and not any((row.created_by_id, row.updated_by_id, row.reviewer_id,
                         row.reviewed_at, row.suggested_target_concept_id,
                         row.suggestion_model_version, row.suggestion_outcome,
                         row.last_suggest_attempt, row.suggest_strategy)))


def reconcile(apps, connection, *, dry_run=False):
    Mapping = apps.get_model('omop_core', 'SourceCodeConceptMapping')
    Concept = apps.get_model('omop_core', 'Concept')
    Candidate = apps.get_model('omop_core', 'MappingDestinationCandidate')
    Review = apps.get_model('omop_core', 'MappingSuggestionReview')
    alias = connection.alias
    mappings = Mapping.objects.using(alias)
    candidates = Candidate.objects.using(alias)
    reviews = Review.objects.using(alias)
    receipts = []
    rows = list(mappings.filter(source_vocabulary_id=OID).order_by('pk'))
    # Keep remote read-only previews bounded to four queries, with no locks or
    # per-row transactions. Writes re-read and lock each affected pair below.
    if dry_run:
        canonical_rows = {m.source_code: m for m in mappings.filter(
            source_vocabulary_id='SNOMED', source_code__in=[r.source_code for r in rows])}
        targets = Concept.objects.using(alias).in_bulk([r.target_concept_id for r in rows if r.target_concept_id is not None])
        candidate_rows = defaultdict(list)
        for candidate in candidates.filter(mapping_id__in=[r.pk for r in rows] + [r.pk for r in canonical_rows.values()]):
            candidate_rows[candidate.mapping_id].append(candidate)
    for initial in rows:
        with nullcontext() if dry_run else transaction.atomic(using=alias):
            query = mappings.select_for_update()
            row = initial if dry_run else query.filter(pk=initial.pk, source_vocabulary_id=OID).first()
            if row is None:
                continue
            canonical = canonical_rows.get(row.source_code) if dry_run else query.filter(source_vocabulary_id='SNOMED', source_code=row.source_code).first()
            target = targets.get(row.target_concept_id) if dry_run else Concept.objects.using(alias).filter(pk=row.target_concept_id).first()
            incoming = candidate_rows[row.pk] if dry_run else list(candidates.filter(mapping_id=row.pk))
            problem = _identity_problem(row, target, date.today())
            if not problem and not canonical and any(
                (c.target_vocabulary_id, c.target_concept_code) != ('SNOMED', row.source_code)
                for c in incoming
            ):
                problem = 'alternative_destination'
            receipt = dict(mapping_id=row.pk, source_code=row.source_code,
                           canonical_id=canonical.pk if canonical else None,
                           target_concept_id=row.target_concept_id)
            if row.locked_by_id or (canonical and canonical.locked_by_id):
                outcome = 'skipped_locked'
            elif canonical:
                if not _untouched_import(row):
                    outcome = 'skipped_curator_or_suggestion'
                elif problem:
                    outcome = 'skipped_' + problem
                elif canonical.status != 'approved' or canonical.target_concept_id != row.target_concept_id:
                    outcome = 'skipped_canonical_conflict'
                elif _identity_problem(canonical, target, date.today()):
                    outcome = 'skipped_canonical_metadata_conflict'
                else:
                    outcome = 'merged'
            else:
                outcome = 'normalized_approved' if not problem and _untouched_import(row) else 'normalized_unapproved'
                receipt['reason'] = problem or ('' if _untouched_import(row) else 'curator_or_suggestion')
                # Preserve existing approval/rejection decisions; outcome means
                # "not automatically approved", not a forced demotion.

            if outcome == 'merged':
                others = candidate_rows[canonical.pk] if dry_run else candidates.filter(mapping_id=canonical.pk)
                existing = {(c.target_vocabulary_id, c.target_concept_code): c
                            for c in others}
                if any(c.target_concept_id is not None and existing.get((c.target_vocabulary_id, c.target_concept_code))
                       and existing[(c.target_vocabulary_id, c.target_concept_code)].target_concept_id
                       not in (None, c.target_concept_id) for c in incoming):
                    outcome = 'skipped_candidate_conflict'

            receipt['outcome'] = outcome
            receipts.append(receipt)
            if outcome.startswith('skipped') or outcome == 'normalized_unapproved':
                logger.warning('SNOMED OID reconciliation: %s', receipt)
            if dry_run or outcome.startswith('skipped'):
                continue
            if outcome == 'merged':
                snapshot = {field.attname: getattr(row, field.attname) for field in Mapping._meta.concrete_fields}
                note = 'SNOMED OID duplicate merged (#1584): ' + json.dumps(snapshot, default=str, sort_keys=True)
                values = {'notes': '\n'.join(filter(None, (canonical.notes, note))),
                          'occurrence_count': max(canonical.occurrence_count, row.occurrence_count),
                          'updated_at': timezone.now()}
                for name, choose in [('first_seen', min), ('last_seen', max)]:
                    times = [v for v in (getattr(canonical, name), getattr(row, name)) if v is not None]
                    if times:
                        values[name] = choose(times)
                for name in ('source_code_description', 'umls_source_name'):
                    if not getattr(canonical, name):
                        values[name] = getattr(row, name)
                for candidate in incoming:
                    other = existing.get((candidate.target_vocabulary_id, candidate.target_concept_code))
                    if other:
                        candidates.filter(pk=other.pk).update(
                            origins=sorted(set(other.origins) | set(candidate.origins)),
                            target_concept_id=(other.target_concept_id if other.target_concept_id is not None
                                               else candidate.target_concept_id),
                        )
                    else:
                        candidates.filter(pk=candidate.pk).update(mapping_id=canonical.pk)
                reviews.filter(mapping_id=row.pk).update(mapping_id=canonical.pk)
                mappings.filter(pk=canonical.pk).update(**values)
                mappings.filter(pk=row.pk).delete()
            else:
                note = f'SNOMED source identifier normalized from {OID} (#1584); {outcome}.'
                values = dict(source_vocabulary_id='SNOMED', updated_at=timezone.now(),
                              notes='\n'.join(filter(None, (row.notes, note))))
                if outcome == 'normalized_approved':
                    values.update(status='approved', destination_vocabulary_id='SNOMED')
                mappings.filter(pk=row.pk).update(**values)
    return receipts


def summarize(receipts):
    return dict(Counter(row['outcome'] for row in receipts))
