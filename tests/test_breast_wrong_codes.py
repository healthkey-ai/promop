from datetime import date
from io import StringIO, BytesIO
import json

import pytest
from django.core.management import call_command

from omop_core.models import FieldConceptMapping, Measurement, PatientRecord
from omop_core.services.patient_record_service import refresh_patient_record
from omop_core.services.write_descriptor import build_writable_field_descriptor
from tests.factories import ConceptFactory, MeasurementFactory, PatientRecordFactory, PersonFactory

pytestmark = pytest.mark.django_db

# Code meanings are independently defined in LOINC, not inferred from labels
# previously used by the application's extractors.
LOINC_NAMES = {
    '85319-2': 'HER2 Ag [Presence] in Breast cancer specimen by Immune stain',
    '85337-4': 'Estrogen receptor Ag [Presence] in Breast cancer specimen by Immune stain',
    '92837-4': 'Perineural invasion [Presence] in Cancer specimen',
    '29593-1': 'Cells.Ki-67 nuclear Ag/cells in Tissue by Immune stain',
    '85069-3': 'Lab test method [Type]',
}



@pytest.mark.parametrize('code,field', [
    ('85319-2', 'ki67_proliferation_index'),
    ('85337-4', 'ki67_proliferation_index'),
    ('85337-4', 'test_methodology'),
    ('85337-4', 'oncotype_dx_score'),
    ('92837-4', 'lymph_node_status'),
])
@pytest.mark.parametrize('mapped', [False, True])
def test_wrong_codes_cannot_populate_fields_even_with_approved_recipe(code, field, mapped):
    person = PersonFactory()
    concept = ConceptFactory(concept_code=code, concept_name=LOINC_NAMES[code])
    mapping = FieldConceptMapping.objects.create(
        field_name=field, concept=concept, vocabulary_id='LOINC', concept_code=code,
        source_value=code, omop_table='measurement', value_kind='number', status='approved',
    )
    MeasurementFactory(
        person=person, measurement_concept_id=concept.pk if mapped else 0,
        measurement_source_value=code, value_as_number=17, value_as_string='Positive',
    )
    record = refresh_patient_record(person)
    assert getattr(record, field) is None
    entry = build_writable_field_descriptor()[field]
    assert (entry.get('projection') or {}).get('concept_id') != concept.pk
    mapping.refresh_from_db()
    assert mapping.status == 'approved'
    assert mapping.concept_id == concept.pk  # preserve the curator record for review
    assert Measurement.objects.filter(person=person).count() == 1


@pytest.mark.parametrize('code,field,value', [
    ('29593-1', 'ki67_proliferation_index', 0),
    ('29593-1', 'ki67_proliferation_index', 23),
    ('85069-3', 'test_methodology', 'IHC'),
])
def test_correct_loinc_selects_newest_mapped_or_unmapped_fact(code, field, value):
    person = PersonFactory()
    numeric = isinstance(value, int)
    MeasurementFactory(
        person=person, measurement_concept=ConceptFactory(concept_code=code),
        measurement_date=date(2024, 1, 1), value_as_number=91 if numeric else None,
        value_as_string=None if numeric else 'Old method',
    )
    newest = MeasurementFactory(
        person=person, measurement_concept_id=0, measurement_source_value=code,
        measurement_date=date(2024, 2, 1), value_as_number=value if numeric else None,
        value_as_string=None if numeric else value,
    )
    for _ in range(2):
        assert getattr(refresh_patient_record(person), field) == value
    # An explicit newer empty fact suppresses older results.
    Measurement.objects.filter(pk=newest.pk).update(value_as_number=None, value_as_string=None)
    assert getattr(refresh_patient_record(person), field) is None


def test_method_accepts_a_coded_answer_and_does_not_become_oncotype():
    person = PersonFactory()
    MeasurementFactory(
        person=person, measurement_concept=ConceptFactory(concept_code='85069-3'),
        value_as_concept=ConceptFactory(concept_name='IHC'), value_as_number=42,
    )
    record = refresh_patient_record(person)
    assert record.test_methodology == 'IHC'
    assert record.oncotype_dx_score is None


def test_conflicting_concept_cannot_masquerade_as_ki67_source():
    person = PersonFactory()
    MeasurementFactory(
        person=person, measurement_concept=ConceptFactory(concept_code='85319-2'),
        measurement_source_value='29593-1', value_as_number=23,
    )
    assert refresh_patient_record(person).ki67_proliferation_index is None


def test_refresh_preserves_pending_edit_including_explicit_clear():
    person = PersonFactory()
    MeasurementFactory(person=person, measurement_source_value='85337-4', value_as_number=42)
    record = PatientRecord.objects.get(person=person)
    PatientRecord.objects.filter(pk=record.pk).update(
        oncotype_dx_score=9, test_methodology=None,
        user_edited_fields=['oncotype_dx_score', 'test_methodology'],
    )
    record = refresh_patient_record(person)
    assert record.oncotype_dx_score == 9
    assert record.test_methodology is None
    assert 'oncotype_dx_score' in record.user_edited_fields


def test_receptor_results_and_tnbc_are_unchanged():
    person = PersonFactory()
    for code in ('16112-5', '16113-3', '48676-1'):
        MeasurementFactory(
            person=person, measurement_concept=ConceptFactory(concept_code=code),
            value_as_string='Negative',
        )
    record = refresh_patient_record(person)
    assert record.estrogen_receptor_status == record.progesterone_receptor_status == record.her2_status == 'Negative'
    assert record.tnbc_status is True


