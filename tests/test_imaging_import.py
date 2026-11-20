"""Structured imaging studies from FHIR (#1731, ADR 0003)."""
import base64
import io
import json

import pytest
from rest_framework.test import APIClient

from omop_core.models import (
    CareSite, Concept, ImageOccurrence, Note, NoteNlp, Observation, PatientDocument, Person,
    ProcedureOccurrence, ProvenanceRecord,
)
from omop_core.services.imaging import (
    contrast, import_imaging, imaging_studies, index_resources, is_radiology_report, modality_label,
)
from omop_core.services.mappings import CONCEPT_EHR_TYPE
from patient_portal.models import Identity
from tests.factories import ConceptFactory, VocabularyFactory

pytestmark = pytest.mark.django_db

# Exact bytes matter: trailing spaces, a non-breaking space, an en dash, CRLF.
IMPRESSION = 'Stable disease.\r\nNo new osseous lesions – L2, L4 unchanged.  \n'
PATIENT = 'imaging-patient'


@pytest.fixture
def concepts():
    omop = VocabularyFactory(vocabulary_id='OMOP', vocabulary_name='OMOP')
    for cid, name in ((0, 'No matching concept'), (CONCEPT_EHR_TYPE, 'EHR')):
        if not Concept.objects.filter(concept_id=cid).exists():
            ConceptFactory(concept_id=cid, concept_name=name, concept_code=str(cid), vocabulary=omop)


def _pet_ct():
    """A PET/CT report naming its ImagingStudy, with findings, a file and a performer."""
    report = {
        'resourceType': 'DiagnosticReport', 'id': 'rad-1', 'status': 'final',
        'subject': {'reference': f'Patient/{PATIENT}'},
        'category': [{'coding': [{'system': 'http://terminology.hl7.org/CodeSystem/v2-0074', 'code': 'RAD'}]}],
        'code': {'coding': [{'system': 'http://loinc.org', 'code': '81555-5'}],
                 'text': 'PET/CT skeletal survey, FDG'},
        'effectiveDateTime': '2026-06-18T09:30:00Z',
        'performer': [{'reference': 'Organization/mda'}],
        'imagingStudy': [{'reference': 'ImagingStudy/pet-1'}],
        'result': [{'reference': 'Observation/f1'}, {'reference': 'Observation/f2'}],
        'conclusion': IMPRESSION,
        'presentedForm': [
            {'contentType': 'text/plain',
             'data': base64.b64encode('Comparison: PET/CT of Feb 14, 2026.'.encode()).decode()},
            {'contentType': 'application/pdf', 'url': 'https://records.example.org/rad-1.pdf',
             'title': 'PET/CT report'},
        ],
    }
    study = {
        'resourceType': 'ImagingStudy', 'id': 'pet-1', 'status': 'available',
        'subject': {'reference': f'Patient/{PATIENT}'}, 'started': '2026-06-18T09:00:00Z',
        'identifier': [{'system': 'urn:dicom:uid', 'value': 'urn:oid:1.2.840.113619.2.55.3.1'}],
        'endpoint': [{'reference': 'Endpoint/pacs'}],
        'numberOfInstances': 412,
        'series': [
            {'uid': '1.2.3.1', 'modality': {'code': 'PT'},
             'bodySite': {'system': 'http://snomed.info/sct', 'code': '38266002', 'display': 'Whole body'}},
            {'uid': '1.2.3.2', 'modality': {'code': 'CT'}},
        ],
    }
    others = [
        {'resourceType': 'Organization', 'id': 'mda', 'name': 'MD Anderson radiology'},
        {'resourceType': 'Endpoint', 'id': 'pacs', 'status': 'active',
         'address': 'https://pacs.example.org/dicomweb/studies/1.2.840.113619.2.55.3.1'},
        {'resourceType': 'Observation', 'id': 'f1', 'status': 'final', 'code': {'text': 'Finding'},
         'subject': {'reference': f'Patient/{PATIENT}'},
         'valueString': 'No new lytic lesions'},
        {'resourceType': 'Observation', 'id': 'f2', 'status': 'final', 'code': {'text': 'Finding'},
         'subject': {'reference': f'Patient/{PATIENT}'},
         'valueString': 'Known lesions at L2, L4 and right femoral head unchanged'},
    ]
    return report, study, others


