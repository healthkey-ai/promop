"""Round-trip coverage for export_org_patients → import_org_patients.

The importer exists so the published benchmark cohort (Zenodo 10.5281/zenodo.21430170,
an export_org_patients JSON document) can be loaded into a fresh deployment. These tests
pin the two properties that makes it usable for that: the round trip preserves the OMOP
facts, and an export that predates a schema change still imports.
"""
import json

from io import StringIO

import pytest
from django.core.management import call_command, CommandError

from omop_core.models import (
    ConditionOccurrence,
    Measurement,
    Observation,
    Organization,
    PatientRecord,
    Person,
)
from tests.factories import (
    ConceptFactory,
    ConditionOccurrenceFactory,
    MeasurementFactory,
    ObservationFactory,
    OrganizationFactory,
    PatientRecordFactory,
    PersonFactory,
)

pytestmark = pytest.mark.django_db


def _build_cohort(slug='src-org', n=2):
    """Create an org with n patients, each carrying a few OMOP rows."""
    org = OrganizationFactory(slug=slug, name=slug.title())
    for i in range(n):
        person = PersonFactory(year_of_birth=1960 + i)
        PatientRecordFactory(person=person, organization=org, disease='breast cancer')
        MeasurementFactory(
            person=person,
            measurement_concept=ConceptFactory(
                concept_name='Hemoglobin', concept_code='718-7'
            ),
            value_as_number=12.5 + i,
            measurement_source_value='718-7',
        )
        ObservationFactory(
            person=person,
            observation_concept=ConceptFactory(
                concept_name='ECOG performance status', concept_code='ECOG'
            ),
            value_as_number=1,
            observation_source_value='ECOG',
        )
        ConditionOccurrenceFactory(
            person=person,
            condition_concept=ConceptFactory(
                concept_name='Malignant neoplasm of breast', concept_code='254837009'
            ),
            condition_source_value='254837009',
        )
    return org


def _export(tmp_path, slug='src-org'):
    path = tmp_path / 'export.json'
    call_command('export_org_patients', org=slug, output=str(path), stdout=StringIO())
    return path


def _wipe_source(org):
    """Remove the source cohort so the import lands in a clean namespace."""
    Person.objects.all().delete()
    org.delete()


class TestRoundTrip:
    def test_import_recreates_patients_and_omop_rows(self, tmp_path):
        org = _build_cohort(n=2)
        path = _export(tmp_path)
        _wipe_source(org)
        assert PatientRecord.objects.count() == 0

        call_command(
            'import_org_patients', str(path),
            org='dest-org', create_org=True, stdout=StringIO(),
        )

        assert Organization.objects.filter(slug='dest-org').exists()
        assert PatientRecord.objects.filter(organization__slug='dest-org').count() == 2
        assert Measurement.objects.count() == 2
        assert Observation.objects.count() == 2
        assert ConditionOccurrence.objects.count() == 2

    def test_source_values_survive_the_round_trip(self, tmp_path):
        """The benchmark reads *_source_value as its LOINC fallback."""
        org = _build_cohort(n=1)
        path = _export(tmp_path)
        _wipe_source(org)

        call_command(
            'import_org_patients', str(path),
            org='dest-org', create_org=True, stdout=StringIO(),
        )

        assert Measurement.objects.first().measurement_source_value == '718-7'
        assert ConditionOccurrence.objects.first().condition_source_value == '254837009'

    def test_input_may_be_given_as_a_flag(self, tmp_path):
        """The published Zenodo record documents `--input`; both spellings work."""
        org = _build_cohort(n=1)
        path = _export(tmp_path)
        _wipe_source(org)

        call_command(
            'import_org_patients',
            input_flag=str(path), org='dest-org', create_org=True, stdout=StringIO(),
        )

        assert PatientRecord.objects.filter(organization__slug='dest-org').count() == 1

    def test_missing_input_is_a_command_error(self):
        with pytest.raises(CommandError, match='positional argument or with --input'):
            call_command('import_org_patients', org='dest-org', stdout=StringIO())

    def test_dry_run_writes_nothing(self, tmp_path):
        org = _build_cohort(n=2)
        path = _export(tmp_path)
        _wipe_source(org)

        call_command(
            'import_org_patients', str(path),
            org='dest-org', create_org=True, dry_run=True, stdout=StringIO(),
        )

        assert PatientRecord.objects.count() == 0
        assert Measurement.objects.count() == 0


