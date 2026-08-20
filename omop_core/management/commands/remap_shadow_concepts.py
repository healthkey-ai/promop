"""
Repoint clinical rows off concepts that shadow a genuine Athena concept.

A block of concepts at concept_id 392021009-392021287 duplicates real vocabulary
content: SNOMED procedures, CVX vaccines, and SNOMED concepts filed under 'sct'
(the FHIR system-URI form of http://snomed.info/sct rather than 'SNOMED').

Origin (see #415): one writer inserted a concept using the SNOMED *code* as the
concept_id — 392021009 'Lumpectomy of breast', duplicating the genuine 4213045.
That set MAX(concept_id) to 392021009, and next_pk's self-heal
(setval(GREATEST(last_value, MAX(concept_id)))) faithfully adopted it, so every
subsequent mint was allocated 392021010, 392021011, ... One bad insert, then 141
sequence-allocated mints trailing behind it.

The producer is NOT identified. enrich_breast_cancer_omop_data was minting
concepts at concept_id=int(concept_code) by the same pattern, but only ever for
seven hard-coded codes whose largest is 266919005 — below 392021009 — and only
under vocabulary_id='SNOMED', so it cannot account for this block nor for its
'sct' and CVX rows. It produced the code-as-id rows BELOW the block; those are a
separate cleanup.

So this command removes rows whose producer is unknown. That is a real risk: if
the producer still runs somewhere, the block refills. Two things make it
acceptable. The id-poisoning mechanism is inactive (with Athena loaded
MAX(concept_id) is ~2.02e9, so next_pk allocates in the OHDSI custom range), and
'sct' appears nowhere in the tree. Re-run the dry run after any bulk import to
confirm the block has not returned.

Resolution is by (vocabulary_id, concept_code), which is OMOP's natural key and
therefore genuine identity — NOT by concept_name. Name matching is what produced
Troponin T for a Troponin I code in seed_omop_concepts, and it is why the drug
remap in #427 used a hard-coded constant. Code matching within a vocabulary
carries no such risk, and every shadow here was verified to resolve to exactly
one target.

Where the genuine concept is itself non-standard, the row is pointed at its
'Maps to' Standard concept, per OMOP: *_concept_id holds a Standard concept and
*_source_concept_id records what was actually stated.

Usage:
    python manage.py remap_shadow_concepts              # dry run (default)
    python manage.py remap_shadow_concepts --apply
    python manage.py remap_shadow_concepts --apply --keep-mints
"""
import logging

from django.core.management.base import BaseCommand, CommandError
from django.db import IntegrityError, connection, transaction
from django.db.models import ProtectedError

from omop_core.models import (
    Concept, ConditionOccurrence, DrugExposure, Measurement, Observation,
    PatientRecord, ProcedureOccurrence,
)
from omop_core.signals import suppress_patient_record_refresh

logger = logging.getLogger(__name__)

# The contiguous block of sequence-allocated mints.
BLOCK_MIN, BLOCK_MAX = 392_021_000, 392_022_000

# Vocabularies in the block that shadow genuine Athena content, mapped to the
# vocabulary the genuine concept actually lives in. 'sct' is the FHIR system-URI
# form of SNOMED. Vocabularies NOT listed here (LOCAL, FHIR, HK-Regimen) are
# legitimately local and are left alone.
SHADOW_VOCABULARIES = {
    'SNOMED': 'SNOMED',
    'sct': 'SNOMED',
    'CVX': 'CVX',
}

# Clinical tables referencing concepts, with their concept and source-concept
# columns. measurement/observation carry no rows on this block today but are
# included so the command stays correct if that changes.
CLINICAL_TABLES = [
    (ProcedureOccurrence, 'procedure_concept_id', 'procedure_source_concept_id'),
    (DrugExposure, 'drug_concept_id', 'drug_source_concept_id'),
    (ConditionOccurrence, 'condition_concept_id', 'condition_source_concept_id'),
    (Measurement, 'measurement_concept_id', 'measurement_source_concept_id'),
    (Observation, 'observation_concept_id', 'observation_source_concept_id'),
]

