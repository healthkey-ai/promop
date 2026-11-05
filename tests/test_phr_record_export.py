"""/api/v1/phr/record/export/ — the member's own record as a FHIR bundle or a PDF."""
import io
import json

import pytest
from rest_framework.test import APIClient

from patient_portal.models import AuditEvent, Identity, PatientUser
from tests.factories import PatientRecordFactory, PersonFactory
from tests.test_phr_read_model import condition, icd10, lab, loinc, signed_in

pytestmark = pytest.mark.django_db

EXPORT = '/api/v1/phr/record/export/'


@pytest.fixture(autouse=True)
def _inline_derivation(settings):
    settings.CELERY_BROKER_URL = ''


def member(**kwargs):
    record = PatientRecordFactory(disease='Multiple myeloma', disease_slug='myeloma', **kwargs)
    record.person.given_name, record.person.family_name = 'Ada', 'Lovelace'
    record.person.save(update_fields=['given_name', 'family_name'])
    return record


def pdf_text(body: bytes) -> str:
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(body))
    return '\n'.join(page.extract_text() or '' for page in reader.pages)


# ---------------------------------------------------------------- access


def test_requires_authentication():
    assert APIClient().get(EXPORT + '?format=fhir').status_code in (401, 403)


def test_identity_without_a_record_gets_404():
    identity = Identity.objects.create_user(email='clinician@example.test')
    client = APIClient()
    client.force_authenticate(identity)

    assert client.get(EXPORT + '?format=fhir').status_code == 404
    assert client.get(EXPORT + '?format=pdf').status_code == 404


def test_head_is_not_served():
    """HEAD would build the whole export unaudited (the middleware skips HEAD)."""
    assert signed_in(member()).head(EXPORT + '?format=pdf').status_code == 405


def test_the_format_must_be_named():
    client = signed_in(member())

    missing, unknown = client.get(EXPORT), client.get(EXPORT + '?format=docx')

    assert missing.status_code == 400 and missing.data['formats'] == ['fhir', 'pdf']
    assert unknown.status_code == 400


# ---------------------------------------------------------------- FHIR


def test_fhir_export_is_the_members_own_signed_bundle_and_nobody_elses():
    mine, theirs = member(), PatientRecordFactory(disease='')
    condition(mine, icd10('C90.0', 'Multiple myeloma'), '2024-01-15')
    condition(theirs, icd10('E11.9', 'Type 2 diabetes'), '2020-01-01')

    response = signed_in(mine).get(EXPORT + '?format=fhir')

    assert response.status_code == 200
    assert response['Content-Type'] == 'application/fhir+json'
    assert response['Content-Disposition'] == 'attachment; filename="my-record.fhir.json"'
    assert len(response['X-Content-SHA256']) == 64 and response['X-Content-Signature']
    bundle = json.loads(response.content)
    assert bundle['resourceType'] == 'Bundle'
    by_type = {}
    for entry in bundle['entry']:
        by_type.setdefault(entry['resource']['resourceType'], []).append(entry['resource'])
    assert by_type['Patient'][0]['name'] == [{'family': 'Lovelace', 'given': ['Ada']}]
    codes = [c['code']['coding'][0]['code'] for c in by_type['Condition']]
    assert codes == ['C90.0']
    assert 'E11.9' not in response.content.decode()


def test_fhir_export_integrity_headers_are_readable_cross_origin(settings):
    """#1603: the browser app that asks for the bundle can read its digest and signature."""
    settings.CORS_ALLOWED_ORIGINS = ['https://member.example.test']

    response = signed_in(member()).get(EXPORT + '?format=fhir', HTTP_ORIGIN='https://member.example.test')

    assert response.status_code == 200
    assert response['Access-Control-Allow-Origin'] == 'https://member.example.test'
    exposed = {h.strip() for h in response['Access-Control-Expose-Headers'].split(',')}
    assert {'X-Content-SHA256', 'X-Content-Signature', 'Content-Disposition'} <= exposed
    assert response['Cache-Control'] == 'no-store'


def test_fhir_export_refuses_another_fhir_version():
    response = signed_in(member()).get(EXPORT + '?format=fhir&fhirVersion=3.0')
    assert response.status_code == 406


# ---------------------------------------------------------------- PDF


def test_pdf_export_carries_each_section_of_the_record():
    record = member(facility_name='Dana-Farber', date_of_birth='1961-04-02', stage='II',
                    first_line_therapy='VRd', first_line_start_date='2024-03-12', first_line_end_date='2024-10-14',
                    first_line_outcome='VGPR')
    condition(record, icd10('E11.9', 'Type 2 diabetes'), '2020-01-01')
    lab(record, loinc('718-7', 'Hemoglobin [Mass/volume] in Blood'), '2024-05-01', 11.5, unit_source_value='g/dL')

    response = signed_in(record).get(EXPORT + '?format=pdf&lang=en')

    assert response.status_code == 200
    assert response['Content-Type'] == 'application/pdf'
    assert response['Content-Disposition'] == 'attachment; filename="my-record.pdf"'
    assert response.content.startswith(b'%PDF') and len(response.content) > 4000
    text = pdf_text(response.content)
    assert 'Ada Lovelace' in text
    assert 'Multiple myeloma' in text
    assert 'Type 2 diabetes' in text
    assert 'VRd' in text
    assert 'Hemoglobin' in text and '11.5 g/dL' in text
    for heading in ('About me', 'Diagnoses', 'Lines of therapy', 'Check-ins', 'Medications', 'Procedures',
                    'Lab results'):
        assert heading in text


