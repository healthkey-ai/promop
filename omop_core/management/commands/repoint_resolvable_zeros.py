"""Safely re-point concept-0 clinical rows whose source value is unambiguous.

This repairs facts written before their vocabulary was loaded.  It is more
conservative than ingest: some historical rows lack source-system provenance,
so it never guesses between colliding code systems and leaves ambiguity at 0.
It never deletes or collapses rows; a bulk repair must not trade an unresolved
fact for a lost fact.
"""
from collections import Counter

from django.core.management.base import BaseCommand
from django.db import connection, transaction
from django.db.models import Count

from omop_core.models import Concept, PatientRecord, SourceCodeConceptMapping, resolve_concept_replacement
from omop_core.services.code_mapping import CLINICAL_TABLES, _QUARANTINE_TARGETS


NO_MATCHING_CONCEPT_ID = 0


def _valid_destination(concept, expected_domain):
    """Follow replacements and accept only a live standard concept in-table."""
    if concept is None:
        return None
    resolved, _chain = resolve_concept_replacement(concept.concept_id)
    if (
        resolved is None
        or resolved.standard_concept != 'S'
        or resolved.invalid_reason is not None
        or resolved.domain_id != expected_domain
    ):
        return None
    return resolved


def resolve_historical_value(source_value, table, source_vocabulary_id=''):
    """Return (concept, reason), never selecting a cross-domain collision.

    An approved mapping wins, but only if all equally matching approved mappings
    lead to the same safe destination.  Otherwise an exact code match is only
    safe when every viable candidate resolves to one destination.
    """
    expected_domain = _QUARANTINE_TARGETS[table][1]
    mappings = SourceCodeConceptMapping.objects.filter(
        source_code__iexact=source_value,
        source_vocabulary_id=source_vocabulary_id or '',
        status='approved',
        omop_table=table,
        target_concept__isnull=False,
    ).select_related('target_concept')
    mapping_exists = mappings.exists()
    mapped_destinations = [
        _valid_destination(mapping.target_concept, expected_domain)
        for mapping in mappings
    ]
    # A nonblank mapping is considered only for rows with matching source
    # provenance.  Within that system, one malformed sibling mapping makes the
    # code ambiguous; do not let a different, safe sibling silently win.
    if mapping_exists and any(candidate is None for candidate in mapped_destinations):
        return None, 'unsafe approved mappings'
    mapped = {candidate.concept_id for candidate in mapped_destinations if candidate is not None}
    if mapped:
        if len(mapped) == 1:
            return Concept.objects.get(concept_id=mapped.pop()), 'approved mapping'
        return None, 'ambiguous approved mappings'
    # An approved mapping is a curator decision.  A malformed, deprecated, or
    # wrong-domain one must be surfaced for repair, never bypassed with a
    # coincidental direct-code hit.
    if mapping_exists:
        return None, 'no safe approved mapping'

    direct = Concept.objects.filter(concept_code__iexact=source_value)
    if source_vocabulary_id:
        direct = direct.filter(vocabulary_id=source_vocabulary_id)
    resolved = {
        candidate.concept_id
        for concept in direct.iterator()
        for candidate in [_valid_destination(concept, expected_domain)]
        if candidate is not None
    }
    if len(resolved) == 1:
        return Concept.objects.get(concept_id=resolved.pop()), 'direct concept'
    if resolved:
        return None, 'ambiguous direct concepts'
    return None, 'no safe destination'


class Command(BaseCommand):
    help = 'Safely re-point concept-0 rows only where a live, standard, same-domain destination is unambiguous.'

    def add_arguments(self, parser):
        parser.add_argument('--apply', action='store_true', help='Write changes. Default is dry-run.')
        parser.add_argument('--table', choices=sorted(CLINICAL_TABLES), action='append', help='Repeat to restrict tables.')
        parser.add_argument('--limit', type=int, help='Optional maximum distinct values per table; omitted means no cap.')

    def handle(self, *args, **options):
        apply_changes = options['apply']
        tables = options['table'] or sorted(CLINICAL_TABLES)
        limit = options['limit']
        if limit is not None and limit <= 0:
            raise ValueError('--limit must be positive')

        self.stdout.write(
            f"{'APPLYING' if apply_changes else 'DRY RUN'} against database "
            f"{connection.settings_dict.get('HOST') or 'local'}/{connection.settings_dict['NAME']}"
        )
        totals = Counter()
        for table in tables:
            model, concept_col, source_col = CLINICAL_TABLES[table]
            source_concept_col = source_col.replace('_source_value', '_source_concept_id')
            groups = (
                model.objects.filter(**{concept_col: NO_MATCHING_CONCEPT_ID})
                .exclude(**{f'{source_col}__isnull': True})
                .exclude(**{source_col: ''})
                .values(source_col, source_concept_col, f'{source_concept_col}__vocabulary_id')
                .annotate(rows=Count('pk')).order_by(source_col, source_concept_col)
            )
            if limit is not None:
                groups = groups[:limit]
            self.stdout.write(f'\n{table}:')
            for group in groups.iterator() if limit is None else groups:
                source_value, count = group[source_col], group['rows']
                source_concept_id = group[source_concept_col]
                source_vocabulary_id = (
                    group[f'{source_concept_col}__vocabulary_id']
                    if source_concept_id not in (None, NO_MATCHING_CONCEPT_ID)
                    else ''
                ) or ''
                totals['values_seen'] += 1
                concept, reason = resolve_historical_value(source_value, table, source_vocabulary_id)
                provenance = source_vocabulary_id or 'uncoded'
                if concept is None:
                    totals[f'skipped_{reason}'] += 1
                    self.stdout.write(f'  skip {source_value!r} [{provenance}] ({count} rows): {reason}')
                    continue
                if not apply_changes:
                    totals['would_update'] += count
                    self.stdout.write(f'  would update {count} {source_value!r} [{provenance}] -> {concept.concept_id} ({reason})')
                    continue

                # Capture and lock exactly the rows this invocation owns.  A
                # concurrent upload remains at 0 for a later run rather than
                # being changed without marking its PatientRecord stale.
                with transaction.atomic():
                    locked = list(model.objects.select_for_update().filter(
                        **{concept_col: NO_MATCHING_CONCEPT_ID, source_col: source_value,
                           source_concept_col: source_concept_id},
                    ).values_list('pk', 'person_id'))
                    pks = [pk for pk, _person_id in locked]
                    person_ids = {person_id for _pk, person_id in locked}
                    if not pks:
                        continue
                    PatientRecord.objects.filter(person_id__in=person_ids).update(derivation_version=0)
                    model.objects.filter(pk__in=pks, **{concept_col: NO_MATCHING_CONCEPT_ID}).update(
                        **{concept_col: concept.concept_id},
                    )
                totals['updated'] += len(pks)
                totals['patients_marked_stale'] += len(person_ids)
                self.stdout.write(f'  updated {len(pks)} {source_value!r} [{provenance}] -> {concept.concept_id} ({reason})')

        verb = 'Updated' if apply_changes else 'Would update'
        self.stdout.write(self.style.SUCCESS(
            f'\n{verb} {totals["updated"] if apply_changes else totals["would_update"]} row(s) '
            f'from {totals["values_seen"]} distinct source value(s). '
            f'{totals["patients_marked_stale"] if apply_changes else 0} patient record(s) marked stale.'
        ))
        skipped = sum(value for key, value in totals.items() if key.startswith('skipped_'))
        if skipped:
            self.stdout.write(self.style.WARNING(
                f'Skipped {skipped} unsafe or ambiguous source value(s); no rows were changed for them.'
            ))
