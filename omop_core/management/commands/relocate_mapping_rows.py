"""Move a code's stored rows into the table its mapping names (#2386).

A mapping save that changes the table relocates the rows itself, in the
request or in a job after commit. A job that was lost, or a mapping whose
table an upload or an older save changed, leaves rows in the other table;
this moves them. Only measurement and observation rows are moved.
"""
from django.core.management.base import BaseCommand, CommandError

from omop_core.mapping.table_relocation import RELOCATABLE_TABLES, relocatable, relocate_clinical_rows
from omop_core.models import SourceCodeConceptMapping
from omop_core.services.repoint_jobs import run_repoint


class Command(BaseCommand):
    help = "Move a code mapping's stored clinical rows from another table into the mapping's table"

    def add_arguments(self, parser):
        parser.add_argument('mapping_ids', nargs='+', type=int, help='Mappings whose rows to move.')
        parser.add_argument('--from-table', dest='from_table', required=True,
                            choices=sorted(RELOCATABLE_TABLES), help='The table the rows sit in now.')
        parser.add_argument('--dry-run', action='store_true', help='Count what would move.')

    def handle(self, *args, mapping_ids, from_table, dry_run, **options):
        for mapping in SourceCodeConceptMapping.objects.filter(pk__in=mapping_ids).order_by('pk'):
            if not relocatable(from_table, mapping.omop_table):
                raise CommandError(
                    f'Mapping {mapping.pk} names {mapping.omop_table or "no table"}; '
                    f'rows move only between {" and ".join(sorted(RELOCATABLE_TABLES))}.'
                )
            outcome = relocate_clinical_rows(
                mapping=mapping, from_table=from_table, to_table=mapping.omop_table, apply_changes=not dry_run,
            )
            outcome.pop('person_ids', None)
            label = f'{mapping.pk} {mapping.source_vocabulary_id}:{mapping.source_code} {from_table} -> {mapping.omop_table}'
            self.stdout.write(f'{label}: {outcome}')
            if not dry_run and outcome['rows_relocated'] and mapping.status == 'approved':
                self.stdout.write(f'{label}: re-point {run_repoint(mapping.pk)}')
