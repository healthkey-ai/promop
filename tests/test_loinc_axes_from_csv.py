"""Test that Loinc.csv loading populates LoincCodeClass axis columns."""
import tempfile
from io import StringIO
from pathlib import Path

import pytest

from omop_core.management.commands.load_athena_vocabularies import (
    load_loinc_axes_from_csv,
)
from omop_core.models import LoincClass, LoincCodeClass


LOINC_CSV_HEADER = (
    'LOINC_NUM,COMPONENT,PROPERTY,TIME_ASPCT,SYSTEM,SCALE_TYP,METHOD_TYP,'
    'CLASS,EXAMPLE_UNITS\n'
)


def _loinc_csv_row(loinc_num, component='Albumin', prop='MCnc',
                   time='Pt', system='Ser/Plas', scale='Qn', method='',
                   loinc_class='CHEM', example_units='g/dL'):
    return (
        f'{loinc_num},{component},{prop},{time},{system},{scale},{method},'
        f'{loinc_class},{example_units}\n'
    )


@pytest.fixture
def loinc_csv(tmp_path):
    """Write a small Loinc.csv to a temp directory and return its path."""
    path = tmp_path / 'Loinc.csv'
    path.write_text(
        LOINC_CSV_HEADER
        + _loinc_csv_row('1751-7')
        + _loinc_csv_row('86921-4', component='Albumin goal')
        + _loinc_csv_row('2862-1', component='Albumin', prop='SCnc',
                         example_units='mmol/L')
    )
    return path


@pytest.fixture(autouse=True)
def loinc_class_chem():
    """Create the LoincClass parent row that LoincCodeClass FK references."""
    LoincClass.objects.get_or_create(code='CHEM', defaults={'display_name': 'Chemistry'})


@pytest.fixture
def empty_loinc_rows():
    """Create LoincCodeClass rows with empty axis columns (simulating HT GCP)."""
    LoincCodeClass.objects.bulk_create([
        LoincCodeClass(loinc_num='1751-7', loinc_class_id='CHEM'),
        LoincCodeClass(loinc_num='86921-4', loinc_class_id='CHEM'),
        LoincCodeClass(loinc_num='2862-1', loinc_class_id='CHEM'),
    ], update_conflicts=True, unique_fields=['loinc_num'],
       update_fields=['loinc_class_id'])


@pytest.mark.django_db
class TestLoadLoincAxesFromCsv:

    def test_populates_empty_axis_columns(self, loinc_csv, empty_loinc_rows):
        """Axes are filled when starting from empty columns."""
        # Verify axes are empty before
        row = LoincCodeClass.objects.get(loinc_num='1751-7')
        assert row.component == ''

        count = load_loinc_axes_from_csv(loinc_csv, StringIO().write)

        assert count == 3
        row.refresh_from_db()
        assert row.component == 'Albumin'
        assert row.property == 'MCnc'
        assert row.time_aspect == 'Pt'
        assert row.system == 'Ser/Plas'
        assert row.scale_type == 'Qn'
        assert row.method_type == ''
        assert row.example_units == 'g/dL'

    def test_updates_existing_rows(self, loinc_csv, empty_loinc_rows):
        """bulk_create with update_conflicts overwrites existing rows."""
        count = load_loinc_axes_from_csv(loinc_csv, StringIO().write)
        assert count == 3

        goal = LoincCodeClass.objects.get(loinc_num='86921-4')
        assert goal.component == 'Albumin goal'
        assert goal.property == 'MCnc'

        scnc = LoincCodeClass.objects.get(loinc_num='2862-1')
        assert scnc.property == 'SCnc'
        assert scnc.example_units == 'mmol/L'

    def test_creates_new_rows(self, loinc_csv):
        """Rows not already in LoincCodeClass are inserted."""
        assert LoincCodeClass.objects.filter(loinc_num='1751-7').count() == 0

        count = load_loinc_axes_from_csv(loinc_csv, StringIO().write)

        assert count == 3
        row = LoincCodeClass.objects.get(loinc_num='1751-7')
        assert row.component == 'Albumin'

    def test_empty_csv_loads_zero(self, tmp_path):
        """A Loinc.csv with only a header produces no rows."""
        path = tmp_path / 'Loinc.csv'
        path.write_text(LOINC_CSV_HEADER)
        count = load_loinc_axes_from_csv(path, StringIO().write)
        assert count == 0
