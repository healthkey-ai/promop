from io import StringIO
from unittest.mock import patch

import pytest
from django.core.management import CommandError, call_command

from omop_core.models import Observation, PatientRecord
from omop_core.services.patient_record_service import SAMPLE_DISEASE_STATUS_SOURCE_VALUE, refresh_patient_record
from omop_core.services.sample_patient_disease_status import SAMPLE_DISEASE_STATUSES, ensure_sample_patient_disease_status
from omop_core.signals import suppress_patient_record_refresh
from tests.factories import ConceptFactory, ConditionOccurrenceFactory, ObservationFactory, OrganizationFactory, PatientRecordFactory

pytestmark = pytest.mark.django_db


@pytest.mark.parametrize('slug', ['synthea-bc', 'synthea-mm', 'synthea-fl', 'abc-foundation', 'bbc-foundation'])
def test_sample_status_is_durable_and_idempotent(slug):
    record = PatientRecordFactory(organization=OrganizationFactory(slug=slug), condition_clinical_status=None)
    with patch('omop_core.services.patient_record_service.refresh_patient_record') as refresh:
        call_command('backfill_sample_patient_disease_status', confirm=True)
        refresh.assert_not_called()
    record.refresh_from_db()
    status = record.condition_clinical_status
    assert status in SAMPLE_DISEASE_STATUSES
    fact = Observation.objects.get(person=record.person, observation_source_value=SAMPLE_DISEASE_STATUS_SOURCE_VALUE)
    assert fact.qualifier_source_value == 'synthetic fallback'
    assert fact.observation_concept_id == 0
    assert refresh_patient_record(record.person).condition_clinical_status == status
    call_command('backfill_sample_patient_disease_status', confirm=True)
    assert Observation.objects.filter(person=record.person).count() == 1


def test_preview_scope_and_existing_values():
    org = OrganizationFactory(slug='synthea-fl')
    missing = PatientRecordFactory(organization=org, condition_clinical_status=None)
    existing = PatientRecordFactory(organization=org, condition_clinical_status='remission')
    other = PatientRecordFactory(condition_clinical_status=None)
    call_command('backfill_sample_patient_disease_status')
    call_command('backfill_sample_patient_disease_status', confirm=True, dry_run=True)
    missing.refresh_from_db()
    assert missing.condition_clinical_status is None
    assert not Observation.objects.exists()
    call_command('backfill_sample_patient_disease_status', confirm=True)
    existing.refresh_from_db()
    other.refresh_from_db()
    assert existing.condition_clinical_status == 'remission'
    assert other.condition_clinical_status is None
    assert not Observation.objects.filter(person__in=[existing.person, other.person]).exists()
    with pytest.raises(CommandError, match='Only known sample'):
        call_command('backfill_sample_patient_disease_status', org_slugs=other.organization.slug, confirm=True)


@pytest.mark.parametrize('source,expected', [('active', 'active'), ('inactive', 'resolved'), ('remission', 'remission')])
def test_existing_unmapped_omop_status_wins(source, expected):
    record = PatientRecordFactory(condition_clinical_status=None)
    with suppress_patient_record_refresh():
        ConditionOccurrenceFactory(person=record.person, condition_status_concept_id=0, condition_status_source_value=source)
    assert ensure_sample_patient_disease_status(record) == (expected, 'existing OMOP')
    assert not Observation.objects.filter(observation_source_value=SAMPLE_DISEASE_STATUS_SOURCE_VALUE).exists()
    assert refresh_patient_record(record.person).condition_clinical_status == expected


def test_real_status_supersedes_fallback_on_import_refresh():
    record = PatientRecordFactory(condition_clinical_status=None)
    ensure_sample_patient_disease_status(record)
    with suppress_patient_record_refresh():
        ConditionOccurrenceFactory(person=record.person, condition_status_concept=ConceptFactory(concept_name='Remission'))
    assert refresh_patient_record(record.person).condition_clinical_status == 'remission'


def test_unknown_status_and_erroneous_fallback_are_ignored():
    record = PatientRecordFactory(condition_clinical_status='unknown')
    with suppress_patient_record_refresh():
        ConditionOccurrenceFactory(person=record.person, condition_status_source_value='unknown')
        ObservationFactory(person=record.person, observation_source_value=SAMPLE_DISEASE_STATUS_SOURCE_VALUE,
                           value_as_string='invalid', is_erroneous=True)
    status, source = ensure_sample_patient_disease_status(record)
    assert source == 'synthetic fallback'
    assert status in SAMPLE_DISEASE_STATUSES
    assert refresh_patient_record(record.person).condition_clinical_status == status


def test_preserves_progression_and_reuses_existing_fallback():
    record = PatientRecordFactory(condition_clinical_status=None, progression='stable')
    assert ensure_sample_patient_disease_status(record) == ('stable', 'preserved progression')
    PatientRecord.objects.filter(pk=record.pk).update(condition_clinical_status=None)
    record.refresh_from_db()
    assert ensure_sample_patient_disease_status(record) == ('stable', 'existing OMOP')
    assert Observation.objects.filter(person=record.person).count() == 1


def test_limit_and_invalid_arguments():
    org = OrganizationFactory(slug='synthea-fl')
    records = [PatientRecordFactory(organization=org, condition_clinical_status=None) for _ in range(2)]
    call_command('backfill_sample_patient_disease_status', org_slugs='SYNTHEA-FL', confirm=True, limit=1, stdout=StringIO())
    for record in records:
        record.refresh_from_db()
    assert records[0].condition_clinical_status
    assert records[1].condition_clinical_status is None
    for kwargs in [{'limit': 0}, {'org_slugs': ''}]:
        with pytest.raises(CommandError):
            call_command('backfill_sample_patient_disease_status', **kwargs)
