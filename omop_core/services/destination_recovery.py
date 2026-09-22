"""Reconcile Athena evidence with unchanged, unreviewed imported proposals."""
from django.db import transaction
from django.db.models import Q
from django.db.models.functions import Lower, Trim
from django.utils import timezone

from omop_core.models import Concept, ConceptClass, Domain, MappingDestinationCandidate, SourceCodeConceptMapping, Vocabulary
from omop_core.services.athena_destinations import LookupFailure, active, parse_date
from omop_core.services.source_vocabularies import DOMAIN_TO_TABLE

REFERENCE_TABLES = (
    ('domain_id', Domain, ('domain_name', 'domain_concept_id')),
    ('vocabulary_id', Vocabulary, ('vocabulary_name', 'vocabulary_reference',
                                 'vocabulary_version', 'vocabulary_concept_id')),
    ('concept_class_id', ConceptClass, ('concept_class_name', 'concept_class_concept_id')),
)
UPDATE_FIELDS = ['target_concept', 'source_concept', 'destination_vocabulary_id', 'domain_id',
                 'omop_table', 'status', 'origin', 'origin_system', 'source', 'reviewed_at',
                 'notes', 'source_code_description', 'updated_at']


def recoverable_mappings():
    return SourceCodeConceptMapping.objects.filter(
        Q(target_concept_id__isnull=True) | Q(target_concept_id=0)
        | Q(origin='import', created_by_id__isnull=True),
        status__in=['proposed', 'none', ''], reviewer_id__isnull=True,
        reviewed_at__isnull=True, updated_by_id__isnull=True, locked_by_id__isnull=True,
    ).exclude(origin_system__istartswith='curator').exclude(origin_system='athena-multiple').exclude(source_vocabulary_id='').exclude(source_code='')


def validate_concept(row, today):
    try:
        if not 0 < int(row['concept_id']) < 2_000_000_000:
            raise ValueError('Not an external OMOP concept ID')
        for key, size in [('concept_name', 255), ('concept_code', 50), ('vocabulary_id', 20),
                          ('domain_id', 20), ('concept_class_id', 20)]:
            if not isinstance(row[key], str) or not row[key] or len(row[key]) > size:
                raise ValueError(f'Invalid {key}')
        if row['standard_concept'] != 'S' or not active(row, today):
            raise ValueError('Destination is not currently standard and valid')
    except (KeyError, TypeError, ValueError) as exc:
        raise LookupFailure(str(exc)) from exc


