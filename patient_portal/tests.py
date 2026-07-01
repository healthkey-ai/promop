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
import os
import tempfile
from datetime import date, timedelta

from patient_portal.models import Identity
from django.test import TestCase
from rest_framework import status
from rest_framework.test import APIClient

from omop_core.models import (
    Concept, ConceptClass, Domain, Vocabulary,
    Person, PatientInfo, ProvenanceRecord,
    ConditionOccurrence, DrugExposure, Measurement, ProcedureOccurrence,
    Relationship, ConceptRelationship, ConceptAncestor,
    SctEligibility,
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
        cls.admin = Identity.objects.create_superuser(
            email='admin@test.com', password='testpass'
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

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        _client = APIClient()
        _client.force_authenticate(user=cls.admin)
        bundle_bytes = json.dumps(_make_fhir_bundle()).encode('utf-8')
        fhir_file = io.BytesIO(bundle_bytes)
        fhir_file.name = 'test_bundle.json'
        cls._upload_response = _client.post(
            '/api/patient-info/upload_fhir/', {'file': fhir_file}, format='multipart'
        )
        cls._person = Person.objects.filter(family_name='Smith', given_name='Jane').first()
        assert cls._person is not None, 'Setup: person not found after upload'

    def test_upload_returns_success(self):
        self.assertIn(self._upload_response.status_code,
                      [status.HTTP_200_OK, status.HTTP_201_CREATED],
                      msg=f'Upload failed: {self._upload_response.data}')

    def test_person_created(self):
        self.assertIsNotNone(self._person, 'Person record not created for Jane Smith')
        self.assertEqual(self._person.year_of_birth, 1975)
        self.assertEqual(self._person.month_of_birth, 3)
        self.assertEqual(self._person.day_of_birth, 15)

    def test_condition_occurrence_created(self):
        """A ConditionOccurrence row should exist for the breast cancer Condition resource."""
        conditions = ConditionOccurrence.objects.filter(person=self._person)
        self.assertGreater(conditions.count(), 0, 'No ConditionOccurrence created')
        self.assertEqual(conditions.first().condition_start_date, date(2022, 1, 15))

    def test_measurements_created_for_each_observation(self):
        """A Measurement row should exist for each LOINC-coded Observation."""
        measurements = Measurement.objects.filter(person=self._person)
        self.assertGreaterEqual(measurements.count(), 3,
                                f'Expected ≥3 Measurement rows, got {measurements.count()}')
        source_values = list(measurements.values_list('measurement_source_value', flat=True))
        # source_value is now the LOINC code (718-7) when available, not the display name
        self.assertTrue(
            any(('Hemoglobin' in (v or '') or v == '718-7') for v in source_values),
            f'Hemoglobin measurement missing. source_values={source_values}',
        )

    def test_drug_exposures_created_per_lot(self):
        """One DrugExposure per MedicationStatement (therapy line)."""
        drug_exposures = DrugExposure.objects.filter(person=self._person)
        self.assertEqual(drug_exposures.count(), 2,
                         f'Expected 2 DrugExposure rows, got {drug_exposures.count()}')
        source_values = set(drug_exposures.values_list('drug_source_value', flat=True))
        self.assertIn('AC-T', source_values)
        self.assertIn('Kadcyla', source_values)

    def test_episodes_created_with_correct_lot_numbers(self):
        """Episode rows should exist with the correct episode_number for each LOT."""
        episodes = Episode.objects.filter(person=self._person).order_by('episode_number')
        self.assertEqual(episodes.count(), 2,
                         f'Expected 2 Episode rows, got {episodes.count()}')
        self.assertEqual(episodes[0].episode_number, 1)
        self.assertEqual(episodes[1].episode_number, 2)
        self.assertEqual(episodes[0].episode_start_date, date(2022, 3, 1))
        self.assertEqual(episodes[0].episode_end_date,   date(2022, 9, 1))
        self.assertIsNone(episodes[1].episode_end_date,  'LOT 2 should have no end date')

    def test_episode_events_link_drug_exposures_to_episodes(self):
        """Each Episode should have at least one EpisodeEvent linking it to a DrugExposure."""
        for episode in Episode.objects.filter(person=self._person):
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

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        _client = APIClient()
        _client.force_authenticate(user=cls.admin)
        bundle_bytes = json.dumps(_make_fhir_bundle()).encode('utf-8')
        fhir_file = io.BytesIO(bundle_bytes)
        fhir_file.name = 'test_bundle.json'
        _client.post('/api/patient-info/upload_fhir/', {'file': fhir_file}, format='multipart')
        cls._person = Person.objects.filter(family_name='Smith', given_name='Jane').first()
        assert cls._person is not None, 'Setup: person not found after upload'
        cls._pi = PatientInfo.objects.get(person=cls._person)

    def test_patient_info_created(self):
        self.assertIsNotNone(self._pi, 'PatientInfo not created for uploaded patient')

    def test_disease_populated_from_condition(self):
        self.assertIsNotNone(self._pi.disease, 'PatientInfo.disease not populated')

    def test_demographics_populated(self):
        self.assertEqual(self._pi.date_of_birth, date(1975, 3, 15))
        self.assertIsNotNone(self._pi.gender)

    def test_hemoglobin_populated_from_loinc_718_7(self):
        self.assertIsNotNone(self._pi.hemoglobin_g_dl)
        self.assertAlmostEqual(float(self._pi.hemoglobin_g_dl), 11.2, places=1)

    def test_wbc_populated_from_loinc_6690_2(self):
        self.assertIsNotNone(self._pi.wbc_count_thousand_per_ul)
        self.assertAlmostEqual(float(self._pi.wbc_count_thousand_per_ul), 4.5, places=1)

    def test_creatinine_populated_from_loinc_2160_0(self):
        self.assertIsNotNone(self._pi.serum_creatinine_mg_dl)
        self.assertAlmostEqual(float(self._pi.serum_creatinine_mg_dl), 0.9, places=1)

    def test_first_line_therapy_from_medication_statement(self):
        self.assertEqual(self._pi.first_line_therapy, 'AC-T')
        self.assertEqual(self._pi.first_line_start_date, date(2022, 3, 1))
        self.assertEqual(self._pi.first_line_end_date,   date(2022, 9, 1))
        self.assertEqual(self._pi.first_line_outcome,    'CR')

    def test_second_line_therapy_from_medication_statement(self):
        self.assertEqual(self._pi.second_line_therapy,    'Kadcyla')
        self.assertEqual(self._pi.second_line_start_date, date(2023, 1, 15))
        self.assertIsNone(self._pi.second_line_end_date,  'Open-ended LOT 2 should have no end date')


# ---------------------------------------------------------------------------
# 3. UI API view tests — data visible through endpoints the frontend uses
# ---------------------------------------------------------------------------

class UIViewsReflectUploadedDataTest(FhirUploadBase):
    """GET requests to UI-facing REST endpoints should return the data
    written by the FHIR upload pipeline."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        _client = APIClient()
        _client.force_authenticate(user=cls.admin)
        bundle_bytes = json.dumps(_make_fhir_bundle()).encode('utf-8')
        fhir_file = io.BytesIO(bundle_bytes)
        fhir_file.name = 'test_bundle.json'
        _client.post('/api/patient-info/upload_fhir/', {'file': fhir_file}, format='multipart')
        cls._person = Person.objects.filter(family_name='Smith', given_name='Jane').first()
        assert cls._person is not None, 'Setup: person not found after upload'
        cls._pid = cls._person.person_id

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
        # source_value is now the LOINC code (718-7) when available, not the display name
        self.assertTrue(any(('Hemoglobin' in v or v == '718-7') for v in source_values),
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

        cls.user = Identity.objects.create_user(
            email='smartuser@test.com', password='smartpass'
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

        cls.foundation_user = Identity.objects.create_user(
            email='foundation_svc@test.com', password='foundation_pass'
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

        # Organization + ApplicationOrganization so get_request_org() returns an org
        # (without this, access checks fall through to can_access_patient which rejects
        # foundation_user because it has no PatientUser/GroupAccess).
        from omop_core.models import Organization, ApplicationOrganization
        cls.organization = Organization.objects.create(
            name='SMART Test Org',
            slug='smart-test-org',
        )
        ApplicationOrganization.objects.create(
            application=cls.app,
            organization=cls.organization,
        )
        # PatientInfo for cls.person, scoped to the test org.  Subclasses that
        # create OMOP records for cls.person (conditions, measurements, etc.)
        # need this to exist so _ProvenanceMixin.perform_create org-check passes.
        cls.patient_info = PatientInfo.objects.create(
            person=cls.person,
            organization=cls.organization,
        )

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

    def test_measurement_write_triggers_patient_info_refresh(self):
        """Writing a Measurement via OAuth must update the corresponding PatientInfo lab field."""
        PatientInfo.objects.filter(person=self.person).delete()
        hgb_concept = Concept.objects.filter(concept_code='718-7').first()
        if not hgb_concept:
            self.skipTest('Hemoglobin concept not in test DB')
        payload = {
            'person': self.person.person_id,
            'measurement_concept': hgb_concept.concept_id,
            'measurement_date': '2024-10-15',
            'measurement_type_concept': self.type_concept.concept_id,
            'value_as_number': 11.5,
            'measurement_source_value': '718-7',
        }
        resp = self.write_client.post('/api/measurements/', payload, format='json')
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)
        pi = PatientInfo.objects.filter(person=self.person).first()
        self.assertIsNotNone(pi, 'PatientInfo not created after measurement POST')
        self.assertEqual(float(pi.hemoglobin_g_dl), 11.5)

    def test_cross_org_write_rejected(self):
        """An org-scoped token for Org A must not write OMOP records for Org B's patient."""
        from oauth2_provider.models import Application, AccessToken
        from omop_core.models import Organization, ApplicationOrganization
        from django.utils import timezone as tz
        import datetime

        # Create Org A with a write-scoped token
        org_a = Organization.objects.create(name='Org A Cross-write Test', slug='org-a-cross-write')
        user_a = Identity.objects.create_user(email='svc_cross_a_write@test.com', password='x')
        app_a = Application.objects.create(
            name='Org A Cross Write App',
            client_id='cross-a-write-client',
            client_type=Application.CLIENT_CONFIDENTIAL,
            authorization_grant_type=Application.GRANT_CLIENT_CREDENTIALS,
            user=user_a,
        )
        ApplicationOrganization.objects.create(application=app_a, organization=org_a)
        write_token_a = AccessToken.objects.create(
            user=user_a,
            application=app_a,
            token='cross-write-token-org-a',
            expires=tz.now() + datetime.timedelta(hours=1),
            scope='patient/*.write',
        )

        # Create Org B with a patient
        org_b = Organization.objects.create(name='Org B Cross-write Test', slug='org-b-cross-write')
        person_b = Person.objects.create(
            person_id=72001,
            given_name='Bob',
            family_name='OrgBCross',
            year_of_birth=1975,
            gender_source_value='male',
            race_source_value='unknown',
            ethnicity_source_value='unknown',
        )
        PatientInfo.objects.create(person=person_b, organization=org_b)

        # Org A token tries to write for Org B's patient — must be rejected
        client_a = APIClient()
        client_a.credentials(HTTP_AUTHORIZATION=f'Bearer {write_token_a.token}')
        payload = {
            'person': person_b.person_id,
            'condition_concept': self.condition_concept.concept_id,
            'condition_start_date': '2024-01-01',
            'condition_type_concept': self.type_concept.concept_id,
        }
        resp = client_a.post('/api/conditions/', payload, format='json')
        self.assertIn(resp.status_code, [403, 404])


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

    def test_patient_info_patch_succeeds_with_write_token(self):
        """PATCH is now supported — write-through to OMOP was added in HKI-PDS-01."""
        PatientInfo.objects.get_or_create(person=self.person, defaults={'organization': self.organization})
        resp = self.write_client.patch(
            f'/api/patient-info/{self.person.person_id}/',
            {'disease': 'Updated disease'},
            format='json',
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

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

    def _upload(self, name):
        bundle = _make_fhir_bundle()
        fhir_file = io.BytesIO(json.dumps(bundle).encode())
        fhir_file.name = name
        return self.write_client.post(
            '/api/patient-info/upload_fhir/',
            {'file': fhir_file},
            format='multipart',
        )

    def test_fhir_upload_upsert_no_duplicates(self):
        """Re-uploading the same bundle must not create duplicate records."""
        from omop_core.models import Person, Measurement, ConditionOccurrence

        resp1 = self._upload('bundle_upsert_1.json')
        self.assertIn(resp1.status_code, [200, 201])
        self.assertEqual(resp1.json()['created_count'], 1)

        person_count_after_first = Person.objects.count()
        measurement_count_after_first = Measurement.objects.count()
        condition_count_after_first = ConditionOccurrence.objects.count()

        resp2 = self._upload('bundle_upsert_2.json')
        self.assertIn(resp2.status_code, [200, 201])
        data2 = resp2.json()
        # Second upload should update, not create
        self.assertEqual(data2['created_count'], 0)
        self.assertEqual(data2['updated_count'], 1)

        # Record counts must not increase
        self.assertEqual(Person.objects.count(), person_count_after_first)
        self.assertEqual(Measurement.objects.count(), measurement_count_after_first)
        self.assertEqual(ConditionOccurrence.objects.count(), condition_count_after_first)

    def test_fhir_upload_response_includes_record_ids(self):
        """Response must include per-patient breakdown of created OMOP record IDs."""
        resp = self._upload('bundle_ids.json')
        self.assertIn(resp.status_code, [200, 201])
        data = resp.json()

        self.assertIn('patients', data)
        self.assertEqual(len(data['patients']), 1)

        pt = data['patients'][0]
        self.assertIn('person_id', pt)
        self.assertIn('patient_info_id', pt)
        self.assertIn('measurement_ids', pt)
        self.assertIn('condition_ids', pt)
        self.assertIn('drug_exposure_ids', pt)
        self.assertIn('procedure_ids', pt)
        self.assertIn('episode_ids', pt)
        self.assertIn('episode_event_ids', pt)

        # The bundle has 3 observations → ≥1 measurement, 1 condition, 2 drug exposures
        self.assertGreater(len(pt['measurement_ids']), 0)
        self.assertGreater(len(pt['condition_ids']), 0)
        self.assertGreater(len(pt['drug_exposure_ids']), 0)

        # Verify IDs actually exist in DB
        person = Person.objects.get(person_id=pt['person_id'])
        self.assertIsNotNone(person)
        pi = PatientInfo.objects.get(pk=pt['patient_info_id'])
        self.assertIsNotNone(pi)
        for mid in pt['measurement_ids']:
            self.assertTrue(Measurement.objects.filter(measurement_id=mid).exists())
        for cid in pt['condition_ids']:
            self.assertTrue(ConditionOccurrence.objects.filter(condition_occurrence_id=cid).exists())


# ---------------------------------------------------------------------------
# 8. DrugClassification tests — HemOnc vocabulary-backed _classify_drug()
# ---------------------------------------------------------------------------

class DrugClassificationTest(TestCase):
    """Test _classify_drug() HemOnc two-step lookup + DRUG_SUBTYPE_MAP fallback."""

    def setUp(self):
        _make_vocab_fixtures()
        self.hemonc_vocab, _ = Vocabulary.objects.get_or_create(
            vocabulary_id='HemOnc',
            defaults={'vocabulary_name': 'HemOnc Oncology', 'vocabulary_concept_id': 0},
        )
        self.rxnorm_vocab, _ = Vocabulary.objects.get_or_create(
            vocabulary_id='RxNorm',
            defaults={'vocabulary_name': 'RxNorm', 'vocabulary_concept_id': 0},
        )
        self.domain_drug = Domain.objects.get(domain_id='Drug')
        self.cc, _ = ConceptClass.objects.get_or_create(
            concept_class_id='HemOnc Class',
            defaults={'concept_class_name': 'HemOnc Class', 'concept_class_concept_id': 0},
        )
        self.cc_ing, _ = ConceptClass.objects.get_or_create(
            concept_class_id='Ingredient',
            defaults={'concept_class_name': 'Ingredient', 'concept_class_concept_id': 0},
        )

        self.pi_class = Concept.objects.create(
            concept_id=8800001, concept_name='Proteasome inhibitor',
            domain=self.domain_drug, vocabulary=self.hemonc_vocab, concept_class=self.cc,
            concept_code='PI', valid_start_date=date(1970, 1, 1), valid_end_date=date(2099, 12, 31),
        )
        self.bort_hemonc = Concept.objects.create(
            concept_id=8800002, concept_name='bortezomib',
            domain=self.domain_drug, vocabulary=self.hemonc_vocab, concept_class=self.cc,
            concept_code='HO-Bort', valid_start_date=date(1970, 1, 1), valid_end_date=date(2099, 12, 31),
        )
        self.cart_class = Concept.objects.create(
            concept_id=8800003, concept_name='CAR T-cell therapy',
            domain=self.domain_drug, vocabulary=self.hemonc_vocab, concept_class=self.cc,
            concept_code='CART', valid_start_date=date(1970, 1, 1), valid_end_date=date(2099, 12, 31),
        )
        self.cart_drug = Concept.objects.create(
            concept_id=8800004, concept_name='idecabtagene vicleucel',
            domain=self.domain_drug, vocabulary=self.hemonc_vocab, concept_class=self.cc,
            concept_code='IdecelHemOnc', valid_start_date=date(1970, 1, 1), valid_end_date=date(2099, 12, 31),
        )
        self.bort_rxnorm = Concept.objects.create(
            concept_id=8810001, concept_name='bortezomib',
            domain=self.domain_drug, vocabulary=self.rxnorm_vocab, concept_class=self.cc_ing,
            concept_code='1421', standard_concept='S',
            valid_start_date=date(1970, 1, 1), valid_end_date=date(2099, 12, 31),
        )

        self.maps_to, _ = Relationship.objects.get_or_create(
            relationship_id='Maps to',
            defaults={
                'relationship_name': 'Maps to', 'is_hierarchical': 0,
                'defines_ancestry': 0, 'reverse_relationship_id': 'Mapped from',
                'relationship_concept_id': 0,
            },
        )
        ConceptRelationship.objects.get_or_create(
            concept_1=self.bort_rxnorm, concept_2=self.bort_hemonc, relationship=self.maps_to,
            defaults={'valid_start_date': date(1970, 1, 1), 'valid_end_date': date(2099, 12, 31)},
        )
        ConceptRelationship.objects.get_or_create(
            concept_1=self.cart_drug, concept_2=self.cart_class, relationship=self.maps_to,
            defaults={'valid_start_date': date(1970, 1, 1), 'valid_end_date': date(2099, 12, 31)},
        )
        ConceptAncestor.objects.get_or_create(
            ancestor_concept=self.pi_class, descendant_concept=self.bort_hemonc,
            defaults={'min_levels_of_separation': 1, 'max_levels_of_separation': 1},
        )
        ConceptAncestor.objects.get_or_create(
            ancestor_concept=self.cart_class, descendant_concept=self.cart_drug,
            defaults={'min_levels_of_separation': 0, 'max_levels_of_separation': 0},
        )

    def test_rxnorm_bortezomib_classifies_as_myeloma(self):
        from omop_core.services.lot_inference_service import _classify_drug
        result = _classify_drug(self.bort_rxnorm.concept_id, 'bortezomib')
        self.assertEqual(result, 'myeloma')

    def test_cart_drug_classifies_as_cart(self):
        from omop_core.services.lot_inference_service import _classify_drug
        result = _classify_drug(self.cart_drug.concept_id, 'idecabtagene vicleucel')
        self.assertEqual(result, 'cart')

    def test_zero_concept_id_falls_back_to_drug_subtype_map(self):
        from omop_core.services.lot_inference_service import _classify_drug
        result = _classify_drug(0, 'bortezomib')
        self.assertEqual(result, 'myeloma')  # bortezomib is in DRUG_SUBTYPE_MAP

    def test_novel_drug_not_in_hemonc_returns_mixed(self):
        from omop_core.services.lot_inference_service import _classify_drug
        novel = Concept.objects.create(
            concept_id=8899999, concept_name='noveldrugxyz',
            domain=self.domain_drug, vocabulary=self.rxnorm_vocab, concept_class=self.cc_ing,
            concept_code='NOVEL99', valid_start_date=date(1970, 1, 1), valid_end_date=date(2099, 12, 31),
        )
        result = _classify_drug(novel.concept_id, 'noveldrugxyz')
        self.assertEqual(result, 'mixed')


# ---------------------------------------------------------------------------
# Task 2: ArtemisHemOncLotTest — integration: HemOnc-backed LOT classification
# ---------------------------------------------------------------------------

class ArtemisHemOncLotTest(TestCase):
    """Integration: infer_lot_for_person classifies brand-name drug via HemOnc."""

    def setUp(self):
        _make_vocab_fixtures()
        self.hemonc_vocab, _ = Vocabulary.objects.get_or_create(
            vocabulary_id='HemOnc',
            defaults={'vocabulary_name': 'HemOnc Oncology', 'vocabulary_concept_id': 0},
        )
        self.rxnorm_vocab, _ = Vocabulary.objects.get_or_create(
            vocabulary_id='RxNorm',
            defaults={'vocabulary_name': 'RxNorm', 'vocabulary_concept_id': 0},
        )
        self.domain_drug = Domain.objects.get(domain_id='Drug')
        self.cc_ing, _ = ConceptClass.objects.get_or_create(
            concept_class_id='Ingredient',
            defaults={'concept_class_name': 'Ingredient', 'concept_class_concept_id': 0},
        )
        self.cc_hemonc, _ = ConceptClass.objects.get_or_create(
            concept_class_id='HemOnc Class',
            defaults={'concept_class_name': 'HemOnc Class', 'concept_class_concept_id': 0},
        )

        # HemOnc hierarchy: Proteasome inhibitor → bortezomib (HemOnc)
        self.pi_class = Concept.objects.create(
            concept_id=9900101, concept_name='Proteasome inhibitor',
            domain=self.domain_drug, vocabulary=self.hemonc_vocab, concept_class=self.cc_hemonc,
            concept_code='PI', valid_start_date=date(1970, 1, 1), valid_end_date=date(2099, 12, 31),
        )
        self.bort_hemonc = Concept.objects.create(
            concept_id=9900102, concept_name='bortezomib',
            domain=self.domain_drug, vocabulary=self.hemonc_vocab, concept_class=self.cc_hemonc,
            concept_code='HO-Bort', valid_start_date=date(1970, 1, 1), valid_end_date=date(2099, 12, 31),
        )
        self.bort_rxnorm = Concept.objects.create(
            concept_id=9900103, concept_name='bortezomib',
            domain=self.domain_drug, vocabulary=self.rxnorm_vocab, concept_class=self.cc_ing,
            concept_code='1421', standard_concept='S',
            valid_start_date=date(1970, 1, 1), valid_end_date=date(2099, 12, 31),
        )

        maps_to, _ = Relationship.objects.get_or_create(
            relationship_id='Maps to',
            defaults={
                'relationship_name': 'Maps to', 'is_hierarchical': 0,
                'defines_ancestry': 0, 'reverse_relationship_id': 'Mapped from',
                'relationship_concept_id': 0,
            },
        )
        ConceptRelationship.objects.get_or_create(
            concept_1=self.bort_rxnorm, concept_2=self.bort_hemonc, relationship=maps_to,
            defaults={'valid_start_date': date(1970, 1, 1), 'valid_end_date': date(2099, 12, 31)},
        )
        ConceptAncestor.objects.get_or_create(
            ancestor_concept=self.pi_class, descendant_concept=self.bort_hemonc,
            defaults={'min_levels_of_separation': 1, 'max_levels_of_separation': 1},
        )

        from omop_core.models import Person, DrugExposure
        self.person = Person.objects.create(
            person_id=7700001,
            gender_concept_id=8532,
            year_of_birth=1960,
            race_concept_id=0,
            ethnicity_concept_id=0,
        )
        self.drug_type, _ = Concept.objects.get_or_create(
            concept_id=38000177,
            defaults={
                'concept_name': 'Prescription written',
                'domain': self.domain_drug,
                'vocabulary': self.rxnorm_vocab,
                'concept_class': self.cc_ing,
                'concept_code': '38000177',
                'valid_start_date': date(1970, 1, 1),
                'valid_end_date': date(2099, 12, 31),
            },
        )
        DrugExposure.objects.create(
            drug_exposure_id=9900001,
            person=self.person,
            drug_concept=self.bort_rxnorm,
            drug_source_value='Velcade',
            drug_type_concept=self.drug_type,
            drug_exposure_start_date=date(2023, 1, 15),
            drug_exposure_end_date=date(2023, 4, 15),
        )

    def test_brand_name_drug_classified_via_hemonc(self):
        """Velcade with RxNorm concept_id → infer_lot_for_person returns a LOT."""
        from omop_core.services.lot_inference_service import infer_lot_for_person
        lots = infer_lot_for_person(self.person, force=True, dry_run=True)
        self.assertGreater(len(lots), 0, 'Expected at least one LOT')
        self.assertNotEqual(lots[0].regimen_name, '')

    def test_novel_agent_no_hemonc_mapping_returns_mixed(self):
        """Drug with concept_id but no HemOnc mapping → _classify_drug returns mixed."""
        from omop_core.services.lot_inference_service import _classify_drug
        novel = Concept.objects.create(
            concept_id=9999999, concept_name='talquetamab',
            domain=self.domain_drug, vocabulary=self.rxnorm_vocab, concept_class=self.cc_ing,
            concept_code='TALQ99', valid_start_date=date(1970, 1, 1), valid_end_date=date(2099, 12, 31),
        )
        self.assertEqual(_classify_drug(novel.concept_id, 'talquetamab'), 'mixed')

    def test_infer_lot_is_callable_and_returns_list(self):
        """Smoke test: infer_lot_for_person is callable, returns list."""
        from omop_core.services.lot_inference_service import infer_lot_for_person
        lots = infer_lot_for_person(self.person, force=True, dry_run=True)
        self.assertIsInstance(lots, list)


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

        cls.service_user = Identity.objects.create_user(
            email='svc_token_user@test.com', password='irrelevant'
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


# ---------------------------------------------------------------------------
# Multi-tenant isolation tests (HKI-SEC-04 / issue #36)
# ---------------------------------------------------------------------------

class MultiTenantIsolationTest(_SmartBase):
    """Org-scoped tokens must not see another org's patients."""

    @classmethod
    def setUpTestData(cls):
        from oauth2_provider.models import Application, AccessToken
        from omop_core.models import Organization, ApplicationOrganization
        from django.utils import timezone as tz
        import datetime

        # Inherits vocab + app + tokens + person(70001) from _SmartBase
        super().setUpTestData()

        # --- Org A ---
        cls.org_a = Organization.objects.create(name='Org A', slug='org-a')
        cls.user_a = Identity.objects.create_user(email='svc_org_a@test.com', password='x')
        cls.app_a = Application.objects.create(
            name='Org A App',
            client_id='org-a-client',
            client_type=Application.CLIENT_CONFIDENTIAL,
            authorization_grant_type=Application.GRANT_CLIENT_CREDENTIALS,
            user=cls.user_a,
        )
        ApplicationOrganization.objects.create(application=cls.app_a, organization=cls.org_a)
        cls.token_a = AccessToken.objects.create(
            user=cls.user_a,
            application=cls.app_a,
            token='org-a-read-token',
            expires=tz.now() + datetime.timedelta(hours=1),
            scope='patient/*.read',
        )

        # --- Org B ---
        cls.org_b = Organization.objects.create(name='Org B', slug='org-b')
        cls.user_b = Identity.objects.create_user(email='svc_org_b@test.com', password='x')
        cls.app_b = Application.objects.create(
            name='Org B App',
            client_id='org-b-client',
            client_type=Application.CLIENT_CONFIDENTIAL,
            authorization_grant_type=Application.GRANT_CLIENT_CREDENTIALS,
            user=cls.user_b,
        )
        ApplicationOrganization.objects.create(application=cls.app_b, organization=cls.org_b)
        cls.token_b = AccessToken.objects.create(
            user=cls.user_b,
            application=cls.app_b,
            token='org-b-read-token',
            expires=tz.now() + datetime.timedelta(hours=1),
            scope='patient/*.read',
        )

        # --- Patients (person IDs distinct from _SmartBase's 70001) ---
        cls.person_a = Person.objects.create(
            person_id=80001,
            given_name='Alice',
            family_name='OrgA',
            year_of_birth=1970,
            gender_source_value='female',
            race_source_value='unknown',
            ethnicity_source_value='unknown',
        )
        cls.patient_a = PatientInfo.objects.create(
            person=cls.person_a,
            organization=cls.org_a,
            disease='Breast Cancer',
        )

        cls.person_b = Person.objects.create(
            person_id=80002,
            given_name='Bob',
            family_name='OrgB',
            year_of_birth=1975,
            gender_source_value='male',
            race_source_value='unknown',
            ethnicity_source_value='unknown',
        )
        cls.patient_b = PatientInfo.objects.create(
            person=cls.person_b,
            organization=cls.org_b,
            disease='Lung Cancer',
        )

    def _client(self, token_str):
        c = APIClient()
        c.credentials(HTTP_AUTHORIZATION=f'Bearer {token_str}')
        return c

    def test_org_a_token_sees_only_org_a_patient_info(self):
        """Org A token must not return Org B's PatientInfo records."""
        resp = self._client(self.token_a.token).get('/api/patient-info/')
        self.assertEqual(resp.status_code, 200)
        ids = [p['id'] for p in resp.json()]
        self.assertIn(self.patient_a.id, ids)
        self.assertNotIn(self.patient_b.id, ids)

    def test_org_b_token_sees_only_org_b_patient_info(self):
        """Org B token must not return Org A's PatientInfo records."""
        resp = self._client(self.token_b.token).get('/api/patient-info/')
        self.assertEqual(resp.status_code, 200)
        ids = [p['id'] for p in resp.json()]
        self.assertIn(self.patient_b.id, ids)
        self.assertNotIn(self.patient_a.id, ids)

    def test_org_a_token_cannot_retrieve_org_b_patient_detail(self):
        """Org A token must receive 404 for Org B's patient detail (AUTH-04)."""
        resp = self._client(self.token_a.token).get(f'/api/patient-info/{self.person_b.person_id}/')
        self.assertEqual(resp.status_code, 404)

    def test_org_a_token_can_retrieve_own_patient_detail(self):
        """Org A token must be able to retrieve its own patient detail (AUTH-04)."""
        resp = self._client(self.token_a.token).get(f'/api/patient-info/{self.person_a.person_id}/')
        self.assertEqual(resp.status_code, 200)
        self.assertIn('patient_info', resp.json())

    def test_org_a_token_sees_only_org_a_omop_conditions(self):
        """Org A token must not see ConditionOccurrences belonging to Org B's patient."""
        condition_concept = Concept.objects.get(concept_id=4112853)
        type_concept = Concept.objects.get(concept_id=32817)
        ConditionOccurrence.objects.create(
            condition_occurrence_id=80101,
            person=self.person_a,
            condition_concept=condition_concept,
            condition_start_date=date(2023, 1, 10),
            condition_type_concept=type_concept,
        )
        ConditionOccurrence.objects.create(
            condition_occurrence_id=80102,
            person=self.person_b,
            condition_concept=condition_concept,
            condition_start_date=date(2023, 2, 15),
            condition_type_concept=type_concept,
        )
        resp = self._client(self.token_a.token).get('/api/conditions/', {'person_id': 80002})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(resp.json()), 0, "Org A must not see Org B's conditions even with explicit person_id")

    def test_superuser_session_sees_all_patients(self):
        """Superuser session auth bypasses org scoping and sees all patients."""
        su = Identity.objects.create_superuser(email='su@test.com', password='su_pass')
        c = APIClient()
        c.force_authenticate(user=su)
        resp = c.get('/api/patient-info/')
        self.assertEqual(resp.status_code, 200)
        ids = [p['id'] for p in resp.json()]
        self.assertIn(self.patient_a.id, ids)
        self.assertIn(self.patient_b.id, ids)

    def test_bulk_delete_org_scoping(self):
        """Org A write token must not be able to delete Org B's patient via bulk_delete."""
        from oauth2_provider.models import AccessToken
        from django.utils import timezone as tz
        import datetime
        write_token_a = AccessToken.objects.create(
            user=self.user_a,
            application=self.app_a,
            token='org-a-bulk-delete-write-token',
            expires=tz.now() + datetime.timedelta(hours=1),
            scope='patient/*.write',
        )
        client_a = self._client(write_token_a.token)
        resp = client_a.delete(
            '/api/patient-info/bulk_delete/',
            {'person_ids': [self.person_b.person_id]},
            format='json',
        )
        self.assertEqual(resp.status_code, 200)
        # Must report "not found" — not a successful delete
        self.assertEqual(resp.data.get('deleted_count'), 0)
        self.assertEqual(len(resp.data.get('errors', [])), 1)
        # Org B's person must still exist
        from omop_core.models import Person as P
        self.assertTrue(P.objects.filter(person_id=self.person_b.person_id).exists())


# ---------------------------------------------------------------------------
# PatientInfo PATCH write-through tests (HKI-PDS-01 / issue #59)
# ---------------------------------------------------------------------------

class PatientInfoPatchWriteThroughTest(_SmartBase):
    """PATCH /api/patient-info/{person_id}/ must update PatientInfo AND create a Measurement."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        # cls.patient_info already created by _SmartBase; just set disease.
        PatientInfo.objects.filter(person=cls.person).update(disease='Breast Cancer')
        cls.patient_info = PatientInfo.objects.get(person=cls.person)

    def test_patch_updates_patient_info(self):
        """PATCH updates the PatientInfo field value."""
        resp = self.write_client.patch(
            f'/api/patient-info/{self.person.person_id}/',
            {'hemoglobin_g_dl': '12.5'},
            format='json',
        )
        self.assertEqual(resp.status_code, 200)
        self.patient_info.refresh_from_db()
        self.assertAlmostEqual(float(self.patient_info.hemoglobin_g_dl), 12.5, places=1)

    def test_patch_creates_measurement_record(self):
        """PATCH a lab field creates a Measurement row with the correct LOINC concept."""
        resp = self.write_client.patch(
            f'/api/patient-info/{self.person.person_id}/',
            {'hemoglobin_g_dl': '11.0'},
            format='json',
        )
        self.assertEqual(resp.status_code, 200)
        m = Measurement.objects.filter(
            person=self.person,
            measurement_source_value='718-7',
        ).first()
        self.assertIsNotNone(m, 'No Measurement record created for hemoglobin_g_dl patch')
        self.assertAlmostEqual(float(m.value_as_number), 11.0, places=1)

    def test_patch_upserts_existing_measurement(self):
        """Patching the same field twice updates the existing Measurement rather than duplicating it."""
        self.write_client.patch(
            f'/api/patient-info/{self.person.person_id}/',
            {'wbc_count_thousand_per_ul': '5.0'},
            format='json',
        )
        self.write_client.patch(
            f'/api/patient-info/{self.person.person_id}/',
            {'wbc_count_thousand_per_ul': '6.2'},
            format='json',
        )
        count = Measurement.objects.filter(
            person=self.person,
            measurement_source_value='6690-2',
        ).count()
        self.assertEqual(count, 1, 'Duplicate Measurement rows created on second patch')
        m = Measurement.objects.get(
            person=self.person,
            measurement_source_value='6690-2',
        )
        self.assertAlmostEqual(float(m.value_as_number), 6.2, places=1)

    def test_patch_non_lab_field_does_not_create_measurement(self):
        """Patching a non-lab field (e.g. disease) must not create a Measurement row."""
        before = Measurement.objects.filter(person=self.person).count()
        self.write_client.patch(
            f'/api/patient-info/{self.person.person_id}/',
            {'disease': 'Lung Cancer'},
            format='json',
        )
        after = Measurement.objects.filter(person=self.person).count()
        self.assertEqual(before, after)

    def test_patch_requires_write_scope(self):
        """Read-only token must be rejected with 403."""
        resp = self.read_client.patch(
            f'/api/patient-info/{self.person.person_id}/',
            {'hemoglobin_g_dl': '10.0'},
            format='json',
        )
        self.assertEqual(resp.status_code, 403)


# ---------------------------------------------------------------------------
# Provenance tests (HKI-PDS-01 / issues #57 + #61)
# ---------------------------------------------------------------------------

class ProvenancePatchTest(_SmartBase):
    """PATCH with provenance headers creates ProvenanceRecord entries."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        # cls.patient_info already created by _SmartBase; just set disease.
        PatientInfo.objects.filter(person=cls.person).update(disease='Breast Cancer')
        cls.patient_info = PatientInfo.objects.get(person=cls.person)

    def test_patch_with_source_creates_provenance_for_patient_info(self):
        resp = self.write_client.patch(
            f'/api/patient-info/{self.person.person_id}/',
            {'disease': 'Lung Cancer', 'source': 'EHR_SYNC', 'source_user_id': 'svc-123'},
            format='json',
        )
        self.assertEqual(resp.status_code, 200)
        p = ProvenanceRecord.objects.filter(
            object_id=self.patient_info.pk,
        ).first()
        self.assertIsNotNone(p)
        self.assertEqual(p.source, 'EHR_SYNC')
        self.assertEqual(p.source_user_id, 'svc-123')

    def test_patch_with_source_creates_provenance_for_measurement(self):
        self.write_client.patch(
            f'/api/patient-info/{self.person.person_id}/',
            {'hemoglobin_g_dl': '13.0', 'source': 'PATIENT_SELF'},
            format='json',
        )
        m = Measurement.objects.filter(
            person=self.person,
            measurement_source_value='718-7',
        ).first()
        self.assertIsNotNone(m)
        p = ProvenanceRecord.objects.filter(object_id=m.pk).first()
        self.assertIsNotNone(p)
        self.assertEqual(p.source, 'PATIENT_SELF')

    def test_patch_without_source_creates_no_provenance(self):
        before = ProvenanceRecord.objects.count()
        self.write_client.patch(
            f'/api/patient-info/{self.person.person_id}/',
            {'disease': 'CLL'},
            format='json',
        )
        self.assertEqual(ProvenanceRecord.objects.count(), before)

    def test_patch_returns_previous_values(self):
        """PATCH response must include previous_values snapshot of changed fields."""
        self.patient_info.disease = 'Multiple Myeloma'
        self.patient_info.save()
        resp = self.write_client.patch(
            f'/api/patient-info/{self.person.person_id}/',
            {'disease': 'CLL', 'source': 'EHR_SYNC'},
            format='json',
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertIn('previous_values', data)
        self.assertEqual(data['previous_values'].get('disease'), 'Multiple Myeloma')

    def test_admin_correction_requires_modification_reason(self):
        resp = self.write_client.patch(
            f'/api/patient-info/{self.person.person_id}/',
            {'disease': 'CLL', 'source': 'ADMIN_CORRECTION'},
            format='json',
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIn('modification_reason', resp.json().get('error', ''))

    def test_admin_correction_with_reason_succeeds(self):
        resp = self.write_client.patch(
            f'/api/patient-info/{self.person.person_id}/',
            {'disease': 'CLL', 'source': 'ADMIN_CORRECTION', 'modification_reason': 'Correcting misdiagnosis'},
            format='json',
        )
        self.assertEqual(resp.status_code, 200)
        p = ProvenanceRecord.objects.filter(object_id=self.patient_info.pk).first()
        self.assertIsNotNone(p)
        self.assertEqual(p.modification_reason, 'Correcting misdiagnosis')

    def test_provenance_endpoint_returns_history(self):
        self.write_client.patch(
            f'/api/patient-info/{self.person.person_id}/',
            {'disease': 'Myeloma', 'source': 'EHR_SYNC', 'source_user_id': 'ehr-456'},
            format='json',
        )
        resp = self.read_client.get(f'/api/patient-info/{self.person.person_id}/provenance/')
        self.assertEqual(resp.status_code, 200)
        sources = [r['source'] for r in resp.json()]
        self.assertIn('EHR_SYNC', sources)


    def test_omop_write_endpoint_records_provenance(self):
        """POST to a direct OMOP endpoint with source header records provenance."""
        resp = self.write_client.post(
            '/api/conditions/',
            {
                'condition_occurrence_id': 79901,
                'person': self.person.person_id,
                'condition_concept': self.condition_concept.concept_id,
                'condition_start_date': '2024-01-01',
                'condition_type_concept': self.type_concept.concept_id,
            },
            format='json',
            HTTP_X_PROVENANCE_SOURCE='EHR_SYNC',
            HTTP_X_PROVENANCE_USER_ID='ehr-omop-001',
        )
        self.assertEqual(resp.status_code, 201)
        from omop_core.models import ConditionOccurrence
        co = ConditionOccurrence.objects.filter(person=self.person).order_by('-condition_occurrence_id').first()
        self.assertIsNotNone(co)
        prov = ProvenanceRecord.objects.filter(object_id=co.pk).first()
        self.assertIsNotNone(prov, 'No ProvenanceRecord created for direct OMOP write')
        self.assertEqual(prov.source, 'EHR_SYNC')
        self.assertEqual(prov.source_user_id, 'ehr-omop-001')


class ProvenanceFhirUploadTest(_SmartBase):
    """FHIR upload with provenance headers tags all created OMOP records."""

    def test_fhir_upload_with_ehr_sync_tags_records(self):
        bundle_bytes = json.dumps(_make_fhir_bundle()).encode('utf-8')
        fhir_file = io.BytesIO(bundle_bytes)
        fhir_file.name = 'bundle.json'
        resp = self.write_client.post(
            '/api/patient-info/upload_fhir/',
            {'file': fhir_file, 'source': 'EHR_SYNC', 'source_user_id': 'ehr-001'},
            format='multipart',
        )
        self.assertIn(resp.status_code, [200, 201])
        person = Person.objects.filter(family_name='Smith', given_name='Jane').first()
        self.assertIsNotNone(person)
        pi = PatientInfo.objects.get(person=person)
        self.assertTrue(
            ProvenanceRecord.objects.filter(object_id=pi.pk).exists(),
            'PatientInfo was not tagged with provenance',
        )

    def test_fhir_upload_admin_correction_without_reason_rejected(self):
        bundle_bytes = json.dumps(_make_fhir_bundle()).encode('utf-8')
        fhir_file = io.BytesIO(bundle_bytes)
        fhir_file.name = 'bundle.json'
        resp = self.write_client.post(
            '/api/patient-info/upload_fhir/',
            {'file': fhir_file, 'source': 'ADMIN_CORRECTION'},
            format='multipart',
        )
        self.assertEqual(resp.status_code, 400)


# ---------------------------------------------------------------------------
# HKI-SEC-07: Audit log middleware
# ---------------------------------------------------------------------------

class AuditLogMiddlewareTest(_SmartBase):
    """Audit log middleware emits JSON for mutating requests, silent on reads."""

    def _capture_audit_logs(self, handler, *args, **kwargs):
        """Call handler and return list of parsed audit log JSON entries emitted."""
        import logging
        records = []

        class _Capture(logging.Handler):
            def emit(self, record):
                records.append(record.getMessage())

        capture = _Capture()
        audit_logger = logging.getLogger('audit')
        audit_logger.addHandler(capture)
        try:
            handler(*args, **kwargs)
        finally:
            audit_logger.removeHandler(capture)
        return [json.loads(r) for r in records]

    def _make_person_and_pi(self, person_id):
        person = Person.objects.create(person_id=person_id)
        pi = PatientInfo.objects.create(person=person, organization=self.organization)
        return person, pi

    # ------------------------------------------------------------------
    # HTTP method coverage
    # ------------------------------------------------------------------

    def test_patch_emits_audit_log(self):
        """PATCH produces exactly one audit log entry."""
        _, pi = self._make_person_and_pi(88801)

        logs = self._capture_audit_logs(
            self.write_client.patch,
            f'/api/patient-info/{pi.pk}/',
            {'ecog_status': '1'},
            format='json',
        )

        self.assertEqual(len(logs), 1)
        entry = logs[0]
        self.assertEqual(entry['event'], 'api_write')
        self.assertEqual(entry['method'], 'PATCH')
        self.assertIn('patient-info', entry['path'])
        self.assertEqual(entry['client_id'], 'foundation-client-id')

    def test_post_emits_audit_log(self):
        """POST produces exactly one audit log entry."""
        payload = {
            'person': self.person.pk,
            'measurement_concept': self.type_concept.pk,
            'measurement_date': '2024-01-01',
            'measurement_type_concept': self.type_concept.pk,
            'measurement_id': 99901,
        }
        logs = self._capture_audit_logs(
            self.write_client.post,
            '/api/measurements/',
            payload,
            format='json',
        )

        self.assertEqual(len(logs), 1)
        self.assertEqual(logs[0]['method'], 'POST')
        self.assertIn('measurements', logs[0]['path'])

    def test_delete_emits_audit_log(self):
        """DELETE produces exactly one audit log entry."""
        from omop_core.models import Measurement
        m = Measurement.objects.create(
            measurement_id=99902,
            person=self.person,
            measurement_concept=self.type_concept,
            measurement_date='2024-01-01',
            measurement_type_concept=self.type_concept,
        )
        logs = self._capture_audit_logs(
            self.write_client.delete,
            f'/api/measurements/{m.measurement_id}/',
        )

        self.assertEqual(len(logs), 1)
        self.assertEqual(logs[0]['method'], 'DELETE')

    def test_get_does_not_emit_audit_log(self):
        """GET produces no audit log entries."""
        _, pi = self._make_person_and_pi(88802)

        logs = self._capture_audit_logs(
            self.read_client.get,
            f'/api/patient-info/{pi.pk}/',
        )

        self.assertEqual(len(logs), 0, f'Unexpected audit logs for GET: {logs}')

    def test_list_get_does_not_emit_audit_log(self):
        """GET list endpoint produces no audit log entries."""
        logs = self._capture_audit_logs(
            self.read_client.get,
            '/api/patient-info/',
        )
        self.assertEqual(len(logs), 0)

    # ------------------------------------------------------------------
    # Log content correctness
    # ------------------------------------------------------------------

    def test_audit_log_contains_required_fields(self):
        """Every audit entry must include all fields from the acceptance criteria."""
        _, pi = self._make_person_and_pi(88803)

        logs = self._capture_audit_logs(
            self.write_client.patch,
            f'/api/patient-info/{pi.pk}/',
            {'ecog_status': '2'},
            format='json',
        )

        self.assertEqual(len(logs), 1)
        entry = logs[0]
        for field in ('event', 'method', 'path', 'status_code', 'client_id', 'ip_address', 'duration_ms'):
            self.assertIn(field, entry, f'Missing required audit field: {field}')

    def test_audit_log_is_valid_json(self):
        """Each audit line must be parseable as JSON (SIEM-compatible)."""
        import logging as _logging

        raw_records = []

        class _RawCapture(_logging.Handler):
            def emit(self, record):
                raw_records.append(record.getMessage())

        capture = _RawCapture()
        _logging.getLogger('audit').addHandler(capture)
        try:
            _, pi = self._make_person_and_pi(88804)
            self.write_client.patch(f'/api/patient-info/{pi.pk}/', {'ecog_status': '0'}, format='json')
        finally:
            _logging.getLogger('audit').removeHandler(capture)

        self.assertEqual(len(raw_records), 1)
        try:
            parsed = json.loads(raw_records[0])
        except json.JSONDecodeError as exc:
            self.fail(f'Audit log is not valid JSON: {exc}\nRaw: {raw_records[0]}')
        self.assertIsInstance(parsed, dict)

    def test_audit_log_captures_status_code(self):
        """status_code in the log must reflect the actual HTTP response status."""
        _, pi = self._make_person_and_pi(88805)
        # Patch a non-existent resource to get a predictable 404
        logs = self._capture_audit_logs(
            self.write_client.patch,
            '/api/patient-info/999999/',
            {'ecog_status': '3'},
            format='json',
        )
        self.assertEqual(len(logs), 1)
        self.assertEqual(logs[0]['status_code'], 404)

    def test_audit_log_client_id_from_oauth_token(self):
        """client_id in the log must reflect the OAuth2 application's client_id."""
        _, pi = self._make_person_and_pi(88806)

        logs = self._capture_audit_logs(
            self.write_client.patch,
            f'/api/patient-info/{pi.pk}/',
            {'ecog_status': '1'},
            format='json',
        )

        self.assertEqual(len(logs), 1)
        self.assertEqual(logs[0]['client_id'], 'foundation-client-id')

    def test_audit_log_no_client_id_for_unauthenticated(self):
        """Unauthenticated requests must log client_id as null, not raise."""
        anon = APIClient()
        logs = self._capture_audit_logs(
            anon.patch,
            '/api/patient-info/1/',
            {'ecog_status': '0'},
            format='json',
        )
        # Unauthenticated returns 401/403; middleware must still emit a log entry
        self.assertEqual(len(logs), 1)
        self.assertIsNone(logs[0]['client_id'])

    # ------------------------------------------------------------------
    # Reliability: logging failure must not block the response
    # ------------------------------------------------------------------

    def test_logging_failure_does_not_block_response(self):
        """If the audit logger raises, the API response must still be returned."""
        import logging as _logging
        from unittest.mock import patch as mock_patch

        _, pi = self._make_person_and_pi(88807)

        with mock_patch.object(_logging.getLogger('audit'), 'info', side_effect=RuntimeError('log exploded')):
            response = self.write_client.patch(
                f'/api/patient-info/{pi.pk}/',
                {'ecog_status': '1'},
                format='json',
            )

        # Response must be returned regardless of logging failure
        self.assertIn(response.status_code, range(200, 600))


class PatientInfoOmopSyncTest(_SmartBase):
    """PatientInfo PATCH → OMOP write-through via omop_write_service."""

    def _patch(self, pi, payload):
        return self.write_client.patch(
            f'/api/patient-info/{pi.person.person_id}/',
            payload,
            format='json',
        )

    def test_patch_lab_creates_measurement(self):
        """PATCHing a lab field creates a Measurement row."""
        from omop_core.models import Measurement
        person = Person.objects.create(person_id=91001)
        pi = PatientInfo.objects.create(person=person, organization=self.organization)
        before = Measurement.objects.filter(person=person).count()

        self._patch(pi, {'hemoglobin_g_dl': 12.5})

        self.assertEqual(Measurement.objects.filter(person=person).count(), before + 1)
        m = Measurement.objects.filter(person=person).latest('measurement_id')
        self.assertEqual(float(m.value_as_number), 12.5)

    def test_patch_lab_same_day_updates_not_duplicates(self):
        """Two PATCHes of the same lab on the same day → still 1 Measurement row."""
        from omop_core.models import Measurement
        person = Person.objects.create(person_id=91002)
        pi = PatientInfo.objects.create(person=person, organization=self.organization)

        self._patch(pi, {'hemoglobin_g_dl': 11.0})
        self._patch(pi, {'hemoglobin_g_dl': 11.5})

        rows = Measurement.objects.filter(
            person=person,
            measurement_source_value='718-7',
        )
        self.assertEqual(rows.count(), 1)
        self.assertEqual(float(rows.first().value_as_number), 11.5)

    def test_patch_lab_different_day_appends(self):
        """PATCHes on different dates → separate Measurement rows."""
        from unittest.mock import patch as mock_patch
        from datetime import date
        from omop_core.models import Measurement
        person = Person.objects.create(person_id=91003)
        pi = PatientInfo.objects.create(person=person, organization=self.organization)

        with mock_patch('omop_core.services.omop_write_service._today', return_value=date(2024, 1, 1)):
            self._patch(pi, {'hemoglobin_g_dl': 10.0})
        with mock_patch('omop_core.services.omop_write_service._today', return_value=date(2024, 2, 1)):
            self._patch(pi, {'hemoglobin_g_dl': 10.5})

        rows = Measurement.objects.filter(person=person, measurement_source_value='718-7')
        self.assertEqual(rows.count(), 2)

    def test_patch_disease_creates_condition_occurrence(self):
        """PATCHing 'disease' creates a new ConditionOccurrence row."""
        from omop_core.models import ConditionOccurrence
        person = Person.objects.create(person_id=91010)
        pi = PatientInfo.objects.create(person=person, organization=self.organization)

        self._patch(pi, {'disease': 'Breast cancer'})

        self.assertEqual(
            ConditionOccurrence.objects.filter(person=person).count(), 1
        )
        co = ConditionOccurrence.objects.get(person=person)
        self.assertEqual(co.condition_source_value, 'Breast cancer')

    def test_patch_stage_appends_condition_occurrence(self):
        """Two PATCHes of 'stage' create two separate ConditionOccurrence rows."""
        from omop_core.models import ConditionOccurrence
        from unittest.mock import patch as mock_patch
        from datetime import date
        person = Person.objects.create(person_id=91011)
        pi = PatientInfo.objects.create(person=person, organization=self.organization)

        with mock_patch('omop_core.services.omop_write_service._today', return_value=date(2024, 1, 1)):
            self._patch(pi, {'stage': 'Stage II'})
        with mock_patch('omop_core.services.omop_write_service._today', return_value=date(2024, 3, 1)):
            self._patch(pi, {'stage': 'Stage III'})

        self.assertEqual(ConditionOccurrence.objects.filter(person=person).count(), 2)

    def test_patch_demographics_updates_person(self):
        """PATCHing gender and date_of_birth updates the linked Person record."""
        person = Person.objects.create(person_id=91020)
        pi = PatientInfo.objects.create(person=person, organization=self.organization)

        self._patch(pi, {'gender': 'Female', 'date_of_birth': '1975-06-15'})

        person.refresh_from_db()
        self.assertEqual(person.year_of_birth, 1975)
        self.assertEqual(person.month_of_birth, 6)
        self.assertEqual(person.day_of_birth, 15)
        self.assertIsNotNone(person.gender_concept)
        self.assertEqual(person.gender_concept.concept_id, 8532)  # FEMALE

    def test_patch_first_line_therapy_creates_episode(self):
        """PATCHing first_line_therapy creates an Episode with episode_number=1."""
        from omop_oncology.models import Episode
        person = Person.objects.create(person_id=91030)
        pi = PatientInfo.objects.create(person=person, organization=self.organization)

        self._patch(pi, {
            'first_line_therapy': 'AC-T',
            'first_line_start_date': '2023-01-15',
            'first_line_end_date': '2023-07-01',
        })

        episodes = Episode.objects.filter(person=person, episode_number=1)
        self.assertEqual(episodes.count(), 1)
        ep = episodes.first()
        self.assertEqual(ep.episode_source_value, 'AC-T')
        from datetime import date
        self.assertEqual(ep.episode_start_date, date(2023, 1, 15))

    def test_patch_therapy_links_existing_drug_exposures(self):
        """DrugExposure rows in the episode date range are linked via EpisodeEvent."""
        from omop_oncology.models import Episode, EpisodeEvent
        from omop_core.models import DrugExposure, Concept
        person = Person.objects.create(person_id=91031)
        pi = PatientInfo.objects.create(person=person, organization=self.organization)
        drug_concept = Concept.objects.get(concept_id=19136160)
        type_concept = Concept.objects.get(concept_id=32817)

        # Pre-existing DrugExposure within the therapy date range
        de = DrugExposure.objects.create(
            drug_exposure_id=9910001,
            person=person,
            drug_concept=drug_concept,
            drug_exposure_start_date='2023-02-01',
            drug_type_concept=type_concept,
            drug_source_value='Paclitaxel',
        )

        self._patch(pi, {
            'first_line_therapy': 'AC-T',
            'first_line_start_date': '2023-01-15',
            'first_line_end_date': '2023-07-01',
        })

        episode = Episode.objects.get(person=person, episode_number=1)
        self.assertTrue(
            EpisodeEvent.objects.filter(episode_id=episode.episode_id, event_id=de.drug_exposure_id).exists(),
            'DrugExposure was not linked to Episode via EpisodeEvent',
        )

    def test_patch_therapy_no_duplicate_episode_events(self):
        """Repeating the PATCH does not create duplicate EpisodeEvent rows."""
        from omop_oncology.models import Episode, EpisodeEvent
        from omop_core.models import DrugExposure, Concept
        person = Person.objects.create(person_id=91032)
        pi = PatientInfo.objects.create(person=person, organization=self.organization)
        drug_concept = Concept.objects.get(concept_id=19136160)
        type_concept = Concept.objects.get(concept_id=32817)

        DrugExposure.objects.create(
            drug_exposure_id=9910002,
            person=person,
            drug_concept=drug_concept,
            drug_exposure_start_date='2023-02-01',
            drug_type_concept=type_concept,
            drug_source_value='Paclitaxel',
        )

        payload = {
            'first_line_therapy': 'AC-T',
            'first_line_start_date': '2023-01-15',
            'first_line_end_date': '2023-07-01',
        }
        self._patch(pi, payload)
        self._patch(pi, payload)  # second identical PATCH

        episode = Episode.objects.get(person=person, episode_number=1)
        self.assertEqual(
            EpisodeEvent.objects.filter(episode_id=episode.episode_id, event_id=9910002).count(), 1,
            'EpisodeEvent was duplicated',
        )

    def test_sync_failure_returns_500(self):
        """If sync_to_omop raises, the PATCH rolls back and returns 500."""
        from unittest.mock import patch as mock_patch
        person = Person.objects.create(person_id=91040)
        pi = PatientInfo.objects.create(person=person, organization=self.organization)
        original_status = pi.ecog_performance_status

        with mock_patch(
            'patient_portal.api.views.sync_to_omop',
            side_effect=RuntimeError('simulated DB failure'),
        ):
            response = self._patch(pi, {'ecog_performance_status': 1})

        self.assertEqual(response.status_code, 500)
        # PatientInfo must not have been updated — transaction was rolled back.
        pi.refresh_from_db()
        self.assertEqual(pi.ecog_performance_status, original_status)

    def test_lab_field_to_loinc_in_mappings_not_views(self):
        """LAB_FIELD_TO_LOINC must live in mappings, not be directly importable from views."""
        import importlib
        views_mod = importlib.import_module('patient_portal.api.views')
        self.assertFalse(
            hasattr(views_mod, '_LAB_FIELD_TO_LOINC'),
            '_LAB_FIELD_TO_LOINC should have been removed from views.py',
        )


class VocabularyRelationshipModelTest(TestCase):
    """Verify Relationship, ConceptRelationship, ConceptAncestor models exist and are queryable."""

    def setUp(self):
        _make_vocab_fixtures()
        vocab = Vocabulary.objects.get(vocabulary_id='TEST')
        domain = Domain.objects.get(domain_id='Drug')
        cc = ConceptClass.objects.get(concept_class_id='Clinical Finding')
        self.c1 = Concept.objects.create(
            concept_id=9901001, concept_name='Drug A',
            domain=domain, vocabulary=vocab, concept_class=cc,
            concept_code='A1',
            valid_start_date=date(1970, 1, 1), valid_end_date=date(2099, 12, 31),
        )
        self.c2 = Concept.objects.create(
            concept_id=9901002, concept_name='Drug Class B',
            domain=domain, vocabulary=vocab, concept_class=cc,
            concept_code='B1',
            valid_start_date=date(1970, 1, 1), valid_end_date=date(2099, 12, 31),
        )

    def test_relationship_model(self):
        Relationship.objects.create(
            relationship_id='test-maps-to',
            relationship_name='Test Maps To',
            is_hierarchical=0,
            defines_ancestry=0,
            reverse_relationship_id='test-mapped-from',
            relationship_concept_id=0,
        )
        self.assertEqual(
            Relationship.objects.get(pk='test-maps-to').relationship_name,
            'Test Maps To',
        )

    def test_concept_relationship_model(self):
        r = Relationship.objects.create(
            relationship_id='Maps to',
            relationship_name='Maps to',
            is_hierarchical=0,
            defines_ancestry=0,
            reverse_relationship_id='Mapped from',
            relationship_concept_id=0,
        )
        ConceptRelationship.objects.create(
            concept_1=self.c1,
            concept_2=self.c2,
            relationship=r,
            valid_start_date=date(1970, 1, 1),
            valid_end_date=date(2099, 12, 31),
        )
        self.assertEqual(
            ConceptRelationship.objects.filter(concept_1=self.c1).count(), 1
        )

    def test_concept_ancestor_model(self):
        ConceptAncestor.objects.create(
            ancestor_concept=self.c2,
            descendant_concept=self.c1,
            min_levels_of_separation=1,
            max_levels_of_separation=1,
        )
        self.assertEqual(
            ConceptAncestor.objects.filter(descendant_concept=self.c1).count(), 1
        )

    def test_unique_together_concept_relationship(self):
        from django.db import IntegrityError
        r = Relationship.objects.create(
            relationship_id='Is a',
            relationship_name='Is a',
            is_hierarchical=1,
            defines_ancestry=1,
            reverse_relationship_id='Subsumes',
            relationship_concept_id=0,
        )
        ConceptRelationship.objects.create(
            concept_1=self.c1, concept_2=self.c2, relationship=r,
            valid_start_date=date(1970, 1, 1), valid_end_date=date(2099, 12, 31),
        )
        with self.assertRaises(IntegrityError):
            ConceptRelationship.objects.create(
                concept_1=self.c1, concept_2=self.c2, relationship=r,
                valid_start_date=date(1970, 1, 1), valid_end_date=date(2099, 12, 31),
            )


# ---------------------------------------------------------------------------
# load_athena_vocabularies management command tests
# ---------------------------------------------------------------------------

class AthenaVocabularyLoadTest(TestCase):
    """Test load_athena_vocabularies management command with minimal fixture TSV files."""

    def _write_tsv(self, directory, filename, headers, rows):
        path = os.path.join(directory, filename)
        with open(path, 'w', newline='') as f:
            f.write('\t'.join(headers) + '\n')
            for row in rows:
                f.write('\t'.join(str(v) for v in row) + '\n')

    def _write_minimal_athena(self, directory):
        """Write the minimal set of Athena TSV files needed for tests."""
        self._write_tsv(directory, 'RELATIONSHIP.csv',
            ['relationship_id', 'relationship_name', 'is_hierarchical',
             'defines_ancestry', 'reverse_relationship_id', 'relationship_concept_id'],
            [['Maps to', 'Maps to value', '0', '0', 'Mapped from', '44818965'],
             ['Is a', 'Is a', '1', '1', 'Subsumes', '44818723']],
        )
        self._write_tsv(directory, 'VOCABULARY.csv',
            ['vocabulary_id', 'vocabulary_name', 'vocabulary_reference',
             'vocabulary_version', 'vocabulary_concept_id'],
            [['HemOnc', 'HemOnc Oncology', '', 'v2024', '0'],
             ['RxNorm', 'RxNorm', '', '2024AA', '0'],
             ['CPT4', 'CPT-4', '', '2024', '0']],  # out of scope — should be skipped
        )
        self._write_tsv(directory, 'DOMAIN.csv',
            ['domain_id', 'domain_name', 'domain_concept_id'],
            [['Drug', 'Drug', '13']],
        )
        self._write_tsv(directory, 'CONCEPT_CLASS.csv',
            ['concept_class_id', 'concept_class_name', 'concept_class_concept_id'],
            [['HemOnc Class', 'HemOnc Class', '0'],
             ['Ingredient', 'Ingredient', '0'],
             ['Branded Drug', 'Branded Drug', '0'],
             ['Clinical Finding', 'Clinical Finding', '0']],
        )
        self._write_tsv(directory, 'CONCEPT.csv',
            ['concept_id', 'concept_name', 'domain_id', 'vocabulary_id',
             'concept_class_id', 'standard_concept', 'concept_code',
             'valid_start_date', 'valid_end_date', 'invalid_reason'],
            # HemOnc concepts — should be loaded
            [['5000001', 'Proteasome inhibitor', 'Drug', 'HemOnc', 'HemOnc Class', 'S', 'PI', '19700101', '20991231', ''],
             ['5000002', 'bortezomib',           'Drug', 'HemOnc', 'HemOnc Class', 'S', 'HO-Bort', '19700101', '20991231', ''],
             # RxNorm Ingredient — should be loaded
             ['5000003', 'bortezomib',           'Drug', 'RxNorm', 'Ingredient', 'S', '1421', '19700101', '20991231', ''],
             # RxNorm Branded — should be loaded
             ['5000004', 'Velcade',              'Drug', 'RxNorm', 'Branded Drug', 'S', '213269', '19700101', '20991231', ''],
             # CPT4 concept — should be SKIPPED (not in vocabulary scope)
             ['5000099', 'Out-of-scope concept', 'Drug', 'CPT4', 'Clinical Finding', 'S', '123456', '19700101', '20991231', '']],
        )
        self._write_tsv(directory, 'CONCEPT_RELATIONSHIP.csv',
            ['concept_id_1', 'concept_id_2', 'relationship_id',
             'valid_start_date', 'valid_end_date', 'invalid_reason'],
            # RxNorm bortezomib → HemOnc bortezomib (both in scope)
            [['5000003', '5000002', 'Maps to', '19700101', '20991231', ''],
             # Edge to out-of-scope CPT4 concept — should be SKIPPED
             ['5000003', '5000099', 'Maps to', '19700101', '20991231', '']],
        )
        self._write_tsv(directory, 'CONCEPT_ANCESTOR.csv',
            ['ancestor_concept_id', 'descendant_concept_id',
             'min_levels_of_separation', 'max_levels_of_separation'],
            # HemOnc: PI class is ancestor of bortezomib HemOnc concept
            [['5000001', '5000002', '1', '1'],
             # Edge referencing out-of-scope concept — should be SKIPPED
             ['5000001', '5000099', '2', '2']],
        )

    def test_load_creates_relationship_rows(self):
        from omop_core.models import Relationship
        from django.core.management import call_command
        with tempfile.TemporaryDirectory() as tmpdir:
            self._write_minimal_athena(tmpdir)
            call_command('load_athena_vocabularies', path=tmpdir)
        self.assertTrue(Relationship.objects.filter(relationship_id='Maps to').exists())
        self.assertTrue(Relationship.objects.filter(relationship_id='Is a').exists())

    def test_load_filters_concepts_to_scope(self):
        from omop_core.models import Concept
        from django.core.management import call_command
        with tempfile.TemporaryDirectory() as tmpdir:
            self._write_minimal_athena(tmpdir)
            call_command('load_athena_vocabularies', path=tmpdir)
        self.assertTrue(Concept.objects.filter(concept_id=5000001).exists())  # HemOnc
        self.assertTrue(Concept.objects.filter(concept_id=5000003).exists())  # RxNorm Ingredient
        self.assertTrue(Concept.objects.filter(concept_id=5000004).exists())  # RxNorm Branded
        self.assertFalse(Concept.objects.filter(concept_id=5000099).exists())  # CPT4 — excluded

    def test_load_filters_concept_relationships(self):
        from omop_core.models import ConceptRelationship
        from django.core.management import call_command
        with tempfile.TemporaryDirectory() as tmpdir:
            self._write_minimal_athena(tmpdir)
            call_command('load_athena_vocabularies', path=tmpdir)
        # Edge between two in-scope concepts should be loaded
        self.assertTrue(ConceptRelationship.objects.filter(
            concept_1_id=5000003, concept_2_id=5000002
        ).exists())
        # Edge to out-of-scope SNOMED concept should be skipped
        self.assertFalse(ConceptRelationship.objects.filter(
            concept_2_id=5000099
        ).exists())

    def test_load_concept_ancestors_hemonc_only(self):
        from omop_core.models import ConceptAncestor
        from django.core.management import call_command
        with tempfile.TemporaryDirectory() as tmpdir:
            self._write_minimal_athena(tmpdir)
            call_command('load_athena_vocabularies', path=tmpdir)
        self.assertTrue(ConceptAncestor.objects.filter(
            ancestor_concept_id=5000001, descendant_concept_id=5000002
        ).exists())
        # Out-of-scope ancestor edge should be skipped
        self.assertFalse(ConceptAncestor.objects.filter(
            descendant_concept_id=5000099
        ).exists())

    def test_idempotent_reload(self):
        from omop_core.models import Concept
        from django.core.management import call_command
        with tempfile.TemporaryDirectory() as tmpdir:
            self._write_minimal_athena(tmpdir)
            call_command('load_athena_vocabularies', path=tmpdir)
            count_after_first = Concept.objects.filter(vocabulary_id='HemOnc').count()
            call_command('load_athena_vocabularies', path=tmpdir)
            count_after_second = Concept.objects.filter(vocabulary_id='HemOnc').count()
        self.assertEqual(count_after_first, count_after_second)

    def test_dry_run_writes_nothing(self):
        from omop_core.models import Concept, Relationship
        from django.core.management import call_command
        before_concepts = Concept.objects.count()
        before_rels = Relationship.objects.count()
        with tempfile.TemporaryDirectory() as tmpdir:
            self._write_minimal_athena(tmpdir)
            call_command('load_athena_vocabularies', path=tmpdir, dry_run=True)
        self.assertEqual(Concept.objects.count(), before_concepts)
        self.assertEqual(Relationship.objects.count(), before_rels)


class RxNavServiceTest(TestCase):
    """Test rxnav_service.resolve_drug() with mocked HTTP calls."""

    def setUp(self):
        _make_vocab_fixtures()
        self.vocab_rxnorm, _ = Vocabulary.objects.get_or_create(
            vocabulary_id='RxNorm',
            defaults={'vocabulary_name': 'RxNorm', 'vocabulary_concept_id': 0},
        )
        self.domain_drug = Domain.objects.get(domain_id='Drug')
        self.cc_ingredient, _ = ConceptClass.objects.get_or_create(
            concept_class_id='Ingredient',
            defaults={'concept_class_name': 'Ingredient', 'concept_class_concept_id': 0},
        )

    def _rxnav_response(self, rxcui, name):
        import json
        return json.dumps({
            'drugGroup': {
                'conceptGroup': [
                    {'tty': 'IN', 'conceptProperties': [{'rxcui': rxcui, 'name': name}]}
                ]
            }
        }).encode()

    def _rxnav_empty(self):
        import json
        return json.dumps({'drugGroup': {'conceptGroup': []}}).encode()

    def _mock_urlopen(self, payload):
        from unittest.mock import MagicMock, patch
        mock_resp = MagicMock()
        mock_resp.read.return_value = payload
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        return patch('urllib.request.urlopen', return_value=mock_resp)

    def test_known_drug_returns_existing_concept_without_api_call(self):
        """Drug already in local Concept table → returned without hitting RxNav."""
        from omop_core.services.rxnav_service import resolve_drug
        Concept.objects.create(
            concept_id=9990001, concept_name='bortezomib',
            domain=self.domain_drug, vocabulary=self.vocab_rxnorm,
            concept_class=self.cc_ingredient,
            concept_code='1421', standard_concept='S',
            valid_start_date=date(1970, 1, 1), valid_end_date=date(2099, 12, 31),
        )
        with self._mock_urlopen(b'should not be called') as mock_open:
            result = resolve_drug('bortezomib')
            mock_open.assert_not_called()
        self.assertEqual(result.concept_id, 9990001)

    def test_unknown_drug_calls_rxnav_and_creates_concept(self):
        """Drug not in local vocab → RxNav called → new Concept row created."""
        from omop_core.services.rxnav_service import resolve_drug
        with self._mock_urlopen(self._rxnav_response('1421', 'bortezomib')):
            result = resolve_drug('Velcade')
        self.assertIsNotNone(result)
        self.assertEqual(result.concept_code, '1421')
        self.assertEqual(result.vocabulary_id, 'RxNorm')
        self.assertTrue(Concept.objects.filter(concept_code='1421', vocabulary_id='RxNorm').exists())

    def test_rxnav_no_results_returns_none(self):
        """RxNav returns no ingredient matches → resolve_drug returns None."""
        from omop_core.services.rxnav_service import resolve_drug
        with self._mock_urlopen(self._rxnav_empty()):
            result = resolve_drug('unknowndrugxyz')
        self.assertIsNone(result)

    def test_rxnav_http_error_returns_none(self):
        """RxNav HTTP error → resolve_drug returns None without raising."""
        from omop_core.services.rxnav_service import resolve_drug
        from unittest.mock import patch
        with patch('urllib.request.urlopen', side_effect=Exception('network error')):
            result = resolve_drug('anything')
        self.assertIsNone(result)

    def test_second_call_uses_cached_concept(self):
        """After first call caches a Concept, second call returns it without API hit."""
        from omop_core.services.rxnav_service import resolve_drug
        with self._mock_urlopen(self._rxnav_response('9876', 'lenalidomide')) as mock_open:
            resolve_drug('Revlimid')
            call_count_after_first = mock_open.call_count
        with self._mock_urlopen(b'should not be called') as mock_open2:
            result = resolve_drug('lenalidomide')
            mock_open2.assert_not_called()
        self.assertIsNotNone(result)
        self.assertEqual(result.concept_code, '9876')


class LotInferenceTest(_SmartBase):
    """Tests for omop_core.services.lot_inference_service (ARTEMIS-lite + HealthTree)."""

    def _make_exposure(self, person, drug_name, start, end=None, pk=None):
        from omop_core.models import DrugExposure, Concept, Domain, Vocabulary, ConceptClass
        from datetime import date as _date
        # Create (or reuse) a concept whose concept_name matches the drug name so
        # that _drug_key() in lot_inference_service resolves to the correct string.
        domain_drug = Domain.objects.filter(domain_id='Drug').first()
        vocab = Vocabulary.objects.filter(vocabulary_id='TEST').first()
        cc = ConceptClass.objects.filter(concept_class_id='Clinical Finding').first()
        # Use a stable concept_id derived from a hash of the drug name to avoid collisions.
        import hashlib
        drug_cid = int(hashlib.md5(drug_name.lower().encode()).hexdigest()[:8], 16) % 900000 + 100000
        drug_concept, _ = Concept.objects.get_or_create(
            concept_id=drug_cid,
            defaults={
                'concept_name': drug_name,
                'domain': domain_drug,
                'vocabulary': vocab,
                'concept_class': cc,
                'concept_code': drug_name.lower(),
                'valid_start_date': _date(1970, 1, 1),
                'valid_end_date': _date(2099, 12, 31),
            },
        )
        type_concept = Concept.objects.filter(concept_id=32817).first()
        if pk is None:
            last = DrugExposure.objects.order_by('-drug_exposure_id').first()
            pk = (last.drug_exposure_id + 1) if last else 1
        return DrugExposure.objects.create(
            drug_exposure_id=pk,
            person=person,
            drug_concept=drug_concept,
            drug_exposure_start_date=start,
            drug_exposure_end_date=end,
            drug_type_concept=type_concept,
            drug_source_value=drug_name,
        )

    def _make_procedure(self, person, snomed_code, proc_date, pk=None):
        from omop_core.models import ProcedureOccurrence, Concept, Domain, Vocabulary, ConceptClass
        from datetime import date as _date
        type_concept = Concept.objects.filter(concept_id=32817).first()
        # Create (or reuse) a concept for the SNOMED procedure code so the NOT NULL
        # constraint on procedure_concept_id is satisfied.
        domain_proc, _ = Domain.objects.get_or_create(
            domain_id='Procedure',
            defaults={'domain_name': 'Procedure', 'domain_concept_id': 10},
        )
        vocab = Vocabulary.objects.filter(vocabulary_id='TEST').first()
        cc = ConceptClass.objects.filter(concept_class_id='Clinical Finding').first()
        import hashlib
        proc_cid = int(hashlib.md5(f'proc-{snomed_code}'.encode()).hexdigest()[:8], 16) % 900000 + 100000
        concept, _ = Concept.objects.get_or_create(
            concept_id=proc_cid,
            defaults={
                'concept_name': f'Procedure {snomed_code}',
                'domain': domain_proc,
                'vocabulary': vocab,
                'concept_class': cc,
                'concept_code': snomed_code,
                'valid_start_date': _date(1970, 1, 1),
                'valid_end_date': _date(2099, 12, 31),
            },
        )
        if pk is None:
            from omop_core.models import ProcedureOccurrence as PO
            last = PO.objects.order_by('-procedure_occurrence_id').first()
            pk = (last.procedure_occurrence_id + 1) if last else 1
        return ProcedureOccurrence.objects.create(
            procedure_occurrence_id=pk,
            person=person,
            procedure_concept=concept,
            procedure_date=proc_date,
            procedure_type_concept=type_concept,
            procedure_source_value=snomed_code,
        )

    # ── Core ARTEMIS-lite tests ────────────────────────────────────────────

    def test_single_drug_creates_one_episode(self):
        from datetime import date
        from omop_core.services.lot_inference_service import infer_lot_for_person
        from omop_oncology.models import Episode
        person = Person.objects.create(person_id=92001)
        self._make_exposure(person, 'Ibrutinib', date(2023, 1, 1), date(2023, 6, 30), pk=9200101)
        lots = infer_lot_for_person(person)
        self.assertEqual(len(lots), 1)
        self.assertEqual(Episode.objects.filter(person=person).count(), 1)
        ep = Episode.objects.get(person=person)
        self.assertEqual(ep.episode_number, 1)

    def test_combination_window_groups_drugs(self):
        from datetime import date
        from omop_core.services.lot_inference_service import infer_lot_for_person
        from omop_oncology.models import Episode
        person = Person.objects.create(person_id=92002)
        self._make_exposure(person, 'bortezomib',   date(2023, 1, 1),  date(2023, 6, 30), pk=9200201)
        self._make_exposure(person, 'lenalidomide', date(2023, 1, 10), date(2023, 6, 30), pk=9200202)
        self._make_exposure(person, 'dexamethasone',date(2023, 1, 15), date(2023, 6, 30), pk=9200203)
        infer_lot_for_person(person)
        self.assertEqual(Episode.objects.filter(person=person).count(), 1)
        ep = Episode.objects.get(person=person)
        self.assertIn('VRD', ep.episode_source_value)

    def test_gap_rule_creates_new_lot(self):
        from datetime import date
        from omop_core.services.lot_inference_service import infer_lot_for_person
        from omop_oncology.models import Episode
        person = Person.objects.create(person_id=92003)
        self._make_exposure(person, 'bortezomib', date(2023, 1, 1), date(2023, 6, 30), pk=9200301)
        self._make_exposure(person, 'carfilzomib', date(2024, 1, 1), date(2024, 6, 30), pk=9200302)
        infer_lot_for_person(person)
        self.assertEqual(Episode.objects.filter(person=person).count(), 2)

    def test_switch_rule_creates_new_lot(self):
        from datetime import date
        from omop_core.services.lot_inference_service import infer_lot_for_person
        from omop_oncology.models import Episode
        person = Person.objects.create(person_id=92004)
        self._make_exposure(person, 'bortezomib',   date(2023, 1, 1), date(2023, 3, 31), pk=9200401)
        self._make_exposure(person, 'lenalidomide', date(2023, 1, 1), date(2023, 3, 31), pk=9200402)
        self._make_exposure(person, 'pomalidomide', date(2023, 4, 30), date(2023, 9, 30), pk=9200403)
        self._make_exposure(person, 'daratumumab',  date(2023, 4, 30), date(2023, 9, 30), pk=9200404)
        infer_lot_for_person(person)
        self.assertEqual(Episode.objects.filter(person=person).count(), 2)

    def test_supportive_agent_not_counted_in_switch(self):
        from datetime import date
        from omop_core.services.lot_inference_service import infer_lot_for_person
        from omop_oncology.models import Episode
        person = Person.objects.create(person_id=92005)
        self._make_exposure(person, 'bortezomib',   date(2023, 1, 1),  date(2023, 3, 31), pk=9200501)
        self._make_exposure(person, 'bortezomib',   date(2023, 4, 15), date(2023, 6, 30), pk=9200502)
        self._make_exposure(person, 'dexamethasone',date(2023, 4, 15), date(2023, 6, 30), pk=9200503)
        infer_lot_for_person(person)
        self.assertEqual(Episode.objects.filter(person=person).count(), 1)

    def test_regimen_lookup_names_vrd(self):
        from datetime import date
        from omop_core.services.lot_inference_service import infer_lot_for_person
        from omop_oncology.models import Episode
        person = Person.objects.create(person_id=92006)
        self._make_exposure(person, 'bortezomib',   date(2023, 1, 1), date(2023, 6, 30), pk=9200601)
        self._make_exposure(person, 'lenalidomide', date(2023, 1, 5), date(2023, 6, 30), pk=9200602)
        self._make_exposure(person, 'dexamethasone',date(2023, 1, 5), date(2023, 6, 30), pk=9200603)
        infer_lot_for_person(person)
        ep = Episode.objects.get(person=person)
        self.assertIn('VRD', ep.episode_source_value)

    def test_regimen_lookup_names_daravrd(self):
        from datetime import date
        from omop_core.services.lot_inference_service import infer_lot_for_person
        from omop_oncology.models import Episode
        person = Person.objects.create(person_id=92007)
        for drug, pk in [('daratumumab', 9200701), ('bortezomib', 9200702),
                         ('lenalidomide', 9200703), ('dexamethasone', 9200704)]:
            self._make_exposure(person, drug, date(2023, 1, 1), date(2023, 6, 30), pk=pk)
        infer_lot_for_person(person)
        ep = Episode.objects.get(person=person)
        self.assertIn('DaraVRD', ep.episode_source_value)

    def test_alphabetic_fallback_name(self):
        from datetime import date
        from omop_core.services.lot_inference_service import infer_lot_for_person
        from omop_oncology.models import Episode
        person = Person.objects.create(person_id=92008)
        self._make_exposure(person, 'AlphaDrug', date(2023, 1, 1), date(2023, 6, 30), pk=9200801)
        self._make_exposure(person, 'BetaDrug',  date(2023, 1, 5), date(2023, 6, 30), pk=9200802)
        infer_lot_for_person(person)
        ep = Episode.objects.get(person=person)
        # _drug_key lowercases names; the fallback regimen name joins lowercase drug keys.
        self.assertIn('alphadrug', ep.episode_source_value)
        self.assertIn('betadrug', ep.episode_source_value)

    def test_episode_events_linked(self):
        from datetime import date
        from omop_core.services.lot_inference_service import infer_lot_for_person
        from omop_oncology.models import Episode, EpisodeEvent
        person = Person.objects.create(person_id=92009)
        de = self._make_exposure(person, 'Ibrutinib', date(2023, 1, 1), date(2023, 6, 30), pk=9200901)
        infer_lot_for_person(person)
        ep = Episode.objects.get(person=person)
        self.assertTrue(EpisodeEvent.objects.filter(episode_id=ep.episode_id, event_id=de.drug_exposure_id).exists())

    def test_no_duplicate_episodes(self):
        from datetime import date
        from omop_core.services.lot_inference_service import infer_lot_for_person
        from omop_oncology.models import Episode
        person = Person.objects.create(person_id=92010)
        self._make_exposure(person, 'Ibrutinib', date(2023, 1, 1), date(2023, 6, 30), pk=9201001)
        infer_lot_for_person(person, force=True)
        infer_lot_for_person(person, force=True)
        self.assertEqual(Episode.objects.filter(person=person).count(), 1)

    def test_no_duplicate_episode_events(self):
        from datetime import date
        from omop_core.services.lot_inference_service import infer_lot_for_person
        from omop_oncology.models import Episode, EpisodeEvent
        person = Person.objects.create(person_id=92011)
        de = self._make_exposure(person, 'Ibrutinib', date(2023, 1, 1), date(2023, 6, 30), pk=9201101)
        infer_lot_for_person(person, force=True)
        infer_lot_for_person(person, force=True)
        ep = Episode.objects.get(person=person)
        self.assertEqual(EpisodeEvent.objects.filter(episode_id=ep.episode_id, event_id=de.drug_exposure_id).count(), 1)

    def test_patient_info_refreshed(self):
        from datetime import date
        from omop_core.services.lot_inference_service import infer_lot_for_person
        person = Person.objects.create(person_id=92012)
        self._make_exposure(person, 'bortezomib',   date(2023, 1, 1), date(2023, 6, 30), pk=9201201)
        self._make_exposure(person, 'lenalidomide', date(2023, 1, 5), date(2023, 6, 30), pk=9201202)
        self._make_exposure(person, 'dexamethasone',date(2023, 1, 5), date(2023, 6, 30), pk=9201203)
        infer_lot_for_person(person)
        pi = PatientInfo.objects.filter(person=person).first()
        self.assertIsNotNone(pi)
        self.assertIsNotNone(pi.first_line_therapy)

    def test_existing_episodes_skipped(self):
        from datetime import date
        from omop_core.services.lot_inference_service import infer_lot_for_person
        from omop_oncology.models import Episode
        from omop_core.models import Concept
        person = Person.objects.create(person_id=92013)
        self._make_exposure(person, 'Ibrutinib', date(2023, 1, 1), date(2023, 6, 30), pk=9201301)
        ep_concept = Concept.objects.filter(concept_id=32531).first()
        ehr_concept = Concept.objects.filter(concept_id=32817).first()
        from omop_oncology.models import Episode as _Ep
        last_ep = _Ep.objects.order_by('-episode_id').first()
        manual_ep_id = (last_ep.episode_id + 1) if last_ep else 1
        Episode.objects.create(
            episode_id=manual_ep_id,
            person=person, episode_concept=ep_concept, episode_object_concept=ehr_concept,
            episode_type_concept=ehr_concept, episode_number=1,
            episode_start_date=date(2023, 1, 1), episode_source_value='Manual',
        )
        infer_lot_for_person(person)
        self.assertEqual(Episode.objects.filter(person=person).count(), 1)
        self.assertEqual(Episode.objects.get(person=person).episode_source_value, 'Manual')

    def test_dry_run_no_db_writes(self):
        from datetime import date
        from omop_core.services.lot_inference_service import infer_lot_for_person
        from omop_oncology.models import Episode
        person = Person.objects.create(person_id=92014)
        self._make_exposure(person, 'Ibrutinib', date(2023, 1, 1), date(2023, 6, 30), pk=9201401)
        lots = infer_lot_for_person(person, dry_run=True)
        self.assertEqual(len(lots), 1)
        self.assertEqual(Episode.objects.filter(person=person).count(), 0)

    def test_management_command_single_patient(self):
        from datetime import date
        from omop_oncology.models import Episode
        from django.core.management import call_command
        person = Person.objects.create(person_id=92015)
        self._make_exposure(person, 'Ibrutinib', date(2023, 1, 1), date(2023, 6, 30), pk=9201501)
        call_command('infer_lot', person_id=person.person_id, verbosity=0)
        self.assertEqual(Episode.objects.filter(person=person).count(), 1)

    # ── HealthTree phase/procedure tests ──────────────────────────────────

    def test_induction_label_first_lot(self):
        from datetime import date
        from omop_core.services.lot_inference_service import infer_lot_for_person
        from omop_oncology.models import Episode
        person = Person.objects.create(person_id=92016)
        self._make_exposure(person, 'bortezomib',   date(2023, 1, 1), date(2023, 6, 30), pk=9201601)
        self._make_exposure(person, 'lenalidomide', date(2023, 1, 5), date(2023, 6, 30), pk=9201602)
        self._make_exposure(person, 'dexamethasone',date(2023, 1, 5), date(2023, 6, 30), pk=9201603)
        infer_lot_for_person(person)
        ep = Episode.objects.get(person=person)
        self.assertIn('induction', ep.episode_source_value)

    def test_steroid_only_window_no_new_lot(self):
        from datetime import date
        from omop_core.services.lot_inference_service import infer_lot_for_person
        from omop_oncology.models import Episode
        person = Person.objects.create(person_id=92017)
        self._make_exposure(person, 'bortezomib',   date(2023, 1, 1), date(2023, 3, 31), pk=9201701)
        self._make_exposure(person, 'dexamethasone', date(2023, 4, 1), date(2023, 4, 30), pk=9201702)
        self._make_exposure(person, 'bortezomib',   date(2023, 5, 1), date(2023, 8, 31), pk=9201703)
        infer_lot_for_person(person)
        self.assertEqual(Episode.objects.filter(person=person).count(), 1)

    def test_transplant_procedure_creates_new_lot(self):
        from datetime import date
        from omop_core.services.lot_inference_service import infer_lot_for_person
        from omop_oncology.models import Episode
        person = Person.objects.create(person_id=92018)
        self._make_exposure(person, 'bortezomib',   date(2023, 1, 1), date(2023, 6, 30), pk=9201801)
        self._make_exposure(person, 'lenalidomide', date(2023, 1, 5), date(2023, 6, 30), pk=9201802)
        self._make_exposure(person, 'dexamethasone',date(2023, 1, 5), date(2023, 6, 30), pk=9201803)
        self._make_procedure(person, '425983008', date(2023, 7, 15), pk=9201804)
        lots = infer_lot_for_person(person)
        self.assertGreaterEqual(len(lots), 2)
        eps = Episode.objects.filter(person=person).order_by('episode_number')
        self.assertIn('induction', eps[0].episode_source_value)

    def test_tandem_transplant_same_lot(self):
        from datetime import date
        from omop_core.services.lot_inference_service import infer_lot_for_person
        from omop_oncology.models import Episode
        person = Person.objects.create(person_id=92019)
        self._make_exposure(person, 'bortezomib',   date(2023, 1, 1), date(2023, 6, 30), pk=9201901)
        self._make_procedure(person, '425983008', date(2023, 7, 1), pk=9201902)
        self._make_procedure(person, '425983008', date(2023, 11, 1), pk=9201903)
        lots = infer_lot_for_person(person)
        transplant_lots = [l for l in lots if 'transplant' in l.phase_label]
        self.assertEqual(len(transplant_lots), 1)

    def test_consolidation_phase_label(self):
        from datetime import date
        from omop_core.services.lot_inference_service import infer_lot_for_person
        from omop_oncology.models import Episode
        person = Person.objects.create(person_id=92020)
        self._make_exposure(person, 'bortezomib',   date(2023, 1, 1), date(2023, 6, 30), pk=9202001)
        self._make_exposure(person, 'dexamethasone',date(2023, 1, 5), date(2023, 6, 30), pk=9202002)
        self._make_procedure(person, '425983008', date(2023, 7, 15), pk=9202003)
        self._make_exposure(person, 'lenalidomide', date(2023, 9, 1), date(2023, 12, 31), pk=9202004)
        infer_lot_for_person(person)
        eps = Episode.objects.filter(person=person).order_by('episode_number')
        labels = [ep.episode_source_value for ep in eps]
        self.assertTrue(any('consolidation' in l for l in labels))

    def test_maintenance_phase_label(self):
        from datetime import date
        from omop_core.services.lot_inference_service import infer_lot_for_person
        from omop_oncology.models import Episode
        person = Person.objects.create(person_id=92021)
        self._make_exposure(person, 'bortezomib',   date(2023, 1, 1), date(2023, 6, 30), pk=9202101)
        self._make_exposure(person, 'dexamethasone',date(2023, 1, 5), date(2023, 6, 30), pk=9202102)
        self._make_procedure(person, '425983008', date(2023, 7, 15), pk=9202103)
        self._make_exposure(person, 'lenalidomide', date(2023, 11, 1), date(2024, 6, 30), pk=9202104)
        infer_lot_for_person(person)
        eps = Episode.objects.filter(person=person).order_by('episode_number')
        labels = [ep.episode_source_value for ep in eps]
        self.assertTrue(any('maintenance' in l for l in labels))

    def test_cart_procedure_creates_new_lot(self):
        from datetime import date
        from omop_core.services.lot_inference_service import infer_lot_for_person
        from omop_oncology.models import Episode
        person = Person.objects.create(person_id=92022)
        self._make_exposure(person, 'pomalidomide', date(2023, 1, 1), date(2023, 6, 30), pk=9202201)
        self._make_exposure(person, 'dexamethasone',date(2023, 1, 5), date(2023, 6, 30), pk=9202202)
        self._make_procedure(person, '1156961008', date(2023, 8, 1), pk=9202203)
        lots = infer_lot_for_person(person)
        self.assertGreaterEqual(len(lots), 2)
        cart_lots = [l for l in lots if 'CAR T-Cell' in l.phase_label]
        self.assertEqual(len(cart_lots), 1)


# ---------------------------------------------------------------------------
# ScopedTokenPermission role-based enforcement
# ---------------------------------------------------------------------------

class ScopedTokenPermissionTest(TestCase):
    """Verify role-based enforcement for non-OAuth2 auth paths."""

    def setUp(self):
        from django.test import RequestFactory
        from patient_portal.api.permissions import ScopedTokenPermission

        self.factory = RequestFactory()
        self.permission = ScopedTokenPermission()

    def _user(self, **kwargs):
        import uuid
        return Identity.objects.create_user(
            email=f"perm-{uuid.uuid4()}@test.com",
            password="x",
            **kwargs,
        )

    def _req(self, method, auth, user):
        req = getattr(self.factory, method.lower())("/")
        req.auth = auth
        req.user = user
        return req

    # --- service-token ---

    def test_service_token_allows_delete(self):
        req = self._req("DELETE", "service-token", self._user())
        self.assertTrue(self.permission.has_permission(req, None))

    def test_service_token_allows_post(self):
        req = self._req("POST", "service-token", self._user())
        self.assertTrue(self.permission.has_permission(req, None))

    def test_service_token_allows_get(self):
        req = self._req("GET", "service-token", self._user())
        self.assertTrue(self.permission.has_permission(req, None))

    # --- staff / superuser ---

    def test_superuser_allows_delete(self):
        req = self._req("DELETE", None, self._user(is_superuser=True, is_staff=True))
        self.assertTrue(self.permission.has_permission(req, None))

    def test_staff_allows_post(self):
        req = self._req("POST", None, self._user(is_staff=True))
        self.assertTrue(self.permission.has_permission(req, None))

    def test_staff_allows_delete(self):
        req = self._req("DELETE", None, self._user(is_staff=True))
        self.assertTrue(self.permission.has_permission(req, None))

    # --- patient (session auth, non-staff) ---

    def test_patient_allows_get(self):
        req = self._req("GET", None, self._user())
        self.assertTrue(self.permission.has_permission(req, None))

    def test_patient_allows_patch(self):
        req = self._req("PATCH", None, self._user())
        self.assertTrue(self.permission.has_permission(req, None))

    def test_patient_denies_delete(self):
        req = self._req("DELETE", None, self._user())
        self.assertFalse(self.permission.has_permission(req, None))

    def test_patient_denies_post(self):
        req = self._req("POST", None, self._user())
        self.assertFalse(self.permission.has_permission(req, None))

    def test_patient_denies_put(self):
        req = self._req("PUT", None, self._user())
        self.assertFalse(self.permission.has_permission(req, None))

    # --- unauthenticated ---

    def test_unauthenticated_denies_get(self):
        from django.contrib.auth.models import AnonymousUser
        req = self._req("GET", None, AnonymousUser())
        self.assertFalse(self.permission.has_permission(req, None))

    # --- Firebase / partner auth (TokenClaims) ---

    def test_firebase_patient_denies_delete(self):
        from patient_portal.api.providers.base import TokenClaims
        claims = TokenClaims(issuer="https://securetoken.google.com/proj",
                             sub="uid1", email="p@test.com", name="P", raw={})
        req = self._req("DELETE", claims, self._user())
        self.assertFalse(self.permission.has_permission(req, None))

    def test_firebase_patient_denies_post(self):
        from patient_portal.api.providers.base import TokenClaims
        claims = TokenClaims(issuer="https://securetoken.google.com/proj",
                             sub="uid2", email="p2@test.com", name="P2", raw={})
        req = self._req("POST", claims, self._user())
        self.assertFalse(self.permission.has_permission(req, None))

    def test_firebase_patient_allows_patch(self):
        from patient_portal.api.providers.base import TokenClaims
        claims = TokenClaims(issuer="https://securetoken.google.com/proj",
                             sub="uid3", email="p3@test.com", name="P3", raw={})
        req = self._req("PATCH", claims, self._user())
        self.assertTrue(self.permission.has_permission(req, None))

    def test_firebase_staff_allows_delete(self):
        from patient_portal.api.providers.base import TokenClaims
        claims = TokenClaims(issuer="https://securetoken.google.com/proj",
                             sub="uid4", email="s@test.com", name="S", raw={})
        req = self._req("DELETE", claims, self._user(is_staff=True))
        self.assertTrue(self.permission.has_permission(req, None))


# ---------------------------------------------------------------------------
# Person ID enumeration fix — TODO #4
# ---------------------------------------------------------------------------

class PersonIdEnumerationTest(FhirUploadBase):
    """bulk_delete error responses must not echo back submitted person IDs.

    Returning f'Person {person_id} not found' lets an attacker confirm whether
    a given person_id exists in the system.  Error strings must be generic.
    """

    def test_nonexistent_person_error_is_generic(self):
        """DELETE bulk_delete with an unknown ID returns generic 'Person not found.'."""
        resp = self.client.delete(
            '/api/patient-info/bulk_delete/',
            {'person_ids': [999999987]},
            format='json',
        )
        self.assertEqual(resp.status_code, 200)
        errors = resp.data.get('errors', [])
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0], 'Person not found.')
        # The numeric ID must not appear anywhere in the response body
        self.assertNotIn('999999987', str(resp.data))

    def test_successful_delete_not_affected(self):
        """Deleting an existing person still works correctly after the fix."""
        from omop_core.models import Person as P
        p = P.objects.create(
            person_id=78901,
            given_name='Tmp',
            family_name='Delete',
            year_of_birth=1990,
            gender_source_value='unknown',
            race_source_value='unknown',
            ethnicity_source_value='unknown',
        )
        resp = self.client.delete(
            '/api/patient-info/bulk_delete/',
            {'person_ids': [78901]},
            format='json',
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data['deleted_count'], 1)
        self.assertEqual(resp.data['errors'], [])
        self.assertFalse(P.objects.filter(person_id=78901).exists())


# ---------------------------------------------------------------------------
# Disease persistence tests — issues #110 / #113
# ---------------------------------------------------------------------------

class DiseasePersistenceTest(_SmartBase):
    """PATCH /api/patient-info/{person_id}/ must preserve PatientInfo.disease.

    When the user saves a disease selection the serializer writes it directly to
    PatientInfo.  _sync_condition then creates a ConditionOccurrence to mirror
    that change in the OMOP tables.  That post_save would normally trigger
    refresh_patient_info → _clear_derived_fields → disease wiped.

    The fix sets _skip_patient_info_refresh = True on the new ConditionOccurrence
    so the user's selection survives the round-trip.
    """

    PERSON_ID = 95001

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        # Fresh person and empty PatientInfo for this class
        cls.dp_person = Person.objects.create(
            person_id=cls.PERSON_ID,
            given_name='Disease',
            family_name='PersistTest',
            year_of_birth=1975,
            gender_source_value='female',
            race_source_value='unknown',
            ethnicity_source_value='unknown',
        )
        PatientInfo.objects.get_or_create(
            person=cls.dp_person,
            defaults={'organization': cls.organization},
        )

    # ------------------------------------------------------------------ #
    # Issue #110: disease persists across a PATCH + DB re-fetch cycle     #
    # ------------------------------------------------------------------ #

    def test_disease_survives_patch_for_follicular_lymphoma(self):
        """PATCH disease='Follicular Lymphoma' stays in DB after sync_to_omop."""
        resp = self.write_client.patch(
            f'/api/patient-info/{self.PERSON_ID}/',
            {'disease': 'Follicular Lymphoma'},
            format='json',
        )
        self.assertEqual(resp.status_code, 200, resp.data)

        pi = PatientInfo.objects.get(person=self.dp_person)
        self.assertEqual(
            pi.disease, 'Follicular Lymphoma',
            'PatientInfo.disease was overwritten after PATCH — refresh_patient_info '
            'must not run from _sync_condition (issue #110)',
        )

    def test_disease_survives_patch_for_cll(self):
        """PATCH disease='Chronic Lymphocytic Leukemia (CLL)' stays in DB."""
        resp = self.write_client.patch(
            f'/api/patient-info/{self.PERSON_ID}/',
            {'disease': 'Chronic Lymphocytic Leukemia (CLL)'},
            format='json',
        )
        self.assertEqual(resp.status_code, 200, resp.data)

        pi = PatientInfo.objects.get(person=self.dp_person)
        self.assertEqual(
            pi.disease, 'Chronic Lymphocytic Leukemia (CLL)',
            'PatientInfo.disease was overwritten after PATCH — '
            'CLL selection must persist (issue #110)',
        )

    def test_disease_survives_patch_for_multiple_myeloma(self):
        """PATCH disease='Multiple Myeloma' stays in DB."""
        resp = self.write_client.patch(
            f'/api/patient-info/{self.PERSON_ID}/',
            {'disease': 'Multiple Myeloma'},
            format='json',
        )
        self.assertEqual(resp.status_code, 200, resp.data)

        pi = PatientInfo.objects.get(person=self.dp_person)
        self.assertEqual(pi.disease, 'Multiple Myeloma')

    def test_get_after_patch_returns_saved_disease(self):
        """GET /api/patient-info/{id}/ after PATCH returns the saved disease value.

        Simulates the navigation-away-and-back scenario from issue #110.
        """
        self.write_client.patch(
            f'/api/patient-info/{self.PERSON_ID}/',
            {'disease': 'Follicular Lymphoma'},
            format='json',
        )

        get_resp = self.read_client.get(f'/api/patient-info/{self.PERSON_ID}/')
        self.assertEqual(get_resp.status_code, 200)
        self.assertEqual(
            get_resp.data['patient_info']['disease'], 'Follicular Lymphoma',
            'GET after PATCH returned wrong disease — field was overwritten server-side '
            '(issue #110)',
        )

    # ------------------------------------------------------------------ #
    # Issue #113: _skip_patient_info_refresh flag prevents OMOP overwrite #
    # ------------------------------------------------------------------ #

    def test_disease_survives_sync_to_omop(self):
        """disease persists after sync_to_omop runs _sync_condition directly.

        We verify this by checking that PatientInfo.disease is unchanged
        immediately after sync_to_omop runs (no extra DB write occurred).
        """
        from omop_core.services.omop_write_service import sync_to_omop
        from datetime import date

        pi = PatientInfo.objects.get(person=self.dp_person)
        pi.disease = 'Follicular Lymphoma'
        pi.save(update_fields=['disease'])

        # Call sync_to_omop directly — this runs _sync_condition internally
        sync_to_omop(pi, {'disease'}, changed_data={'disease': 'Follicular Lymphoma'})

        pi.refresh_from_db()
        self.assertEqual(
            pi.disease, 'Follicular Lymphoma',
            'sync_to_omop wiped PatientInfo.disease — _skip_patient_info_refresh '
            'not set on ConditionOccurrence (issue #113)',
        )


class FhirRxNavIntegrationTest(_SmartBase):
    """FHIR upload for a drug unknown in local vocab → RxNav called → concept resolved."""

    def _fhir_file(self, drug_name, filename='rxnav_test.json'):
        """Build a multipart-upload file object for the given drug name."""
        bundle = {
            'resourceType': 'Bundle',
            'type': 'collection',
            'entry': [
                {'resource': {
                    'resourceType': 'Patient',
                    'id': 'rxnav-test-pt-1',
                    'name': [{'family': 'RxNavTest', 'given': ['Patient']}],
                    'gender': 'female',
                    'birthDate': '1970-01-01',
                }},
                {'resource': {
                    'resourceType': 'MedicationStatement',
                    'id': 'rxnav-med-1',
                    'status': 'completed',
                    'subject': {'reference': 'Patient/rxnav-test-pt-1'},
                    'medicationCodeableConcept': {'text': drug_name},
                    'effectivePeriod': {'start': '2023-01-15', 'end': '2023-07-01'},
                    'extension': [
                        {'url': 'http://ctomop.io/fhir/StructureDefinition/therapy-line',
                         'valueInteger': 1},
                    ],
                }},
            ],
        }
        f = io.BytesIO(json.dumps(bundle).encode('utf-8'))
        f.name = filename
        return f

    def test_fhir_upload_uses_rxnav_for_unknown_drug(self):
        """FHIR bundle with unknown drug name → RxNav resolves it → DrugExposure concept set."""
        from unittest.mock import patch
        from omop_core.models import DrugExposure

        with patch(
            'omop_core.services.rxnav_service._rxnav_lookup',
            return_value=('1421', 'bortezomib'),
        ):
            response = self.write_client.post(
                '/api/patient-info/upload_fhir/',
                {'file': self._fhir_file('Velcade')},
                format='multipart',
            )

        self.assertIn(response.status_code, [200, 201])
        de = DrugExposure.objects.filter(drug_source_value='Velcade').first()
        self.assertIsNotNone(de, 'DrugExposure for Velcade not created')
        self.assertNotEqual(
            de.drug_concept_id, 0,
            'drug_concept_id should be set via RxNav; got 0',
        )

    def test_fhir_upload_unknown_drug_rxnav_fails_gracefully(self):
        """RxNav returns nothing → FHIR upload still succeeds, uses fallback concept."""
        from unittest.mock import patch
        from omop_core.models import DrugExposure

        with patch(
            'omop_core.services.rxnav_service._rxnav_lookup',
            return_value=(None, None),
        ):
            response = self.write_client.post(
                '/api/patient-info/upload_fhir/',
                {'file': self._fhir_file('completely-unknown-drug-xyz', 'rxnav_fallback.json')},
                format='multipart',
            )

        self.assertIn(response.status_code, [200, 201])


# =============================================================================
# Survey models and API tests
# =============================================================================

class SurveyModelTest(_SmartBase):
    """Survey and PatientSurveyResponse model-level tests."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        from omop_core.models import Survey, PatientSurveyResponse
        cls.survey = Survey.objects.create(
            name='mm-quality-of-life',
            title='Multiple Myeloma Quality of Life',
            description='Patient-reported outcomes for MM patients.',
            status=Survey.STATUS_ACTIVE,
            disease='Multiple Myeloma',
            pages=[
                {
                    'name': 'page1',
                    'title': 'Symptoms',
                    'inputs': [
                        {'name': 'fatigue', 'label': 'Fatigue level', 'type': 'rating',
                         'data': {'maxRating': 10}},
                        {'name': 'pain', 'label': 'Pain level', 'type': 'rating',
                         'data': {'maxRating': 10}},
                        {'name': 'notes', 'label': 'Additional notes', 'type': 'textarea'},
                    ],
                }
            ],
            estimated_minutes=5,
        )
        cls.response = PatientSurveyResponse.objects.create(
            person=cls.person,
            survey=cls.survey,
            values={'fatigue': 7, 'pain': 4, 'notes': 'Feeling tired'},
            values_dates={'fatigue': '2024-03-01T10:00:00Z', 'pain': '2024-03-01T10:01:00Z'},
            percent_complete=66,
        )

    def test_survey_saved_to_db(self):
        from omop_core.models import Survey
        s = Survey.objects.get(name='mm-quality-of-life')
        self.assertEqual(s.title, 'Multiple Myeloma Quality of Life')
        self.assertEqual(s.status, Survey.STATUS_ACTIVE)
        self.assertEqual(s.disease, 'Multiple Myeloma')
        self.assertEqual(len(s.pages), 1)
        self.assertEqual(len(s.pages[0]['inputs']), 3)

    def test_survey_pages_json_roundtrip(self):
        from omop_core.models import Survey
        s = Survey.objects.get(name='mm-quality-of-life')
        self.assertEqual(s.pages[0]['inputs'][0]['name'], 'fatigue')
        self.assertEqual(s.pages[0]['inputs'][0]['data']['maxRating'], 10)

    def test_response_saved_to_db(self):
        from omop_core.models import PatientSurveyResponse
        r = PatientSurveyResponse.objects.get(person=self.person, survey=self.survey)
        self.assertEqual(r.values['fatigue'], 7)
        self.assertEqual(r.values['pain'], 4)
        self.assertEqual(r.percent_complete, 66)

    def test_response_person_survey_unique(self):
        from omop_core.models import PatientSurveyResponse
        from django.db import IntegrityError
        with self.assertRaises(IntegrityError):
            PatientSurveyResponse.objects.create(
                person=self.person,
                survey=self.survey,
                values={},
            )

    def test_survey_external_id_nullable(self):
        from omop_core.models import Survey
        s = Survey.objects.get(name='mm-quality-of-life')
        self.assertIsNone(s.external_id)

    def test_survey_str(self):
        self.assertEqual(str(self.survey), 'Multiple Myeloma Quality of Life')

    def test_response_str(self):
        self.assertIn(str(self.person.person_id), str(self.response))
        self.assertIn('mm-quality-of-life', str(self.response))


class SurveyAPITest(_SmartBase):
    """REST API tests for /api/surveys/ and /api/survey-responses/."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        from oauth2_provider.models import Application, AccessToken
        from django.utils import timezone as tz
        import datetime
        # Internal app (no org) — survey template writes require no org-scoping.
        cls._internal_app = Application.objects.create(
            name='Internal Survey Service',
            client_id='internal-survey-client-id',
            client_type=Application.CLIENT_CONFIDENTIAL,
            authorization_grant_type=Application.GRANT_CLIENT_CREDENTIALS,
            user=cls.foundation_user,
        )
        cls._internal_write_token = AccessToken.objects.create(
            user=cls.foundation_user,
            application=cls._internal_app,
            token='internal-survey-write-token-s1',
            expires=tz.now() + datetime.timedelta(hours=1),
            scope='patient/*.read patient/*.write openid launch/patient',
        )
        from omop_core.models import Survey, PatientSurveyResponse
        cls.survey = Survey.objects.create(
            name='cll-proms',
            title='CLL Patient-Reported Outcomes',
            status=Survey.STATUS_ACTIVE,
            disease='Chronic Lymphocytic Leukemia (CLL)',
            pages=[{'name': 'p1', 'inputs': [
                {'name': 'fatigue', 'label': 'Fatigue', 'type': 'rating'}
            ]}],
        )
        cls.response = PatientSurveyResponse.objects.create(
            person=cls.person,
            survey=cls.survey,
            values={'fatigue': 3},
            percent_complete=100,
        )

    @property
    def survey_write_client(self):
        """Internal (no-org) client for mutating shared survey templates."""
        return self._bearer(self._internal_write_token.token)

    # --- Survey CRUD ---

    def test_list_surveys_requires_auth(self):
        res = APIClient().get('/api/surveys/')
        self.assertEqual(res.status_code, 401)

    def test_list_surveys(self):
        res = self.read_client.get('/api/surveys/')
        self.assertEqual(res.status_code, 200)
        data = res.data if isinstance(res.data, list) else res.data.get('results', [])
        names = [s['name'] for s in data]
        self.assertIn('cll-proms', names)

    def test_filter_surveys_by_disease(self):
        res = self.read_client.get('/api/surveys/?disease=Chronic+Lymphocytic+Leukemia+%28CLL%29')
        self.assertEqual(res.status_code, 200)
        data = res.data if isinstance(res.data, list) else res.data.get('results', [])
        self.assertTrue(all(s['disease'] == 'Chronic Lymphocytic Leukemia (CLL)' for s in data))

    def test_filter_surveys_by_status(self):
        res = self.read_client.get('/api/surveys/?status=ACTIVE')
        self.assertEqual(res.status_code, 200)
        data = res.data if isinstance(res.data, list) else res.data.get('results', [])
        self.assertTrue(all(s['status'] == 'ACTIVE' for s in data))

    def test_create_survey_requires_write_scope(self):
        payload = {
            'name': 'new-survey', 'title': 'New Survey',
            'status': 'DRAFT', 'disease': 'Breast Cancer', 'pages': [],
        }
        res = self.read_client.post('/api/surveys/', payload, format='json')
        self.assertEqual(res.status_code, 403)

    def test_create_survey(self):
        payload = {
            'name': 'breast-cancer-proms', 'title': 'Breast Cancer PROMs',
            'status': 'ACTIVE', 'disease': 'Breast Cancer',
            'pages': [{'name': 'p1', 'inputs': [
                {'name': 'q1', 'label': 'How are you?', 'type': 'radioGroup',
                 'data': {'options': [{'value': 'good', 'label': 'Good'},
                                      {'value': 'poor', 'label': 'Poor'}]}}
            ]}],
        }
        res = self.survey_write_client.post('/api/surveys/', payload, format='json')
        self.assertEqual(res.status_code, 201)
        self.assertEqual(res.data['name'], 'breast-cancer-proms')
        self.assertEqual(len(res.data['pages'][0]['inputs']), 1)

    def test_retrieve_survey(self):
        res = self.read_client.get(f'/api/surveys/{self.survey.id}/')
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data['name'], 'cll-proms')
        self.assertIn('pages', res.data)

    def test_update_survey_status(self):
        from omop_core.models import Survey
        s = Survey.objects.create(
            name='to-archive', title='To Archive',
            status=Survey.STATUS_ACTIVE, pages=[],
        )
        res = self.survey_write_client.patch(f'/api/surveys/{s.id}/', {'status': 'ARCHIVED'}, format='json')
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data['status'], 'ARCHIVED')

    # --- Survey response CRUD ---

    def test_list_responses_requires_auth(self):
        res = APIClient().get('/api/survey-responses/')
        self.assertEqual(res.status_code, 401)

    def test_list_responses_filtered_by_person(self):
        res = self.read_client.get(f'/api/survey-responses/?person_id={self.person.person_id}')
        self.assertEqual(res.status_code, 200)
        data = res.data if isinstance(res.data, list) else res.data.get('results', [])
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]['values']['fatigue'], 3)

    def test_list_responses_includes_survey_title(self):
        res = self.read_client.get(f'/api/survey-responses/?person_id={self.person.person_id}')
        data = res.data if isinstance(res.data, list) else res.data.get('results', [])
        self.assertEqual(data[0]['survey_title'], 'CLL Patient-Reported Outcomes')

    def test_create_response(self):
        from omop_core.models import Survey
        s2 = Survey.objects.create(
            name='mm-proms-2', title='MM PROMs v2',
            status=Survey.STATUS_ACTIVE, pages=[],
        )
        payload = {
            'person': self.person.person_id,
            'survey': s2.id,
            'values': {'pain': 5, 'fatigue': 8},
            'percent_complete': 50,
        }
        res = self.write_client.post('/api/survey-responses/', payload, format='json')
        self.assertEqual(res.status_code, 201)
        self.assertEqual(res.data['values']['pain'], 5)
        self.assertEqual(res.data['percent_complete'], 50)

    def test_patch_response_autosave(self):
        """PATCH merges new answers without overwriting existing ones."""
        from omop_core.models import PatientSurveyResponse
        # Seed two fields so we can verify the pre-existing one survives the PATCH.
        self.response.values = {'fatigue': 3, 'pain': 5}
        self.response.save()
        res = self.write_client.patch(
            f'/api/survey-responses/{self.response.id}/',
            {'values': {'fatigue': 9}, 'percent_complete': 100},
            format='json',
        )
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data['values']['fatigue'], 9)
        self.assertEqual(res.data['values']['pain'], 5, 'pre-existing key should survive merge')
        self.assertEqual(res.data['percent_complete'], 100)

    def test_response_not_writable_with_read_token(self):
        payload = {
            'person': self.person.person_id,
            'survey': self.survey.id,
            'values': {'fatigue': 1},
        }
        res = self.read_client.post('/api/survey-responses/', payload, format='json')
        self.assertEqual(res.status_code, 403)


