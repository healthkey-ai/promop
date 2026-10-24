"""Authoring identity belongs to a line, not a patient's name/date coincidence."""
from datetime import date

import pytest
from rest_framework.test import APIClient

from omop_core.models import Concept, DrugExposure, Observation
from omop_core.services.episode_service import author_therapy_line
from omop_core.services.mappings import CONCEPT_DRUG_EXPOSURE_FIELD, CONCEPT_TREATMENT_REGIMEN
from omop_oncology.models import Episode, EpisodeEvent
from patient_portal.models import Identity, PatientUser
from tests.factories import ConceptFactory, DrugExposureFactory, PatientRecordFactory
from tests.test_episode_service import _seed_episode_writer_concepts

pytestmark = pytest.mark.django_db
START, END = date(2025, 1, 1), date(2025, 4, 1)


@pytest.fixture
def setup():
    _seed_episode_writer_concepts()
    record = PatientRecordFactory()
    a = ConceptFactory(concept_name='Drug A')
    b = ConceptFactory(concept_name='Drug B')
    return record.person, a, b


def write(setup, **changes):
    person, a, _ = setup
    kwargs = dict(line_number=1, drugs=[{'concept_id': a.pk, 'source_value': 'reported-drug'}],
                  start_date=START, end_date=END)
    kwargs.update(changes)
    return author_therapy_line(person, **kwargs)


def exposure(result):
    return DrugExposure.objects.get(pk=result.drug_exposure_ids[0])


def test_same_name_and_date_in_two_lines_never_share_exposure(setup):
    one = write(setup)
    two = write(setup, line_number=2)
    assert one.episode.pk != two.episode.pk
    assert set(one.drug_exposure_ids).isdisjoint(two.drug_exposure_ids)


def test_owned_exposure_concept_and_end_date_are_corrected_in_place(setup):
    result = write(setup)
    _, _, b = setup
    corrected = write(setup, episode=result.episode, replace=True, end_date=None,
                      drugs=[{'drug_exposure_id': result.drug_exposure_ids[0],
                              'concept_id': b.pk, 'source_value': 'reported-drug', 'end_date': None}])
    assert corrected.drug_exposure_ids == result.drug_exposure_ids
    assert corrected.drugs_created == 0
    row = exposure(corrected)
    assert row.drug_concept_id == b.pk
    assert row.drug_exposure_end_date is None


def test_repeated_payload_is_idempotent(setup):
    first = write(setup)
    again = write(setup)
    assert again.episode.pk == first.episode.pk
    assert again.drug_exposure_ids == first.drug_exposure_ids
    assert again.drugs_created == 0


def test_a_late_invalid_drug_rolls_the_entire_direct_write_back(setup):
    _, a, _ = setup
    before = (DrugExposure.objects.count(), Episode.objects.count(), EpisodeEvent.objects.count())
    with pytest.raises(ValueError, match='Unknown drug'):
        write(setup, drugs=[{'concept_id': a.pk}, {'concept_id': 999999999}])
    assert (DrugExposure.objects.count(), Episode.objects.count(), EpisodeEvent.objects.count()) == before


def test_missing_grouping_vocabulary_never_leaves_exposures(setup):
    Concept.objects.filter(pk=CONCEPT_TREATMENT_REGIMEN).delete()
    result = write(setup)
    assert result.episode is None
    assert not DrugExposure.objects.filter(person=setup[0]).exists()


def test_unrelated_import_with_the_same_name_and_date_is_not_adopted(setup):
    person, a, _ = setup
    imported = DrugExposureFactory(person=person, drug_concept=a,
                                   drug_source_value='reported-drug', drug_exposure_start_date=START,
                                   drug_exposure_end_date=None)
    result = write(setup)
    assert imported.pk not in result.drug_exposure_ids
    imported.refresh_from_db()
    assert imported.drug_exposure_end_date is None


def test_shared_exposure_is_copied_before_correction(setup):
    original = write(setup)
    other = write(setup, line_number=2)
    EpisodeEvent.objects.create(episode_id=other.episode.pk, event_id=original.drug_exposure_ids[0],
                               episode_event_field_concept_id=CONCEPT_DRUG_EXPOSURE_FIELD)
    _, _, b = setup
    corrected = write(setup, episode=original.episode, replace=True,
                      drugs=[{'concept_id': b.pk, 'source_value': 'reported-drug'}])
    assert corrected.drug_exposure_ids != original.drug_exposure_ids
    assert exposure(original).drug_concept_id == setup[1].pk
    assert EpisodeEvent.objects.filter(episode_id=other.episode.pk,
                                       event_id=original.drug_exposure_ids[0]).exists()


