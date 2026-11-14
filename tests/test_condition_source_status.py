"""EHR sync keeps what a Condition says about itself (#2512).

Category, verification status, clinical status and abatement were dropped at
ingest, so care-plan items, refuted entries and resolved conditions could not
be told from real active diagnoses.
"""
from datetime import date

import pytest

from omop_core.models import ConditionOccurrence
from patient_portal.api.fhir.sync import FhirSyncView
from tests.factories import ConceptFactory, PersonFactory

pytestmark = pytest.mark.django_db

HL7 = 'http://terminology.hl7.org/CodeSystem/'


@pytest.fixture
def sync():
    person = PersonFactory()
    ehr = ConceptFactory(concept_name='EHR', concept_code='EHR-2512')
    no_match = ConceptFactory(concept_name='No matching concept', concept_code='NM-2512')
    found = ConceptFactory(concept_name='Hypothyroidism', concept_code='40930008')
    cache = {('SNOMED', '40930008'): found, ('*', '40930008'): found}

    def run(conditions):
        return FhirSyncView()._ingest_conditions(person, conditions, ehr, no_match, cache, 'iss|sub', None, {})
    run.person = person
    return run


def coded(system, code):
    return {'coding': [{'system': HL7 + system, 'code': code}]}


def condition(**extra):
    return {'resourceType': 'Condition', 'onsetDateTime': '2020-03-01',
            'code': {'coding': [{'system': 'http://snomed.info/sct', 'code': '40930008'}]}, **extra}


def stored(sync):
    return ConditionOccurrence.objects.get(person=sync.person)


def test_status_category_and_abatement_are_kept(sync):
    sync([condition(
        category=[coded('condition-category', 'problem-list-item')],
        verificationStatus=coded('condition-ver-status', 'confirmed'),
        clinicalStatus=coded('condition-clinical', 'resolved'),
        abatementDateTime='2022-06-15T10:00:00Z')])

    row = stored(sync)
    assert row.condition_category == 'problem-list-item'
    assert row.condition_verification_status == 'confirmed'
    assert row.condition_clinical_status == 'resolved'
    assert row.condition_end_date == date(2022, 6, 15)


def test_several_categories_are_joined_and_abatement_period_end_is_read(sync):
    sync([condition(
        category=[coded('condition-category', 'encounter-diagnosis'), coded('condition-category', 'problem-list-item')],
        abatementPeriod={'start': '2021-01-01', 'end': '2021-02-02'})])

    row = stored(sync)
    assert row.condition_category == 'encounter-diagnosis,problem-list-item'
    assert row.condition_end_date == date(2021, 2, 2)


def test_what_the_source_did_not_say_stays_null(sync):
    sync([condition()])

    row = stored(sync)
    assert (row.condition_category, row.condition_verification_status,
            row.condition_clinical_status, row.condition_end_date) == (None, None, None, None)


def test_a_resync_fills_the_status_on_a_row_stored_without_it(sync):
    sync([condition()])
    sync([condition(clinicalStatus=coded('condition-clinical', 'inactive'),
                    abatementDate='2023-01-01')])

    assert ConditionOccurrence.objects.filter(person=sync.person).count() == 1
    row = stored(sync)
    assert row.condition_clinical_status == 'inactive'
    assert row.condition_end_date == date(2023, 1, 1)


def test_a_resync_that_omits_the_status_does_not_blank_it(sync):
    sync([condition(clinicalStatus=coded('condition-clinical', 'active'))])
    sync([condition()])

    assert stored(sync).condition_clinical_status == 'active'
