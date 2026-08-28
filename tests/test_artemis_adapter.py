"""Contract tests for the external ARTEMIS result adapter.

These fixtures stand in for ARTEMIS R output: CI never needs an R runtime or a
network-installed ARTEMIS package to verify our ingestion boundary.
"""
from datetime import date
import json

import pytest
from django.core.exceptions import ValidationError
from django.core.management import call_command

from omop_core.services.artemis_adapter import materialize_artemis_output, validate_artemis_output
from omop_core.services.mappings import (
    CONCEPT_DRUG_EXPOSURE_FIELD, CONCEPT_EHR_TYPE, CONCEPT_TREATMENT_REGIMEN,
)
from omop_oncology.models import Episode, EpisodeEvent
from tests.factories import ConceptFactory, DrugExposureFactory, PersonFactory, VocabularyFactory


pytestmark = pytest.mark.django_db


def _seed_writer_concepts():
    vocab = VocabularyFactory(vocabulary_id="OMOP", vocabulary_name="OMOP")
    ConceptFactory(concept_id=0, concept_name="No matching concept", concept_code="0", vocabulary=vocab)
    ConceptFactory(concept_id=CONCEPT_TREATMENT_REGIMEN, concept_name="Treatment Regimen",
                   concept_code=str(CONCEPT_TREATMENT_REGIMEN), vocabulary=vocab)
    ConceptFactory(concept_id=CONCEPT_EHR_TYPE, concept_name="EHR",
                   concept_code=str(CONCEPT_EHR_TYPE), vocabulary=vocab)
    ConceptFactory(concept_id=CONCEPT_DRUG_EXPOSURE_FIELD, concept_name="drug_exposure_id",
                   concept_code=str(CONCEPT_DRUG_EXPOSURE_FIELD), vocabulary=vocab)


def _payload(person, *exposures, **overrides):
    row = {
        "person_id": person.person_id,
        "line_number": 1,
        "start_date": "2024-01-01",
        "end_date": "2024-03-01",
        "drug_exposure_ids": [exposure.drug_exposure_id for exposure in exposures],
    }
    row.update(overrides)
    return {"schema_version": "1", "episodes": [row]}


def test_materializes_fixture_output_via_episode_service_and_reruns_idempotently():
    _seed_writer_concepts()
    person = PersonFactory()
    first = DrugExposureFactory(person=person, drug_exposure_start_date=date(2024, 1, 1))
    second = DrugExposureFactory(person=person, drug_exposure_start_date=date(2024, 1, 2))
    payload = _payload(person, first, second)

    first_result = materialize_artemis_output(payload)
    second_result = materialize_artemis_output(payload)

    episode = Episode.objects.get(person=person, episode_number=1)
    assert first_result.created == 1
    assert second_result.updated == 1
    assert episode.episode_source_value == "ARTEMIS-LOT-1"
    assert episode.episode_start_date == date(2024, 1, 1)
    assert EpisodeEvent.objects.filter(episode_id=episode.episode_id).count() == 2


def test_manual_episode_is_preserved():
    _seed_writer_concepts()
    person = PersonFactory()
    exposure = DrugExposureFactory(person=person)
    regimen = ConceptFactory(concept_id=8_888_101, concept_code="manual-regimen")
    ehr = ConceptFactory(concept_id=8_888_102, concept_code="manual-ehr")
    existing = Episode.objects.create(
        episode_id=8_888_100, person=person, episode_concept=regimen,
        episode_object_concept=regimen, episode_type_concept=ehr,
        episode_start_date=date(2020, 1, 1), episode_number=1,
        episode_source_value="Manual",
    )

    result = materialize_artemis_output(_payload(person, exposure))

    existing.refresh_from_db()
    assert result.skipped_manual == 1
    assert existing.episode_source_value == "Manual"
    assert EpisodeEvent.objects.filter(episode_id=existing.episode_id).count() == 0


def test_invalid_later_row_fails_before_any_rows_are_written():
    _seed_writer_concepts()
    person = PersonFactory()
    exposure = DrugExposureFactory(person=person)
    payload = _payload(person, exposure)
    payload["episodes"].append({
        "person_id": person.person_id, "line_number": 2, "start_date": "not-a-date",
        "drug_exposure_ids": [exposure.drug_exposure_id],
    })

    with pytest.raises(ValidationError, match="ISO-8601"):
        materialize_artemis_output(payload)

    assert not Episode.objects.filter(person=person).exists()


def test_rejects_exposure_owned_by_another_person():
    person = PersonFactory()
    foreign = DrugExposureFactory(person=PersonFactory())

    with pytest.raises(ValidationError, match="another person"):
        validate_artemis_output(_payload(person, foreign))


def test_command_dry_run_validates_fixture_without_writing(tmp_path, capsys):
    _seed_writer_concepts()
    person = PersonFactory()
    exposure = DrugExposureFactory(person=person)
    fixture = tmp_path / "artemis.json"
    fixture.write_text(json.dumps(_payload(person, exposure)), encoding="utf-8")

    call_command("materialize_artemis_episodes", input=str(fixture), dry_run=True)

    assert "Validated 1 ARTEMIS episode" in capsys.readouterr().out
    assert not Episode.objects.filter(person=person).exists()
