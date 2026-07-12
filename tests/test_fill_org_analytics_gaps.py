from datetime import date
from io import StringIO

import pytest
from django.core.management import call_command, CommandError

from omop_core.models import ConditionOccurrence, DrugExposure, Observation, Organization, PatientRecord
from tests.factories import ConditionOccurrenceFactory, ConceptFactory, PatientRecordFactory, PersonFactory

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _no_close_connections(monkeypatch):
    """Prevent close_old_connections() from killing the pytest-django test transaction."""
    monkeypatch.setattr(
        'omop_core.management.commands.fill_org_analytics_gaps.close_old_connections',
        lambda: None,
    )


def _make_org(slug, name):
    return Organization.objects.create(slug=slug, name=name)


def test_requires_confirm_or_dry_run():
    org = _make_org('bmm-foundation', 'BMM Foundation')
    PatientRecordFactory(organization=org)

    with pytest.raises(CommandError, match='Pass --dry-run to preview or --confirm to apply changes'):
        call_command('fill_org_analytics_gaps', org_slugs='bmm-foundation')


def test_backfills_analytics_fields_for_specified_org(monkeypatch):
    org = _make_org('bmm-foundation', 'BMM Foundation')
    other_org = _make_org('other-org', 'Other Org')

    person = PersonFactory()
    record = PatientRecordFactory(
        person=person,
        organization=org,
        diagnosis_date=None,
        first_line_therapy='VRd',
        first_line_date=date(2024, 1, 15),
        first_line_start_date=None,
        first_line_outcome='Partial Response',
        best_response=None,
        last_treatment=None,
        therapy_lines_count=None,
    )
    untouched = PatientRecordFactory(
        organization=other_org,
        diagnosis_date=None,
        first_line_therapy='Dara-Rd',
        first_line_date=date(2024, 3, 1),
        first_line_start_date=None,
        best_response=None,
    )

    populate_calls = []

    def _fake_call_command(name, **kwargs):
        assert name == 'populate_patient_record'
        populate_calls.append(kwargs['person_id'])

    monkeypatch.setattr(
        'omop_core.management.commands.fill_org_analytics_gaps.call_command',
        _fake_call_command,
    )

    call_command('fill_org_analytics_gaps', org_slugs='bmm-foundation', confirm=True, stdout=StringIO())

    record.refresh_from_db()
    untouched.refresh_from_db()

    assert ConditionOccurrence.objects.filter(person=person, condition_start_date=date(2024, 1, 15)).exists()
    assert DrugExposure.objects.filter(person=person, drug_source_value='VRd').exists()
    assert Observation.objects.filter(person=person, observation_source_value='182841002').exists()
    assert record.first_line_start_date == date(2024, 1, 15)
    assert record.diagnosis_date == date(2024, 1, 15)
    assert record.best_response == 'Partial Response'
    assert record.last_treatment == date(2024, 1, 15)
    assert populate_calls == [person.person_id]

    assert untouched.first_line_start_date is None
    assert untouched.best_response is None
    assert untouched.diagnosis_date is None


def test_backfills_condition_occurrence_even_when_unrelated_condition_exists_on_same_date(monkeypatch):
    org = _make_org('bmm-foundation', 'BMM Foundation')
    person = PersonFactory()
    PatientRecordFactory(
        person=person,
        organization=org,
        disease='Multiple Myeloma',
        disease_slug='multiple-myeloma',
        diagnosis_date=None,
        first_line_therapy='VRd',
        first_line_date=date(2024, 1, 15),
        first_line_start_date=None,
        first_line_outcome='Partial Response',
        best_response=None,
        last_treatment=None,
        therapy_lines_count=None,
    )
    ConditionOccurrenceFactory(
        person=person,
        condition_start_date=date(2024, 1, 15),
        condition_source_value='unrelated-condition',
        condition_concept=ConceptFactory(concept_name='Unrelated condition'),
    )

    monkeypatch.setattr(
        'omop_core.management.commands.fill_org_analytics_gaps.call_command',
        lambda *args, **kwargs: None,
    )

    call_command('fill_org_analytics_gaps', org_slugs='bmm-foundation', confirm=True, stdout=StringIO())

    matching_conditions = ConditionOccurrence.objects.filter(person=person, condition_start_date=date(2024, 1, 15))
    assert matching_conditions.count() == 2
    assert matching_conditions.filter(condition_concept__concept_code__startswith='ANALYTICS-').exists()


