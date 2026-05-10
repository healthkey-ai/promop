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


# ---------------------------------------------------------------------------
# 5. SMART on FHIR tests
# ---------------------------------------------------------------------------

class SmartConfigurationTest(TestCase):
    """/.well-known/smart-configuration must return correct SMART metadata."""

    def setUp(self):
        self.client = APIClient()

    def test_discovery_is_public(self):
        resp = self.client.get('/.well-known/smart-configuration')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

    def test_discovery_shape(self):
        resp = self.client.get('/.well-known/smart-configuration')
        data = resp.json()
        required = {
            'authorization_endpoint',
            'token_endpoint',
            'scopes_supported',
            'response_types_supported',
            'capabilities',
            'code_challenge_methods_supported',
        }
        for key in required:
            self.assertIn(key, data, f'Missing key: {key}')

    def test_discovery_pkce_advertised(self):
        resp = self.client.get('/.well-known/smart-configuration')
        self.assertIn('S256', resp.json()['code_challenge_methods_supported'])

    def test_discovery_scopes_include_smart(self):
        resp = self.client.get('/.well-known/smart-configuration')
        scopes = resp.json()['scopes_supported']
        for required_scope in ['openid', 'patient/*.read', 'patient/*.write', 'launch/patient']:
            self.assertIn(required_scope, scopes, f'Scope missing: {required_scope}')

    def test_discovery_capabilities_include_standalone(self):
        resp = self.client.get('/.well-known/smart-configuration')
        caps = resp.json()['capabilities']
        self.assertIn('launch-standalone', caps)
        self.assertIn('client-public', caps)