class SurveyModelExtendedTest(_SmartBase):
    """Additional model-level tests for Survey and PatientSurveyResponse."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        from omop_core.models import Survey
        cls.survey = Survey.objects.create(
            name='fl-proms',
            title='FL Quality of Life',
            status=Survey.STATUS_ACTIVE,
            disease='Follicular Lymphoma',
            pages=[],
        )

    def test_survey_estimated_minutes_nullable(self):
        from omop_core.models import Survey
        s = Survey.objects.get(name='fl-proms')
        self.assertIsNone(s.estimated_minutes)

    def test_survey_without_disease_allowed(self):
        from omop_core.models import Survey
        s = Survey.objects.create(
            name='no-disease-survey',
            title='General Survey',
            status=Survey.STATUS_DRAFT,
            pages=[],
        )
        self.assertEqual('', s.disease)

    def test_response_values_dates_roundtrip(self):
        from omop_core.models import PatientSurveyResponse
        r = PatientSurveyResponse.objects.create(
            person=self.person,
            survey=self.survey,
            values={'q1': 'yes'},
            values_dates={'q1': '2025-01-15T09:30:00Z'},
        )
        r.refresh_from_db()
        self.assertEqual(r.values_dates['q1'], '2025-01-15T09:30:00Z')

    def test_response_consent_fields_nullable(self):
        from omop_core.models import PatientSurveyResponse
        r = PatientSurveyResponse.objects.create(
            person=self.person,
            survey=self.survey,
            values={},
        )
        self.assertIsNone(r.consent_date)
        self.assertIsNone(r.consent_signature)
        self.assertIsNone(r.completed_at)

    def test_response_timestamps_auto_set(self):
        from omop_core.models import PatientSurveyResponse
        r = PatientSurveyResponse.objects.create(
            person=self.person,
            survey=self.survey,
            values={},
        )
        self.assertIsNotNone(r.created_at)
        self.assertIsNotNone(r.updated_at)

    def test_survey_timestamps_auto_set(self):
        s = self.survey
        self.assertIsNotNone(s.created_at)
        self.assertIsNotNone(s.updated_at)


class SurveyAPIExtendedTest(_SmartBase):
    """Additional API tests for edge cases and merge behaviour."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        from oauth2_provider.models import Application, AccessToken
        from django.utils import timezone as tz
        import datetime
        # Internal app (no org) — survey template writes require no org-scoping.
        cls._internal_app = Application.objects.create(
            name='Internal Survey Service (Ext)',
            client_id='internal-survey-client-id-ext',
            client_type=Application.CLIENT_CONFIDENTIAL,
            authorization_grant_type=Application.GRANT_CLIENT_CREDENTIALS,
            user=cls.foundation_user,
        )
        cls._internal_write_token = AccessToken.objects.create(
            user=cls.foundation_user,
            application=cls._internal_app,
            token='internal-survey-write-token-s2',
            expires=tz.now() + datetime.timedelta(hours=1),
            scope='patient/*.read patient/*.write openid launch/patient',
        )
        from omop_core.models import Survey, PatientSurveyResponse
        cls.survey = Survey.objects.create(
            name='mm-ext-test',
            title='MM Extended Test Survey',
            status=Survey.STATUS_ACTIVE,
            disease='Multiple Myeloma',
            pages=[{'name': 'p1', 'inputs': [
                {'name': 'fatigue', 'label': 'Fatigue', 'type': 'rating'},
                {'name': 'pain', 'label': 'Pain', 'type': 'rating'},
            ]}],
        )
        cls.response = PatientSurveyResponse.objects.create(
            person=cls.person,
            survey=cls.survey,
            values={'fatigue': 5, 'pain': 3},
            values_dates={
                'fatigue': '2025-01-01T10:00:00Z',
                'pain': '2025-01-01T10:00:00Z',
            },
            percent_complete=50,
        )

    @property
    def survey_write_client(self):
        """Internal (no-org) client for mutating shared survey templates."""
        return self._bearer(self._internal_write_token.token)

    def test_retrieve_survey_404(self):
        res = self.read_client.get('/api/surveys/999999/')
        self.assertEqual(res.status_code, 404)

    def test_retrieve_response_404(self):
        res = self.read_client.get('/api/survey-responses/999999/')
        self.assertEqual(res.status_code, 404)

    def test_patch_response_merges_without_overwriting(self):
        """PATCH with one key must not erase the other existing key."""
        res = self.write_client.patch(
            f'/api/survey-responses/{self.response.id}/',
            {'values': {'fatigue': 9}},
            format='json',
        )
        self.assertEqual(res.status_code, 200)
        # fatigue updated
        self.assertEqual(res.data['values']['fatigue'], 9)
        # pain must still be present
        self.assertIn('pain', res.data['values'])
        self.assertEqual(res.data['values']['pain'], 3)

    def test_patch_response_updates_values_dates(self):
        """PATCH with values_dates merges timestamps."""
        res = self.write_client.patch(
            f'/api/survey-responses/{self.response.id}/',
            {
                'values': {'fatigue': 8},
                'values_dates': {'fatigue': '2025-06-01T12:00:00Z'},
            },
            format='json',
        )
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data['values_dates']['fatigue'], '2025-06-01T12:00:00Z')
        # pain timestamp preserved
        self.assertIn('pain', res.data['values_dates'])

    def test_patch_response_sets_completed_at(self):
        from omop_core.models import Survey, PatientSurveyResponse
        s = Survey.objects.create(
            name='completion-test', title='Completion Test',
            status=Survey.STATUS_ACTIVE, pages=[],
        )
        r = PatientSurveyResponse.objects.create(
            person=self.person, survey=s, values={},
        )
        res = self.write_client.patch(
            f'/api/survey-responses/{r.id}/',
            {'completed_at': '2025-06-03T14:00:00Z', 'percent_complete': 100},
            format='json',
        )
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data['percent_complete'], 100)
        self.assertIsNotNone(res.data['completed_at'])

    def test_create_response_duplicate_returns_400(self):
        """Creating a second response for (person, survey) must fail with 400."""
        payload = {
            'person': self.person.person_id,
            'survey': self.survey.id,
            'values': {'fatigue': 1},
        }
        res = self.write_client.post('/api/survey-responses/', payload, format='json')
        self.assertEqual(res.status_code, 400)

    def test_list_responses_filtered_by_survey(self):
        res = self.read_client.get(f'/api/survey-responses/?survey={self.survey.id}')
        self.assertEqual(res.status_code, 200)
        data = res.data if isinstance(res.data, list) else res.data.get('results', [])
        self.assertTrue(all(r['survey'] == self.survey.id for r in data))

    def test_response_includes_survey_name(self):
        res = self.read_client.get(f'/api/survey-responses/?person_id={self.person.person_id}')
        data = res.data if isinstance(res.data, list) else res.data.get('results', [])
        matching = [r for r in data if r['survey'] == self.survey.id]
        self.assertTrue(len(matching) > 0)
        self.assertEqual(matching[0]['survey_name'], 'mm-ext-test')

    def test_filter_surveys_unknown_disease_returns_empty(self):
        res = self.read_client.get('/api/surveys/?disease=UnknownDiseaseXYZ')
        self.assertEqual(res.status_code, 200)
        data = res.data if isinstance(res.data, list) else res.data.get('results', [])
        self.assertEqual(len(data), 0)

    def test_create_survey_missing_name_returns_400(self):
        payload = {'title': 'No Name Survey', 'status': 'ACTIVE', 'pages': []}
        res = self.survey_write_client.post('/api/surveys/', payload, format='json')
        self.assertEqual(res.status_code, 400)

    def test_create_survey_with_external_id(self):
        payload = {
            'name': 'ext-id-survey',
            'title': 'External ID Survey',
            'status': 'DRAFT',
            'pages': [],
            'external_id': 'firestore-doc-abc123',
        }
        res = self.survey_write_client.post('/api/surveys/', payload, format='json')
        self.assertEqual(res.status_code, 201)
        self.assertEqual(res.data['external_id'], 'firestore-doc-abc123')

    def test_update_survey_blocked_with_read_token(self):
        res = self.read_client.patch(
            f'/api/surveys/{self.survey.id}/',
            {'status': 'ARCHIVED'},
            format='json',
        )
        self.assertEqual(res.status_code, 403)

    def test_delete_survey_returns_405(self):
        res = self.write_client.delete(f'/api/surveys/{self.survey.id}/')
        self.assertEqual(res.status_code, 405)

    def test_duplicate_survey_name_returns_400(self):
        payload = {
            'name': 'mm-ext-test',  # same as cls.survey
            'title': 'Duplicate Name Survey',
            'status': 'DRAFT',
            'pages': [],
        }
        res = self.survey_write_client.post('/api/surveys/', payload, format='json')
        self.assertEqual(res.status_code, 400)


