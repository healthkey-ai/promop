"""Report deployed text widths and aggregate overflow without retrieving source text."""
import json

from django.core.management.base import BaseCommand, CommandError
from django.db import DatabaseError, connections
from django.utils.connection import ConnectionDoesNotExist

from omop_core.services.genomics_schema_audit import audit_schema


class Command(BaseCommand):
    help = 'Read-only PostgreSQL audit of shared genomic text widths, counts and migration history.'
    requires_system_checks = []

    def add_arguments(self, parser):
        parser.add_argument('--database', default='default', help='Configured Django database alias.')
        parser.add_argument('--environment', required=True, help='Deployment label for the evidence report; no credentials.')
        parser.add_argument('--statement-timeout-ms', type=int, default=30000)
        parser.add_argument('--check', action='store_true', help='Exit nonzero when either column differs from varchar(60).')

    def handle(self, *args, **options):
        try:
            report = audit_schema(connections[options['database']],
                                  statement_timeout_ms=options['statement_timeout_ms'])
        except ConnectionDoesNotExist:
            raise CommandError('The selected database alias is not configured.') from None
        except ValueError as exc:
            raise CommandError(str(exc)) from None
        except DatabaseError as exc:
            # Driver errors can contain connection details or source text.
            # A failed/timeout audit must never emit a partial success report.
            raise CommandError(f'Genomics schema audit incomplete ({type(exc).__name__}).') from None
        report['environment'] = options['environment']
        self.stdout.write(json.dumps(report, indent=2))
        if options['check'] and not report['columns_match']:
            raise CommandError('Shared text columns need reconciliation review; no data or schema was changed.')
