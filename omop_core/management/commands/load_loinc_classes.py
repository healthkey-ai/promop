import csv
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from omop_core.models import LoincClass, LoincCodeClass

BATCH = 2000


class Command(BaseCommand):
    help = (
        'Load LOINC class data from the loinc.org archive.\n'
        '  --classes-csv: LoincClass.csv (CLASS → DISPLAY_NAME, ~470 rows)\n'
        '  --loinc-csv:   Loinc.csv (LOINC_NUM → CLASS mapping, ~100k rows)\n'
        'Both files come from the quarterly Loinc_x.yy.zip archive.'
    )

    def add_arguments(self, parser):
        parser.add_argument('--classes-csv', required=True,
                            help='Path to LoincClass.csv')
        parser.add_argument('--loinc-csv',
                            help='Path to Loinc.csv (loads LOINC_NUM → CLASS mapping)')
        parser.add_argument('--replace', action='store_true',
                            help='Clear existing rows before loading')

    def handle(self, *args, **options):
        classes_path = Path(options['classes_csv'])
        if not classes_path.exists():
            raise CommandError(f'File not found: {classes_path}')

        if options['replace']:
            LoincCodeClass.objects.all().delete()
            deleted, _ = LoincClass.objects.all().delete()
            self.stdout.write(f'Cleared {deleted} LoincClass rows.')

        self._load_classes(classes_path)

        loinc_csv = options.get('loinc_csv')
        if loinc_csv:
            loinc_path = Path(loinc_csv)
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