# ---------------------------------------------------------------------------
# Cross-org isolation for survey responses
# ---------------------------------------------------------------------------

class SurveyCrossOrgTest(MultiTenantIsolationTest):
    """Org-scoped tokens must not read or write another org's survey responses."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        from omop_core.models import Survey, PatientSurveyResponse
        from oauth2_provider.models import AccessToken
        from django.utils import timezone as tz
        import datetime

        cls.survey = Survey.objects.create(
            name='cross-org-survey',
            title='Cross Org Survey',
            status=Survey.STATUS_ACTIVE,
            pages=[],
        )
        cls.response_a = PatientSurveyResponse.objects.create(
            person=cls.person_a,
            survey=cls.survey,
            values={'pain': 3},
        )

        # Write token for org A
        cls.write_token_a = AccessToken.objects.create(
            user=cls.user_a,
            application=cls.app_a,
            token='org-a-write-token',
            expires=tz.now() + datetime.timedelta(hours=1),
            scope='patient/*.write',
        )

    def test_org_a_cannot_list_org_b_responses(self):
        """Org A token listing responses filtered by org-B person gets empty result."""
        resp = self._client(self.token_a.token).get(
            f'/api/survey-responses/?person_id={self.person_b.person_id}'
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.data if isinstance(resp.data, list) else resp.data.get('results', [])
        self.assertEqual(len(data), 0, 'Org A must not see Org B survey responses')

    def test_org_a_cannot_create_response_for_org_b_patient(self):
        """Org A write token must be denied when posting a response for Org B's patient."""
        from omop_core.models import Survey
        payload = {
            'person': self.person_b.person_id,
            'survey': self.survey.id,
            'values': {'pain': 9},
        }
        resp = self._client(self.write_token_a.token).post(
            '/api/survey-responses/', payload, format='json'
        )
        self.assertIn(resp.status_code, [403, 404],
                      'Org A must not create a response for Org B patient')

    def test_org_a_sees_own_responses(self):
        """Org A token can list its own survey responses."""
        resp = self._client(self.token_a.token).get(
            f'/api/survey-responses/?person_id={self.person_a.person_id}'
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.data if isinstance(resp.data, list) else resp.data.get('results', [])
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]['values']['pain'], 3)

    def test_org_a_cannot_patch_org_b_response(self):
        """Org A write token must be denied when patching a response owned by Org B's patient."""
        from omop_core.models import PatientSurveyResponse
        response_b = PatientSurveyResponse.objects.create(
            person=self.person_b,
            survey=self.survey,
            values={'fatigue': 2},
        )
        resp = self._client(self.write_token_a.token).patch(
            f'/api/survey-responses/{response_b.id}/',
            {'values': {'fatigue': 9}},
            format='json',
        )
        self.assertIn(resp.status_code, [403, 404],
                      'Org A must not patch a response for Org B patient')

    def test_org_token_cannot_write_survey_template(self):
        """An org-linked write token must not be able to mutate shared survey templates."""
        resp = self._client(self.write_token_a.token).patch(
            f'/api/surveys/{self.survey.id}/',
            {'status': 'ARCHIVED'},
            format='json',
        )
        self.assertEqual(resp.status_code, 403,
                         'Partner org token must not archive shared survey templates')

    def test_put_on_survey_response_is_not_allowed(self):
        """PUT is disabled on survey responses — use PATCH for incremental autosave."""
        resp = self._client(self.write_token_a.token).put(
            f'/api/survey-responses/{self.response_a.id}/',
            {'person': self.person_a.person_id, 'survey': self.survey.id, 'values': {'pain': 9}},
            format='json',
        )
        self.assertEqual(resp.status_code, 405,
                         'PUT must be disabled on survey responses')