def _ct_chest_without_study():
    return {
        'resourceType': 'DiagnosticReport', 'id': 'rad-2', 'status': 'final',
        'subject': {'reference': f'Patient/{PATIENT}'},
        'category': [{'coding': [{'code': 'RAD'}]}],
        'code': {'text': 'CT CHEST WO CONTRAST'},
        'effectiveDateTime': '2026-09-05',
        'conclusion': 'Lungs clear.',
        'conclusionCode': [{'text': 'No acute finding'}],
    }


def _lab_report():
    return {
        'resourceType': 'DiagnosticReport', 'id': 'lab-1', 'status': 'final',
        'subject': {'reference': f'Patient/{PATIENT}'},
        'category': [{'coding': [{'code': 'LAB'}]}], 'code': {'text': 'CBC panel'},
        'effectiveDateTime': '2026-09-01', 'conclusion': 'Normal.',
    }


def _bundle(*resources):
    patient = {'resourceType': 'Patient', 'id': PATIENT, 'name': [{'family': 'Imaging', 'given': ['Ada']}],
               'gender': 'female', 'birthDate': '1961-02-14'}
    return {'resourceType': 'Bundle', 'type': 'collection',
            'entry': [{'resource': r} for r in (patient, *resources)]}


def _upload(bundle):
    client = APIClient()
    client.force_authenticate(Identity.objects.create_superuser(email='imaging@example.test', password='test')
                              if not Identity.objects.filter(email='imaging@example.test').exists()
                              else Identity.objects.get(email='imaging@example.test'))
    payload = io.BytesIO(json.dumps(bundle).encode())
    payload.name = 'imaging.json'
    response = client.post('/api/patient-info/upload_fhir/', {'file': payload}, format='multipart')
    assert response.status_code in (200, 201), response.data
    return Person.objects.get(family_name='Imaging')


def test_the_upload_maps_reports_and_studies_to_omop(concepts):
    report, study, others = _pet_ct()
    person = _upload(_bundle(report, study, *others, _ct_chest_without_study(), _lab_report()))

    studies = imaging_studies(person)
    assert [s['name'] for s in studies] == ['CT CHEST WO CONTRAST', 'PET/CT skeletal survey, FDG']
    ct, pet = studies

    # The impression is kept byte-for-byte.
    assert pet['impression'] == IMPRESSION
    assert Note.objects.get(note_event_id=pet['procedure_occurrence_id'],
                            note_source_value='rad-impression').note_text.encode() == IMPRESSION.encode()

    assert pet['type'] == 'PET/CT' and pet['modalities'] == ['PT', 'CT']
    assert str(pet['date']) == '2026-06-18'
    assert pet['body_part'] == 'Whole body'
    assert pet['contrast'] == {'status': 'with', 'text': 'FDG tracer'}
    assert pet['findings'] == ['No new lytic lesions', 'Known lesions at L2, L4 and right femoral head unchanged']
    assert pet['notes'] == 'Comparison: PET/CT of Feb 14, 2026.'
    assert pet['has_image'] is True
    assert pet['image_url'] == 'https://pacs.example.org/dicomweb/studies/1.2.840.113619.2.55.3.1'
    assert pet['document'].file_url == 'https://records.example.org/rad-1.pdf'
    assert pet['document'].doc_type == 'IMAGING'
    image = ImageOccurrence.objects.get(procedure_occurrence_id=pet['procedure_occurrence_id'])
    assert image.image_study_uid == '1.2.840.113619.2.55.3.1'
    # The facility comes through the visit, as for every other record row.
    procedure = pet['procedure']
    assert CareSite.objects.get(care_site_id=procedure.visit_occurrence.care_site_id).care_site_name == \
        'MD Anderson radiology'
    assert ProvenanceRecord.objects.filter(object_id=procedure.pk).exists()

    # A report with no ImagingStudy: a study without images, modality from its code.
    assert ct['type'] == 'CT'
    assert ct['contrast'] == {'status': 'without', 'text': 'None'}
    assert ct['findings'] == ['No acute finding']
    assert ct['has_image'] is False and ct['image_url'] is None and ct['document'] is None
    assert ct['notes'] is None

    # A lab report is not imaging.
    assert ProcedureOccurrence.objects.filter(person=person).count() == 2


