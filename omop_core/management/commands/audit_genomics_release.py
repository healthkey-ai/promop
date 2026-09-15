"""Read-only release preflight combining migrations, physical schema and recipes."""
import io
import json

from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction
from django.db.migrations.executor import MigrationExecutor

from omop_core.models import Concept, FieldConceptMapping, Measurement, Note, Observation, PatientRecord, Person
from omop_core.services.genomics_schema_audit import audit_schema


class Command(BaseCommand):
    help = 'Read-only genomics release preflight; pending migrations or incomplete prerequisites fail --check.'

    def add_arguments(self, parser):
        parser.add_argument('--environment', required=True)
        parser.add_argument('--check', action='store_true')

    def handle(self, *args, **options):
        report = {'report_version': 1, 'environment': options['environment'], 'ready': False}
        try:
            report['text_schema'] = audit_schema(connection)
            with transaction.atomic(), connection.cursor() as cursor:
                cursor.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY')
                cursor.execute('SET LOCAL statement_timeout = 30000')
                cursor.execute('SET LOCAL row_security = off')
                executor = MigrationExecutor(connection)
                executor.loader.check_consistent_history(connection)
                report['migration_conflicts'] = executor.loader.detect_conflicts()
                report['pending_migrations'] = [m.app_label + '.' + m.name for m, _ in
                    executor.migration_plan(executor.loader.graph.leaf_nodes())]
                missing = {}
                tables = set(connection.introspection.table_names(cursor))
                for model in (Concept, FieldConceptMapping, Measurement, Note, Observation, PatientRecord, Person):
                    table = model._meta.db_table
                    actual = {c.name for c in connection.introspection.get_table_description(cursor, table)} if table in tables else set()
                    absent = sorted({f.column for f in model._meta.local_concrete_fields} - actual)
                    if absent:
                        missing[table] = absent
                report['missing_columns'] = missing
                output = io.StringIO()
                if not missing:
                    try:
                        call_command('audit_genomics_domains', include_writer_prerequisites=True, stdout=output)
                        report['writer_prerequisites_passed'] = True
                    except CommandError:
                        report['writer_prerequisites_passed'] = False
                    report['writer_prerequisites'] = output.getvalue()
                else:
                    report['writer_prerequisites_passed'] = False
                report['ready'] = (report['text_schema']['columns_match'] and not missing
                                   and not report['pending_migrations'] and not report['migration_conflicts']
                                   and report['writer_prerequisites_passed'])
        except Exception as exc:
            report['error_type'] = type(exc).__name__
        self.stdout.write(json.dumps(report, sort_keys=True))
        if options['check'] and not report['ready']:
            raise CommandError('Genomics release prerequisites are not ready; review the preflight receipt.')