# ---------------------------------------------------------------------------
# SCT fields tests (PR #115)
# ---------------------------------------------------------------------------

class SctEligibilityVocabTest(FhirUploadBase):
    """Verify the sct-eligibility vocabulary endpoint returns expected values."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        for code, title in [
            ('eligibleAuto',   'eligible for autologous SCT'),
            ('eligibleAllo',   'eligible for allogeneic SCT'),
            ('ineligibleAuto', 'ineligible for autologous SCT'),
            ('ineligibleAllo', 'ineligible for allogeneic SCT'),
        ]:
            SctEligibility.objects.get_or_create(code=code, defaults={'title': title})

    def test_vocab_endpoint_returns_four_values(self):
        resp = self.client.get('/api/vocabularies/sct-eligibility/')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(resp.data), 4)

    def test_vocab_codes_present(self):
        resp = self.client.get('/api/vocabularies/sct-eligibility/')
        codes = {item['code'] for item in resp.data}
        self.assertIn('eligibleAuto', codes)
        self.assertIn('ineligibleAllo', codes)


class SctFieldsModelTest(FhirUploadBase):
    """Verify sct_date, sct_eligibility, and stem_cell_transplant_history persist correctly."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.person = Person.objects.create(person_id=88001)
        cls.patient = PatientInfo.objects.create(
            person=cls.person,
            disease='Multiple Myeloma',
            stem_cell_transplant_history=['autologous SCT'],
            sct_date=date(2022, 5, 10),
            sct_eligibility=['eligible for autologous SCT'],
        )

    def test_sct_fields_saved_to_db(self):
        p = PatientInfo.objects.get(pk=self.patient.pk)
        self.assertEqual(p.stem_cell_transplant_history, ['autologous SCT'])
        self.assertEqual(str(p.sct_date), '2022-05-10')
        self.assertEqual(p.sct_eligibility, ['eligible for autologous SCT'])

    def test_sct_fields_in_api_response(self):
        # retrieve uses person_id in URL (ViewSet design); response is wrapped in patient_info
        resp = self.client.get(f'/api/patient-info/{self.person.person_id}/')
        self.assertEqual(resp.status_code, 200)
        pi_data = resp.data['patient_info']
        self.assertIn('sct_date', pi_data)
        self.assertIn('sct_eligibility', pi_data)
        self.assertIn('stem_cell_transplant_history', pi_data)
        self.assertEqual(pi_data['sct_date'], '2022-05-10')

    def test_sct_date_future_rejected(self):
        from datetime import date, timedelta
        future = (date.today() + timedelta(days=30)).isoformat()
        resp = self.client.patch(
            f'/api/patient-info/{self.person.person_id}/',
            {'sct_date': future},
            format='json',
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIn('sct_date', resp.data)

    def test_sct_eligibility_patch(self):
        resp = self.client.patch(
            f'/api/patient-info/{self.person.person_id}/',
            {'sct_eligibility': ['eligible for autologous SCT', 'ineligible for allogeneic SCT']},
            format='json',
        )
        self.assertEqual(resp.status_code, 200)
        self.patient.refresh_from_db()
        self.assertIn('eligible for autologous SCT', self.patient.sct_eligibility)


class SctFhirUploadTest(FhirUploadBase):
    """Verify that SCT extensions in a FHIR Patient resource are mapped to PatientInfo."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        from omop_core.models import StemCellTransplant, SctEligibility
        for code, title in [
            ('autologousSCT', 'autologous SCT'),
            ('allogeneicSCT', 'allogeneic SCT'),
            ('tandemSCT',     'tandem SCT'),
        ]:
            StemCellTransplant.objects.get_or_create(code=code, defaults={'title': title})
        for code, title in [
            ('eligibleAuto',   'eligible for autologous SCT'),
            ('eligibleAllo',   'eligible for allogeneic SCT'),
            ('ineligibleAuto', 'ineligible for autologous SCT'),
            ('ineligibleAllo', 'ineligible for allogeneic SCT'),
        ]:
            SctEligibility.objects.get_or_create(code=code, defaults={'title': title})

    def _upload_sct_bundle(self):
        """FHIR bundle with SCT extensions on the Patient resource."""
        patient_id = 'test-patient-sct-001'
        bundle = {
            'resourceType': 'Bundle',
            'type': 'collection',
            'entry': [
                {
                    'resource': {
                        'resourceType': 'Patient',
                        'id': patient_id,
                        'name': [{'family': 'Jones', 'given': ['Bob']}],
                        'gender': 'male',
                        'birthDate': '1960-07-20',
                        'extension': [
                            {
                                'url': 'http://ctomop.io/fhir/StructureDefinition/mm-sct-date',
                                'valueString': '2021-03-15',
                            },
                            {
                                'url': 'http://ctomop.io/fhir/StructureDefinition/mm-sct-history',
                                'valueString': 'autologous SCT,tandem SCT',
                            },
                            {
                                'url': 'http://ctomop.io/fhir/StructureDefinition/mm-sct-eligibility',
                                'valueString': 'eligible for autologous SCT',
                            },
                        ],
                    }
                },
            ],
        }
        bundle_bytes = json.dumps(bundle).encode('utf-8')
        fhir_file = io.BytesIO(bundle_bytes)
        fhir_file.name = 'sct_bundle.json'
        return self.client.post(
            '/api/patient-info/upload_fhir/',
            {'file': fhir_file},
            format='multipart',
        )

    def _upload_bundle_with_extensions(self, extensions, patient_suffix='002'):
        """Upload a minimal FHIR bundle with the given Patient extensions."""
        bundle = {
            'resourceType': 'Bundle',
            'type': 'collection',
            'entry': [{
                'resource': {
                    'resourceType': 'Patient',
                    'id': f'test-patient-sct-{patient_suffix}',
                    'name': [{'family': f'TestSct{patient_suffix}', 'given': ['X']}],
                    'gender': 'female',
                    'birthDate': '1970-01-01',
                    'extension': extensions,
                }
            }],
        }
        fhir_file = io.BytesIO(json.dumps(bundle).encode())
        fhir_file.name = 'bundle.json'
        return self.client.post(
            '/api/patient-info/upload_fhir/',
            {'file': fhir_file},
            format='multipart',
        )

    def test_sct_extensions_mapped_to_patient_info(self):
        resp = self._upload_sct_bundle()
        self.assertIn(resp.status_code, [200, 201],
                      msg=f'Upload failed: {getattr(resp, "data", resp.content)}')
        pi = PatientInfo.objects.filter(person__family_name='Jones', person__given_name='Bob').first()
        self.assertIsNotNone(pi, 'PatientInfo not created for Bob Jones')
        self.assertEqual(str(pi.sct_date), '2021-03-15')
        self.assertIn('autologous SCT', pi.stem_cell_transplant_history)
        self.assertIn('tandem SCT', pi.stem_cell_transplant_history)
        self.assertIn('eligible for autologous SCT', pi.sct_eligibility)

    def test_invalid_sct_date_string_is_ignored(self):
        """A malformed mm-sct-date value must be silently dropped; upload must still succeed."""
        resp = self._upload_bundle_with_extensions([
            {'url': 'http://ctomop.io/fhir/StructureDefinition/mm-sct-date',
             'valueString': 'not-a-date'},
            {'url': 'http://ctomop.io/fhir/StructureDefinition/mm-sct-history',
             'valueString': 'autologous SCT'},
        ], patient_suffix='003')
        self.assertIn(resp.status_code, [200, 201],
                      msg=f'Upload failed: {getattr(resp, "data", resp.content)}')
        pi = PatientInfo.objects.filter(person__family_name='TestSct003').first()
        self.assertIsNotNone(pi)
        self.assertIsNone(pi.sct_date, 'Invalid sct_date should be dropped, not stored')

    def test_comma_only_sct_history_stores_empty_list(self):
        """A valueString of only commas/whitespace must produce an empty list, not error."""
        resp = self._upload_bundle_with_extensions([
            {'url': 'http://ctomop.io/fhir/StructureDefinition/mm-sct-history',
             'valueString': ',  ,'},
        ], patient_suffix='004')
        self.assertIn(resp.status_code, [200, 201],
                      msg=f'Upload failed: {getattr(resp, "data", resp.content)}')
        pi = PatientInfo.objects.filter(person__family_name='TestSct004').first()
        self.assertIsNotNone(pi)
        self.assertEqual(pi.stem_cell_transplant_history or [], [],
                         'Comma-only valueString should produce an empty list')

    def test_unknown_vocab_tokens_filtered_from_sct_history(self):
        """Tokens not in the StemCellTransplant vocabulary are silently discarded."""
        resp = self._upload_bundle_with_extensions([
            {'url': 'http://ctomop.io/fhir/StructureDefinition/mm-sct-history',
             'valueString': 'autologous SCT,unknown experimental SCT,allogeneic SCT'},
        ], patient_suffix='005')
        self.assertIn(resp.status_code, [200, 201],
                      msg=f'Upload failed: {getattr(resp, "data", resp.content)}')
        pi = PatientInfo.objects.filter(person__family_name='TestSct005').first()
        self.assertIsNotNone(pi)
        self.assertIn('autologous SCT', pi.stem_cell_transplant_history)
        self.assertIn('allogeneic SCT', pi.stem_cell_transplant_history)
        self.assertNotIn('unknown experimental SCT', pi.stem_cell_transplant_history,
                         'Unrecognized vocab token must be filtered out')


# ---------------------------------------------------------------------------
# Data migration remapping tests (migration 0086)
# ---------------------------------------------------------------------------

class SctDataMigrationTest(TestCase):
    """Unit tests for migrate_patientinfo_sct_history (migration 0086).

    Calls the migration function directly using the live apps registry, which is
    equivalent to what Django does when the migration runs against the real DB.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        import importlib
        _mod = importlib.import_module(
            'omop_core.migrations'
            '.0086_seed_sct_eligibility_update_stem_cell_transplant'
        )
        cls._migrate_fn = staticmethod(_mod.migrate_patientinfo_sct_history)

    def _run(self):
        from django.apps import apps as django_apps
        self._migrate_fn(django_apps, None)

    def _make_patient(self, person_id, sct_history):
        person = Person.objects.create(person_id=person_id)
        return PatientInfo.objects.create(
            person=person,
            stem_cell_transplant_history=sct_history,
        )

    def test_old_strings_remapped_to_new_vocabulary(self):
        """All 13 old strings remap to the correct new vocabulary string."""
        CASES = [
            ('prior SCT',                    'autologous SCT'),
            ('prior autologous SCT',         'autologous SCT'),
            ('prior allogeneic SCT',         'allogeneic SCT'),
            ('recent SCT',                   'autologous SCT'),
            ('recent autologous SCT',        'autologous SCT'),
            ('recent allogeneic SCT',        'allogeneic SCT'),
            ('relapsed post-SCT',            'autologous SCT'),
            ('relapsed post-autologous SCT', 'autologous SCT'),
            ('relapsed post-allogeneic SCT', 'allogeneic SCT'),
            ('completed tandem SCT',         'tandem SCT'),
            ('pre-autologous SCT',           'autologous SCT'),
            ('pre-allogeneic SCT',           'allogeneic SCT'),
        ]
        patients = []
        for idx, (old, _) in enumerate(CASES):
            patients.append(self._make_patient(89100 + idx, [old]))

        self._run()

        for (old, expected), pi in zip(CASES, patients):
            pi.refresh_from_db()
            self.assertEqual(
                pi.stem_cell_transplant_history, [expected],
                f'{old!r} should remap to {expected!r}',
            )

    def test_never_received_sct_is_cleared(self):
        """'never received SCT' maps to None and must be removed from the list."""
        pi = self._make_patient(89200, ['never received SCT'])
        self._run()
        pi.refresh_from_db()
        self.assertEqual(pi.stem_cell_transplant_history, [],
                         "'never received SCT' should be cleared to []")

    def test_deduplication_when_multiple_old_strings_map_to_same_value(self):
        """Two old strings that map to the same new string produce only one entry."""
        pi = self._make_patient(89201, ['prior SCT', 'recent SCT'])  # both → 'autologous SCT'
        self._run()
        pi.refresh_from_db()
        self.assertEqual(pi.stem_cell_transplant_history, ['autologous SCT'],
                         'Duplicate new values must be deduplicated')

    def test_mixed_old_strings_remap_correctly(self):
        """Mixed autologous/allogeneic old strings produce distinct new entries."""
        pi = self._make_patient(89202, ['prior autologous SCT', 'prior allogeneic SCT'])
        self._run()
        pi.refresh_from_db()
        self.assertIn('autologous SCT', pi.stem_cell_transplant_history)
        self.assertIn('allogeneic SCT', pi.stem_cell_transplant_history)
        self.assertEqual(len(pi.stem_cell_transplant_history), 2)

    def test_unrecognized_string_is_preserved_not_dropped(self):
        """A string not in the mapping must be kept as-is rather than silently deleted."""
        pi = self._make_patient(89203, ['some future SCT type'])
        self._run()
        pi.refresh_from_db()
        self.assertIn('some future SCT type', pi.stem_cell_transplant_history,
                      'Unrecognized values must be preserved, not silently dropped')

    def test_non_string_items_are_skipped(self):
        """Non-string items (e.g. dicts from old BQ loader) must be removed."""
        pi = self._make_patient(89204, [{'line_number': 1, 'procedures': 'ASCT'}])
        self._run()
        pi.refresh_from_db()
        # Dict items have no valid string mapping and are not strings — they should be dropped.
        self.assertEqual(pi.stem_cell_transplant_history, [],
                         'Non-string items must be removed during migration')

    def test_already_new_vocabulary_passes_through_unchanged(self):
        """Rows already in the new 3-value vocabulary are left unchanged."""
        pi = self._make_patient(89205, ['autologous SCT', 'tandem SCT'])
        self._run()
        pi.refresh_from_db()
        self.assertEqual(pi.stem_cell_transplant_history, ['autologous SCT', 'tandem SCT'])

    def test_empty_list_rows_are_skipped(self):
        """Rows with an empty list are excluded from processing and remain []."""
        pi = self._make_patient(89206, [])
        self._run()
        pi.refresh_from_db()
        self.assertEqual(pi.stem_cell_transplant_history, [])

    def test_migration_is_idempotent(self):
        """Running the migration twice produces the same result as running it once."""
        pi = self._make_patient(89210, ['prior SCT', 'prior allogeneic SCT'])
        self._run()
        pi.refresh_from_db()
        after_first = list(pi.stem_cell_transplant_history)

        self._run()  # second run
        pi.refresh_from_db()
        self.assertEqual(pi.stem_cell_transplant_history, after_first)
        self.assertEqual(sorted(after_first), ['allogeneic SCT', 'autologous SCT'])

    def test_audit_and_migration_dicts_are_identical(self):
        """_OLD_TO_NEW_SCT must be identical in audit_sct_history and migration 0086.

        Both files duplicate the mapping dict. This test catches any future divergence
        so that the audit command always accurately predicts what the migration will do.
        """
        import importlib
        audit_mod = importlib.import_module(
            'omop_core.management.commands.audit_sct_history'
        )
        mig_mod = importlib.import_module(
            'omop_core.migrations'
            '.0086_seed_sct_eligibility_update_stem_cell_transplant'
        )
        self.assertEqual(
            audit_mod._OLD_TO_NEW_SCT,
            mig_mod._OLD_TO_NEW_SCT,
            "_OLD_TO_NEW_SCT in audit_sct_history.py and migration 0086 have diverged. "
            "Update both files to keep them in sync.",
        )


# =============================================================================
# phr-etl integration endpoint tests
# POST /api/persons/find_or_create/
# PATCH /api/persons/{person_id}/
# GET  /api/concepts/lookup/
# =============================================================================

class PersonFindOrCreateTest(_SmartBase):
    """POST /api/persons/find_or_create/"""

    URL = '/api/persons/find_or_create/'

    def _auth(self):
        return {'HTTP_AUTHORIZATION': f'Bearer {self.write_token.token}'}

    def test_creates_person_on_first_call(self):
        resp = self.client.post(
            self.URL,
            {'actor_iss': 'https://securetoken.google.com/proj', 'actor_sub': 'uid-abc'},
            content_type='application/json',
            **self._auth(),
        )
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)
        self.assertIn('person_id', resp.json())
        self.assertTrue(resp.json()['created'])

    def test_returns_same_person_id_on_repeat(self):
        payload = {'actor_iss': 'https://securetoken.google.com/proj', 'actor_sub': 'uid-xyz'}
        r1 = self.client.post(self.URL, payload, content_type='application/json', **self._auth())
        r2 = self.client.post(self.URL, payload, content_type='application/json', **self._auth())
        self.assertEqual(r1.status_code, status.HTTP_201_CREATED)
        self.assertEqual(r2.status_code, status.HTTP_200_OK)
        self.assertEqual(r1.json()['person_id'], r2.json()['person_id'])
        self.assertFalse(r2.json()['created'])

    def test_different_subs_get_different_persons(self):
        base = {'actor_iss': 'https://securetoken.google.com/proj'}
        r1 = self.client.post(self.URL, {**base, 'actor_sub': 'uid-1'}, content_type='application/json', **self._auth())
        r2 = self.client.post(self.URL, {**base, 'actor_sub': 'uid-2'}, content_type='application/json', **self._auth())
        self.assertNotEqual(r1.json()['person_id'], r2.json()['person_id'])

    def test_missing_actor_iss_returns_400(self):
        resp = self.client.post(
            self.URL, {'actor_sub': 'uid-abc'}, content_type='application/json', **self._auth(),
        )
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_missing_actor_sub_returns_400(self):
        resp = self.client.post(
            self.URL, {'actor_iss': 'https://securetoken.google.com/proj'}, content_type='application/json', **self._auth(),
        )
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_unauthenticated_returns_401(self):
        resp = self.client.post(
            self.URL,
            {'actor_iss': 'https://securetoken.google.com/proj', 'actor_sub': 'uid-noauth'},
            content_type='application/json',
        )
        self.assertIn(resp.status_code, [status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN])