class DestinationPlan:
    """Bounded batch prefetch; no database requests inside the per-code loop."""
    def __init__(self, items, today, dry_run):
        self.today = today
        targets = [target for item in items for target in item['evidence'].targets]
        ids = {int(target['concept_id']) for target in targets}
        ids.update(int(item['evidence'].source['concept_id']) for item in items if item['evidence'].source)
        concepts = Concept.objects.filter(pk__in=ids)
        if not dry_run:
            concepts = concepts.select_for_update()
        self.concepts = concepts.in_bulk()
        self.natural_ids = {(vocab, code): cid for vocab, code, cid in Concept.objects.filter(
            vocabulary_id__in={t['vocabulary_id'] for t in targets},
            concept_code__in={t['concept_code'] for t in targets},
        ).values_list('vocabulary_id', 'concept_code', 'pk')}
        self.references = {}
        for key, model, _ in REFERENCE_TABLES:
            query = model.objects.filter(pk__in={t[key] for t in targets})
            if not dry_run:
                query = query.select_for_update()
            self.references[key] = query.in_bulk()
        self.new_concepts, self.new_references = {}, {key: {} for key, _, _ in REFERENCE_TABLES}

    def validate_targets(self, targets, metadata):
        concepts, references = {}, {key: {} for key, _, _ in REFERENCE_TABLES}
        for target in targets:
            validate_concept(target, self.today)
            cid = int(target['concept_id'])
            existing = self.concepts.get(cid) or self.new_concepts.get(cid)
            vocabulary = self.references['vocabulary_id'].get(target['vocabulary_id'])
            if vocabulary and vocabulary.is_deprecated:
                raise LookupFailure('Destination vocabulary is deprecated locally')
            if existing:
                if (existing.vocabulary_id != target['vocabulary_id'] or existing.concept_code != target['concept_code']
                        or existing.domain_id != target['domain_id'] or existing.source
                        or existing.standard_concept != 'S' or existing.invalid_reason
                        or not existing.valid_start_date <= self.today <= existing.valid_end_date):
                    raise LookupFailure('Local destination conflicts with Athena evidence; refresh/review vocabulary first')
                continue
            key_pair = (target['vocabulary_id'], target['concept_code'])
            other_id = self.natural_ids.get(key_pair)
            if other_id is not None and other_id != cid:
                raise LookupFailure('Destination code already has a different local concept ID')
            for key, model, fields in REFERENCE_TABLES:
                if target[key] in self.references[key] or target[key] in self.new_references[key]:
                    continue
                reference = metadata.get(key, {}).get(target[key])
                if not reference or any(field not in reference for field in fields):
                    raise LookupFailure(f'Missing reference metadata for {key}={target[key]}; load the vocabulary first')
                references[key][target[key]] = model(pk=target[key], **{f: reference[f] for f in fields})
            concepts[cid] = Concept(concept_id=cid, concept_name=target['concept_name'],
                concept_code=target['concept_code'], vocabulary_id=target['vocabulary_id'],
                domain_id=target['domain_id'], concept_class_id=target['concept_class_id'],
                standard_concept='S', invalid_reason=None,
                valid_start_date=parse_date(target['valid_start_date']), valid_end_date=parse_date(target['valid_end_date']))
        # Merge only after every destination for this code passes validation.
        self.new_concepts.update(concepts)
        for key, rows in references.items():
            self.new_references[key].update(rows)
        self.natural_ids.update({(c.vocabulary_id, c.concept_code): c.pk for c in concepts.values()})


