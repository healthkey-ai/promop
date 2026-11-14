"""#2567: the status left by a past procedure (Z94, transplant status) is listed
as a historical procedure, apart from the diagnoses."""
import pytest

from tests.factories import ConceptFactory, PatientRecordFactory
from tests.test_phr_read_model import condition, icd10, signed_in

pytestmark = pytest.mark.django_db

URL = '/api/v1/phr/diagnoses/'


@pytest.fixture(autouse=True)
def _inline_derivation(settings):
    settings.CELERY_BROKER_URL = ''


def unmatched():
    return ConceptFactory(concept_id=0, concept_code='No matching concept', concept_name='No matching concept')


def test_transplant_status_is_a_historical_procedure_and_not_an_other_diagnosis():
    record = PatientRecordFactory(disease='')
    transplant = condition(record, icd10('Z94.81', 'Bone marrow transplant status'), '2016-09-26')
    condition(record, icd10('E11.9', 'Type 2 diabetes'), '2020-01-01')

    data = signed_in(record).get(URL).data

    assert [(c['name'], c['date']) for c in data['historical_procedures']] == [
        ('Bone marrow transplant status', '2016-09-26')]
    assert data['historical_procedures'][0]['id'] == f'condition-{transplant.pk}'
    assert 'source' in data['historical_procedures'][0]
    assert [c['name'] for c in data['other']] == ['Type 2 diabetes']
    assert data['cancer'] == []


def test_a_bare_z94_source_code_is_routed_and_named_from_the_terminology():
    record = PatientRecordFactory(disease='')
    icd10('Z94.81', 'Bone marrow transplant status')
    condition(record, unmatched(), '2016-09-26', condition_source_value='Z94.81')

    data = signed_in(record).get(URL).data

    assert [c['name'] for c in data['historical_procedures']] == ['Bone marrow transplant status']
    assert data['other'] == []


def test_the_whole_z94_block_is_historical_and_nearby_codes_are_not():
    record = PatientRecordFactory(disease='')
    condition(record, icd10('Z94.0', 'Kidney transplant status'), '2010-01-01')
    condition(record, icd10('Z94.84', 'Stem cells transplant status'), '2012-01-01')
    condition(record, icd10('Z93.0', 'Tracheostomy status'), '2013-01-01')
    condition(record, icd10('Z95.1', 'Presence of aortocoronary bypass graft'), '2014-01-01')

    data = signed_in(record).get(URL).data

    assert [c['name'] for c in data['historical_procedures']] == [
        'Stem cells transplant status', 'Kidney transplant status']
    assert sorted(c['name'] for c in data['other']) == [
        'Presence of aortocoronary bypass graft', 'Tracheostomy status']


def test_repeated_status_collapses_to_the_earliest_date():
    record = PatientRecordFactory(disease='')
    concept = icd10('Z94.81', 'Bone marrow transplant status')
    condition(record, concept, '2016-09-26')
    condition(record, concept, '2019-05-01')

    data = signed_in(record).get(URL).data

    assert [(c['name'], c['date']) for c in data['historical_procedures']] == [
        ('Bone marrow transplant status', '2016-09-26')]


def test_a_patient_with_none_gets_an_empty_list():
    record = PatientRecordFactory(disease='')
    condition(record, icd10('E11.9', 'Type 2 diabetes'), '2020-01-01')

    assert signed_in(record).get(URL).data['historical_procedures'] == []


def test_the_entry_can_still_be_shared_and_the_section_has_one_shape():
    from patient_portal.api.phr import sharing
    from patient_portal.api.phr.views import diagnoses

    record = PatientRecordFactory(disease='')
    transplant = condition(record, icd10('Z94.81', 'Bone marrow transplant status'), '2016-09-26')

    catalog = sharing.catalog(record.person, record)
    items = next(s for s in catalog if s['id'] == 'diagnoses')['items']
    assert f'condition-{transplant.pk}' in {i['key'] for i in items}
    assert set(diagnoses(record.person, record)) == {'cancer', 'other', 'historical_procedures'}
