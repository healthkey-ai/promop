"""Fast projection of line-scoped OMOP treatment assertions onto PatientRecord."""
from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction


class Command(BaseCommand):
    help = 'Project LOT-N intent/discontinuation Observations to Episode-backed PatientRecord fields.'

    def add_arguments(self, parser):
        parser.add_argument('--all', action='store_true', help='Project every Episode-backed PatientRecord.')
        parser.add_argument('--confirm', action='store_true', help='Required to write projections.')

    def handle(self, *args, **options):
        if not options['all'] or not options['confirm']:
            raise CommandError('Re-run with --all --confirm.')
        statements = [
            """UPDATE patient_record p SET first_line_intent=o.value_as_string
               FROM episode e JOIN observation o ON o.person_id=e.person_id
                 AND o.observation_source_value='LOT-' || e.episode_number || '-intent'
               WHERE p.person_id=e.person_id AND e.episode_number=1""",
            """UPDATE patient_record p SET second_line_intent=o.value_as_string
               FROM episode e JOIN observation o ON o.person_id=e.person_id
                 AND o.observation_source_value='LOT-' || e.episode_number || '-intent'
               WHERE p.person_id=e.person_id AND e.episode_number=2""",
            """UPDATE patient_record p SET later_intent=src.value_as_string FROM (
                 SELECT DISTINCT ON (e.person_id) e.person_id,o.value_as_string
                 FROM episode e JOIN observation o ON o.person_id=e.person_id
                   AND o.observation_source_value='LOT-' || e.episode_number || '-intent'
                 WHERE e.episode_number >= 3 ORDER BY e.person_id,e.episode_number DESC,o.observation_date DESC
               ) src WHERE p.person_id=src.person_id""",
            """UPDATE patient_record p SET first_line_discontinuation_reason=o.value_as_string
               FROM episode e JOIN observation o ON o.person_id=e.person_id
                 AND o.observation_source_value='LOT-' || e.episode_number || '-discontinuation'
               WHERE p.person_id=e.person_id AND e.episode_number=1""",
            """UPDATE patient_record p SET second_line_discontinuation_reason=o.value_as_string
               FROM episode e JOIN observation o ON o.person_id=e.person_id
                 AND o.observation_source_value='LOT-' || e.episode_number || '-discontinuation'
               WHERE p.person_id=e.person_id AND e.episode_number=2""",
            """UPDATE patient_record p SET later_discontinuation_reason=src.value_as_string FROM (
                 SELECT DISTINCT ON (e.person_id) e.person_id,o.value_as_string
                 FROM episode e JOIN observation o ON o.person_id=e.person_id
                   AND o.observation_source_value='LOT-' || e.episode_number || '-discontinuation'
                 WHERE e.episode_number >= 3 ORDER BY e.person_id,e.episode_number DESC,o.observation_date DESC
               ) src WHERE p.person_id=src.person_id""",
        ]
        with transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute('SELECT pg_advisory_xact_lock(731906)')
                affected = []
                for statement in statements:
                    cursor.execute(statement)
                    affected.append(cursor.rowcount)
        self.stdout.write(self.style.SUCCESS(f'Projected LOT assertions: {affected} rows by field.'))