# The OMOP domain each table's *_concept_id is expected to hold. A SNOMED
# assessment or finding code can resolve to an Observation- or Condition-domain
# concept while its rows sit in procedure_occurrence; writing it there is the
# same class of CDM violation this command fixes, and once the mint is deleted
# the original intent is unrecoverable. Reported rather than blocked, since the
# genuine concept is still a better target than the shadow.
EXPECTED_DOMAIN = {
    ProcedureOccurrence: 'Procedure',
    DrugExposure: 'Drug',
    ConditionOccurrence: 'Condition',
    Measurement: 'Measurement',
    Observation: 'Observation',
}



class Command(BaseCommand):
    help = 'Repoint clinical rows off shadow concepts onto genuine ones (see #415).'

    def add_arguments(self, parser):
        parser.add_argument('--apply', action='store_true',
                            help='Write the changes. Without this the command only reports.')
        parser.add_argument('--dry-run', action='store_true',
                            help='Explicitly request a dry run (the default).')
        parser.add_argument('--keep-mints', action='store_true',
                            help='Leave the shadow concepts in place after remapping.')

    def handle(self, *args, **options):
        apply_changes = options['apply']
        if apply_changes and options['dry_run']:
            raise CommandError('--apply and --dry-run are mutually exclusive.')

        if not apply_changes:
            self.stdout.write(self.style.WARNING(
                'DRY RUN — nothing will be written. Re-run with --apply.\n'))

        self._maps_to_reason = {}
        totals = {'rows': 0, 'concepts': 0, 'unresolved': 0,
                  'mints_deleted': 0, 'mints_still_referenced': 0, 'stale': 0}
        wrote = False

        try:
            with suppress_patient_record_refresh():
                pairs, totals['unresolved'] = self._resolve_all()
                person_ids = self._affected_person_ids([m for m, _, _ in pairs])

                # Marked before any write. _remap_one commits per concept, so
                # an abort inside the loop would otherwise leave rewritten rows
                # with no PatientRecord marked — and backfill_patient_records
                # selects on derivation_version, so they would never be
                # re-derived. Over-marking is harmless: it costs one redundant
                # re-derivation.
                totals['stale'] = self._mark_stale(person_ids, apply_changes)

                for mint, genuine, standard in pairs:
                    n = self._remap_one(mint, genuine, standard, apply_changes)
                    totals['rows'] += n
                    totals['concepts'] += 1
                    wrote |= bool(apply_changes and n)

                if not options['keep_mints']:
                    self._delete_mints([m for m, _, _ in pairs], totals, apply_changes)
        finally:
            self._summarise(totals, apply_changes, wrote)

    # ------------------------------------------------------------------

    def _shadow_qs(self):
        return Concept.objects.filter(
            concept_id__gte=BLOCK_MIN, concept_id__lte=BLOCK_MAX,
            vocabulary_id__in=SHADOW_VOCABULARIES,
        ).order_by('concept_id')

    def _resolve_all(self):
        """((mint, genuine, standard) list, unresolved count).

        Counted here rather than by re-querying afterwards: a post-run count
        returns 0 once the mints are deleted regardless of what was skipped, and
        with --keep-mints it counts the successfully remapped ones too.
        """
        resolved = []
        unresolved = 0
        for mint in self._shadow_qs():
            target_vocab = SHADOW_VOCABULARIES[mint.vocabulary_id]
            candidates = list(
                Concept.objects
                .filter(vocabulary_id=target_vocab, concept_code=mint.concept_code,
                        invalid_reason__isnull=True)
                .exclude(concept_id__gte=BLOCK_MIN, concept_id__lte=BLOCK_MAX)
                .order_by('concept_id')[:2]
            )
            if len(candidates) != 1:
                # [:2] answers "more than one?" cheaply; report it as such
                # rather than claiming an exact count it did not measure.
                how_many = 'no' if not candidates else 'multiple'
                self.stdout.write(self.style.ERROR(
                    f'  {mint.concept_id} {mint.vocabulary_id} {mint.concept_code}: '
                    f'{how_many} genuine candidates — skipped'))
                logger.warning(
                    'remap_shadow_concepts unresolved concept_id=%s vocabulary=%s '
                    'code=%s candidates=%s', mint.concept_id, mint.vocabulary_id,
                    mint.concept_code, len(candidates))
                unresolved += 1
                continue
            genuine = candidates[0]
            resolved.append((mint, genuine, self._standard_for(genuine)))
        return resolved, unresolved

    def _standard_for(self, genuine):
        """The Standard concept a clinical row should point at.

        OMOP: *_concept_id holds a Standard concept. Where the genuine concept
        is itself non-standard, follow its single 'Maps to' edge. If there is
        none, use the genuine concept anyway — still vastly better than a mint,
        and the caller is told.
        """
        if genuine.standard_concept == 'S':
            return genuine
        with connection.cursor() as cur:
            # invalid_reason must be filtered on BOTH the relationship and the
            # target: load_athena_vocabularies loads deprecated rows, and every
            # other relationship query in this repo excludes them
            # (patient_record_service.py, views.py). Without it a concept with
            # one deprecated and one current edge yields two rows and silently
            # falls back to the non-standard concept, and a concept with only a
            # deprecated edge is remapped onto a retired standard concept.
            cur.execute(
                "SELECT cr.concept_id_2 FROM concept_relationship cr "
                "JOIN concept t ON t.concept_id = cr.concept_id_2 "
                "WHERE cr.concept_id_1 = %s AND cr.relationship_id = 'Maps to' "
                "AND t.standard_concept = 'S' "
                "AND cr.invalid_reason IS NULL AND t.invalid_reason IS NULL",
                [genuine.concept_id])
            rows = cur.fetchall()
        targets = {r[0] for r in rows}   # DISTINCT: the join can repeat a target
        if len(targets) == 1:
            return Concept.objects.filter(concept_id=targets.pop()).first() or genuine
        # 0 targets = no mapping; >1 = a genuine ambiguity OMOP permits. Both fall
        # back, but they are different situations and the operator is told which.
        self._maps_to_reason[genuine.concept_id] = (
            'no Maps to edge' if not targets else f'{len(targets)} Maps to targets')
        return genuine

    def _affected_person_ids(self, mints):
        ids = set()
        mint_ids = [m.concept_id for m in mints]
        for model, concept_col, source_col in CLINICAL_TABLES:
            # Only the columns this command rewrites. Including type/unit/value
            # columns zeroed derivation_version for persons whose rows are never
            # touched, forcing needless re-derivation.
            for col in (concept_col, source_col):
                ids |= set(model.objects.filter(**{f'{col}__in': mint_ids})
                           .values_list('person_id', flat=True).distinct())
        return ids

    def _remap_one(self, mint, genuine, standard, apply_changes):
        total = 0
        for model, concept_col, source_col in CLINICAL_TABLES:
            qs = model.objects.filter(**{concept_col: mint.concept_id})
            n = qs.count()
            if n and standard.domain_id != EXPECTED_DOMAIN.get(model):
                self.stdout.write(self.style.WARNING(
                    f'  {mint.concept_id}: {n} {model.__name__} row(s) -> '
                    f'{standard.concept_id} in domain {standard.domain_id!r}, '
                    f'expected {EXPECTED_DOMAIN.get(model)!r}'))
            total += n
            # No `continue` when n == 0: the source-only block below handles rows
            # whose *_source_concept_id holds the mint while *_concept_id does
            # not, which is precisely the case with zero concept_col hits.
            # Short-circuiting here made that block unreachable.
            if n and apply_changes:
                with transaction.atomic():
                    # Only claim source_col where it is empty or still points at
                    # the mint. Today's writers set source == concept for these
                    # tables so this is equivalent, but Measurement/Observation
                    # are in this list precisely so the command stays correct if
                    # that changes — and there, overwriting would discard a
                    # genuinely different source concept.
                    qs.filter(**{f'{source_col}__isnull': True}).update(
                        **{source_col: genuine.concept_id})
                    qs.filter(**{source_col: mint.concept_id}).update(
                        **{source_col: genuine.concept_id})
                    qs.update(**{concept_col: standard.concept_id})

            # Rows whose *_source_concept_id holds the mint but whose
            # *_concept_id does not are never in `qs`, so they survive and block
            # deletion. Repointing a SOURCE column at the genuine concept is
            # unambiguously right: it records what was stated, and the mint was
            # only ever a stand-in for that concept. Type/unit/value columns are
            # deliberately NOT touched — a mint in those is anomalous and the
            # correct value is not inferable, so it is reported instead.
            # Exclude rows already counted via concept_col: writers commonly set
            # source == concept (views.py drug_source_concept=drug_concept), so
            # counting both would double-count them in the dry run, where
            # nothing has been rewritten yet.
            source_only = model.objects.filter(
                **{source_col: mint.concept_id}).exclude(**{concept_col: mint.concept_id})
            n_source = source_only.count()
            if n_source:
                total += n_source
                if apply_changes:
                    with transaction.atomic():
                        source_only.update(**{source_col: genuine.concept_id})
        if total:
            if standard.concept_id != genuine.concept_id:
                note = f' (via Maps to from {genuine.concept_id})'
            elif genuine.standard_concept != 'S':
                # No usable 'Maps to' edge. The row still improves — it points at
                # a genuine concept rather than a mint — but *_concept_id is left
                # non-standard, which is an OMOP violation the operator must see
                # rather than have buried.
                reason = self._maps_to_reason.get(genuine.concept_id, 'not standard')
                note = self.style.WARNING(f' [NON-STANDARD: {reason}]')
            else:
                note = ''
            self.stdout.write(
                f'  {mint.concept_id} {mint.vocabulary_id:7s} {mint.concept_code[:14]:16s} '
                f'{total:5d} rows -> {standard.concept_id}{note}')
        return total

    def _referring_fields_with_rows(self, mint_ids):
        """(model, column) pairs anywhere in the schema that reference these mints.

        Concept has 112 incoming FK fields, 97 of them PROTECT. Checking only the
        five clinical tables under-projects: a single ConditionEra, DrugEra,
        Episode or VisitOccurrence row pointing at a mint blocks its deletion,
        and the dry run would still have promised it. Django's own metadata is
        the only complete list.

        Probed once for the whole block rather than per mint — 112 fields x 100
        mints of individual counts would be unusably slow against a remote
        database.
        """
        fields = []
        for rel in Concept._meta.related_objects:
            model = rel.related_model
            column = rel.field.attname
            if model is Concept:
                continue
            # Each probe gets its own savepoint. Without one, a failing query
            # (unmanaged model, missing table) aborts the surrounding
            # transaction, and catching the exception does not un-abort it —
            # every subsequent query then fails with "current transaction is
            # aborted", cascading far beyond this method.
            try:
                with transaction.atomic():
                    if model.objects.filter(**{f'{column}__in': mint_ids}).exists():
                        fields.append((model, column))
            except Exception:  # unmanaged or otherwise unqueryable relation
                logger.warning('remap_shadow_concepts could not probe %s.%s',
                               model.__name__, column)
        return fields

    def _residual_references(self, mint_id):
        """References that would REMAIN after the remap, by column.

        The remap claims *_concept_id and *_source_concept_id. Anything holding
        the mint in a type/unit/value column survives and blocks deletion, since
        the correct replacement there is not inferable. Reporting 'would be
        removed' without checking these is how a dry run promises a deletion
        that then fails.
        """
        claimed = {(model, col) for model, concept_col, source_col in CLINICAL_TABLES
                   for col in (concept_col, source_col)}
        blocking, nulled = [], []
        for model, column in self._referring_fields:
            if (model, column) in claimed:
                continue   # the remap rewrites these
            n = model.objects.filter(**{column: mint_id}).count()
            if not n:
                continue
            # SET_NULL referrers do not block the delete — Django nulls them.
            # Reporting them as blocking made the dry run say 'not removable'
            # where --apply deletes the concept and silently mutates the other
            # table. They are reported separately so that mutation is visible.
            on_delete = self._on_delete_for(model, column)
            (nulled if on_delete == 'SET_NULL' else blocking).append(
                f'{model.__name__}.{column}={n}')
        return blocking, nulled

    def _set_null_referrers(self, concept_id):
        """SET_NULL columns currently holding this concept, for disclosure."""
        found = []
        for rel in Concept._meta.related_objects:
            model, column = rel.related_model, rel.field.attname
            if model is Concept or rel.field.remote_field.on_delete.__name__ != 'SET_NULL':
                continue
            try:
                with transaction.atomic():
                    n = model.objects.filter(**{column: concept_id}).count()
            except Exception:
                continue
            if n:
                found.append(f'{model.__name__}.{column}={n}')
        return found

    @staticmethod
    def _on_delete_for(model, column):
        for field in model._meta.get_fields():
            if getattr(field, 'attname', None) == column and field.remote_field:
                return field.remote_field.on_delete.__name__
        return '?'

    def _delete_mints(self, mints, totals, apply_changes):
        self.stdout.write('')
        # ~112 savepoint-wrapped EXISTS probes; only the dry run reads the
        # result, so do not pay for them on an apply run.
        self._referring_fields = (
            [] if apply_changes
            else self._referring_fields_with_rows([m.concept_id for m in mints]))
        for mint in mints:
            if not apply_changes:
                blocking, nulled = self._residual_references(mint.concept_id)
                if nulled:
                    self.stdout.write(self.style.WARNING(
                        f'  {mint.concept_id}: deleting will NULL '
                        f'{", ".join(nulled)} (on_delete=SET_NULL)'))
                if blocking:
                    self.stdout.write(self.style.WARNING(
                        f'  {mint.concept_id}: would remain referenced by '
                        f'{", ".join(blocking)} — not removable'))
                    totals['mints_still_referenced'] += 1
                    continue
                totals['mints_deleted'] += 1
                continue

            # Disclosed on apply too: RegimenMappingGap.matched_concept and
            # .quarantine_concept are SET_NULL, so deleting silently nulls them.
            # Reporting that only in the mode that does not mutate was backwards.
            nulled_on_apply = self._set_null_referrers(mint.concept_id)
            try:
                with transaction.atomic():
                    # Only 97 of the 112 Concept FK fields use PROTECT; a
                    # DO_NOTHING reference would otherwise fail at COMMIT of the
                    # outer transaction, past every handler here.
                    with connection.cursor() as cur:
                        cur.execute('SET CONSTRAINTS ALL IMMEDIATE')
                    Concept.objects.filter(concept_id=mint.concept_id).delete()
            except (ProtectedError, IntegrityError) as exc:
                self.stdout.write(self.style.WARNING(
                    f'  {mint.concept_id}: still referenced, left in place '
                    f'({type(exc).__name__})'))
                totals['mints_still_referenced'] += 1
            else:
                if nulled_on_apply:
                    self.stdout.write(self.style.WARNING(
                        f'  {mint.concept_id}: deleted — NULLed '
                        f'{", ".join(nulled_on_apply)}'))
                totals['mints_deleted'] += 1
        if apply_changes:
            self.stdout.write(f'  deleted {totals["mints_deleted"]} shadow concept(s)')
        else:
            self.stdout.write(f'  would delete {totals["mints_deleted"]} shadow concept(s)')

    def _mark_stale(self, person_ids, apply_changes):
        qs = PatientRecord.objects.filter(person_id__in=person_ids)
        if not apply_changes:
            return qs.count()
        return qs.update(derivation_version=0)

    def _summarise(self, totals, apply_changes, wrote):
        self.stdout.write('')
        verb = 'Would remap' if not apply_changes else 'Remapped'
        self.stdout.write(self.style.SUCCESS(
            '{v} — clinical rows: {r}  |  shadow concepts: {c}  |  '
            'mints removed: {m}  |  skipped (unresolved): {u}  |  '
            'PatientRecords marked stale: {s}'.format(
                v=verb, r=totals['rows'], c=totals['concepts'],
                m=totals['mints_deleted'], u=totals['unresolved'],
                s=totals['stale'])))
        if totals['mints_still_referenced']:
            self.stdout.write(self.style.WARNING(
                f"{totals['mints_still_referenced']} shadow concept(s) still referenced."))
        if not apply_changes:
            self.stdout.write(self.style.WARNING('Nothing was written. Re-run with --apply.'))
        elif wrote:
            self.stdout.write(self.style.WARNING(
                'Run `manage.py backfill_patient_records` to re-derive the affected records.'))
