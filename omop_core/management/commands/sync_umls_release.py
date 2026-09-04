"""Stream UMLS MRCONSO into PostgreSQL without caching its archive on disk."""
import os
import codecs

from django.core.management.base import BaseCommand, CommandError
import requests
from stream_unzip import stream_unzip

from omop_core.management.commands.load_athena_vocabularies import (
    UMLS_DOWNLOAD_URL, UMLS_RELEASES_URL, _release_version_from_url,
)
from omop_core.models import UmlsConcept, UmlsRelease, UmlsSourceCode

BATCH = 10_000


def _find_mrconso(chunks):
    """Yield MRCONSO bytes from either a release ZIP or its nested payload ZIP."""
    for name, _, member_chunks in stream_unzip(chunks):
        filename = name.decode('utf-8')
        if filename.endswith('MRCONSO.RRF'):
            return member_chunks
        if filename.lower().endswith('.zip'):
            nested = _find_mrconso(member_chunks)
            if nested is not None:
                return nested
        else:
            for _ in member_chunks:
                pass
    return None


class Command(BaseCommand):
    help = 'Stream/import raw UMLS RRF codes without writing the archive to disk.'

    def add_arguments(self, parser):
        parser.add_argument('--release-url', help='Pin an NLM UMLS Full Release URL.')
        parser.add_argument('--sources', help='Comma-separated UMLS SABs; defaults to all sources.')

    def handle(self, **options):
        api_key = os.environ.get('UMLS_API_KEY')
        if not api_key:
            raise CommandError('UMLS_API_KEY must be configured to download a UMLS release.')
        pinned_url = options['release_url'] or os.environ.get('UMLS_RELEASE_URL')
        if pinned_url:
            release = {'release_url': pinned_url, 'release_version': _release_version_from_url(pinned_url)}
        else:
            listing = requests.get(UMLS_RELEASES_URL, params={'releaseType': 'umls-metathesaurus-mrconso-file', 'current': 'true'}, timeout=30)
            listing.raise_for_status()
            releases = listing.json()
            if not isinstance(releases, list) or len(releases) != 1 or not releases[0].get('downloadUrl'):
                raise CommandError('NLM UTS did not return a current UMLS MRCONSO release.')
            release = {'release_url': releases[0]['downloadUrl'], 'release_version': releases[0].get('releaseVersion')}
        if not release['release_version']:
            raise CommandError('Could not determine the UMLS release version.')
        release_row, _ = UmlsRelease.objects.update_or_create(release_version=release['release_version'], defaults={'release_url': release['release_url'], 'archive_sha256': ''})
        allowed = set(options['sources'].split(',')) if options['sources'] else None
        response = requests.get(UMLS_DOWNLOAD_URL, params={'url': release['release_url'], 'apiKey': api_key}, stream=True, timeout=(30, 300))
        response.raise_for_status()
        concepts, codes, remainder, total = [], [], '', 0
        chunks = _find_mrconso(response.iter_content(1024 * 1024))
        if chunks is None:
            raise CommandError('UMLS archive did not contain META/MRCONSO.RRF.')
        decoder = codecs.getincrementaldecoder('utf-8')('replace')
        for chunk in chunks:
                lines = (remainder + decoder.decode(chunk)).split('\n'); remainder = lines.pop()
                for line in lines:
                    row = line.rstrip('\r').split('|')
                    if len(row) < 15 or (allowed and row[11] not in allowed): continue
                    cui, pref, sab, tty, code, label = row[0], row[6] == 'Y', row[11], row[12], row[13], row[14]
                    concepts.append(UmlsConcept(cui=cui, preferred_name=label if pref else '', release=release_row)); codes.append(UmlsSourceCode(concept_id=cui, root_source=sab, code=code, term_type=tty, name=label, is_preferred=pref))
                    if len(codes) >= BATCH: self._flush(concepts, codes); total += len(codes); concepts, codes = [], []
        if codes: self._flush(concepts, codes); total += len(codes)
        self.stdout.write(self.style.SUCCESS(f'Loaded {total:,} source-code rows without archive caching.'))

    @staticmethod
    def _flush(concepts, codes):
        UmlsConcept.objects.bulk_create(concepts, ignore_conflicts=True, batch_size=BATCH)
        UmlsSourceCode.objects.bulk_create(codes, ignore_conflicts=True, batch_size=BATCH)
