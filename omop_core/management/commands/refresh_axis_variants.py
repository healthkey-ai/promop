"""Find source codes whose units imply more than one LOINC, and flag them.

The batch form of the refresh the source-code upload runs for the codes it
touches. Candidates are :func:`axis_variants.multi_unit_keys`.

Inline by default. ``--queue`` records a durable request for the worker
instead, which is how a deployment runs it (#2021): on a large instance the
walk takes longer than a release step may hold. ``--rebuild-evidence`` first
rewrites every code's stored unit evidence from this instance's own patient
data (#2043), walking every code with evidence or patient records.
"""
from django.core.management.base import BaseCommand, CommandError

from omop_core.mapping import axis_variants
from omop_core.models import SourceCodeConceptMapping


class Command(BaseCommand):
    help = 'Refresh LOINC axis variants for source codes seen in more than one unit.'

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true',
                            help='Report the codes that would be refreshed; write nothing.')
        parser.add_argument('--queue', action='store_true',
                            help='Record a request for the Celery worker instead of refreshing here.')
        parser.add_argument('--rebuild-evidence', action='store_true',
                            help="Rewrite each code's stored unit evidence from this instance's patient data first.")

    def handle(self, *args, **options):
        if options['queue'] and options['dry_run']:
            raise CommandError('--queue and --dry-run are exclusive.')
        rebuild = options['rebuild_evidence']
        if options['queue']:
            from omop_core.services.axis_variant_jobs import request_refresh

            request, created = request_refresh(
                reason='manage.py refresh_axis_variants --queue' + (' --rebuild-evidence' if rebuild else ''),
                rebuild_evidence=rebuild)
            if created:
                self.stdout.write(self.style.SUCCESS(
                    f'Queued axis variant refresh {request.pk}; the worker performs it.'))
            else:
                self.stdout.write(
                    f'Axis variant refresh {request.pk} is already {request.outcome}; not queued twice.')
            return
        if options['dry_run']:
            if rebuild:
                self.stdout.write(f'{len(axis_variants.evidence_keys())} source codes would have their '
                                  'unit evidence rebuilt from patient data.')
                return
            keys = axis_variants.multi_unit_keys()
            self.stdout.write(f'{len(keys)} source codes have more than one unit.')
            return
        from omop_core.services.axis_variant_jobs import run_pending_inline

        # Through the request row, so a request a migration left queued on an
        # instance without a worker is completed here rather than left to
        # block every later request.
        result = run_pending_inline(
            reason='manage.py refresh_axis_variants' + (' --rebuild-evidence' if rebuild else ''),
            rebuild_evidence=rebuild)
        if result['skipped']:
            self.stdout.write(f'Axis variant refresh is already {result["outcome"]}; nothing to do.')
            return
        if result.get('rebuild_evidence'):
            self.stdout.write(f'{result["keys"]} source codes had their unit evidence rebuilt from patient data.')
        else:
            self.stdout.write(f'{result["keys"]} source codes have more than one unit.')
        flagged = SourceCodeConceptMapping.objects.filter(axis_variant_count__gt=0).count()
        self.stdout.write(f'Refreshed {result["refreshed"]} mappings; {flagged} now have axis variants.')
