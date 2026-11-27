"""Derive the records of the people who already have facts at a created field's concept (#2941).

What the worker does after a created field is created, restored or re-mapped, run in the
foreground: for a deployment with no Celery broker (where nothing is queued), or to redo
it by hand. Writes to ``DATABASE_URL``; name the database explicitly.
"""
from django.core.management.base import BaseCommand, CommandError

from omop_core.models import CustomPatientField
from omop_core.services import custom_field_rederive


class Command(BaseCommand):
    help = "Re-derive the PatientRecords of everyone with a fact at a created field's concept."

    def add_arguments(self, parser):
        parser.add_argument('field_name', help='The created field, by name.')
        parser.add_argument('--dry-run', action='store_true',
                            help='Count the people that would be derived; derive nobody.')
        parser.add_argument('--limit', type=int, default=0,
                            help='Stop after this many people (default: everyone).')

    def handle(self, *args, **options):
        field = (CustomPatientField.objects.active().select_related('mapping')
                 .filter(field_name=options['field_name']).first())
        if field is None:
            raise CommandError(f"No active created field named {options['field_name']!r}.")
        if field.mode != 'editable' or field.mapping.status != 'approved':
            raise CommandError('The field is computed or its mapping is not approved; it reads no facts.')
        mapping = field.mapping
        chunk = custom_field_rederive.CHUNK
        after, total, errors = 0, 0, 0
        remaining = options['limit'] or None
        while True:
            size = chunk if remaining is None else min(chunk, remaining - total)
            if size <= 0:
                break
            if options['dry_run']:
                ids = custom_field_rederive.person_ids(mapping, after=after, limit=size)
                total += len(ids)
                if len(ids) < size:
                    break
                after = ids[-1]
                continue
            result = custom_field_rederive.run_chunk(mapping.pk, after, limit=size, budget=float('inf'))
            total += result['refreshed']
            errors += result['errors']
            if result['next_after'] is None:
                break
            after = result['next_after']
        verb = 'would be derived' if options['dry_run'] else 'derived'
        self.stdout.write(f'{total} records {verb}' + (f', {errors} failed' if errors else '') + '.')
        if errors:
            raise CommandError(f'{errors} records could not be derived; see the log.')