def test_pdf_export_has_a_page_per_section_even_when_empty():
    from pypdf import PdfReader

    response = signed_in(member()).get(EXPORT + '?format=pdf')

    assert response.status_code == 200
    # A title page, then one page per section.
    assert len(PdfReader(io.BytesIO(response.content)).pages) == 8
    assert 'Nothing recorded.' in pdf_text(response.content)


def test_pdf_export_for_a_member_whose_record_is_not_derived_yet():
    """A linked person with no PatientRecord row still gets their (mostly empty) PDF."""
    person = PersonFactory(given_name='Ada', family_name='Lovelace')
    identity = Identity.objects.create_user(email='ada@example.test')
    PatientUser.objects.create(identity=identity, person=person)
    client = APIClient()
    client.force_authenticate(identity)

    response = client.get(EXPORT + '?format=pdf')

    assert response.status_code == 200
    assert response['Cache-Control'] == 'no-store'
    assert 'Ada Lovelace' in pdf_text(response.content)


def pdf_fonts(body: bytes) -> set[str]:
    """The base font names every page draws with."""
    from pypdf import PdfReader

    names = set()
    for page in PdfReader(io.BytesIO(body)).pages:
        for font in (page['/Resources'].get('/Font') or {}).values():
            names.add(str(font.get_object()['/BaseFont']))
    return names


@pytest.mark.parametrize('given, family', [
    ('Иван', 'Петров'),          # Cyrillic
    ('Νίκος', 'Παπαδόπουλος'),   # Greek
    ('Łukasz', 'Żółć'),          # Polish
    ('Nguyễn', 'Hồng Ánh'),      # Vietnamese
])
def test_pdf_export_renders_a_name_outside_western_latin(given, family):
    """The embedded font has the glyphs: the name on the title page, the footer and About me reads back."""
    record = member(facility_name='Больница №1')
    record.person.given_name, record.person.family_name = given, family
    record.person.save(update_fields=['given_name', 'family_name'])

    response = signed_in(record).get(EXPORT + '?format=pdf')

    assert response.status_code == 200
    text = pdf_text(response.content)
    assert f'{given} {family}' in text
    assert 'Больница №1' in text


def test_pdf_export_uses_the_embedded_font_everywhere():
    """Not one page falls back to Helvetica, whose glyphs stop at Western Latin."""
    record = member(facility_name='Dana-Farber', first_line_therapy='VRd', first_line_start_date='2024-03-12')
    condition(record, icd10('E11.9', 'Type 2 diabetes'), '2020-01-01')
    lab(record, loinc('718-7', 'Hemoglobin [Mass/volume] in Blood'), '2024-05-01', 11.5, unit_source_value='g/dL')

    response = signed_in(record).get(EXPORT + '?format=pdf')

    fonts = pdf_fonts(response.content)
    assert fonts and all('DejaVuSans' in name for name in fonts), fonts


def test_pdf_export_survives_a_cjk_name():
    """DejaVu Sans has no CJK glyphs (documented): the name shows as boxes, the export still succeeds."""
    record = member()
    record.person.given_name, record.person.family_name = '李', '小龍'
    record.person.save(update_fields=['given_name', 'family_name'])

    response = signed_in(record).get(EXPORT + '?format=pdf')

    assert response.status_code == 200 and response.content.startswith(b'%PDF')


def test_pdf_export_falls_back_to_english_for_an_unknown_language():
    response = signed_in(member()).get(EXPORT + '?format=pdf&lang=xx')
    assert response.status_code == 200
    assert 'My record' in pdf_text(response.content)


# ---------------------------------------------------------------- audit


@pytest.mark.parametrize('fmt', ['fhir', 'pdf'])
def test_every_export_is_audited_with_an_export_marker(fmt):
    record = member()
    client = signed_in(record)
    AuditEvent.objects.all().delete()

    assert client.get(EXPORT + f'?format={fmt}').status_code == 200

    event = AuditEvent.objects.get(path=EXPORT)
    assert event.event_type == AuditEvent.EVENT_VIEW
    assert event.detail == {'export': fmt, 'person_id': record.person_id}
    assert event.user_email == f'p{record.person_id}@example.test'
    assert event.signature_valid()


def test_an_audit_detail_the_row_cannot_hold_is_dropped_not_fatal(monkeypatch):
    """The hook takes a small JSON object; anything else is dropped with a warning, the row still written."""
    from patient_portal.api.phr import export as export_module

    class Unserializable:
        pass

    def get(self, request):
        request._request.audit_detail = {'export': Unserializable()}
        return export_module.Response({'ok': True})

    monkeypatch.setattr(export_module.RecordExportView, 'get', get)
    client = signed_in(member())
    AuditEvent.objects.all().delete()

    assert client.get(EXPORT).status_code == 200

    event = AuditEvent.objects.get(path=EXPORT)
    assert event.detail is None and event.signature_valid()


def test_a_refused_export_is_audited_without_the_marker():
    client = signed_in(member())
    AuditEvent.objects.all().delete()

    assert client.get(EXPORT + '?format=docx').status_code == 400

    event = AuditEvent.objects.get(path=EXPORT)
    assert event.status_code == 400 and event.detail is None
