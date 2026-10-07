"""Lines of therapy per cancer (#1739): a second cancer has its own line 1."""
from datetime import date

import pytest
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient

from omop_core.models import Observation, PatientRecord
from omop_core.services.disease_episodes import (
    disease_slug,
    find_disease_episode,
    lines_by_disease,
    primary_lines,
)
from omop_core.services.episode_service import author_therapy_line, upsert_therapy_line_episode
from omop_core.services.mappings import (
    CONCEPT_DISEASE_FIRST_OCCURRENCE,
    CONCEPT_DRUG_EXPOSURE_FIELD,
    CONCEPT_EHR_TYPE,
    CONCEPT_TREATMENT_REGIMEN,
)
from omop_core.services.patient_record_service import refresh_patient_record
from omop_core.signals import suppress_patient_record_refresh
from omop_oncology.models import Episode
from tests.factories import (
    ConceptFactory,
    ConditionOccurrenceFactory,
    PatientRecordFactory,
    VocabularyFactory,
)

pytestmark = pytest.mark.django_db

MYELOMA, PROSTATE = 'Multiple myeloma', 'Prostate cancer'


def _concepts(disease_episode=True):
    omop = VocabularyFactory(vocabulary_id='OMOP', vocabulary_name='OMOP')
    ConceptFactory(concept_id=0, concept_name='No matching concept', concept_code='0', vocabulary=omop)
    ids = [(CONCEPT_TREATMENT_REGIMEN, 'Treatment Regimen'), (CONCEPT_EHR_TYPE, 'EHR'),
           (CONCEPT_DRUG_EXPOSURE_FIELD, 'drug_exposure_id')]
    if disease_episode:
        ids.append((CONCEPT_DISEASE_FIRST_OCCURRENCE, 'Disease First Occurrence'))
    for cid, name in ids:
        ConceptFactory(concept_id=cid, concept_name=name, concept_code=str(cid), vocabulary=omop)
    rx = VocabularyFactory(vocabulary_id='RxNorm', vocabulary_name='RxNorm')
    return {
        name: ConceptFactory(concept_id=cid, concept_name=name, concept_code=code, vocabulary=rx)
        for cid, name, code in ((1301025, 'lenalidomide', '6360'), (1518254, 'dexamethasone', '3264'),
                                (1344381, 'bicalutamide', '83008'))
    }


def _patient():
    """Myeloma (the primary: diagnosed most recently) and an earlier prostate cancer."""
    record = PatientRecordFactory(disease=MYELOMA, disease_slug=disease_slug(MYELOMA))
    icd = VocabularyFactory(vocabulary_id='ICD10CM', vocabulary_name='ICD10CM')
    with suppress_patient_record_refresh():
        ConditionOccurrenceFactory(
            person=record.person, condition_start_date=date(2019, 5, 1),
            condition_concept=ConceptFactory(concept_name=PROSTATE, concept_code='C61', vocabulary=icd),
        )
        ConditionOccurrenceFactory(
            person=record.person, condition_start_date=date(2024, 3, 12),
            condition_concept=ConceptFactory(concept_name=MYELOMA, concept_code='C90.00', vocabulary=icd),
        )
    return record.person


def _drug(drugs, name):
    return {'concept_id': drugs[name].concept_id, 'source_value': name}


def test_each_cancer_has_its_own_line_1_with_its_own_outcome():
    drugs = _concepts()
    person = _patient()

    myeloma = author_therapy_line(person, line_number=1, drugs=[_drug(drugs, 'lenalidomide')],
                                  start_date=date(2024, 4, 1), outcome='Partial Response')
    prostate = author_therapy_line(person, line_number=1, drugs=[_drug(drugs, 'bicalutamide')],
                                   start_date=date(2019, 6, 1), outcome='Complete Response',
                                   disease=PROSTATE)

    assert myeloma.episode.episode_id != prostate.episode.episode_id
    # Not filed under a cancer: the primary cancer's line, so unparented.
    assert Episode.objects.get(pk=myeloma.episode.pk).episode_parent_id is None
    assert Episode.objects.get(pk=prostate.episode.pk).episode_parent_id == \
        find_disease_episode(person, disease_slug(PROSTATE)).episode_id
    outcomes = dict(Observation.objects.filter(person=person, observation_source_value='LOT-1-outcome')
                    .values_list('observation_event_id', 'value_as_string'))
    assert outcomes == {myeloma.episode.episode_id: 'Partial Response',
                        prostate.episode.episode_id: 'Complete Response'}

    # Re-sending either line converges on it and leaves the other alone.
    again = author_therapy_line(person, line_number=1, drugs=[_drug(drugs, 'bicalutamide')],
                                start_date=date(2019, 6, 1), outcome='Stable Disease', disease=PROSTATE)
    assert again.episode.episode_id == prostate.episode.episode_id and not again.created
    assert Observation.objects.get(observation_event_id=myeloma.episode.episode_id).value_as_string == 'Partial Response'

    groups = {g['slug']: g for g in lines_by_disease(person)}
    assert groups[disease_slug(MYELOMA)]['primary'] and not groups[disease_slug(PROSTATE)]['primary']
    assert [line['outcome'] for line in groups[disease_slug(PROSTATE)]['lines']] == ['Stable Disease']
    # A line with no named regimen is named after its drugs, not its LOT-n label.
    assert [line['regimen'] for line in groups[disease_slug(PROSTATE)]['lines']] == ['bicalutamide']