class PersonDemographicPatchTest(_SmartBase):
    """PATCH /api/persons/{person_id}/"""

    def setUp(self):
        from omop_core.models import Person
        from omop_core.services.pk import next_pk
        self.person = Person.objects.create(
            person_id=next_pk(Person, 'person_id'),
            given_name=None,
            family_name=None,
            year_of_birth=None,
            gender_source_value=None,
            race_source_value=None,
            ethnicity_source_value=None,
        )
        # PersonViewSet.partial_update org-check requires PatientInfo; create one
        # scoped to the test org so the write token's org matches.
        PatientInfo.objects.create(person=self.person, organization=self.organization)

    def _url(self):
        return f'/api/persons/{self.person.person_id}/'

    def _auth(self):
        return {'HTTP_AUTHORIZATION': f'Bearer {self.write_token.token}'}

    def test_fills_null_fields(self):
        resp = self.client.patch(
            self._url(),
            {'given_name': 'Jane', 'family_name': 'Doe', 'year_of_birth': 1980},
            content_type='application/json',
            **self._auth(),
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.person.refresh_from_db()
        self.assertEqual(self.person.given_name, 'Jane')
        self.assertEqual(self.person.family_name, 'Doe')
        self.assertEqual(self.person.year_of_birth, 1980)

    def test_does_not_clobber_existing_value(self):
        self.person.given_name = 'Existing'
        self.person.save(update_fields=['given_name'])
        resp = self.client.patch(
            self._url(),
            {'given_name': 'Attempted Override'},
            content_type='application/json',
            **self._auth(),
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.person.refresh_from_db()
        self.assertEqual(self.person.given_name, 'Existing')
        self.assertNotIn('given_name', resp.json()['updated_fields'])

    def test_overwrites_placeholder_string(self):
        self.person.race_source_value = 'unknown'
        self.person.save(update_fields=['race_source_value'])
        self.client.patch(
            self._url(),
            {'race_source_value': 'White'},
            content_type='application/json',
            **self._auth(),
        )
        self.person.refresh_from_db()
        self.assertEqual(self.person.race_source_value, 'White')

    def test_overwrites_placeholder_year(self):
        self.person.year_of_birth = 1900
        self.person.save(update_fields=['year_of_birth'])
        self.client.patch(
            self._url(),
            {'year_of_birth': 1975},
            content_type='application/json',
            **self._auth(),
        )
        self.person.refresh_from_db()
        self.assertEqual(self.person.year_of_birth, 1975)

    def test_unknown_person_returns_404(self):
        resp = self.client.patch(
            '/api/persons/999999/',
            {'given_name': 'Ghost'},
            content_type='application/json',
            **self._auth(),
        )
        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)

    def test_updated_fields_list_in_response(self):
        resp = self.client.patch(
            self._url(),
            {'given_name': 'Alice', 'family_name': 'Smith'},
            content_type='application/json',
            **self._auth(),
        )
        self.assertIn('given_name', resp.json()['updated_fields'])
        self.assertIn('family_name', resp.json()['updated_fields'])


class ConceptLookupTest(_SmartBase):
    """GET /api/concepts/lookup/"""

    URL = '/api/concepts/lookup/'

    def setUp(self):
        from omop_core.models import Concept, Vocabulary, Domain, ConceptClass
        import datetime
        # Minimal vocab/domain/class stubs needed for Concept FK constraints
        vocab, _ = Vocabulary.objects.get_or_create(
            vocabulary_id='LOINC',
            defaults={'vocabulary_name': 'LOINC', 'vocabulary_reference': '', 'vocabulary_version': '',
                      'vocabulary_concept_id': 0},
        )
        domain, _ = Domain.objects.get_or_create(
            domain_id='Measurement',
            defaults={'domain_name': 'Measurement', 'domain_concept_id': 0},
        )
        cc, _ = ConceptClass.objects.get_or_create(
            concept_class_id='Lab Test',
            defaults={'concept_class_name': 'Lab Test', 'concept_class_concept_id': 0},
        )
        self.concept = Concept.objects.get_or_create(
            concept_id=3013682,
            defaults={
                'concept_name': 'Creatinine [Mass/volume] in Serum or Plasma',
                'domain_id': 'Measurement',
                'vocabulary_id': 'LOINC',
                'concept_class_id': 'Lab Test',
                'concept_code': '2160-0',
                'valid_start_date': datetime.date(1970, 1, 1),
                'valid_end_date': datetime.date(2099, 12, 31),
            },
        )[0]

    def _auth(self):
        return {'HTTP_AUTHORIZATION': f'Bearer {self.read_token.token}'}

    def test_returns_concept_id_for_known_code(self):
        resp = self.client.get(
            self.URL, {'lookup': 'LOINC:2160-0'}, **self._auth(),
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.json()['LOINC']['2160-0'], 3013682)

    def test_returns_null_for_unknown_code(self):
        resp = self.client.get(
            self.URL, {'lookup': 'LOINC:9999-X'}, **self._auth(),
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertIsNone(resp.json()['LOINC']['9999-X'])

    def test_multiple_lookup_pairs(self):
        resp = self.client.get(
            f'{self.URL}?lookup=LOINC:2160-0&lookup=LOINC:9999-X', **self._auth(),
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        data = resp.json()['LOINC']
        self.assertEqual(data['2160-0'], 3013682)
        self.assertIsNone(data['9999-X'])

    def test_missing_lookup_param_returns_400(self):
        resp = self.client.get(self.URL, **self._auth())
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_malformed_lookup_param_returns_400(self):
        resp = self.client.get(f'{self.URL}?lookup=LOINC-2160-0', **self._auth())
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_unauthenticated_returns_401(self):
        resp = self.client.get(f'{self.URL}?lookup=LOINC:2160-0')
        self.assertIn(resp.status_code, [status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN])


# ---------------------------------------------------------------------------
# IDOR: PatientInfoViewSet row-level access (issue #134)
# ---------------------------------------------------------------------------

class PatientInfoIDORTest(TestCase):
    """
    Verify that a patient user cannot read or modify another patient's record
    via retrieve, partial_update, or provenance when org scoping is absent
    (partner-auth / session-auth path).
    """

    @classmethod
    def setUpTestData(cls):
        from patient_portal.models import PatientUser

        # Patient A
        cls.person_a = Person.objects.create(person_id=88801, family_name='Alpha', given_name='Alice')
        cls.patient_a = PatientInfo.objects.create(person=cls.person_a)
        cls.identity_a = Identity.objects.create_user(email='alice@test.com', password='pw')
        PatientUser.objects.create(identity=cls.identity_a, person=cls.person_a)

        # Patient B — the victim
        cls.person_b = Person.objects.create(person_id=88802, family_name='Beta', given_name='Bob')
        cls.patient_b = PatientInfo.objects.create(person=cls.person_b)
        cls.identity_b = Identity.objects.create_user(email='bob@test.com', password='pw')
        PatientUser.objects.create(identity=cls.identity_b, person=cls.person_b)

        # Superuser
        cls.superuser = Identity.objects.create_superuser(email='su@test.com', password='pw')

    def _client_as(self, identity):
        c = APIClient()
        c.force_authenticate(user=identity)
        return c

    def test_patient_cannot_retrieve_other_patient(self):
        """GET /api/patient-info/{B}/ as patient A must return 404."""
        resp = self._client_as(self.identity_a).get(
            f'/api/patient-info/{self.person_b.person_id}/'
        )
        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)

    def test_patient_can_retrieve_own_record(self):
        """GET /api/patient-info/{A}/ as patient A must succeed."""
        resp = self._client_as(self.identity_a).get(
            f'/api/patient-info/{self.person_a.person_id}/'
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

    def test_patient_cannot_patch_other_patient(self):
        """PATCH /api/patient-info/{B}/ as patient A must return 404."""
        resp = self._client_as(self.identity_a).patch(
            f'/api/patient-info/{self.person_b.person_id}/',
            {'ecog_performance_status': 1},
            format='json',
        )
        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)

    def test_patient_cannot_access_other_provenance(self):
        """GET /api/patient-info/{B}/provenance/ as patient A must return 404."""
        resp = self._client_as(self.identity_a).get(
            f'/api/patient-info/{self.person_b.person_id}/provenance/'
        )
        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)

    def test_superuser_can_retrieve_any_patient(self):
        """Superusers retain unrestricted read access."""
        resp = self._client_as(self.superuser).get(
            f'/api/patient-info/{self.person_b.person_id}/'
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)


# ---------------------------------------------------------------------------
# OMOP ViewSet row-level access (issue #135)
# ---------------------------------------------------------------------------

class OmopViewSetAccessTest(TestCase):
    """
    Verify that _OmopFilterMixin enforces per-patient access for session /
    partner-auth users (org is None path) on the OMOP clinical ViewSets.
    """

    @classmethod
    def setUpTestData(cls):
        from patient_portal.models import PatientUser

        # Patient A — the attacker
        cls.person_a = Person.objects.create(person_id=88901, family_name='Attacker', given_name='Alice')
        PatientInfo.objects.create(person=cls.person_a)
        cls.identity_a = Identity.objects.create_user(email='attacker@test.com', password='pw')
        PatientUser.objects.create(identity=cls.identity_a, person=cls.person_a)

        # Patient B — the victim
        cls.person_b = Person.objects.create(person_id=88902, family_name='Victim', given_name='Bob')
        PatientInfo.objects.create(person=cls.person_b)
        cls.identity_b = Identity.objects.create_user(email='victim@test.com', password='pw')
        PatientUser.objects.create(identity=cls.identity_b, person=cls.person_b)

        # A measurement belonging to patient B
        cls.measurement = Measurement.objects.create(
            measurement_id=998877,
            person=cls.person_b,
            measurement_concept_id=0,
            measurement_type_concept_id=0,
            measurement_date=date(2024, 1, 1),
        )

        # A condition belonging to patient B
        cls.condition = ConditionOccurrence.objects.create(
            condition_occurrence_id=998877,
            person=cls.person_b,
            condition_concept_id=0,
            condition_type_concept_id=0,
            condition_start_date=date(2024, 1, 1),
        )

        cls.superuser = Identity.objects.create_superuser(email='su2@test.com', password='pw')

    def _client_as(self, identity):
        c = APIClient()
        c.force_authenticate(user=identity)
        return c

    # --- List filtered by person_id ---

    def test_patient_cannot_list_other_measurements(self):
        """GET /api/measurements/?person_id=B as patient A returns empty list."""
        resp = self._client_as(self.identity_a).get(
            f'/api/measurements/?person_id={self.person_b.person_id}'
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(len((resp.data if isinstance(resp.data, list) else resp.data.get('results', resp.data))), 0)

    def test_patient_can_list_own_measurements(self):
        """GET /api/measurements/?person_id=A as patient A returns their records."""
        Measurement.objects.create(
            measurement_id=998878,
            person=self.person_a,
            measurement_concept_id=0,
            measurement_type_concept_id=0,
            measurement_date=date(2024, 1, 1),
        )
        resp = self._client_as(self.identity_a).get(
            f'/api/measurements/?person_id={self.person_a.person_id}'
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        results = (resp.data if isinstance(resp.data, list) else resp.data.get('results', resp.data))
        self.assertGreater(len(results), 0)

    def test_patient_cannot_list_other_conditions(self):
        """GET /api/conditions/?person_id=B as patient A returns empty list."""
        resp = self._client_as(self.identity_a).get(
            f'/api/conditions/?person_id={self.person_b.person_id}'
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(len((resp.data if isinstance(resp.data, list) else resp.data.get('results', resp.data))), 0)

    def test_list_without_person_id_returns_own_records_only(self):
        """GET /api/measurements/ (no person_id) as patient A returns only their records."""
        resp = self._client_as(self.identity_a).get('/api/measurements/')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        results = (resp.data if isinstance(resp.data, list) else resp.data.get('results', resp.data))
        person_ids = {r['person'] for r in results}
        self.assertNotIn(self.person_b.person_id, person_ids)

    def test_superuser_can_list_any_patient_measurements(self):
        """Superusers retain unrestricted access."""
        resp = self._client_as(self.superuser).get(
            f'/api/measurements/?person_id={self.person_b.person_id}'
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        results = (resp.data if isinstance(resp.data, list) else resp.data.get('results', resp.data))
        self.assertGreater(len(results), 0)


# ---------------------------------------------------------------------------
# Mass-assignable organization field (issue #139)
# ---------------------------------------------------------------------------

class PatientInfoOrganizationReadOnlyTest(TestCase):
    """
    Verify that a client cannot PATCH organization or person onto a
    PatientInfo record — these fields must be silently ignored (read-only).
    """

    @classmethod
    def setUpTestData(cls):
        from omop_core.models import Organization
        from patient_portal.models import PatientUser

        cls.org_a = Organization.objects.create(name='Org A', slug='org-a-139')
        cls.org_b = Organization.objects.create(name='Org B', slug='org-b-139')

        cls.person = Person.objects.create(person_id=89001, family_name='Test', given_name='User')
        cls.patient = PatientInfo.objects.create(person=cls.person, organization=cls.org_a)
        cls.identity = Identity.objects.create_user(email='orgtest@test.com', password='pw')
        PatientUser.objects.create(identity=cls.identity, person=cls.person)

        cls.other_person = Person.objects.create(person_id=89002, family_name='Other', given_name='Person')
        PatientInfo.objects.create(person=cls.other_person, organization=cls.org_b)

    def _client(self):
        c = APIClient()
        c.force_authenticate(user=self.identity)
        return c

    def test_patch_cannot_change_organization(self):
        """PATCH {organization: org_b} must not change the record's org."""
        resp = self._client().patch(
            f'/api/patient-info/{self.person.person_id}/',
            {'organization': self.org_b.id},
            format='json',
        )
        self.assertIn(resp.status_code, [status.HTTP_200_OK, status.HTTP_400_BAD_REQUEST])
        self.patient.refresh_from_db()
        self.assertEqual(self.patient.organization_id, self.org_a.id)

    def test_organization_field_is_read_only_in_response(self):
        """organization appears in the GET response but cannot be changed via PATCH."""
        resp = self._client().get(f'/api/patient-info/{self.person.person_id}/')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        # organization is present in the response (readable)
        pi_data = resp.data.get('patient_info', resp.data)
        self.assertIn('organization', pi_data)


# ---------------------------------------------------------------------------
# Per-patient transaction boundary in upload_fhir (issue #149)
# ---------------------------------------------------------------------------

class FhirUploadTransactionTest(FhirUploadBase):
    """
    Verify that a mid-patient failure rolls back all DB writes for that
    patient so no orphaned Person / OMOP rows persist.
    """

    def test_failed_patient_leaves_no_orphaned_rows(self):
        """
        If refresh_patient_info raises mid-patient, the Person and all OMOP
        rows written before the error must be rolled back.
        """
        from unittest.mock import patch
        from omop_core.services.patient_info_service import refresh_patient_info

        person_id_before = (
            Person.objects.order_by('-person_id').values_list('person_id', flat=True).first() or 0
        )

        bundle = _make_fhir_bundle()
        bundle_bytes = json.dumps(bundle).encode('utf-8')
        fhir_file = io.BytesIO(bundle_bytes)
        fhir_file.name = 'bundle.json'

        with patch(
            'patient_portal.api.views.refresh_patient_info',
            side_effect=RuntimeError('simulated mid-patient failure'),
        ):
            resp = self.client.post(
                '/api/patient-info/upload_fhir/',
                {'file': fhir_file},
                format='multipart',
            )

        self.assertEqual(resp.status_code, 200)
        # The error is recorded, not a 500
        self.assertGreater(len(resp.data.get('errors', [])), 0)

        # No new Person row should have been committed
        new_persons = Person.objects.filter(person_id__gt=person_id_before).count()
        self.assertEqual(new_persons, 0, "Partial patient rows were not rolled back")

    def test_successful_patient_commits_rows(self):
        """Successful uploads still persist rows after the transaction fix."""
        resp = self._upload_bundle()
        self.assertEqual(resp.status_code, 200)
        self.assertGreater(resp.data.get('created_count', 0), 0)
        self.assertIsNotNone(
            Person.objects.filter(family_name='Smith', given_name='Jane').first()
        )


# ---------------------------------------------------------------------------
# IDOR: EpisodeEventViewSet cross-org isolation (issue #136)
# ---------------------------------------------------------------------------

class EpisodeEventIDORTest(TestCase):
    """
    Verify that an org-A service token cannot read EpisodeEvent rows that
    belong to an org-B patient, even when episode_id is known.
    """

    @classmethod
    def setUpTestData(cls):
        from oauth2_provider.models import Application, AccessToken
        from omop_core.models import Organization, ApplicationOrganization
        from django.utils import timezone
        from datetime import timedelta
        _make_vocab_fixtures()

        cls.org_a = Organization.objects.create(name='EE Org A', slug='ee-org-a')
        cls.org_b = Organization.objects.create(name='EE Org B', slug='ee-org-b')

        cls.svc_user = Identity.objects.create_user(email='ee-svc@test.com', password='x')
        cls.app = Application.objects.create(
            name='EE App',
            user=cls.svc_user,
            client_type=Application.CLIENT_CONFIDENTIAL,
            authorization_grant_type=Application.GRANT_CLIENT_CREDENTIALS,
        )
        ApplicationOrganization.objects.create(application=cls.app, organization=cls.org_a)
        cls.token = AccessToken.objects.create(
            user=cls.svc_user,
            application=cls.app,
            token='ee-idor-test-token',
            expires=timezone.now() + timedelta(hours=1),
            scope='patient/*.read',
        )

        # Org-A patient with an episode + event
        cls.person_a = Person.objects.create(person_id=19101)
        PatientInfo.objects.create(person=cls.person_a, organization=cls.org_a)
        cls.ep_a = Episode.objects.create(
            episode_id=19101,
            person=cls.person_a,
            episode_concept=Concept.objects.get(concept_id=32531),   # treatment regimen
            episode_object_concept=Concept.objects.get(concept_id=32817),
            episode_type_concept=Concept.objects.get(concept_id=32817),
            episode_start_date=date(2024, 1, 1),
            episode_number=1,
            episode_source_value='RCHOP',
        )
        cls.drug_a = DrugExposure.objects.create(
            drug_exposure_id=19101,
            person=cls.person_a,
            drug_concept=Concept.objects.get(concept_id=19136160),
            drug_exposure_start_date=date(2024, 1, 1),
            drug_type_concept=Concept.objects.get(concept_id=32817),
        )
        cls.ee_a = EpisodeEvent.objects.create(
            episode_id=cls.ep_a.episode_id,
            event_id=cls.drug_a.drug_exposure_id,
            episode_event_field_concept=Concept.objects.get(concept_id=1147094),
        )

        # Org-B patient with an episode + event (must NOT be visible via org-A token)
        cls.person_b = Person.objects.create(person_id=19102)
        PatientInfo.objects.create(person=cls.person_b, organization=cls.org_b)
        cls.ep_b = Episode.objects.create(
            episode_id=19102,
            person=cls.person_b,
            episode_concept=Concept.objects.get(concept_id=32531),   # treatment regimen
            episode_object_concept=Concept.objects.get(concept_id=32817),
            episode_type_concept=Concept.objects.get(concept_id=32817),
            episode_start_date=date(2024, 2, 1),
            episode_number=1,
            episode_source_value='VRd',
        )
        cls.drug_b = DrugExposure.objects.create(
            drug_exposure_id=19102,
            person=cls.person_b,
            drug_concept=Concept.objects.get(concept_id=19136160),
            drug_exposure_start_date=date(2024, 2, 1),
            drug_type_concept=Concept.objects.get(concept_id=32817),
        )
        cls.ee_b = EpisodeEvent.objects.create(
            episode_id=cls.ep_b.episode_id,
            event_id=cls.drug_b.drug_exposure_id,
            episode_event_field_concept=Concept.objects.get(concept_id=1147094),
        )

    def _client(self):
        c = APIClient()
        c.credentials(HTTP_AUTHORIZATION=f'Bearer {self.token.token}')
        return c

    def test_list_scoped_to_own_org_episode(self):
        """List with org-A episode_id returns events; org-B episode_id returns empty."""
        c = self._client()
        resp = c.get(f'/api/episode-events/?episode_id={self.ep_a.episode_id}')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        ids = [e['event_id'] for e in resp.data]
        self.assertIn(self.ee_a.event_id, ids)

    def test_list_excludes_other_org_events(self):
        """List with org-B episode_id (known via IDOR) must return empty for org-A token."""
        c = self._client()
        resp = c.get(f'/api/episode-events/?episode_id={self.ep_b.episode_id}')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(len(resp.data), 0, 'Org-B EpisodeEvent leaked to org-A token')

    def test_retrieve_other_org_event_returns_404(self):
        """Direct retrieve of org-B EpisodeEvent PK via org-A token must return 404."""
        c = self._client()
        # ee_b PK is (episode_id, event_id) — DRF ModelViewSet uses the PK for retrieve
        resp = c.get(f'/api/episode-events/{self.ee_b.pk}/')
        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND,
                         'Org-B EpisodeEvent accessible via direct retrieve with org-A token')

    def test_destroy_other_org_event_denied(self):
        """DELETE of org-B EpisodeEvent PK via org-A read token must be denied (403 scope or 404 isolation)."""
        c = self._client()
        resp = c.delete(f'/api/episode-events/{self.ee_b.pk}/')
        # A read-only token gets 403 (scope check fires before the org filter).
        # A write-scope org-A token would get 404 (org filter). Either is safe.
        self.assertIn(resp.status_code, [status.HTTP_403_FORBIDDEN, status.HTTP_404_NOT_FOUND],
                      'Org-B EpisodeEvent was deleted by org-A token')


# ---------------------------------------------------------------------------
# TherapyConceptIdTest — HemOnc concept_id fields on PatientInfo
# ---------------------------------------------------------------------------

class TherapyConceptIdTest(TestCase):
    """
    Verify that refresh_patient_info populates first_line_therapy_id /
    second_line_therapy_id / later_therapy_ids via the HemOnc regimen lookup,
    and that the PatientInfoSerializer display fields fall back correctly.
    """

    @classmethod
    def setUpTestData(cls):
        _make_vocab_fixtures()

        today = date.today()
        far_future = date(2099, 12, 31)
        vocab = Vocabulary.objects.get(vocabulary_id='TEST')
        domain_drug = Domain.objects.get(domain_id='Drug')
        cc = ConceptClass.objects.get(concept_class_id='Clinical Finding')

        def _drug_concept(cid, name):
            obj, _ = Concept.objects.get_or_create(
                concept_id=cid,
                defaults={
                    'concept_name': name,
                    'domain': domain_drug,
                    'vocabulary': vocab,
                    'concept_class': cc,
                    'concept_code': str(cid),
                    'valid_start_date': today,
                    'valid_end_date': far_future,
                },
            )
            return obj

        # Drug concepts for KRd
        cls.carfilzomib_c  = _drug_concept(1112807, 'carfilzomib')
        cls.lenalidomide_c = _drug_concept(1110942, 'lenalidomide')
        cls.dexamethasone_c = _drug_concept(1518254, 'dexamethasone')

        # Drug concepts for VRd (bortezomib already exists or create it)
        cls.bortezomib_c = _drug_concept(1110835, 'bortezomib')

        # HemOnc concept for KRd (concept_id 35806284)
        hemonc_vocab, _ = Vocabulary.objects.get_or_create(
            vocabulary_id='HemOnc',
            defaults={'vocabulary_name': 'HemOnc', 'vocabulary_concept_id': 0},
        )
        hemonc_cc, _ = ConceptClass.objects.get_or_create(
            concept_class_id='Regimen',
            defaults={'concept_class_name': 'Regimen', 'concept_class_concept_id': 0},
        )
        domain_obs, _ = Domain.objects.get_or_create(
            domain_id='Observation',
            defaults={'domain_name': 'Observation', 'domain_concept_id': 27},
        )
        cls.krd_concept, _ = Concept.objects.get_or_create(
            concept_id=35806284,
            defaults={
                'concept_name': 'KRd',
                'domain': domain_obs,
                'vocabulary': hemonc_vocab,
                'concept_class': hemonc_cc,
                'concept_code': 'KRd',
                'valid_start_date': today,
                'valid_end_date': far_future,
            },
        )

        ep_concept = Concept.objects.get(concept_id=32531)
        ehr_concept = Concept.objects.get(concept_id=32817)
        field_concept = Concept.objects.get(concept_id=1147094)
        type_concept = Concept.objects.get(concept_id=32817)

        # ── Patient 1: KRd first-line ───────────────────────────────────────
        cls.person_krd = Person.objects.create(person_id=92001)
        cls.pi_krd = PatientInfo.objects.create(person=cls.person_krd)

        last_ep = Episode.objects.order_by('-episode_id').first()
        ep_id = (last_ep.episode_id + 1) if last_ep else 1
        cls.ep_krd = Episode.objects.create(
            episode_id=ep_id,
            person=cls.person_krd,
            episode_concept=ep_concept,
            episode_object_concept=ehr_concept,
            episode_type_concept=ehr_concept,
            episode_number=1,
            episode_start_date=date(2023, 1, 1),
            episode_source_value='KRd (induction)',
        )

        def _drug_exp(person, concept, exp_id, start=date(2023, 1, 1)):
            return DrugExposure.objects.create(
                drug_exposure_id=exp_id,
                person=person,
                drug_concept=concept,
                drug_exposure_start_date=start,
                drug_type_concept=type_concept,
            )

        cls.de_carf = _drug_exp(cls.person_krd, cls.carfilzomib_c,  920011)
        cls.de_lena = _drug_exp(cls.person_krd, cls.lenalidomide_c, 920012)
        cls.de_dexa = _drug_exp(cls.person_krd, cls.dexamethasone_c, 920013)

        for de in [cls.de_carf, cls.de_lena, cls.de_dexa]:
            EpisodeEvent.objects.create(
                episode_id=cls.ep_krd.episode_id,
                event_id=de.drug_exposure_id,
                episode_event_field_concept=field_concept,
            )

        # ── Patient 2: VRd first-line (no HemOnc concept_id) ───────────────
        cls.person_vrd = Person.objects.create(person_id=92002)
        cls.pi_vrd = PatientInfo.objects.create(person=cls.person_vrd)

        last_ep = Episode.objects.order_by('-episode_id').first()
        ep_id2 = last_ep.episode_id + 1
        cls.ep_vrd = Episode.objects.create(
            episode_id=ep_id2,
            person=cls.person_vrd,
            episode_concept=ep_concept,
            episode_object_concept=ehr_concept,
            episode_type_concept=ehr_concept,
            episode_number=1,
            episode_start_date=date(2023, 2, 1),
            episode_source_value='VRd (induction)',
        )

        cls.de_bort = _drug_exp(cls.person_vrd, cls.bortezomib_c,  920021, date(2023, 2, 1))
        cls.de_lena2 = _drug_exp(cls.person_vrd, cls.lenalidomide_c, 920022, date(2023, 2, 1))
        cls.de_dexa2 = _drug_exp(cls.person_vrd, cls.dexamethasone_c, 920023, date(2023, 2, 1))

        for de in [cls.de_bort, cls.de_lena2, cls.de_dexa2]:
            EpisodeEvent.objects.create(
                episode_id=cls.ep_vrd.episode_id,
                event_id=de.drug_exposure_id,
                episode_event_field_concept=field_concept,
            )

    def _refresh(self, person):
        from omop_core.services.patient_info_service import refresh_patient_info
        return refresh_patient_info(person)

    def test_krd_first_line_therapy_id_is_populated(self):
        """refresh_patient_info sets first_line_therapy_id=35806284 for KRd."""
        pi = self._refresh(self.person_krd)
        self.assertEqual(pi.first_line_therapy_id, 35806284)

    def test_krd_first_line_therapy_text_uses_canonical_name(self):
        """When HemOnc concept_id resolved, therapy text is set to canonical name."""
        pi = self._refresh(self.person_krd)
        self.assertEqual(pi.first_line_therapy, 'KRd')

    def test_vrd_first_line_therapy_id_is_none(self):
        """VRd has no HemOnc concept_id — field stays None."""
        pi = self._refresh(self.person_vrd)
        self.assertIsNone(pi.first_line_therapy_id)

    def test_vrd_first_line_therapy_text_is_populated(self):
        """VRd therapy text is still populated even without a concept_id."""
        pi = self._refresh(self.person_vrd)
        self.assertIsNotNone(pi.first_line_therapy)
        self.assertNotEqual(pi.first_line_therapy, '')

    def test_serializer_display_returns_hemonc_name_when_concept_id_set(self):
        """first_line_therapy_display returns HemOnc concept_name when concept_id present."""
        pi = self._refresh(self.person_krd)
        from patient_portal.api.serializers import PatientInfoSerializer
        data = PatientInfoSerializer(pi).data
        self.assertEqual(data['first_line_therapy_display'], 'KRd')

    def test_serializer_display_falls_back_to_text_when_no_concept_id(self):
        """first_line_therapy_display falls back to first_line_therapy text when id is None."""
        pi = self._refresh(self.person_vrd)
        pi.first_line_therapy = 'VRd'
        pi.first_line_therapy_id = None
        pi.save(update_fields=['first_line_therapy', 'first_line_therapy_id'])
        from patient_portal.api.serializers import PatientInfoSerializer
        data = PatientInfoSerializer(pi).data
        self.assertEqual(data['first_line_therapy_display'], 'VRd')

    def test_later_therapy_ids_is_list_or_none(self):
        """later_therapy_ids is either None or a list."""
        pi = self._refresh(self.person_krd)
        self.assertIn(pi.later_therapy_ids, [None, []])


class OrgDiseaseStatsTest(TestCase):
    def setUp(self):
        self.client = APIClient()
        from omop_core.models import Organization, PatientGroup, GroupAccess
        self.org_a = Organization.objects.create(name='Org A', slug='org-a')
        self.org_b = Organization.objects.create(name='Org B', slug='org-b')
        self.group_a = PatientGroup.objects.create(
            organization=self.org_a, name='Group A', slug='group-a'
        )

        # Create patients in org_a
        for i, slug in enumerate(['mm', 'mm', 'breast-cancer'], start=1):
            p = Person.objects.create(person_id=9000 + i)
            PatientInfo.objects.create(person=p, organization=self.org_a, disease_slug=slug)

        # Create patient in org_b
        p4 = Person.objects.create(person_id=9004)
        PatientInfo.objects.create(person=p4, organization=self.org_b, disease_slug='cll')

        self.staff = Identity.objects.create_user(email='staff@t.com', password='x', is_staff=True)
        self.org_admin = Identity.objects.create_user(email='admin@t.com', password='x')
        self.doctor = Identity.objects.create_user(email='doc@t.com', password='x')
        self.nobody = Identity.objects.create_user(email='none@t.com', password='x')

        GroupAccess.objects.create(identity=self.org_admin, org=self.org_a, role='org_admin')
        GroupAccess.objects.create(identity=self.doctor, group=self.group_a, role='doctor')

    def _get(self, user):
        self.client.force_authenticate(user=user)
        return self.client.get('/api/stats/org-disease/')

    def test_staff_sees_all_orgs(self):
        resp = self._get(self.staff)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        slugs = {o['org_slug'] for o in resp.data}
        self.assertIn('org-a', slugs)
        self.assertIn('org-b', slugs)

    def test_staff_disease_counts_correct(self):
        resp = self._get(self.staff)
        org_a_data = next(o for o in resp.data if o['org_slug'] == 'org-a')
        self.assertEqual(org_a_data['total'], 3)
        counts = {d['disease_slug']: d['count'] for d in org_a_data['disease_counts']}
        self.assertEqual(counts['mm'], 2)
        self.assertEqual(counts['breast-cancer'], 1)

    def test_org_admin_sees_only_their_org(self):
        resp = self._get(self.org_admin)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        slugs = {o['org_slug'] for o in resp.data}
        self.assertIn('org-a', slugs)
        self.assertNotIn('org-b', slugs)

    def test_doctor_sees_their_group_org(self):
        resp = self._get(self.doctor)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        slugs = {o['org_slug'] for o in resp.data}
        self.assertIn('org-a', slugs)

    def test_direct_org_doctor_sees_aggregated_org_data(self):
        doctor = Identity.objects.create_user(email='directdoc@t.com', password='x')
        GroupAccess.objects.create(identity=doctor, org=self.org_b, role='doctor')
        resp = self._get(doctor)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        slugs = {o['org_slug'] for o in resp.data}
        self.assertIn('org-b', slugs)
        self.assertNotIn('org-a', slugs)

    def test_direct_org_navigator_sees_aggregated_org_data(self):
        navigator = Identity.objects.create_user(email='navigator@t.com', password='x')
        GroupAccess.objects.create(identity=navigator, org=self.org_b, role='navigator')
        resp = self._get(navigator)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        slugs = {o['org_slug'] for o in resp.data}
        self.assertIn('org-b', slugs)
        self.assertNotIn('org-a', slugs)

    def test_no_grants_returns_empty_list(self):
        resp = self._get(self.nobody)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data, [])

    def test_unauthenticated_returns_401(self):
        self.client.logout()
        resp = self.client.get('/api/stats/org-disease/')
        self.assertEqual(resp.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_response_shape(self):
        resp = self._get(self.org_admin)
        org = resp.data[0]
        self.assertIn('org_slug', org)
        self.assertIn('org_name', org)
        self.assertIn('total', org)
        self.assertIn('owned_count', org)
        self.assertIn('accessible_count', org)
        self.assertIn('disease_counts', org)
        if org['disease_counts']:
            dc = org['disease_counts'][0]
            self.assertIn('disease_slug', dc)
            self.assertIn('label', dc)
            self.assertIn('count', dc)

    def test_owned_and_accessible_counts_no_trusts(self):
        resp = self._get(self.org_admin)
        org_a = next(o for o in resp.data if o['org_slug'] == 'org-a')
        self.assertEqual(org_a['owned_count'], 3)
        self.assertEqual(org_a['accessible_count'], 3)

    def test_org_trust_inflates_accessible_count(self):
        from omop_core.models import OrgTrust
        # org_b grants access to org_a's users (trusted_org=org_a)
        OrgTrust.objects.create(granting_org=self.org_b, trusted_org=self.org_a)
        resp = self._get(self.org_admin)
        org_a = next(o for o in resp.data if o['org_slug'] == 'org-a')
        self.assertEqual(org_a['owned_count'], 3)
        self.assertEqual(org_a['accessible_count'], 4)  # 3 owned + 1 from org_b

    def test_domain_trust_inflates_accessible_count(self):
        from omop_core.models import OrgTrust
        # org_b grants access to users with @t.com — org_admin has email admin@t.com
        OrgTrust.objects.create(granting_org=self.org_b, trusted_domain='t.com')
        resp = self._get(self.org_admin)
        org_a = next(o for o in resp.data if o['org_slug'] == 'org-a')
        self.assertEqual(org_a['owned_count'], 3)
        self.assertEqual(org_a['accessible_count'], 4)  # 3 owned + 1 from org_b via domain trust

    def test_self_trust_does_not_double_count(self):
        from omop_core.models import OrgTrust
        from django.db import IntegrityError
        # DB constraint should prevent self-trust; confirm it raises
        with self.assertRaises(IntegrityError):
            OrgTrust.objects.create(granting_org=self.org_a, trusted_org=self.org_a)

    def test_total_field_equals_owned_count(self):
        resp = self._get(self.org_admin)
        org_a = next(o for o in resp.data if o['org_slug'] == 'org-a')
        self.assertEqual(org_a['total'], org_a['owned_count'])


class OrgAdminPatientListScopingTest(TestCase):
    """Verify that org_admin GroupAccess grants scope the patient list correctly."""

    def setUp(self):
        from omop_core.models import Organization, PatientGroup, GroupAccess
        self.client = APIClient()

        self.org_a = Organization.objects.create(name='Org A', slug='org-a-scope')
        self.org_b = Organization.objects.create(name='Org B', slug='org-b-scope')
        self.group_a = PatientGroup.objects.create(
            organization=self.org_a, name='Group A', slug='group-a-scope'
        )

        # Two patients in org_a, one in org_b, one with no org
        p1 = Person.objects.create(person_id=8001)
        p2 = Person.objects.create(person_id=8002)
        p3 = Person.objects.create(person_id=8003)
        p4 = Person.objects.create(person_id=8004)
        self.pi_a1 = PatientInfo.objects.create(person=p1, organization=self.org_a)
        self.pi_a2 = PatientInfo.objects.create(person=p2, organization=self.org_a)
        self.pi_b = PatientInfo.objects.create(person=p3, organization=self.org_b)
        self.pi_none = PatientInfo.objects.create(person=p4)

        self.org_admin = Identity.objects.create_user(email='orgadmin@t.com', password='x')
        self.no_grant = Identity.objects.create_user(email='nogrant@t.com', password='x')
        self.staff = Identity.objects.create_user(email='staff2@t.com', password='x', is_staff=True)

        from django.utils import timezone
        GroupAccess.objects.create(
            identity=self.org_admin,
            org=self.org_a,
            role='org_admin',
        )
        PatientInfo.objects.filter(pk=self.pi_a1.pk).update(
            disease='Breast Cancer',
            stage='Breast Cancer Stage IIA',
            updated_at=timezone.now(),
        )
        PatientInfo.objects.filter(pk=self.pi_a2.pk).update(
            disease='Multiple Myeloma',
            stage='III',
            updated_at=timezone.now() - timedelta(days=45),
        )
        PatientInfo.objects.filter(pk=self.pi_b.pk).update(
            disease='Breast Cancer',
            stage='Stage II',
            updated_at=timezone.now(),
        )
        PatientInfo.objects.filter(pk=self.pi_none.pk).update(
            disease='Breast Cancer',
            stage='Stage IV',
            updated_at=timezone.now(),
        )

    def _get(self, user):
        self.client.force_authenticate(user=user)
        return self.client.get('/api/patient-info/')

    def test_org_admin_sees_only_their_org_patients(self):
        resp = self._get(self.org_admin)
        self.assertEqual(resp.status_code, 200)
        ids = {p['id'] for p in resp.data}
        self.assertIn(self.pi_a1.id, ids)
        self.assertIn(self.pi_a2.id, ids)
        self.assertNotIn(self.pi_b.id, ids)
        self.assertNotIn(self.pi_none.id, ids)

    def test_no_grant_user_sees_nothing(self):
        resp = self._get(self.no_grant)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(resp.data), 0)

    def test_staff_sees_all_patients(self):
        resp = self._get(self.staff)
        self.assertEqual(resp.status_code, 200)
        ids = {p['id'] for p in resp.data}
        self.assertIn(self.pi_a1.id, ids)
        self.assertIn(self.pi_b.id, ids)
        self.assertIn(self.pi_none.id, ids)

    def test_unpaginated_patient_list_still_returns_plain_list(self):
        resp = self._get(self.org_admin)
        self.assertEqual(resp.status_code, 200)
        self.assertIsInstance(resp.data, list)

    def test_paginated_patient_list_returns_filtered_count(self):
        self.client.force_authenticate(user=self.org_admin)
        resp = self.client.get(
            '/api/patient-info/',
            {'page': 1, 'page_size': 10, 'disease': 'Breast Cancer', 'stage': 'II'},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data['count'], 1)
        self.assertEqual([p['id'] for p in resp.data['results']], [self.pi_a1.id])
        self.assertIn('filter_options', resp.data)

    def test_paginated_patient_list_stage_filter_does_not_match_other_roman_stages(self):
        self.client.force_authenticate(user=self.org_admin)
        resp = self.client.get(
            '/api/patient-info/',
            {'page': 1, 'page_size': 10, 'disease': 'Breast Cancer', 'stage': 'I'},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data['count'], 0)

    def test_paginated_patient_list_filters_by_date(self):
        self.client.force_authenticate(user=self.org_admin)
        resp = self.client.get(
            '/api/patient-info/',
            {'page': 1, 'page_size': 10, 'date': '30d'},
        )
        self.assertEqual(resp.status_code, 200)
        ids = {p['id'] for p in resp.data['results']}
        self.assertIn(self.pi_a1.id, ids)
        self.assertNotIn(self.pi_a2.id, ids)

    def test_filter_options_absent_on_page_2(self):
        self.client.force_authenticate(user=self.org_admin)
        resp = self.client.get('/api/patient-info/', {'page': 2, 'page_size': 1})
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn('filter_options', resp.data)

    def test_filter_options_present_on_page_1(self):
        self.client.force_authenticate(user=self.org_admin)
        resp = self.client.get('/api/patient-info/', {'page': 1, 'page_size': 1})
        self.assertEqual(resp.status_code, 200)
        self.assertIn('filter_options', resp.data)


# ---------------------------------------------------------------------------
# bulk_delete_filtered Tests
# ---------------------------------------------------------------------------

class BulkDeleteFilteredTest(TestCase):
    """Tests for DELETE /api/patient-info/bulk_delete_filtered/

    PatientInfo has a unique constraint on person_id (one row per person).
    Org scoping is enforced at the PatientInfo.organization level.
    """

    def setUp(self):
        from oauth2_provider.models import Application, AccessToken
        from omop_core.models import Organization, ApplicationOrganization
        from django.utils import timezone as tz
        import datetime

        self.client = APIClient()

        self.org_a = Organization.objects.create(name='BDF Org A', slug='bdf-org-a')
        self.org_b = Organization.objects.create(name='BDF Org B', slug='bdf-org-b')

        # Persons (IDs chosen to avoid conflicts with other test classes)
        self.p1 = Person.objects.create(person_id=9001, gender_source_value='female',
                                        race_source_value='unknown', ethnicity_source_value='unknown')
        self.p2 = Person.objects.create(person_id=9002, gender_source_value='male',
                                        race_source_value='unknown', ethnicity_source_value='unknown')
        self.p3 = Person.objects.create(person_id=9003, gender_source_value='female',
                                        race_source_value='unknown', ethnicity_source_value='unknown')

        # p1, p2 in org_a; p3 in org_b
        self.pi_a1 = PatientInfo.objects.create(person=self.p1, organization=self.org_a, disease='Breast Cancer')
        self.pi_a2 = PatientInfo.objects.create(person=self.p2, organization=self.org_a, disease='Multiple Myeloma')
        self.pi_b  = PatientInfo.objects.create(person=self.p3, organization=self.org_b, disease='Breast Cancer')

        # Staff user — DELETE allowed via ScopedTokenPermission (is_staff=True)
        self.staff = Identity.objects.create_user(email='bdf_staff@t.com', password='x', is_staff=True)

        # OAuth2 write token for org_a
        self.user_a = Identity.objects.create_user(email='bdf_svc_a@t.com', password='x')
        self.app_a = Application.objects.create(
            name='BDF Org A App',
            client_id='bdf-org-a-client',
            client_type=Application.CLIENT_CONFIDENTIAL,
            authorization_grant_type=Application.GRANT_CLIENT_CREDENTIALS,
            user=self.user_a,
        )
        ApplicationOrganization.objects.create(application=self.app_a, organization=self.org_a)
        self.write_token_a = AccessToken.objects.create(
            user=self.user_a,
            application=self.app_a,
            token='bdf-org-a-write-token',
            expires=tz.now() + datetime.timedelta(hours=1),
            scope='patient/*.write',
        )

    def test_unauthenticated_request_rejected(self):
        """DELETE without credentials must be rejected."""
        resp = APIClient().delete('/api/patient-info/bulk_delete_filtered/')
        self.assertIn(resp.status_code, [401, 403])

    def test_org_a_token_cannot_delete_org_b_patients(self):
        """Org A write token must not delete Org B's PatientInfo via bulk_delete_filtered."""
        c = APIClient()
        c.credentials(HTTP_AUTHORIZATION=f'Bearer {self.write_token_a.token}')
        resp = c.delete('/api/patient-info/bulk_delete_filtered/')
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.data['success'])
        # pi_b belongs to org_b — must still exist
        self.assertTrue(PatientInfo.objects.filter(pk=self.pi_b.pk).exists())
        # p3 (org_b patient) must still exist
        from omop_core.models import Person as P
        self.assertTrue(P.objects.filter(person_id=self.p3.person_id).exists())

    def test_disease_filter_scopes_deletion(self):
        """Only PatientInfo matching the disease filter should be deleted."""
        self.client.force_authenticate(user=self.staff)
        resp = self.client.delete(
            '/api/patient-info/bulk_delete_filtered/?disease=Multiple+Myeloma'
        )
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.data['success'])
        # pi_a2 (Multiple Myeloma) should be gone
        self.assertFalse(PatientInfo.objects.filter(pk=self.pi_a2.pk).exists())
        # pi_a1 and pi_b (Breast Cancer) should survive
        self.assertTrue(PatientInfo.objects.filter(pk=self.pi_a1.pk).exists())
        self.assertTrue(PatientInfo.objects.filter(pk=self.pi_b.pk).exists())

    def test_deleted_count_matches_matched_rows(self):
        """deleted_count must equal the number of PatientInfo rows that matched the filters."""
        self.client.force_authenticate(user=self.staff)
        # Breast Cancer across both orgs: pi_a1 + pi_b = 2
        resp = self.client.delete(
            '/api/patient-info/bulk_delete_filtered/?disease=Breast+Cancer'
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data['deleted_count'], 2)

    def test_empty_filter_match_returns_zero(self):
        """A filter that matches no records should return deleted_count=0 with no error."""
        self.client.force_authenticate(user=self.staff)
        resp = self.client.delete(
            '/api/patient-info/bulk_delete_filtered/?disease=Nonexistent+Disease'
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data['deleted_count'], 0)
        self.assertEqual(resp.data['errors'], [])

    def test_org_a_token_deletes_all_org_a_patients_when_no_filter(self):
        """Org A token with no additional filters deletes all PatientInfo in org A only."""
        c = APIClient()
        c.credentials(HTTP_AUTHORIZATION=f'Bearer {self.write_token_a.token}')
        resp = c.delete('/api/patient-info/bulk_delete_filtered/')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data['deleted_count'], 2)
        # pi_a1 and pi_a2 should be gone; pi_b should survive
        self.assertFalse(PatientInfo.objects.filter(pk=self.pi_a1.pk).exists())
        self.assertFalse(PatientInfo.objects.filter(pk=self.pi_a2.pk).exists())
        self.assertTrue(PatientInfo.objects.filter(pk=self.pi_b.pk).exists())


