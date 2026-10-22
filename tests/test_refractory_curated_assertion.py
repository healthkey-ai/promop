"""#785 — curated drug-class refractoriness supersedes the drug-exposure guess.

`btk_inhibitor_refractory` and `bcl2_inhibitor_refractory` were never stored
facts. `_get_cll_data` infers them: it looks for a BTK/BCL2 drug exposure and
ANDs it with any progression observation, so the answer means "this patient took
the drug and progressed at some point", not "this drug failed". That is the
ambiguity `mappings.py` flags, and the reason #785 asked for source concepts.

Migration 0180 mints a concept per drug class and 0181 maps it. These tests pin
the two halves of what that has to mean: the inference still applies where
nothing was curated, and a curated answer is not overwritten by the next
derivation — which it would be, because `_get_cll_data` runs after
`_get_assertion_data` and assigns field by field.
"""

from datetime import date

import pytest

from omop_core.services.patient_record_service import refresh_patient_record
from tests.factories import (
    ConceptFactory,
    DomainFactory,
    DrugExposureFactory,
    ObservationFactory,
    PatientRecordFactory,
    PersonFactory,
    VocabularyFactory,
)


pytestmark = pytest.mark.django_db

_PROGRESSION_CODE = '182842009'
_BTK_CURATED_CODE = 'hko:btk-inhibitor-refractory'
_BCL2_CURATED_CODE = 'hko:bcl2-inhibitor-refractory'


def _curated_concept(code, name):
    """A concept shaped like the ones migration 0180 mints.

    The vocabulary and domain are created explicitly: the pytest suite runs
    --no-migrations, so nothing has seeded HK-Observation or the Observation
    domain, and Concept FKs both.
    """
    return ConceptFactory(
        concept_code=code, concept_name=name,
        vocabulary=VocabularyFactory(
            vocabulary_id='HK-Observation',
            vocabulary_name='HealthKey local observation source concepts',
        ),
        domain=DomainFactory(domain_id='Observation', domain_name='Observation'),
        standard_concept=None,
    )


def _person_on_btk_with_progression():
    """A record the inference reads as BTK-refractory."""
    person = PersonFactory()
    PatientRecordFactory(person=person)
    ibrutinib = ConceptFactory(concept_code='RX-IBRUT', concept_name='Ibrutinib')
    progression = ConceptFactory(
        concept_code=_PROGRESSION_CODE, concept_name='Disease progression',
    )
    DrugExposureFactory(person=person, drug_concept=ibrutinib)
    ObservationFactory(
        person=person, observation_concept=progression,
        observation_source_value=_PROGRESSION_CODE,
        observation_date=date(2026, 1, 1),
    )
    return person


def _curate(person, code, name, answer='no', on=date(2026, 6, 1)):
    ObservationFactory(
        person=person, observation_concept=_curated_concept(code, name),
        observation_source_value=code, observation_date=on,
        value_as_string=answer,
    )


def test_drug_exposure_inference_still_applies_without_a_curated_answer():
    """Pre-existing behaviour, unchanged where nothing was curated."""
    person = _person_on_btk_with_progression()

    record = refresh_patient_record(person)

    assert record.btk_inhibitor_refractory is True


def test_curated_assertion_supersedes_the_inference():
    """An explicit 'not refractory' must survive a progression on the record.

    This is the case the inference gets wrong: the patient took ibrutinib and
    progressed, but the clinician recorded that the disease was not refractory
    to it. Without the curated read-back the next derivation flips it back.
    """
    person = _person_on_btk_with_progression()
    _curate(person, _BTK_CURATED_CODE, 'BTK inhibitor refractory disease status')

    record = refresh_patient_record(person)

    assert record.btk_inhibitor_refractory is False


def test_curated_answer_is_stable_across_repeated_derivation():
    """Derivation re-runs constantly; a curated answer must not decay."""
    person = _person_on_btk_with_progression()
    _curate(person, _BTK_CURATED_CODE, 'BTK inhibitor refractory disease status')

    refresh_patient_record(person)
    record = refresh_patient_record(person)

    assert record.btk_inhibitor_refractory is False


def test_curating_one_drug_class_does_not_answer_the_other():
    """The whole point of two concepts: the classes are answered separately.

    Under the shared SNOMED code this was inexpressible — one code had to stand
    for both questions.
    """
    person = PersonFactory()
    PatientRecordFactory(person=person)
    ibrutinib = ConceptFactory(concept_code='RX-IBRUT', concept_name='Ibrutinib')
    venetoclax = ConceptFactory(concept_code='RX-VENE', concept_name='Venetoclax')
    progression = ConceptFactory(
        concept_code=_PROGRESSION_CODE, concept_name='Disease progression',
    )
    DrugExposureFactory(person=person, drug_concept=ibrutinib)
    DrugExposureFactory(person=person, drug_concept=venetoclax)
    ObservationFactory(
        person=person, observation_concept=progression,
        observation_source_value=_PROGRESSION_CODE,
        observation_date=date(2026, 1, 1),
    )
    _curate(person, _BTK_CURATED_CODE, 'BTK inhibitor refractory disease status')

    record = refresh_patient_record(person)

    # BTK was answered explicitly; BCL2 was not, so it still falls back to the
    # inference over the venetoclax exposure.
    assert record.btk_inhibitor_refractory is False
    assert record.bcl2_inhibitor_refractory is True


def test_curated_yes_is_also_honoured():
    """Not just the 'no' direction — the curated answer is the answer."""
    person = PersonFactory()
    PatientRecordFactory(person=person)
    venetoclax = ConceptFactory(concept_code='RX-VENE', concept_name='Venetoclax')
    DrugExposureFactory(person=person, drug_concept=venetoclax)
    # No progression observation, so the inference would say False.
    _curate(person, _BCL2_CURATED_CODE,
            'BCL-2 inhibitor refractory disease status', answer='yes')

    record = refresh_patient_record(person)

    assert record.bcl2_inhibitor_refractory is True