def test_lines_written_before_the_change_belong_to_the_primary_cancer():
    _concepts()
    person = _patient()
    legacy = Episode.objects.create(
        episode_id=900_001, person=person, episode_concept_id=CONCEPT_TREATMENT_REGIMEN,
        episode_object_concept_id=0, episode_type_concept_id=CONCEPT_EHR_TYPE,
        episode_start_date=date(2024, 4, 1), episode_number=1, episode_source_value='LOT-1',
    )
    Observation.objects.create(
        observation_id=900_101, person=person, observation_concept_id=0, observation_date=date(2024, 9, 1),
        observation_type_concept_id=CONCEPT_EHR_TYPE, value_as_string='Partial Response',
        observation_source_value='LOT-1-outcome',
    )

    # Another cancer's line 1 is new; it does not take the old line.
    prostate = upsert_therapy_line_episode(person, line_number=1, start_date=date(2019, 6, 1),
                                           outcome='Complete Response', disease=PROSTATE)
    assert prostate.created and prostate.episode.episode_id != legacy.episode_id
    legacy.refresh_from_db()
    assert legacy.episode_parent_id is None
    assert Observation.objects.get(observation_id=900_101).observation_event_id is None

    # The primary cancer's line 1 is the old one, which takes its outcome.
    primary = upsert_therapy_line_episode(person, line_number=1, outcome='Stable Disease')
    assert primary.episode.episode_id == legacy.episode_id and not primary.created
    legacy.refresh_from_db()
    assert legacy.episode_parent_id is None
    # Naming the primary cancer files the line under it.
    named = upsert_therapy_line_episode(person, line_number=1, disease=MYELOMA)
    assert named.episode.episode_id == legacy.episode_id
    legacy.refresh_from_db()
    assert legacy.episode_parent_id == find_disease_episode(person, disease_slug(MYELOMA)).episode_id
    adopted = Observation.objects.get(observation_id=900_101)
    assert adopted.observation_event_id == legacy.episode_id and adopted.value_as_string == 'Stable Disease'


def test_the_record_s_flat_therapy_fields_stay_the_primary_cancer_s():
    drugs = _concepts()
    person = _patient()
    author_therapy_line(person, line_number=1, drugs=[_drug(drugs, 'lenalidomide'), _drug(drugs, 'dexamethasone')],
                        start_date=date(2024, 4, 1), end_date=date(2024, 10, 1), outcome='Partial Response')
    author_therapy_line(person, line_number=1, drugs=[_drug(drugs, 'bicalutamide')],
                        start_date=date(2019, 6, 1), end_date=date(2019, 12, 1),
                        outcome='Complete Response', disease=PROSTATE)
    # Ended: a cancer still on a line would be the primary (#1738).
    author_therapy_line(person, line_number=2, drugs=[_drug(drugs, 'bicalutamide')],
                        start_date=date(2020, 1, 1), end_date=date(2020, 6, 1), disease=PROSTATE)

    record = refresh_patient_record(person)

    assert record.disease_slug == disease_slug(MYELOMA)
    assert record.first_line_outcome == 'Partial Response'
    assert record.therapy_lines_count == 1
    assert not record.second_line_therapy
    assert 'bicalutamide' not in (record.first_line_therapy or '').lower()
    assert list(primary_lines(person).values_list('episode_number', flat=True)) == [1]


