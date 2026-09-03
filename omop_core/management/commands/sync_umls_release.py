"""Download and import the current raw UMLS release without reloading Athena."""
import os
from pathlib import Path

from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError

from omop_core.management.commands.load_athena_vocabularies import (
    DEFAULT_UMLS_CACHE_DIR, _cache_umls_release,
)


class Command(BaseCommand):
    help = 'Download/cache and import raw UMLS RRF codes into the non-OMOP UMLS tables.'

    def add_arguments(self, parser):
        parser.add_argument('--release-url', help='Pin an NLM UMLS Full Release URL.')
        parser.add_argument('--cache-dir', help='Override UMLS_CACHE_DIR.')
        parser.add_argument('--sources', help='Comma-separated UMLS SABs; defaults to all sources.')

    def handle(self, **options):
        api_key = os.environ.get('UMLS_API_KEY')
        if not api_key:
            raise CommandError('UMLS_API_KEY must be configured to download a UMLS release.')
        cache_dir = options['cache_dir'] or os.environ.get('UMLS_CACHE_DIR', DEFAULT_UMLS_CACHE_DIR)
        release = _cache_umls_release(
            api_key=api_key, cache_dir=cache_dir,
            release_url=options['release_url'] or os.environ.get('UMLS_RELEASE_URL'),
            log=self.stdout.write,
        )
        call_command(
            'load_umls_release',
            archive=str(Path(cache_dir) / release['archive_name']),
            release_version=release['release_version'], release_url=release['release_url'],
            sha256=release.get('sha256', ''), sources=options['sources'],
            verbosity=options['verbosity'],
        )