def test_outcome_only_patch_preserves_drugs_dates_and_regimen(setup):
    person, a, b = setup
    result = write(setup, regimen_concept_id=a.pk)
    identity = Identity.objects.create_user(email='therapy-edit@example.test')
    PatientUser.objects.create(identity=identity, person=person)
    client = APIClient()
    client.force_authenticate(identity)
    response = client.patch(f'/api/v1/therapy-lines/{result.episode.pk}/',
                            {'intent': 'Curative'}, format='json')
    assert response.status_code == 200, response.data
    row = exposure(result)
    assert row.drug_exposure_start_date == START
    assert row.drug_exposure_end_date == END
    result.episode.refresh_from_db()
    assert result.episode.episode_source_concept_id == a.pk
    assert response.data['drug_exposure_ids'] == result.drug_exposure_ids


def test_regimen_correction_updates_both_concept_slots(setup):
    _, a, b = setup
    result = write(setup, regimen_concept_id=a.pk)
    corrected = write(setup, episode=result.episode, replace=True, regimen_concept_id=b.pk)
    assert corrected.episode.episode_source_concept_id == b.pk
    assert corrected.episode.episode_object_concept_id == b.pk


def test_omitted_service_fields_preserve_existing_state(setup):
    person, a, _ = setup
    result = write(setup, regimen_concept_id=a.pk, source_value='source assertion')
    corrected = author_therapy_line(person, line_number=1, episode=result.episode,
                                    intent='Curative', replace=True)
    assert corrected.drug_exposure_ids == result.drug_exposure_ids
    assert corrected.episode.episode_end_date == END
    assert corrected.episode.episode_source_value == 'source assertion'


def test_duplicate_source_keys_with_conflicting_concepts_are_rejected(setup):
    _, a, b = setup
    with pytest.raises(ValueError, match='Duplicate'):
        write(setup, drugs=[{'concept_id': a.pk, 'source_value': 'same'},
                            {'concept_id': b.pk, 'source_value': 'same'}])
    assert not DrugExposure.objects.filter(person=setup[0]).exists()


def test_unchanged_shared_fact_is_reused_without_adopting_or_copying(setup):
    one = write(setup)
    other = write(setup, line_number=2)
    EpisodeEvent.objects.create(episode_id=other.episode.pk, event_id=one.drug_exposure_ids[0],
                               episode_event_field_concept_id=CONCEPT_DRUG_EXPOSURE_FIELD)
    before = DrugExposure.objects.count()
    again = write(setup, episode=one.episode, replace=True)
    assert again.drug_exposure_ids == one.drug_exposure_ids
    assert DrugExposure.objects.count() == before


def test_explicit_metadata_clear_does_not_clear_omitted_metadata(setup):
    original = write(setup, outcome='Partial Response', intent='Curative')
    person, _, _ = setup
    author_therapy_line(person, line_number=1, episode=original.episode, replace=True,
                        outcome=None)
    assert not Observation.objects.filter(person=person, observation_source_value='LOT-1-outcome').exists()
    assert Observation.objects.get(person=person, observation_source_value='LOT-1-intent').value_as_string == 'Curative'


def test_unknown_drug_date_is_rejected_without_inventing_a_date(setup):
    with pytest.raises(ValueError, match='known start_date'):
        write(setup, start_date=None)
    assert not DrugExposure.objects.filter(person=setup[0]).exists()
    assert not Episode.objects.filter(person=setup[0]).exists()


def test_replaced_owned_fact_is_retained_but_not_used_in_the_projection(setup):
    from omop_core.services.patient_record_service import _build_snapshot

    original = write(setup)
    _, _, b = setup
    write(setup, episode=original.episode, replace=True,
          drugs=[{'concept_id': b.pk, 'source_value': 'new-drug'}])
    old = exposure(original)
    assert old.is_erroneous is True
    assert old.pk not in {de.pk for de in _build_snapshot(setup[0]).drug_exposures}


def test_unowned_linked_import_is_not_mutated_or_retracted_on_replace(setup):
    from omop_oncology.models import TherapyLineExposure

    original = write(setup)
    own = exposure(original)
    TherapyLineExposure.objects.filter(drug_exposure=own).delete()
    _, _, b = setup
    write(setup, episode=original.episode, replace=True,
          drugs=[{'concept_id': b.pk, 'source_value': 'reported-drug'}])
    own.refresh_from_db()
    assert own.drug_concept_id == setup[1].pk
    assert own.is_erroneous is False


