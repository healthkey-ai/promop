"""
Integration tests for the FHIR upload pipeline and UI API views.

Test flow:
  1. POST a synthetic FHIR bundle to /api/patient-info/upload_fhir/
  2. Assert OMOP tables (Person, ConditionOccurrence, Measurement,
     DrugExposure, Episode, EpisodeEvent) are populated
  3. Assert PatientInfo is derived and key fields are correct
  4. Assert the UI-facing API endpoints return the uploaded data
"""

import io
import json
from datetime import date

from django.contrib.auth.models import User
from django.test import TestCase
from rest_framework import status
from rest_framework.test import APIClient

from omop_core.models import (
    Concept, ConceptClass, Domain, Vocabulary,
    Person, PatientInfo,
    ConditionOccurrence, DrugExposure, Measurement, ProcedureOccurrence,
)
from omop_oncology.models import Episode, EpisodeEvent


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_vocab_fixtures():
    """Create the minimum OMOP vocabulary records required by Concept FKs."""
    vocab, _ = Vocabulary.objects.get_or_create(
        vocabulary_id='TEST',
        defaults={
            'vocabulary_name': 'Test Vocabulary',
            'vocabulary_concept_id': 0,
        },
    )
    domain_condition, _ = Domain.objects.get_or_create(
        domain_id='Condition',
        defaults={'domain_name': 'Condition', 'domain_concept_id': 19},
    )
    domain_measurement, _ = Domain.objects.get_or_create(
        domain_id='Measurement',
        defaults={'domain_name': 'Measurement', 'domain_concept_id': 21},
    )
    domain_drug, _ = Domain.objects.get_or_create(
        domain_id='Drug',
        defaults={'domain_name': 'Drug', 'domain_concept_id': 13},
    )
    domain_type, _ = Domain.objects.get_or_create(
        domain_id='Type Concept',
        defaults={'domain_name': 'Type Concept', 'domain_concept_id': 58},
    )
    domain_gender, _ = Domain.objects.get_or_create(
        domain_id='Gender',
        defaults={'domain_name': 'Gender', 'domain_concept_id': 2},
    )
    cc, _ = ConceptClass.objects.get_or_create(
        concept_class_id='Clinical Finding',
        defaults={'concept_class_name': 'Clinical Finding', 'concept_class_concept_id': 0},
    )
    today = date.today()
    far_future = date(2099, 12, 31)

    def _concept(cid, name, domain):
        obj, _ = Concept.objects.get_or_create(
            concept_id=cid,
            defaults={
                'concept_name': name,
                'domain': domain,
                'vocabulary': vocab,
                'concept_class': cc,
                'concept_code': str(cid),
                'valid_start_date': today,
                'valid_end_date': far_future,
            },
        )
        return obj

    # Concept records the upload view looks up by ID or name
    _concept(4112853,  'Breast cancer',           domain_condition)
    _concept(3000963,  'Laboratory test result',  domain_measurement)
    _concept(32817,    'EHR',                     domain_type)
    _concept(32856,    'Lab',                     domain_type)
    _concept(32869,    'EHR prescription',        domain_type)
    _concept(32531,    'Treatment Regimen',       domain_type)   # episode_concept for LOT
    _concept(1147094,  'drug_exposure_id field',  domain_type)   # EpisodeEvent field concept
    # Generic drug concept — fallback when named regimen not found
    _concept(19136160, 'Drug',                    domain_drug)
    # Gender concepts used by get_gender_concept() in views.py
    _concept(8532, 'FEMALE', domain_gender)
    _concept(8507, 'MALE',   domain_gender)
    _concept(8551, 'UNKNOWN', domain_gender)


