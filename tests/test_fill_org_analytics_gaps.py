from datetime import date
from io import StringIO

import pytest
from django.core.management import call_command, CommandError

from omop_core.models import ConditionOccurrence, DrugExposure, Observation, Organization, PatientRecord
from omop_oncology.models import Episode
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


def _refresh_from_db(person):
    return PatientRecord.objects.get(person=person)


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
        last_treatment=None,
        therapy_lines_count=None,
    )
    untouched = PatientRecordFactory(
        organization=other_org,
        diagnosis_date=None,
        first_line_therapy='Dara-Rd',
        first_line_date=date(2024, 3, 1),
        first_line_start_date=None,
    )

    refresh_calls = []

    def _fake_refresh(person_arg):
        refresh_calls.append(person_arg.person_id)
        return _refresh_from_db(person_arg)

    monkeypatch.setattr(
        'omop_core.management.commands.fill_org_analytics_gaps.refresh_patient_record',
        _fake_refresh,
    )

    call_command('fill_org_analytics_gaps', org_slugs='bmm-foundation', confirm=True, stdout=StringIO())

    record.refresh_from_db()
    untouched.refresh_from_db()

    assert ConditionOccurrence.objects.filter(person=person, condition_start_date=date(2024, 1, 15)).exists()
    assert DrugExposure.objects.filter(person=person, drug_source_value='VRd').exists()
    assert record.first_line_start_date == date(2024, 1, 15)
    assert record.diagnosis_date == date(2024, 1, 15)
    assert record.last_treatment == date(2024, 1, 15)
    assert refresh_calls == [person.person_id]

    assert untouched.first_line_start_date is None
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
        'omop_core.management.commands.fill_org_analytics_gaps.refresh_patient_record',
        _refresh_from_db,
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
        last_treatment=None,
        therapy_lines_count=None,
    )

    monkeypatch.setattr(
        'omop_core.management.commands.fill_org_analytics_gaps.refresh_patient_record',
        _refresh_from_db,
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
        last_treatment=None,
    )

    def _unexpected_populate(*args, **kwargs):
        raise AssertionError('refresh_patient_record should not be called during dry-run')

    monkeypatch.setattr(
        'omop_core.management.commands.fill_org_analytics_gaps.refresh_patient_record',
        _unexpected_populate,
    )

    stdout = StringIO()
    call_command('fill_org_analytics_gaps', org_slugs='bmm-foundation', dry_run=True, stdout=stdout)

    record.refresh_from_db()
    assert ConditionOccurrence.objects.filter(person=person).count() == 0
    assert DrugExposure.objects.filter(person=person).count() == 0
    assert Observation.objects.filter(person=person).count() == 0
    assert record.first_line_start_date is None
    assert record.diagnosis_date is None
    assert 'would update' in stdout.getvalue()


def test_last_treatment_uses_latest_treatment_start_when_no_end_dates(monkeypatch):
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
    )

    monkeypatch.setattr(
        'omop_core.management.commands.fill_org_analytics_gaps.refresh_patient_record',
        _refresh_from_db,
    )

    call_command('fill_org_analytics_gaps', org_slugs='bmm-foundation', confirm=True, stdout=StringIO())

    record.refresh_from_db()
    assert record.last_treatment == date(2024, 6, 1)


def test_backfills_lot_outcome_observations_from_existing_episodes(monkeypatch):
    org = _make_org('afl', 'AFL')
    person = PersonFactory()
    record = PatientRecordFactory(
        person=person,
        organization=org,
        first_line_therapy='R-CHOP',
        first_line_start_date=date(2023, 1, 1),
        first_line_outcome='Partial Response',
        second_line_therapy='BR',
        second_line_start_date=date(2023, 9, 1),
    )
    episode_concept = ConceptFactory(concept_name='Treatment regimen episode')
    object_concept = ConceptFactory(concept_name='Treatment regimen')
    type_concept = ConceptFactory(concept_name='EHR')
    Episode.objects.create(
        episode_id=1001,
        person=person,
        episode_concept=episode_concept,
        episode_object_concept=object_concept,
        episode_type_concept=type_concept,
        episode_number=1,
        episode_start_date=date(2023, 1, 1),
        episode_end_date=date(2023, 6, 1),
        episode_source_value='LOT-1',
    )
    Episode.objects.create(
        episode_id=1002,
        person=person,
        episode_concept=episode_concept,
        episode_object_concept=object_concept,
        episode_type_concept=type_concept,
        episode_number=2,
        episode_start_date=date(2023, 9, 1),
        episode_end_date=date(2024, 2, 1),
        episode_source_value='LOT-2',
    )

    monkeypatch.setattr(
        'omop_core.management.commands.fill_org_analytics_gaps.refresh_patient_record',
        _refresh_from_db,
    )

    call_command('fill_org_analytics_gaps', org_slugs='afl', confirm=True, stdout=StringIO())

    lot_1 = Observation.objects.get(person=person, observation_source_value='LOT-1-outcome')
    lot_2 = Observation.objects.get(person=person, observation_source_value='LOT-2-outcome')
    record.refresh_from_db()

    assert lot_1.value_as_string == 'Partial Response'
    assert lot_1.observation_date == date(2023, 6, 1)
    assert lot_2.value_as_string in {
        'Complete Response',
        'Partial Response',
        'Stable Disease',
        'Progressive Disease',
    }
