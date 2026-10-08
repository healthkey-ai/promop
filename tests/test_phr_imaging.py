"""GET /api/v1/phr/imaging/ — structured imaging studies (#1731)."""
import pytest
from rest_framework.test import APIClient

from omop_core.models import PatientDocument, ProcedureOccurrence
from omop_core.services.imaging import import_imaging, index_resources
from tests.factories import ConceptFactory, PatientRecordFactory
from tests.test_imaging_import import IMPRESSION, _ct_chest_without_study, _pet_ct, concepts  # noqa: F401
from tests.test_phr_read_model import signed_in
from tests.test_phr_sharing import grant, holder

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _settings(settings):
    settings.CELERY_BROKER_URL = ''
    settings.PHR_SHARE_URL = 'https://one.example/r'


def _studies(record):
    report, study, others = _pet_ct()
    ct = _ct_chest_without_study()
    resources = (report, study, ct, *others)
    return import_imaging(record.person, [report, ct], [study], index_resources([{'resource': r} for r in resources]))


def test_a_patient_reads_their_studies_with_every_field(concepts):  # noqa: F811
    record = PatientRecordFactory(disease='')
    pet_id, ct_id = _studies(record)
    PatientDocument.objects.create(person=record.person, doc_type='IMAGING', title='Bone survey (scan)',
                                   effective_date='2024-01-10')

    studies = signed_in(record).get('/api/v1/phr/imaging/').data['studies']

    assert [s['id'] for s in studies][:2] == [f'study-{ct_id}', f'study-{pet_id}']
    pet = studies[1]
    assert pet == {
        'id': f'study-{pet_id}',
        'type': 'PET/CT',
        'name': 'PET/CT skeletal survey, FDG',
        'date': '2026-06-18',
        'body_part': 'Whole body',
        'contrast': {'status': 'with', 'text': 'FDG tracer'},
        'impression': IMPRESSION,
        'findings': ['No new lytic lesions', 'Known lesions at L2, L4 and right femoral head unchanged'],
        'notes': 'Comparison: PET/CT of Feb 14, 2026.',
        'has_image': True,
        'image_url': 'https://pacs.example.org/dicomweb/studies/1.2.840.113619.2.55.3.1',
        'document': {'title': 'PET/CT report', 'url': 'https://records.example.org/rad-1.pdf'},
        'source': {'kind': 'record', 'facility': 'MD Anderson radiology', 'date': '2026-06-18'},
    }
    ct = studies[0]
    assert ct['image_url'] is None and ct['has_image'] is False and 'document' not in ct
    # A report filed without a study is still listed, as filed; the PET/CT's
    # own report file is not listed twice.
    assert [s['name'] for s in studies[2:]] == ['Bone survey (scan)']


def test_studies_are_scoped_to_the_caller(concepts):  # noqa: F811
    mine, theirs = PatientRecordFactory(disease=''), PatientRecordFactory(disease='')
    _studies(theirs)

    assert signed_in(mine).get('/api/v1/phr/imaging/').data == {'studies': []}
    assert APIClient().get('/api/v1/phr/imaging/').status_code in (401, 403)


def test_imaging_studies_are_not_procedures(concepts):  # noqa: F811
    record = PatientRecordFactory(disease='')
    _studies(record)
    ProcedureOccurrence.objects.create(
        procedure_occurrence_id=731_901, person=record.person, procedure_date='2024-05-01',
        procedure_concept=ConceptFactory(concept_name='Bone marrow biopsy', concept_code='BMB'),
        procedure_type_concept_id=0,
    )
    client = signed_in(record)

    assert [p['name'] for p in client.get('/api/v1/phr/procedures/').data['procedures']] == ['Bone marrow biopsy']
    status = client.get('/api/v1/phr/status/').data['sections']
    assert status['imaging'] == 'ready' and status['procedures'] == 'ready'


def test_a_study_alone_makes_the_section_ready(concepts):  # noqa: F811
    record = PatientRecordFactory(disease='')
    client = signed_in(record)
    assert client.get('/api/v1/phr/status/').data['sections']['imaging'] == 'empty'
    _studies(record)
    sections = client.get('/api/v1/phr/status/').data['sections']
    assert sections['imaging'] == 'ready' and sections['procedures'] == 'empty'


def test_a_shared_record_shows_only_the_chosen_studies(concepts):  # noqa: F811
    record = PatientRecordFactory(disease='')
    pet_id, _ = _studies(record)
    client = signed_in(record)

    items = {s['id']: s for s in client.get('/api/v1/phr/shares/options/').data['sections']}['imaging']['items']
    assert {i['key'] for i in items} >= {f'study-{pet_id}'}
    share = grant(client, selection={'imaging': {'items': [f'study-{pet_id}']}}).data

    shared = holder(share).get('/api/v1/phr/shared/imaging/').data['studies']
    assert [(s['id'], s['impression']) for s in shared] == [(f'study-{pet_id}', IMPRESSION)]