def test_editing_a_line_by_id_never_touches_another_cancers_line_with_the_same_number():
    drugs = _concepts()
    person = _patient()
    myeloma = author_therapy_line(person, line_number=1, drugs=[_drug(drugs, 'lenalidomide')],
                                  start_date=date(2024, 4, 1))
    prostate = author_therapy_line(person, line_number=1, drugs=[_drug(drugs, 'bicalutamide')],
                                   start_date=date(2019, 6, 1), disease=PROSTATE)
    staff = get_user_model().objects.create_user(email='lot-editor@example.test', password='pw', is_staff=True)
    client = APIClient()
    client.force_authenticate(staff)

    edited = client.patch(f'/api/v1/therapy-lines/{prostate.episode.episode_id}/', {
        'drugs': [_drug(drugs, 'bicalutamide')], 'end_date': '2019-12-01', 'outcome': 'Complete Response',
    }, format='json')
    assert edited.status_code == 200, edited.data
    assert edited.data['episode_id'] == prostate.episode.episode_id
    assert Episode.objects.get(pk=prostate.episode.pk).episode_end_date == date(2019, 12, 1)
    assert Episode.objects.get(pk=myeloma.episode.pk).episode_end_date is None

    created = client.post('/api/v1/therapy-lines/', {
        'person': person.person_id, 'line_number': 2, 'start_date': '2020-01-01',
        'drugs': [_drug(drugs, 'bicalutamide')], 'disease': PROSTATE,
    }, format='json')
    assert created.status_code == 201, created.data
    assert Episode.objects.get(pk=created.data['episode_id']).episode_parent_id == \
        find_disease_episode(person, disease_slug(PROSTATE)).episode_id


def test_old_lines_are_the_primary_cancers_without_a_backfill():
    _concepts()
    person = _patient()
    for n in (1, 2):
        Episode.objects.create(
            episode_id=900_010 + n, person=person, episode_concept_id=CONCEPT_TREATMENT_REGIMEN,
            episode_object_concept_id=0, episode_type_concept_id=CONCEPT_EHR_TYPE,
            episode_start_date=date(2024, n, 1), episode_number=n,
        )
    Observation.objects.create(
        observation_id=900_201, person=person, observation_concept_id=0, observation_date=date(2024, 9, 1),
        observation_type_concept_id=CONCEPT_EHR_TYPE, value_as_string='Curative',
        observation_source_value='LOT-2-intent',
    )
    upsert_therapy_line_episode(person, line_number=1, start_date=date(2019, 6, 1), disease=PROSTATE)

    assert set(primary_lines(person).values_list('episode_number', flat=True)) == {1, 2}
    groups = {g['slug']: g for g in lines_by_disease(person)}
    primary = groups[disease_slug(MYELOMA)]
    assert primary['primary'] and [line['line'] for line in primary['lines']] == [1, 2]
    assert primary['lines'][1]['intent'] == 'Curative'
    assert [line['line'] for line in groups[disease_slug(PROSTATE)]['lines']] == [1]


def test_a_disease_episode_alone_does_not_stop_line_inference():
    from omop_core.services.lot_inference_service import infer_lot_for_person

    _concepts()
    person = _patient()
    upsert_therapy_line_episode(person, line_number=1, start_date=date(2019, 6, 1), disease=PROSTATE)
    Episode.objects.filter(person=person, episode_concept_id=CONCEPT_TREATMENT_REGIMEN).delete()
    assert Episode.objects.filter(person=person).count() == 1  # the Disease Episode
    # Inference runs (it found no lines); with lines present it would skip.
    assert infer_lot_for_person(person, dry_run=True) == []


def test_without_the_disease_episode_concept_lines_stay_unparented_as_before():
    _concepts(disease_episode=False)
    person = _patient()
    first = upsert_therapy_line_episode(person, line_number=1, start_date=date(2024, 4, 1))
    assert first.episode is not None and first.episode.episode_parent_id is None
    assert upsert_therapy_line_episode(person, line_number=1, start_date=date(2024, 4, 1)).episode.episode_id == first.episode.episode_id
    # Naming the primary cancer is fine without the vocabulary: the line stays unparented.
    assert upsert_therapy_line_episode(person, line_number=1, disease=MYELOMA).episode.episode_parent_id is None
    assert PatientRecord.objects.filter(person=person).exists()


# ---------------------------------------------------------------- review fixes (pass 1)

