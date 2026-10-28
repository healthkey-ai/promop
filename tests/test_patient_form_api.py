"""The standalone v1 API and the embedded host use the same form writer."""
import pytest
from django.utils import timezone
from rest_framework.test import APIClient
from omop_core.models import (
    ConditionOccurrence, Measurement, Observation, PersonalRepresentative,
    ProvenanceRecord,
)
from patient_portal.models import Identity, PatientUser
from tests.factories import (
    ConceptFactory, DomainFactory, ObservationFactory, PatientRecordFactory,
    VocabularyFactory,
)

pytestmark = pytest.mark.django_db


def _client():
    client = APIClient()
    client.force_authenticate(Identity.objects.create_user(email='form-editor@example.test', is_staff=True))
    return client


def test_shared_form_api_writes_bmi_inputs_and_returns_server_computed_projection():
    record = PatientRecordFactory()
    vocab = VocabularyFactory(vocabulary_id='LOINC')
    for concept_id, code, title in [(990001, '29463-7', 'Body weight'), (990002, '8302-2', 'Body height')]:
        ConceptFactory(concept_id=concept_id, concept_code=code, concept_name=title, vocabulary=vocab)
    ConceptFactory(concept_id=32865, vocabulary=VocabularyFactory(vocabulary_id='Type Concept'))
    client = _client()
    response = client.patch(f'/api/v1/patient-records/{record.person_id}/',
                            {'height': 200, 'weight': 80}, format='json', HTTP_X_PATIENT_FORM='1')
    assert response.status_code == 200, response.data
    assert response.data['patient_info']['bmi'] == 20
    assert set(response.data['write']['applied']) == {'height', 'weight'}
    assert Measurement.objects.filter(person=record.person, measurement_concept_id__in=[990001, 990002]).count() == 2
    descriptor = client.get('/api/v1/patient-records/writable-fields/', {'person_id': record.person_id},
                            HTTP_X_PATIENT_FORM='1').data
    assert descriptor['bmi']['inputs'] == ['weight', 'height']
    assert descriptor['bmi']['writable'] is False


def test_readonly_form_payload_is_rejected_instead_of_a_silent_200():
    record = PatientRecordFactory()
    response = _client().patch(f'/api/v1/patient-records/{record.person_id}/',
                               {'bmi': 50}, format='json', HTTP_X_PATIENT_FORM='1')
    assert response.status_code == 400, response.data
    assert 'bmi' in response.data['rejected']


def test_complex_verdict_gate_withholds_the_result_but_not_its_source_facts():
    """SLiM/CRAB/MIPI rows are computed: the verdict is not authorable, while the
    clinical facts it is derived from stay ordinary writable inputs."""
    record = PatientRecordFactory()
    client = _client()
    descriptor = client.get('/api/v1/patient-records/writable-fields/', {'person_id': record.person_id},
                            HTTP_X_PATIENT_FORM='1').data
    for verdict in ('meets_crab', 'meets_slim', 'mipi_risk', 'mipi_c_risk', 'high_risk_mcl_criteria'):
        assert descriptor[verdict]['writable'] is False, verdict
        assert descriptor[verdict].get('compatibility_scope') == 'result', verdict
    assert descriptor['hemoglobin_g_dl']['writable'] is True
    assert descriptor['serum_calcium_mg_dl']['writable'] is True

    rejected = client.patch(f'/api/v1/patient-records/{record.person_id}/',
                            {'meets_crab': True}, format='json', HTTP_X_PATIENT_FORM='1')
    assert rejected.status_code == 400, rejected.data
    assert 'meets_crab' in rejected.data['rejected']

    applied = client.patch(f'/api/v1/patient-records/{record.person_id}/',
                           {'hemoglobin_g_dl': 8.5}, format='json', HTTP_X_PATIENT_FORM='1')
    assert applied.status_code == 200, applied.data
    assert 'hemoglobin_g_dl' in applied.data['write']['applied']
    assert 'meets_crab' not in applied.data['write']['applied']


