from io import StringIO

import pytest
from django.core.management import call_command

from omop_core.models import Organization
from tests.factories import PatientRecordFactory

pytestmark = pytest.mark.django_db


class OrganizationFactoryMixin:
    @staticmethod
    def make_org(slug='abc-foundation', name='ABC Foundation'):
        return Organization.objects.create(name=name, slug=slug)


def test_fix_abc_foundation_bc_data_only_updates_targeted_bc_records():
    org = OrganizationFactoryMixin.make_org()
    bc_record = PatientRecordFactory(
        organization=org,
        disease='Breast Cancer',
        disease_slug='breast-cancer',
        stage='Stage III',
        stem_cell_transplant_history=['autologous'],
        plasma_cell_leukemia=True,
        her2_status=None,
        estrogen_receptor_status=None,
    )
    non_bc_record = PatientRecordFactory(
        organization=org,
        disease='multiple myeloma',
        disease_slug='multiple-myeloma',
        stage='Stage II',
        stem_cell_transplant_history=['autologous'],
        plasma_cell_leukemia=True,
    )

    call_command('fix_abc_foundation_bc_data', confirm=True, stdout=StringIO())

    bc_record.refresh_from_db()
    non_bc_record.refresh_from_db()

    assert bc_record.her2_status == 'Positive'
    assert bc_record.estrogen_receptor_status == 'Positive'
    assert bc_record.stem_cell_transplant_history == []
    assert non_bc_record.plasma_cell_leukemia is True
    assert non_bc_record.stem_cell_transplant_history == ['autologous']


def test_fix_abc_foundation_bc_data_dry_run_reports_skipped_non_bc_records():
    org = OrganizationFactoryMixin.make_org()
    PatientRecordFactory(
        organization=org,
        disease='Breast Cancer',
        disease_slug='breast-cancer',
    )
    PatientRecordFactory(
        organization=org,
        disease='multiple myeloma',
        disease_slug='multiple-myeloma',
    )

    stdout = StringIO()
    call_command('fix_abc_foundation_bc_data', dry_run=True, stdout=stdout)

    output = stdout.getvalue()
    assert 'Found 1 targeted breast-cancer PatientRecord(s)' in output
    assert 'Skipping 1 non-breast-cancer PatientRecord(s)' in output
