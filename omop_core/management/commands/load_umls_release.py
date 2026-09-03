"""Load raw UMLS MRCONSO data into separate, non-OMOP tables."""
import csv
import zipfile
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from omop_core.models import UmlsConcept, UmlsRelease, UmlsSourceCode

BATCH = 10_000


class Command(BaseCommand):
    help = 'Load UMLS CUIs and source-asserted codes from a UMLS Full Release zip.'

    def add_arguments(self, parser):
        parser.add_argument('--archive', required=True)
        parser.add_argument('--release-version', required=True)
        parser.add_argument('--release-url', required=True)
        parser.add_argument('--sha256', default='')
        parser.add_argument('--sources', help='Comma-separated UMLS SABs; defaults to all sources.')

    def handle(self, **options):
        archive = Path(options['archive'])
        if not archive.exists():
            raise CommandError(f'UMLS archive not found: {archive}')
        sources = set(options['sources'].split(',')) if options['sources'] else None
        release, _ = UmlsRelease.objects.update_or_create(
            release_version=options['release_version'],
            defaults={'release_url': options['release_url'], 'archive_sha256': options['sha256']},
        )
        with zipfile.ZipFile(archive) as zf:
            member = next((n for n in zf.namelist() if n.endswith('META/MRCONSO.RRF')), None)
            if not member:
                raise CommandError('Archive does not contain META/MRCONSO.RRF.')
            concepts, codes, count = [], [], 0
            with zf.open(member) as raw:
                reader = csv.reader((line.decode('utf-8', 'replace') for line in raw), delimiter='|')
                for row in reader:
                    if len(row) < 15 or (sources and row[11] not in sources):
                        continue
                    cui, preferred, sab, tty, code, name = row[0], row[6] == 'Y', row[11], row[12], row[13], row[14]
                    concepts.append(UmlsConcept(cui=cui, preferred_name=name if preferred else '', release=release))
                    codes.append(UmlsSourceCode(concept_id=cui, root_source=sab, code=code, term_type=tty, name=name, is_preferred=preferred))
                    if len(codes) >= BATCH:
                        self._flush(concepts, codes); count += len(codes); concepts, codes = [], []
            if codes:
                self._flush(concepts, codes); count += len(codes)
        self.stdout.write(self.style.SUCCESS(f'Loaded {count:,} raw UMLS source-code rows.'))

    @staticmethod
    def _flush(concepts, codes):
        UmlsConcept.objects.bulk_create(concepts, ignore_conflicts=True, batch_size=BATCH)
        UmlsSourceCode.objects.bulk_create(codes, ignore_conflicts=True, batch_size=BATCH)