@pytest.mark.parametrize('wrong_person', [False, True])
def test_wrong_table_or_patient_events_never_become_composition(setup, wrong_person):
    # Since #1924 the writer's assertion makes the line exist on its own. A link
    # that names the wrong table or another patient must still contribute no drug
    # and no component: the line stays asserted with an unknown composition.
    from omop_core.services.patient_record_service import refresh_patient_record
    from tests.factories import PersonFactory

    original = write(setup)
    EpisodeEvent.objects.filter(episode_id=original.episode.pk).delete()
    event_id = original.drug_exposure_ids[0]
    field = ConceptFactory().pk
    if wrong_person:
        other = DrugExposureFactory(person=PersonFactory())
        event_id = other.pk
        field = CONCEPT_DRUG_EXPOSURE_FIELD
    EpisodeEvent.objects.create(episode_id=original.episode.pk, event_id=event_id,
                               episode_event_field_concept_id=field)
    record = refresh_patient_record(setup[0])
    assert record.therapy_lines_count == 1
    assert record.first_line_component_ids == []
    # 'Unknown' is the projection's marker for a line whose regimen it cannot
    # name; the point is that neither a foreign drug nor a component leaks in.
    assert record.first_line_therapy == 'Unknown'
    assert record.first_line_therapy_id is None


def test_same_drug_and_date_in_two_cancers_have_distinct_owned_facts():
    from tests.test_lines_per_cancer import _concepts, _patient, _drug, PROSTATE

    drugs = _concepts()
    person = _patient()
    kwargs = dict(line_number=1, drugs=[_drug(drugs, 'dexamethasone')], start_date=START)
    primary = author_therapy_line(person, **kwargs)
    secondary = author_therapy_line(person, disease=PROSTATE, **kwargs)
    assert primary.episode.pk != secondary.episode.pk
    assert set(primary.drug_exposure_ids).isdisjoint(secondary.drug_exposure_ids)


def patient_client(person):
    identity = Identity.objects.create_user(email='therapy-safety-api@example.test')
    PatientUser.objects.create(identity=identity, person=person)
    client = APIClient()
    client.force_authenticate(identity)
    return client


def test_api_late_invalid_drug_is_atomic(setup):
    person, a, _ = setup
    response = patient_client(person).post('/api/v1/therapy-lines/', {
        'person': person.pk, 'line_number': 1, 'start_date': START.isoformat(),
        'drugs': [{'concept_id': a.pk}, {'concept_id': 999999999}],
    }, format='json')
    assert response.status_code == 400, response.data
    assert not DrugExposure.objects.filter(person=person).exists()
    assert not Episode.objects.filter(person=person).exists()


def test_api_missing_vocabulary_returns_conflict_without_orphan_facts(setup):
    person, a, _ = setup
    Concept.objects.filter(pk=CONCEPT_TREATMENT_REGIMEN).delete()
    response = patient_client(person).post('/api/v1/therapy-lines/', {
        'person': person.pk, 'line_number': 1, 'start_date': START.isoformat(),
        'drugs': [{'concept_id': a.pk}],
    }, format='json')
    assert response.status_code == 409, response.data
    assert not DrugExposure.objects.filter(person=person).exists()


def test_api_write_error_does_not_echo_exception_text(setup, monkeypatch):
    from omop_core.services import episode_service

    person, a, _ = setup

    def fail(*args, **kwargs):
        raise ValueError('internal patient detail 42')

    monkeypatch.setattr(episode_service, 'author_therapy_line', fail)
    response = patient_client(person).post('/api/v1/therapy-lines/', {
        'person': person.pk, 'line_number': 1, 'start_date': START.isoformat(),
        'drugs': [{'concept_id': a.pk}],
    }, format='json')
    assert response.status_code == 400, response.data
    assert response.data['detail'] == 'The therapy line could not be saved as submitted.'
    assert 'internal patient detail' not in str(response.data)


def test_api_retract_error_does_not_echo_exception_text(setup, monkeypatch):
    from omop_core.api.views import TherapyLineViewSet
    from omop_core.services import episode_service
    from rest_framework.test import APIRequestFactory

    original = write(setup)

    def fail(*args, **kwargs):
        raise ValueError('internal retract detail 43')

    monkeypatch.setattr(episode_service, 'retract_therapy_line', fail)
    # Call the packaged view directly: the API client's patient role is denied
    # DELETE by the host permission classes, which is not what this test covers.
    response = TherapyLineViewSet().destroy(
        APIRequestFactory().delete('/v1/therapy-lines/'),
        pk=str(original.episode.pk),
    )
    assert response.status_code == 400, response.data
    assert response.data['detail'] == 'The therapy line could not be retracted.'
    assert 'internal retract detail' not in str(response.data)


