"""Test that LOINC release archive loading populates LoincCodeClass axis columns."""
import io
import zipfile
from io import StringIO
from pathlib import Path

import pytest

from omop_core.management.commands.load_athena_vocabularies import (
    load_loinc_axes_from_archive,
)
from omop_core.models import LoincClass, LoincCodeClass


# --- helpers to build a minimal LOINC release ZIP in memory ---

LOINC_CSV_HEADER = (
    'LOINC_NUM,COMPONENT,PROPERTY,TIME_ASPCT,SYSTEM,SCALE_TYP,METHOD_TYP,'
    'CLASS,EXAMPLE_UNITS\n'
)

PART_CSV_HEADER = (
    'PartNumber,PartTypeName,PartName,PartDisplayName,Status\n'
)


def _loinc_csv_row(loinc_num, component='Albumin', prop='MCnc',
                   time='Pt', system='Ser/Plas', scale='Qn', method='',
                   loinc_class='CHEM', example_units='g/dL'):
    return (
        f'{loinc_num},{component},{prop},{time},{system},{scale},{method},'
        f'{loinc_class},{example_units}\n'
    )


def _build_loinc_zip(tmp_path, loinc_rows='', part_rows=''):
    """Create a minimal LOINC release ZIP at tmp_path / 'Loinc_test.zip'."""
    loinc_csv = LOINC_CSV_HEADER + loinc_rows
    part_csv = PART_CSV_HEADER + part_rows
    zip_path = tmp_path / 'Loinc_test.zip'
    with zipfile.ZipFile(zip_path, 'w') as zf:
        zf.writestr('Loinc_test/LoincTable/Loinc.csv', loinc_csv)
        zf.writestr('Loinc_test/AccessoryFiles/PartFile/Part.csv', part_csv)
    return zip_path


@pytest.fixture
def loinc_archive(tmp_path):
    """Build a minimal LOINC release ZIP with 3 codes and the CHEM class."""
    return _build_loinc_zip(
        tmp_path,
        loinc_rows=(
            _loinc_csv_row('1751-7')
            + _loinc_csv_row('86921-4', component='Albumin goal')
            + _loinc_csv_row('2862-1', component='Albumin', prop='SCnc',
                             example_units='mmol/L')
        ),
        part_rows='LP7787-7,CLASS,CHEM,Chemistry,ACTIVE\n',
    )


@pytest.fixture
def empty_loinc_rows():
    """Create LoincCodeClass rows with empty axis columns (simulating HT GCP)."""
    LoincClass.objects.get_or_create(code='CHEM', defaults={'display_name': 'Chemistry'})
    LoincCodeClass.objects.bulk_create([
        LoincCodeClass(loinc_num='1751-7', loinc_class_id='CHEM'),
        LoincCodeClass(loinc_num='86921-4', loinc_class_id='CHEM'),
        LoincCodeClass(loinc_num='2862-1', loinc_class_id='CHEM'),
    ], update_conflicts=True, unique_fields=['loinc_num'],
       update_fields=['loinc_class_id'])


@pytest.mark.django_db
class TestLoadLoincAxesFromArchive:

    def test_populates_empty_axis_columns(self, loinc_archive, empty_loinc_rows):
        """Axes are filled when starting from empty columns."""
        row = LoincCodeClass.objects.get(loinc_num='1751-7')
        assert row.component == ''

        count = load_loinc_axes_from_archive(loinc_archive, lambda msg: None)

        assert count == 3
        row.refresh_from_db()
        assert row.component == 'Albumin'
        assert row.property == 'MCnc'
        assert row.time_aspect == 'Pt'
        assert row.system == 'Ser/Plas'
        assert row.scale_type == 'Qn'
        assert row.method_type == ''
        assert row.example_units == 'g/dL'

    def test_updates_existing_rows(self, loinc_archive, empty_loinc_rows):
        """load_from_archive with update_conflicts overwrites existing rows."""
        count = load_loinc_axes_from_archive(loinc_archive, lambda msg: None)
        assert count == 3

        goal = LoincCodeClass.objects.get(loinc_num='86921-4')
        assert goal.component == 'Albumin goal'
        assert goal.property == 'MCnc'

        scnc = LoincCodeClass.objects.get(loinc_num='2862-1')
        assert scnc.property == 'SCnc'
        assert scnc.example_units == 'mmol/L'

    def test_creates_new_rows(self, loinc_archive):
        """Rows not already in LoincCodeClass are inserted."""
        assert LoincCodeClass.objects.filter(loinc_num='1751-7').count() == 0

        count = load_loinc_axes_from_archive(loinc_archive, lambda msg: None)

        assert count == 3
        row = LoincCodeClass.objects.get(loinc_num='1751-7')
        assert row.component == 'Albumin'

    def test_creates_loinc_class_from_part_csv(self, loinc_archive):
        """Part.csv in the archive creates LoincClass rows (FK parent)."""
        LoincClass.objects.filter(code='CHEM').delete()
        assert not LoincClass.objects.filter(code='CHEM').exists()

        load_loinc_axes_from_archive(loinc_archive, lambda msg: None)

        lc = LoincClass.objects.get(code='CHEM')
        assert lc.display_name == 'Chemistry'

    def test_empty_archive_loads_zero(self, tmp_path):
        """An archive with only headers produces no rows."""
        zip_path = _build_loinc_zip(tmp_path)
        count = load_loinc_axes_from_archive(zip_path, lambda msg: None)
        assert count == 0