# ---------------------------------------------------------------------------
# Org Management Tests
# ---------------------------------------------------------------------------

import secrets as _secrets
from omop_core.models import Organization, OrgTrust, OrgInvitation, GroupAccess
from omop_core.services.access import get_visible_orgs
from rest_framework.test import APIClient


def _make_user(email, is_staff=False):
    from patient_portal.models import Identity
    u = Identity.objects.create_user(email=email, password='testpass')
    u.is_staff = is_staff
    u.save()
    return u


def _make_org(name, slug):
    return Organization.objects.create(name=name, slug=slug)


class OrgManagementModelTest(TestCase):
    """OrgTrust XOR constraint, OrgInvitation uniqueness."""

    def setUp(self):
        self.org = _make_org('Test Org', 'test-org')
        self.org2 = _make_org('Partner Org', 'partner-org')

    def test_domain_trust_created(self):
        t = OrgTrust.objects.create(granting_org=self.org, trusted_domain='example.com')
        self.assertEqual(t.trusted_domain, 'example.com')
        self.assertIsNone(t.trusted_org)

    def test_org_trust_created(self):
        t = OrgTrust.objects.create(granting_org=self.org, trusted_org=self.org2)
        self.assertEqual(t.trusted_org, self.org2)
        self.assertEqual(t.trusted_domain, '')

    def test_xor_constraint_both_raises(self):
        from django.db import IntegrityError
        with self.assertRaises(IntegrityError):
            OrgTrust.objects.create(
                granting_org=self.org,
                trusted_org=self.org2,
                trusted_domain='bad.com',
            )

    def test_xor_constraint_neither_raises(self):
        from django.db import IntegrityError
        with self.assertRaises(IntegrityError):
            OrgTrust.objects.create(granting_org=self.org)

    def test_invitation_uniqueness_pending(self):
        """Two pending invitations for same org+email should fail."""
        from django.utils import timezone
        from django.db import IntegrityError
        expires = timezone.now() + timezone.timedelta(days=7)
        OrgInvitation.objects.create(
            org=self.org, email='test@example.com', role='doctor',
            token=_secrets.token_hex(32), expires_at=expires,
        )
        with self.assertRaises(IntegrityError):
            OrgInvitation.objects.create(
                org=self.org, email='test@example.com', role='doctor',
                token=_secrets.token_hex(32), expires_at=expires,
            )

    def test_invitation_status_pending(self):
        from django.utils import timezone
        expires = timezone.now() + timezone.timedelta(days=7)
        inv = OrgInvitation.objects.create(
            org=self.org, email='user@example.com', role='doctor',
            token=_secrets.token_hex(32), expires_at=expires,
        )
        self.assertEqual(inv.status, OrgInvitation.STATUS_PENDING)

    def test_invitation_status_expired(self):
        from django.utils import timezone
        expires = timezone.now() - timezone.timedelta(days=1)
        inv = OrgInvitation.objects.create(
            org=self.org, email='user2@example.com', role='doctor',
            token=_secrets.token_hex(32), expires_at=expires,
        )
        self.assertEqual(inv.status, OrgInvitation.STATUS_EXPIRED)

    def test_organization_is_active_default_true(self):
        self.assertTrue(self.org.is_active)

    def test_organization_can_be_inactive(self):
        self.org.is_active = False
        self.org.save()
        self.org.refresh_from_db()
        self.assertFalse(self.org.is_active)


