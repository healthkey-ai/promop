"""Reconcile hand-mapped ICD-10 SCCM rows against the live STCM table.

Reads ``source_to_concept_map`` (already populated with new Athena vocabulary
data) and re-attributes approved, non-Athena ICD-10 rows whose source code
now has a valid standard Athena mapping.  Rows with no STCM match or with
multiple ambiguous targets are left unchanged.

No frozen snapshot -- reads the STCM table as-is at run time.
"""
import logging
from collections import Counter
from datetime import date

from django.db import transaction
from django.utils import timezone

logger = logging.getLogger(__name__)

ICD10_VOCABS = ('ICD10CM', 'ICD10')

DOMAIN_TO_TABLE = {
    'Condition': 'condition',
    'Observation': 'observation',
    'Measurement': 'measurement',
    'Procedure': 'procedure',
    'Drug': 'drug_exposure',
}

UPDATE_FIELDS = [
    'target_concept', 'destination_vocabulary_id', 'domain_id',
    'omop_table', 'origin_system', 'source', 'reviewed_at',
    'notes', 'updated_at',
]

BATCH_SIZE = 250


def reconcile(apps, connection):
    """Re-attribute hand-mapped ICD-10 rows to Athena where STCM agrees.

    Returns a list of receipt dicts, one per eligible row processed.
    """
    Mapping = apps.get_model('omop_core', 'SourceCodeConceptMapping')
    STCM = apps.get_model('omop_core', 'SourceToConceptMap')
    Concept = apps.get_model('omop_core', 'Concept')
    alias = connection.alias
    today = date.today()
    now = timezone.now()
    receipts = []

    # 1. Fetch all eligible SCCM rows
    eligible = list(
        Mapping.objects.using(alias)
        .filter(source_vocabulary_id__in=ICD10_VOCABS, status='approved')
        .exclude(origin_system='athena')
    )
    if not eligible:
        logger.info('No eligible ICD-10 mapped rows to reconcile.')
        return receipts

    logger.info('Found %d eligible ICD-10 mapped rows.', len(eligible))

    # 2. Build STCM lookup: (vocab, code_casefolded) → [stcm_row, ...]
    stcm_rows = (
        STCM.objects.using(alias)
        .filter(
            source_vocabulary_id__in=ICD10_VOCABS,
            invalid_reason__isnull=True,
            valid_start_date__lte=today,
            valid_end_date__gte=today,
        )
    )
    stcm_by_key = {}
    for stcm in stcm_rows.iterator():
        key = (stcm.source_vocabulary_id, stcm.source_code.strip().upper())
        stcm_by_key.setdefault(key, []).append(stcm)

    # 3. Pre-fetch valid standard target concepts
    target_ids = set()
    for matches in stcm_by_key.values():
        for m in matches:
            target_ids.add(m.target_concept_id)

    valid_concepts = {}
    for concept in (Concept.objects.using(alias)
                    .filter(concept_id__in=target_ids,
                            standard_concept='S',
                            invalid_reason__isnull=True)):
        valid_concepts[concept.concept_id] = concept

    # 4. Process in batches
    for start in range(0, len(eligible), BATCH_SIZE):
        batch = eligible[start:start + BATCH_SIZE]
        _process_batch(Mapping, alias, batch, stcm_by_key, valid_concepts,
                       today, now, receipts)

    return receipts


def _process_batch(Mapping, alias, batch, stcm_by_key, valid_concepts,
                   today, now, receipts):
    to_update = []
    with transaction.atomic(using=alias):
        row_ids = [r.pk for r in batch]
        locked = {
            r.pk: r
            for r in Mapping.objects.using(alias)
            .select_for_update()
            .filter(pk__in=row_ids)
        }

        for original in batch:
            row = locked.get(original.pk)
            if not row:
                continue
            # Re-check eligibility after locking
            if row.origin_system == 'athena' or row.status != 'approved':
                receipts.append(_receipt(row, 'skipped'))
                continue

            code_upper = row.source_code.strip().upper()

            # Try same-vocab first, then cross-vocab fallback
            key = (row.source_vocabulary_id, code_upper)
            alt_vocab = 'ICD10' if row.source_vocabulary_id == 'ICD10CM' else 'ICD10CM'
            alt_key = (alt_vocab, code_upper)

            matches = stcm_by_key.get(key, [])
            if not matches:
                matches = stcm_by_key.get(alt_key, [])

            # Filter to valid standard targets
            valid_matches = [m for m in matches
                             if m.target_concept_id in valid_concepts]

            if not valid_matches:
                receipts.append(_receipt(row, 'no_stcm_match'))
                continue

            # Deduplicate by target_concept_id (same target from multiple STCM rows)
            seen_targets = {}
            for m in valid_matches:
                seen_targets.setdefault(m.target_concept_id, m)
            unique_targets = list(seen_targets.values())

            if len(unique_targets) > 1:
                receipts.append(_receipt(
                    row, 'multiple_targets',
                    targets=[m.target_concept_id for m in unique_targets],
                ))
                continue

            # Exactly one valid target
            stcm_match = unique_targets[0]
            target = valid_concepts[stcm_match.target_concept_id]
            prior_target_id = row.target_concept_id
            destination_changed = (prior_target_id != target.concept_id)

            row.target_concept_id = target.concept_id
            row.origin_system = 'athena'
            row.source = 'Athena'
            row.destination_vocabulary_id = target.vocabulary_id
            row.domain_id = target.domain_id
            row.omop_table = DOMAIN_TO_TABLE.get(target.domain_id, '')
            row.reviewed_at = now
            row.updated_at = now

            if destination_changed:
                note = (
                    f'STCM reconciliation {today}: destination changed from '
                    f'concept {prior_target_id} to {target.concept_id} '
                    f'({target.vocabulary_id}:{target.concept_code}). '
                    f'Re-attributed to Athena.'
                )
            else:
                note = (
                    f'STCM reconciliation {today}: destination confirmed by '
                    f'Athena ({target.vocabulary_id}:{target.concept_code}). '
                    f'Re-attributed to Athena.'
                )
            row.notes = '\n'.join(filter(None, [row.notes, note]))

            to_update.append(row)
            receipts.append(_receipt(
                row,
                'destination_changed' if destination_changed else 'reattributed',
                prior=prior_target_id,
                new=target.concept_id,
            ))

        if to_update:
            Mapping.objects.using(alias).bulk_update(
                to_update, UPDATE_FIELDS, batch_size=BATCH_SIZE,
            )


def _receipt(row, outcome, **extra):
    r = {
        'code': row.source_code,
        'vocabulary': row.source_vocabulary_id,
        'outcome': outcome,
    }
    r.update(extra)
    return r


def summarize(receipts):
    """Return a dict of outcome → count."""
    return dict(Counter(r['outcome'] for r in receipts))