def _make_fhir_bundle():
    """Minimal but realistic FHIR R4 Bundle for one breast-cancer patient.

    Includes:
      * Patient demographics
      * Condition (breast cancer, onset 2022-01-15)
      * 3 Observations with LOINC codes (Hgb, WBC, Creatinine)
      * 2 MedicationStatements (LOT 1: AC-T, LOT 2: Kadcyla)
    """
    patient_id = 'test-patient-jane-001'

    patient = {
        'resourceType': 'Patient',
        'id': patient_id,
        'name': [{'family': 'Smith', 'given': ['Jane']}],
        'gender': 'female',
        'birthDate': '1975-03-15',
        'address': [{'city': 'Salt Lake City', 'state': 'UT', 'country': 'US', 'postalCode': '84101'}],
        'extension': [
            {'url': 'http://ctomop.io/fhir/StructureDefinition/ethnicity', 'valueString': 'White'},
            {'url': 'http://ctomop.io/fhir/StructureDefinition/bodyWeight',
             'valueQuantity': {'value': 65.0, 'unit': 'kg'}},
            {'url': 'http://ctomop.io/fhir/StructureDefinition/bodyHeight',
             'valueQuantity': {'value': 165.0, 'unit': 'cm'}},
            {'url': 'http://ctomop.io/fhir/StructureDefinition/ecog-performance-status',
             'valueInteger': 1},
        ],
    }

    condition = {
        'resourceType': 'Condition',
        'id': 'cond-bc-001',
        'subject': {'reference': f'Patient/{patient_id}'},
        'code': {'text': 'Invasive Ductal Carcinoma', 'coding': [
            {'system': 'http://snomed.info/sct', 'code': '413448000',
             'display': 'Invasive ductal carcinoma of breast'},
        ]},
        'onsetDateTime': '2022-01-15',
        'stage': [{'summary': {'text': 'Stage II'}}],
    }

    def _obs(loinc_code, display, value, unit, date_str):
        return {
            'resourceType': 'Observation',
            'status': 'final',
            'subject': {'reference': f'Patient/{patient_id}'},
            'effectiveDateTime': date_str,
            'code': {
                'coding': [{'system': 'http://loinc.org', 'code': loinc_code, 'display': display}],
                'text': display,
            },
            'valueQuantity': {'value': value, 'unit': unit},
        }

    hemoglobin_obs = _obs('718-7',  'Hemoglobin [Mass/volume] in Blood',          11.2, 'g/dL',  '2022-02-01')
    wbc_obs        = _obs('6690-2', 'Leukocytes [#/volume] in Blood',              4.5, 'K/uL',  '2022-02-01')
    creatinine_obs = _obs('2160-0', 'Creatinine [Mass/volume] in Serum or Plasma', 0.9, 'mg/dL', '2022-02-01')

    def _med_statement(med_id, regimen_name, lot_num, start, end, outcome):
        stmt = {
            'resourceType': 'MedicationStatement',
            'id': med_id,
            'subject': {'reference': f'Patient/{patient_id}'},
            'status': 'completed',
            'medicationCodeableConcept': {'text': regimen_name},
            'effectivePeriod': {'start': start},
            'extension': [
                {'url': 'http://ctomop.io/fhir/StructureDefinition/therapy-line',
                 'valueInteger': lot_num},
                {'url': 'http://ctomop.io/fhir/StructureDefinition/therapy-outcome',
                 'valueString': outcome},
            ],
        }
        if end:
            stmt['effectivePeriod']['end'] = end
        return stmt

    lot1 = _med_statement('med-ac-t',    'AC-T',    1, '2022-03-01', '2022-09-01', 'CR')
    lot2 = _med_statement('med-kadcyla', 'Kadcyla', 2, '2023-01-15', None,         'PR')

    return {
        'resourceType': 'Bundle',
        'type': 'collection',
        'entry': [
            {'resource': patient},
            {'resource': condition},
            {'resource': hemoglobin_obs},
            {'resource': wbc_obs},
            {'resource': creatinine_obs},
            {'resource': lot1},
            {'resource': lot2},
        ],
    }


# ---------------------------------------------------------------------------
# Base class shared by all test classes
# ---------------------------------------------------------------------------