def test_defining_events_follow_their_edited_inputs():
    """The computed verdict is derived again from the facts the form edited."""
    record = PatientRecordFactory()
    client = _client()
    url = f'/api/v1/patient-records/{record.person_id}/'

    response = client.patch(url, {'hemoglobin_g_dl': 8.5}, format='json', HTTP_X_PATIENT_FORM='1')
    assert response.status_code == 200, response.data
    assert response.data['patient_info']['meets_crab'] is True

    response = client.patch(url, {
        'hemoglobin_g_dl': 13.5, 'serum_calcium_mg_dl': 9.5,
        'creatinine_clearance_ml_min': 90, 'bone_lesions': 'Absent',
    }, format='json', HTTP_X_PATIENT_FORM='1')
    assert response.status_code == 200, response.data
    assert response.data['patient_info']['meets_crab'] is False

    # Serum creatinine is the recorded alternative when no clearance exists.
    response = client.patch(url, {
        'creatinine_clearance_ml_min': None, 'serum_creatinine_mg_dl': 2.4,
    }, format='json', HTTP_X_PATIENT_FORM='1')
    assert response.status_code == 200, response.data
    assert response.data['patient_info']['meets_crab'] is True

    response = client.patch(url, {'clonal_plasma_cells': 70}, format='json', HTTP_X_PATIENT_FORM='1')
    assert response.status_code == 200, response.data
    assert response.data['patient_info']['meets_slim'] is True

    response = client.patch(url, {'clonal_plasma_cells': 10, 'kappa_flc': 300, 'lambda_flc': 2},
                            format='json', HTTP_X_PATIENT_FORM='1')
    assert response.status_code == 200, response.data
    # Ratio >= 100 with an involved chain >= 100 mg/L is the SLiM light-chain
    # criterion; a low plasma-cell count alone does not establish it.
    assert response.data['patient_info']['meets_slim'] is True


def test_incomplete_defining_event_inputs_stay_unknown():
    record = PatientRecordFactory()
    client = _client()
    url = f'/api/v1/patient-records/{record.person_id}/'
    response = client.patch(url, {'hemoglobin_g_dl': 13.5}, format='json', HTTP_X_PATIENT_FORM='1')
    assert response.status_code == 200, response.data
    assert response.data['patient_info']['meets_crab'] is None
    # A low plasma-cell count does not establish SLiM while the light-chain
    # criterion has no recorded pair.
    response = client.patch(url, {'clonal_plasma_cells': 10}, format='json', HTTP_X_PATIENT_FORM='1')
    assert response.status_code == 200, response.data
    assert response.data['patient_info']['meets_slim'] is None


def test_shared_form_preserves_profile_phone_and_name_workflows():
    record = PatientRecordFactory()
    response = _client().patch(f'/api/v1/patient-records/{record.person_id}/',
        {'phone_number': '+15551234567', 'patient_name': 'Synthetic Patient'}, format='json', HTTP_X_PATIENT_FORM='1')
    assert response.status_code == 200, response.data
    record.person.refresh_from_db()
    assert record.person.phone_number == '+15551234567'
    assert record.person.given_name == 'Synthetic'
    assert record.person.family_name == 'Patient'
    assert response.data['patient_name'] == 'Synthetic Patient'


def test_direct_edits_survive_external_refresh_and_can_transition_to_omop():
    from omop_core.services.patient_record_service import refresh_patient_record
    record = PatientRecordFactory()
    client = _client()
    url = f'/api/v1/patient-records/{record.person_id}/'
    response = client.patch(url, {'weight': 80, 'height': 200}, format='json', HTTP_X_PATIENT_FORM='1')
    assert response.status_code == 200, response.data
    record = refresh_patient_record(record.person)
    assert record.weight == 80
    assert record.height == 200
    assert {'height', 'weight'} <= set(record.user_edited_fields)
    ConceptFactory(concept_id=990001, concept_code='29463-7', vocabulary=VocabularyFactory(vocabulary_id='LOINC'))
    ConceptFactory(concept_id=32865, vocabulary=VocabularyFactory(vocabulary_id='Type Concept'))
    response = client.patch(url, {'weight': 100}, format='json', HTTP_X_PATIENT_FORM='1')
    assert response.status_code == 200, response.data
    record = refresh_patient_record(record.person)
    assert record.weight == 100
    assert 'weight' not in record.user_edited_fields
    assert 'height' in record.user_edited_fields


