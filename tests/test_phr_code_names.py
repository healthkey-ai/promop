"""/api/v1/phr/ — an unmapped medication or procedure shows a name, not its code.

Imports leave a row unmapped (concept 0) with only its code as the source
value: RxNorm "2762" for a medicine, CPT "85025" for a procedure. The record
names it from the vocabularies we hold, as the code-mapping queue does
(``describe_source_code``), and shows the source value only when nothing knows
it.
"""
import pytest

from omop_core.models import PatientSourceCode, ProcedureOccurrence, SourceCodeConceptMapping
from omop_core.signals import suppress_patient_record_refresh
from patient_portal.models import Identity, PatientUser
from rest_framework.test import APIClient
from tests.factories import ConceptFactory, DrugExposureFactory, PatientRecordFactory, VocabularyFactory

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _inline_derivation(settings):
    settings.CELERY_BROKER_URL = ''


@pytest.fixture
def record():
    record = PatientRecordFactory(disease='')
    ConceptFactory(concept_id=0, concept_code='No matching concept', concept_name='No matching concept')
    return record


def signed_in(record):
    identity = Identity.objects.create_user(email=f'p{record.person_id}@example.test')
    PatientUser.objects.create(identity=identity, person=record.person)
    client = APIClient()
    client.force_authenticate(identity)
    return client


def concept(vocabulary_id, code, name):
    return ConceptFactory(
        concept_code=code, concept_name=name,
        vocabulary=VocabularyFactory(vocabulary_id=vocabulary_id, vocabulary_name=vocabulary_id),
    )


def unmapped_drug(record, source_value, start='2025-12-03', **kwargs):
    with suppress_patient_record_refresh():
        return DrugExposureFactory(person=record.person, drug_concept_id=0, drug_source_value=source_value,
                                   drug_exposure_start_date=start, drug_exposure_end_date=None, **kwargs)


def unmapped_procedure(record, pk, source_value, day='2025-10-24'):
    kind = ConceptFactory(concept_name='Procedure type', concept_code=f'TYPE-PROC-{pk}')
    return ProcedureOccurrence.objects.create(
        procedure_occurrence_id=pk, person=record.person, procedure_concept_id=0,
        procedure_source_value=source_value, procedure_date=day, procedure_type_concept=kind,
    )


def test_an_unmapped_medicine_coded_in_rxnorm_shows_the_rxnorm_name(record):
    source = concept('RxNorm', '2762', 'clonazepam')
    unmapped_drug(record, '2762', drug_source_concept=source)

    meds = signed_in(record).get('/api/v1/phr/medications/').data['medications']

    assert [m['name'] for m in meds] == ['clonazepam']


def test_an_unmapped_medicine_with_a_source_concept_shows_that_concept(record):
    source = concept('RxNorm', '197397', 'furosemide 20 MG Oral Tablet')
    unmapped_drug(record, '197397', drug_source_concept=source)

    meds = signed_in(record).get('/api/v1/phr/medications/').data['medications']

    assert [m['name'] for m in meds] == ['furosemide 20 MG Oral Tablet']


def test_text_and_unknown_codes_are_shown_as_recorded(record):
    concept('CPT4', '2762', 'not a medicine')  # a code in another vocabulary is not this drug
    unmapped_drug(record, 'Vitamin D3 / 2000 IU', start='2025-12-01')
    unmapped_drug(record, '999999', start='2025-11-01')
    unmapped_drug(record, '2762', start='2025-10-01')

    meds = signed_in(record).get('/api/v1/phr/medications/').data['medications']

    assert [m['name'] for m in meds] == ['Vitamin D3 / 2000 IU', '999999', '2762']


def test_an_unmapped_medicine_recorded_as_an_hcpcs_j_code_shows_its_name(record):
    """#2300: a drug administered arrives as a J-code, often filed under no table."""
    concept('HCPCS', 'J9041', 'Injection, bortezomib, 0.1 mg')
    unmapped_drug(record, 'J9041')
    recorded(record, 'J9041', 'urn:oid:2.16.840.1.113883.6.14', '')

    meds = signed_in(record).get('/api/v1/phr/medications/').data['medications']

    assert [m['name'] for m in meds] == ['Injection, bortezomib, 0.1 mg']