class FhirUploadBase(TestCase):
    """Sets up vocab fixtures and provides helpers used by all test classes."""

    @classmethod
    def setUpTestData(cls):
        _make_vocab_fixtures()
        cls.admin = User.objects.create_superuser(
            username='testadmin', password='testpass', email='admin@test.com'
        )

    def setUp(self):
        self.client = APIClient()
        self.client.force_authenticate(user=self.admin)

    def _upload_bundle(self):
        """POST the synthetic FHIR bundle; return the DRF Response."""
        bundle_bytes = json.dumps(_make_fhir_bundle()).encode('utf-8')
        fhir_file = io.BytesIO(bundle_bytes)
        fhir_file.name = 'test_bundle.json'
        return self.client.post(
            '/api/patient-info/upload_fhir/',
            {'file': fhir_file},
            format='multipart',
        )

    def _get_person(self):
        return Person.objects.filter(family_name='Smith', given_name='Jane').first()


# ---------------------------------------------------------------------------
# 1. OMOP table population tests
# ---------------------------------------------------------------------------

class FhirUploadOmopTablesTest(FhirUploadBase):
    """Verify that uploading a FHIR bundle populates the correct OMOP tables."""

    def test_upload_returns_success(self):
        response = self._upload_bundle()
        self.assertIn(response.status_code, [status.HTTP_200_OK, status.HTTP_201_CREATED],
                      msg=f'Upload failed: {response.data}')

    def test_person_created(self):
        self._upload_bundle()
        person = self._get_person()
        self.assertIsNotNone(person, 'Person record not created for Jane Smith')
        self.assertEqual(person.year_of_birth, 1975)
        self.assertEqual(person.month_of_birth, 3)
        self.assertEqual(person.day_of_birth, 15)

    def test_condition_occurrence_created(self):
        """A ConditionOccurrence row should exist for the breast cancer Condition resource."""
        self._upload_bundle()
        person = self._get_person()
        self.assertIsNotNone(person)
        conditions = ConditionOccurrence.objects.filter(person=person)
        self.assertGreater(conditions.count(), 0, 'No ConditionOccurrence created')
        co = conditions.first()
        self.assertEqual(co.condition_start_date, date(2022, 1, 15))

    def test_measurements_created_for_each_observation(self):
        """A Measurement row should exist for each LOINC-coded Observation."""
        self._upload_bundle()
        person = self._get_person()
        self.assertIsNotNone(person)
        measurements = Measurement.objects.filter(person=person)
        self.assertGreaterEqual(measurements.count(), 3,
                                f'Expected ≥3 Measurement rows, got {measurements.count()}')
        source_values = list(measurements.values_list('measurement_source_value', flat=True))
        self.assertTrue(
            any('Hemoglobin' in (v or '') for v in source_values),
            f'Hemoglobin measurement missing. source_values={source_values}',
        )

    def test_drug_exposures_created_per_lot(self):
        """One DrugExposure per MedicationStatement (therapy line)."""
        self._upload_bundle()
        person = self._get_person()
        self.assertIsNotNone(person)
        drug_exposures = DrugExposure.objects.filter(person=person)
        self.assertEqual(drug_exposures.count(), 2,
                         f'Expected 2 DrugExposure rows, got {drug_exposures.count()}')
        source_values = set(drug_exposures.values_list('drug_source_value', flat=True))
        self.assertIn('AC-T', source_values)
        self.assertIn('Kadcyla', source_values)

    def test_episodes_created_with_correct_lot_numbers(self):
        """Episode rows should exist with the correct episode_number for each LOT."""
        self._upload_bundle()
        person = self._get_person()
        self.assertIsNotNone(person)
        episodes = Episode.objects.filter(person=person).order_by('episode_number')
        self.assertEqual(episodes.count(), 2,
                         f'Expected 2 Episode rows, got {episodes.count()}')
        self.assertEqual(episodes[0].episode_number, 1)
        self.assertEqual(episodes[1].episode_number, 2)
        self.assertEqual(episodes[0].episode_start_date, date(2022, 3, 1))
        self.assertEqual(episodes[0].episode_end_date,   date(2022, 9, 1))
        self.assertIsNone(episodes[1].episode_end_date,  'LOT 2 should have no end date')

    def test_episode_events_link_drug_exposures_to_episodes(self):
        """Each Episode should have at least one EpisodeEvent linking it to a DrugExposure."""
        self._upload_bundle()
        person = self._get_person()
        self.assertIsNotNone(person)
        for episode in Episode.objects.filter(person=person):
            ee_count = EpisodeEvent.objects.filter(episode_id=episode.episode_id).count()
            self.assertGreater(
                ee_count, 0,
                f'Episode {episode.episode_number} (id={episode.episode_id}) has no EpisodeEvents',
            )


