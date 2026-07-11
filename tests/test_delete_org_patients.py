from io import StringIO

import pytest
from django.core.management import call_command, CommandError

from omop_core.models import (
    ConditionOccurrence,
    Concept,
    DrugExposure,
    Measurement,
    MeasurementOwnership,
    Organization,
    PatientGroup,
    PatientGroupMembership,
    PatientRecord,
    PersonalRepresentative,
    Person,
    VisitOccurrence,
)
from omop_core.services.patient_cleanup import delete_omop_clinical_rows_bulk
from omop_oncology.models import Episode, EpisodeEvent
from patient_portal.models import Identity, PatientUser
from tests.factories import ConceptFactory, MeasurementFactory, PatientRecordFactory, PersonFactory

pytestmark = pytest.mark.django_db


def _create_org(slug, name=None):
    return Organization.objects.create(name=name or slug.title(), slug=slug)


def _concept(name, code):
    return ConceptFactory(concept_name=name, concept_code=code)


def _create_visit(person):
    visit_concept = _concept('Visit concept', f'VISIT-{person.person_id}')
    visit_type_concept = _concept('Visit type concept', f'VISIT-TYPE-{person.person_id}')
    return VisitOccurrence.objects.create(
        visit_occurrence_id=person.person_id * 10,
        person=person,
        visit_concept=visit_concept,
        visit_start_date='2024-01-01',
        visit_end_date='2024-01-02',
        visit_type_concept=visit_type_concept,
    )


def _create_episode(person):
    episode_concept = _concept('Episode concept', f'EP-{person.person_id}')
    object_concept = _concept('Episode object', f'EP-OBJ-{person.person_id}')
    type_concept = _concept('Episode type', f'EP-TYPE-{person.person_id}')
    return Episode.objects.create(
        episode_id=person.person_id * 100,
        person=person,
        episode_concept=episode_concept,
        episode_start_date='2024-01-01',
        episode_object_concept=object_concept,
        episode_type_concept=type_concept,
    )


def _seed_patient_graph(person, org):
    record = PatientRecordFactory(person=person, organization=org)

    identity = Identity.objects.create(
        issuer='urn:test',
        sub=f'sub-{person.person_id}',
        email=f'user{person.person_id}@example.com',
    )
    patient_user = PatientUser.objects.create(identity=identity, person=person)

    measurement_concept = _concept('Measurement concept', f'MEAS-{person.person_id}')
    measurement_type_concept = _concept('Measurement type', f'MEAS-TYPE-{person.person_id}')
    measurement = MeasurementFactory(
        person=person,
        measurement_id=person.person_id * 1000,
        measurement_concept=measurement_concept,
        measurement_type_concept=measurement_type_concept,
    )

    visit = _create_visit(person)
    MeasurementOwnership.objects.create(
        measurement_id=measurement.measurement_id,
        visit_occurrence_id=visit.visit_occurrence_id,
    )

    condition_concept = _concept('Condition concept', f'COND-{person.person_id}')
    condition_type_concept = _concept('Condition type', f'COND-TYPE-{person.person_id}')
    ConditionOccurrence.objects.create(
        condition_occurrence_id=person.person_id * 2000,
        person=person,
        condition_concept=condition_concept,
        condition_start_date='2024-01-03',
        condition_type_concept=condition_type_concept,
    )

    drug_concept = _concept('Drug concept', f'DRUG-{person.person_id}')
    drug_type_concept = _concept('Drug type', f'DRUG-TYPE-{person.person_id}')
    DrugExposure.objects.create(
        drug_exposure_id=person.person_id * 3000,
        person=person,
        drug_concept=drug_concept,
        drug_exposure_start_date='2024-01-04',
        drug_exposure_end_date='2024-01-05',
        drug_type_concept=drug_type_concept,
    )

    episode = _create_episode(person)
    episode_event_concept = _concept('Episode event field', f'EP-EVENT-{person.person_id}')
    EpisodeEvent.objects.create(
        episode_id=episode.episode_id,
        event_id=person.person_id * 4000,
        episode_event_field_concept=episode_event_concept,
    )

    group = PatientGroup.objects.create(
        organization=org,
        name=f'Group {person.person_id}',
        slug=f'group-{person.person_id}',
    )
    PatientGroupMembership.objects.create(group=group, person_id=person.person_id)

    rep_identity = Identity.objects.create(
        issuer='urn:test',
        sub=f'rep-{person.person_id}',
        email=f'rep{person.person_id}@example.com',
    )
    PersonalRepresentative.objects.create(
        representative=rep_identity,
        person_id=person.person_id,
        relationship='guardian',
    )

    return {
        'record': record,
        'identity': identity,
        'patient_user': patient_user,
        'measurement': measurement,
        'visit': visit,
        'episode': episode,
        'group': group,
        'rep_identity': rep_identity,
    }