class OrgTrustAccessTest(TestCase):
    """get_visible_orgs includes trust-based orgs."""

    def setUp(self):
        self.org_a = _make_org('Org A', 'org-a')
        self.org_b = _make_org('Org B', 'org-b')

        self.direct_user = _make_user('direct@test.com')
        GroupAccess.objects.create(identity=self.direct_user, org=self.org_a, role='org_admin')

        self.domain_user = _make_user('user@trusted.com')

        self.no_access_user = _make_user('noone@other.com')

    def test_direct_groupaccess_visible(self):
        orgs = get_visible_orgs(self.direct_user)
        self.assertIn(self.org_a, orgs)
        self.assertNotIn(self.org_b, orgs)

    def test_domain_trust_gives_access(self):
        OrgTrust.objects.create(granting_org=self.org_b, trusted_domain='trusted.com')
        orgs = get_visible_orgs(self.domain_user)
        self.assertIn(self.org_b, orgs)

    def test_org_to_org_trust_gives_access(self):
        """User with access to org_a gets access to org_b via org-to-org trust."""
        OrgTrust.objects.create(granting_org=self.org_b, trusted_org=self.org_a)
        orgs = get_visible_orgs(self.direct_user)
        self.assertIn(self.org_a, orgs)
        self.assertIn(self.org_b, orgs)

    def test_no_access_user_sees_nothing(self):
        orgs = get_visible_orgs(self.no_access_user)
        self.assertEqual(list(orgs), [])

    def test_no_open_org_fallback(self):
        """Users with no grants/trusts must not see any org."""
        new_user = _make_user('stranger@nowhere.com')
        # Even if org exists, no access without grant or trust
        orgs = get_visible_orgs(new_user)
        self.assertEqual(list(orgs), [])

    def test_staff_sees_all_orgs(self):
        staff = _make_user('staff@test.com', is_staff=True)
        orgs = get_visible_orgs(staff)
        self.assertIn(self.org_a, orgs)
        self.assertIn(self.org_b, orgs)


