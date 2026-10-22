"""Frozen offline loader for the 2026-09-21 Athena recovery snapshot.

Uses only historical models supplied by the caller. Do not change this version
for later vocabulary releases; ship a new loader and migration instead.
"""
from collections import Counter
from datetime import date

from django.db import transaction
from django.db.models.functions import Lower, Trim
from django.utils import timezone

TABLES = {'Condition': 'condition', 'Observation': 'observation', 'Measurement': 'measurement',
          'Procedure': 'procedure', 'Drug': 'drug_exposure'}
REFERENCE_FIELDS = {
    'domain_id': ('Domain', ('domain_name', 'domain_concept_id')),
    'vocabulary_id': ('Vocabulary', ('vocabulary_name', 'vocabulary_reference', 'vocabulary_version', 'vocabulary_concept_id')),
    'concept_class_id': ('ConceptClass', ('concept_class_name', 'concept_class_concept_id')),
}
UPDATE_FIELDS = ['target_concept', 'source_concept', 'destination_vocabulary_id', 'domain_id',
                 'omop_table', 'status', 'origin', 'origin_system', 'source', 'reviewed_at',
                 'notes', 'source_code_description', 'updated_at']


def _date(value):
    value = str(value)
    return date(int(value[:4]), int(value[4:6]), int(value[6:8])) if len(value) == 8 else date.fromisoformat(value[:10])


def _eligible(row):
    if row.status not in ('proposed', 'none', '') or any(
            getattr(row, key) for key in ('reviewer_id', 'reviewed_at', 'updated_by_id', 'locked_by_id')):
        return False
    if row.origin_system.lower().startswith('curator') or row.origin_system == 'athena-multiple':
        return False
    return not row.target_concept_id or (row.origin == 'import' and row.created_by_id is None)


def _refresh_verified_concepts(apps, connection, payload, dry_run):
    """Refresh only the explicitly listed identities verified on Athena's site."""
    Concept = apps.get_model('omop_core', 'Concept')
    alias, refreshed = connection.alias, {}
    fields = ['concept_name', 'domain', 'concept_class', 'standard_concept',
              'invalid_reason', 'valid_start_date', 'valid_end_date']
    with transaction.atomic(using=alias):
        query = Concept.objects.using(alias).filter(pk__in=payload.get('refresh_concept_ids', []))
        if not dry_run:
            query = query.select_for_update()
        updates = []
        for concept in query:
            target = payload['concepts'][str(concept.pk)]
            if (concept.source or concept.vocabulary_id != target['vocabulary_id']
                    or concept.concept_code != target['concept_code']):
                continue
            if target['standard_concept'] != 'S' or target['invalid_reason']:
                raise ValueError('Verified concept refresh must retain standard valid destinations')
            for key in ('domain_id', 'concept_class_id'):
                model_name, reference_fields = REFERENCE_FIELDS[key]
                model = apps.get_model('omop_core', model_name)
                if not model.objects.using(alias).filter(pk=target[key]).exists():
                    metadata = payload['metadata'][key][target[key]]
                    if not dry_run:
                        model.objects.using(alias).create(pk=target[key], **{f: metadata[f] for f in reference_fields})
            for key in ('concept_name', 'domain_id', 'concept_class_id', 'standard_concept'):
                setattr(concept, key, target[key])
            concept.invalid_reason = None
            concept.valid_start_date = _date(target['valid_start_date'])
            concept.valid_end_date = _date(target['valid_end_date'])
            updates.append(concept)
            refreshed[concept.pk] = concept
        if not dry_run:
            Concept.objects.using(alias).bulk_update(updates, fields, batch_size=100)
    return refreshed


