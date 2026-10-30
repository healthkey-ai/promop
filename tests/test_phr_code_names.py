"""/api/v1/phr/ — an unmapped medication or procedure shows a name, not its code.

Imports leave a row unmapped (concept 0) with only its code as the source
value: RxNorm "2762" for a medicine, CPT "85025" for a procedure. The record
names it from the vocabularies we hold, as the code-mapping queue does
(``describe_source_code``), and shows the source value only when nothing knows
it.
"""
import pytest

from omop_core.models import ProcedureOccurrence
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
    concept('RxNorm', '2762', 'clonazepam')
    unmapped_drug(record, '2762')

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


def test_an_unmapped_procedure_coded_in_cpt_or_hcpcs_shows_its_name(record):
    concept('CPT4', '85025', 'Blood count; complete (CBC), automated and automated differential WBC count')
    concept('HCPCS', 'J9041', 'Injection, bortezomib, 0.1 mg')
    unmapped_procedure(record, 1, '85025', day='2025-10-24')
    unmapped_procedure(record, 2, 'J9041', day='2025-10-20')
    unmapped_procedure(record, 3, 'Port flush', day='2025-10-10')

    items = signed_in(record).get('/api/v1/phr/procedures/').data['procedures']

    assert [p['name'] for p in items] == [
        'Blood count; complete (CBC), automated and automated differential WBC count',
        'Injection, bortezomib, 0.1 mg',
        'Port flush',
    ]