def test_a_line_for_another_cancer_never_lands_on_the_primary_cancers_line():
    """Without the Disease Episode concept a second cancer's line cannot be told apart: refuse it."""
    _concepts(disease_episode=False)
    person = _patient()
    primary = upsert_therapy_line_episode(person, line_number=1, start_date=date(2024, 4, 1), outcome='Partial Response')

    with pytest.raises(ValueError):
        upsert_therapy_line_episode(person, line_number=1, start_date=date(2019, 6, 1), disease=PROSTATE)
    with pytest.raises(ValueError):
        upsert_therapy_line_episode(person, line_number=1, start_date=date(2019, 6, 1), disease='---')

    assert Episode.objects.get(pk=primary.episode.pk).episode_start_date == date(2024, 4, 1)


def test_a_line_for_a_cancer_the_record_does_not_have_is_refused():
    _concepts()
    person = _patient()
    with pytest.raises(ValueError):
        upsert_therapy_line_episode(person, line_number=1, start_date=date(2024, 4, 1), disease='Melanoma')
    assert not Episode.objects.filter(person=person).exists()


def test_lines_follow_the_primary_cancer_when_it_changes():
    """Lines nobody filed under a cancer are the primary cancer's, whichever that is now."""
    drugs = _concepts()
    person = _patient()
    author_therapy_line(person, line_number=1, drugs=[_drug(drugs, 'lenalidomide')],
                        start_date=date(2024, 4, 1), end_date=date(2024, 10, 1), outcome='Partial Response')
    assert refresh_patient_record(person).therapy_lines_count == 1

    icd = VocabularyFactory(vocabulary_id='ICD10CM', vocabulary_name='ICD10CM')
    with suppress_patient_record_refresh():
        ConditionOccurrenceFactory(  # a later oncologic row with another name becomes the primary
            person=person, condition_start_date=date(2025, 2, 1),
            condition_concept=ConceptFactory(concept_name='Plasma cell leukemia', concept_code='C90.10', vocabulary=icd),
        )
    record = refresh_patient_record(person)
    assert record.disease_slug != disease_slug(MYELOMA)
    assert record.therapy_lines_count == 1 and record.first_line_outcome == 'Partial Response'


def test_a_named_cancers_lines_go_to_the_primary_when_that_diagnosis_is_gone():
    """A Disease Episode whose cancer is no longer on the record (removed, renamed) is the primary's."""
    drugs = _concepts()
    person = _patient()
    line = author_therapy_line(person, line_number=1, drugs=[_drug(drugs, 'lenalidomide')],
                               start_date=date(2024, 4, 1), disease=MYELOMA)
    from omop_core.models import ConditionOccurrence
    with suppress_patient_record_refresh():
        ConditionOccurrence.objects.filter(person=person, condition_concept__concept_name=MYELOMA).delete()
    assert refresh_patient_record(person).disease_slug == disease_slug(PROSTATE)
    assert line.episode.episode_id in set(primary_lines(person).values_list('episode_id', flat=True))


def test_a_cancer_gets_one_disease_episode_even_when_two_writers_race(monkeypatch):
    from django.db import IntegrityError, transaction

    from omop_core.services import disease_episodes

    _concepts()
    person = _patient()
    slug = disease_slug(PROSTATE)
    first = disease_episodes.disease_episode(person, slug)
    # A second Disease Episode for the same cancer is refused by the database.
    with pytest.raises(IntegrityError), transaction.atomic():
        Episode.objects.create(
            episode_id=900_999, person=person, episode_concept_id=CONCEPT_DISEASE_FIRST_OCCURRENCE,
            episode_object_concept_id=0, episode_type_concept_id=CONCEPT_EHR_TYPE,
            episode_start_date=date(2024, 1, 1), episode_source_value=f'disease:{slug}',
        )
    # A writer that looked before the other committed gets the existing row back.
    real = disease_episodes.find_disease_episode
    calls = []

    def stale_then_real(*args):
        calls.append(1)
        return None if len(calls) == 1 else real(*args)

    monkeypatch.setattr(disease_episodes, 'find_disease_episode', stale_then_real)
    assert disease_episodes.disease_episode(person, slug).episode_id == first.episode_id


def test_a_lines_observations_name_the_episode_field_they_point_at():
    _concepts()
    field = ConceptFactory(concept_id=1_147_000, concept_name='episode.episode_id', concept_code='episode.episode_id',
                           vocabulary=VocabularyFactory(vocabulary_id='CDM', vocabulary_name='CDM'))
    person = _patient()
    line = upsert_therapy_line_episode(person, line_number=1, start_date=date(2024, 4, 1),
                                       outcome='Partial Response', intent='Curative')
    rows = Observation.objects.filter(person=person, observation_event_id=line.episode.episode_id)
    assert rows.count() == 2
    assert set(rows.values_list('obs_event_field_concept_id', flat=True)) == {field.concept_id}
