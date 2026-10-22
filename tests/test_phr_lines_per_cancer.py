"""/api/v1/phr/therapy/ with lines per cancer (#1739): one track per cancer."""
from datetime import date

import pytest

from omop_core.services.disease_episodes import disease_slug
from omop_core.services.episode_service import author_therapy_line
from omop_core.services.mappings import (
    CONCEPT_DISEASE_FIRST_OCCURRENCE,
    CONCEPT_DRUG_EXPOSURE_FIELD,
    CONCEPT_EHR_TYPE,
    CONCEPT_TREATMENT_REGIMEN,
)
from patient_portal.models import LabMarker
from tests.factories import ConceptFactory, PatientRecordFactory, VocabularyFactory
from tests.test_phr_read_model import condition, icd10, lab, loinc, signed_in

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _inline_derivation(settings):
    settings.CELERY_BROKER_URL = ''


@pytest.fixture
def two_cancers():
    omop = VocabularyFactory(vocabulary_id='OMOP', vocabulary_name='OMOP')
    ConceptFactory(concept_id=0, concept_name='No matching concept', concept_code='0', vocabulary=omop)
    for cid, name in ((CONCEPT_TREATMENT_REGIMEN, 'Treatment Regimen'), (CONCEPT_EHR_TYPE, 'EHR'),
                      (CONCEPT_DRUG_EXPOSURE_FIELD, 'drug_exposure_id'),
                      (CONCEPT_DISEASE_FIRST_OCCURRENCE, 'Disease First Occurrence')):
        ConceptFactory(concept_id=cid, concept_name=name, concept_code=str(cid), vocabulary=omop)
    record = PatientRecordFactory(
        disease='Multiple myeloma', disease_slug=disease_slug('Multiple myeloma'), facility_name='MD Anderson',
    )
    condition(record, icd10('C61', 'Prostate cancer'), '2025-11-08')
    condition(record, icd10('C44.92', 'Basal cell carcinoma'), '2021-08-21')
    lenalidomide = ConceptFactory(concept_name='lenalidomide', concept_code='RX-LEN')
    bicalutamide = ConceptFactory(concept_name='bicalutamide', concept_code='RX-BIC')
    myeloma = author_therapy_line(record.person, line_number=1, start_date=date(2024, 3, 12),
                                  end_date=date(2024, 10, 14), outcome='VGPR',
                                  drugs=[{'concept_id': lenalidomide.concept_id, 'source_value': 'lenalidomide'}])
    prostate = author_therapy_line(record.person, line_number=1, start_date=date(2026, 1, 5),
                                   end_date=date(2026, 3, 20), outcome='CR', disease='Prostate cancer',
                                   drugs=[{'concept_id': bicalutamide.concept_id, 'source_value': 'bicalutamide'}])
    return record, signed_in(record), myeloma.episode, prostate.episode


def test_each_cancer_gets_its_own_track_and_its_own_line_1(two_cancers):
    record, client, myeloma, prostate = two_cancers

    tracks = client.get('/api/v1/phr/therapy/').data['tracks']

    by_name = {t['diagnosis']['name']: t for t in tracks}
    assert list(by_name) == ['Multiple myeloma', 'Prostate cancer', 'Basal cell carcinoma']
    assert [(l['id'], l['number']) for l in by_name['Prostate cancer']['lines']] == [(str(prostate.episode_id), 1)]
    assert by_name['Prostate cancer']['lines'][0]['outcome']['label'] == 'Complete response'
    assert by_name['Prostate cancer']['lines'][0]['medications'][0]['name'] == 'bicalutamide'
    assert [l['id'] for l in by_name['Multiple myeloma']['lines']] == [str(myeloma.episode_id)]
    # A cancer with no lines is listed, so the record can say so when filtered to it.
    assert by_name['Basal cell carcinoma']['lines'] == []


def test_the_patient_can_say_why_the_second_cancer_s_line_ended(two_cancers):
    _, client, _, prostate = two_cancers
    saved = client.put(f'/api/v1/phr/therapy/{prostate.episode_id}/reason/',
                       {'reason': 'finished', 'note': ''}, format='json')
    assert saved.status_code == 200, saved.data
    tracks = client.get('/api/v1/phr/therapy/').data['tracks']
    line = next(t for t in tracks if t['diagnosis']['name'] == 'Prostate cancer')['lines'][0]
    assert line['patient_reason']['reason'] == 'finished'


def test_a_marker_chart_is_shaded_with_the_lines_of_the_cancer_it_marks(two_cancers):
    record, client, _, _ = two_cancers
    LabMarker.objects.create(loinc_code='2857-1', rank=1, disease_slugs=[disease_slug('Prostate cancer')])
    psa = loinc('2857-1', 'Prostate specific Ag [Mass/volume] in Serum or Plasma')
    lab(record, psa, '2026-02-01', 0.02)
    hgb = loinc('718-7', 'Hemoglobin [Mass/volume] in Blood')
    lab(record, hgb, '2024-05-01', 11.0)

    psa_lines = client.get(f'/api/v1/phr/labs/{psa.concept_id}/').data['therapy']
    hgb_lines = client.get(f'/api/v1/phr/labs/{hgb.concept_id}/').data['therapy']
    assert [l['start'] for l in psa_lines] == ['2026-01-05']
    assert [l['start'] for l in hgb_lines] == ['2024-03-12']


def test_status_counts_any_cancer_s_lines():
    omop = VocabularyFactory(vocabulary_id='OMOP', vocabulary_name='OMOP')
    for cid, name in ((0, 'No matching concept'), (CONCEPT_TREATMENT_REGIMEN, 'Treatment Regimen'),
                      (CONCEPT_EHR_TYPE, 'EHR'), (CONCEPT_DISEASE_FIRST_OCCURRENCE, 'Disease First Occurrence')):
        ConceptFactory(concept_id=cid, concept_name=name, concept_code=str(cid), vocabulary=omop)
    record = PatientRecordFactory(disease='Multiple myeloma', disease_slug=disease_slug('Multiple myeloma'))
    condition(record, icd10('C61', 'Prostate cancer'), '2025-11-08')
    from omop_core.services.episode_service import upsert_therapy_line_episode

    upsert_therapy_line_episode(record.person, line_number=1, start_date=date(2026, 1, 5), disease='Prostate cancer')
    status = signed_in(record).get('/api/v1/phr/status/').data['sections']
    assert status['therapy'] == 'ready'


def test_a_shared_marker_chart_shows_only_the_lines_the_patient_shared(two_cancers, settings):
    """Sharing labs and one myeloma line must not reveal the prostate cancer's treatment on the PSA chart."""
    from tests.test_phr_sharing import grant, holder

    settings.PHR_SHARE_URL = 'https://one.example/r'
    record, client, myeloma, _ = two_cancers
    LabMarker.objects.create(loinc_code='2857-1', rank=1, disease_slugs=[disease_slug('Prostate cancer')])
    psa = loinc('2857-1', 'Prostate specific Ag [Mass/volume] in Serum or Plasma')
    lab(record, psa, '2026-02-01', 0.02)
    share = grant(client, selection={'labs': {'all': True}, 'therapy': {'items': [str(myeloma.episode_id)]}}).data

    shared = holder(share).get(f'/api/v1/phr/shared/labs/{psa.concept_id}/')
    assert shared.status_code == 200
    assert shared.data['therapy'] == []
    # The patient's own chart still shows the prostate line.
    assert [l['start'] for l in client.get(f'/api/v1/phr/labs/{psa.concept_id}/').data['therapy']] == ['2026-01-05']
