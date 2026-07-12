from datetime import date
from io import StringIO

import pytest
from django.core.management import call_command

from omop_core.models import Organization
from tests.factories import PatientRecordFactory, PersonFactory

pytestmark = pytest.mark.django_db


def _make_org(slug):
    return Organization.objects.create(slug=slug, name=slug)


def test_scopes_refresh_to_org_slugs_with_limit(monkeypatch):
    org = _make_org('synthea-bc')
    other_org = _make_org('other-org')

    p1 = PersonFactory(person_id=101)
    p2 = PersonFactory(person_id=102)
    p3 = PersonFactory(person_id=103)
    p4 = PersonFactory(person_id=104)

    PatientRecordFactory(person=p1, organization=org, diagnosis_date=date(2024, 1, 1))
    PatientRecordFactory(person=p2, organization=org, diagnosis_date=date(2024, 1, 2))
    PatientRecordFactory(person=p3, organization=org, diagnosis_date=date(2024, 1, 3))
    PatientRecordFactory(person=p4, organization=other_org, diagnosis_date=date(2024, 1, 4))

    refreshed = []

    monkeypatch.setattr(
        'omop_core.management.commands.populate_patient_record.refresh_patient_record',
        lambda person: refreshed.append(person.person_id),
    )

    call_command(
        'populate_patient_record',
        org_slugs='synthea-bc',
        limit=2,
        stdout=StringIO(),
    )

    assert refreshed == [101, 102]


def test_scopes_refresh_to_explicit_person_ids(monkeypatch):
    p1 = PersonFactory(person_id=201)
    PersonFactory(person_id=202)
    p3 = PersonFactory(person_id=203)

    refreshed = []

    monkeypatch.setattr(
        'omop_core.management.commands.populate_patient_record.refresh_patient_record',
        lambda person: refreshed.append(person.person_id),
    )

    call_command(
        'populate_patient_record',
        person_ids='203,201',
        stdout=StringIO(),
    )

    assert refreshed == [201, 203]


def test_rejects_conflicting_person_id_flags():
    PersonFactory(person_id=301)
    stdout = StringIO()

    call_command(
        'populate_patient_record',
        person_id=301,
        person_ids='301,302',
        stdout=stdout,
    )

    assert 'either --person-id or --person-ids' in stdout.getvalue()
