"""Mark an entire OMOP vocabulary (code system) deprecated/retired.

PHR-S FM TI.4.2#05 (promop#305): the base OMOP CDM has no vocabulary-level
status column, so there was no way to retire a whole terminology. This command
sets the deprecation state on `Vocabulary` and appends a 'deprecated' row to the
append-only `VocabularyVersionHistory` change-history.

Usage:
    python manage.py deprecate_vocabulary ICD9CM --reason="Superseded by ICD10CM"
    python manage.py deprecate_vocabulary ICD9CM --undo
"""
from datetime import date

from django.core.management.base import BaseCommand, CommandError

from omop_core.models import (
    Vocabulary, VocabularyVersionHistory, record_vocabulary_version_history,
)


class Command(BaseCommand):
    help = 'Mark an OMOP vocabulary (code system) deprecated/retired, or undo it.'

    def add_arguments(self, parser):
        parser.add_argument('vocabulary_id', help='The vocabulary_id to deprecate (e.g. ICD9CM)')
        parser.add_argument('--reason', default=None,
                            help='Optional human-readable reason for deprecation.')
        parser.add_argument('--date', dest='deprecated_date', default=None,
                            help='Deprecation date (YYYY-MM-DD). Defaults to today.')
        parser.add_argument('--undo', action='store_true',
                            help='Clear the deprecation state instead of setting it.')

    def handle(self, *args, **options):
        vocabulary_id = options['vocabulary_id']
        try:
            vocab = Vocabulary.objects.get(vocabulary_id=vocabulary_id)
        except Vocabulary.DoesNotExist:
            raise CommandError(f'Vocabulary not found: {vocabulary_id}')

        if options['undo']:
            vocab.is_deprecated = False
            vocab.deprecated_date = None
            vocab.deprecated_reason = None
            vocab.save(update_fields=['is_deprecated', 'deprecated_date', 'deprecated_reason'])
            self.stdout.write(self.style.SUCCESS(
                f'Cleared deprecation state on vocabulary {vocabulary_id}.'
            ))
            return

        if options['deprecated_date']:
            try:
                dep_date = date.fromisoformat(options['deprecated_date'])
            except ValueError:
                raise CommandError('--date must be ISO format YYYY-MM-DD.')
        else:
            dep_date = date.today()

        vocab.is_deprecated = True
        vocab.deprecated_date = dep_date
        vocab.deprecated_reason = options['reason']
        vocab.save(update_fields=['is_deprecated', 'deprecated_date', 'deprecated_reason'])

        record_vocabulary_version_history(
            vocabulary_id=vocabulary_id,
            version=vocab.vocabulary_version,
            action=VocabularyVersionHistory.ACTION_DEPRECATED,
            cdm_release_date=dep_date,
            note=options['reason'],
        )
        self.stdout.write(self.style.SUCCESS(
            f'Marked vocabulary {vocabulary_id} deprecated (date={dep_date}'
            + (f', reason="{options["reason"]}"' if options['reason'] else '')
            + ') and recorded version-history row.'
        ))