def test_api_end_date_only_clear_updates_line_not_individual_administration(setup):
    original = write(setup)
    response = patient_client(setup[0]).patch(f'/api/v1/therapy-lines/{original.episode.pk}/',
                                            {'end_date': None}, format='json')
    assert response.status_code == 200, response.data
    assert response.data['drug_exposure_ids'] == original.drug_exposure_ids
    original.episode.refresh_from_db()
    assert original.episode.episode_end_date is None
    assert exposure(original).drug_exposure_end_date == END


def test_metadata_only_patch_preserves_imported_repeat_administrations(setup):
    person, a, b = setup
    line = write(setup).episode
    EpisodeEvent.objects.filter(episode_id=line.pk).delete()
    facts = [DrugExposureFactory(person=person, drug_concept=concept,
                drug_source_value='same-source', drug_exposure_start_date=start,
                drug_exposure_end_date=date(2025, 3, 1))
             for concept, start in [(a, date(2025, 2, 1)), (b, date(2025, 2, 2))]]
    for fact in facts:
        EpisodeEvent.objects.create(episode_id=line.pk, event_id=fact.pk,
            episode_event_field_concept_id=CONCEPT_DRUG_EXPOSURE_FIELD)
    before = DrugExposure.objects.count()
    response = patient_client(person).patch(f'/api/v1/therapy-lines/{line.pk}/',
                                            {'intent': 'Curative'}, format='json')
    assert response.status_code == 200, response.data
    assert set(response.data['drug_exposure_ids']) == {fact.pk for fact in facts}
    assert DrugExposure.objects.count() == before
    for fact in facts:
        fact.refresh_from_db()
        assert fact.drug_exposure_start_date in (date(2025, 2, 1), date(2025, 2, 2))
        assert fact.drug_exposure_end_date == date(2025, 3, 1)


def test_regimen_clear_is_not_silently_ignored_while_unknown_workflow_is_pending(setup):
    original = write(setup, regimen_concept_id=setup[1].pk)
    with pytest.raises(ValueError, match='unknown-regimen assertion workflow'):
        write(setup, episode=original.episode, replace=True, regimen_concept_id=None)
    original.episode.refresh_from_db()
    assert original.episode.episode_source_concept_id == setup[1].pk


def test_event_key_includes_the_cdm_table_namespace(setup):
    original = write(setup)
    EpisodeEvent.objects.filter(episode_id=original.episode.pk).delete()
    wrong_field = ConceptFactory()
    wrong = EpisodeEvent.objects.create(episode_id=original.episode.pk,
        event_id=original.drug_exposure_ids[0], episode_event_field_concept=wrong_field)
    write(setup)
    assert EpisodeEvent.objects.filter(pk=wrong.pk).exists()
    assert EpisodeEvent.objects.filter(episode_id=original.episode.pk,
        event_id=original.drug_exposure_ids[0],
        episode_event_field_concept_id=CONCEPT_DRUG_EXPOSURE_FIELD).exists()


def test_direct_edit_cannot_renumber_a_line_or_its_observations(setup):
    original = write(setup)
    with pytest.raises(ValueError, match='line number'):
        write(setup, episode=original.episode, line_number=2, outcome='Partial Response')
    assert not Observation.objects.filter(person=setup[0]).exists()