def test_clear_dob_clears_both_person_and_record_age():
    record = PatientRecordFactory()
    client = _client()
    url = f'/api/v1/patient-records/{record.person_id}/'
    response = client.patch(url, {'date_of_birth': '1980-01-02'}, format='json', HTTP_X_PATIENT_FORM='1')
    assert response.status_code == 200, response.data
    response = client.patch(url, {'date_of_birth': None}, format='json', HTTP_X_PATIENT_FORM='1')
    assert response.status_code == 200, response.data
    record.refresh_from_db()
    record.person.refresh_from_db()
    assert record.person.year_of_birth is None
    assert record.date_of_birth is None
    assert record.patient_age is None


def test_long_projected_clinical_string_is_rejected_without_truncation():
    from omop_core.services.patient_form import apply_patient_form_patch
    from omop_core.models import Observation
    record = PatientRecordFactory()
    # An explicitly supplied recipe isolates the writer contract from curator
    # seed data. No mapping is added or approved by this regression test.
    recipe = {'progression': {'kind': 'direct', 'writable': True, 'target': 'patient_record',
                             'value_kind': 'string', 'projection': {'omop_table': 'observation', 'concept_id': 1}}}
    with pytest.raises(ValueError, match='at most 60 characters'):
        apply_patient_form_patch(record.person, {'progression': 'a' * 61}, descriptor=recipe)
    assert not Observation.objects.filter(person=record.person).exists()


@pytest.mark.parametrize('table', ['condition', 'procedure', 'drug_exposure'])
def test_unsupported_projection_is_rejected_without_a_measurement(table):
    from omop_core.services.patient_form import apply_patient_form_patch
    record = PatientRecordFactory()
    recipe = {'progression': {'kind': 'direct', 'writable': True, 'target': 'patient_record',
                             'value_kind': 'string', 'projection': {'omop_table': table, 'concept_id': 1}}}
    _, result = apply_patient_form_patch(record.person, {'progression': 'yes'}, descriptor=recipe)
    assert result.applied == []
    assert 'progression' in result.rejected
    assert not Measurement.objects.filter(person=record.person).exists()


def test_legacy_descriptor_is_not_narrowed_by_shared_form_restrictions():
    from omop_core.services.write_descriptor import build_writable_field_descriptor
    record = PatientRecordFactory()
    client = _client()
    url = '/api/v1/patient-records/writable-fields/'
    legacy = client.get(url, {'person_id': record.person_id}).data
    assert legacy == build_writable_field_descriptor()
    shared = client.get(url, {'person_id': record.person_id}, HTTP_X_PATIENT_FORM='1').data
    assert not shared['relapse_count']['writable']
    # Derivation owns these columns on both surfaces: a direct write would be
    # overwritten by the next save and would hide the facts that drive them.
    expected_inputs = {
        'measurable_disease_imwg': [
            'monoclonal_protein_serum', 'monoclonal_protein_urine',
            'kappa_flc', 'lambda_flc',
        ],
        'measurable_disease_iwcll': [
            'alc_thousand_per_ul', 'largest_lymph_node_size',
            'splenomegaly', 'hepatomegaly',
        ],
        'metastatic_status': ['distant_metastasis_stage'],
        'renal_adequacy_status': ['date_of_birth', 'gender', 'serum_creatinine_mg_dl',
                                  'creatinine_clearance_ml_min'],
        'no_other_active_malignancies': ['active_malignancies'],
        'patient_age': ['date_of_birth'],
    }
    for name, inputs in expected_inputs.items():
        assert legacy[name]['kind'] == 'computed', name
        assert legacy[name]['writable'] is False, name
        assert shared[name]['inputs'] == inputs, name


