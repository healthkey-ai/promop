"""Reconcile existing approved SCCM mappings against an original Athena export."""
from contextlib import nullcontext
from datetime import date

from django.db import transaction
from django.utils import timezone

from omop_core.data_migrations.athena_icd10_stcm_reconcile import _snapshot
from omop_core.models import Concept, SourceCodeConceptMapping as Mapping, Vocabulary
from omop_core.services.athena_destinations import active
from omop_core.services.source_vocabularies import DOMAIN_TO_TABLE
from omop_core.services.stcm_mapping_audit import mapping_key

UPDATE_FIELDS = ['target_concept', 'destination_vocabulary_id', 'domain_id', 'omop_table',
                 'origin_system', 'source', 'notes', 'updated_at']
REPORT_FIELDS = ['mapping_id', 'source_vocabulary', 'source_code', 'lookup_vocabulary',
                 'prior_provenance', 'prior_target_id', 'athena_source_id', 'athena_target_ids',
                 'proposed_target_id', 'proposed_vocabulary', 'proposed_domain',
                 'outcome', 'reason', 'reference']


def _receipt(row, evidence):
    return dict(mapping_id=row.pk, source_vocabulary=row.source_vocabulary_id,
                source_code=row.source_code, lookup_vocabulary=mapping_key(row)[0],
                prior_provenance=row.origin_system, prior_target_id=row.target_concept_id,
                athena_source_id=evidence.source['concept_id'] if evidence.source else '',
                athena_target_ids=';'.join(str(t['concept_id']) for t in evidence.targets),
                proposed_target_id='', proposed_vocabulary='', proposed_domain='',
                outcome='', reason='', reference=evidence.reference)


def reconcile_batch(originals, provider, *, using='default', apply=False):
    """Use complete export target sets; never infer Athena origin from local CR.

    Evidence is read before this function, without holding any database locks.
    Apply rechecks the whole SCCM snapshot and current destination metadata while
    holding row locks. Dry-run does not open transactions or acquire row locks.
    """
    today = date.today()
    items = [(row, provider.lookup(*mapping_key(row), today)) for row in originals]
    target_ids = {int(t['concept_id']) for _, evidence in items for t in evidence.targets}
    with transaction.atomic(using=using) if apply else nullcontext():
        current = {row.pk: row for row in originals}
        concepts = Concept.objects.using(using).filter(pk__in=target_ids).order_by('pk')
        if apply:
            current = Mapping.objects.using(using).select_for_update().filter(
                pk__in=current).order_by('pk').in_bulk()
            concepts = concepts.select_for_update()
        concepts = concepts.in_bulk()
        deprecated = set(Vocabulary.objects.using(using).filter(
            pk__in={t.vocabulary_id for t in concepts.values()}, is_deprecated=True,
        ).values_list('pk', flat=True))
        receipts, updates = [], []
        for original, evidence in items:
            receipt = _receipt(original, evidence)
            receipts.append(receipt)
            row = current.get(original.pk)
            if row is None or _snapshot(row) != _snapshot(original):
                receipt.update(outcome='changed_during_audit', reason='Mapping was edited or removed')
                continue
            if row.status != 'approved' or row.origin_system == 'athena':
                receipt.update(outcome='ineligible', reason='Requires an approved non-Athena mapping')
                continue
            if row.locked_by_id is not None:
                receipt.update(outcome='locked', reason='Curator edit lock is present')
                continue
            if evidence.reason != 'found' or len(evidence.targets) != 1:
                receipt.update(outcome=evidence.reason, reason='Export must supply exactly one current standard destination')
                continue
            key = mapping_key(row)
            source = evidence.source
            if (not source or source['vocabulary_id'] != key[0]
                    or source['concept_code'].strip().upper() != key[1] or not active(source, today)):
                receipt.update(outcome='source_conflict', reason='Export source does not match the lookup identity')
                continue
            target = evidence.targets[0]
            target_id = int(target['concept_id'])
            receipt.update(proposed_target_id=target_id, proposed_vocabulary=target['vocabulary_id'],
                           proposed_domain=target['domain_id'])
            if target['standard_concept'] != 'S' or not active(target, today):
                receipt.update(outcome='invalid_export_target', reason='Export destination is not currently standard and valid')
                continue
            local = concepts.get(target_id)
            if local is None:
                receipt.update(outcome='missing_local_target', reason='Load the Athena destination concept before applying')
                continue
            differences = [field for field in ('vocabulary_id', 'concept_code', 'domain_id')
                           if getattr(local, field) != target[field]]
            if differences:
                receipt.update(outcome='local_target_conflict', reason='Local destination differs from export: ' + ', '.join(differences))
                continue
            if (local.source or local.standard_concept != 'S' or local.invalid_reason
                    or not local.valid_start_date <= today <= local.valid_end_date
                    or local.vocabulary_id in deprecated):
                receipt.update(outcome='invalid_local_target', reason='Local destination must be external, standard, active and in an active vocabulary')
                continue
            if local.domain_id not in DOMAIN_TO_TABLE:
                receipt.update(outcome='unsupported_domain', reason='Destination domain has no clinical import table')
                continue
            outcome = 'reattributed' if row.target_concept_id == target_id else 'destination_changed'
            receipt.update(outcome=outcome if apply else (
                'would_reattribute' if outcome == 'reattributed' else 'would_change_destination'),
                reason='One current standard destination verified in the original Athena export')
            if not apply:
                continue
            row.target_concept_id = target_id
            row.destination_vocabulary_id = local.vocabulary_id
            row.domain_id = local.domain_id
            row.omop_table = DOMAIN_TO_TABLE[local.domain_id]
            row.origin_system, row.source = 'athena', 'Athena'
            row.updated_at = timezone.now()
            note = (f'Athena export reconciliation {today}: {key[0]}:{row.source_code} '
                    f'Maps to {target_id}; previous target {receipt["prior_target_id"]}, '
                    f'previous provenance {receipt["prior_provenance"]!r}. {evidence.reference}')
            row.notes = '\n'.join(filter(None, [row.notes, note]))
            updates.append(row)
        if updates:
            # No approval hooks, candidate rewrites, or patient/clinical updates.
            Mapping.objects.using(using).bulk_update(updates, UPDATE_FIELDS, batch_size=250)
    return receipts