def reconcile(apps, connection, payload, *, dry_run=False):
    """Return per-code receipts; batch transactions make reruns safe after interruption."""
    if payload['schema_version'] != 1 or payload['mapping_count'] != len(payload['mappings']):
        raise ValueError('Invalid Athena recovery snapshot')
    if payload['concept_count'] != len(payload['concepts']):
        raise ValueError('Invalid Athena recovery concept count')
    all_keys = [(r['source_vocabulary_id'], r['source_code'].strip().casefold()) for r in payload['mappings']]
    if len(set(all_keys)) != len(all_keys):
        raise ValueError('Duplicate source codes in Athena recovery snapshot')
    Concept = apps.get_model('omop_core', 'Concept')
    Mapping = apps.get_model('omop_core', 'SourceCodeConceptMapping')
    Candidate = apps.get_model('omop_core', 'MappingDestinationCandidate')
    alias, today = connection.alias, date.today()
    concepts, mappings, candidates = (model.objects.using(alias) for model in (Concept, Mapping, Candidate))
    references = {key: apps.get_model('omop_core', model) for key, (model, _) in REFERENCE_FIELDS.items()}
    refreshed = _refresh_verified_concepts(apps, connection, payload, dry_run)
    receipts = []
    def lock(query):
        return query if dry_run else query.select_for_update()

    for start in range(0, len(payload['mappings']), 250):
        batch = payload['mappings'][start:start + 250]
        with transaction.atomic(using=alias):
            existing_rows = list(lock(mappings).alias(code_norm=Lower(Trim('source_code'))).filter(
                source_vocabulary_id__in={r['source_vocabulary_id'] for r in batch},
                code_norm__in={r['source_code'].strip().lower() for r in batch}))
            by_key = {}
            for row in existing_rows:
                by_key.setdefault((row.source_vocabulary_id, row.source_code.strip().casefold()), []).append(row)
            target_ids = {int(cid) for record in batch for cid in record['target_concept_ids']}
            source_ids = {record['source_concept_id'] for record in batch}
            existing_concepts = lock(concepts).filter(pk__in=target_ids | source_ids).in_bulk()
            if dry_run:
                existing_concepts.update({cid: value for cid, value in refreshed.items() if cid in existing_concepts})
            target_records = {cid: payload['concepts'][str(cid)] for cid in target_ids}
            natural_ids = {(v, c): cid for v, c, cid in concepts.filter(
                vocabulary_id__in={r['vocabulary_id'] for r in target_records.values()},
                concept_code__in={r['concept_code'] for r in target_records.values()},
            ).values_list('vocabulary_id', 'concept_code', 'pk')}
            existing_refs = {key: lock(model.objects.using(alias)).in_bulk(
                {r[key] for r in target_records.values()}) for key, model in references.items()}
            old_choices = {}
            for candidate in candidates.filter(mapping_id__in=[r.pk for r in existing_rows]):
                old_choices.setdefault(candidate.mapping_id, []).append(
                    f'{candidate.target_vocabulary_id}:{candidate.target_concept_code}')
            accepted, new_concepts = [], {}
            new_refs = {key: {} for key in references}
            for record in batch:
                key = (record['source_vocabulary_id'], record['source_code'].strip().casefold())
                peers = by_key.get(key, [])
                receipt = dict(vocabulary=key[0], code=record['source_code'], tier=record['tier'],
                    targets=';'.join(map(str, record['target_concept_ids'])), outcome='', reason='')
                receipts.append(receipt)
                if len(peers) > 1:
                    receipt.update(outcome='preserved', reason='Case-variant mapping rows require review')
                    continue
                row = peers[0] if peers else None
                if row and not _eligible(row):
                    receipt.update(outcome='already_athena' if row.status == 'approved' and row.origin_system == 'athena'
                                   else 'already_athena_choices' if row.origin_system == 'athena-multiple'
                                   else 'preserved', reason='Existing approved or curator-owned mapping')
                    continue
                targets = [target_records[int(cid)] for cid in record['target_concept_ids']]
                if not targets:
                    raise ValueError('Snapshot contains an empty destination set')
                error = ''
                for target in targets:
                    cid = int(target['concept_id'])
                    if (not 0 < cid < 2_000_000_000 or target['standard_concept'] != 'S' or target['invalid_reason']
                            or not _date(target['valid_start_date']) <= today <= _date(target['valid_end_date'])):
                        error = 'Snapshot destination is not currently standard and valid'
                        break
                    current = existing_concepts.get(cid)
                    if current and (current.vocabulary_id != target['vocabulary_id']
                            or current.concept_code != target['concept_code']
                            or current.domain_id != target['domain_id']
                            or current.source or current.standard_concept != 'S' or current.invalid_reason
                            or not current.valid_start_date <= today <= current.valid_end_date):
                        error = 'Existing destination conflicts with Athena evidence'
                        break
                    if not current and natural_ids.get((target['vocabulary_id'], target['concept_code']), cid) != cid:
                        error = 'Destination code already has a different local ID'
                        break
                    vocabulary = existing_refs['vocabulary_id'].get(target['vocabulary_id'])
                    if vocabulary and vocabulary.is_deprecated:
                        error = 'Destination vocabulary is deprecated locally'
                        break
                    for field in references:
                        if (target[field] not in existing_refs[field]
                                and target[field] not in payload['metadata'].get(field, {})):
                            error = 'Missing reference metadata: ' + field
                source = existing_concepts.get(record['source_concept_id'])
                if source and (source.vocabulary_id != record['lookup_vocabulary']
                        or source.concept_code.strip().casefold() != record['source_code'].strip().casefold()):
                    error = 'Existing source concept identity conflicts with Athena evidence'
                if error:
                    receipt.update(outcome='conflict', reason=error)
                    continue
                for target in targets:
                    cid = int(target['concept_id'])
                    if cid in existing_concepts:
                        continue
                    for field, model in references.items():
                        if target[field] in existing_refs[field]:
                            continue
                        data = payload['metadata'][field][target[field]]
                        fields = REFERENCE_FIELDS[field][1]
                        new_refs[field][target[field]] = model(pk=target[field], **{f: data[f] for f in fields})
                    new_concepts[cid] = Concept(concept_id=cid, **{k: target[k] for k in (
                        'concept_name', 'concept_code', 'vocabulary_id', 'domain_id', 'concept_class_id', 'standard_concept')},
                        invalid_reason=None, valid_start_date=_date(target['valid_start_date']), valid_end_date=_date(target['valid_end_date']))
                created = row is None
                if created:
                    row = Mapping(source_vocabulary_id=key[0], source_code=record['source_code'])
                prior_target = row.target_concept_id
                prior_choices = old_choices.get(row.pk, [])
                row.target_concept_id = int(targets[0]['concept_id']) if len(targets) == 1 else None
                row.source_concept_id = source.pk if source else None
                vocabs = {t['vocabulary_id'] for t in targets}
                domains = {(existing_concepts.get(int(t['concept_id'])) or new_concepts[int(t['concept_id'])]).domain_id
                           for t in targets}
                row.destination_vocabulary_id = next(iter(vocabs)) if len(vocabs) == 1 else ''
                row.domain_id = next(iter(domains)) if len(domains) == 1 else ''
                row.omop_table = TABLES.get(row.domain_id, '')
                multiple = len(targets) > 1
                row.status = 'proposed' if multiple else 'approved'
                row.origin, row.origin_system, row.source = 'import', 'athena-multiple' if multiple else 'athena', 'Athena'
                row.reviewed_at = None if multiple else timezone.now()
                row.updated_at = timezone.now()
                if not row.source_code_description:
                    row.source_code_description = record['source_code_description'][:255]
                note = (f'Athena recovery snapshot {payload["as_of"]} ({record["tier"]}): '
                        f'{record["lookup_vocabulary"]}:{record["source_code"]} Maps to {receipt["targets"]}. '
                        f'Previous proposed destination: {prior_target or "none"}. {record["reference"]}')
                if prior_choices:
                    note += ' Superseded destination choices: ' + ', '.join(sorted(prior_choices)) + '.'
                row.notes = '\n'.join(filter(None, [row.notes, note]))
                accepted.append((row, targets, created))
                receipt['outcome'] = ('would_load' if dry_run else 'loaded') + ('_multiple' if len(targets) > 1 else '')
            if dry_run:
                continue
            for field, model in references.items():
                model.objects.using(alias).bulk_create(list(new_refs[field].values()), batch_size=500)
            concepts.bulk_create(list(new_concepts.values()), batch_size=500)
            mappings.bulk_create([row for row, _, created in accepted if created], batch_size=250)
            mappings.bulk_update([row for row, _, created in accepted if not created], UPDATE_FIELDS, batch_size=100)
            candidates.filter(mapping_id__in=[row.pk for row, _, _ in accepted]).delete()
            candidates.bulk_create([Candidate(mapping_id=row.pk, target_concept_id=int(t['concept_id']),
                target_vocabulary_id=t['vocabulary_id'], target_concept_code=t['concept_code'], origins=['Athena'])
                for row, targets, _ in accepted for t in targets], batch_size=500)
    return receipts


def summarize(receipts):
    return dict(Counter(record['outcome'] for record in receipts))
