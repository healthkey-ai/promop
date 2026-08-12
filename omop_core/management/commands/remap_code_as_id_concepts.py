"""
Repoint clinical rows off concepts that used their own code as their id.

A concept whose ``concept_id`` equals its ``concept_code`` was not assigned by
OHDSI — it was minted locally by code doing ``concept_id = int(concept_code)``.
That single pattern is the root of #415: it duplicates the genuine concept, and
because it also raised ``MAX(concept_id)``, ``next_pk``'s self-heal adopted the
inflated maximum and allocated a further 141 mints behind it (cleaned up by
#436).

These rows sit BELOW that block, so #436 does not touch them.

Targeting uses two independent tests, both of which must hold:

  1. ``concept_id::text = concept_code`` — the minting signature.
  2. Zero participation in the vocabulary graph: no ``concept_relationship``,
     ``concept_synonym`` or ``concept_ancestor`` rows.

The second matters because the first alone is not conclusive. OMOP does author
placeholder concepts, and several candidates looked like they might be genuine —
UCUM "Invalid UCUM Concept, do not use", and a SNOMED qualifier series
("Improved", "Unchanged", "Deteriorated"). A genuine Athena concept always
participates in the graph; the real olaparib concept carries 63 relationships.
On staging all 24 candidates have zero, confirming every one is local.

Resolution is by ``(vocabulary_id, concept_code)`` — OMOP's natural key — never
by ``concept_name``. Rows with no genuine counterpart are reported and left
alone: they may be legitimate local content that needs moving to an ``HK-*``
vocabulary rather than deleting.

Run the writers fix (#453) first. Cleaning up before the producers stop just
refills the table.

Usage:
    python manage.py remap_code_as_id_concepts              # dry run (default)
    python manage.py remap_code_as_id_concepts --apply
    python manage.py remap_code_as_id_concepts --apply --keep-mints
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

CLINICAL_TABLES = [
    (ProcedureOccurrence, 'procedure_concept_id', 'procedure_source_concept_id', 'Procedure'),
    (DrugExposure, 'drug_concept_id', 'drug_source_concept_id', 'Drug'),
    (ConditionOccurrence, 'condition_concept_id', 'condition_source_concept_id', 'Condition'),
    (Measurement, 'measurement_concept_id', 'measurement_source_concept_id', 'Measurement'),
    (Observation, 'observation_concept_id', 'observation_source_concept_id', 'Observation'),
]


class Command(BaseCommand):
    help = 'Repoint clinical rows off code-as-id concepts and delete them (see #452).'

    def add_arguments(self, parser):
        parser.add_argument('--apply', action='store_true',
                            help='Write the changes. Without this the command only reports.')
        parser.add_argument('--dry-run', action='store_true',
                            help='Explicitly request a dry run (the default).')
        parser.add_argument('--keep-mints', action='store_true',
                            help='Leave the mint concepts in place after remapping.')

    def handle(self, *args, **options):
        apply_changes = options['apply']
        if apply_changes and options['dry_run']:
            raise CommandError('--apply and --dry-run are mutually exclusive.')

        if not apply_changes:
            self.stdout.write(self.style.WARNING(
                'DRY RUN — nothing will be written. Re-run with --apply.\n'))

        totals = {'rows': 0, 'concepts': 0, 'unresolved': 0, 'skipped_genuine': 0,
                  'mints_deleted': 0, 'mints_still_referenced': 0, 'stale': 0}

        try:
            with suppress_patient_record_refresh():
                pairs, unresolved, genuine = self._resolve_all()
                totals['unresolved'] = len(unresolved)
                totals['skipped_genuine'] = genuine

                person_ids = self._affected_person_ids([m for m, _, _ in pairs])
                # Marked before any write: _remap_one commits per concept, so an
                # abort mid-loop would otherwise leave rewritten rows that
                # backfill_patient_records can never find.
                totals['stale'] = self._mark_stale(person_ids, apply_changes)

                for mint, target, source_concept in pairs:
                    totals['rows'] += self._remap_one(mint, target, source_concept, apply_changes)
                    totals['concepts'] += 1

                if not options['keep_mints']:
                    self._delete_mints([m for m, _, _ in pairs], totals, apply_changes)

                if unresolved:
                    self.stdout.write('')
                    self.stdout.write(self.style.WARNING(
                        f'{len(unresolved)} concept(s) have no genuine counterpart and were '
                        f'left alone — they may be local content needing an HK-* home:'))
                    for mint in unresolved:
                        self.stdout.write(
                            f'  {mint.concept_id} {mint.vocabulary_id} '
                            f'{mint.concept_name[:44]}')
        finally:
            self._summarise(totals, apply_changes)

    # ------------------------------------------------------------------

    def _candidates(self):
        """Code-as-id rows with no vocabulary participation.

        Both tests are required. `concept_id == concept_code` alone is not
        conclusive — OMOP authors placeholder concepts, and several candidates
        looked plausibly genuine. A real Athena concept always carries
        relationships, synonyms or ancestry; a mint carries none.
        """
        with connection.cursor() as cur:
            cur.execute("""
                SELECT c.concept_id FROM concept c
                WHERE c.concept_id::text = c.concept_code
                  AND NOT EXISTS (SELECT 1 FROM concept_relationship r
                                  WHERE r.concept_id_1 = c.concept_id
                                     OR r.concept_id_2 = c.concept_id)
                  AND NOT EXISTS (SELECT 1 FROM concept_synonym s
                                  WHERE s.concept_id = c.concept_id)
                  AND NOT EXISTS (SELECT 1 FROM concept_ancestor a
                                  WHERE a.descendant_concept_id = c.concept_id
                                     OR a.ancestor_concept_id = c.concept_id)
                ORDER BY c.concept_id
            """)
            ids = [r[0] for r in cur.fetchall()]
        return list(Concept.objects.filter(concept_id__in=ids).order_by('concept_id'))

    def _resolve_all(self):
        """((mint, target, source_concept) list, unresolved list, genuine-skipped count)."""
        resolved, unresolved = [], []
        genuine_skipped = 0

        # Rows matching the code pattern but participating in the vocabulary are
        # genuine and must never be touched; counted so the operator sees the
        # predicate is doing more than a string comparison.
        with connection.cursor() as cur:
            cur.execute("SELECT count(*) FROM concept WHERE concept_id::text = concept_code")
            total_code_as_id = cur.fetchone()[0]

        candidates = self._candidates()
        genuine_skipped = total_code_as_id - len(candidates)

        for mint in candidates:
            natural_key_matches = list(
                Concept.objects
                .filter(vocabulary_id=mint.vocabulary_id, concept_code=mint.concept_code,
                        invalid_reason__isnull=True)
                .exclude(concept_id=mint.concept_id)
                .order_by('concept_id')[:3]
            )
            genuine = [
                concept for concept in natural_key_matches
                if self._has_vocabulary_participation(concept.concept_id)
            ][:2]
            if len(genuine) != 1:
                logger.warning(
                    'remap_code_as_id_concepts unresolved concept_id=%s vocabulary=%s '
                    'code=%s candidates=%s', mint.concept_id, mint.vocabulary_id,
                    mint.concept_code, len(genuine))
                unresolved.append(mint)
                continue
            source_concept = genuine[0]
            resolved.append((mint, self._standard_for(source_concept), source_concept))
        return resolved, unresolved, genuine_skipped

    def _has_vocabulary_participation(self, concept_id):
        with connection.cursor() as cur:
            cur.execute("""
                SELECT EXISTS (
                    SELECT 1 FROM concept_relationship r
                    WHERE r.concept_id_1 = %s OR r.concept_id_2 = %s
                ) OR EXISTS (
                    SELECT 1 FROM concept_synonym s
                    WHERE s.concept_id = %s
                ) OR EXISTS (
                    SELECT 1 FROM concept_ancestor a
                    WHERE a.descendant_concept_id = %s OR a.ancestor_concept_id = %s
                )
            """, [concept_id, concept_id, concept_id, concept_id, concept_id])
            return cur.fetchone()[0]

    def _standard_for(self, genuine):
        """The Standard concept a clinical row should point at.

        OMOP: *_concept_id holds a Standard concept. Where the genuine concept
        is non-standard, follow its single 'Maps to' edge, filtering
        invalid_reason on both the relationship and the target as every other
        relationship query in this repo does. With no usable edge, use the
        genuine concept anyway — still far better than a mint — and say so.
        """
        if genuine.standard_concept == 'S':
            return genuine
        with connection.cursor() as cur:
            cur.execute(
                "SELECT DISTINCT cr.concept_id_2 FROM concept_relationship cr "
                "JOIN concept t ON t.concept_id = cr.concept_id_2 "
                "WHERE cr.concept_id_1 = %s AND cr.relationship_id = 'Maps to' "
                "AND t.standard_concept = 'S' "
                "AND cr.invalid_reason IS NULL AND t.invalid_reason IS NULL",
                [genuine.concept_id])
            targets = [r[0] for r in cur.fetchall()]
        if len(targets) == 1:
            return Concept.objects.filter(concept_id=targets[0]).first() or genuine
        return genuine

    def _affected_person_ids(self, mints):
        ids = set()
        mint_ids = [m.concept_id for m in mints]
        for model, concept_col, source_col, _domain in CLINICAL_TABLES:
            for col in (concept_col, source_col):
                ids |= set(model.objects.filter(**{f'{col}__in': mint_ids})
                           .values_list('person_id', flat=True).distinct())
        return ids

    def _remap_one(self, mint, target, source_concept, apply_changes):
        total = 0
        for model, concept_col, source_col, expected_domain in CLINICAL_TABLES:
            qs = model.objects.filter(**{concept_col: mint.concept_id})
            n = qs.count()
            if n and target.domain_id != expected_domain:
                self.stdout.write(self.style.WARNING(
                    f'  {mint.concept_id}: {n} {model.__name__} row(s) -> '
                    f'{target.concept_id} in domain {target.domain_id!r}, '
                    f'expected {expected_domain!r}'))
            total += n
            if n and apply_changes:
                with transaction.atomic():
                    qs.filter(**{f'{source_col}__isnull': True}).update(
                        **{source_col: source_concept.concept_id})
                    qs.filter(**{source_col: mint.concept_id}).update(
                        **{source_col: source_concept.concept_id})
                    qs.update(**{concept_col: target.concept_id})

            # Rows holding the mint only in the source column are not in `qs`,
            # so they survive and block deletion unless handled here.
            source_only = model.objects.filter(
                **{source_col: mint.concept_id}).exclude(**{concept_col: mint.concept_id})
            n_source = source_only.count()
            if n_source:
                total += n_source
                if apply_changes:
                    with transaction.atomic():
                        source_only.update(**{source_col: source_concept.concept_id})

        if total:
            note = '' if target.concept_id == source_concept.concept_id else \
                   f' (via Maps to from {source_concept.concept_id})'
            if target.concept_id == source_concept.concept_id and \
                    source_concept.standard_concept != 'S':
                note = self.style.WARNING(' [NON-STANDARD: no Maps to edge]')
            self.stdout.write(
                f'  {mint.concept_id} {mint.vocabulary_id:7s} {total:6d} rows -> '
                f'{target.concept_id}{note}')
        return total

    def _delete_mints(self, mints, totals, apply_changes):
        self.stdout.write('')
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
        verb = 'deleted' if apply_changes else 'would delete'
        self.stdout.write(f'  {verb} {totals["mints_deleted"]} mint concept(s)')

    def _referring_fields_with_rows(self, mint_ids):
        fields = []
        for rel in Concept._meta.related_objects:
            model = rel.related_model
            column = rel.field.attname
            if model is Concept:
                continue
            try:
                with transaction.atomic():
                    if model.objects.filter(**{f'{column}__in': mint_ids}).exists():
                        fields.append((model, column))
            except Exception:
                logger.warning('remap_code_as_id_concepts could not probe %s.%s',
                               model.__name__, column)
        return fields

    def _residual_references(self, mint_id):
        claimed = {(model, col) for model, concept_col, source_col, _domain in CLINICAL_TABLES
                   for col in (concept_col, source_col)}
        blocking, nulled = [], []
        for model, column in self._referring_fields:
            if (model, column) in claimed:
                continue
            n = model.objects.filter(**{column: mint_id}).count()
            if not n:
                continue
            on_delete = self._on_delete_for(model, column)
            (nulled if on_delete == 'SET_NULL' else blocking).append(
                f'{model.__name__}.{column}={n}')
        return blocking, nulled

    def _set_null_referrers(self, concept_id):
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

    def _mark_stale(self, person_ids, apply_changes):
        qs = PatientRecord.objects.filter(person_id__in=person_ids)
        if not apply_changes:
            return qs.count()
        return qs.update(derivation_version=0)

    def _summarise(self, totals, apply_changes):
        self.stdout.write('')
        verb = 'Would remap' if not apply_changes else 'Remapped'
        self.stdout.write(self.style.SUCCESS(
            '{v} — clinical rows: {r}  |  concepts: {c}  |  mints removed: {m}  |  '
            'no counterpart: {u}  |  genuine (untouched): {g}  |  '
            'PatientRecords marked stale: {s}'.format(
                v=verb, r=totals['rows'], c=totals['concepts'], m=totals['mints_deleted'],
                u=totals['unresolved'], g=totals['skipped_genuine'], s=totals['stale'])))
        if totals['mints_still_referenced']:
            self.stdout.write(self.style.WARNING(
                f"{totals['mints_still_referenced']} mint(s) still referenced."))
        if not apply_changes:
            self.stdout.write(self.style.WARNING('Nothing was written. Re-run with --apply.'))
        else:
            self.stdout.write(self.style.WARNING(
                'Run `manage.py backfill_patient_records` to re-derive the affected records.'))
