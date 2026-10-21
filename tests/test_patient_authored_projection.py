"""A patient's own PatientRecord edits project as patient self-report (#1959).

The recipe's type describes the fact a clinician or import would record. When
the patient or their representative is the author, the fact is typed 32865 and
matches only facts of that type; anyone else's edit never matches a patient's
fact. The patient's own writes get PATIENT_SELF provenance, as the PHR entry
endpoints record.
"""
import pytest
from django.contrib.contenttypes.models import ContentType
from django.utils import timezone
from rest_framework.test import APIClient

from omop_core.models import FieldConceptMapping, Observation, PersonalRepresentative, ProvenanceRecord
from omop_core.services.omop_projection import PATIENT_SELF_REPORT_TYPE, project_single_value
from omop_core.signals import suppress_patient_record_refresh
from patient_portal.models import Identity, PatientUser
from tests.factories import ConceptFactory, ObservationFactory, PatientRecordFactory, VocabularyFactory

pytestmark = pytest.mark.django_db

EHR = 32817
SOURCE = 'planned-therapies'


@pytest.fixture
def record(settings):
    settings.CELERY_BROKER_URL = ''
    ConceptFactory(concept_id=EHR, concept_code='ehr-type')
    ConceptFactory(concept_id=PATIENT_SELF_REPORT_TYPE, concept_code='patient-report-type')
    return PatientRecordFactory()


@pytest.fixture
def concept(record):
    """An approved curated Observation recipe, which defaults to the EHR type."""
    concept = ConceptFactory(concept_code='planned-therapy',
                             vocabulary=VocabularyFactory(vocabulary_id='SNOMED'))
    FieldConceptMapping.objects.create(field_name='planned_therapies', concept=concept,
                                       omop_table='observation', source_value=SOURCE,
                                       value_kind='string', status='approved')
    return concept


def _recipe(concept):
    return {'omop_table': 'observation', 'concept_id': concept.pk, 'source_value': SOURCE,
            'value_kind': 'string'}


def _facts(record, concept):
    return Observation.objects.filter(person=record.person, observation_concept=concept)


def _patient_provenance(row):
    return ProvenanceRecord.objects.filter(content_type=ContentType.objects.get_for_model(Observation),
                                           object_id=row.pk, source='PATIENT_SELF')


def _patch(identity, url, value='R-CHOP next'):
    client = APIClient()
    client.force_authenticate(identity)
    response = client.patch(url, {'planned_therapies': value}, format='json')
    assert response.status_code == 200, response.data
    return response


# ---------------------------------------------------------------- the service


def test_patient_authored_fact_is_self_report_with_provenance(record, concept):
    assert project_single_value(record.person, 'planned_therapies', 'R-CHOP next', _recipe(concept),
                                type_concept_id=PATIENT_SELF_REPORT_TYPE, patient_user_id='urn:local|abc')
    row = _facts(record, concept).get()
    assert row.observation_type_concept_id == PATIENT_SELF_REPORT_TYPE
    assert row.value_as_string == 'R-CHOP next'
    assert list(_patient_provenance(row).values_list('source_user_id', flat=True)) == ['urn:local|abc']


def test_patient_answer_sits_beside_a_same_day_clinical_fact(record, concept):
    with suppress_patient_record_refresh():
        clinical = ObservationFactory(person=record.person, observation_concept=concept,
                                      observation_source_value=SOURCE, observation_date=timezone.localdate(),
                                      observation_type_concept_id=EHR, value_as_string='watch and wait')
    project_single_value(record.person, 'planned_therapies', 'R-CHOP next', _recipe(concept),
                         type_concept_id=PATIENT_SELF_REPORT_TYPE, patient_user_id='urn:local|abc')
    clinical.refresh_from_db()
    assert clinical.value_as_string == 'watch and wait'
    assert clinical.observation_type_concept_id == EHR
    assert not _patient_provenance(clinical).exists()
    patient = _facts(record, concept).exclude(pk=clinical.pk).get()
    assert (patient.observation_type_concept_id, patient.value_as_string) == (PATIENT_SELF_REPORT_TYPE,
                                                                              'R-CHOP next')


def test_a_second_patient_edit_today_reuses_their_own_fact(record, concept):
    for value in ('R-CHOP next', 'BR next'):
        project_single_value(record.person, 'planned_therapies', value, _recipe(concept),
                             type_concept_id=PATIENT_SELF_REPORT_TYPE, patient_user_id='urn:local|abc')
    row = _facts(record, concept).get()
    assert row.value_as_string == 'BR next'
    assert _patient_provenance(row).count() == 1


