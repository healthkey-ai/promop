import csv
import sys
import tempfile
import zipfile
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from omop_core.models import LoincClass, LoincCodeClass

csv.field_size_limit(sys.maxsize)
BATCH = 2000


def _extract_from_archive(archive_path, stdout):
    """Pull the two CSVs out of a loinc.org archive zip.

    The archive is how the files are actually kept — ~110MB unzipped, ~12MB
    zipped — so a deployment that has the zip should not also need the CSVs
    unpacked beside it.
    """
    tmpdir = Path(tempfile.mkdtemp(prefix='loinc_classes_'))
    wanted = ('LoincClass.csv', 'Loinc.csv')
    with zipfile.ZipFile(archive_path) as zf:
        names = {Path(n).name: n for n in zf.namelist()}
        missing = [w for w in wanted if w not in names]
        if missing:
            raise CommandError(
                f'Archive {archive_path} is missing {", ".join(missing)}'
            )
        for w in wanted:
            with zf.open(names[w]) as src, (tmpdir / w).open('wb') as dst:
                dst.write(src.read())
    stdout.write(f'  Extracted {", ".join(wanted)} from archive.')
    return tmpdir / 'LoincClass.csv', tmpdir / 'Loinc.csv'


class Command(BaseCommand):
    help = (
        'Load LOINC class data from the loinc.org archive.\n'
        '  --classes-csv: LoincClass.csv (CLASS → DISPLAY_NAME, ~470 rows)\n'
        '  --loinc-csv:   Loinc.csv (LOINC_NUM → CLASS mapping, ~100k rows)\n'
        'Both files come from the quarterly Loinc_x.yy.zip archive.'
    )

    def add_arguments(self, parser):
        parser.add_argument('--classes-csv',
                            help='Path to LoincClass.csv')
        parser.add_argument('--loinc-csv',
                            help='Path to Loinc.csv (loads LOINC_NUM → CLASS mapping)')
        parser.add_argument('--archive',
                            help=('Path or gs:// URI of a loinc.org archive zip '
                                  'containing LoincClass.csv and Loinc.csv'))
        parser.add_argument('--replace', action='store_true',
                            help='Clear existing rows before loading')

    def handle(self, *args, **options):
        # GCS sourcing and the Cloud Run job that drove it are gone (#1624);
        # routine refreshes come from the release API via sync_loinc_release.
        archive = options.get('archive')
        archive_loinc_path = None

        if archive:
            archive = Path(archive)
            if not archive.exists():
                raise CommandError(f'File not found: {archive}')
            classes_path, archive_loinc_path = _extract_from_archive(archive, self.stdout)
        else:
            if not options.get('classes_csv'):
                raise CommandError(
                    'Provide --classes-csv or --archive. For a routine refresh '
                    'from loinc.org, use: manage.py sync_loinc_release'
                )
            classes_path = Path(options['classes_csv'])
            if not classes_path.exists():
                raise CommandError(f'File not found: {classes_path}')

        if options['replace']:
            LoincCodeClass.objects.all().delete()
            deleted, _ = LoincClass.objects.all().delete()
            self.stdout.write(f'Cleared {deleted} LoincClass rows.')

        self._load_classes(classes_path)

        if archive_loinc_path is not None:
            self._load_code_class_mapping(archive_loinc_path)
        elif options.get('loinc_csv'):
            loinc_path = Path(options['loinc_csv'])
            if not loinc_path.exists():
                raise CommandError(f'File not found: {loinc_path}')
            self._load_code_class_mapping(loinc_path)

        from omop_core.services.concept_unit_info import get_loinc_to_unit, get_loinc_example_units
        get_loinc_to_unit.cache_clear()
        get_loinc_example_units.cache_clear()

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
                example_units = row.get('EXAMPLE_UNITS', '').strip()
                batch.append(LoincCodeClass(
                    loinc_num=loinc_num,
                    loinc_class_id=loinc_class,
                    example_units=example_units,
                    property=row.get('PROPERTY', '').strip(),
                    scale_type=row.get('SCALE_TYP', '').strip(),
                ))
                count += 1
                if len(batch) >= BATCH:
                    LoincCodeClass.objects.bulk_create(
                        batch, ignore_conflicts=False,
                        update_conflicts=True,
                        unique_fields=['loinc_num'],
                        update_fields=['loinc_class_id', 'example_units', 'property', 'scale_type'],
                    )
                    batch = []
        if batch:
            LoincCodeClass.objects.bulk_create(
                batch, ignore_conflicts=False,
                update_conflicts=True,
                unique_fields=['loinc_num'],
                update_fields=['loinc_class_id', 'example_units', 'property', 'scale_type'],
            )
        self.stdout.write(
            f'Loaded {count} LOINC code → class mappings '
            f'(skipped {skipped} with unknown class).'
        )
