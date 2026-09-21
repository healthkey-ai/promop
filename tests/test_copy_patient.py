"""copy_patient: one patient crosses instances with new ids and every link remapped.

Source and target are the test database: a patient is read, then written as
another person_id, which exercises the same remapping a real copy needs.
"""
from datetime import datetime, timezone
from io import StringIO
from typing import Any

import pytest
from django.contrib.contenttypes.models import ContentType
from django.core.management import call_command
from django.core.management.base import CommandError

from omop_core.models import (
    CareSite,
    Concept,
    DrugExposure,
    Location,
    Measurement,
    MeasurementOwnership,
    Note,
    NoteNlp,
    PatientDocument,
    PatientRecord,
    Person,
    ProvenanceRecord,
    RecordRevision,
    SupportiveTherapyCourse,
    TherapyRegimen,
    VisitOccurrence,
)
from omop_core.services.patient_transfer import (
    COPY_SOURCE,
    PatientCopyError,
    PatientCopyStats,
    apply_patient,
    copied_person_id,
    read_patient,
    select_person_ids,
)
from omop_core.signals import suppress_patient_record_refresh
from omop_oncology.models import Episode, EpisodeEvent
from patient_portal.models import Identity, PatientUser
from tests.factories import (
    ConceptFactory,
    DrugExposureFactory,
    MeasurementFactory,
    OrganizationFactory,
    PersonFactory,
    VocabularyFactory,
)

pytestmark = pytest.mark.django_db

SOURCE_ID = 501
TARGET_ID = 902


def _cdm(concept_id: int, name: str) -> Concept:
    """A CDM field concept, named 'table.column' like the Athena ones."""
    return ConceptFactory(
        concept_id=concept_id, concept_name=name, concept_code=f'CDM{concept_id}',
        vocabulary=VocabularyFactory(vocabulary_id='CDM', vocabulary_name='CDM'),
    )


@pytest.fixture
def patient() -> Person:
    """A source patient with visits, events, notes, provenance and a record."""
    with suppress_patient_record_refresh():
        return _build_patient()


def _build_patient() -> Person:
    ConceptFactory(concept_id=0, concept_code='No matching concept')
    org = OrganizationFactory(slug='source-org')
    location = Location.objects.create(location_id=77, city='Tulsa')
    person = PersonFactory(person_id=SOURCE_ID, location_id=location.location_id)
    concept = ConceptFactory(concept_id=3_000_001, concept_code='C1')
    visit = VisitOccurrence.objects.create(
        visit_occurrence_id=10, person=person, visit_concept=concept, visit_type_concept=concept,
        visit_start_date='2024-01-01', visit_end_date='2024-01-02',
    )
    drug = DrugExposureFactory(drug_exposure_id=20, person=person, visit_occurrence=visit)
    episode = Episode.objects.create(
        episode_id=30, person=person, episode_concept=concept, episode_object_concept=concept,
        episode_type_concept=concept, episode_start_date='2024-01-01',
    )
    measurement = MeasurementFactory(
        measurement_id=40, person=person, visit_occurrence=visit,
        measurement_event_id=episode.episode_id, meas_event_field_concept=_cdm(1_147_000, 'episode.episode_id'),
    )
    EpisodeEvent.objects.create(
        episode_id=episode.episode_id, event_id=drug.drug_exposure_id,
        episode_event_field_concept=_cdm(1_147_094, 'drug_exposure.drug_exposure_id'),
    )
    MeasurementOwnership.objects.create(measurement_id=measurement.measurement_id, visit_occurrence_id=visit.visit_occurrence_id)
    note = Note.objects.create(
        note_id=50, person=person, note_date='2024-01-01', note_type_concept=concept, note_text='x',
    )
    NoteNlp.objects.create(note_nlp_id=60, note=note, lexical_variant='x')
    record = PatientRecord.objects.create(
        person=person, organization=org, smoking_status='never', user_edited_fields=['smoking_status'],
    )
    RecordRevision.objects.create(patient_record=record, field='smoking_status', new_value='never')
    ProvenanceRecord.objects.create(
        source='etl', source_user_id='u1', target_patient_id=str(SOURCE_ID),
        content_type=ContentType.objects.get_for_model(Measurement), object_id=measurement.measurement_id,
    )
    regimen = TherapyRegimen.objects.create(code='r-chop', title='R-CHOP')
    SupportiveTherapyCourse.objects.create(person=person, regimen=regimen)
    return person


def _copy(**kwargs: Any) -> PatientCopyStats:
    """Copy the source patient as TARGET_ID into a fresh org."""
    kwargs.setdefault('target_person_id', TARGET_ID)
    return apply_patient(read_patient('default', SOURCE_ID), OrganizationFactory(slug='target-org'), **kwargs)