class SmartTokenAuthTest(TestCase):
    """OMOP endpoints accept OAuth2 Bearer tokens with the correct scope."""

    @classmethod
    def setUpTestData(cls):
        from oauth2_provider.models import Application, AccessToken
        from django.utils import timezone as tz
        import datetime

        cls.user = User.objects.create_user(
            username='smartuser', password='smartpass'
        )

        cls.app = Application.objects.create(
            name='Test SMART App',
            client_id='test-smart-client',
            client_type=Application.CLIENT_PUBLIC,
            authorization_grant_type=Application.GRANT_AUTHORIZATION_CODE,
            user=cls.user,
        )

        # Token with patient/*.read — should allow GET
        cls.read_token = AccessToken.objects.create(
            user=cls.user,
            application=cls.app,
            token='test-read-token-abc123',
            expires=tz.now() + datetime.timedelta(hours=1),
            scope='patient/*.read openid',
        )

        # Token with no useful scope — should be denied
        cls.empty_scope_token = AccessToken.objects.create(
            user=cls.user,
            application=cls.app,
            token='test-empty-token-xyz789',
            expires=tz.now() + datetime.timedelta(hours=1),
            scope='',
        )

    def _bearer(self, token_str: str) -> APIClient:
        c = APIClient()
        c.credentials(HTTP_AUTHORIZATION=f'Bearer {token_str}')
        return c

    def test_read_token_allows_list_conditions(self):
        client = self._bearer(self.read_token.token)
        resp = client.get('/api/conditions/')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

    def test_read_token_allows_list_observations(self):
        client = self._bearer(self.read_token.token)
        resp = client.get('/api/observations/')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

    def test_read_token_allows_list_procedures(self):
        client = self._bearer(self.read_token.token)
        resp = client.get('/api/procedures/')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

    def test_read_token_allows_list_drug_exposures(self):
        client = self._bearer(self.read_token.token)
        resp = client.get('/api/drug-exposures/')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

    def test_no_token_returns_401(self):
        resp = self.client.get('/api/conditions/')
        self.assertEqual(resp.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_empty_scope_token_returns_403(self):
        client = self._bearer(self.empty_scope_token.token)
        resp = client.get('/api/conditions/')
        self.assertIn(resp.status_code, [
            status.HTTP_403_FORBIDDEN,
            status.HTTP_401_UNAUTHORIZED,
        ])

    def test_oauth2_token_endpoint_exists(self):
        resp = self.client.get('/o/token/')
        # GET is not allowed on token endpoint (returns 405), but it must exist
        self.assertNotEqual(resp.status_code, status.HTTP_404_NOT_FOUND)

    def test_oauth2_authorize_endpoint_exists(self):
        resp = self.client.get('/o/authorize/')
        # Redirects to login or returns 200/400 — anything but 404
        self.assertNotEqual(resp.status_code, status.HTTP_404_NOT_FOUND)


# ---------------------------------------------------------------------------
# 6. OMOP → PatientInfo signal tests
#
# Each test writes directly to an OMOP table via the ORM (no API, no FHIR
# upload) and asserts that the post_save / post_delete signal automatically
# refreshes the PatientInfo row with the correct derived value.
# ---------------------------------------------------------------------------

class _SignalBase(TestCase):
    """Shared fixtures for signal tests.

    Uses setUpTestData (runs once per class, rolled back after the class) for
    vocab, concepts, and the test Person — matching the pattern used by
    FhirUploadBase so the remote Render DB isn't hammered with per-test creates.

    Django's TestCase wraps each individual test method in a savepoint that is
    rolled back after the test, so OMOP records and PatientInfo rows created
    during a test are gone before the next test starts.  Only the setUpTestData
    fixtures (vocab, concepts, Person) survive across tests within the class.

    Each subclass declares its own PERSON_ID to avoid collisions between classes
    (they run sequentially but the class-level transactions overlap in time).
    """

    PERSON_ID = 80000  # override in each subclass

    @classmethod
    def setUpTestData(cls):
        _make_vocab_fixtures()
        from omop_core.models import Vocabulary, Domain, ConceptClass

        vocab = Vocabulary.objects.get(vocabulary_id='TEST')
        domain_condition = Domain.objects.get(domain_id='Condition')
        domain_measurement = Domain.objects.get(domain_id='Measurement')
        domain_drug = Domain.objects.get(domain_id='Drug')
        cc = ConceptClass.objects.get(concept_class_id='Clinical Finding')
        today = date.today()
        far_future = date(2099, 12, 31)

        def _concept(cid, name, domain, code=None):
            obj, _ = Concept.objects.get_or_create(
                concept_id=cid,
                defaults={
                    'concept_name': name,
                    'domain': domain,
                    'vocabulary': vocab,
                    'concept_class': cc,
                    'concept_code': code or str(cid),
                    'valid_start_date': today,
                    'valid_end_date': far_future,
                },
            )
            return obj

        cls.cancer_concept    = _concept(8000001, 'Breast cancer',            domain_condition)
        cls.other_concept     = _concept(8000002, 'Hypertension',             domain_condition)
        cls.remission_concept = _concept(8000003, 'In remission',             domain_condition)
        cls.relapse_concept   = _concept(8000004, 'Relapse of disease',       domain_condition)
        cls.drug_concept_a    = _concept(8000010, 'Paclitaxel',               domain_drug)
        cls.drug_concept_b    = _concept(8000011, 'Carboplatin',              domain_drug)
        cls.drug_concept_c    = _concept(8000012, 'Trastuzumab',              domain_drug)
        cls.hemoglobin_concept = _concept(8000020, 'Hemoglobin measurement',  domain_measurement)
        cls.creatinine_concept = _concept(8000021, 'Creatinine in serum',     domain_measurement)
        cls.platelet_concept   = _concept(8000022, 'Platelet count',          domain_measurement)
        cls.ecog_concept       = _concept(8000030, 'ECOG performance status', domain_condition)
        cls.karnofsky_concept  = _concept(8000031, 'Karnofsky performance score', domain_condition)
        cls.procedure_concept  = _concept(8000040, 'Core needle biopsy',      domain_condition)
        cls.type_concept = Concept.objects.get(concept_id=32817)

        # One Person per class — shared across all tests in the class.
        # Each test's OMOP writes and PatientInfo rows are rolled back by TestCase.
        cls.person = Person.objects.create(
            person_id=cls.PERSON_ID,
            year_of_birth=1970,
            gender_source_value='female',
            race_source_value='unknown',
            ethnicity_source_value='unknown',
        )

    def _get_pi(self):
        return PatientInfo.objects.filter(person=self.person).first()


class ConditionToPatientInfoTest(_SignalBase):
    """ConditionOccurrence saves/deletes update PatientInfo.disease,
    diagnosis_date, condition_clinical_status, and disease_slug."""

    PERSON_ID = 80001

    def test_create_cancer_condition_sets_disease(self):
        ConditionOccurrence.objects.create(
            condition_occurrence_id=90101,
            person=self.person,
            condition_concept=self.cancer_concept,
            condition_start_date=date(2022, 6, 1),
            condition_type_concept=self.type_concept,
        )
        pi = self._get_pi()
        self.assertIsNotNone(pi, 'PatientInfo not created after ConditionOccurrence save')
        self.assertEqual(pi.disease, 'Breast cancer')

    def test_create_cancer_condition_sets_diagnosis_date(self):
        ConditionOccurrence.objects.create(
            condition_occurrence_id=90101,
            person=self.person,
            condition_concept=self.cancer_concept,
            condition_start_date=date(2021, 3, 15),
            condition_type_concept=self.type_concept,
        )
        pi = self._get_pi()
        self.assertEqual(pi.diagnosis_date, date(2021, 3, 15))

    def test_create_cancer_condition_sets_disease_slug(self):
        ConditionOccurrence.objects.create(
            condition_occurrence_id=90101,
            person=self.person,
            condition_concept=self.cancer_concept,
            condition_start_date=date(2022, 1, 1),
            condition_type_concept=self.type_concept,
        )
        pi = self._get_pi()
        self.assertEqual(pi.disease_slug, 'breast-cancer')

    def test_non_cancer_condition_sets_diagnosis_date_without_disease(self):
        ConditionOccurrence.objects.create(
            condition_occurrence_id=90101,
            person=self.person,
            condition_concept=self.other_concept,
            condition_start_date=date(2020, 5, 10),
            condition_type_concept=self.type_concept,
        )
        pi = self._get_pi()
        self.assertIsNotNone(pi)
        self.assertIsNone(pi.disease)
        self.assertEqual(pi.diagnosis_date, date(2020, 5, 10))

    def test_condition_status_remission_maps_correctly(self):
        ConditionOccurrence.objects.create(
            condition_occurrence_id=90101,
            person=self.person,
            condition_concept=self.cancer_concept,
            condition_start_date=date(2023, 1, 1),
            condition_type_concept=self.type_concept,
            condition_status_concept=self.remission_concept,
        )
        pi = self._get_pi()
        self.assertEqual(pi.condition_clinical_status, 'remission')

    def test_condition_status_relapse_maps_correctly(self):
        ConditionOccurrence.objects.create(
            condition_occurrence_id=90101,
            person=self.person,
            condition_concept=self.cancer_concept,
            condition_start_date=date(2023, 6, 1),
            condition_type_concept=self.type_concept,
            condition_status_concept=self.relapse_concept,
        )
        pi = self._get_pi()
        self.assertEqual(pi.condition_clinical_status, 'relapse')

    def test_update_condition_concept_updates_disease(self):
        cond = ConditionOccurrence.objects.create(
            condition_occurrence_id=90101,
            person=self.person,
            condition_concept=self.other_concept,
            condition_start_date=date(2022, 1, 1),
            condition_type_concept=self.type_concept,
        )
        self.assertIsNone(self._get_pi().disease)

        cond.condition_concept = self.cancer_concept
        cond.save()

        self.assertEqual(self._get_pi().disease, 'Breast cancer')

    def test_delete_cancer_condition_clears_disease(self):
        cond = ConditionOccurrence.objects.create(
            condition_occurrence_id=90101,
            person=self.person,
            condition_concept=self.cancer_concept,
            condition_start_date=date(2022, 1, 1),
            condition_type_concept=self.type_concept,
        )
        self.assertEqual(self._get_pi().disease, 'Breast cancer')

        cond.delete()

        self.assertIsNone(self._get_pi().disease)

    def test_most_recent_cancer_condition_wins(self):
        ConditionOccurrence.objects.create(
            condition_occurrence_id=90101,
            person=self.person,
            condition_concept=self.cancer_concept,
            condition_start_date=date(2020, 1, 1),
            condition_type_concept=self.type_concept,
        )
        ConditionOccurrence.objects.create(
            condition_occurrence_id=90102,
            person=self.person,
            condition_concept=self.cancer_concept,
            condition_start_date=date(2023, 6, 1),
            condition_type_concept=self.type_concept,
        )
        self.assertEqual(self._get_pi().diagnosis_date, date(2023, 6, 1))


class DrugExposureToPatientInfoTest(_SignalBase):
    """DrugExposure saves/deletes update PatientInfo therapy line fields."""

    PERSON_ID = 80002

    def test_first_drug_exposure_sets_first_line_therapy(self):
        DrugExposure.objects.create(
            drug_exposure_id=91001,
            person=self.person,
            drug_concept=self.drug_concept_a,
            drug_exposure_start_date=date(2022, 3, 1),
            drug_type_concept=self.type_concept,
        )
        pi = self._get_pi()
        self.assertIsNotNone(pi)
        self.assertEqual(pi.first_line_therapy, 'Paclitaxel')

    def test_two_drug_exposures_set_first_and_second_line(self):
        DrugExposure.objects.create(
            drug_exposure_id=91001,
            person=self.person,
            drug_concept=self.drug_concept_a,
            drug_exposure_start_date=date(2022, 3, 1),
            drug_type_concept=self.type_concept,
        )
        DrugExposure.objects.create(
            drug_exposure_id=91002,
            person=self.person,
            drug_concept=self.drug_concept_b,
            drug_exposure_start_date=date(2023, 1, 1),
            drug_type_concept=self.type_concept,
        )
        pi = self._get_pi()
        self.assertIsNotNone(pi.first_line_therapy)
        self.assertIsNotNone(pi.second_line_therapy)

    def test_therapy_lines_count_matches_unique_start_dates(self):
        for idx, drug in enumerate([self.drug_concept_a, self.drug_concept_b, self.drug_concept_c], start=1):
            DrugExposure.objects.create(
                drug_exposure_id=91000 + idx,
                person=self.person,
                drug_concept=drug,
                drug_exposure_start_date=date(2021 + idx, 1, 1),
                drug_type_concept=self.type_concept,
            )
        self.assertEqual(self._get_pi().therapy_lines_count, 3)

    def test_same_start_date_drugs_count_as_one_line(self):
        # Two drugs on the same date = one therapy line (combination regimen)
        DrugExposure.objects.create(
            drug_exposure_id=91001,
            person=self.person,
            drug_concept=self.drug_concept_a,
            drug_exposure_start_date=date(2022, 6, 1),
            drug_type_concept=self.type_concept,
        )
        DrugExposure.objects.create(
            drug_exposure_id=91002,
            person=self.person,
            drug_concept=self.drug_concept_b,
            drug_exposure_start_date=date(2022, 6, 1),
            drug_type_concept=self.type_concept,
        )
        self.assertEqual(self._get_pi().therapy_lines_count, 1)

    def test_combination_regimen_joined_in_first_line_therapy(self):
        # Same-date drugs are joined as "Drug A + Drug B" in first_line_therapy
        DrugExposure.objects.create(
            drug_exposure_id=91001,
            person=self.person,
            drug_concept=self.drug_concept_a,
            drug_exposure_start_date=date(2022, 6, 1),
            drug_type_concept=self.type_concept,
        )
        DrugExposure.objects.create(
            drug_exposure_id=91002,
            person=self.person,
            drug_concept=self.drug_concept_b,
            drug_exposure_start_date=date(2022, 6, 1),
            drug_type_concept=self.type_concept,
        )
        pi = self._get_pi()
        self.assertIn('Paclitaxel', pi.first_line_therapy)
        self.assertIn('Carboplatin', pi.first_line_therapy)
        self.assertIsNone(pi.second_line_therapy)

    def test_delete_drug_exposure_removes_therapy_line(self):
        de = DrugExposure.objects.create(
            drug_exposure_id=91001,
            person=self.person,
            drug_concept=self.drug_concept_a,
            drug_exposure_start_date=date(2022, 3, 1),
            drug_type_concept=self.type_concept,
        )
        self.assertEqual(self._get_pi().first_line_therapy, 'Paclitaxel')

        de.delete()

        self.assertIsNone(self._get_pi().first_line_therapy)

    def test_prior_therapy_reflects_line_count_vocabulary(self):
        # PatientInfo.save() sets prior_therapy to controlled vocabulary based
        # on therapy_lines_count — not drug names.  One exposure → 'One line'.
        DrugExposure.objects.create(
            drug_exposure_id=91001,
            person=self.person,
            drug_concept=self.drug_concept_b,
            drug_exposure_start_date=date(2022, 1, 1),
            drug_type_concept=self.type_concept,
        )
        pi = self._get_pi()
        self.assertEqual(pi.first_line_therapy, 'Carboplatin')
        self.assertEqual(pi.prior_therapy, 'One line')


class MeasurementToPatientInfoTest(_SignalBase):
    """Measurement saves update PatientInfo lab value fields."""

    PERSON_ID = 80003

    def test_hemoglobin_measurement_sets_hemoglobin_level(self):
        from omop_core.models import Measurement as OmopMeasurement
        OmopMeasurement.objects.create(
            measurement_id=92001,
            person=self.person,
            measurement_concept=self.hemoglobin_concept,
            measurement_date=date(2023, 1, 15),
            measurement_type_concept=self.type_concept,
            value_as_number=11.8,
        )
        pi = self._get_pi()
        self.assertIsNotNone(pi)
        self.assertIsNotNone(pi.hemoglobin_level)
        self.assertAlmostEqual(float(pi.hemoglobin_level), 11.8, places=1)

    def test_creatinine_measurement_sets_creatinine_level(self):
        from omop_core.models import Measurement as OmopMeasurement
        OmopMeasurement.objects.create(
            measurement_id=92001,
            person=self.person,
            measurement_concept=self.creatinine_concept,
            measurement_date=date(2023, 2, 1),
            measurement_type_concept=self.type_concept,
            value_as_number=1.1,
        )
        pi = self._get_pi()
        self.assertIsNotNone(pi.serum_creatinine_level)
        self.assertAlmostEqual(float(pi.serum_creatinine_level), 1.1, places=1)

    def test_platelet_measurement_sets_platelet_count(self):
        from omop_core.models import Measurement as OmopMeasurement
        OmopMeasurement.objects.create(
            measurement_id=92001,
            person=self.person,
            measurement_concept=self.platelet_concept,
            measurement_date=date(2023, 3, 1),
            measurement_type_concept=self.type_concept,
            value_as_number=150000,
        )
        pi = self._get_pi()
        self.assertIsNotNone(pi.platelet_count)
        self.assertEqual(float(pi.platelet_count), 150000)

    def test_more_recent_measurement_supersedes_older(self):
        from omop_core.models import Measurement as OmopMeasurement
        OmopMeasurement.objects.create(
            measurement_id=92001,
            person=self.person,
            measurement_concept=self.hemoglobin_concept,
            measurement_date=date(2022, 6, 1),
            measurement_type_concept=self.type_concept,
            value_as_number=10.0,
        )
        OmopMeasurement.objects.create(
            measurement_id=92002,
            person=self.person,
            measurement_concept=self.hemoglobin_concept,
            measurement_date=date(2023, 6, 1),
            measurement_type_concept=self.type_concept,
            value_as_number=12.5,
        )
        self.assertAlmostEqual(float(self._get_pi().hemoglobin_level), 12.5, places=1)

    def test_delete_measurement_clears_lab_value(self):
        from omop_core.models import Measurement as OmopMeasurement
        m = OmopMeasurement.objects.create(
            measurement_id=92001,
            person=self.person,
            measurement_concept=self.hemoglobin_concept,
            measurement_date=date(2023, 1, 1),
            measurement_type_concept=self.type_concept,
            value_as_number=9.5,
        )
        self.assertIsNotNone(self._get_pi().hemoglobin_level)

        m.delete()

        self.assertIsNone(self._get_pi().hemoglobin_level)


class ObservationToPatientInfoTest(_SignalBase):
    """Observation saves update PatientInfo performance status fields."""

    PERSON_ID = 80004

    def test_ecog_observation_sets_ecog_performance_status(self):
        from omop_core.models import Observation as OmopObservation
        OmopObservation.objects.create(
            observation_id=93001,
            person=self.person,
            observation_concept=self.ecog_concept,
            observation_date=date(2023, 4, 1),
            observation_type_concept=self.type_concept,
            value_as_number=1,
        )
        pi = self._get_pi()
        self.assertIsNotNone(pi)
        self.assertEqual(pi.ecog_performance_status, 1)

    def test_ecog_observation_update_changes_performance_status(self):
        from omop_core.models import Observation as OmopObservation
        obs = OmopObservation.objects.create(
            observation_id=93001,
            person=self.person,
            observation_concept=self.ecog_concept,
            observation_date=date(2023, 4, 1),
            observation_type_concept=self.type_concept,
            value_as_number=2,
        )
        self.assertEqual(self._get_pi().ecog_performance_status, 2)

        obs.value_as_number = 0
        obs.save()

        self.assertEqual(self._get_pi().ecog_performance_status, 0)

    def test_karnofsky_observation_sets_karnofsky_score(self):
        from omop_core.models import Observation as OmopObservation
        OmopObservation.objects.create(
            observation_id=93001,
            person=self.person,
            observation_concept=self.karnofsky_concept,
            observation_date=date(2023, 5, 1),
            observation_type_concept=self.type_concept,
            value_as_number=80,
        )
        pi = self._get_pi()
        self.assertIsNotNone(pi)
        self.assertEqual(pi.karnofsky_performance_score, 80)

    def test_delete_ecog_observation_clears_performance_status(self):
        from omop_core.models import Observation as OmopObservation
        obs = OmopObservation.objects.create(
            observation_id=93001,
            person=self.person,
            observation_concept=self.ecog_concept,
            observation_date=date(2023, 6, 1),
            observation_type_concept=self.type_concept,
            value_as_number=3,
        )
        self.assertEqual(self._get_pi().ecog_performance_status, 3)

        obs.delete()

        self.assertIsNone(self._get_pi().ecog_performance_status)


class ProcedureToPatientInfoTest(_SignalBase):
    """ProcedureOccurrence saves/deletes update PatientInfo.prior_procedures."""

    PERSON_ID = 80005

    def test_procedure_sets_prior_procedures(self):
        ProcedureOccurrence.objects.create(
            procedure_occurrence_id=94001,
            person=self.person,
            procedure_concept=self.procedure_concept,
            procedure_date=date(2022, 8, 20),
            procedure_type_concept=self.type_concept,
            procedure_source_value='Core needle biopsy',
        )
        pi = self._get_pi()
        self.assertIsNotNone(pi)
        self.assertIsInstance(pi.prior_procedures, list)
        self.assertEqual(len(pi.prior_procedures), 1)
        self.assertEqual(pi.prior_procedures[0]['procedure'], 'Core needle biopsy')

    def test_multiple_procedures_all_appear_in_prior_procedures(self):
        ProcedureOccurrence.objects.create(
            procedure_occurrence_id=94001,
            person=self.person,
            procedure_concept=self.procedure_concept,
            procedure_date=date(2022, 1, 10),
            procedure_type_concept=self.type_concept,
            procedure_source_value='Biopsy',
        )
        ProcedureOccurrence.objects.create(
            procedure_occurrence_id=94002,
            person=self.person,
            procedure_concept=self.procedure_concept,
            procedure_date=date(2023, 3, 5),
            procedure_type_concept=self.type_concept,
            procedure_source_value='Lumpectomy',
        )
        pi = self._get_pi()
        names = [p['procedure'] for p in pi.prior_procedures]
        self.assertIn('Core needle biopsy', names)
        self.assertIn('Core needle biopsy', names)
        self.assertEqual(len(pi.prior_procedures), 2)

    def test_procedure_date_stored_in_prior_procedures(self):
        ProcedureOccurrence.objects.create(
            procedure_occurrence_id=94001,
            person=self.person,
            procedure_concept=self.procedure_concept,
            procedure_date=date(2021, 11, 30),
            procedure_type_concept=self.type_concept,
        )
        self.assertEqual(self._get_pi().prior_procedures[0]['date'], '2021-11-30')

    def test_delete_procedure_removes_it_from_prior_procedures(self):
        proc = ProcedureOccurrence.objects.create(
            procedure_occurrence_id=94001,
            person=self.person,
            procedure_concept=self.procedure_concept,
            procedure_date=date(2022, 5, 1),
            procedure_type_concept=self.type_concept,
        )
        self.assertEqual(len(self._get_pi().prior_procedures), 1)

        proc.delete()

        self.assertEqual(len(self._get_pi().prior_procedures), 0)


# ---------------------------------------------------------------------------
# 7. Service client SMART on FHIR integration tests
#
# These tests simulate a generic service client's two primary flows:
#   A. Reading patient data with a patient/*.read token
#   B. Writing OMOP records with a patient/*.write token and verifying
#      that PatientInfo is automatically refreshed from the written data
#
# Token setup mirrors what any confidential service client receives after
# the client_credentials exchange. Tokens are inserted directly into the
# DB to avoid round-tripping the full OAuth2 flow in tests.
# ---------------------------------------------------------------------------

class _SmartBase(TestCase):
    """Shared fixtures for service client SMART tests."""

    @classmethod
    def setUpTestData(cls):
        from oauth2_provider.models import Application, AccessToken
        from django.utils import timezone as tz
        import datetime

        _make_vocab_fixtures()

        cls.foundation_user = User.objects.create_user(
            username='foundation_svc', password='foundation_pass'
        )

        cls.app = Application.objects.create(
            name='Foundation EHR',
            client_id='foundation-client-id',
            client_type=Application.CLIENT_CONFIDENTIAL,
            authorization_grant_type=Application.GRANT_CLIENT_CREDENTIALS,
            user=cls.foundation_user,
        )

        # Read-only token — service client reads patient data
        cls.read_token = AccessToken.objects.create(
            user=cls.foundation_user,
            application=cls.app,
            token='foundation-read-token-111',
            expires=tz.now() + datetime.timedelta(hours=1),
            scope='patient/*.read openid launch/patient',
        )

        # Read+write token — service client writes OMOP records
        cls.write_token = AccessToken.objects.create(
            user=cls.foundation_user,
            application=cls.app,
            token='foundation-write-token-222',
            expires=tz.now() + datetime.timedelta(hours=1),
            scope='patient/*.read patient/*.write openid launch/patient',
        )

        # Expired token — must be rejected
        cls.expired_token = AccessToken.objects.create(
            user=cls.foundation_user,
            application=cls.app,
            token='foundation-expired-token-333',
            expires=tz.now() - datetime.timedelta(seconds=1),
            scope='patient/*.read patient/*.write openid',
        )

        # Patient and minimal OMOP fixtures shared across subclasses
        cls.person = Person.objects.create(
            person_id=70001,
            given_name='Alice',
            family_name='Foundation',
            year_of_birth=1980,
            gender_source_value='female',
            race_source_value='unknown',
            ethnicity_source_value='unknown',
        )

        # Reuse concepts created by _make_vocab_fixtures()
        from omop_core.models import Concept
        cls.condition_concept = Concept.objects.get(concept_id=4112853)  # Breast cancer
        cls.drug_concept = Concept.objects.get(concept_id=19136160)       # Drug (generic)
        cls.type_concept = Concept.objects.get(concept_id=32817)          # EHR

    def _bearer(self, token_str: str) -> APIClient:
        c = APIClient()
        c.credentials(HTTP_AUTHORIZATION=f'Bearer {token_str}')
        return c

    @property
    def read_client(self):
        return self._bearer(self.read_token.token)

    @property
    def write_client(self):
        return self._bearer(self.write_token.token)


class SmartServiceClientReadTest(_SmartBase):
    """Service client reads patient OMOP data using a patient/*.read Bearer token."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        # Seed one record of each type so list endpoints have data to return
        cls.condition = ConditionOccurrence.objects.create(
            condition_occurrence_id=70101,
            person=cls.person,
            condition_concept=cls.condition_concept,
            condition_start_date=date(2023, 1, 10),
            condition_type_concept=cls.type_concept,
            condition_source_value='Breast cancer',
        )
        from omop_core.models import Observation as OmopObservation, DrugExposure as DE
        cls.observation = OmopObservation.objects.create(
            observation_id=70201,
            person=cls.person,
            observation_concept=cls.condition_concept,
            observation_date=date(2023, 2, 1),
            observation_type_concept=cls.type_concept,
            value_as_string='ECOG 1',
        )
        cls.drug = DE.objects.create(
            drug_exposure_id=70301,
            person=cls.person,
            drug_concept=cls.drug_concept,
            drug_exposure_start_date=date(2023, 3, 1),
            drug_type_concept=cls.type_concept,
            drug_source_value='Trastuzumab',
        )
        cls.procedure = ProcedureOccurrence.objects.create(
            procedure_occurrence_id=70401,
            person=cls.person,
            procedure_concept=cls.condition_concept,
            procedure_date=date(2023, 4, 15),
            procedure_type_concept=cls.type_concept,
            procedure_source_value='Lumpectomy',
        )

    def test_read_token_lists_conditions(self):
        resp = self.read_client.get('/api/conditions/', {'person_id': self.person.person_id})
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        ids = [r['condition_occurrence_id'] for r in resp.data]
        self.assertIn(70101, ids)

    def test_read_token_retrieves_single_condition(self):
        resp = self.read_client.get('/api/conditions/70101/')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data['condition_source_value'], 'Breast cancer')

    def test_read_token_lists_observations(self):
        resp = self.read_client.get('/api/observations/', {'person_id': self.person.person_id})
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        ids = [r['observation_id'] for r in resp.data]
        self.assertIn(70201, ids)

    def test_read_token_retrieves_single_observation(self):
        resp = self.read_client.get('/api/observations/70201/')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data['value_as_string'], 'ECOG 1')

    def test_read_token_lists_drug_exposures(self):
        resp = self.read_client.get('/api/drug-exposures/', {'person_id': self.person.person_id})
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        ids = [r['drug_exposure_id'] for r in resp.data]
        self.assertIn(70301, ids)

    def test_read_token_retrieves_single_drug_exposure(self):
        resp = self.read_client.get('/api/drug-exposures/70301/')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data['drug_source_value'], 'Trastuzumab')

    def test_read_token_lists_procedures(self):
        resp = self.read_client.get('/api/procedures/', {'person_id': self.person.person_id})
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        ids = [r['procedure_occurrence_id'] for r in resp.data]
        self.assertIn(70401, ids)

    def test_read_token_retrieves_single_procedure(self):
        resp = self.read_client.get('/api/procedures/70401/')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data['procedure_source_value'], 'Lumpectomy')

    def test_read_token_lists_patient_info(self):
        resp = self.read_client.get('/api/patient-info/')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

    def test_expired_token_returns_401_on_conditions(self):
        client = self._bearer(self.expired_token.token)
        resp = client.get('/api/conditions/')
        self.assertEqual(resp.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_no_token_returns_401_on_all_omop_endpoints(self):
        anon = APIClient()
        for url in ['/api/conditions/', '/api/observations/',
                    '/api/drug-exposures/', '/api/procedures/']:
            resp = anon.get(url)
            self.assertEqual(resp.status_code, status.HTTP_401_UNAUTHORIZED,
                             f'Expected 401 on {url} with no token')

    def test_person_id_filter_isolates_patient_data(self):
        # A second person with their own condition
        other_person = Person.objects.create(
            person_id=70002,
            year_of_birth=1990,
            gender_source_value='male',
            race_source_value='unknown',
            ethnicity_source_value='unknown',
        )
        ConditionOccurrence.objects.create(
            condition_occurrence_id=70102,
            person=other_person,
            condition_concept=self.condition_concept,
            condition_start_date=date(2024, 1, 1),
            condition_type_concept=self.type_concept,
            condition_source_value='Other patient condition',
        )
        resp = self.read_client.get('/api/conditions/', {'person_id': self.person.person_id})
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        ids = [r['condition_occurrence_id'] for r in resp.data]
        self.assertIn(70101, ids)
        self.assertNotIn(70102, ids)


class SmartServiceClientWriteTest(_SmartBase):
    """Service client writes OMOP records using a patient/*.write Bearer token
    and verifies PatientInfo is automatically refreshed."""

    def test_write_token_creates_condition(self):
        payload = {
            'condition_occurrence_id': 70501,
            'person': self.person.person_id,
            'condition_concept': self.condition_concept.concept_id,
            'condition_start_date': '2024-06-01',
            'condition_type_concept': self.type_concept.concept_id,
            'condition_source_value': 'Breast cancer recurrence',
        }
        resp = self.write_client.post('/api/conditions/', payload, format='json')
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)
        self.assertTrue(ConditionOccurrence.objects.filter(condition_occurrence_id=70501).exists())

    def test_write_token_updates_condition(self):
        cond = ConditionOccurrence.objects.create(
            condition_occurrence_id=70502,
            person=self.person,
            condition_concept=self.condition_concept,
            condition_start_date=date(2024, 1, 1),
            condition_type_concept=self.type_concept,
            condition_source_value='Initial diagnosis',
        )
        resp = self.write_client.patch(
            f'/api/conditions/{cond.condition_occurrence_id}/',
            {'condition_source_value': 'Confirmed diagnosis'},
            format='json',
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        cond.refresh_from_db()
        self.assertEqual(cond.condition_source_value, 'Confirmed diagnosis')

    def test_write_token_deletes_condition(self):
        cond = ConditionOccurrence.objects.create(
            condition_occurrence_id=70503,
            person=self.person,
            condition_concept=self.condition_concept,
            condition_start_date=date(2024, 2, 1),
            condition_type_concept=self.type_concept,
        )
        resp = self.write_client.delete(f'/api/conditions/{cond.condition_occurrence_id}/')
        self.assertEqual(resp.status_code, status.HTTP_204_NO_CONTENT)
        self.assertFalse(ConditionOccurrence.objects.filter(condition_occurrence_id=70503).exists())

    def test_write_token_creates_observation(self):
        from omop_core.models import Observation as OmopObservation
        payload = {
            'observation_id': 70601,
            'person': self.person.person_id,
            'observation_concept': self.condition_concept.concept_id,
            'observation_date': '2024-07-01',
            'observation_type_concept': self.type_concept.concept_id,
            'value_as_string': 'ECOG 0',
        }
        resp = self.write_client.post('/api/observations/', payload, format='json')
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)
        self.assertTrue(OmopObservation.objects.filter(observation_id=70601).exists())

    def test_write_token_creates_drug_exposure(self):
        payload = {
            'drug_exposure_id': 70701,
            'person': self.person.person_id,
            'drug_concept': self.drug_concept.concept_id,
            'drug_exposure_start_date': '2024-08-01',
            'drug_type_concept': self.type_concept.concept_id,
            'drug_source_value': 'Pertuzumab',
        }
        resp = self.write_client.post('/api/drug-exposures/', payload, format='json')
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)
        self.assertTrue(DrugExposure.objects.filter(drug_exposure_id=70701).exists())

    def test_write_token_creates_procedure(self):
        payload = {
            'procedure_occurrence_id': 70801,
            'person': self.person.person_id,
            'procedure_concept': self.condition_concept.concept_id,
            'procedure_date': '2024-09-10',
            'procedure_type_concept': self.type_concept.concept_id,
            'procedure_source_value': 'Sentinel node biopsy',
        }
        resp = self.write_client.post('/api/procedures/', payload, format='json')
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)
        self.assertTrue(ProcedureOccurrence.objects.filter(procedure_occurrence_id=70801).exists())

    def test_condition_write_triggers_patient_info_refresh(self):
        """Writing a ConditionOccurrence via OAuth must update PatientInfo.disease."""
        PatientInfo.objects.filter(person=self.person).delete()
        payload = {
            'condition_occurrence_id': 70901,
            'person': self.person.person_id,
            'condition_concept': self.condition_concept.concept_id,
            'condition_start_date': '2024-10-01',
            'condition_type_concept': self.type_concept.concept_id,
            'condition_source_value': 'Breast cancer',
        }
        self.write_client.post('/api/conditions/', payload, format='json')
        pi = PatientInfo.objects.filter(person=self.person).first()
        self.assertIsNotNone(pi, 'PatientInfo not created after condition POST')
        self.assertIsNotNone(pi.disease, 'PatientInfo.disease not populated after condition write')

    def test_drug_exposure_write_triggers_patient_info_refresh(self):
        """Writing a DrugExposure via OAuth must update PatientInfo therapy data."""
        PatientInfo.objects.filter(person=self.person).delete()
        payload = {
            'drug_exposure_id': 71001,
            'person': self.person.person_id,
            'drug_concept': self.drug_concept.concept_id,
            'drug_exposure_start_date': '2024-11-01',
            'drug_type_concept': self.type_concept.concept_id,
            'drug_source_value': 'Capecitabine',
        }
        self.write_client.post('/api/drug-exposures/', payload, format='json')
        pi = PatientInfo.objects.filter(person=self.person).first()
        self.assertIsNotNone(pi, 'PatientInfo not created after drug exposure POST')

    def test_delete_condition_triggers_patient_info_refresh(self):
        """Deleting a ConditionOccurrence via OAuth must re-derive PatientInfo."""
        cond = ConditionOccurrence.objects.create(
            condition_occurrence_id=71101,
            person=self.person,
            condition_concept=self.condition_concept,
            condition_start_date=date(2024, 12, 1),
            condition_type_concept=self.type_concept,
            condition_source_value='Temporary staging condition',
        )
        # Verify PatientInfo exists before deletion
        from omop_core.services.patient_info_service import refresh_patient_info
        refresh_patient_info(self.person)
        self.assertTrue(PatientInfo.objects.filter(person=self.person).exists())

        self.write_client.delete(f'/api/conditions/{cond.condition_occurrence_id}/')
        # PatientInfo must still exist and be updated (not deleted)
        self.assertTrue(
            PatientInfo.objects.filter(person=self.person).exists(),
            'PatientInfo should persist after a condition is deleted',
        )


class SmartPatientInfoReadOnlyTest(_SmartBase):
    """PatientInfo endpoints are read-only regardless of the OAuth scope."""

    def test_patient_info_put_returns_405(self):
        pi = PatientInfo.objects.filter(person=self.person).first()
        if pi is None:
            from omop_core.services.patient_info_service import refresh_patient_info
            pi = refresh_patient_info(self.person)
        resp = self.write_client.put(
            f'/api/patient-info/{self.person.person_id}/',
            {'disease': 'Should not be written directly'},
            format='json',
        )
        self.assertEqual(resp.status_code, status.HTTP_405_METHOD_NOT_ALLOWED)

    def test_patient_info_patch_returns_405(self):
        pi = PatientInfo.objects.filter(person=self.person).first()
        if pi is None:
            from omop_core.services.patient_info_service import refresh_patient_info
            pi = refresh_patient_info(self.person)
        resp = self.write_client.patch(
            f'/api/patient-info/{self.person.person_id}/',
            {'disease': 'Should not be written directly'},
            format='json',
        )
        self.assertEqual(resp.status_code, status.HTTP_405_METHOD_NOT_ALLOWED)

    def test_patient_info_delete_returns_405(self):
        resp = self.write_client.delete(f'/api/patient-info/{self.person.person_id}/')
        self.assertEqual(resp.status_code, status.HTTP_405_METHOD_NOT_ALLOWED)

    def test_patient_info_read_with_write_token_succeeds(self):
        resp = self.write_client.get('/api/patient-info/')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)


class SmartFhirUploadTest(_SmartBase):
    """Service client can bulk-ingest a patient via the FHIR upload endpoint
    using a write-scoped Bearer token."""

    def test_fhir_upload_with_write_token_succeeds(self):
        bundle = _make_fhir_bundle()
        bundle_bytes = json.dumps(bundle).encode('utf-8')
        fhir_file = io.BytesIO(bundle_bytes)
        fhir_file.name = 'service_bundle.json'
        resp = self.write_client.post(
            '/api/patient-info/upload_fhir/',
            {'file': fhir_file},
            format='multipart',
        )
        self.assertIn(resp.status_code, [status.HTTP_200_OK, status.HTTP_201_CREATED],
                      msg=f'FHIR upload failed: {resp.data}')

    def test_fhir_upload_creates_omop_records_and_patient_info(self):
        bundle = _make_fhir_bundle()
        bundle_bytes = json.dumps(bundle).encode('utf-8')
        fhir_file = io.BytesIO(bundle_bytes)
        fhir_file.name = 'service_bundle2.json'
        self.write_client.post(
            '/api/patient-info/upload_fhir/',
            {'file': fhir_file},
            format='multipart',
        )
        person = Person.objects.filter(family_name='Smith', given_name='Jane').first()
        self.assertIsNotNone(person, 'Person not created by FHIR upload via OAuth')
        pi = PatientInfo.objects.filter(person=person).first()
        self.assertIsNotNone(pi, 'PatientInfo not derived after FHIR upload via OAuth')
        self.assertIsNotNone(pi.disease)

    def test_fhir_upload_with_read_only_token_is_rejected(self):
        """upload_fhir requires patient/*.write scope — a read-only token must be rejected."""
        bundle = _make_fhir_bundle()
        bundle_bytes = json.dumps(bundle).encode('utf-8')
        fhir_file = io.BytesIO(bundle_bytes)
        fhir_file.name = 'service_bundle3.json'
        resp = self.read_client.post(
            '/api/patient-info/upload_fhir/',
            {'file': fhir_file},
            format='multipart',
        )
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)

    def test_fhir_upload_unauthenticated_is_rejected(self):
        """upload_fhir must reject requests with no credentials."""
        from rest_framework.test import APIClient as _APIClient
        anon = _APIClient()
        bundle = _make_fhir_bundle()
        bundle_bytes = json.dumps(bundle).encode('utf-8')
        fhir_file = io.BytesIO(bundle_bytes)
        fhir_file.name = 'service_bundle_anon.json'
        resp = anon.post(
            '/api/patient-info/upload_fhir/',
            {'file': fhir_file},
            format='multipart',
        )
        self.assertEqual(resp.status_code, status.HTTP_401_UNAUTHORIZED)


# ---------------------------------------------------------------------------
# HKI-AUTH-01: client_credentials grant — service-to-service token acquisition
# ---------------------------------------------------------------------------

class ClientCredentialsTokenTest(TestCase):
    """
    Verify that a confidential service client can obtain a Bearer token via
    POST /o/token/ with grant_type=client_credentials, then use it to call
    protected API endpoints.  No user session or browser redirect involved.
    """

    @classmethod
    def setUpTestData(cls):
        from oauth2_provider.models import Application
        _make_vocab_fixtures()

        cls.service_user = User.objects.create_user(
            username='svc_token_user', password='irrelevant'
        )
        cls.app = Application.objects.create(
            name='Test Service Client',
            client_id='test-service-client',
            client_secret='test-service-secret',
            client_type=Application.CLIENT_CONFIDENTIAL,
            authorization_grant_type=Application.GRANT_CLIENT_CREDENTIALS,
            user=cls.service_user,
        )

    def test_client_credentials_returns_access_token(self):
        """POST /o/token/ with client_credentials yields a Bearer token."""
        resp = self.client.post('/o/token/', {
            'grant_type': 'client_credentials',
            'client_id': self.app.client_id,
            'client_secret': 'test-service-secret',
            'scope': 'patient/*.read',
        })
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertIn('access_token', data)
        self.assertEqual(data['token_type'].lower(), 'bearer')

    def test_client_credentials_token_accesses_api(self):
        """Token obtained via client_credentials can call a protected endpoint."""
        token_resp = self.client.post('/o/token/', {
            'grant_type': 'client_credentials',
            'client_id': self.app.client_id,
            'client_secret': 'test-service-secret',
            'scope': 'patient/*.read',
        })
        token = token_resp.json()['access_token']

        api_client = APIClient()
        api_client.credentials(HTTP_AUTHORIZATION=f'Bearer {token}')
        resp = api_client.get('/api/conditions/')
        self.assertEqual(resp.status_code, 200)

    def test_wrong_secret_is_rejected(self):
        """Invalid client_secret must return 401."""
        resp = self.client.post('/o/token/', {
            'grant_type': 'client_credentials',
            'client_id': self.app.client_id,
            'client_secret': 'wrong-secret',
            'scope': 'patient/*.read',
        })
        self.assertEqual(resp.status_code, 401)

    def test_client_credentials_advertised_in_smart_config(self):
        """SMART discovery endpoint must advertise client_credentials grant."""
        resp = self.client.get('/.well-known/smart-configuration')
        self.assertEqual(resp.status_code, 200)
        self.assertIn('client_credentials', resp.json()['grant_types_supported'])
