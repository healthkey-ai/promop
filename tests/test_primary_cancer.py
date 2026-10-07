"""The derived primary cancer is the one being treated, not the latest diagnosis (#1738)."""
from datetime import timedelta

import pytest
from django.utils import timezone

from omop_core.models import PatientRecord
from omop_core.services.disease_episodes import disease_slug
from omop_core.services.episode_service import author_therapy_line
from omop_core.services.mappings import CONCEPT_EHR_TYPE, CONCEPT_TREATMENT_REGIMEN
from omop_core.services.patient_record_service import refresh_patient_record
from omop_core.signals import suppress_patient_record_refresh
from omop_oncology.models import Episode
from tests.factories import ConceptFactory, ConditionOccurrenceFactory, PatientRecordFactory, VocabularyFactory
from tests.test_lines_per_cancer import _concepts, _drug

pytestmark = pytest.mark.django_db

MYELOMA, PROSTATE, BCC = 'Multiple myeloma', 'Prostate cancer', 'Basal cell carcinoma'
TODAY = timezone.localdate()
MYELOMA_SLUG = disease_slug(MYELOMA)


def ago(days):
    return TODAY - timedelta(days=days)


def _person(**record):
    return PatientRecordFactory(**record).person


def _condition(person, name, start, end=None, status=None):
    icd = VocabularyFactory(vocabulary_id='ICD10CM', vocabulary_name='ICD10CM')
    with suppress_patient_record_refresh():
        return ConditionOccurrenceFactory(
            person=person, condition_start_date=start, condition_end_date=end,
            condition_status_source_value=status,
            condition_concept=ConceptFactory(concept_name=name, concept_code=name[:8], vocabulary=icd),
        )


def _refreshed(person):
    refresh_patient_record(person)
    return PatientRecord.objects.get(person=person)


def test_a_later_resolved_cancer_does_not_displace_the_treated_one():
    """The issue's case: an active myeloma on its lines, then a prostate cancer, treated and resolved."""
    drugs = _concepts()
    person = _person()
    _condition(person, MYELOMA, ago(940))
    _condition(person, PROSTATE, ago(330), end=ago(215))
    with suppress_patient_record_refresh():
        author_therapy_line(person, line_number=1, disease=MYELOMA, drugs=[_drug(drugs, 'lenalidomide')],
                            start_date=ago(930), end_date=ago(720), outcome='Very Good Partial Response')
        author_therapy_line(person, line_number=2, disease=MYELOMA, drugs=[_drug(drugs, 'dexamethasone')],
                            start_date=ago(80), outcome='Stringent Complete Response')
        author_therapy_line(person, line_number=1, disease=PROSTATE, drugs=[_drug(drugs, 'bicalutamide')],
                            start_date=ago(275), end_date=ago(200), outcome='Complete Response')

    record = _refreshed(person)

    assert record.disease_slug == disease_slug(MYELOMA)
    assert record.diagnosis_date == ago(940)
    # The flat therapy fields are the myeloma's two lines, not the prostate's one.
    assert record.therapy_lines_count == 2
    assert record.first_line_outcome == 'Very Good Partial Response'
    assert record.second_line_outcome == 'Stringent Complete Response'
    assert record.condition_clinical_status != 'resolved'

    # Stable: refreshing again (any write does) keeps it.
    assert _refreshed(person).disease_slug == MYELOMA_SLUG


def test_a_later_untreated_cancer_does_not_displace_the_treated_one():
    drugs = _concepts()
    person = _person()
    _condition(person, MYELOMA, ago(900))
    _condition(person, BCC, ago(20))
    with suppress_patient_record_refresh():
        author_therapy_line(person, line_number=1, disease=MYELOMA, drugs=[_drug(drugs, 'lenalidomide')],
                            start_date=ago(890), end_date=ago(500))

    assert _refreshed(person).disease_slug == MYELOMA_SLUG


def test_a_current_line_outranks_past_lines():
    drugs = _concepts()
    person = _person()
    _condition(person, PROSTATE, ago(900))
    _condition(person, MYELOMA, ago(1500))
    with suppress_patient_record_refresh():
        author_therapy_line(person, line_number=1, disease=PROSTATE, drugs=[_drug(drugs, 'bicalutamide')],
                            start_date=ago(890), end_date=ago(700))
        author_therapy_line(person, line_number=1, disease=MYELOMA, drugs=[_drug(drugs, 'lenalidomide')],
                            start_date=ago(30))

    assert _refreshed(person).disease_slug == MYELOMA_SLUG


def test_without_lines_an_active_cancer_outranks_a_later_resolved_one():
    person = _person()
    _condition(person, MYELOMA, ago(900))
    _condition(person, PROSTATE, ago(300), status='Resolved')

    record = _refreshed(person)
    assert record.disease_slug == MYELOMA_SLUG
    assert record.condition_clinical_status != 'resolved'


def test_without_lines_an_active_cancer_outranks_a_later_one_in_remission():
    person = _person()
    _condition(person, MYELOMA, ago(900))
    _condition(person, PROSTATE, ago(300), status='In remission')

    assert _refreshed(person).disease_slug == MYELOMA_SLUG


def test_without_lines_or_status_the_latest_diagnosis_still_wins():
    person = _person()
    _condition(person, PROSTATE, ago(900))
    _condition(person, MYELOMA, ago(300))

    assert _refreshed(person).disease_slug == MYELOMA_SLUG


def test_a_line_with_no_cancer_belongs_to_the_primary_the_record_already_holds():
    """Lines written before #1739 (or by the bulk importer) have no Disease Episode."""
    _concepts()
    person = _person(disease=MYELOMA, disease_slug=disease_slug(MYELOMA))
    _condition(person, MYELOMA, ago(900))
    _condition(person, BCC, ago(20))
    Episode.objects.create(
        episode_id=938_001, person=person, episode_concept_id=CONCEPT_TREATMENT_REGIMEN,
        episode_object_concept_id=0, episode_type_concept_id=CONCEPT_EHR_TYPE,
        episode_start_date=ago(60), episode_number=1, episode_source_value='LOT-1',
    )

    assert _refreshed(person).disease_slug == MYELOMA_SLUG


def test_the_status_is_the_cancers_not_a_later_conditions():
    person = _person()
    _condition(person, MYELOMA, ago(900), status='Active')
    _condition(person, 'Pneumonia', ago(30), end=ago(10), status='Resolved')

    record = _refreshed(person)
    assert record.disease_slug == MYELOMA_SLUG
    assert record.condition_clinical_status == 'active'


def test_one_cancer_costs_no_line_queries(monkeypatch):
    from omop_core.services import patient_record_service

    person = _person()
    _condition(person, MYELOMA, ago(900))
    monkeypatch.setattr(patient_record_service, '_cancers_with_lines',
                        lambda *a: pytest.fail('lines read for a single cancer'))

    assert _refreshed(person).disease_slug == MYELOMA_SLUG


def test_a_long_named_cancer_on_a_current_line_stays_primary():
    """Disease Episodes keep a slug's first 42 characters; the ranking must compare in that form."""
    long_name = 'Diffuse large B-cell lymphoma, unspecified site'
    assert len(disease_slug(long_name)) > 42
    drugs = _concepts()
    person = _person()
    _condition(person, long_name, ago(400))
    _condition(person, BCC, ago(20))
    with suppress_patient_record_refresh():
        author_therapy_line(person, line_number=1, disease=long_name, drugs=[_drug(drugs, 'lenalidomide')],
                            start_date=ago(30))

    assert _refreshed(person).disease_slug == disease_slug(long_name)