def test_every_row_is_copied_under_new_ids(patient: Person):
    stats = _copy()

    assert stats.person_id == TARGET_ID
    for model in (VisitOccurrence, DrugExposure, Measurement, Episode, Note):
        source = model.objects.get(person_id=SOURCE_ID)
        target = model.objects.get(person_id=TARGET_ID)
        assert source.pk != target.pk
    assert NoteNlp.objects.get(note__person_id=TARGET_ID).note_id != 50
    # The address is shared, not duplicated: it is the same address.
    assert Person.objects.get(person_id=TARGET_ID).location_id == 77


def test_links_point_at_the_copied_rows(patient: Person):
    _copy()

    visit = VisitOccurrence.objects.get(person_id=TARGET_ID)
    drug = DrugExposure.objects.get(person_id=TARGET_ID)
    episode = Episode.objects.get(person_id=TARGET_ID)
    measurement = Measurement.objects.get(person_id=TARGET_ID)
    assert (drug.visit_occurrence_id, measurement.visit_occurrence_id) == (visit.pk, visit.pk)
    assert measurement.measurement_event_id == episode.pk
    assert EpisodeEvent.objects.filter(episode_id=episode.pk, event_id=drug.pk).exists()
    assert MeasurementOwnership.objects.filter(measurement_id=measurement.pk, visit_occurrence_id=visit.pk).exists()
    provenance = ProvenanceRecord.objects.get(object_id=measurement.pk)
    assert provenance.target_patient_id == str(TARGET_ID)
    assert SupportiveTherapyCourse.objects.get(person_id=TARGET_ID).regimen.code == 'r-chop'


def test_patient_record_joins_the_org_and_keeps_user_edits(patient: Person):
    _copy()

    record = PatientRecord.objects.get(person_id=TARGET_ID)
    assert record.organization.slug == 'target-org'
    assert record.smoking_status == 'never'
    assert RecordRevision.objects.filter(patient_record=record, field='smoking_status').exists()


def test_missing_concept_becomes_zero_where_required(patient: Person):
    payload = read_patient('default', SOURCE_ID)
    missing = next(iter(ref for ref in payload['concepts'].values() if ref[1] == 'C1'))
    Concept.objects.filter(concept_code='C1').update(concept_code='C1-renamed-here')

    stats = apply_patient(payload, OrganizationFactory(slug='target-org'), target_person_id=TARGET_ID)

    assert VisitOccurrence.objects.get(person_id=TARGET_ID).visit_concept_id == 0
    assert stats.missing_concepts[f'{missing[0]}:C1'] > 0


def test_event_links_resolve_without_the_cdm_vocabulary_here(patient: Person):
    payload = read_patient('default', SOURCE_ID)
    for concept in Concept.objects.filter(vocabulary_id='CDM'):
        Concept.objects.filter(pk=concept.pk).update(concept_code=f'not-loaded-{concept.pk}')

    stats = apply_patient(payload, OrganizationFactory(slug='target-org'), target_person_id=TARGET_ID)

    drug = DrugExposure.objects.get(person_id=TARGET_ID)
    episode = Episode.objects.get(person_id=TARGET_ID)
    assert EpisodeEvent.objects.filter(episode_id=episode.pk, event_id=drug.pk).exists()
    assert Measurement.objects.get(person_id=TARGET_ID).measurement_event_id == episode.pk
    assert not stats.skipped


def test_org_is_assigned_even_when_the_source_record_has_none(patient: Person):
    PatientRecord.objects.filter(person_id=SOURCE_ID).update(organization=None)
    _copy()

    assert PatientRecord.objects.get(person_id=TARGET_ID).organization.slug == 'target-org'


def test_org_is_assigned_when_the_source_has_no_record(patient: Person):
    PatientRecord.objects.filter(person_id=SOURCE_ID).delete()
    _copy()

    assert PatientRecord.objects.get(person_id=TARGET_ID).organization.slug == 'target-org'


def test_source_timestamps_survive_auto_now_fields(patient: Person):
    past = datetime(2020, 5, 1, tzinfo=timezone.utc)
    RecordRevision.objects.filter(patient_record__person_id=SOURCE_ID).update(changed_at=past)
    _copy()

    assert RecordRevision.objects.get(patient_record__person_id=TARGET_ID).changed_at == past


