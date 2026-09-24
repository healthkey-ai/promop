"""Frozen original-export reconciliation for migration 0258.

Historical models only. Keep this version stable; later releases need their
own versioned evidence. No STCM, local relationship evidence, network or patient
data is used. Rules match the manually applied reconcile_athena_mappings job.
"""
from collections import Counter, defaultdict
from contextlib import nullcontext
from datetime import date

from django.db import transaction
from django.utils import timezone

CONCEPT_FIELDS = ['concept_id', 'vocabulary_id', 'concept_code', 'domain_id',
                  'standard_concept', 'valid_start_date', 'valid_end_date', 'invalid_reason']
RELATIONSHIP_FIELDS = ['concept_id_1', 'concept_id_2', 'valid_start_date', 'valid_end_date', 'invalid_reason']
TABLES = {'Condition': 'condition', 'Observation': 'observation', 'Measurement': 'measurement',
          'Procedure': 'procedure', 'Drug': 'drug_exposure'}
UPDATE_FIELDS = ['target_concept', 'destination_vocabulary_id', 'domain_id', 'omop_table',
                 'origin_system', 'source', 'notes', 'updated_at']


def _date(value):
    text = str(value)
    return (date(int(text[:4]), int(text[4:6]), int(text[6:8])) if len(text) == 8
            else date.fromisoformat(text))


def _active(start, end, invalid, today):
    return not invalid and _date(start) <= today <= _date(end)


def _key(row):
    vocabulary = row.source_vocabulary_id
    if vocabulary == 'ICD10' and row.origin_system == 'HT-One':
        vocabulary = 'ICD10CM'
    return vocabulary, row.source_code.strip().upper()


def _snapshot(row):
    return tuple(getattr(row, field.attname) for field in row._meta.concrete_fields)


def validate(payload):
    if (payload['schema_version'] != 1 or payload['concept_fields'] != CONCEPT_FIELDS
            or payload['relationship_fields'] != RELATIONSHIP_FIELDS):
        raise ValueError('Unsupported Athena reconciliation snapshot schema')
    for name, count, width in [('sources', 'source_count', 8), ('targets', 'target_count', 8),
                               ('relationships', 'relationship_count', 5)]:
        rows = payload[name]
        if len(rows) != payload[count] or any(len(row) != width for row in rows):
            raise ValueError('Invalid Athena snapshot counts or row shape: ' + name)
        for row in rows:
            if type(row[0]) is not int or (name == 'relationships' and type(row[1]) is not int):
                raise ValueError('Invalid Athena concept identifier')
            # Validate all date fields before any writes, including later batches.
            _date(row[-3])
            _date(row[-2])
        if name != 'relationships' and len({row[0] for row in rows}) != len(rows):
            raise ValueError('Duplicate Athena concept identifier')
    if any(row[1] not in ('ICD10', 'ICD10CM') for row in payload['sources']):
        raise ValueError('Unexpected Athena source vocabulary')
    source_ids = {row[0] for row in payload['sources']}
    if any(row[0] not in source_ids for row in payload['relationships']):
        raise ValueError('Relationship has no export source concept')


def evidence_for(payload, keys, today):
    sources = defaultdict(list)
    for record in payload['sources']:
        key = record[1], record[2].strip().upper()
        if key in keys and _active(*record[-3:], today):
            sources[key].append(record[0])
    wanted = {cid for ids in sources.values() for cid in ids}
    edges = defaultdict(set)
    for source, target, start, end, invalid in payload['relationships']:
        if source in wanted and _active(start, end, invalid, today):
            edges[source].add(target)
    target_rows = {row[0]: row for row in payload['targets']}
    result = {}
    for key in keys:
        ids = sources.get(key, [])
        if len(ids) != 1:
            result[key] = ('ambiguous_source' if ids else 'not_found', [])
            continue
        target_ids = edges.get(ids[0], set())
        if any(cid not in target_rows for cid in target_ids):
            result[key] = ('incomplete_evidence', [])
            continue
        targets = [dict(zip(CONCEPT_FIELDS, target_rows[cid])) for cid in sorted(target_ids)
                   if target_rows[cid][4] == 'S' and _active(*target_rows[cid][-3:], today)]
        result[key] = ('found' if len(targets) == 1 else 'ambiguous_targets' if targets else 'not_found', targets)
    return result


