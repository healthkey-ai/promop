from datetime import date

import pytest

from omop_core.models import Observation
from omop_core.services.episode_service import upsert_therapy_line_episode
from omop_core.services.mappings import (
    CONCEPT_DRUG_EXPOSURE_FIELD,
    CONCEPT_EHR_TYPE,
    CONCEPT_TREATMENT_REGIMEN,
)
from omop_oncology.models import Episode
from tests.factories import ConceptFactory, DrugExposureFactory, PersonFactory, VocabularyFactory

pytestmark = pytest.mark.django_db


def _seed_episode_writer_concepts():
    vocab = VocabularyFactory(vocabulary_id='OMOP', vocabulary_name='OMOP')
    ConceptFactory(concept_id=0, concept_name='No matching concept', concept_code='0', vocabulary=vocab)
    ConceptFactory(
        concept_id=CONCEPT_TREATMENT_REGIMEN,
        concept_name='Treatment Regimen',
        concept_code=str(CONCEPT_TREATMENT_REGIMEN),
        vocabulary=vocab,
    )
    ConceptFactory(
        concept_id=CONCEPT_EHR_TYPE,
        concept_name='EHR',
        concept_code=str(CONCEPT_EHR_TYPE),
        vocabulary=vocab,
    )
    ConceptFactory(
        concept_id=CONCEPT_DRUG_EXPOSURE_FIELD,
        concept_name='drug_exposure_id',
        concept_code=str(CONCEPT_DRUG_EXPOSURE_FIELD),
        vocabulary=vocab,
    )
    snomed = VocabularyFactory(vocabulary_id='SNOMED', vocabulary_name='SNOMED')
    ConceptFactory(
        concept_id=182841002,
        concept_name='Partial Response',
        concept_code='182841002',
        vocabulary=snomed,
    )


def test_upsert_therapy_line_episode_updates_corrected_dates_and_outcome_date():
    _seed_episode_writer_concepts()
    person = PersonFactory()
    exposure = DrugExposureFactory(
        person=person,
        drug_exposure_start_date=date(2024, 1, 1),
        drug_exposure_end_date=date(2024, 4, 1),
    )

    upsert_therapy_line_episode(
        person,
        line_number=1,
        start_date=date(2024, 1, 1),
        end_date=date(2024, 4, 1),
        drug_exposure_ids=[exposure.drug_exposure_id],
        outcome='Partial Response',
        today=date(2024, 1, 1),
    )

    upsert_therapy_line_episode(
        person,
        line_number=1,
        start_date=date(2024, 2, 1),
        end_date=date(2024, 5, 1),
        drug_exposure_ids=[exposure.drug_exposure_id],
        outcome='Partial Response',
        today=date(2024, 2, 1),
    )

    episode = Episode.objects.get(person=person, episode_number=1)
    outcome = Observation.objects.get(person=person, observation_source_value='LOT-1-outcome')

    assert episode.episode_start_date == date(2024, 2, 1)
    assert episode.episode_end_date == date(2024, 5, 1)
    assert outcome.observation_date == date(2024, 5, 1)


def test_upsert_preserves_existing_end_date_when_none_and_flag_set():
    # `preserve_end_date_when_none=True` (the CB profile-write path): a None end_date means "not provided,
    # keep what's there", NOT "clear it". Regression guard — CB has no therapy end_date field, so without
    # this a partial edit (e.g. outcome-only) would NULL an imported episode's episode_end_date.
    _seed_episode_writer_concepts()
    person = PersonFactory()

    upsert_therapy_line_episode(
        person, line_number=1,
        start_date=date(2024, 1, 1), end_date=date(2024, 4, 1),
        today=date(2024, 1, 1),
    )
    # A later edit that carries no end date (e.g. an outcome-only CB PATCH) must not erase it.
    upsert_therapy_line_episode(
        person, line_number=1,
        start_date=date(2024, 1, 1), end_date=None,
        today=date(2024, 1, 1),
        preserve_end_date_when_none=True,
    )

    episode = Episode.objects.get(person=person, episode_number=1)
    assert episode.episode_end_date == date(2024, 4, 1)   # preserved, not NULLed


def test_upsert_clears_end_date_when_none_by_default():
    # Default (flag unset): a None end_date still clears — the original behaviour, which callers like
    # `lot_inference_service._persist_lots` rely on to reopen a line recomputed as ongoing.
    _seed_episode_writer_concepts()
    person = PersonFactory()

    upsert_therapy_line_episode(
        person, line_number=1,
        start_date=date(2024, 1, 1), end_date=date(2024, 4, 1),
        today=date(2024, 1, 1),
    )
    upsert_therapy_line_episode(
        person, line_number=1,
        start_date=date(2024, 1, 1), end_date=None,   # no preserve flag → clear
        today=date(2024, 1, 1),
    )

    episode = Episode.objects.get(person=person, episode_number=1)
    assert episode.episode_end_date is None   # cleared (ongoing line)