@pytest.fixture
def serialized_links():
    from importlib import import_module
    from django.db import connection

    if connection.vendor != 'postgresql':
        pytest.skip('The PostgreSQL link serialization trigger is vendor-specific.')
    migration = import_module('omop_oncology.migrations.0008_therapylineexposure')
    with connection.cursor() as cursor:
        cursor.execute("SELECT 1 FROM pg_trigger WHERE tgname = 'lock_therapy_exposure_link' "
                       "AND tgrelid = 'episode_event'::regclass")
        existing = cursor.fetchone()
    if not existing:
        with connection.schema_editor() as editor:
            migration.install_link_lock(None, editor)
    try:
        yield
    finally:
        if not existing:
            with connection.schema_editor() as editor:
                migration.uninstall_link_lock(None, editor)


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize('fact_count', [1, 2])
def test_bulk_link_writer_forces_full_retry_before_owned_fact_correction(setup, serialized_links, monkeypatch, fact_count):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    from django.db import close_old_connections, connection, transaction
    from omop_core.services import episode_service

    payload = [{'concept_id': setup[1].pk, 'source_value': 'reported-drug'}]
    if fact_count == 2:
        payload.append({'concept_id': setup[2].pk, 'source_value': 'second-drug'})
    original = write(setup, drugs=payload)
    other = write(setup, line_number=2)
    link_inserted, retried, release, committed = Event(), Event(), Event(), Event()
    real_backoff = episode_service._sleep

    def backoff(seconds):
        assert not connection.in_atomic_block, 'The failed attempt must release all its locks first.'
        retried.set()
        release.set()
        assert committed.wait(10)
        real_backoff(seconds)

    monkeypatch.setattr(episode_service, '_sleep', backoff)

    def linker():
        close_old_connections()
        try:
            with transaction.atomic():
                # Covers bulk ETL as well as the generic API's INSERT: the lock
                # is in PostgreSQL, not an optional Python save hook.
                descending = sorted(original.drug_exposure_ids, reverse=True)
                EpisodeEvent.objects.bulk_create([EpisodeEvent(episode_id=other.episode.pk,
                    event_id=descending[0],
                    episode_event_field_concept_id=CONCEPT_DRUG_EXPOSURE_FIELD)])
                link_inserted.set()
                assert release.wait(10)
                # In the two-fact case, authoring has tried E1 then E2 while
                # this transaction held E2. Its full rollback must release E1.
                EpisodeEvent.objects.bulk_create([EpisodeEvent(episode_id=other.episode.pk,
                    event_id=event_id, episode_event_field_concept_id=CONCEPT_DRUG_EXPOSURE_FIELD)
                    for event_id in descending[1:]])
            committed.set()
        finally:
            connection.close()

    def corrector():
        close_old_connections()
        try:
            assert link_inserted.wait(10)
            with connection.cursor() as cursor:
                cursor.execute("SET lock_timeout = '10s'")
            return write(setup, episode=original.episode, replace=True,
                         drugs=[{**item, 'end_date': None} for item in payload], end_date=None)
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        link = pool.submit(linker)
        edit = pool.submit(corrector)
        try:
            assert retried.wait(5), 'The link transaction must exclude ownership-sensitive writes.'
        finally:
            release.set()
        link.result(timeout=10)
        result = edit.result(timeout=10)
    assert retried.is_set()
    assert result.drug_exposure_ids != original.drug_exposure_ids
    assert exposure(original).drug_concept_id == setup[1].pk
    assert exposure(original).drug_exposure_end_date == END
    assert not exposure(original).is_erroneous


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize('entity', ['drug', 'observation', 'person', 'episode'])
def test_fact_first_bulk_update_can_refresh_while_authoring_retries(setup, monkeypatch, entity):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    from django.db import close_old_connections, connection, transaction
    from omop_core.services import episode_service
    from omop_core.services.patient_record_service import refresh_patient_record
    from omop_core.models import Person

    original = write(setup, intent='Curative')
    targets = {
        'drug': (DrugExposure, original.drug_exposure_ids[0], 'stop_reason', 'Reviewed'),
        'observation': (Observation, Observation.objects.get(person=setup[0],
            observation_source_value='LOT-1-intent').pk, 'value_as_string', 'Curative'),
        'person': (Person, setup[0].pk, 'gender_source_value', 'F'),
        'episode': (Episode, original.episode.pk, 'episode_source_value', 'Reviewed regimen'),
    }
    model, pk, field, value = targets[entity]
    fact_held, retried, committed = Event(), Event(), Event()

    def backoff(seconds):
        assert not connection.in_atomic_block
        retried.set()
        assert committed.wait(10), 'Bulk refresh must not wait for a rolled-back authoring attempt.'

    monkeypatch.setattr(episode_service, '_sleep', backoff)

    def bulk_update():
        close_old_connections()
        try:
            with transaction.atomic():
                row = model.objects.select_for_update().get(pk=pk)
                fact_held.set()
                assert retried.wait(10)
                setattr(row, field, value)
                row.save(update_fields=[field])
                refresh_patient_record(Person.objects.get(pk=setup[0].pk))
            committed.set()
        finally:
            connection.close()

    def author():
        close_old_connections()
        try:
            assert fact_held.wait(10)
            return write(setup, episode=original.episode, replace=True, end_date=None,
                         drugs=[{'drug_exposure_id': original.drug_exposure_ids[0],
                                 'concept_id': setup[1].pk, 'end_date': None}])
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        bulk = pool.submit(bulk_update)
        edited = pool.submit(author)
        bulk.result(timeout=15)
        result = edited.result(timeout=15)
    assert retried.is_set()
    assert exposure(result).drug_exposure_end_date is None
    assert getattr(model.objects.get(pk=pk), field) == value