def reconcile(apps, connection, payload, *, dry_run=False, today=None, reference=''):
    validate(payload)
    Mapping = apps.get_model('omop_core', 'SourceCodeConceptMapping')
    Concept = apps.get_model('omop_core', 'Concept')
    Vocabulary = apps.get_model('omop_core', 'Vocabulary')
    alias, today = connection.alias, today or date.today()
    originals = list(Mapping.objects.using(alias).filter(
        source_vocabulary_id__in=['ICD10', 'ICD10CM'], status='approved',
    ).exclude(origin_system='athena').order_by('pk'))
    if not originals:
        return []
    evidence = evidence_for(payload, {_key(row) for row in originals}, today)
    receipts = []
    for start in range(0, len(originals), 250):
        receipts.extend(_batch(Mapping, Concept, Vocabulary, alias, originals[start:start + 250],
                               evidence, today, reference, dry_run))
    return receipts


def _batch(Mapping, Concept, Vocabulary, alias, originals, evidence, today, reference, dry_run):
    with nullcontext() if dry_run else transaction.atomic(using=alias):
        current = {row.pk: row for row in originals}
        target_ids = {target['concept_id'] for row in originals for target in evidence[_key(row)][1]}
        concepts = Concept.objects.using(alias).filter(pk__in=target_ids).order_by('pk')
        if not dry_run:
            current = Mapping.objects.using(alias).select_for_update().filter(pk__in=current).order_by('pk').in_bulk()
            concepts = concepts.select_for_update()
        concepts = concepts.in_bulk()
        deprecated = set(Vocabulary.objects.using(alias).filter(
            pk__in={row.vocabulary_id for row in concepts.values()}, is_deprecated=True,
        ).values_list('pk', flat=True))
        updates, receipts = [], []
        for original in originals:
            row = current.get(original.pk)
            key = _key(original)
            reason, targets = evidence[key]
            receipt = dict(mapping_id=original.pk, vocabulary=original.source_vocabulary_id,
                           code=original.source_code, prior=original.target_concept_id,
                           targets=[t['concept_id'] for t in targets], outcome='')
            receipts.append(receipt)
            if row is None or _snapshot(row) != _snapshot(original):
                receipt['outcome'] = 'changed_during_audit'
            elif row.locked_by_id is not None:
                receipt['outcome'] = 'locked'
            elif reason != 'found':
                receipt['outcome'] = reason
            else:
                target = targets[0]
                local = concepts.get(target['concept_id'])
                if local is None:
                    receipt['outcome'] = 'missing_local_target'
                elif any(getattr(local, field) != target[field] for field in ('vocabulary_id', 'concept_code', 'domain_id')):
                    receipt['outcome'] = 'local_target_conflict'
                elif (local.source or local.standard_concept != 'S' or local.invalid_reason
                      or not local.valid_start_date <= today <= local.valid_end_date
                      or local.vocabulary_id in deprecated):
                    receipt['outcome'] = 'invalid_local_target'
                elif local.domain_id not in TABLES:
                    receipt['outcome'] = 'unsupported_domain'
                else:
                    changed = row.target_concept_id != local.pk
                    outcome = 'destination_changed' if changed else 'reattributed'
                    receipt['outcome'] = ('would_change_destination' if changed else 'would_reattribute') if dry_run else outcome
                    if dry_run:
                        continue
                    prior_provenance = row.origin_system
                    row.target_concept_id = local.pk
                    row.destination_vocabulary_id = local.vocabulary_id
                    row.domain_id = local.domain_id
                    row.omop_table = TABLES[local.domain_id]
                    row.origin_system, row.source = 'athena', 'Athena'
                    row.updated_at = timezone.now()
                    note = (f'Athena export reconciliation migration 0258, {today}: '
                            f'{key[0]}:{row.source_code} Maps to {local.pk}; previous target '
                            f'{receipt["prior"]}, previous provenance {prior_provenance!r}. {reference}')
                    row.notes = '\n'.join(filter(None, [row.notes, note]))
                    updates.append(row)
        if updates:
            Mapping.objects.using(alias).bulk_update(updates, UPDATE_FIELDS, batch_size=250)
    return receipts


def summarize(receipts):
    return dict(Counter(row['outcome'] for row in receipts))