def test_impact_report_counts_exposure_without_writes():
    person = PersonFactory()
    record = PatientRecordFactory(person=person)
    MeasurementFactory(
        person=person, measurement_concept=ConceptFactory(concept_code='85319-2'), value_as_number=42,
    )
    # Model the old projection without running the corrected derivation.
    PatientRecord.objects.filter(pk=record.pk).update(ki67_proliferation_index=42)
    before = list(PatientRecord.objects.values())
    out = StringIO()
    call_command('report_breast_mapping_impact', stdout=out)
    report = json.loads(out.getvalue())
    field = report['fields']['ki67_proliferation_index']
    assert field['measurement_rows'] == field['exposed_records_with_stored_value'] == 1
    assert list(PatientRecord.objects.values()) == before
    assert 'person_id' not in out.getvalue()


@pytest.mark.parametrize('code,field', [
    ('29593-1', 'ki67_proliferation_index'), ('85069-3', 'test_methodology'),
])
@pytest.mark.parametrize('clear_marker', [False, True])
def test_newer_source_only_clear_suppresses_older_approved_mapping(code, field, clear_marker):
    from omop_core.services.omop_projection import CLEAR_VALUE
    person = PersonFactory()
    concept = ConceptFactory(concept_code=code, concept_name=LOINC_NAMES[code])
    numeric = field == 'ki67_proliferation_index'
    FieldConceptMapping.objects.create(
        field_name=field, concept=concept, vocabulary_id='LOINC', concept_code=code,
        source_value=code, omop_table='measurement', status='approved',
        value_kind='number' if numeric else 'string',
    )
    MeasurementFactory(
        person=person, measurement_concept=concept, measurement_source_value=code,
        measurement_date=date(2024, 1, 1), value_as_number=23 if numeric else None,
        value_as_string=None if numeric else 'IHC',
    )
    assert getattr(refresh_patient_record(person), field) == (23 if numeric else 'IHC')
    MeasurementFactory(
        person=person, measurement_concept_id=0, measurement_source_value=code,
        measurement_date=date(2024, 2, 1),
        value_source_value=CLEAR_VALUE if clear_marker else None,
    )
    for _ in range(2):
        assert getattr(refresh_patient_record(person), field) is None
    MeasurementFactory(
        person=person, measurement_concept_id=0, measurement_source_value=code,
        measurement_date=date(2024, 3, 1), value_as_number=0 if numeric else None,
        value_as_string=None if numeric else 'NGS',
    )
    assert getattr(refresh_patient_record(person), field) == (0 if numeric else 'NGS')


def test_reviewed_nodal_recipe_remains_available():
    person = PersonFactory()
    concept = ConceptFactory(concept_code='reviewed-nodal-question', vocabulary__vocabulary_id='LOCAL')
    FieldConceptMapping.objects.create(
        field_name='lymph_node_status', concept=concept, vocabulary_id='LOCAL',
        concept_code=concept.concept_code, source_value=concept.concept_code,
        omop_table='measurement', value_kind='string', status='approved',
    )
    MeasurementFactory(person=person, measurement_concept=concept,
                       measurement_source_value=concept.concept_code, value_as_string='Positive')
    assert refresh_patient_record(person).lymph_node_status == 'Positive'
    assert build_writable_field_descriptor()['lymph_node_status']['projection']['concept_id'] == concept.pk


@pytest.mark.parametrize('correct', [False, True])
def test_fhir_import_uses_question_codes_and_preserves_raw_results(correct, request):
    from rest_framework.test import APIClient
    from patient_portal.models import Identity
    from patient_portal.tests import _make_vocab_fixtures
    from omop_core.services.concept_cache import concept_cache_clear
    concept_cache_clear()
    request.addfinalizer(concept_cache_clear)
    _make_vocab_fixtures()
    for code, name in LOINC_NAMES.items():
        ConceptFactory(concept_code=code, concept_name=name)
    client = APIClient()
    client.force_authenticate(Identity.objects.create_superuser(email='mapping@example.test', password='test'))
    patient = {'resourceType': 'Patient', 'id': 'breast-code-test',
               'name': [{'family': 'BreastCodes', 'given': ['Test']}],
               'gender': 'female', 'birthDate': '1975-01-01'}
    specs = [('29593-1' if correct else '85319-2', 'Ki-67', {'valueQuantity': {'value': 0, 'unit': '%'}}),
             ('85069-3' if correct else '85337-4', 'Test Methodology', {'valueString': 'IHC'}),
             ('92837-4', 'Lymph node status', {'valueString': 'Positive'})]
    resources = [patient]
    for code, label, answer in specs:
        resources.append({'resourceType': 'Observation', 'id': 'obs-' + code,
                          'status': 'final', 'subject': {'reference': 'Patient/breast-code-test'},
                          'effectiveDateTime': '2024-01-15',
                          'code': {'coding': [{'system': 'http://loinc.org', 'code': code}], 'text': label},
                          **answer})
    bundle = {'resourceType': 'Bundle', 'type': 'collection',
              'entry': [{'resource': r} for r in resources]}
    payload = BytesIO(json.dumps(bundle).encode())
    payload.name = 'breast-codes.json'
    response = client.post('/api/v1/patient-records/upload_fhir/', {'file': payload}, format='multipart')
    assert response.status_code in (200, 201), response.data
    record = PatientRecord.objects.get(person__family_name='BreastCodes')
    assert record.ki67_proliferation_index == (0 if correct else None)
    assert record.test_methodology == ('IHC' if correct else None)
    assert record.oncotype_dx_score is None
    assert record.lymph_node_status is None
    assert Measurement.objects.filter(person=record.person).count() == 3
