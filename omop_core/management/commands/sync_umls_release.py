"""Stream UMLS MRCONSO into PostgreSQL without caching its archive on disk."""
import os
import codecs

from django.core.management.base import BaseCommand, CommandError
import requests
from stream_unzip import stream_unzip

from omop_core.management.commands.load_athena_vocabularies import (
    UMLS_DOWNLOAD_URL, _resolve_umls_release,
)
from omop_core.models import UmlsConcept, UmlsRelease, UmlsSourceCode

BATCH = 10_000


class Command(BaseCommand):
    help = 'Stream/import raw UMLS RRF codes without writing the archive to disk.'

    def add_arguments(self, parser):
        parser.add_argument('--release-url', help='Pin an NLM UMLS Full Release URL.')
        parser.add_argument('--sources', help='Comma-separated UMLS SABs; defaults to all sources.')

    def handle(self, **options):
        api_key = os.environ.get('UMLS_API_KEY')
        if not api_key:
            raise CommandError('UMLS_API_KEY must be configured to download a UMLS release.')
        release = _resolve_umls_release(options['release_url'] or os.environ.get('UMLS_RELEASE_URL'))
        release_row, _ = UmlsRelease.objects.update_or_create(release_version=release['release_version'], defaults={'release_url': release['release_url'], 'archive_sha256': ''})
        allowed = set(options['sources'].split(',')) if options['sources'] else None
        response = requests.get(UMLS_DOWNLOAD_URL, params={'url': release['release_url'], 'apiKey': api_key}, stream=True, timeout=(30, 300))
        response.raise_for_status()
        concepts, codes, remainder, total, found = [], [], '', 0, False
        for name, _, chunks in stream_unzip(response.iter_content(1024 * 1024)):
            if not name.decode('utf-8').endswith('META/MRCONSO.RRF'):
                for _ in chunks: pass
                continue
            found = True; decoder = codecs.getincrementaldecoder('utf-8')('replace')
            for chunk in chunks:
                lines = (remainder + decoder.decode(chunk)).split('\n'); remainder = lines.pop()
                for line in lines:
                    row = line.rstrip('\r').split('|')
                    if len(row) < 15 or (allowed and row[11] not in allowed): continue
                    cui, pref, sab, tty, code, label = row[0], row[6] == 'Y', row[11], row[12], row[13], row[14]
                    concepts.append(UmlsConcept(cui=cui, preferred_name=label if pref else '', release=release_row)); codes.append(UmlsSourceCode(concept_id=cui, root_source=sab, code=code, term_type=tty, name=label, is_preferred=pref))
                    if len(codes) >= BATCH: self._flush(concepts, codes); total += len(codes); concepts, codes = [], []
            break
        if not found: raise CommandError('UMLS archive did not contain META/MRCONSO.RRF.')
        if codes: self._flush(concepts, codes); total += len(codes)
        self.stdout.write(self.style.SUCCESS(f'Loaded {total:,} source-code rows without archive caching.'))

    @staticmethod
    def _flush(concepts, codes):
        UmlsConcept.objects.bulk_create(concepts, ignore_conflicts=True, batch_size=BATCH)
        UmlsSourceCode.objects.bulk_create(codes, ignore_conflicts=True, batch_size=BATCH)