def test_noncanonical_body_units_are_not_silently_relabelled_or_written():
    record = PatientRecordFactory(height=70, height_units='in', weight=180, weight_units='lb')
    client = _client()
    descriptor = client.get('/api/v1/patient-records/writable-fields/', {'person_id': record.person_id},
                            HTTP_X_PATIENT_FORM='1').data
    assert descriptor['height']['unit'] == 'in'
    assert descriptor['weight']['unit'] == 'lb'
    assert not descriptor['height']['writable']
    assert not descriptor['weight']['writable']
    response = client.patch(f'/api/v1/patient-records/{record.person_id}/',
                             {'height': 72, 'weight': 190}, format='json', HTTP_X_PATIENT_FORM='1')
    assert response.status_code == 400, response.data
    record.refresh_from_db()
    assert record.height == 70
    assert record.weight == 180


def test_location_uses_its_own_limit_not_the_scalar_fact_limit():
    from omop_core.models import Location
    record = PatientRecordFactory()
    country = 'a' * 70  # Location.country allows 100, not the OMOP value string's 60.
    response = _client().patch(f'/api/v1/patient-records/{record.person_id}/',
        {'country': country, 'planned_therapies': 'VRd'}, format='json', HTTP_X_PATIENT_FORM='1')
    assert response.status_code == 200, response.data
    assert set(response.data['write']['applied']) == {'country', 'planned_therapies'}
    record.refresh_from_db()
    record.person.refresh_from_db()
    assert Location.objects.get(pk=record.person.location_id).country == country
    assert record.planned_therapies == 'VRd'


MM_FIELDS = {
    'mgus': ('mgus', 4149022, '35601003', 'Observation'),
    'smoldering_multiple_myeloma': ('smolderingMultipleMyeloma', 4184985, '413587002', 'Condition'),
    'waldenstrom_macroglobulinemia': ('waldenstromMacroglobulinemia', 4098597, '190818004', 'Condition'),
    'igm_multiple_myeloma': ('igmMultipleMyeloma', None, None, None),
    'al_amyloidosis': ('alAmyloidosis', 4047607, '23132008', 'Condition'),
    'poems_syndrome': ('poemsSyndrome', 4193776, '79268002', 'Condition'),
}


def _seed_mm_vocab(*, ehr_type=True, self_report_type=True):
    VocabularyFactory(vocabulary_id='SNOMED')
    type_vocab = VocabularyFactory(vocabulary_id='Type Concept')
    if ehr_type:
        ConceptFactory(concept_id=32817, concept_code='32817', vocabulary=type_vocab)
    if self_report_type:
        ConceptFactory(concept_id=32865, concept_code='32865', vocabulary=type_vocab)
    for _binding, concept_id, code, domain in MM_FIELDS.values():
        if concept_id is None:
            continue
        ConceptFactory(
            concept_id=concept_id, concept_code=code, concept_name=code,
            vocabulary=VocabularyFactory(vocabulary_id='SNOMED'),
            domain=DomainFactory(domain_id=domain),
        )


def test_shared_form_api_advertises_the_six_current_diagnosis_fields():
    record = PatientRecordFactory()
    _seed_mm_vocab()

    descriptor = _client().get(
        '/api/v1/patient-records/writable-fields/', {'person_id': record.person_id},
        HTTP_X_PATIENT_FORM='1',
    ).data

    for field, (binding, _concept_id, _code, _domain) in MM_FIELDS.items():
        entry = descriptor[field]
        assert entry['writable'] is True, (field, entry.get('reason'))
        assert entry['control'] == 'boolean'
        assert entry['tri_state'] is True
        assert binding in entry['bindings']


def test_shared_form_api_saves_and_projects_a_condition_field():
    record = PatientRecordFactory()
    _seed_mm_vocab()

    response = _client().patch(
        f'/api/v1/patient-records/{record.person_id}/',
        {'smoldering_multiple_myeloma': True}, format='json', HTTP_X_PATIENT_FORM='1',
    )

    assert response.status_code == 200, response.data
    assert 'smoldering_multiple_myeloma' in response.data['write']['applied']
    record.refresh_from_db()
    assert record.smoldering_multiple_myeloma is True
    assert ConditionOccurrence.objects.filter(
        person=record.person, condition_concept_id=4184985,
    ).count() == 1


