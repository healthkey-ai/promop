import csv
import sys
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from omop_core.models import LoincClass, LoincCodeClass

csv.field_size_limit(sys.maxsize)
BATCH = 2000


def _download_gcs_blob(bucket, filename, stdout):
    blob = bucket.blob(filename)
    if not blob.exists():
        raise CommandError(f'Required blob not found: gs://{bucket.name}/{filename}')
    dest = Path('/tmp/loinc') / filename
    dest.parent.mkdir(parents=True, exist_ok=True)
    size_mb = (blob.size or 0) / 1048576
    stdout.write(f'  Downloading {filename} ({size_mb:.0f}MB)...')
    blob.download_to_filename(str(dest))
    stdout.write(f'  Downloaded {filename}.')
    return dest


class Command(BaseCommand):
    help = (
        'Load LOINC class data from the loinc.org archive.\n'
        '  --classes-csv: LoincClass.csv (CLASS → DISPLAY_NAME, ~470 rows)\n'
        '  --loinc-csv:   Loinc.csv (LOINC_NUM → CLASS mapping, ~100k rows)\n'
        '  --bucket:      GCS bucket to download files from (alternative to local paths)\n'
        'Both files come from the quarterly Loinc_x.yy.zip archive.'
    )

    def add_arguments(self, parser):
        parser.add_argument('--classes-csv',
                            help='Path to LoincClass.csv')
        parser.add_argument('--loinc-csv',
                            help='Path to Loinc.csv (loads LOINC_NUM → CLASS mapping)')
        parser.add_argument('--bucket',
                            help='GCS bucket name to download files from')
        parser.add_argument('--replace', action='store_true',
                            help='Clear existing rows before loading')

    def handle(self, *args, **options):
        bucket_name = options.get('bucket')
        gcs_bucket = None

        if bucket_name:
            from google.cloud import storage as gcs
            gcs_bucket = gcs.Client().bucket(bucket_name)
            self.stdout.write(f'Loading from gs://{bucket_name}/')
            classes_path = _download_gcs_blob(gcs_bucket, 'LoincClass.csv', self.stdout)
        else:
            if not options.get('classes_csv'):
                raise CommandError('Provide either --classes-csv or --bucket')
            classes_path = Path(options['classes_csv'])
            if not classes_path.exists():
                raise CommandError(f'File not found: {classes_path}')

        if options['replace']:
            LoincCodeClass.objects.all().delete()
            deleted, _ = LoincClass.objects.all().delete()
            self.stdout.write(f'Cleared {deleted} LoincClass rows.')

        self._load_classes(classes_path)

        if gcs_bucket:
            loinc_path = _download_gcs_blob(gcs_bucket, 'Loinc.csv', self.stdout)
            self._load_code_class_mapping(loinc_path)
            loinc_path.unlink()
            self.stdout.write('  Cleaned up Loinc.csv.')
        elif options.get('loinc_csv'):
            loinc_path = Path(options['loinc_csv'])
            if not loinc_path.exists():
                raise CommandError(f'File not found: {loinc_path}')
            self._load_code_class_mapping(loinc_path)

    def _load_classes(self, path):
        count = 0
        batch = []
        with open(path, encoding='utf-8', newline='') as f:
            for row in csv.DictReader(f):
                code = row.get('CLASS', '').strip()
                display_name = row.get('DISPLAY_NAME', '').strip()
                if not code or not display_name:
                    continue
                batch.append(LoincClass(code=code, display_name=display_name))
                count += 1
                if len(batch) >= BATCH:
                    LoincClass.objects.bulk_create(batch, ignore_conflicts=True)
                    batch = []
        if batch:
            LoincClass.objects.bulk_create(batch, ignore_conflicts=True)
        self.stdout.write(f'Loaded {count} LoincClass rows.')

    def _load_code_class_mapping(self, path):
        valid_classes = set(LoincClass.objects.values_list('code', flat=True))
        count = 0
        skipped = 0
        batch = []
        with open(path, encoding='utf-8', newline='') as f:
            for row in csv.DictReader(f):
                loinc_num = row.get('LOINC_NUM', '').strip()
                loinc_class = row.get('CLASS', '').strip()
                if not loinc_num or not loinc_class:
                    continue
                if loinc_class not in valid_classes:
                    skipped += 1
                    continue
                batch.append(LoincCodeClass(
                    loinc_num=loinc_num,
                    loinc_class_id=loinc_class,
                ))
                count += 1
                if len(batch) >= BATCH:
                    LoincCodeClass.objects.bulk_create(batch, ignore_conflicts=True)
                    batch = []
        if batch:
            LoincCodeClass.objects.bulk_create(batch, ignore_conflicts=True)
        self.stdout.write(
            f'Loaded {count} LOINC code → class mappings '
            f'(skipped {skipped} with unknown class).'
        )
