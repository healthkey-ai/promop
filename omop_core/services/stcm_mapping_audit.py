"""Read-only diagnosis and explicit, rerunnable application of migration 0256."""
from collections import defaultdict
from contextlib import nullcontext
from datetime import date

from django.db import transaction
from django.db.models.functions import Trim, Upper
from django.utils import timezone

from omop_core.data_migrations.athena_icd10_stcm_reconcile import (
    DOMAIN_TO_TABLE, ICD10_VOCABS, UPDATE_FIELDS, _snapshot,
)
from omop_core.models import Concept, SourceCodeConceptMapping as Mapping, SourceToConceptMap as STCM


def mapping_key(row):
    vocabulary = row.source_vocabulary_id
    if vocabulary == 'ICD10' and row.origin_system == 'HT-One':
        vocabulary = 'ICD10CM'
    return vocabulary, row.source_code.strip().upper()


def active(row, today):
    return (row.invalid_reason is None
            and row.valid_start_date <= today <= row.valid_end_date)


def read_evidence(rows, using):
    """Keep raw matches as well as eligible matches, so an exclusion is visible."""
    codes = {row.source_code.strip().upper() for row in rows}
    matches = defaultdict(list)
    query = STCM.objects.using(using).filter(source_vocabulary_id__in=ICD10_VOCABS).alias(
        normalized_code=Upper(Trim('source_code')),
    ).filter(normalized_code__in=codes)
    for entry in query:
        matches[(entry.source_vocabulary_id, entry.source_code.strip().upper())].append(entry)
    targets = Concept.objects.using(using).in_bulk(
        {entry.target_concept_id for entries in matches.values() for entry in entries})
    return matches, targets


def classify(row, matches, targets, today):
    key = mapping_key(row)
    exact_key = row.source_vocabulary_id, row.source_code.strip().upper()
    entries = matches.get(key, [])
    current = [entry for entry in entries if active(entry, today)]
    valid_ids = sorted({entry.target_concept_id for entry in current
                        if entry.target_concept_id in targets
                        and targets[entry.target_concept_id].standard_concept == 'S'
                        and active(targets[entry.target_concept_id], today)})
    receipt = {
        'mapping_id': row.pk, 'source_vocabulary': row.source_vocabulary_id,
        'source_code': row.source_code, 'lookup_vocabulary': key[0],
        'current_provenance': row.origin_system, 'current_target_id': row.target_concept_id,
        'exact_vocabulary_code_matches': len(matches.get(exact_key, [])),
        'lookup_matches': len(entries), 'current_stcm_matches': len(current),
        'valid_standard_target_ids': ';'.join(map(str, valid_ids)),
        'proposed_target_id': '', 'proposed_vocabulary': '', 'proposed_domain': '',
        'outcome': '',
    }
    if row.locked_by_id is not None:
        receipt['outcome'] = 'locked'
    elif not entries:
        receipt['outcome'] = 'no_stcm_match'
    elif not current:
        receipt['outcome'] = 'no_current_stcm_match'
    elif not valid_ids:
        receipt['outcome'] = 'no_valid_standard_target'
    elif len(valid_ids) > 1:
        receipt['outcome'] = 'multiple_targets'
    else:
        target = targets[valid_ids[0]]
        receipt.update(
            proposed_target_id=target.pk, proposed_vocabulary=target.vocabulary_id,
            proposed_domain=target.domain_id,
            outcome='would_reattribute' if row.target_concept_id == target.pk else 'would_change_destination',
        )
    return receipt


def audit_batch(originals, *, using='default', apply=False):
    """Dry runs issue SELECTs only; apply locks and rechecks complete row snapshots.

    Only SCCM is updated, via bulk_update (no approval hooks or patient rewrites).
    Vocabulary evidence is freshly read for every batch, including apply runs.
    """
    today = date.today()
    with transaction.atomic(using=using) if apply else nullcontext():
        current = {row.pk: row for row in originals}
        if apply:
            current = Mapping.objects.using(using).select_for_update().filter(
                pk__in=current).in_bulk()
        matches, targets = read_evidence(originals, using)
        receipts, updates = [], []
        for original in originals:
            row = current.get(original.pk)
            receipt = classify(original, matches, targets, today)
            if apply and (row is None or _snapshot(row) != _snapshot(original)):
                receipt['outcome'] = 'changed_during_audit'
            elif apply and receipt['outcome'].startswith('would_'):
                target = targets[receipt['proposed_target_id']]
                row.target_concept_id = target.pk
                row.destination_vocabulary_id = target.vocabulary_id
                row.domain_id = target.domain_id
                row.omop_table = DOMAIN_TO_TABLE.get(target.domain_id, '')
                row.origin_system, row.source = 'athena', 'Athena'
                row.updated_at = timezone.now()
                note = (f'STCM reconciliation {today}: concept {receipt["current_target_id"]} '
                        f'-> {target.pk}; re-attributed to Athena using '
                        f'{receipt["lookup_vocabulary"]}:{row.source_code}.')
                row.notes = '\n'.join(filter(None, [row.notes, note]))
                receipt['outcome'] = ('reattributed' if receipt['outcome'] == 'would_reattribute'
                                      else 'destination_changed')
                updates.append(row)
            receipts.append(receipt)
        if updates:
            Mapping.objects.using(using).bulk_update(updates, UPDATE_FIELDS, batch_size=250)
    return receipts