# ---------------------------------------------------------------------------
# 2. PatientInfo derivation tests
# ---------------------------------------------------------------------------

class FhirUploadPatientInfoTest(FhirUploadBase):
    """Verify PatientInfo is created and correctly derived from uploaded FHIR data."""

    def test_patient_info_created(self):
        self._upload_bundle()
        person = self._get_person()
        self.assertIsNotNone(person)
        pi = PatientInfo.objects.filter(person=person).first()
        self.assertIsNotNone(pi, 'PatientInfo not created for uploaded patient')

    def test_disease_populated_from_condition(self):
        self._upload_bundle()
        pi = PatientInfo.objects.get(person=self._get_person())
        self.assertIsNotNone(pi.disease, 'PatientInfo.disease not populated')

    def test_demographics_populated(self):
        self._upload_bundle()
        pi = PatientInfo.objects.get(person=self._get_person())
        self.assertEqual(pi.date_of_birth, date(1975, 3, 15))
        self.assertIsNotNone(pi.gender)

    def test_hemoglobin_populated_from_loinc_718_7(self):
        self._upload_bundle()
        pi = PatientInfo.objects.get(person=self._get_person())
        self.assertIsNotNone(pi.hemoglobin_g_dl)
        self.assertAlmostEqual(float(pi.hemoglobin_g_dl), 11.2, places=1)

    def test_wbc_populated_from_loinc_6690_2(self):
        self._upload_bundle()
        pi = PatientInfo.objects.get(person=self._get_person())
        self.assertIsNotNone(pi.wbc_count_thousand_per_ul)
        self.assertAlmostEqual(float(pi.wbc_count_thousand_per_ul), 4.5, places=1)

    def test_creatinine_populated_from_loinc_2160_0(self):
        self._upload_bundle()
        pi = PatientInfo.objects.get(person=self._get_person())
        self.assertIsNotNone(pi.serum_creatinine_mg_dl)
        self.assertAlmostEqual(float(pi.serum_creatinine_mg_dl), 0.9, places=1)

    def test_first_line_therapy_from_medication_statement(self):
        self._upload_bundle()
        pi = PatientInfo.objects.get(person=self._get_person())
        self.assertEqual(pi.first_line_therapy, 'AC-T')
        self.assertEqual(pi.first_line_start_date, date(2022, 3, 1))
        self.assertEqual(pi.first_line_end_date,   date(2022, 9, 1))
        self.assertEqual(pi.first_line_outcome,    'CR')

    def test_second_line_therapy_from_medication_statement(self):
        self._upload_bundle()
        pi = PatientInfo.objects.get(person=self._get_person())
        self.assertEqual(pi.second_line_therapy,    'Kadcyla')
        self.assertEqual(pi.second_line_start_date, date(2023, 1, 15))
        self.assertIsNone(pi.second_line_end_date,  'Open-ended LOT 2 should have no end date')


# ---------------------------------------------------------------------------
# 3. UI API view tests — data visible through endpoints the frontend uses
# ---------------------------------------------------------------------------

