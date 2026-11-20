"""Report code mappings whose destination concept has been retired (#1465).

A vocabulary release can retire a concept that an approved mapping already
points at, and nothing told anyone: the dialog shows a "destination is retired"
banner, but only to a curator who happens to open that row. This lists every
such mapping with the active replacement Athena records for it, if any.

Read-only. Re-pointing an approved mapping changes what ingest resolves a source
code to, so it is a curator's decision, made in the Edit Mapping dialog with
"Replace with active concept" -- this command finds the rows, it does not edit
them. Exits 1 when any are found, so a release checklist or CI step can gate on
it, on the pattern of audit_sct_history.
"""

from django.core.management.base import BaseCommand
from django.db.models import Q

from omop_core.models import SourceCodeConceptMapping, resolve_concept_replacement


def retired_destination_mappings(status=None):
    """Mappings whose target concept carries an invalid_reason, busiest first."""
    queryset = (
        SourceCodeConceptMapping.objects
        .filter(target_concept__isnull=False)
        .exclude(Q(target_concept__invalid_reason__isnull=True) | Q(target_concept__invalid_reason=''))
        .select_related('target_concept')
        .order_by('-occurrence_count', 'source_vocabulary_id', 'source_code')
    )
    if status:
        queryset = queryset.filter(status=status)
    return queryset


class Command(BaseCommand):
    help = 'List code mappings whose destination concept is retired, with its active replacement'

    def add_arguments(self, parser):
        parser.add_argument(
            '--status', choices=['proposed', 'approved', 'rejected'],
            help='Only mappings in this status (default: all)',
        )

    def handle(self, *args, **options):
        mappings = list(retired_destination_mappings(options.get('status')))
        if not mappings:
            self.stdout.write(self.style.SUCCESS('No mapping points at a retired concept.'))
            return

        for mapping in mappings:
            target = mapping.target_concept
            resolved, _chain = resolve_concept_replacement(target.concept_id)
            if resolved is not None and resolved.concept_id != target.concept_id \
                    and not resolved.invalid_reason:
                replacement = (
                    f'-> {resolved.concept_id} {resolved.vocabulary_id}:'
                    f'{resolved.concept_code} "{resolved.concept_name}"'
                )
            else:
                replacement = '-> no active replacement recorded'
            self.stdout.write(
                f'{mapping.status:9s} {mapping.source_vocabulary_id}:{mapping.source_code} '
                f'(seen {mapping.occurrence_count}) points at retired {target.concept_id} '
                f'{target.vocabulary_id}:{target.concept_code} "{target.concept_name}" '
                f'[{target.invalid_reason}] {replacement}'
            )
        self.stdout.write(self.style.WARNING(
            f'{len(mappings)} mapping(s) point at a retired concept. Re-point each in the '
            'Edit Mapping dialog: to the replacement shown, or by searching where none is recorded.'
        ))
        raise SystemExit(1)