def test_backfills_open_ended_drug_exposure_without_forcing_an_end_date(monkeypatch):
    org = _make_org('bmm-foundation', 'BMM Foundation')
    person = PersonFactory()
    PatientRecordFactory(
        person=person,
        organization=org,
        diagnosis_date=None,
        first_line_therapy='VRd',
        first_line_date=date(2024, 1, 15),
        first_line_start_date=None,
        first_line_end_date=None,
        first_line_outcome='Partial Response',
        best_response=None,
        last_treatment=None,
        therapy_lines_count=None,
    )

    monkeypatch.setattr(
        'omop_core.management.commands.fill_org_analytics_gaps.call_command',
        lambda *args, **kwargs: None,
    )

    call_command('fill_org_analytics_gaps', org_slugs='bmm-foundation', confirm=True, stdout=StringIO())

    exposure = DrugExposure.objects.get(person=person, drug_source_value='VRd')
    assert exposure.drug_exposure_start_date == date(2024, 1, 15)
    assert exposure.drug_exposure_end_date is None
    assert exposure.drug_exposure_end_datetime is None


def test_dry_run_does_not_persist_changes(monkeypatch):
    org = _make_org('bmm-foundation', 'BMM Foundation')
    person = PersonFactory()
    record = PatientRecordFactory(
        person=person,
        organization=org,
        diagnosis_date=None,
        first_line_therapy='VRd',
        first_line_date=date(2024, 1, 15),
        first_line_start_date=None,
        first_line_outcome='Partial Response',
        best_response=None,
        last_treatment=None,
    )

    def _unexpected_populate(*args, **kwargs):
        raise AssertionError('populate_patient_record should not be called during dry-run')

    monkeypatch.setattr(
        'omop_core.management.commands.fill_org_analytics_gaps.call_command',
        _unexpected_populate,
    )

    stdout = StringIO()
    call_command('fill_org_analytics_gaps', org_slugs='bmm-foundation', dry_run=True, stdout=stdout)

    record.refresh_from_db()
    assert ConditionOccurrence.objects.filter(person=person).count() == 0
    assert DrugExposure.objects.filter(person=person).count() == 0
    assert Observation.objects.filter(person=person).count() == 0
    assert record.first_line_start_date is None
    assert record.best_response is None
    assert record.diagnosis_date is None
    assert 'would update' in stdout.getvalue()


def test_best_response_prefers_stronger_later_outcome(monkeypatch):
    org = _make_org('bmm-foundation', 'BMM Foundation')
    person = PersonFactory()
    record = PatientRecordFactory(
        person=person,
        organization=org,
        first_line_therapy='VRd',
        first_line_start_date=date(2024, 1, 1),
        first_line_outcome='Stable Disease',
        second_line_therapy='Dara-Rd',
        second_line_start_date=date(2024, 6, 1),
        second_line_outcome='Very Good Partial Response (VGPR)',
        best_response=None,
    )

    monkeypatch.setattr(
        'omop_core.management.commands.fill_org_analytics_gaps.call_command',
        lambda *args, **kwargs: None,
    )

    call_command('fill_org_analytics_gaps', org_slugs='bmm-foundation', confirm=True, stdout=StringIO())

    record.refresh_from_db()
    assert record.best_response == 'Very Good Partial Response (VGPR)'
    assert record.last_treatment == date(2024, 6, 1)
