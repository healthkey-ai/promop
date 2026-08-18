from datetime import date

import pytest

from omop_oncology.models import Episode, EpisodeEvent
from omop_core.services.patient_record_service import _get_treatment_data
from tests.factories import (
    ConceptFactory, DrugExposureFactory, MeasurementFactory, ObservationFactory,
    PersonFactory, VocabularyFactory,
)

pytestmark = pytest.mark.django_db


def _therapy_episode(person, episode_id, line, start, end):
    vocab = VocabularyFactory(vocabulary_id='HemOnc', vocabulary_name='HemOnc')
    regimen = ConceptFactory(
        concept_id=35804232 + episode_id,
        concept_name=f'Regimen {line}', vocabulary=vocab,
    )
    exposure = DrugExposureFactory(
        person=person, drug_exposure_start_date=start, drug_exposure_end_date=end,
    )
    episode = Episode.objects.create(
        episode_id=episode_id, person=person,
        episode_concept=ConceptFactory(concept_name='Treatment Regimen', vocabulary=vocab),
        episode_start_date=start, episode_end_date=end, episode_number=line,
        episode_object_concept=ConceptFactory(concept_name='Disease Episode', vocabulary=vocab),
        episode_type_concept=ConceptFactory(concept_name='Derived Episode', vocabulary=vocab),
        episode_source_concept=regimen,
    )
    EpisodeEvent.objects.create(
        episode_id=episode.episode_id, event_id=exposure.drug_exposure_id,
        episode_event_field_concept=ConceptFactory(concept_name='Episode event field'),
    )
    return episode


def test_treatment_assertions_derive_from_dated_omop_facts():
    person = PersonFactory()
    _therapy_episode(person, 901, 1, date(2024, 1, 1), date(2024, 1, 21))
    _therapy_episode(person, 902, 2, date(2024, 2, 1), date(2024, 2, 21))

    # Explicit line fact wins for line 1; standard FHIR LOINC Measurement is
    # safely associated with line 2 by its unambiguous event date.
    ObservationFactory(
        person=person, observation_date=date(2024, 1, 1),
        observation_source_value='LOT-1-intent', value_as_string='Adjuvant',
    )
    ObservationFactory(
        person=person, observation_date=date(2024, 1, 21),
        observation_source_value='LOT-1-discontinuation', value_as_string='Completion',
    )
    MeasurementFactory(
        person=person, measurement_date=date(2024, 2, 2),
        measurement_source_value='42804-5', value_as_string='Salvage',
    )
    MeasurementFactory(
        person=person, measurement_date=date(2024, 2, 21),
        measurement_source_value='91379-3', value_as_string='Progression',
    )

    data = _get_treatment_data(person)

    assert data['first_line_intent'] == 'Adjuvant'
    assert data['first_line_discontinuation_reason'] == 'Completion'
    assert data['second_line_intent'] == 'Salvage'
    assert data['second_line_discontinuation_reason'] == 'Progression'
    assert data['therapy_intent'] == 'Salvage'
    assert data['reason_for_discontinuation'] == 'Progression'
    assert data['washout_period_duration'] == '11 days'
    assert data['line_of_therapy'] == '2'


def test_treatment_assertion_does_not_cross_overlapping_episode_lines():
    person = PersonFactory()
    _therapy_episode(person, 911, 1, date(2024, 1, 1), date(2024, 1, 31))
    _therapy_episode(person, 912, 2, date(2024, 1, 15), date(2024, 2, 15))
    MeasurementFactory(
        person=person, measurement_date=date(2024, 1, 20),
        measurement_source_value='42804-5', value_as_string='Ambiguous',
    )

    data = _get_treatment_data(person)

    assert 'first_line_intent' not in data
    assert 'second_line_intent' not in data


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


def test_episode_treatment_data_derives_per_line_outcomes_from_lot_observations():
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

    first_drug = DrugExposureFactory(
        person=person,
        drug_concept=ConceptFactory(concept_name='docetaxel'),
        drug_exposure_start_date=date(2024, 1, 1),
        drug_exposure_end_date=date(2024, 1, 21),
    )
    Episode.objects.create(
        episode_id=21,
        person=person,
        episode_concept=episode_concept,
        episode_start_date=date(2024, 1, 1),
        episode_end_date=date(2024, 1, 21),
        episode_number=1,
        episode_object_concept=episode_object_concept,
        episode_type_concept=episode_type_concept,
        episode_source_concept=first_regimen,
    )
    EpisodeEvent.objects.create(
        episode_id=21,
        event_id=first_drug.drug_exposure_id,
        episode_event_field_concept=episode_event_field_concept,
    )

    from tests.factories import ObservationFactory

    ObservationFactory(
        person=person,
        value_as_string='Partial Response',
        observation_source_value='LOT-1-outcome',
        observation_date=date(2024, 1, 21),
    )
    ObservationFactory(
        person=person,
        value_as_string='Progressive Disease',
        observation_source_value='LOT-2-outcome',
        observation_date=date(2024, 6, 1),
    )
    ObservationFactory(
        person=person,
        value_as_string='Very Good Partial Response',
        observation_source_value='LOT-3-outcome',
        observation_date=date(2024, 9, 1),
    )

    data = _get_treatment_data(person)

    assert data['first_line_outcome'] == 'Partial Response'
    assert data['second_line_outcome'] == 'Progressive Disease'
    assert data['later_outcome'] == 'Very Good Partial Response'


def test_episode_treatment_data_ignores_malformed_lot_outcome_source_values():
    person = PersonFactory()
    hemonc_vocab = VocabularyFactory(vocabulary_id='HemOnc', vocabulary_name='HemOnc')
    episode_concept = ConceptFactory(concept_name='Treatment Regimen', vocabulary=hemonc_vocab)
    episode_object_concept = ConceptFactory(concept_name='Disease Episode', vocabulary=hemonc_vocab)
    episode_type_concept = ConceptFactory(concept_name='Derived Episode', vocabulary=hemonc_vocab)

    Episode.objects.create(
        episode_id=31,
        person=person,
        episode_concept=episode_concept,
        episode_start_date=date(2024, 1, 1),
        episode_number=1,
        episode_object_concept=episode_object_concept,
        episode_type_concept=episode_type_concept,
    )

    from tests.factories import ObservationFactory

    ObservationFactory(
        person=person,
        value_as_string='Partial Response',
        observation_source_value='LOT-x-outcome',
        observation_date=date(2024, 1, 21),
    )

    data = _get_treatment_data(person)

    assert 'first_line_outcome' not in data