def test_an_unmapped_procedure_coded_in_cpt_or_hcpcs_shows_its_name(record):
    cpt = concept('CPT4', '85025', 'Blood count; complete (CBC), automated and automated differential WBC count')
    hcpcs = concept('HCPCS', 'J9041', 'Injection, bortezomib, 0.1 mg')
    first = unmapped_procedure(record, 1, '85025', day='2025-10-24')
    second = unmapped_procedure(record, 2, 'J9041', day='2025-10-20')
    first.procedure_source_concept = cpt
    second.procedure_source_concept = hcpcs
    first.save(update_fields=['procedure_source_concept'])
    second.save(update_fields=['procedure_source_concept'])
    unmapped_procedure(record, 3, 'Port flush', day='2025-10-10')

    items = signed_in(record).get('/api/v1/phr/procedures/').data['procedures']

    assert [p['name'] for p in items] == [
        'Blood count; complete (CBC), automated and automated differential WBC count',
        'Injection, bortezomib, 0.1 mg',
        'Port flush',
    ]


# The row does not say which system its code is from; the patient's source
# codes do (#2168).

CPT_OID = 'urn:oid:2.16.840.1.113883.6.12'
EPIC_PROCEDURES = 'urn:oid:1.2.840.114350.1.13.90.2.7.2.696580'
EPIC_LAB = 'urn:oid:1.2.840.114350.1.13.90.2.7.5.737384.26'


def recorded(record, code, system, table):
    return PatientSourceCode.objects.create(person=record.person, source_value=code,
                                            source_vocabulary_id=system, omop_table=table)


def mapped_name(system, code, name, table='procedure'):
    return SourceCodeConceptMapping.objects.create(source_vocabulary_id=system, source_code=code,
                                                   omop_table=table, source_code_description=name)


def test_a_code_no_vocabulary_holds_is_named_by_its_mapping_row(record):
    # CPT is licensed and not loaded; the import sent the name with the code.
    recorded(record, '85025', CPT_OID, '')
    mapped_name('CPT4', '85025', 'CBC with differential')
    recorded(record, '23521', EPIC_PROCEDURES, 'procedure_occurrence')
    mapped_name(EPIC_PROCEDURES, '23521', 'RBC product request')
    unmapped_procedure(record, 1, '85025', day='2025-10-24')
    unmapped_procedure(record, 2, '23521', day='2025-10-20')

    items = signed_in(record).get('/api/v1/phr/procedures/').data['procedures']

    assert [p['name'] for p in items] == ['CBC with differential', 'RBC product request']


def test_a_code_recorded_in_another_system_is_never_named_by_a_guessed_one(record):
    concept('CPT4', '23521', 'Closed treatment of sternoclavicular dislocation')
    recorded(record, '23521', EPIC_PROCEDURES, 'procedure_occurrence')
    unmapped_procedure(record, 1, '23521')

    items = signed_in(record).get('/api/v1/phr/procedures/').data['procedures']

    assert [p['name'] for p in items] == ['23521']


def test_a_lab_component_with_the_same_code_does_not_name_a_procedure(record):
    concept('CPT4', '80053', 'Comprehensive metabolic panel')
    recorded(record, '80053', EPIC_LAB, 'measurement')
    mapped_name(EPIC_LAB, '80053', 'potassium', table='measurement')
    unmapped_procedure(record, 1, '80053')

    items = signed_in(record).get('/api/v1/phr/procedures/').data['procedures']

    assert [p['name'] for p in items] == ['Comprehensive metabolic panel']


def test_the_table_s_own_system_beats_a_hospital_code_filed_under_no_table(record):
    # Staging's shape for 16813: CPT and two Epic lab component ids, all with no table.
    concept('CPT4', '80053', 'Comprehensive metabolic panel')
    recorded(record, '80053', EPIC_LAB, '')
    recorded(record, '80053', CPT_OID, '')
    mapped_name(EPIC_LAB, '80053', 'potassium', table='measurement')
    unmapped_procedure(record, 1, '80053')

    items = signed_in(record).get('/api/v1/phr/procedures/').data['procedures']

    assert [p['name'] for p in items] == ['Comprehensive metabolic panel']


def test_a_hospital_procedure_id_filed_under_no_table_is_never_named_by_a_guessed_system(record):
    concept('CPT4', '23521', 'Closed treatment of sternoclavicular dislocation')
    concept('HCPCS', '66196', 'not this procedure either')
    recorded(record, '23521', EPIC_PROCEDURES, '')
    mapped_name(EPIC_PROCEDURES, '23521', 'RBC product request')
    recorded(record, '66196', EPIC_PROCEDURES, '')
    unmapped_procedure(record, 1, '23521', day='2025-10-24')
    unmapped_procedure(record, 2, '66196', day='2025-10-20')

    items = signed_in(record).get('/api/v1/phr/procedures/').data['procedures']

    assert [p['name'] for p in items] == ['RBC product request', '66196']