def test_revision_author_is_cleared_but_system_is_kept(patient: Person):
    record = PatientRecord.objects.get(person_id=SOURCE_ID)
    RecordRevision.objects.filter(patient_record=record).update(changed_by='592')
    RecordRevision.objects.create(patient_record=record, field='disease', changed_by='system')
    _copy()

    authors = set(RecordRevision.objects.filter(patient_record__person_id=TARGET_ID).values_list('changed_by', flat=True))
    assert authors == {None, 'system'}


def test_provenance_on_the_person_is_copied(patient: Person):
    ProvenanceRecord.objects.create(
        source='etl', source_user_id='u1', content_type=ContentType.objects.get_for_model(Person),
        object_id=SOURCE_ID,
    )
    _copy()

    assert ProvenanceRecord.objects.filter(
        content_type=ContentType.objects.get_for_model(Person), object_id=TARGET_ID,
    ).exists()


def test_replace_keeps_a_location_a_care_site_uses(patient: Person):
    PersonFactory(person_id=TARGET_ID, location_id=88)
    Location.objects.create(location_id=88, city='Clinic town')
    CareSite.objects.create(care_site_id=1, care_site_name='Clinic', location_id=88)
    _copy(replace=True)

    assert CareSite.objects.get(care_site_id=1).location_id == 88


def test_person_id_comes_from_this_database(patient: Person):
    """Source ids belong to the source, where the same number is somebody else."""
    stranger = PersonFactory(person_id=SOURCE_ID + 1)

    stats = apply_patient(read_patient('default', SOURCE_ID), OrganizationFactory(slug='target-org'))

    assert stats.person_id not in (SOURCE_ID, stranger.person_id)
    assert Measurement.objects.filter(person_id=stats.person_id).count() == 1


def test_copying_twice_needs_replace_and_does_not_duplicate(patient: Person):
    org = OrganizationFactory(slug='target-org')
    first = apply_patient(read_patient('default', SOURCE_ID), org)

    with pytest.raises(PatientCopyError, match='already copied'):
        apply_patient(read_patient('default', SOURCE_ID), org)

    second = apply_patient(read_patient('default', SOURCE_ID), org, replace=True)
    assert not Person.objects.filter(person_id=first.person_id).exists()
    assert Person.objects.exclude(person_id=SOURCE_ID).count() == 1
    assert Measurement.objects.filter(person_id=second.person_id).count() == 1


def test_a_copy_survives_the_patient_being_deleted_elsewhere(patient: Person):
    """The marker holds a generic foreign key, so nothing cascades with the Person.

    `delete_patient` clears it, but the other ways a Person goes — account
    self-deletion, admin delete, either bulk delete, bulk_import_fhir_bundle —
    do not. A marker naming a patient who is gone used to answer "already
    copied here" with a person_id that no longer resolves, so the source
    patient could not be copied again: plain re-copy raised that error, and
    --replace raised Person.DoesNotExist out of delete_patient.
    """
    org = OrganizationFactory(slug='target-org')
    first = apply_patient(read_patient('default', SOURCE_ID), org)
    # Exactly what patient_portal/api/views.py does, rather than delete_patient.
    Person.objects.filter(person_id=first.person_id).delete()
    assert copied_person_id(SOURCE_ID) is None

    second = apply_patient(read_patient('default', SOURCE_ID), org)
    assert second.person_id not in (None, first.person_id)
    assert Measurement.objects.filter(person_id=second.person_id).count() == 1
    # And the marker left behind does not pile up.
    assert ProvenanceRecord.objects.filter(
        source=COPY_SOURCE, source_user_id=str(SOURCE_ID),
        content_type=ContentType.objects.get_for_model(Person),
    ).count() == 1


def test_replace_after_the_patient_was_deleted_elsewhere(patient: Person):
    """--replace has nothing to replace, which is not an error."""
    org = OrganizationFactory(slug='target-org')
    first = apply_patient(read_patient('default', SOURCE_ID), org)
    Person.objects.filter(person_id=first.person_id).delete()

    second = apply_patient(read_patient('default', SOURCE_ID), org, replace=True)
    assert Person.objects.filter(person_id=second.person_id).exists()
    assert copied_person_id(SOURCE_ID) == second.person_id


def test_a_live_copy_still_shadows_an_older_dead_one(patient: Person):
    """Only the dead marker is ignored — a patient who is still here is found."""
    org = OrganizationFactory(slug='target-org')
    first = apply_patient(read_patient('default', SOURCE_ID), org)
    Person.objects.filter(person_id=first.person_id).delete()
    second = apply_patient(read_patient('default', SOURCE_ID), org)

    assert copied_person_id(SOURCE_ID) == second.person_id
    with pytest.raises(PatientCopyError, match='already copied'):
        apply_patient(read_patient('default', SOURCE_ID), org)


