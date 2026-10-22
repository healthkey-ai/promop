"""/api/v1/phr/explanations/ — AI explanations ONE writes and the record shows."""
import pytest
from rest_framework.test import APIClient

from patient_portal.models import AiExplanation, LabMarker
from tests.factories import PatientRecordFactory
from tests.test_phr_read_model import condition, icd10, lab, loinc, signed_in

pytestmark = pytest.mark.django_db

KEY = 'one-server-explanation-key'
BODY = {'text': 'Type 2 diabetes means…', 'prompt_version': 'diagnosis@3', 'model': 'gemini-2.5-flash',
        'source_hash': 'a' * 64}


@pytest.fixture(autouse=True)
def _settings(settings):
    settings.CELERY_BROKER_URL = ''
    settings.PHR_EXPLANATION_WRITE_KEY = KEY
    settings.PHR_SHARE_URL = 'https://one.example/r'


@pytest.fixture
def patient():
    record = PatientRecordFactory(disease='')
    gout = condition(record, icd10('M10.9', 'Gout'), '2021-01-01')
    diabetes = condition(record, icd10('E11.9', 'Type 2 diabetes'), '2020-01-01')
    return record, signed_in(record), f'condition-{gout.pk}', f'condition-{diabetes.pk}'


def url(kind, target, shared=False):
    return f"/api/v1/phr/{'shared/' if shared else ''}explanations/{kind}/{target}/"


def test_only_ones_server_can_write_and_the_patient_reads_it_back(patient):
    record, client, gout, _ = patient
    assert client.put(url('diagnosis', gout), BODY, format='json').status_code == 403
    assert client.put(url('diagnosis', gout), BODY, format='json', HTTP_X_EXPLANATION_KEY='guess').status_code == 403

    written = client.put(url('diagnosis', gout), BODY, format='json', HTTP_X_EXPLANATION_KEY=KEY)
    assert written.status_code == 200 and written.data['model'] == 'gemini-2.5-flash'
    again = client.put(url('diagnosis', gout), {**BODY, 'text': 'Newer'}, format='json', HTTP_X_EXPLANATION_KEY=KEY)
    assert again.data['text'] == 'Newer' and AiExplanation.objects.count() == 1

    read = client.get(url('diagnosis', gout))
    assert read.status_code == 200 and read.data['text'] == 'Newer' and read.data['source_hash'] == 'a' * 64
    assert client.get(url('diagnosis', 'condition-0')).status_code == 404
    assert signed_in(PatientRecordFactory(disease='')).get(url('diagnosis', gout)).status_code == 404


def test_writes_are_refused_when_no_key_is_configured(patient, settings):
    _, client, gout, _ = patient
    settings.PHR_EXPLANATION_WRITE_KEY = ''
    assert client.put(url('diagnosis', gout), BODY, format='json', HTTP_X_EXPLANATION_KEY='').status_code == 403


def test_kind_target_and_fields_are_validated(patient):
    _, client, gout, _ = patient
    assert client.get(url('horoscope', gout)).status_code == 404
    assert client.get(url('diagnosis', 'x' * 121)).status_code == 404
    bad = client.put(url('diagnosis', gout), {'text': 'x' * 4001, 'model': '', 'prompt_version': 'v', 'source_hash': 'h'},
                     format='json', HTTP_X_EXPLANATION_KEY=KEY)
    assert bad.status_code == 400 and set(bad.data) == {'text', 'model'}
    assert APIClient().get(url('diagnosis', gout)).status_code in (401, 403)


def test_a_shared_record_shows_stored_explanations_for_shared_items_only(patient):
    record, client, gout, diabetes = patient
    LabMarker.objects.create(loinc_code='718-7', rank=11, panels=['CBC'])
    hgb = loinc('718-7', 'Hemoglobin [Mass/volume] in Blood')
    lab(record, hgb, '2024-03-01', 11.0)
    for kind, target in (('diagnosis', gout), ('diagnosis', diabetes), ('lab_result', str(hgb.concept_id))):
        client.put(url(kind, target), BODY, format='json', HTTP_X_EXPLANATION_KEY=KEY)
    share = client.post('/api/v1/phr/shares/', {
        'recipient': 'doctor', 'name': 'Dr. Chen', 'method': 'link', 'duration': '1w',
        'selection': {'diagnoses': {'items': [gout]}, 'labs': {'items': ['panel:CBC']}},
    }, format='json').data
    holder = APIClient()
    holder.credentials(HTTP_X_SHARE_TOKEN=share['url'].rsplit('/', 1)[1])

    shown = holder.get(url('diagnosis', gout, shared=True))
    assert shown.status_code == 200 and set(shown.data) == {'kind', 'target', 'text', 'generated_at'}
    assert holder.get(url('lab_result', str(hgb.concept_id), shared=True)).status_code == 200
    assert holder.get(url('diagnosis', diabetes, shared=True)).status_code == 404
    assert holder.get(url('genetic_test', 'test-1', shared=True)).status_code == 404
    assert holder.put(url('diagnosis', gout, shared=True), BODY, format='json', HTTP_X_EXPLANATION_KEY=KEY).status_code == 405