def test_exhausted_lock_retries_return_conflict_without_partial_writes(setup, monkeypatch):
    from django.db import OperationalError
    from django.db.models import QuerySet
    from omop_core.services import episode_service

    original = write(setup)
    attempts = []
    real_lock = QuerySet.select_for_update

    class Busy(Exception):
        sqlstate = '55P03'

    def fail_owned_fact_lock(queryset, *args, **kwargs):
        if queryset.model is DrugExposure and kwargs.get('nowait'):
            attempts.append(True)
            raise OperationalError('The clinical fact is busy.') from Busy()
        return real_lock(queryset, *args, **kwargs)

    monkeypatch.setattr(QuerySet, 'select_for_update', fail_owned_fact_lock)
    monkeypatch.setattr(episode_service, '_sleep', lambda _: None)
    response = patient_client(setup[0]).patch(f'/api/v1/therapy-lines/{original.episode.pk}/', {
        'drugs': [{'concept_id': setup[1].pk, 'source_value': 'reported-drug'}],
        'end_date': '2025-05-01',
    }, format='json')
    assert response.status_code == 409, response.data
    assert len(attempts) == 3
    original.episode.refresh_from_db()
    assert original.episode.episode_end_date == END
    assert exposure(original).drug_exposure_end_date == END
    assert EpisodeEvent.objects.filter(episode_id=original.episode.pk,
        event_id=original.drug_exposure_ids[0],
        episode_event_field_concept_id=CONCEPT_DRUG_EXPOSURE_FIELD).exists()


@pytest.mark.parametrize('field', ['drugs', 'regimen_concept_id'])
def test_no_match_concept_is_not_a_clinical_drug_or_regimen(setup, field):
    value = [{'concept_id': 0}] if field == 'drugs' else 0
    with pytest.raises(ValueError, match='Unknown'):
        write(setup, **{field: value})
    assert not DrugExposure.objects.filter(person=setup[0]).exists()
    assert not Episode.objects.filter(person=setup[0]).exists()


def test_full_form_echo_must_not_rewrite_unchanged_imported_administrations(setup):
    original = write(setup)
    imported = DrugExposureFactory(person=setup[0], drug_concept=setup[2],
        drug_source_value='imported-drug', drug_exposure_start_date=date(2025, 2, 1),
        drug_exposure_end_date=date(2025, 3, 1))
    EpisodeEvent.objects.create(episode_id=original.episode.pk, event_id=imported.pk,
        episode_event_field_concept_id=CONCEPT_DRUG_EXPOSURE_FIELD)
    response = patient_client(setup[0]).patch(f'/api/v1/therapy-lines/{original.episode.pk}/', {
        'drugs': [{'concept_id': setup[1].pk, 'source_value': 'reported-drug'},
                  {'concept_id': setup[2].pk, 'source_value': 'imported-drug'}],
        'start_date': START.isoformat(), 'end_date': END.isoformat(), 'intent': 'Curative',
    }, format='json')
    assert response.status_code == 200, response.data
    assert imported.pk in response.data['drug_exposure_ids'], (
        'A full-form echo must retain unchanged imports and their individual periods.')


def test_id_qualified_repeat_administrations_survive_a_full_form_echo(setup):
    line = write(setup).episode
    EpisodeEvent.objects.filter(episode_id=line.pk).delete()
    facts = [DrugExposureFactory(person=setup[0], drug_concept=setup[1],
                drug_source_value='repeat', drug_exposure_start_date=date(2025, 2, day),
                drug_exposure_end_date=None) for day in (1, 2)]
    for fact in facts:
        EpisodeEvent.objects.create(episode_id=line.pk, event_id=fact.pk,
            episode_event_field_concept_id=CONCEPT_DRUG_EXPOSURE_FIELD)
    payload = [{'drug_exposure_id': fact.pk, 'concept_id': fact.drug_concept_id,
                'source_value': fact.drug_source_value,
                'start_date': fact.drug_exposure_start_date.isoformat(), 'end_date': None}
               for fact in facts]
    response = patient_client(setup[0]).patch(f'/api/v1/therapy-lines/{line.pk}/',
        {'drugs': payload, 'intent': 'Curative', 'end_date': '2025-05-01'}, format='json')
    assert response.status_code == 200, response.data
    assert set(response.data['drug_exposure_ids']) == {fact.pk for fact in facts}
    for fact in facts:
        fact.refresh_from_db()
        assert fact.drug_exposure_end_date is None