def test_a_vaccine_is_named_in_cvx(record):
    concept('CVX', '48', 'Hib (PRP-T)')
    concept('RxNorm', '48', 'not this vaccine')
    unmapped_drug(record, '48', route_source_value='VACCINE')

    meds = signed_in(record).get('/api/v1/phr/medications/').data['medications']

    assert [m['name'] for m in meds] == ['Hib (PRP-T)']


def test_a_vaccine_code_recorded_in_cvx_is_named_from_its_mapping_row(record):
    recorded(record, '133', 'CVX', 'drug_exposure')
    mapped_name('CVX', '133', 'pneumococcal conjugate vaccine, 13 valent', table='drug_exposure')
    unmapped_drug(record, '133')

    meds = signed_in(record).get('/api/v1/phr/medications/').data['medications']

    assert [m['name'] for m in meds] == ['pneumococcal conjugate vaccine, 13 valent']


# #2349: the name is often only on the patient's own source code row, in what
# the ETL sent with it (``source_metadata``).

MEDI_SPAN = 'urn:oid:2.16.840.1.113883.6.253'


def recorded_with(record, code, system, table, **metadata):
    return PatientSourceCode.objects.create(person=record.person, source_value=code, source_vocabulary_id=system,
                                            omop_table=table, source_metadata=metadata)


def test_a_hospital_procedure_id_is_named_by_the_patient_s_source_code_row(record):
    recorded_with(record, '18417', EPIC_PROCEDURES, '', text=['KAPPA LAMBDA QUANT FLC'], display=['KAPPA/LAMBDA FLC'])
    recorded(record, '18417', EPIC_PROCEDURES, 'procedure_occurrence')
    unmapped_procedure(record, 1, '18417')

    items = signed_in(record).get('/api/v1/phr/procedures/').data['procedures']

    assert [p['name'] for p in items] == ['KAPPA LAMBDA QUANT FLC']


def test_a_mapping_row_s_name_beats_the_patient_s_source_code_row(record):
    recorded_with(record, '23521', EPIC_PROCEDURES, 'procedure_occurrence', text=['RBC REQ'])
    mapped_name(EPIC_PROCEDURES, '23521', 'RBC product request')
    unmapped_procedure(record, 1, '23521')

    items = signed_in(record).get('/api/v1/phr/procedures/').data['procedures']

    assert [p['name'] for p in items] == ['RBC product request']


def test_a_medicine_filed_under_no_table_is_named_in_the_system_the_patient_files_medicines_in(record):
    concept('RxNorm', '33014', 'not this medicine')
    recorded_with(record, '33014', MEDI_SPAN, '', text=['potassium chloride'])
    recorded(record, '82160', MEDI_SPAN, 'drug_exposure')
    unmapped_drug(record, '33014')

    meds = signed_in(record).get('/api/v1/phr/medications/').data['medications']

    assert [m['name'] for m in meds] == ['potassium chloride']


def test_the_row_filed_under_the_table_names_the_code_first(record):
    recorded_with(record, '82160', MEDI_SPAN, '', text=['potassium chloride 20 MEQ tablet'])
    recorded_with(record, '82160', MEDI_SPAN, 'drug_exposure', text=['potassium chloride (K-DUR) 20 MEQ tablet'])
    unmapped_drug(record, '82160')

    meds = signed_in(record).get('/api/v1/phr/medications/').data['medications']

    assert [m['name'] for m in meds] == ['potassium chloride (K-DUR) 20 MEQ tablet']


def test_a_source_code_row_in_another_system_does_not_name_the_code(record):
    recorded_with(record, '23521', EPIC_LAB, 'measurement', text=['potassium'])
    recorded(record, '23521', EPIC_PROCEDURES, 'procedure_occurrence')
    unmapped_procedure(record, 1, '23521')

    items = signed_in(record).get('/api/v1/phr/procedures/').data['procedures']

    assert [p['name'] for p in items] == ['23521']


def test_a_medicine_whose_source_row_holds_several_names_stays_a_code(record):
    recorded_with(record, '27472', MEDI_SPAN, 'drug_exposure', text=['fluconazole', 'fluconazole 200 mg tablet'])
    unmapped_drug(record, '27472')

    meds = signed_in(record).get('/api/v1/phr/medications/').data['medications']

    assert [m['name'] for m in meds] == ['27472']


def test_names_differing_only_in_case_are_one_name(record):
    recorded_with(record, '177807', MEDI_SPAN, 'drug_exposure', text=['Pomalyst 4 mg capsule', 'Pomalyst 4 MG capsule'])
    unmapped_drug(record, '177807')

    meds = signed_in(record).get('/api/v1/phr/medications/').data['medications']

    assert [m['name'] for m in meds] == ['Pomalyst 4 mg capsule']