def test_an_identical_address_is_reused(patient: Person):
    """The source address already exists here, so no second row for it."""
    stats = apply_patient(read_patient('default', SOURCE_ID), OrganizationFactory(slug='target-org'))

    assert Person.objects.get(person_id=stats.person_id).location_id == 77
    assert Location.objects.filter(city='Tulsa').count() == 1


def test_an_unknown_address_gets_its_own_row(patient: Person):
    payload = read_patient('default', SOURCE_ID)
    Location.objects.filter(location_id=77).update(city='Elsewhere')

    stats = apply_patient(payload, OrganizationFactory(slug='target-org'))

    location_id = Person.objects.get(person_id=stats.person_id).location_id
    assert location_id != 77
    assert Location.objects.get(location_id=location_id).city == 'Tulsa'


def test_replace_refuses_a_person_with_a_login(patient: Person):
    PatientUser.objects.create(
        person=PersonFactory(person_id=TARGET_ID),
        identity=Identity.objects.create(email='p@example.com'),
    )
    with pytest.raises(PatientCopyError, match='PatientUser'):
        _copy(replace=True)


def test_stored_documents_are_skipped(patient: Person):
    PatientDocument.objects.create(person=patient, doc_type='lab', file='docs/a.pdf')
    stats = _copy()

    assert not PatientDocument.objects.filter(person_id=TARGET_ID).exists()
    assert stats.skipped['PatientDocument'] == 1


def test_dry_run_writes_nothing(patient: Person):
    stats = _copy(dry_run=True)

    assert stats.created['Measurement'] == 1
    assert not Person.objects.filter(person_id=TARGET_ID).exists()


def test_command_requires_an_existing_org(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv('SOURCE_DATABASE_URL', 'postgresql://u:p@localhost/db')
    with pytest.raises(CommandError, match='No organization'):
        call_command('copy_patient', '1', '--org', 'nope')


def test_selection_by_org_and_ids_narrows(patient: Person):
    other = PersonFactory(person_id=777)
    PatientRecord.objects.create(person=other, organization=OrganizationFactory(slug='elsewhere'))
    source_org = PatientRecord.objects.get(person_id=SOURCE_ID).organization_id

    assert select_person_ids('default', organization_id=source_org) == [SOURCE_ID]
    assert select_person_ids('default', [SOURCE_ID, 777], organization_id=source_org) == [SOURCE_ID]
    assert select_person_ids('default', [SOURCE_ID, 777]) == [SOURCE_ID, 777]


def test_selection_needs_ids_or_a_filter():
    with pytest.raises(PatientCopyError, match='filter'):
        select_person_ids('default')


@pytest.fixture
def source_is_this_database(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the command read the test database as its source."""
    from types import SimpleNamespace

    from omop_core.management.commands import copy_patient

    monkeypatch.setattr(copy_patient, 'register_source_connection', lambda url: None)
    monkeypatch.setattr(copy_patient, 'SOURCE_ALIAS', 'default')
    # Closing the real default connection would break the test transaction.
    monkeypatch.setattr(copy_patient, 'connections', {'default': SimpleNamespace(close=lambda: None)})


def test_bulk_copies_the_org_and_reports_failures_without_stopping(patient: Person, source_is_this_database: None):
    OrganizationFactory(slug='target-org')
    source_org = PatientRecord.objects.get(person_id=SOURCE_ID).organization_id
    with suppress_patient_record_refresh():
        PatientRecord.objects.create(person=PersonFactory(person_id=502), organization_id=source_org)

    args = ['copy_patient', '--filter-org-id', str(source_org), '--org', 'target-org',
            '--source-url', 'postgresql://u:p@localhost/db']
    out = StringIO()
    call_command(*args, stdout=out)

    assert 'Copied 2 of 2 patients' in out.getvalue()
    assert PatientRecord.objects.filter(organization__slug='target-org').count() == 2
    # The source ids stay with the source patients.
    assert PatientRecord.objects.filter(person_id__in=[501, 502], organization__slug='target-org').count() == 0

    with pytest.raises(CommandError, match='Copied 0 of 2 patients'):
        call_command(*args)


def test_target_person_id_needs_exactly_one_patient(patient: Person, source_is_this_database: None):
    OrganizationFactory(slug='target-org')
    PersonFactory(person_id=777)
    with pytest.raises(CommandError, match='exactly one patient'):
        call_command(
            'copy_patient', str(SOURCE_ID), '777', '--target-person-id', '9', '--org', 'target-org',
            '--source-url', 'postgresql://u:p@localhost/db',
        )