@transaction.atomic
def apply_evidence_batch(items, today, *, dry_run=False):
    """Commit one bounded batch; all external lookup precedes these row locks."""
    if not items:
        return {}
    ids = [item['snapshot']['id'] for item in items]
    current = recoverable_mappings().filter(pk__in=ids)
    if not dry_run:
        current = current.select_for_update()
    current = current.in_bulk()
    plan = DestinationPlan(items, today, dry_run)
    peers = SourceCodeConceptMapping.objects.filter(
        source_vocabulary_id__in={item['snapshot']['source_vocabulary_id'] for item in items},
    ).alias(code_normalized=Lower(Trim('source_code'))).filter(
        code_normalized__in={item['snapshot']['source_code'].strip().lower() for item in items},
    ).values('id', 'source_vocabulary_id', 'source_code', 'status', 'target_concept_id', 'reviewer_id', 'updated_by_id')
    peer_map = {}
    for peer in peers:
        peer_map.setdefault((peer['source_vocabulary_id'], peer['source_code'].strip().casefold()), []).append(peer)
    prior_candidates = {(c.mapping_id, c.target_vocabulary_id, c.target_concept_code): c
                        for c in MappingDestinationCandidate.objects.filter(mapping_id__in=ids)}
    results, updates, additions = {}, [], []
    now = timezone.now()
    for item in items:
        snapshot, evidence = item['snapshot'], item['evidence']
        row = current.get(snapshot['id'])
        outcome, reason = 'skipped_changed', ''
        if (row is None or row.updated_at != snapshot['updated_at']
                or (row.source_vocabulary_id, row.source_code) != (snapshot['source_vocabulary_id'], snapshot['source_code'])):
            results[snapshot['id']] = (outcome, reason)
            continue
        peers = peer_map.get((row.source_vocabulary_id, row.source_code.strip().casefold()), [])
        if any(peer['id'] != row.pk and (peer['status'] == 'approved' or peer['target_concept_id']
                or peer['reviewer_id'] or peer['updated_by_id']) for peer in peers):
            results[row.pk] = ('skipped_existing_mapping', '')
            continue
        try:
            if evidence.reason not in ('found', 'ambiguous_targets') or not evidence.targets or not evidence.source:
                raise LookupFailure('A unique source with authoritative Maps to destinations is required')
            source = evidence.source
            if (source['vocabulary_id'] != (item.get('lookup_vocabulary') or row.source_vocabulary_id)
                    or source['concept_code'].strip().casefold() != row.source_code.strip().casefold()
                    or not active(source, today)):
                raise LookupFailure('Source vocabulary/code or validity does not match the queue row')
            source_concept = plan.concepts.get(int(source['concept_id']))
            if source_concept and (source_concept.vocabulary_id != source['vocabulary_id']
                    or source_concept.concept_code != source['concept_code']):
                raise LookupFailure('Local source concept identity conflicts with Athena')
            plan.validate_targets(evidence.targets, item['metadata'])
        except (LookupFailure, ValueError, KeyError, TypeError) as exc:
            results[row.pk] = ('unmapped', str(exc))
            continue
        multiple = len(evidence.targets) > 1
        if dry_run:
            results[row.pk] = ('would_load_multiple' if multiple else 'would_load', '')
            continue
        target_ids = [int(target['concept_id']) for target in evidence.targets]
        previous_target_id = row.target_concept_id
        row.target_concept_id = target_ids[0] if not multiple else None
        row.source_concept = source_concept
        vocabularies = {target['vocabulary_id'] for target in evidence.targets}
        domains = {(plan.concepts.get(int(target['concept_id'])) or plan.new_concepts[int(target['concept_id'])]).domain_id
                   for target in evidence.targets}
        row.destination_vocabulary_id = next(iter(vocabularies)) if len(vocabularies) == 1 else ''
        row.domain_id = next(iter(domains)) if len(domains) == 1 else ''
        row.omop_table = DOMAIN_TO_TABLE.get(row.domain_id, '')
        row.status = 'proposed' if multiple else 'approved'
        row.origin, row.origin_system, row.source = 'import', 'athena-multiple' if multiple else 'athena', 'Athena'
        row.reviewed_at = None if multiple else now
        row.updated_at = now
        receipt = (f'Athena destination recovery ({item["tier"]}), {today.isoformat()}: '
                   f'{source["vocabulary_id"]}:{source["concept_code"]} ({source["concept_id"]}) Maps to '
                   f'{",".join(map(str, target_ids))}. Previous proposed destination: '
                   f'{previous_target_id or "none"}. {evidence.reference}')
        prior = [f'{vocab}:{code}' for (mapping_id, vocab, code) in prior_candidates if mapping_id == row.pk]
        if prior:
            receipt += ' Superseded destination choices: ' + ', '.join(sorted(prior)) + '.'
        row.notes = '\n'.join(filter(None, [row.notes, receipt]))
        if not row.source_code_description:
            row.source_code_description = source['concept_name'][:255]
        updates.append(row)
        for target, target_id in zip(evidence.targets, target_ids):
            additions.append(MappingDestinationCandidate(mapping=row,
                target_vocabulary_id=target['vocabulary_id'], target_concept_code=target['concept_code'],
                target_concept_id=target_id, origins=['Athena']))
        results[row.pk] = ('loaded_multiple' if multiple else 'loaded', '')
    if not dry_run:
        for key, model, _ in REFERENCE_TABLES:
            model.objects.bulk_create(list(plan.new_references[key].values()))
        Concept.objects.bulk_create(list(plan.new_concepts.values()))
        SourceCodeConceptMapping.objects.bulk_update(updates, UPDATE_FIELDS, batch_size=100)
        MappingDestinationCandidate.objects.filter(mapping_id__in=[row.pk for row in updates]).delete()
        MappingDestinationCandidate.objects.bulk_create(additions, batch_size=500)
    return results


def apply_evidence(snapshot, evidence, tier, metadata, today, *, dry_run=False, lookup_vocabulary=None):
    """Single-code entry point shares the batch validation and transaction."""
    outcome, reason = apply_evidence_batch([dict(snapshot=snapshot, evidence=evidence, tier=tier,
        metadata=metadata, lookup_vocabulary=lookup_vocabulary)], today, dry_run=dry_run)[snapshot['id']]
    if reason:
        raise LookupFailure(reason)
    return outcome