class UIViewsReflectUploadedDataTest(FhirUploadBase):
    """GET requests to UI-facing REST endpoints should return the data
    written by the FHIR upload pipeline."""

    def setUp(self):
        super().setUp()
        # Upload once per test method (TestCase wraps each test in a transaction)
        self._upload_bundle()
        self._person = self._get_person()
        self.assertIsNotNone(self._person, 'Setup: person not found after upload')
        self._pid = self._person.person_id

    # -- PatientInfo endpoint --------------------------------------------------

    def test_patient_info_endpoint_returns_record(self):
        # Retrieve endpoint (person_id as pk) returns {'patient_info': {...}, 'user': {...}}
        resp = self.client.get(f'/api/patient-info/{self._pid}/')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertIn('patient_info', resp.data)
        self.assertIn('disease', resp.data['patient_info'])

    def test_patient_info_endpoint_has_required_fields(self):
        resp = self.client.get(f'/api/patient-info/{self._pid}/')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        record = resp.data['patient_info']
        for field in ('disease', 'hemoglobin_g_dl', 'wbc_count_thousand_per_ul',
                      'serum_creatinine_mg_dl', 'first_line_therapy', 'second_line_therapy'):
            self.assertIn(field, record, f'Field {field!r} missing from patient-info response')

    def test_patient_info_endpoint_lab_values_match_observations(self):
        resp = self.client.get(f'/api/patient-info/{self._pid}/')
        record = resp.data['patient_info']
        self.assertAlmostEqual(float(record['hemoglobin_g_dl']),          11.2, places=1)
        self.assertAlmostEqual(float(record['wbc_count_thousand_per_ul']), 4.5, places=1)
        self.assertAlmostEqual(float(record['serum_creatinine_mg_dl']),    0.9, places=1)

    def test_patient_info_endpoint_therapy_lines_match_medications(self):
        resp = self.client.get(f'/api/patient-info/{self._pid}/')
        record = resp.data['patient_info']
        self.assertEqual(record['first_line_therapy'],  'AC-T')
        self.assertEqual(record['second_line_therapy'], 'Kadcyla')

    # -- Conditions endpoint ---------------------------------------------------

    def test_conditions_endpoint_returns_condition(self):
        resp = self.client.get('/api/conditions/', {'person_id': self._pid})
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        results = list(resp.data)
        self.assertGreater(len(results), 0, 'conditions endpoint returned empty list')
        # Verify the onset date is in the response
        dates = [r.get('condition_start_date') for r in results]
        self.assertIn('2022-01-15', dates)

    # -- Measurements endpoint -------------------------------------------------

    def test_measurements_endpoint_returns_lab_rows(self):
        resp = self.client.get('/api/measurements/', {'person_id': self._pid})
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        results = list(resp.data)
        self.assertGreaterEqual(len(results), 3,
                                f'Expected ≥3 measurement records via API, got {len(results)}')

    def test_measurements_endpoint_has_hemoglobin(self):
        resp = self.client.get('/api/measurements/', {'person_id': self._pid})
        results = list(resp.data)
        source_values = [r.get('measurement_source_value', '') for r in results]
        self.assertTrue(any('Hemoglobin' in v for v in source_values),
                        f'Hemoglobin not in measurement source values: {source_values}')

    # -- Drug exposures endpoint -----------------------------------------------

    def test_drug_exposures_endpoint_returns_both_lots(self):
        resp = self.client.get('/api/drug-exposures/', {'person_id': self._pid})
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        results = list(resp.data)
        self.assertEqual(len(results), 2)
        source_values = {r['drug_source_value'] for r in results}
        self.assertIn('AC-T',    source_values)
        self.assertIn('Kadcyla', source_values)

    def test_drug_exposures_endpoint_has_correct_dates(self):
        resp = self.client.get('/api/drug-exposures/', {'person_id': self._pid})
        results = list(resp.data)
        by_name = {r['drug_source_value']: r for r in results}
        self.assertEqual(by_name['AC-T']['drug_exposure_start_date'], '2022-03-01')
        self.assertEqual(by_name['AC-T']['drug_exposure_end_date'],   '2022-09-01')
        self.assertEqual(by_name['Kadcyla']['drug_exposure_start_date'], '2023-01-15')

    # -- Episodes endpoint -----------------------------------------------------

    def test_episodes_endpoint_returns_two_episodes(self):
        resp = self.client.get('/api/episodes/', {'person_id': self._pid})
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        results = list(resp.data)
        self.assertEqual(len(results), 2)
        numbers = sorted(r['episode_number'] for r in results)
        self.assertEqual(numbers, [1, 2])

    def test_episodes_endpoint_lot1_dates_correct(self):
        resp = self.client.get('/api/episodes/', {'person_id': self._pid})
        results = list(resp.data)
        lot1 = next(r for r in results if r['episode_number'] == 1)
        self.assertEqual(lot1['episode_start_date'], '2022-03-01')
        self.assertEqual(lot1['episode_end_date'],   '2022-09-01')

    def test_episodes_endpoint_lot2_open_ended(self):
        resp = self.client.get('/api/episodes/', {'person_id': self._pid})
        results = list(resp.data)
        lot2 = next(r for r in results if r['episode_number'] == 2)
        self.assertIsNone(lot2['episode_end_date'],
                          'LOT 2 should have null episode_end_date')

    # -- Episode events endpoint -----------------------------------------------

    def test_episode_events_endpoint_links_drugs_to_episodes(self):
        episodes_resp = self.client.get('/api/episodes/', {'person_id': self._pid})
        episodes = list(episodes_resp.data)
        for ep in episodes:
            ep_pk = ep.get('episode_id', ep.get('id'))
            ee_resp = self.client.get('/api/episode-events/',
                                      {'episode_id': ep_pk})
            self.assertEqual(ee_resp.status_code, status.HTTP_200_OK)
            ee_results = list(ee_resp.data)
            self.assertGreater(
                len(ee_results), 0,
                f'No EpisodeEvents for episode_id={ep_pk} (LOT {ep["episode_number"]})',
            )