def test_patient_self_form_write_projects_self_report_and_provenance():
    record = PatientRecordFactory()
    _seed_mm_vocab()
    identity = Identity.objects.create_user(email='mm-self@example.test')
    PatientUser.objects.create(identity=identity, person=record.person)
    client = APIClient()
    client.force_authenticate(identity)

    response = client.patch(
        f'/api/v1/patient-records/{record.person_id}/',
        {'mgus': True}, format='json', HTTP_X_PATIENT_FORM='1',
    )

    assert response.status_code == 200, response.data
    record.refresh_from_db()
    assert record.mgus is True
    row = Observation.objects.get(person=record.person, observation_concept_id=4149022)
    assert row.observation_type_concept_id == 32865
    assert ProvenanceRecord.objects.filter(
        object_id=row.pk, source='PATIENT_SELF',
        source_user_id=f'{identity.issuer}|{identity.sub}',
    ).exists()


def test_a_missing_ehr_type_concept_does_not_roll_back_the_answer():
    from omop_core.services.write_descriptor import build_writable_field_descriptor

    from omop_core.models import Concept

    record = PatientRecordFactory()
    _seed_mm_vocab(ehr_type=False)
    # conftest seeds the standard EHR type concept; this case is a deployment
    # where it is genuinely absent.
    Concept.objects.filter(concept_id=32817).delete()

    entry = build_writable_field_descriptor()['smoldering_multiple_myeloma']
    assert entry['writable'] is True
    assert 'projection' not in entry
    assert 'type concept' in entry['reason']

    response = _client().patch(
        f'/api/v1/patient-records/{record.person_id}/',
        {'smoldering_multiple_myeloma': True}, format='json', HTTP_X_PATIENT_FORM='1',
    )

    assert response.status_code == 200, response.data
    record.refresh_from_db()
    assert record.smoldering_multiple_myeloma is True
    assert not ConditionOccurrence.objects.filter(
        person=record.person, condition_concept_id=4184985,
    ).exists()


def test_a_patient_answer_skips_projection_without_the_self_report_type():
    record = PatientRecordFactory()
    _seed_mm_vocab(self_report_type=False)
    clinical = ObservationFactory(
        person=record.person, observation_concept_id=4149022,
        observation_source_value='35601003', value_as_number=0.0,
        observation_date=timezone.localdate(), observation_type_concept_id=32817,
    )
    identity = Identity.objects.create_user(email='mm-self-no-type@example.test')
    PatientUser.objects.create(identity=identity, person=record.person)
    client = APIClient()
    client.force_authenticate(identity)

    response = client.patch(
        f'/api/v1/patient-records/{record.person_id}/',
        {'mgus': True}, format='json', HTTP_X_PATIENT_FORM='1',
    )

    assert response.status_code == 200, response.data
    record.refresh_from_db()
    assert record.mgus is True
    clinical.refresh_from_db()
    assert clinical.value_as_number == 0.0
    assert Observation.objects.filter(
        person=record.person, observation_concept_id=4149022,
    ).count() == 1


def test_a_representative_answer_skips_projection_without_the_self_report_type():
    record = PatientRecordFactory()
    _seed_mm_vocab(self_report_type=False)
    carer = Identity.objects.create_user(email='mm-carer-no-type@example.test')
    PersonalRepresentative.objects.create(
        representative=carer, person_id=record.person_id,
        relationship='caregiver', verification_status='VERIFIED',
    )
    client = APIClient()
    client.force_authenticate(carer)

    response = client.patch(
        f'/api/v1/patient-records/{record.person_id}/',
        {'mgus': True}, format='json', HTTP_X_PATIENT_FORM='1',
    )

    assert response.status_code == 200, response.data
    record.refresh_from_db()
    assert record.mgus is True
    assert not Observation.objects.filter(
        person=record.person, observation_concept_id=4149022,
    ).exists()