def test_re_importing_rewrites_a_study_in_place(concepts):
    report, study, others = _pet_ct()
    person = _upload(_bundle(report, study, *others))
    counts = lambda: (ProcedureOccurrence.objects.filter(person=person).count(),  # noqa: E731
                      ImageOccurrence.objects.filter(person=person).count(),
                      Note.objects.filter(person=person).count(), NoteNlp.objects.count(),
                      Observation.objects.filter(person=person, observation_source_value='imaging-contrast').count(),
                      PatientDocument.objects.filter(person=person).count())
    before = counts()

    report['conclusion'] = 'Revised: stable disease.'
    _upload(_bundle(report, study, *others))

    assert counts() == before
    assert imaging_studies(person)[0]['impression'] == 'Revised: stable disease.'


def test_the_provider_sync_imports_imaging_with_its_provenance(concepts):
    from patient_portal.api.fhir.sync import FhirSyncView

    report, study, others = _pet_ct()
    person = Person.objects.create(person_id=731_001)
    bundle = _bundle(report, study, *others)
    view = FhirSyncView()
    ids = view._ingest_imaging(person, bundle, [report], [study], 'iss|sub', None)

    assert len(ids) == 1
    assert imaging_studies(person)[0]['impression'] == IMPRESSION
    assert ProvenanceRecord.objects.filter(object_id=ids[0], source='EHR_SYNC', source_user_id='iss|sub').exists()


def test_an_imaging_study_no_report_names_is_still_a_study(concepts):
    _, study, others = _pet_ct()
    person = Person.objects.create(person_id=731_002)
    import_imaging(person, [], [study], index_resources([{'resource': r} for r in (study, *others)]))

    (only,) = imaging_studies(person)
    assert only['type'] == 'PET/CT' and only['has_image'] is True
    assert only['impression'] is None and only['findings'] == []


def test_a_study_without_a_date_is_skipped(concepts):
    person = Person.objects.create(person_id=731_003)
    report = {**_ct_chest_without_study(), 'effectiveDateTime': None}
    assert import_imaging(person, [report], [], {}) == []


@pytest.mark.parametrize('texts, expected', [
    (['MRI THORACIC SPINE W WO CONTRAST'], ('with', 'With contrast')),
    (['MRI brain with gadolinium'], ('with', 'Gadolinium')),
    (['CT abdomen without contrast'], ('without', 'None')),
    (['Low-dose CT chest, non-contrast'], ('without', 'None')),
    (['X-ray left knee'], ('unknown', None)),
])
def test_contrast_is_read_from_the_studys_own_words(texts, expected):
    assert contrast(texts) == expected


def test_contrast_is_read_from_the_codes_display_too(concepts):
    person = Person.objects.create(person_id=731_004)
    report = {**_ct_chest_without_study(), 'code': {
        'text': 'Prostate MRI, multiparametric',
        'coding': [{'system': 'http://loinc.org', 'code': '30787-7', 'display': 'MR Prostate WO and W contrast IV'}]}}
    import_imaging(person, [report], [], {})
    (study,) = imaging_studies(person)
    assert study['name'] == 'Prostate MRI, multiparametric' and study['type'] == 'MRI'
    assert study['contrast']['status'] == 'with'


def test_modalities_and_radiology_reports():
    assert modality_label(['PT', 'CT']) == 'PET/CT'
    assert modality_label(['MR']) == 'MRI'
    assert modality_label(['DX']) == 'X-ray'
    assert is_radiology_report({'category': [{'text': 'Radiology'}]})
    assert is_radiology_report({'imagingStudy': [{'reference': 'ImagingStudy/1'}]})
    assert not is_radiology_report(_lab_report())


# ---------------------------------------------------------------- review fixes (pass 1)

def test_hostile_study_text_is_matched_in_linear_time():
    import time

    from omop_core.services.imaging import _strip_html

    started = time.monotonic()
    contrast(['w w ' * 20_000])
    _strip_html('<' * 80_000)
    assert time.monotonic() - started < 1.0


