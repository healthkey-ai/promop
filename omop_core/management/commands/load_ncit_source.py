"""Load NCI's versioned flat-file source vocabulary, including descriptive metadata."""
import hashlib
import io
import re
import zipfile
from pathlib import Path
from urllib.parse import urlparse

from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction
from django.utils import timezone

from omop_core.models import SourceVocabulary, SourceVocabularyTerm


class Command(BaseCommand):
    help = 'Load NCIt codes, definitions, synonyms and hierarchy for source mapping.'

    def add_arguments(self, parser):
        parser.add_argument('--archive', required=True)
        parser.add_argument('--release-version', required=True)
        parser.add_argument('--source-url', required=True)
        parser.add_argument('--dry-run', action='store_true')

    def handle(self, **options):
        path = Path(options['archive'])
        if not path.is_file():
            raise CommandError(f'Archive not found: {path}')
        version = options['release_version']
        if not re.fullmatch(r'\d{2}\.\d{2}[a-z]', version):
            raise CommandError('Expected an NCIt release version such as 26.08e.')
        url = options['source_url']
        parsed = urlparse(url)
        if parsed.scheme != 'https' or not parsed.netloc or len(url) > 1000:
            raise CommandError('source-url must be an HTTPS publisher URL, at most 1000 characters.')
        checksum = hashlib.sha256(path.read_bytes()).hexdigest()
        records = []
        seen = set()
        try:
            with zipfile.ZipFile(path) as archive:
                with archive.open('Thesaurus.txt') as raw:
                    for number, line in enumerate(io.TextIOWrapper(raw, encoding='utf-8-sig'), 1):
                        if not line.strip():
                            continue
                        fields = line.rstrip('\r\n').split('\t')
                        if len(fields) != 9:
                            raise CommandError(f'Line {number}: expected 9 NCIt fields, got {len(fields)}.')
                        code, iri, parents, synonyms, definition, display, status, types, subsets = fields
                        names = list(dict.fromkeys(s for s in synonyms.split('|') if s))
                        if not re.fullmatch(r'C\d+', code) or not names or code in seen:
                            raise CommandError(f'Line {number}: missing/duplicate code or preferred name.')
                        seen.add(code)
                        retired = bool({'Retired_Concept', 'Obsolete_Concept'} & set(status.split('|')))
                        records.append(SourceVocabularyTerm(
                            vocabulary_id='NCIt', code=code, name=names[0],
                            definition=definition, synonyms=names[1:],
                            parents=[p for p in parents.split('|') if p],
                            semantic_types=[t for t in types.split('|') if t],
                            status=status, retired=retired,
                            metadata={'iri': iri, 'display_name': display,
                                      'subsets': [s for s in subsets.split('|') if s]},
                            search_text='\n'.join([code, *names]),
                        ))
        except (zipfile.BadZipFile, KeyError, UnicodeError) as exc:
            raise CommandError('Expected a UTF-8 NCIt flat ZIP containing Thesaurus.txt.') from exc
        if not records:
            raise CommandError('Refusing an empty source vocabulary release.')
        definitions = sum(bool(t.definition) for t in records)
        retired = sum(t.retired for t in records)
        self.stdout.write(f'NCIt {version}: {len(records):,} codes, {definitions:,} definitions, {retired:,} retired/obsolete.')
        if options['dry_run']:
            self.stdout.write('Dry run: no changes written.')
            return
        with transaction.atomic():
            vocabulary, _ = SourceVocabulary.objects.update_or_create(
                vocabulary_id='NCIt', defaults={
                    'name': 'NCI Thesaurus', 'release_version': version,
                    'source_url': url, 'archive_sha256': checksum,
                    'term_count': len(records), 'loaded_at': timezone.now(),
                },
            )
            # Serialise concurrent reloads of this vocabulary. Readers see the
            # previous complete release until this transaction commits.
            SourceVocabulary.objects.select_for_update().get(pk=vocabulary.pk)
            SourceVocabularyTerm.objects.filter(vocabulary=vocabulary).delete()
            SourceVocabularyTerm.objects.bulk_create(records, batch_size=1000)
            with connection.cursor() as cursor:
                cursor.execute('ANALYZE source_vocabulary_term')
        self.stdout.write(self.style.SUCCESS(
            f'Loaded {len(records):,} NCIt source terms. Existing mappings and Seen counts were not modified.'
        ))