class OrgViewSetStaffTest(TestCase):
    """Staff can CRUD all orgs."""

    def setUp(self):
        self.client = APIClient()
        self.staff = _make_user('staff@example.com', is_staff=True)
        self.client.force_authenticate(user=self.staff)
        self.org = _make_org('Staff Org', 'staff-org')

    def test_list_orgs(self):
        resp = self.client.get('/api/orgs/')
        self.assertEqual(resp.status_code, 200)
        slugs = [o['slug'] for o in resp.data]
        self.assertIn('staff-org', slugs)

    def test_create_org(self):
        resp = self.client.post('/api/orgs/', {'name': 'New Org', 'slug': 'new-org'})
        self.assertEqual(resp.status_code, 201)
        self.assertTrue(Organization.objects.filter(slug='new-org').exists())

    def test_get_org(self):
        resp = self.client.get('/api/orgs/staff-org/')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data['slug'], 'staff-org')

    def test_patch_org(self):
        resp = self.client.patch('/api/orgs/staff-org/', {'name': 'Updated Name'})
        self.assertEqual(resp.status_code, 200)
        self.org.refresh_from_db()
        self.assertEqual(self.org.name, 'Updated Name')

    def test_delete_org(self):
        resp = self.client.delete('/api/orgs/staff-org/')
        self.assertEqual(resp.status_code, 204)
        self.assertFalse(Organization.objects.filter(slug='staff-org').exists())


class OrgViewSetOrgAdminTest(TestCase):
    """Org admin can only edit their own org."""

    def setUp(self):
        self.client = APIClient()
        self.admin = _make_user('admin@example.com')
        self.org = _make_org('Admin Org', 'admin-org')
        self.other_org = _make_org('Other Org', 'other-org')
        GroupAccess.objects.create(identity=self.admin, org=self.org, role='org_admin')
        self.client.force_authenticate(user=self.admin)

    def test_list_sees_only_own_org(self):
        resp = self.client.get('/api/orgs/')
        self.assertEqual(resp.status_code, 200)
        slugs = [o['slug'] for o in resp.data]
        self.assertIn('admin-org', slugs)
        self.assertNotIn('other-org', slugs)

    def test_cannot_create_org(self):
        resp = self.client.post('/api/orgs/', {'name': 'New', 'slug': 'new-slug'})
        self.assertEqual(resp.status_code, 403)

    def test_can_patch_own_org(self):
        resp = self.client.patch('/api/orgs/admin-org/', {'name': 'Renamed Org'})
        self.assertEqual(resp.status_code, 200)

    def test_cannot_delete_org(self):
        resp = self.client.delete('/api/orgs/admin-org/')
        self.assertEqual(resp.status_code, 403)

    def test_cannot_access_other_org(self):
        resp = self.client.get('/api/orgs/other-org/')
        self.assertEqual(resp.status_code, 403)


class OrgViewSetUnauthorizedTest(TestCase):
    """Non-admin gets 403."""

    def setUp(self):
        self.client = APIClient()
        self.user = _make_user('plain@example.com')
        self.org = _make_org('Secret Org', 'secret-org')
        self.client.force_authenticate(user=self.user)

    def test_list_returns_403(self):
        resp = self.client.get('/api/orgs/')
        self.assertEqual(resp.status_code, 403)

    def test_detail_returns_403(self):
        resp = self.client.get('/api/orgs/secret-org/')
        self.assertEqual(resp.status_code, 403)


class OrgInvitationFlowTest(TestCase):
    """Invite → confirm → GroupAccess created."""

    def setUp(self):
        self.client = APIClient()
        self.staff = _make_user('staff@example.com', is_staff=True)
        self.org = _make_org('Invite Org', 'invite-org')
        self.client.force_authenticate(user=self.staff)

    def test_invite_creates_invitation(self):
        resp = self.client.post('/api/orgs/invite-org/invite/', {
            'email': 'newuser@example.com',
            'role': 'doctor',
        })
        self.assertEqual(resp.status_code, 201)
        self.assertFalse(resp.data['access_granted'])
        self.assertTrue(
            OrgInvitation.objects.filter(org=self.org, email='newuser@example.com').exists()
        )

    def test_invite_existing_user_grants_access_immediately(self):
        invitee = Identity.objects.create_user(email='existing-user@example.com', password='pass')
        resp = self.client.post('/api/orgs/invite-org/invite/', {
            'email': 'existing-user@example.com',
            'role': 'navigator',
        })
        self.assertEqual(resp.status_code, 201)
        self.assertTrue(resp.data['access_granted'])
        self.assertTrue(
            GroupAccess.objects.filter(identity=invitee, org=self.org, role='navigator').exists()
        )

    def test_invite_existing_user_updates_existing_org_role(self):
        invitee = Identity.objects.create_user(email='role-update@example.com', password='pass')
        GroupAccess.objects.create(identity=invitee, org=self.org, role='navigator')
        resp = self.client.post('/api/orgs/invite-org/invite/', {
            'email': 'role-update@example.com',
            'role': 'doctor',
        })
        self.assertEqual(resp.status_code, 201)
        self.assertTrue(resp.data['access_granted'])
        grant = GroupAccess.objects.get(identity=invitee, org=self.org)
        self.assertEqual(grant.role, 'doctor')

    def test_list_invitations(self):
        from django.utils import timezone
        OrgInvitation.objects.create(
            org=self.org, email='listed@example.com', role='doctor',
            token=_secrets.token_hex(32),
            expires_at=timezone.now() + timezone.timedelta(days=7),
        )
        resp = self.client.get('/api/orgs/invite-org/invitations/')
        self.assertEqual(resp.status_code, 200)
        emails = [i['email'] for i in resp.data]
        self.assertIn('listed@example.com', emails)

    def test_confirm_invitation_creates_access(self):
        from django.utils import timezone
        from patient_portal.models import Identity
        # Create the identity for the invited email
        invitee = Identity.objects.create_user(email='invitee@example.com', password='pass')
        token = _secrets.token_hex(32)
        OrgInvitation.objects.create(
            org=self.org, email='invitee@example.com', role='doctor',
            token=token,
            expires_at=timezone.now() + timezone.timedelta(days=7),
        )
        # Confirm (public endpoint — no auth)
        public_client = APIClient()
        resp = public_client.post('/api/orgs/confirm-invitation/', {'token': token})
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(
            GroupAccess.objects.filter(identity=invitee, org=self.org, role='doctor').exists()
        )
        inv = OrgInvitation.objects.get(token=token)
        self.assertEqual(inv.status, OrgInvitation.STATUS_CONFIRMED)

    def test_cancel_invitation(self):
        from django.utils import timezone
        token = _secrets.token_hex(32)
        inv = OrgInvitation.objects.create(
            org=self.org, email='cancel@example.com', role='doctor',
            token=token,
            expires_at=timezone.now() + timezone.timedelta(days=7),
        )
        resp = self.client.delete(f'/api/orgs/invite-org/invitations/{inv.id}/')
        self.assertEqual(resp.status_code, 204)
        inv.refresh_from_db()
        self.assertEqual(inv.status, OrgInvitation.STATUS_CANCELLED)

    def test_confirm_nonexistent_token_returns_404(self):
        public_client = APIClient()
        resp = public_client.post('/api/orgs/confirm-invitation/', {'token': 'deadbeef' * 8})
        self.assertEqual(resp.status_code, 404)

    def test_invite_sends_email(self):
        from django.core import mail
        resp = self.client.post('/api/orgs/invite-org/invite/', {
            'email': 'emailtest@example.com',
            'role': 'doctor',
        })
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(len(mail.outbox), 1)
        msg = mail.outbox[0]
        self.assertEqual(msg.to, ['emailtest@example.com'])
        self.assertIn('Invite Org', msg.subject)
        self.assertIn('/accept-invite?token=', msg.body)

    def test_invite_email_contains_valid_token(self):
        from django.core import mail
        resp = self.client.post('/api/orgs/invite-org/invite/', {
            'email': 'tokencheck@example.com',
            'role': 'navigator',
        })
        self.assertEqual(resp.status_code, 201)
        token = OrgInvitation.objects.get(org=self.org, email='tokencheck@example.com').token
        self.assertIn(token, mail.outbox[0].body)

    def test_email_failure_still_creates_invitation(self):
        from unittest.mock import patch
        invitee = Identity.objects.create_user(email='failmail@example.com', password='pass')
        with patch('patient_portal.api.org_views.send_mail', side_effect=Exception('SMTP error')):
            resp = self.client.post('/api/orgs/invite-org/invite/', {
                'email': 'failmail@example.com',
                'role': 'doctor',
            })
        self.assertEqual(resp.status_code, 201)
        self.assertTrue(resp.data['access_granted'])
        self.assertEqual(
            resp.data['email_warning'],
            'Invitation was created, but the email could not be sent.',
        )
        self.assertTrue(
            OrgInvitation.objects.filter(org=self.org, email='failmail@example.com', role='doctor').exists()
        )
        self.assertTrue(
            GroupAccess.objects.filter(identity=invitee, org=self.org, role='doctor').exists()
        )

    def test_email_failure_updates_existing_pending_invitation(self):
        from django.utils import timezone
        from unittest.mock import patch
        token = _secrets.token_hex(32)
        existing = OrgInvitation.objects.create(
            org=self.org, email='existing@example.com', role='doctor',
            token=token,
            expires_at=timezone.now() + timezone.timedelta(days=7),
        )
        with patch('patient_portal.api.org_views.send_mail', side_effect=Exception('SMTP error')):
            resp = self.client.post('/api/orgs/invite-org/invite/', {
                'email': 'existing@example.com',
                'role': 'navigator',
            })
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(
            resp.data['email_warning'],
            'Invitation was created, but the email could not be sent.',
        )
        existing.refresh_from_db()
        self.assertNotEqual(existing.token, token)
        self.assertEqual(existing.role, 'navigator')
        self.assertIsNone(existing.cancelled_at)


class OrgTrustAPITest(TestCase):
    """Staff can manage trusts via API."""

    def setUp(self):
        self.client = APIClient()
        self.staff = _make_user('staff@example.com', is_staff=True)
        self.org = _make_org('Trust Org', 'trust-org')
        self.partner = _make_org('Partner', 'partner')
        self.client.force_authenticate(user=self.staff)

    def test_add_domain_trust(self):
        resp = self.client.post('/api/orgs/trust-org/trusts/', {'trusted_domain': 'partner.com'})
        self.assertEqual(resp.status_code, 201)
        self.assertTrue(OrgTrust.objects.filter(granting_org=self.org, trusted_domain='partner.com').exists())

    def test_add_org_trust(self):
        resp = self.client.post('/api/orgs/trust-org/trusts/', {'trusted_org': self.partner.id})
        self.assertEqual(resp.status_code, 201)
        self.assertTrue(OrgTrust.objects.filter(granting_org=self.org, trusted_org=self.partner).exists())

    def test_add_both_raises_400(self):
        resp = self.client.post('/api/orgs/trust-org/trusts/', {
            'trusted_domain': 'bad.com', 'trusted_org': self.partner.id,
        })
        self.assertEqual(resp.status_code, 400)

    def test_list_trusts(self):
        OrgTrust.objects.create(granting_org=self.org, trusted_domain='listed.com')
        resp = self.client.get('/api/orgs/trust-org/trusts/')
        self.assertEqual(resp.status_code, 200)
        domains = [t['trusted_domain'] for t in resp.data]
        self.assertIn('listed.com', domains)

    def test_delete_trust(self):
        trust = OrgTrust.objects.create(granting_org=self.org, trusted_domain='delete.com')
        resp = self.client.delete(f'/api/orgs/trust-org/trusts/{trust.id}/')
        self.assertEqual(resp.status_code, 204)
        self.assertFalse(OrgTrust.objects.filter(id=trust.id).exists())


class OrgAccessAPITest(TestCase):
    """Org admin / staff can view and revoke access grants."""

    def setUp(self):
        self.client = APIClient()
        self.staff = _make_user('staff@example.com', is_staff=True)
        self.org = _make_org('Access Org', 'access-org')
        self.grantee = _make_user('grantee@example.com')
        self.grant = GroupAccess.objects.create(identity=self.grantee, org=self.org, role='doctor')
        self.client.force_authenticate(user=self.staff)

    def test_list_access_grants(self):
        resp = self.client.get('/api/orgs/access-org/access/')
        self.assertEqual(resp.status_code, 200)
        emails = [g['email'] for g in resp.data]
        self.assertIn('grantee@example.com', emails)

    def test_revoke_access_grant(self):
        resp = self.client.delete(f'/api/orgs/access-org/access/{self.grant.id}/')
        self.assertEqual(resp.status_code, 204)
        self.assertFalse(GroupAccess.objects.filter(id=self.grant.id).exists())


class SetupDemoCommandTest(TestCase):
    """setup_demo management command is idempotent."""

    def setUp(self):
        _make_org('ABC Foundation', 'abc-foundation')

    def test_idempotent_run_twice(self):
        from django.core.management import call_command
        import io
        out = io.StringIO()
        call_command('setup_demo', stdout=out)
        call_command('setup_demo', stdout=out)

        from patient_portal.models import Identity
        count = Identity.objects.filter(email='random@healthkey.ai', issuer='urn:local').count()
        self.assertEqual(count, 1)

        trust_count = OrgTrust.objects.filter(trusted_domain='healthkey.ai').count()
        self.assertEqual(trust_count, 1)

    def test_demo_user_created(self):
        from django.core.management import call_command
        import io
        call_command('setup_demo', stdout=io.StringIO())
        from patient_portal.models import Identity
        user = Identity.objects.get(email='random@healthkey.ai')
        self.assertFalse(user.is_staff)
        self.assertTrue(user.check_password('password123!'))

    def test_domain_trust_created(self):
        from django.core.management import call_command
        import io
        call_command('setup_demo', stdout=io.StringIO())
        org = Organization.objects.get(slug='abc-foundation')
        self.assertTrue(OrgTrust.objects.filter(granting_org=org, trusted_domain='healthkey.ai').exists())

    def test_no_abc_foundation_skips_trust(self):
        """Command should not crash if abc-foundation org doesn't exist."""
        Organization.objects.filter(slug='abc-foundation').delete()
        from django.core.management import call_command
        import io
        call_command('setup_demo', stdout=io.StringIO())  # should not raise


class UserSerializerOrgAdminTest(TestCase):
    """UserSerializer.is_org_admin field."""

    def setUp(self):
        self.client = APIClient()
        self.user = _make_user('user@example.com')
        self.org = _make_org('Serializer Org', 'serializer-org')

    def test_is_org_admin_false_without_grant(self):
        self.client.force_authenticate(user=self.user)
        resp = self.client.get('/api/user/')
        self.assertEqual(resp.status_code, 200)
        data = resp.data.get('user', resp.data)
        self.assertFalse(data.get('is_org_admin'))

    def test_is_org_admin_true_with_grant(self):
        GroupAccess.objects.create(identity=self.user, org=self.org, role='org_admin')
        self.client.force_authenticate(user=self.user)
        resp = self.client.get('/api/user/')
        self.assertEqual(resp.status_code, 200)
        data = resp.data.get('user', resp.data)
        self.assertTrue(data.get('is_org_admin'))


# ---------------------------------------------------------------------------
# Wearable summary field tests
# ---------------------------------------------------------------------------

class WearablePatientInfoTest(TestCase):
    """_get_wearable_data aggregates OMOP Measurement/Observation into PatientInfo."""

    def setUp(self):
        import datetime
        from omop_core.models import Concept, Vocabulary, Domain, ConceptClass
        from omop_core.services.mappings import WEARABLE_LOINC

        self.today = datetime.date.today()

        # Minimal vocab stubs
        vocab_loinc, _ = Vocabulary.objects.get_or_create(
            vocabulary_id='LOINC',
            defaults={'vocabulary_name': 'LOINC', 'vocabulary_reference': '',
                      'vocabulary_version': '', 'vocabulary_concept_id': 0},
        )
        domain_m, _ = Domain.objects.get_or_create(
            domain_id='Measurement',
            defaults={'domain_name': 'Measurement', 'domain_concept_id': 21},
        )
        domain_o, _ = Domain.objects.get_or_create(
            domain_id='Observation',
            defaults={'domain_name': 'Observation', 'domain_concept_id': 27},
        )
        cc, _ = ConceptClass.objects.get_or_create(
            concept_class_id='Lab Test',
            defaults={'concept_class_name': 'Lab Test', 'concept_class_concept_id': 0},
        )

        base_id = 9_900_000
        self.concepts = {}
        for i, (key, loinc_code) in enumerate(WEARABLE_LOINC.items()):
            c, _ = Concept.objects.get_or_create(
                concept_id=base_id + i,
                defaults={
                    'concept_name': key,
                    'domain_id': 'Observation' if key == 'sleep_duration' else 'Measurement',
                    'vocabulary_id': 'LOINC',
                    'concept_class_id': 'Lab Test',
                    'concept_code': loinc_code,
                    'valid_start_date': datetime.date(1970, 1, 1),
                    'valid_end_date': datetime.date(2099, 12, 31),
                },
            )
            self.concepts[key] = c

        # Measurement type concept required by FK
        import datetime
        type_vocab, _ = Vocabulary.objects.get_or_create(
            vocabulary_id='Meas Type',
            defaults={'vocabulary_name': 'Meas Type', 'vocabulary_reference': '',
                      'vocabulary_version': '', 'vocabulary_concept_id': 0},
        )
        type_domain, _ = Domain.objects.get_or_create(
            domain_id='Type Concept',
            defaults={'domain_name': 'Type Concept', 'domain_concept_id': 0},
        )
        type_cc, _ = ConceptClass.objects.get_or_create(
            concept_class_id='Meas Type',
            defaults={'concept_class_name': 'Meas Type', 'concept_class_concept_id': 0},
        )
        Concept.objects.get_or_create(
            concept_id=32856,
            defaults={
                'concept_name': 'Lab',
                'domain_id': 'Type Concept',
                'vocabulary_id': 'Meas Type',
                'concept_class_id': 'Meas Type',
                'concept_code': 'Lab',
                'valid_start_date': datetime.date(1970, 1, 1),
                'valid_end_date': datetime.date(2099, 12, 31),
            },
        )

        self.person = Person.objects.create(person_id=88_000)
        PatientInfo.objects.get_or_create(person=self.person)
        self._meas_id = 8_800_000
        self._obs_id = 8_900_000

    def _add_measurement(self, concept_key, days_ago, value):
        """Insert a Measurement for the given concept key."""
        import datetime
        from django.utils import timezone
        self._meas_id += 1
        d = self.today - datetime.timedelta(days=days_ago)
        Measurement.objects.create(
            measurement_id=self._meas_id,
            person=self.person,
            measurement_concept=self.concepts[concept_key],
            measurement_date=d,
            measurement_datetime=timezone.make_aware(
                datetime.datetime.combine(d, datetime.time())
            ),
            measurement_type_concept_id=32856,
            value_as_number=value,
            measurement_source_value=self.concepts[concept_key].concept_code,
        )

    def _add_sleep_obs(self, days_ago, hours):
        from omop_core.models import Observation
        from django.utils import timezone
        import datetime
        self._obs_id += 1
        d = self.today - datetime.timedelta(days=days_ago)
        Observation.objects.create(
            observation_id=self._obs_id,
            person=self.person,
            observation_concept=self.concepts['sleep_duration'],
            observation_date=d,
            observation_datetime=timezone.make_aware(
                datetime.datetime.combine(d, datetime.time())
            ),
            observation_type_concept_id=32856,
            value_as_number=hours,
            observation_source_value=self.concepts['sleep_duration'].concept_code,
        )

    def _refresh(self):
        from omop_core.services.patient_info_service import refresh_patient_info
        from omop_core.services.concept_cache import concept_cache_clear
        concept_cache_clear()
        return refresh_patient_info(self.person)

    def test_no_wearable_data_leaves_fields_null(self):
        pi = self._refresh()
        self.assertIsNone(pi.wearable_last_sync_at)
        self.assertIsNone(pi.median_daily_steps_30d)
        self.assertIsNone(pi.wearable_coverage_ratio_30d)

    def test_step_aggregation_30_days(self):
        # 20 days of step data → meets MIN_VALID_DAYS (7)
        for d in range(1, 21):
            self._add_measurement('steps', d, 8000)
        pi = self._refresh()
        self.assertIsNotNone(pi.wearable_last_sync_at)
        self.assertEqual(pi.median_daily_steps_30d, 8000)
        self.assertIsNotNone(pi.wearable_coverage_ratio_30d)

    def test_coverage_ratio_calculation(self):
        # Exactly 15 days of step data → ratio = 0.5
        for d in range(1, 16):
            self._add_measurement('steps', d, 5000)
        pi = self._refresh()
        self.assertAlmostEqual(float(pi.wearable_coverage_ratio_30d), 0.5, places=1)

    def test_insufficient_coverage_leaves_metric_null(self):
        # Only 3 days → below MIN_VALID_DAYS, steps median should be None
        for d in range(1, 4):
            self._add_measurement('steps', d, 10000)
        pi = self._refresh()
        self.assertIsNone(pi.median_daily_steps_30d)
        # But coverage ratio is still computed
        self.assertIsNotNone(pi.wearable_coverage_ratio_30d)

    def test_cardiovascular_aggregation(self):
        for d in range(1, 20):
            self._add_measurement('resting_hr', d, 60)
            self._add_measurement('hrv_sdnn', d, 45)
        pi = self._refresh()
        self.assertEqual(pi.resting_heart_rate_avg_30d, 60)
        self.assertAlmostEqual(float(pi.hrv_sdnn_avg_30d), 45.0, places=1)

    def test_spo2_artifact_filter(self):
        # Valid readings
        for d in range(1, 20):
            self._add_measurement('spo2', d, 97.0)
        # Artifact reading below 70 — should be discarded
        self._add_measurement('spo2', 20, 50.0)
        pi = self._refresh()
        # Min of valid readings only
        self.assertAlmostEqual(float(pi.oxygen_saturation_min_30d), 97.0, places=1)

    def test_activity_trend_improving(self):
        # First half (days 16–29): 3000 steps/day; second half (days 1–15): 9000 steps/day
        for d in range(16, 30):
            self._add_measurement('steps', d, 3000)
        for d in range(1, 16):
            self._add_measurement('steps', d, 9000)
        pi = self._refresh()
        self.assertEqual(pi.activity_trend_30d, 'improving')

    def test_activity_trend_declining(self):
        for d in range(16, 30):
            self._add_measurement('steps', d, 9000)
        for d in range(1, 16):
            self._add_measurement('steps', d, 3000)
        pi = self._refresh()
        self.assertEqual(pi.activity_trend_30d, 'declining')

    def test_sleep_duration_from_observation(self):
        for d in range(1, 20):
            self._add_sleep_obs(d, 7.5)
        pi = self._refresh()
        self.assertAlmostEqual(float(pi.sleep_duration_hours_avg_30d), 7.5, places=1)

    def test_timestamped_sample_anchors_after_older_date_only_sample(self):
        import datetime
        from django.utils import timezone

        self._meas_id += 1
        old_date = self.today - datetime.timedelta(days=60)
        Measurement.objects.create(
            measurement_id=self._meas_id,
            person=self.person,
            measurement_concept=self.concepts['steps'],
            measurement_date=old_date,
            measurement_datetime=None,
            measurement_type_concept_id=32856,
            value_as_number=1000,
            measurement_source_value=self.concepts['steps'].concept_code,
        )
        self._meas_id += 1
        Measurement.objects.create(
            measurement_id=self._meas_id,
            person=self.person,
            measurement_concept=self.concepts['resting_hr'],
            measurement_date=self.today,
            measurement_datetime=timezone.make_aware(
                datetime.datetime.combine(self.today, datetime.time(hour=12))
            ),
            measurement_type_concept_id=32856,
            value_as_number=65,
            measurement_source_value=self.concepts['resting_hr'].concept_code,
        )

        pi = self._refresh()
        self.assertEqual(pi.wearable_last_sync_at.date(), self.today)

    def test_activity_trend_stable(self):
        # Uniform 8000 steps/day for all 30 days → < 10% change → stable
        for d in range(1, 31):
            self._add_measurement('steps', d, 8000)
        pi = self._refresh()
        self.assertEqual(pi.activity_trend_30d, 'stable')

    def test_activity_trend_insufficient_when_no_steps(self):
        # Only HR data, no steps → trend must be 'insufficient_data', not None
        for d in range(1, 20):
            self._add_measurement('resting_hr', d, 65)
        pi = self._refresh()
        self.assertEqual(pi.activity_trend_30d, 'insufficient_data')

    def test_coverage_ratio_counts_non_step_metrics(self):
        # HR data only (no steps) → coverage should reflect those days, not 0.0
        for d in range(1, 16):
            self._add_measurement('resting_hr', d, 65)
        pi = self._refresh()
        self.assertGreater(float(pi.wearable_coverage_ratio_30d), 0.0)

    def test_wearable_fields_in_api_response(self):
        """New fields appear in GET /api/patients/{id}/."""
        user = _make_user('wearable-test@example.com', is_staff=True)
        client = APIClient()
        client.force_authenticate(user=user)
        resp = client.get(f'/api/patient-info/{self.person.person_id}/')
        self.assertEqual(resp.status_code, 200)
        pi_data = resp.data.get('patient_info', resp.data)
        for field in [
            'wearable_last_sync_at', 'wearable_coverage_ratio_30d',
            'median_daily_steps_30d', 'active_minutes_per_day_30d',
            'activity_trend_30d', 'resting_heart_rate_avg_30d',
            'hrv_sdnn_avg_30d', 'oxygen_saturation_min_30d',
            'respiratory_rate_avg_30d', 'sleep_duration_hours_avg_30d',
        ]:
            self.assertIn(field, pi_data, f'Missing field: {field}')

    def test_wearable_fields_are_serializer_read_only(self):
        from patient_portal.api.serializers import PatientInfoSerializer

        pi = PatientInfo.objects.get(person=self.person)
        serializer = PatientInfoSerializer(
            pi,
            data={
                'median_daily_steps_30d': 99999,
                'activity_trend_30d': 'improving',
            },
            partial=True,
        )
        self.assertTrue(serializer.is_valid(), serializer.errors)
        serializer.save()

        pi.refresh_from_db()
        self.assertIsNone(pi.median_daily_steps_30d)
        self.assertIsNone(pi.activity_trend_30d)

    def test_wearable_endpoint_requires_authentication(self):
        """Unauthenticated requests to patient-info must be rejected."""
        from rest_framework.test import APIClient as AnonClient
        anon = AnonClient()
        resp = anon.get(f'/api/patient-info/{self.person.person_id}/')
        self.assertIn(resp.status_code, [401, 403])