def test_a_report_never_takes_another_patients_study_or_findings(concepts):
    """Multi-patient uploads: references must not cross to another patient's resources."""
    report, study, others = _pet_ct()
    other = 'someone-else'
    study['subject'] = {'reference': f'Patient/{other}'}
    for res in others:
        if res['resourceType'] == 'Observation':
            res['subject'] = {'reference': f'Patient/{other}'}
    person = Person.objects.create(person_id=731_010)
    index = index_resources([{'resource': r} for r in (report, study, *others)])

    import_imaging(person, [report], [], index, subject_refs={f'Patient/{PATIENT}', PATIENT})

    (only,) = imaging_studies(person)
    assert (only['has_image'], only['image_url'], only['body_part'], only['findings']) == (False, None, None, [])


def test_a_practitioner_performer_is_not_a_facility_and_does_not_break_the_import(concepts):
    report, study, others = _pet_ct()
    report['performer'] = [{'reference': 'Practitioner/dr-smith', 'display': 'Dr Jane Smith'}]
    practitioner = {'resourceType': 'Practitioner', 'id': 'dr-smith', 'name': [{'family': 'Smith', 'given': ['Jane']}]}
    person = Person.objects.create(person_id=731_011)

    import_imaging(person, [report], [study], index_resources([{'resource': r} for r in (report, study, practitioner, *others)]))

    (only,) = imaging_studies(person)
    assert only['procedure'].visit_occurrence_id is None
    assert not CareSite.objects.filter(care_site_name__icontains='Smith').exists()


def test_two_studies_with_the_same_code_on_the_same_day_stay_two(concepts):
    first, second = _ct_chest_without_study(), _ct_chest_without_study()
    second['id'], second['conclusion'] = 'rad-3', 'Second read.'
    person = Person.objects.create(person_id=731_012)

    import_imaging(person, [first, second], [], {})
    import_imaging(person, [first, second], [], {})  # and re-importing keeps them apart

    assert sorted(s['impression'] for s in imaging_studies(person)) == ['Lungs clear.', 'Second read.']


def test_a_patient_entered_procedure_is_never_adopted(concepts):
    from omop_core.services.mappings import CONCEPT_PATIENT_REPORTED_TYPE

    ConceptFactory(concept_id=CONCEPT_PATIENT_REPORTED_TYPE, concept_name='Patient self-report',
                   concept_code=str(CONCEPT_PATIENT_REPORTED_TYPE),
                   vocabulary=Concept.objects.get(concept_id=0).vocabulary)
    person = Person.objects.create(person_id=731_013)
    own = ProcedureOccurrence.objects.create(
        procedure_occurrence_id=731_901, person=person, procedure_date='2026-09-05',
        procedure_concept_id=0, procedure_type_concept_id=CONCEPT_PATIENT_REPORTED_TYPE,
        procedure_source_value='CT CHEST WO CONTRAST',
    )

    import_imaging(person, [_ct_chest_without_study()], [], {})

    (study,) = imaging_studies(person)
    assert study['procedure_occurrence_id'] != own.pk
    assert not ImageOccurrence.objects.filter(procedure_occurrence=own).exists()


def test_an_entered_in_error_report_is_not_shown_and_retracts_an_earlier_import(concepts):
    person = Person.objects.create(person_id=731_014)
    report = _ct_chest_without_study()
    import_imaging(person, [report], [], {})
    assert len(imaging_studies(person)) == 1

    import_imaging(person, [{**report, 'status': 'entered-in-error'}], [], {})
    assert imaging_studies(person) == []

    other = Person.objects.create(person_id=731_015)
    import_imaging(other, [{**report, 'status': 'cancelled'}], [], {})
    assert imaging_studies(other) == []


def test_an_upload_with_two_patients_keeps_each_patients_imaging_apart(concepts):
    report, study, others = _pet_ct()
    other = {'resourceType': 'Patient', 'id': 'other', 'name': [{'family': 'Other', 'given': ['Bo']}],
             'gender': 'male', 'birthDate': '1950-01-01'}
    study['subject'] = {'reference': 'Patient/other'}  # the PET/CT study is someone else's
    bundle = _bundle(report, study, *others)
    bundle['entry'].append({'resource': other})

    person = _upload(bundle)

    (only,) = imaging_studies(person)
    assert (only['has_image'], only['image_url'], only['body_part']) == (False, None, None)