def test_without_an_author_type_the_recipe_type_and_matching_are_unchanged(record, concept):
    with suppress_patient_record_refresh():
        clinical = ObservationFactory(person=record.person, observation_concept=concept,
                                      observation_source_value=SOURCE, observation_date=timezone.localdate(),
                                      observation_type_concept_id=EHR, value_as_string='watch and wait')
    project_single_value(record.person, 'planned_therapies', 'R-CHOP next', _recipe(concept))
    clinical.refresh_from_db()
    assert _facts(record, concept).count() == 1
    assert clinical.value_as_string == 'R-CHOP next'
    assert clinical.observation_type_concept_id == EHR


# ---------------------------------------------------------------- the PATCH


def test_patient_patching_their_own_record_writes_self_report(record, concept):
    identity = Identity.objects.create_user(email='member@example.test')
    PatientUser.objects.create(identity=identity, person=record.person)
    _patch(identity, '/api/patient-info/me/')
    row = _facts(record, concept).get()
    assert row.observation_type_concept_id == PATIENT_SELF_REPORT_TYPE
    assert list(_patient_provenance(row).values_list('source_user_id', flat=True)) == [
        f'{identity.issuer}|{identity.sub}']
    record.refresh_from_db()
    assert 'planned_therapies' not in (record.user_edited_fields or [])


def test_representative_writes_self_report_without_patient_provenance(record, concept):
    carer = Identity.objects.create_user(email='carer@example.test')
    PersonalRepresentative.objects.create(representative=carer, person_id=record.person_id,
                                          relationship='caregiver', verification_status='VERIFIED')
    _patch(carer, f'/api/v1/patient-records/{record.person_id}/')
    row = _facts(record, concept).get()
    assert row.observation_type_concept_id == PATIENT_SELF_REPORT_TYPE
    assert not _patient_provenance(row).exists()


def test_staff_edit_keeps_the_recipe_type(record, concept):
    staff = Identity.objects.create_user(email='staff@example.test', is_staff=True)
    _patch(staff, f'/api/v1/patient-records/{record.person_id}/')
    row = _facts(record, concept).get()
    assert row.observation_type_concept_id == EHR
    assert not _patient_provenance(row).exists()


def test_without_the_self_report_concept_the_patient_edit_still_saves(settings):
    settings.CELERY_BROKER_URL = ''
    ConceptFactory(concept_id=EHR, concept_code='ehr-type')
    record = PatientRecordFactory()
    concept = ConceptFactory(concept_code='planned-therapy', vocabulary=VocabularyFactory(vocabulary_id='SNOMED'))
    FieldConceptMapping.objects.create(field_name='planned_therapies', concept=concept, omop_table='observation',
                                       source_value=SOURCE, value_kind='string', status='approved')
    identity = Identity.objects.create_user(email='member-no-vocab@example.test')
    PatientUser.objects.create(identity=identity, person=record.person)
    _patch(identity, '/api/patient-info/me/')
    assert _facts(record, concept).get().observation_type_concept_id == EHR


def test_a_later_clinical_edit_today_does_not_take_over_the_patients_fact(record, concept):
    project_single_value(record.person, 'planned_therapies', 'R-CHOP next', _recipe(concept),
                         type_concept_id=PATIENT_SELF_REPORT_TYPE, patient_user_id='urn:local|abc')
    project_single_value(record.person, 'planned_therapies', 'watch and wait', _recipe(concept))
    patient = _facts(record, concept).get(observation_type_concept_id=PATIENT_SELF_REPORT_TYPE)
    clinical = _facts(record, concept).get(observation_type_concept_id=EHR)
    assert patient.value_as_string == 'R-CHOP next'
    assert clinical.value_as_string == 'watch and wait'
    assert _patient_provenance(patient).exists() and not _patient_provenance(clinical).exists()


def test_patient_editing_their_representatives_fact_today_records_their_provenance(record, concept):
    project_single_value(record.person, 'planned_therapies', 'R-CHOP next', _recipe(concept),
                         type_concept_id=PATIENT_SELF_REPORT_TYPE)
    project_single_value(record.person, 'planned_therapies', 'BR next', _recipe(concept),
                         type_concept_id=PATIENT_SELF_REPORT_TYPE, patient_user_id='urn:local|abc')
    row = _facts(record, concept).get()
    assert row.value_as_string == 'BR next'
    assert list(_patient_provenance(row).values_list('source_user_id', flat=True)) == ['urn:local|abc']