def test_requires_confirmation_for_destructive_run():
    org = _create_org('org-delete-guard')
    person = PersonFactory()
    _seed_patient_graph(person, org)

    with pytest.raises(CommandError, match='Re-run with --confirm'):
        call_command('delete_org_patients', org=org.slug)

    assert Organization.objects.filter(pk=org.pk).exists()
    assert PatientRecord.objects.filter(person=person).exists()
    assert Person.objects.filter(pk=person.pk).exists()
    assert Identity.objects.filter(email=f'user{person.person_id}@example.com').exists()


def test_dry_run_reports_without_deleting():
    org = _create_org('org-delete-dry-run')
    person = PersonFactory()
    _seed_patient_graph(person, org)

    stdout = StringIO()
    call_command('delete_org_patients', org=org.slug, dry_run=True, stdout=stdout)

    output = stdout.getvalue()
    assert 'Dry run only; nothing was deleted.' in output
    assert Organization.objects.filter(pk=org.pk).exists()
    assert PatientRecord.objects.filter(person=person).exists()
    assert Person.objects.filter(pk=person.pk).exists()
    assert Measurement.objects.filter(person=person).exists()
    assert Episode.objects.filter(person=person).exists()
    assert MeasurementOwnership.objects.filter(measurement_id=person.person_id * 1000).exists()
    assert PatientGroupMembership.objects.filter(person_id=person.person_id).exists()
    assert PersonalRepresentative.objects.filter(person_id=person.person_id).exists()


def test_deletes_org_and_all_patient_data():
    org = _create_org('org-delete-live')
    keep_org = _create_org('org-keep-live')

    delete_person = PersonFactory()
    _seed_patient_graph(delete_person, org)

    keep_person = PersonFactory()
    PatientRecordFactory(person=keep_person, organization=keep_org)

    call_command('delete_org_patients', org=org.slug, confirm=True)

    assert not Organization.objects.filter(pk=org.pk).exists()
    assert PatientRecord.objects.filter(person=delete_person).count() == 0
    assert Person.objects.filter(pk=delete_person.pk).count() == 0
    assert PatientUser.objects.filter(person=delete_person).count() == 0
    assert Identity.objects.filter(email=f'user{delete_person.person_id}@example.com').count() == 0
    assert Measurement.objects.filter(person=delete_person).count() == 0
    assert ConditionOccurrence.objects.filter(person=delete_person).count() == 0
    assert DrugExposure.objects.filter(person=delete_person).count() == 0
    assert VisitOccurrence.objects.filter(person=delete_person).count() == 0
    assert Episode.objects.filter(person=delete_person).count() == 0
    assert EpisodeEvent.objects.filter(episode_id=delete_person.person_id * 100).count() == 0
    assert MeasurementOwnership.objects.filter(measurement_id=delete_person.person_id * 1000).count() == 0
    assert PatientGroupMembership.objects.filter(person_id=delete_person.person_id).count() == 0
    assert PersonalRepresentative.objects.filter(person_id=delete_person.person_id).count() == 0

    assert Organization.objects.filter(pk=keep_org.pk).exists()
    assert PatientRecord.objects.filter(person=keep_person, organization=keep_org).exists()
    assert Person.objects.filter(pk=keep_person.pk).exists()


def test_bulk_patient_cleanup_deletes_rows_for_multiple_people():
    org = _create_org('org-bulk-delete-live')
    person_a = PersonFactory()
    person_b = PersonFactory()
    _seed_patient_graph(person_a, org)
    _seed_patient_graph(person_b, org)

    delete_omop_clinical_rows_bulk([person_a.person_id, person_b.person_id])

    for person in (person_a, person_b):
        assert Measurement.objects.filter(person=person).count() == 0
        assert ConditionOccurrence.objects.filter(person=person).count() == 0
        assert DrugExposure.objects.filter(person=person).count() == 0
        assert VisitOccurrence.objects.filter(person=person).count() == 0
        assert Episode.objects.filter(person=person).count() == 0
        assert EpisodeEvent.objects.filter(episode_id=person.person_id * 100).count() == 0
        assert MeasurementOwnership.objects.filter(measurement_id=person.person_id * 1000).count() == 0
        assert PatientGroupMembership.objects.filter(person_id=person.person_id).count() == 0
        assert PersonalRepresentative.objects.filter(person_id=person.person_id).count() == 0
