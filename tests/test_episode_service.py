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