@pytest.mark.parametrize('api', [False, True])
def test_future_administration_cannot_be_asserted_as_received(setup, api):
    drugs = [{'concept_id': setup[1].pk, 'start_date': date(2099, 1, 1), 'end_date': None}]
    if api:
        response = patient_client(setup[0]).post('/api/v1/therapy-lines/', {
            'person': setup[0].pk, 'line_number': 1, 'start_date': START.isoformat(),
            'drugs': [{**drugs[0], 'start_date': '2099-01-01'}],
        }, format='json')
        assert response.status_code == 400, response.data
    else:
        with pytest.raises(ValueError, match='future'):
            write(setup, drugs=drugs)
    assert not DrugExposure.objects.filter(person=setup[0]).exists()
    assert not Episode.objects.filter(person=setup[0]).exists()


def test_legacy_payload_cannot_silently_collapse_two_administration_dates(setup):
    with pytest.raises(ValueError, match='Duplicate'):
        write(setup, drugs=[{'concept_id': setup[1].pk, 'source_value': 'same', 'start_date': date(2025, 2, day)}
                            for day in (1, 2)])
    assert not DrugExposure.objects.filter(person=setup[0]).exists()


def test_ambiguous_legacy_drug_reference_requires_an_id(setup):
    line = write(setup).episode
    for day in (1, 2):
        fact = DrugExposureFactory(person=setup[0], drug_concept=setup[1],
            drug_source_value='repeat', drug_exposure_start_date=date(2025, 2, day))
        EpisodeEvent.objects.create(episode_id=line.pk, event_id=fact.pk,
            episode_event_field_concept_id=CONCEPT_DRUG_EXPOSURE_FIELD)
    before = set(EpisodeEvent.objects.filter(episode_id=line.pk).values_list('event_id', flat=True))
    with pytest.raises(ValueError, match='Ambiguous'):
        write(setup, episode=line, replace=True,
              drugs=[{'concept_id': setup[1].pk, 'source_value': 'repeat'}])
    assert set(EpisodeEvent.objects.filter(episode_id=line.pk).values_list('event_id', flat=True)) == before


def test_id_cannot_adopt_an_unlinked_exposure_even_from_this_person(setup):
    original = write(setup)
    unlinked = DrugExposureFactory(person=setup[0])
    with pytest.raises(ValueError, match='already linked'):
        write(setup, episode=original.episode, replace=True,
            drugs=[{'drug_exposure_id': unlinked.pk, 'concept_id': setup[1].pk}])
    assert exposure(original).pk in EpisodeEvent.objects.filter(episode_id=original.episode.pk).values_list('event_id', flat=True)


def test_explicit_import_correction_copies_and_preserves_omitted_individual_dates(setup):
    original = write(setup)
    fact = DrugExposureFactory(person=setup[0], drug_concept=setup[1], drug_source_value=None,
        drug_exposure_start_date=date(2025, 2, 1), drug_exposure_end_date=date(2025, 3, 1))
    EpisodeEvent.objects.create(episode_id=original.episode.pk, event_id=fact.pk,
        episode_event_field_concept_id=CONCEPT_DRUG_EXPOSURE_FIELD)
    corrected = write(setup, episode=original.episode, replace=True,
        drugs=[{'drug_exposure_id': fact.pk, 'concept_id': setup[2].pk, 'source_value': None}])
    copy = exposure(corrected)
    fact.refresh_from_db()
    assert copy.pk != fact.pk
    assert copy.drug_concept_id == setup[2].pk
    assert copy.drug_source_value is None
    assert copy.drug_exposure_start_date == date(2025, 2, 1)
    assert copy.drug_exposure_end_date == date(2025, 3, 1)
    assert fact.drug_concept_id == setup[1].pk
    assert not fact.is_erroneous


@pytest.mark.django_db(transaction=True)
def test_artemis_documents_with_reverse_exposure_order_serialize(setup, serialized_links):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    from django.db import close_old_connections, connection
    from omop_core.services.artemis_adapter import materialize_artemis_output
    from tests.test_artemis_adapter import _payload

    facts = [DrugExposureFactory(person=setup[0], drug_exposure_start_date=START,
             drug_exposure_end_date=END) for _ in range(2)]
    first_link, second_attempted = Event(), Event()

    def run(number):
        close_old_connections()
        try:
            def trace(execute, sql, params, many, context):
                if number == 2 and '"patient_record"' in sql and 'FOR UPDATE' in sql:
                    second_attempted.set()
                result = execute(sql, params, many, context)
                if number == 1 and 'INSERT INTO "episode_event"' in sql and not first_link.is_set():
                    first_link.set()
                    assert second_attempted.wait(10)
                return result

            if number == 2:
                assert first_link.wait(10)
            with connection.cursor() as cursor:
                cursor.execute("SET lock_timeout = '5s'")
            order = facts if number == 1 else list(reversed(facts))
            with connection.execute_wrapper(trace):
                return materialize_artemis_output(_payload(setup[0], *order, line_number=number))
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(run, [1, 2]))
    assert [result.created for result in outcomes] == [1, 1]
    for number in (1, 2):
        line = Episode.objects.get(person=setup[0], episode_number=number)
        assert set(EpisodeEvent.objects.filter(episode_id=line.pk).values_list('event_id', flat=True)) == {fact.pk for fact in facts}


