"""Copy patients from another PRomop instance into this one.

Usage::

    SOURCE_DATABASE_URL="postgresql://..." manage.py copy_patient 12345 --org healthtree --dry-run
    SOURCE_DATABASE_URL="postgresql://..." manage.py copy_patient --filter-org-id 26 --org healthtree

Patients are selected on the source by ids, filters, or both. Each patient is
copied in its own transaction, so one failure does not undo the others. Ids are
allocated here, never taken from the source. Copying the same patient twice
needs --replace. Copy reference data first (copy_reference_data): concepts and
therapy regimens resolve by code.
"""
import os
from typing import Any

from django.core.management.base import BaseCommand, CommandError, CommandParser
from django.db import connections

from omop_core.models import Organization
from omop_core.services.instance_copy import SOURCE_ALIAS, register_source_connection
from omop_core.services.patient_transfer import (
    PatientCopyError,
    PatientCopyStats,
    apply_patient,
    read_patient,
    select_person_ids,
)


class Command(BaseCommand):
    help: str = 'Copy patients from the instance at SOURCE_DATABASE_URL into this one.'

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument('person_ids', nargs='*', type=int, help='person_id of each patient on the source.')
        parser.add_argument(
            '--filter-org-id', type=int,
            help='Only patients whose record belongs to this organization id on the source.',
        )
        parser.add_argument('--org', required=True, help='Slug of the organization here the patients join.')
        parser.add_argument(
            '--target-person-id', type=int,
            help='Force a person_id here instead of the next free one. Only for a single patient.',
        )
        parser.add_argument(
            '--replace', action='store_true',
            help='Re-copy a patient copied from this source before, replacing what is here.',
        )
        parser.add_argument('--source-url', help='Source database URL. Defaults to $SOURCE_DATABASE_URL.')
        parser.add_argument('--dry-run', action='store_true', help='Report what would change and roll back.')

    def handle(self, **options: Any) -> None:
        url = options.get('source_url') or os.environ.get('SOURCE_DATABASE_URL')
        if not url:
            raise CommandError('Set SOURCE_DATABASE_URL (or pass --source-url) to the source database.')
        organization = Organization.objects.filter(slug=options['org']).first()
        if organization is None:
            raise CommandError(f'No organization with slug {options["org"]!r} here.')

        register_source_connection(url)
        try:
            self._copy_all(organization, options)
        finally:
            connections[SOURCE_ALIAS].close()

    def _copy_all(self, organization: Organization, options: dict[str, Any]) -> None:
        """Copy every selected patient, report each one, then the totals."""
        try:
            person_ids = select_person_ids(
                SOURCE_ALIAS, options['person_ids'], organization_id=options['filter_org_id'],
            )
        except PatientCopyError as exc:
            raise CommandError(str(exc))
        missing = sorted(set(options['person_ids']) - set(person_ids))
        if missing:
            self.stdout.write(self.style.WARNING(f'  ! Not selected on the source: {missing}'))
        if options['target_person_id'] and len(person_ids) != 1:
            raise CommandError(f'--target-person-id needs exactly one patient, {len(person_ids)} selected.')

        totals = PatientCopyStats()
        failed: list[int] = []
        for person_id in person_ids:
            try:
                stats = apply_patient(
                    read_patient(SOURCE_ALIAS, person_id), organization,
                    target_person_id=options['target_person_id'],
                    replace=options['replace'], dry_run=options['dry_run'],
                )
            except Exception as exc:
                # Each patient is its own transaction, so the rest can still be copied.
                failed.append(person_id)
                self.stdout.write(self.style.ERROR(f'  {person_id}: {exc}'))
                continue
            self.stdout.write(f'  {person_id} as {stats.person_id}: {sum(stats.created.values())} rows')
            for counter in ('created', 'skipped', 'missing_concepts', 'warnings'):
                getattr(totals, counter).update(getattr(stats, counter))

        self._report(totals)
        verb = 'Would have copied' if options['dry_run'] else 'Copied'
        summary = (
            f'{verb} {len(person_ids) - len(failed)} of {len(person_ids)} patients, '
            f'{sum(totals.created.values())} rows.'
        )
        if failed:
            raise CommandError(f'{summary} Failed: {failed}')
        style = self.style.WARNING if options['dry_run'] else self.style.SUCCESS
        self.stdout.write(style(summary + (' Rolled back.' if options['dry_run'] else '')))

    def _report(self, totals: PatientCopyStats) -> None:
        """Warnings, missing concepts and row counts summed over all patients."""
        for warning, count in totals.warnings.most_common():
            self.stdout.write(self.style.WARNING(f'  ! {warning} (x{count})'))
        if totals.missing_concepts:
            top = ', '.join(f'{ref} x{n}' for ref, n in totals.missing_concepts.most_common(10))
            self.stdout.write(self.style.WARNING(
                f'  ! {len(totals.missing_concepts)} concepts are not loaded here and were cleared: {top}'
            ))
        for table in sorted({*totals.created, *totals.skipped}):
            self.stdout.write(f'  {table:28s} created {totals.created[table]:7d}  skipped {totals.skipped[table]:6d}')
