from datetime import date

import pytest

from omop_oncology.models import Episode, EpisodeEvent
from omop_core.services.patient_record_service import _get_treatment_data
from tests.factories import ConceptFactory, DrugExposureFactory, PersonFactory, VocabularyFactory

pytestmark = pytest.mark.django_db


def test_episode_treatment_data_populates_start_dates_and_line_count():
    person = PersonFactory()
    hemonc_vocab = VocabularyFactory(vocabulary_id='HemOnc', vocabulary_name='HemOnc')
    episode_concept = ConceptFactory(concept_name='Treatment Regimen', vocabulary=hemonc_vocab)
    episode_object_concept = ConceptFactory(concept_name='Disease Episode', vocabulary=hemonc_vocab)
    episode_type_concept = ConceptFactory(concept_name='Derived Episode', vocabulary=hemonc_vocab)
    episode_event_field_concept = ConceptFactory(concept_name='Episode event field')
    first_regimen = ConceptFactory(
        concept_id=35804232,
        concept_name='Cyclophosphamide and Docetaxel (TC)',
        vocabulary=hemonc_vocab,
    )
    second_regimen = ConceptFactory(
        concept_id=35101507,
        concept_name='AC-T',
        vocabulary=hemonc_vocab,
    )

    first_drug = DrugExposureFactory(
        person=person,
        drug_concept=ConceptFactory(concept_name='docetaxel'),
        drug_exposure_start_date=date(2024, 1, 1),
        drug_exposure_end_date=date(2024, 1, 21),
    )
    second_drug = DrugExposureFactory(
        person=person,
        drug_concept=ConceptFactory(concept_name='doxorubicin'),
        drug_exposure_start_date=date(2024, 2, 1),
        drug_exposure_end_date=date(2024, 2, 21),
    )

    Episode.objects.create(
        episode_id=1,
        person=person,
        episode_concept=episode_concept,
        episode_start_date=date(2024, 1, 1),
        episode_end_date=date(2024, 1, 21),
        episode_number=1,
        episode_object_concept=episode_object_concept,
        episode_type_concept=episode_type_concept,
        episode_source_concept=first_regimen,
    )
    Episode.objects.create(
        episode_id=2,
        person=person,
        episode_concept=episode_concept,
        episode_start_date=date(2024, 2, 1),
        episode_end_date=date(2024, 2, 21),
        episode_number=2,
        episode_object_concept=episode_object_concept,
        episode_type_concept=episode_type_concept,
        episode_source_concept=second_regimen,
    )
    EpisodeEvent.objects.create(
        episode_id=1,
        event_id=first_drug.drug_exposure_id,
        episode_event_field_concept=episode_event_field_concept,
    )
    EpisodeEvent.objects.create(
        episode_id=2,
        event_id=second_drug.drug_exposure_id,
        episode_event_field_concept=episode_event_field_concept,
    )

    data = _get_treatment_data(person)

    assert data['first_line_therapy'] == 'Cyclophosphamide and Docetaxel (TC)'
    assert data['first_line_date'] == '2024-01-01'
    assert data['first_line_start_date'] == '2024-01-01'
    assert data['second_line_therapy'] == 'AC-T'
    assert data['second_line_date'] == '2024-02-01'
    assert data['second_line_start_date'] == '2024-02-01'
    assert data['therapy_lines_count'] == 2


def test_episode_treatment_data_counts_non_contiguous_episode_numbers_as_distinct_lines():
    person = PersonFactory()
    hemonc_vocab = VocabularyFactory(vocabulary_id='HemOnc', vocabulary_name='HemOnc')
    episode_concept = ConceptFactory(concept_name='Treatment Regimen', vocabulary=hemonc_vocab)
    episode_object_concept = ConceptFactory(concept_name='Disease Episode', vocabulary=hemonc_vocab)
    episode_type_concept = ConceptFactory(concept_name='Derived Episode', vocabulary=hemonc_vocab)
    episode_event_field_concept = ConceptFactory(concept_name='Episode event field')
    first_regimen = ConceptFactory(
        concept_id=35804232,
        concept_name='Cyclophosphamide and Docetaxel (TC)',
        vocabulary=hemonc_vocab,
    )
    later_regimen = ConceptFactory(
        concept_id=35101507,
        concept_name='AC-T',
        vocabulary=hemonc_vocab,
    )

    first_drug = DrugExposureFactory(
        person=person,
        drug_concept=ConceptFactory(concept_name='docetaxel'),
        drug_exposure_start_date=date(2024, 1, 1),
        drug_exposure_end_date=date(2024, 1, 21),
    )
    later_drug = DrugExposureFactory(
        person=person,
        drug_concept=ConceptFactory(concept_name='doxorubicin'),
        drug_exposure_start_date=date(2024, 3, 1),
        drug_exposure_end_date=date(2024, 3, 21),
    )

    Episode.objects.create(
        episode_id=11,
        person=person,
        episode_concept=episode_concept,
        episode_start_date=date(2024, 1, 1),
        episode_end_date=date(2024, 1, 21),
        episode_number=1,
        episode_object_concept=episode_object_concept,
        episode_type_concept=episode_type_concept,
        episode_source_concept=first_regimen,
    )
    Episode.objects.create(
        episode_id=13,
        person=person,
        episode_concept=episode_concept,
        episode_start_date=date(2024, 3, 1),
        episode_end_date=date(2024, 3, 21),
        episode_number=3,
        episode_object_concept=episode_object_concept,
        episode_type_concept=episode_type_concept,
        episode_source_concept=later_regimen,
    )
    EpisodeEvent.objects.create(
        episode_id=11,
        event_id=first_drug.drug_exposure_id,
        episode_event_field_concept=episode_event_field_concept,
    )
    EpisodeEvent.objects.create(
        episode_id=13,
        event_id=later_drug.drug_exposure_id,
        episode_event_field_concept=episode_event_field_concept,
    )

    data = _get_treatment_data(person)

    assert data['therapy_lines_count'] == 2
    assert data['first_line_therapy'] == 'Cyclophosphamide and Docetaxel (TC)'
    assert data['later_therapies'][0]['therapy'] == 'AC-T'
