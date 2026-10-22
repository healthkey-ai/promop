"""A line that happened is a line, even when its composition is unknown (#1924).

The projection used to require a backing `EpisodeEvent`, which turned "we know
this line happened but not which drugs" into "no prior therapy" — the opposite
assertion, and one that changes trial eligibility. Assertion is now recorded by
the canonical writer, so a dangling header stays ignored.
"""
from datetime import date

import pytest

from omop_core.models import Concept, DrugExposure, PatientRecord
from omop_core.services.episode_service import (
    author_therapy_line, retract_therapy_line, upsert_therapy_line_episode,
)
from omop_core.services.patient_record_service import refresh_patient_record
from omop_oncology.models import Episode, TherapyLineAssertion
from tests.factories import ConceptFactory, VocabularyFactory
from tests.test_episode_service import _seed_episode_writer_concepts

pytestmark = pytest.mark.django_db
START = date(2025, 1, 15)
REGIMEN_ID = 35_806_260


def _regimen(name='Bortezomib & Dexamethasone'):
    from omop_core.models import ConceptClass
    vocab = VocabularyFactory(vocabulary_id='HemOnc', vocabulary_name='HemOnc')
    concept_class, _ = ConceptClass.objects.get_or_create(
        concept_class_id='Regimen',
        defaults={'concept_class_name': 'Regimen', 'concept_class_concept_id': 0},
    )
    return ConceptFactory(
        concept_id=REGIMEN_ID, concept_name=name, concept_code=str(REGIMEN_ID),
        vocabulary=vocab, concept_class=concept_class, standard_concept='S',
    )


@pytest.fixture
def person(setup_concepts=None):
    from tests.factories import PersonFactory, PatientRecordFactory
    _seed_episode_writer_concepts()
    record = PatientRecordFactory()
    return record.person


def _line(person, **kwargs):
    defaults = dict(line_number=1, start_date=START, source_value='VRd')
    defaults.update(kwargs)
    return upsert_therapy_line_episode(person, **defaults)


def _record(person):
    return refresh_patient_record(person)


def test_named_line_without_drugs_is_a_line_whose_composition_is_unknown(person):
    result = _line(person)
    record = _record(person)

    assert result.episode is not None
    assert record.therapy_lines_count == 1
    assert record.first_line_therapy == 'VRd'          # what the caller named
    assert record.first_line_therapy_id is None        # no invented concept
    assert record.first_line_component_ids == []       # unknown, not "no drugs"
    assert record.first_line_start_date == str(START)


def test_mapped_regimen_without_drugs_keeps_its_concept_and_asserted_origin(person):
    regimen = _regimen()
    _line(person, regimen_concept=regimen, regimen_source_concept=regimen)
    record = _record(person)

    assert record.therapy_lines_count == 1
    assert record.first_line_therapy == regimen.concept_name
    assert record.first_line_therapy_id == REGIMEN_ID
    assert record.therapy_ids_provenance['first_line_therapy_id']['origin'] == 'asserted'
    # The regimen's components may be expanded from the concept; what must not
    # appear is a drug the caller never supplied.
    assert not DrugExposure.objects.filter(person=person).exists()


def test_a_header_row_created_without_the_writer_is_still_ignored(person):
    regimen_concept = Concept.objects.get(pk=32531)
    episode = Episode.objects.create(
        episode_id=990_001, person=person, episode_concept=regimen_concept,
        episode_object_concept=regimen_concept, episode_type_concept=regimen_concept,
        episode_start_date=START, episode_number=1, episode_source_value='VRd',
    )
    assert not TherapyLineAssertion.objects.filter(episode=episode).exists()

    record = _record(person)
    assert not record.therapy_lines_count
    assert record.first_line_therapy is None


def test_retracting_the_line_withdraws_it_and_saving_again_restores_it(person):
    regimen = _regimen()
    result = _line(person, regimen_concept=regimen, regimen_source_concept=regimen)
    assert _record(person).therapy_lines_count == 1

    retract_therapy_line(person, result.episode)
    record = _record(person)
    assert not record.therapy_lines_count
    assert record.first_line_therapy is None

    _line(person, regimen_concept=regimen, regimen_source_concept=regimen)
    assert _record(person).therapy_lines_count == 1


def test_clearing_composition_keeps_the_treatment_assertion(person):
    _regimen()
    _, drug_a, _ = _drugs()
    author_therapy_line(
        person, line_number=1, start_date=START, regimen_concept_id=REGIMEN_ID,
        drugs=[{'concept_id': drug_a.pk, 'source_value': 'bortezomib'}],
    )
    assert _record(person).first_line_therapy_id == REGIMEN_ID

    # Composition cleared: treatment happened, the drugs are unknown.
    author_therapy_line(person, line_number=1, drugs=[], replace=True)

    record = _record(person)
    assert record.therapy_lines_count == 1
    assert record.first_line_therapy_id == REGIMEN_ID
    assert record.first_line_component_ids == []
    # The dropped exposure is retained as entered-in-error rather than deleted,
    # so an imported fact is never destroyed by clearing this line's claim.
    assert DrugExposure.objects.get(person=person).is_erroneous
    assert TherapyLineAssertion.objects.get(episode__person=person).state == 'asserted'


def test_retraction_does_not_delete_imported_exposures(person):
    from tests.factories import DrugExposureFactory
    _, drug_a, _ = _drugs()
    imported = DrugExposureFactory(person=person, drug_concept=drug_a, drug_exposure_start_date=START)
    result = _line(person, drug_exposure_ids=[imported.pk])
    retract_therapy_line(person, result.episode)

    assert DrugExposure.objects.filter(pk=imported.pk).exists()
    assert not DrugExposure.objects.get(pk=imported.pk).is_erroneous
    assert not _record(person).therapy_lines_count


def test_every_writer_asserts_and_records_its_source(person):
    _line(person, asserted_by='import')
    assertion = TherapyLineAssertion.objects.get(episode__person=person)
    assert assertion.state == 'asserted'
    assert assertion.source == 'import'


def _drugs():
    a = ConceptFactory(concept_name='Bortezomib')
    b = ConceptFactory(concept_name='Dexamethasone')
    return None, a, b


def test_serializer_marks_unknown_composition_instead_of_an_empty_drug_list(person):
    from patient_portal.api.serializers import PatientRecordSerializer

    _line(person)
    payload = PatientRecordSerializer(refresh_patient_record(person)).data
    line = payload['lines_of_therapy'][0]
    assert line['drugs'] == []
    assert line['composition_known'] is False


def test_serializer_marks_known_composition_for_a_line_with_drugs(person):
    from patient_portal.api.serializers import PatientRecordSerializer

    _, drug_a, _ = _drugs()
    author_therapy_line(
        person, line_number=1, start_date=START,
        drugs=[{'concept_id': drug_a.pk, 'source_value': 'bortezomib'}],
    )
    payload = PatientRecordSerializer(refresh_patient_record(person)).data
    line = payload['lines_of_therapy'][0]
    assert line['composition_known'] is True
    assert len(line['drugs']) == 1