class TestPatientRecordMode:
    """Derived by default; snapshot only when asked."""

    def test_default_derives_the_projection_from_omop(self, tmp_path):
        org = _build_cohort(n=1)
        path = _export(tmp_path)
        _wipe_source(org)

        call_command(
            'import_org_patients', str(path),
            org='dest-org', create_org=True, stdout=StringIO(),
        )

        record = PatientRecord.objects.get(organization__slug='dest-org')
        # Derivation reads the Person row it just imported.
        assert record.patient_age is not None
        assert record.organization.slug == 'dest-org'

    def test_snapshot_mode_preserves_an_underivable_export_value(self, tmp_path):
        """A projection value with no OMOP row behind it survives only under --snapshot."""
        org = _build_cohort(n=1)
        record = PatientRecord.objects.get(organization=org)
        record.disease = 'enriched-value-with-no-omop-row'
        record.save(update_fields=['disease'])
        path = _export(tmp_path)
        _wipe_source(org)

        call_command(
            'import_org_patients', str(path),
            org='dest-org', create_org=True,
            snapshot_patient_record=True, stdout=StringIO(),
        )

        imported = PatientRecord.objects.get(organization__slug='dest-org')
        assert imported.disease == 'enriched-value-with-no-omop-row'
        assert imported.organization.slug == 'dest-org'

    def test_derived_mode_sets_the_target_org(self, tmp_path):
        """refresh_patient_record builds a bare record when none exists — org must survive."""
        org = _build_cohort(n=1)
        path = _export(tmp_path)
        _wipe_source(org)

        call_command(
            'import_org_patients', str(path),
            org='dest-org', create_org=True, stdout=StringIO(),
        )

        assert PatientRecord.objects.exclude(organization__slug='dest-org').count() == 0


class TestSchemaDrift:
    def test_export_field_absent_from_the_model_is_dropped_not_fatal(self, tmp_path):
        """An export outlives schema changes; a retired column must not abort the import."""
        org = _build_cohort(n=1)
        path = _export(tmp_path)
        _wipe_source(org)

        payload = json.loads(path.read_text())
        payload['patients'][0]['patient_record']['column_removed_last_release'] = 'x'
        path.write_text(json.dumps(payload))

        out = StringIO()
        call_command(
            'import_org_patients', str(path),
            org='dest-org', create_org=True,
            snapshot_patient_record=True, stdout=out,
        )

        assert PatientRecord.objects.filter(organization__slug='dest-org').count() == 1
        assert 'column_removed_last_release' in out.getvalue()

    def test_unknown_field_warning_is_emitted_once_not_per_patient(self, tmp_path):
        org = _build_cohort(n=3)
        path = _export(tmp_path)
        _wipe_source(org)

        payload = json.loads(path.read_text())
        for entry in payload['patients']:
            entry['patient_record']['column_removed_last_release'] = 'x'
        path.write_text(json.dumps(payload))

        out = StringIO()
        call_command(
            'import_org_patients', str(path),
            org='dest-org', create_org=True,
            snapshot_patient_record=True, stdout=out,
        )

        assert out.getvalue().count('column_removed_last_release') == 1


class TestOrgResolution:
    def test_missing_org_without_create_flag_is_an_error(self, tmp_path):
        org = _build_cohort(n=1)
        path = _export(tmp_path)
        _wipe_source(org)

        with pytest.raises(CommandError):
            call_command(
                'import_org_patients', str(path), org='nope', stdout=StringIO(),
            )