# ---------------------------------------------------------------------------
# 4. Direct OMOP endpoint CRUD tests
# ---------------------------------------------------------------------------

class OmopEndpointAuthTest(FhirUploadBase):
    """Unauthenticated requests to OMOP endpoints must be rejected with 401."""

    def setUp(self):
        # Deliberately do NOT authenticate
        self.client = APIClient()

    def test_conditions_requires_auth(self):
        resp = self.client.get('/api/conditions/')
        self.assertEqual(resp.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_measurements_requires_auth(self):
        resp = self.client.get('/api/measurements/')
        self.assertEqual(resp.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_drug_exposures_requires_auth(self):
        resp = self.client.get('/api/drug-exposures/')
        self.assertEqual(resp.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_observations_requires_auth(self):
        resp = self.client.get('/api/observations/')
        self.assertEqual(resp.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_procedures_requires_auth(self):
        resp = self.client.get('/api/procedures/')
        self.assertEqual(resp.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_episodes_requires_auth(self):
        resp = self.client.get('/api/episodes/')
        self.assertEqual(resp.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_episode_events_requires_auth(self):
        resp = self.client.get('/api/episode-events/')
        self.assertEqual(resp.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_documents_requires_auth(self):
        resp = self.client.get('/api/documents/')
        self.assertEqual(resp.status_code, status.HTTP_401_UNAUTHORIZED)


class OmopObservationsEndpointTest(FhirUploadBase):
    """Tests for /api/observations/ — list, filter, create, update, delete."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        from omop_core.models import Concept, Domain, Vocabulary, ConceptClass
        from omop_core.models import Observation as OmopObservation
        vocab = Vocabulary.objects.get(vocabulary_id='TEST')
        domain_type = Domain.objects.get(domain_id='Type Concept')
        cc = ConceptClass.objects.get(concept_class_id='Clinical Finding')
        today = date.today()
        far_future = date(2099, 12, 31)
        cls._obs_concept, _ = Concept.objects.get_or_create(
            concept_id=9999901,
            defaults={
                'concept_name': 'Smoking status',
                'domain': domain_type,
                'vocabulary': vocab,
                'concept_class': cc,
                'concept_code': '9999901',
                'valid_start_date': today,
                'valid_end_date': far_future,
            },
        )
        cls._person = Person.objects.create(
            person_id=88801,
            year_of_birth=1980,
            gender_source_value='female',
            race_source_value='unknown',
            ethnicity_source_value='unknown',
        )
        cls._obs = OmopObservation.objects.create(
            observation_id=88801,
            person=cls._person,
            observation_concept=cls._obs_concept,
            observation_date=date(2024, 1, 10),
            observation_type_concept=cls._obs_concept,
            value_as_string='Never',
            observation_source_value='Smoking status',
        )

    def test_list_observations_returns_all(self):
        resp = self.client.get('/api/observations/')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        ids = [r.get('observation_id', r.get('id')) for r in resp.data]
        self.assertIn(88801, ids)

    def test_filter_observations_by_person_id(self):
        resp = self.client.get('/api/observations/', {'person_id': 88801})
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(len(list(resp.data)), 1)
        self.assertEqual(list(resp.data)[0]['observation_source_value'], 'Smoking status')

    def test_filter_observations_excludes_other_persons(self):
        resp = self.client.get('/api/observations/', {'person_id': 99999})
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(len(list(resp.data)), 0)

    def test_retrieve_single_observation(self):
        resp = self.client.get('/api/observations/88801/')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data['value_as_string'], 'Never')

    def test_create_observation(self):
        from omop_core.models import Observation as OmopObservation
        payload = {
            'observation_id': 88802,
            'person': self._person.person_id,
            'observation_concept': self._obs_concept.concept_id,
            'observation_date': '2024-06-01',
            'observation_type_concept': self._obs_concept.concept_id,
            'value_as_string': 'Former',
            'observation_source_value': 'Smoking status',
        }
        resp = self.client.post('/api/observations/', payload, format='json')
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)
        self.assertTrue(OmopObservation.objects.filter(observation_id=88802).exists())

    def test_update_observation(self):
        from omop_core.models import Observation as OmopObservation
        resp = self.client.patch('/api/observations/88801/', {'value_as_string': 'Current'}, format='json')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(OmopObservation.objects.get(observation_id=88801).value_as_string, 'Current')

    def test_delete_observation(self):
        from omop_core.models import Observation as OmopObservation
        resp = self.client.delete('/api/observations/88801/')
        self.assertEqual(resp.status_code, status.HTTP_204_NO_CONTENT)
        self.assertFalse(OmopObservation.objects.filter(observation_id=88801).exists())


class OmopProceduresEndpointTest(FhirUploadBase):
    """Tests for /api/procedures/ — list, filter, create, update, delete."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        from omop_core.models import Concept, Domain, Vocabulary, ConceptClass
        vocab = Vocabulary.objects.get(vocabulary_id='TEST')
        domain_type = Domain.objects.get(domain_id='Type Concept')
        cc = ConceptClass.objects.get(concept_class_id='Clinical Finding')
        today = date.today()
        far_future = date(2099, 12, 31)
        cls._proc_concept, _ = Concept.objects.get_or_create(
            concept_id=9999902,
            defaults={
                'concept_name': 'Biopsy',
                'domain': domain_type,
                'vocabulary': vocab,
                'concept_class': cc,
                'concept_code': '9999902',
                'valid_start_date': today,
                'valid_end_date': far_future,
            },
        )
        cls._person = Person.objects.create(
            person_id=88802,
            year_of_birth=1965,
            gender_source_value='female',
            race_source_value='unknown',
            ethnicity_source_value='unknown',
        )
        cls._proc = ProcedureOccurrence.objects.create(
            procedure_occurrence_id=88801,
            person=cls._person,
            procedure_concept=cls._proc_concept,
            procedure_date=date(2023, 5, 20),
            procedure_type_concept=cls._proc_concept,
            procedure_source_value='Core needle biopsy',
        )

    def test_list_procedures_returns_record(self):
        resp = self.client.get('/api/procedures/')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        ids = [r.get('procedure_occurrence_id', r.get('id')) for r in resp.data]
        self.assertIn(88801, ids)

    def test_filter_procedures_by_person_id(self):
        resp = self.client.get('/api/procedures/', {'person_id': 88802})
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        results = list(resp.data)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]['procedure_source_value'], 'Core needle biopsy')

    def test_filter_procedures_excludes_other_persons(self):
        resp = self.client.get('/api/procedures/', {'person_id': 99999})
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(len(list(resp.data)), 0)

    def test_retrieve_single_procedure(self):
        resp = self.client.get('/api/procedures/88801/')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data['procedure_source_value'], 'Core needle biopsy')

    def test_create_procedure(self):
        payload = {
            'procedure_occurrence_id': 88802,
            'person': self._person.person_id,
            'procedure_concept': self._proc_concept.concept_id,
            'procedure_date': '2024-03-10',
            'procedure_type_concept': self._proc_concept.concept_id,
            'procedure_source_value': 'Lumpectomy',
        }
        resp = self.client.post('/api/procedures/', payload, format='json')
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)
        self.assertTrue(ProcedureOccurrence.objects.filter(procedure_occurrence_id=88802).exists())

    def test_update_procedure(self):
        resp = self.client.patch('/api/procedures/88801/',
                                 {'procedure_source_value': 'Excisional biopsy'}, format='json')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(
            ProcedureOccurrence.objects.get(procedure_occurrence_id=88801).procedure_source_value,
            'Excisional biopsy',
        )

    def test_delete_procedure(self):
        resp = self.client.delete('/api/procedures/88801/')
        self.assertEqual(resp.status_code, status.HTTP_204_NO_CONTENT)
        self.assertFalse(ProcedureOccurrence.objects.filter(procedure_occurrence_id=88801).exists())


class OmopDocumentsEndpointTest(FhirUploadBase):
    """Tests for /api/documents/ — list, filter by person, create, update, delete."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        from omop_core.models import PatientDocument
        cls._person = Person.objects.create(
            person_id=88803,
            year_of_birth=1970,
            gender_source_value='female',
            race_source_value='unknown',
            ethnicity_source_value='unknown',
        )
        cls._doc = PatientDocument.objects.create(
            person=cls._person,
            doc_type='NGS',
            title='NGS Panel Report',
            file_url='https://storage.example.com/ngs-report.pdf',
            file_name='ngs-report.pdf',
            verified=False,
        )

    def test_list_documents_returns_record(self):
        resp = self.client.get('/api/documents/')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        titles = [r.get('title') for r in resp.data]
        self.assertIn('NGS Panel Report', titles)

    def test_filter_documents_by_person_id(self):
        resp = self.client.get('/api/documents/', {'person_id': 88803})
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        results = list(resp.data)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]['doc_type'], 'NGS')

    def test_filter_documents_excludes_other_persons(self):
        resp = self.client.get('/api/documents/', {'person_id': 99999})
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(len(list(resp.data)), 0)

    def test_retrieve_single_document(self):
        resp = self.client.get(f'/api/documents/{self._doc.pk}/')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data['file_name'], 'ngs-report.pdf')

    def test_create_document(self):
        from omop_core.models import PatientDocument
        payload = {
            'person': self._person.person_id,
            'doc_type': 'IMAGING',
            'title': 'CT Scan',
            'file_url': 'https://storage.example.com/ct-scan.pdf',
            'file_name': 'ct-scan.pdf',
            'verified': False,
        }
        resp = self.client.post('/api/documents/', payload, format='json')
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)
        self.assertTrue(
            PatientDocument.objects.filter(person=self._person, doc_type='IMAGING').exists()
        )

    def test_update_document_verified_flag(self):
        from omop_core.models import PatientDocument
        resp = self.client.patch(f'/api/documents/{self._doc.pk}/', {'verified': True}, format='json')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertTrue(PatientDocument.objects.get(pk=self._doc.pk).verified)

    def test_delete_document(self):
        from omop_core.models import PatientDocument
        pk = self._doc.pk
        resp = self.client.delete(f'/api/documents/{pk}/')
        self.assertEqual(resp.status_code, status.HTTP_204_NO_CONTENT)
        self.assertFalse(PatientDocument.objects.filter(pk=pk).exists())