@pytest.mark.django_db(transaction=True)
def test_artemis_outcome_edit_does_not_deadlock_fact_first_observation_refresh(setup):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    from django.db import close_old_connections, connection, transaction
    from omop_core.services.artemis_adapter import materialize_artemis_output
    from omop_core.services.patient_record_service import refresh_patient_record
    from tests.test_artemis_adapter import _payload

    original = write(setup, outcome='Partial Response')
    outcome = Observation.objects.get(person=setup[0], observation_source_value='LOT-1-outcome')
    fact = exposure(original)
    held, metadata_attempted = Event(), Event()

    def bulk():
        close_old_connections()
        try:
            with connection.cursor() as cursor:
                cursor.execute("SET deadlock_timeout = '100ms'")
            with transaction.atomic():
                Observation.objects.select_for_update().get(pk=outcome.pk)
                held.set()
                assert metadata_attempted.wait(10)
                # Same fact-first -> projection order as observations/bulk_update.
                refresh_patient_record(setup[0])
        finally:
            connection.close()

    def materializer():
        close_old_connections()
        try:
            assert held.wait(10)
            with connection.cursor() as cursor:
                cursor.execute("SET deadlock_timeout = '5s'")

            def trace(execute, sql, params, many, context):
                if '"observation"' in sql and ('NOWAIT' in sql or sql.startswith('UPDATE')):
                    metadata_attempted.set()
                return execute(sql, params, many, context)

            with connection.execute_wrapper(trace):
                return materialize_artemis_output(_payload(setup[0], fact, outcome='Complete Response'))
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        left = pool.submit(bulk)
        right = pool.submit(materializer)
        left.result(timeout=15)
        assert right.result(timeout=15).updated == 1
    outcome.refresh_from_db()
    assert outcome.value_as_string == 'Complete Response'


@pytest.mark.django_db(transaction=True)
def test_field_write_and_authoring_use_the_same_lock_order(setup):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    from django.db import close_old_connections, connection, transaction
    from omop_core.models import Person
    from omop_core.services.field_write_service import apply_field_writes

    projection_held, profile_attempted = Event(), Event()

    def author_query(execute, sql, params, many, context):
        result = execute(sql, params, many, context)
        if '"patient_record"' in sql and 'FOR UPDATE' in sql:
            projection_held.set()
            assert profile_attempted.wait(10)
        return result

    def profile_query(execute, sql, params, many, context):
        if '"patient_record"' in sql and 'FOR UPDATE' in sql:
            profile_attempted.set()
        return execute(sql, params, many, context)

    def run(author):
        close_old_connections()
        try:
            with connection.cursor() as cursor:
                cursor.execute("SET lock_timeout = '5s'")
            if author:
                with connection.execute_wrapper(author_query):
                    write(setup)
            else:
                assert projection_held.wait(10)
                person = Person.objects.get(pk=setup[0].pk)
                with transaction.atomic(), connection.execute_wrapper(profile_query):
                    result = apply_field_writes(person, {'gender': 'F'},
                        descriptor={'gender': {'kind': 'profile', 'writable': True}})
                    assert result.applied == ['gender']
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(run, [True, False]))
    assert DrugExposure.objects.filter(person=setup[0]).count() == 1


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize('writers', [2, 8])
def test_concurrent_identical_authoring_converges_to_one_line(setup, writers):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    from django.db import close_old_connections, connection

    barrier = Barrier(writers)

    def submit():
        close_old_connections()
        try:
            with connection.cursor() as cursor:
                cursor.execute("SET lock_timeout = '5s'")
            barrier.wait(timeout=10)
            result = write(setup)
            return result.episode.pk, result.drug_exposure_ids
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=writers) as pool:
        results = list(pool.map(lambda _: submit(), range(writers)))
    assert all(result == results[0] for result in results)
    assert Episode.objects.filter(person=setup[0], episode_concept_id=CONCEPT_TREATMENT_REGIMEN).count() == 1
    assert DrugExposure.objects.filter(person=setup[0]).count() == 1
