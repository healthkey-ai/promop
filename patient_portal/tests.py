"""
Integration tests for the FHIR upload pipeline and UI API views.

Test flow:
  1. POST a synthetic FHIR bundle to /api/patient-info/upload_fhir/
  2. Assert OMOP tables (Person, ConditionOccurrence, Measurement,
     DrugExposure, Episode, EpisodeEvent) are populated
  3. Assert PatientRecord is derived and key fields are correct
  4. Assert the UI-facing API endpoints return the uploaded data
"""

import io
import json
import os
import tempfile
from datetime import date, timedelta

from patient_portal.models import Identity
from django.test import TestCase
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient

from omop_core.models import (
    Concept, ConceptClass, Domain, Vocabulary,
    Person, PatientRecord, ProvenanceRecord,
    ConditionOccurrence, DrugExposure, Measurement, Observation, ProcedureOccurrence,
    Death, PatientDocument, RecordRevision,
    Relationship, ConceptRelationship, ConceptAncestor,
    SctEligibility,
    FhirConnection, FhirOauthState, Institution,
    ObservationPeriod, PatientSurveyResponse, PersonLanguageSkill, Survey,
)
from omop_core.services.organization_cleanup import delete_organization_with_patient_cascade
from omop_oncology.models import CancerModifier, Episode, EpisodeEvent, Histology, StemTable


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
    domain_procedure, _ = Domain.objects.get_or_create(
        domain_id='Procedure',
        defaults={'domain_name': 'Procedure', 'domain_concept_id': 10},
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
    # Generic procedure concept — fallback for FHIR Procedure ingestion
    _concept(20000001, 'Procedure',               domain_procedure)
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
            {'url': 'https://healthkey.ai/fhir/StructureDefinition/ethnicity', 'valueString': 'White'},
            {'url': 'https://healthkey.ai/fhir/StructureDefinition/bodyWeight',
             'valueQuantity': {'value': 65.0, 'unit': 'kg'}},
            {'url': 'https://healthkey.ai/fhir/StructureDefinition/bodyHeight',
             'valueQuantity': {'value': 165.0, 'unit': 'cm'}},
            {'url': 'https://healthkey.ai/fhir/StructureDefinition/ecog-performance-status',
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
                {'url': 'https://healthkey.ai/fhir/StructureDefinition/therapy-line',
                 'valueInteger': lot_num},
                {'url': 'https://healthkey.ai/fhir/StructureDefinition/therapy-outcome',
                 'valueString': outcome},
            ],
        }
        if end:
            stmt['effectivePeriod']['end'] = end
        return stmt

    lot1 = _med_statement('med-ac-t',    'AC-T',    1, '2022-03-01', '2022-09-01', 'CR')
    lot2 = _med_statement('med-kadcyla', 'Kadcyla', 2, '2023-01-15', None,         'PR')

    procedure = {
        'resourceType': 'Procedure',
        'id': 'proc-biopsy-001',
        'status': 'completed',
        'subject': {'reference': f'Patient/{patient_id}'},
        'performedDateTime': '2022-01-20',
        'code': {
            'coding': [{
                'system': 'http://snomed.info/sct',
                'code': '387713003',
                'display': 'Surgical biopsy of breast',
            }],
            'text': 'Surgical biopsy of breast',
        },
    }

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
            {'resource': procedure},
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

    def test_procedure_occurrence_created(self):
        """A FHIR Procedure resource should create a ProcedureOccurrence row."""
        procedures = ProcedureOccurrence.objects.filter(person=self._person)
        self.assertEqual(procedures.count(), 1)
        procedure = procedures.first()
        self.assertEqual(procedure.procedure_date, date(2022, 1, 20))
        self.assertEqual(procedure.procedure_source_value, '387713003')

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

    def test_lot_outcome_observation_created_from_medication_statement(self):
        """The LOT-1 therapy-outcome extension is persisted to OMOP as a
        LOT-1-outcome Observation (source of truth for the derived outcome)."""
        from omop_core.models import Observation
        obs = Observation.objects.filter(
            person=self._person, observation_source_value='LOT-1-outcome',
        )
        self.assertEqual(obs.count(), 1)
        self.assertEqual(obs.first().value_as_string, 'CR')

    def test_deceased_patient_creates_death_row(self):
        bundle = _make_fhir_bundle()
        patient = next(
            entry['resource']
            for entry in bundle['entry']
            if entry['resource']['resourceType'] == 'Patient'
        )
        patient['id'] = 'test-patient-jane-deceased'
        patient['name'] = [{'family': 'Deceased', 'given': ['Jane']}]
        patient['deceasedDateTime'] = '2024-04-05T12:34:00Z'
        bundle_bytes = json.dumps(bundle).encode('utf-8')
        fhir_file = io.BytesIO(bundle_bytes)
        fhir_file.name = 'deceased_bundle.json'

        response = self.client.post(
            '/api/patient-info/upload_fhir/',
            {'file': fhir_file},
            format='multipart',
        )

        self.assertIn(response.status_code, [status.HTTP_200_OK, status.HTTP_201_CREATED])
        person = Person.objects.get(family_name='Deceased', given_name='Jane')
        death = Death.objects.get(person=person)
        self.assertEqual(death.death_date, date(2024, 4, 5))
        provenance = ProvenanceRecord.objects.get(object_id=person.person_id, content_type__model='death')
        self.assertEqual(provenance.source, 'EHR_SYNC')
        self.assertIsNone(provenance.modification_reason)

    def test_deceased_boolean_death_row_records_inferred_date_provenance(self):
        bundle = _make_fhir_bundle()
        patient = next(
            entry['resource']
            for entry in bundle['entry']
            if entry['resource']['resourceType'] == 'Patient'
        )
        patient['id'] = 'test-patient-jane-deceased-bool'
        patient['name'] = [{'family': 'BooleanDeceased', 'given': ['Jane']}]
        patient['deceasedBoolean'] = True
        bundle_bytes = json.dumps(bundle).encode('utf-8')
        fhir_file = io.BytesIO(bundle_bytes)
        fhir_file.name = 'deceased_boolean_bundle.json'

        response = self.client.post(
            '/api/patient-info/upload_fhir/',
            {'file': fhir_file},
            format='multipart',
        )

        self.assertIn(response.status_code, [status.HTTP_200_OK, status.HTTP_201_CREATED])
        person = Person.objects.get(family_name='BooleanDeceased', given_name='Jane')
        death = Death.objects.get(person=person)
        self.assertIsNotNone(death.death_date)
        provenance = ProvenanceRecord.objects.get(object_id=person.person_id, content_type__model='death')
        self.assertEqual(provenance.source, 'EHR_SYNC')
        self.assertIn('deceasedBoolean=true', provenance.modification_reason)


class FhirUploadStringLabValueTest(FhirUploadBase):
    """A string-valued lab Observation (e.g. ISS stage) must keep its value on
    import and drive derivation (regression for issue #218)."""

    def _upload_stage_bundle(self):
        bundle = {
            'resourceType': 'Bundle',
            'type': 'collection',
            'entry': [
                {'resource': {
                    'resourceType': 'Patient',
                    'id': 'stage-pt-1',
                    'name': [{'family': 'Stagevalue', 'given': ['Iss']}],
                    'gender': 'female',
                    'birthDate': '1960-04-01',
                }},
                {'resource': {
                    'resourceType': 'Observation',
                    'status': 'final',
                    'subject': {'reference': 'Patient/stage-pt-1'},
                    'effectiveDateTime': '2023-05-01',
                    'category': [{'coding': [{
                        'system': 'http://terminology.hl7.org/CodeSystem/observation-category',
                        'code': 'laboratory'}]}],
                    'code': {'coding': [{'system': 'http://loinc.org', 'code': '21908-9',
                                         'display': 'ISS stage'}], 'text': 'ISS stage'},
                    'valueString': 'ISS II',
                }},
                {'resource': {
                    'resourceType': 'Observation',
                    'status': 'final',
                    'subject': {'reference': 'Patient/stage-pt-1'},
                    'effectiveDateTime': '2023-05-01',
                    'category': [{'coding': [{
                        'system': 'http://terminology.hl7.org/CodeSystem/observation-category',
                        'code': 'laboratory'}]}],
                    'code': {'coding': [{'system': 'http://loinc.org', 'code': '21908-9-riss',
                                         'display': 'R-ISS stage'}], 'text': 'R-ISS stage'},
                    'valueString': 'R-ISS III',
                }},
            ],
        }
        bundle_bytes = json.dumps(bundle).encode('utf-8')
        fhir_file = io.BytesIO(bundle_bytes)
        fhir_file.name = 'stage_bundle.json'
        return self.client.post(
            '/api/patient-info/upload_fhir/', {'file': fhir_file}, format='multipart',
        )

    def test_string_lab_value_persisted(self):
        from omop_core.models import Measurement
        resp = self._upload_stage_bundle()
        self.assertIn(resp.status_code, [status.HTTP_200_OK, status.HTTP_201_CREATED])

        person = Person.objects.get(family_name='Stagevalue', given_name='Iss')
        measurement = Measurement.objects.get(
            person=person, measurement_source_value='21908-9',
        )
        self.assertEqual(measurement.value_as_string, 'ISS II')

    def test_stage_derivation_prefers_riss(self):
        self._upload_stage_bundle()
        person = Person.objects.get(family_name='Stagevalue', given_name='Iss')
        record = PatientRecord.objects.get(person=person)
        # Both ISS and R-ISS present → R-ISS is preferred for MM.
        self.assertEqual(record.stage, 'R-ISS III')

    def test_stage_with_condition_prefers_riss_end_to_end(self):
        """Real MM bundles carry a Condition with both ISS and R-ISS stage
        entries. The Condition.stage patch (applied after refresh) must also
        prefer R-ISS, otherwise it overrides the derived value with ISS."""
        pid = 'mm-cond-1'

        def _obs(code, disp, val):
            return {'resource': {
                'resourceType': 'Observation', 'status': 'final',
                'subject': {'reference': f'Patient/{pid}'},
                'effectiveDateTime': '2023-05-01',
                'category': [{'coding': [{
                    'system': 'http://terminology.hl7.org/CodeSystem/observation-category',
                    'code': 'laboratory'}]}],
                'code': {'coding': [{'system': 'http://loinc.org', 'code': code,
                                     'display': disp}], 'text': disp},
                'valueString': val,
            }}

        bundle = {
            'resourceType': 'Bundle', 'type': 'collection',
            'entry': [
                {'resource': {
                    'resourceType': 'Patient', 'id': pid,
                    'name': [{'family': 'Mmcond', 'given': ['Joe']}],
                    'gender': 'male', 'birthDate': '1955-01-01',
                }},
                {'resource': {
                    'resourceType': 'Condition', 'id': 'c1',
                    'subject': {'reference': f'Patient/{pid}'},
                    'code': {'text': 'Multiple Myeloma', 'coding': [
                        {'system': 'http://snomed.info/sct', 'code': '55921005',
                         'display': 'Multiple myeloma'}]},
                    'onsetDateTime': '2023-01-01',
                    'stage': [
                        {'summary': {'text': 'ISS Stage II'}},
                        {'summary': {'text': 'R-ISS Stage III'}},
                    ],
                }},
                _obs('21908-9', 'ISS stage', 'ISS II'),
                _obs('21908-9-riss', 'R-ISS stage', 'R-ISS III'),
            ],
        }
        bundle_bytes = json.dumps(bundle).encode('utf-8')
        fhir_file = io.BytesIO(bundle_bytes)
        fhir_file.name = 'mm_cond.json'
        resp = self.client.post(
            '/api/patient-info/upload_fhir/', {'file': fhir_file}, format='multipart',
        )
        self.assertIn(resp.status_code, [status.HTTP_200_OK, status.HTTP_201_CREATED])

        record = PatientRecord.objects.get(person=Person.objects.get(family_name='Mmcond'))
        self.assertEqual(record.stage, 'R-ISS III')


# ---------------------------------------------------------------------------
# 2. PatientRecord derivation tests
# ---------------------------------------------------------------------------

class FhirUploadPatientRecordTest(FhirUploadBase):
    """Verify PatientRecord is created and correctly derived from uploaded FHIR data."""

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
        cls._pi = PatientRecord.objects.get(person=cls._person)

    def test_patient_info_created(self):
        self.assertIsNotNone(self._pi, 'PatientRecord not created for uploaded patient')

    def test_disease_populated_from_condition(self):
        self.assertIsNotNone(self._pi.disease, 'PatientRecord.disease not populated')

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

    def test_death_date_derived_from_omop_death(self):
        ehr_concept = Concept.objects.get(concept_id=32817)
        Death.objects.update_or_create(
            person=self._person,
            defaults={
                'death_date': date(2024, 4, 5),
                'death_type_concept': ehr_concept,
            },
        )
        from omop_core.services.patient_record_service import refresh_patient_record

        refreshed = refresh_patient_record(self._person)

        self.assertEqual(refreshed.death_date, date(2024, 4, 5))


# ---------------------------------------------------------------------------
# 2b. FL → DLBCL transformation — upload, derivation, and validation
# ---------------------------------------------------------------------------

def _make_fl_bundle():
    """FHIR bundle for an FL patient who transformed to DLBCL."""
    patient_id = 'test-patient-fl-001'
    return {
        'resourceType': 'Bundle',
        'type': 'collection',
        'entry': [
            {'resource': {
                'resourceType': 'Patient',
                'id': patient_id,
                'name': [{'family': 'Follic', 'given': ['Larry']}],
                'gender': 'male',
                'birthDate': '1960-01-01',
            }},
            {'resource': {
                'resourceType': 'Condition',
                'id': 'cond-fl-001',
                'subject': {'reference': f'Patient/{patient_id}'},
                'code': {'text': 'Follicular Lymphoma', 'coding': [
                    {'system': 'http://snomed.info/sct', 'code': '413448000',
                     'display': 'Follicular non-Hodgkin lymphoma'},
                ]},
                'onsetDateTime': '2020-06-01',
            }},
            {'resource': {
                'resourceType': 'Condition',
                'id': 'cond-dlbcl-001',
                'subject': {'reference': f'Patient/{patient_id}'},
                'code': {'text': 'Diffuse Large B-Cell Lymphoma (transformed)', 'coding': [
                    {'system': 'http://hl7.org/fhir/sid/icd-10-cm', 'code': 'C83.30',
                     'display': 'Diffuse large B-cell lymphoma, unspecified'},
                ]},
                'onsetDateTime': '2023-04-15',
            }},
        ],
    }


class FhirUploadFlDlbclTransformationTest(FhirUploadBase):
    """FL + DLBCL Conditions upload → ConditionOccurrence rows → derived transformation fields."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        vocab = Vocabulary.objects.get(vocabulary_id='TEST')
        domain = Domain.objects.get(domain_id='Condition')
        cc = ConceptClass.objects.get(concept_class_id='Clinical Finding')
        today = date.today()
        for cid, name in ((42542169, 'Follicular lymphoma'),
                          (42542162, 'Diffuse large B-cell lymphoma')):
            Concept.objects.get_or_create(
                concept_id=cid,
                defaults={
                    'concept_name': name, 'domain': domain, 'vocabulary': vocab,
                    'concept_class': cc, 'concept_code': str(cid),
                    'valid_start_date': today, 'valid_end_date': date(2099, 12, 31),
                },
            )
        _client = APIClient()
        _client.force_authenticate(user=cls.admin)
        bundle_bytes = json.dumps(_make_fl_bundle()).encode('utf-8')
        fhir_file = io.BytesIO(bundle_bytes)
        fhir_file.name = 'fl_bundle.json'
        cls._upload_response = _client.post(
            '/api/patient-info/upload_fhir/', {'file': fhir_file}, format='multipart'
        )
        cls._person = Person.objects.filter(family_name='Follic', given_name='Larry').first()
        assert cls._person is not None, 'Setup: FL person not found after upload'

    def test_upload_returns_success(self):
        self.assertIn(self._upload_response.status_code,
                      [status.HTTP_200_OK, status.HTTP_201_CREATED],
                      msg=f'Upload failed: {self._upload_response.data}')

    def test_fl_and_dlbcl_condition_occurrences_created(self):
        names = set(
            ConditionOccurrence.objects.filter(person=self._person)
            .values_list('condition_concept__concept_name', flat=True)
        )
        self.assertIn('Follicular lymphoma', names)
        self.assertIn('Diffuse large B-cell lymphoma', names)

    def test_patient_record_transformation_fields_derived(self):
        pi = PatientRecord.objects.get(person=self._person)
        self.assertTrue(pi.transformed_to_dlbcl)
        self.assertEqual(pi.dlbcl_transformation_date, date(2023, 4, 15))


class TransformationFieldValidationTest(FhirUploadBase):
    """Serializer validation for the FL → DLBCL transformation fields."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        _client = APIClient()
        _client.force_authenticate(user=cls.admin)
        bundle_bytes = json.dumps(_make_fhir_bundle()).encode('utf-8')
        fhir_file = io.BytesIO(bundle_bytes)
        fhir_file.name = 'test_bundle.json'
        _client.post('/api/patient-info/upload_fhir/', {'file': fhir_file}, format='multipart')
        cls._pi = PatientRecord.objects.get(person__family_name='Smith')

    def _patch(self, payload):
        return self.client.patch(
            f'/api/v1/patient-records/{self._pi.person_id}/', payload, format='json'
        )

    def test_valid_transformation_fields_accepted(self):
        response = self._patch({
            'transformed_to_dlbcl': True,
            'dlbcl_transformation_date': '2023-04-15',
            'post_transformation_outcome': 'Complete Response',
        })
        self.assertEqual(response.status_code, status.HTTP_200_OK,
                         msg=f'PATCH failed: {response.data}')
        self._pi.refresh_from_db()
        self.assertTrue(self._pi.transformed_to_dlbcl)
        self.assertEqual(self._pi.post_transformation_outcome, 'Complete Response')

    def test_future_transformation_date_rejected(self):
        response = self._patch({
            'transformed_to_dlbcl': True,
            'dlbcl_transformation_date': '2999-01-01',
        })
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_outcome_without_flag_rejected(self):
        response = self._patch({'post_transformation_outcome': 'Complete Response'})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_unrecognized_outcome_rejected(self):
        response = self._patch({
            'transformed_to_dlbcl': True,
            'post_transformation_outcome': 'Cured Forever',
        })
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_vocabulary_endpoint_serves_outcomes(self):
        response = self.client.get('/api/vocabularies/post-transformation-outcome/')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        titles = {row['title'] for row in response.data}
        self.assertIn('Complete Response', titles)
        self.assertIn('Deceased', titles)


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

    # -- PatientRecord endpoint --------------------------------------------------

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
                      'serum_creatinine_mg_dl', 'first_line_therapy', 'second_line_therapy',
                      'first_line_outcome', 'first_line_discontinuation_reason',
                      'first_line_start_date', 'first_line_end_date',
                      'second_line_outcome', 'second_line_discontinuation_reason',
                      'later_outcome', 'later_discontinuation_reason',
                      'treatment_refractory_status', 'relapse_count'):
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
# 4. Therapy component concept_ids (#189/#231)
# ---------------------------------------------------------------------------

_COMPONENT_ID_FIELDS = (
    'first_line_component_ids', 'second_line_component_ids',
    'later_component_ids', 'therapy_component_ids',
)


class TherapyComponentIdsAPITest(FhirUploadBase):
    """The component concept_id fields are exposed on both patient-record
    endpoints and are READ-ONLY via the API (derived read model, issue #236)."""

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

    def test_component_fields_in_legacy_patient_info(self):
        resp = self.client.get(f'/api/patient-info/{self._pid}/')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        for field in _COMPONENT_ID_FIELDS:
            self.assertIn(field, resp.data['patient_info'],
                          f'Field {field!r} missing from legacy patient-info response')

    def test_component_fields_in_v1_patient_records(self):
        resp = self.client.get(f'/api/v1/patient-records/{self._pid}/')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        for field in _COMPONENT_ID_FIELDS:
            self.assertIn(field, resp.data['patient_info'],
                          f'Field {field!r} missing from v1 patient-records response')

    def test_component_fields_read_only_via_patch(self):
        """Derived therapy-id fields are a read model (issue #236): a client
        PATCH must not change them — only the derivation pipeline
        (refresh_patient_record / FHIR upload) writes them."""
        record = PatientRecord.objects.get(person_id=self._pid)
        record.therapy_component_ids = [111]
        record.save(update_fields=['therapy_component_ids'])

        resp = self.client.patch(
            f'/api/v1/patient-records/{self._pid}/',
            {'therapy_component_ids': [35806260, 19103793]},
            format='json',
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        record.refresh_from_db()
        self.assertEqual(
            record.therapy_component_ids, [111],
            'Client PATCH wrote to derived read-model field therapy_component_ids',
        )

    def test_all_derived_therapy_id_fields_read_only_via_patch(self):
        """Every derived therapy-id field + provenance ignores client PATCHes."""
        derived = {
            'first_line_therapy_id': 35806260,
            'second_line_therapy_id': 35806261,
            'later_therapy_ids': [35806262],
            'first_line_component_ids': [1],
            'second_line_component_ids': [2],
            'later_component_ids': [3],
            'therapy_component_ids': [4],
            'therapy_ids_provenance': {'first_line_therapy_id': {'value': 1}},
        }
        record = PatientRecord.objects.get(person_id=self._pid)
        for field in derived:
            setattr(record, field, None)
        record.save(update_fields=list(derived))

        resp = self.client.patch(
            f'/api/v1/patient-records/{self._pid}/',
            derived,
            format='json',
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        record.refresh_from_db()
        for field in derived:
            current = getattr(record, field)
            self.assertIn(
                current, (None, [], {}),
                f'Client PATCH wrote to derived read-model field {field}',
            )


class FhirUploadComponentIdsTest(FhirUploadBase):
    """End-to-end: a MedicationStatement carrying a HemOnc coding yields
    component concept_ids expanded from the HemOnc regimen→component graph."""

    REGIMEN_ID = 35806260   # HemOnc RVD regimen
    COMP_A_ID = 35900001    # HemOnc drug component
    COMP_B_ID = 35900002    # HemOnc drug component
    RXNORM_ING_ID = 1900001  # RxNorm ingredient ('Maps to' target of COMP_A)

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls._make_hemonc_fixtures()

        bundle = _make_fhir_bundle()
        for entry in bundle['entry']:
            resource = entry['resource']
            if resource['resourceType'] == 'MedicationStatement' and resource['id'] == 'med-ac-t':
                resource['medicationCodeableConcept']['coding'] = [{
                    'system': 'http://ohdsi.org/omop/HemOnc',
                    'code': str(cls.REGIMEN_ID),
                    'display': 'RVD',
                }]

        _client = APIClient()
        _client.force_authenticate(user=cls.admin)
        bundle_bytes = json.dumps(bundle).encode('utf-8')
        fhir_file = io.BytesIO(bundle_bytes)
        fhir_file.name = 'test_bundle.json'
        cls._upload_response = _client.post(
            '/api/patient-info/upload_fhir/', {'file': fhir_file}, format='multipart'
        )
        cls._person = Person.objects.filter(family_name='Smith', given_name='Jane').first()
        assert cls._person is not None, 'Setup: person not found after upload'
        cls._pid = cls._person.person_id

    @classmethod
    def _make_hemonc_fixtures(cls):
        hemonc_vocab, _ = Vocabulary.objects.get_or_create(
            vocabulary_id='HemOnc',
            defaults={'vocabulary_name': 'HemOnc Oncology', 'vocabulary_concept_id': 0},
        )
        regimen_class, _ = ConceptClass.objects.get_or_create(
            concept_class_id='Regimen',
            defaults={'concept_class_name': 'Regimen', 'concept_class_concept_id': 0},
        )
        drug_domain = Domain.objects.get(domain_id='Drug')
        today = date.today()
        far_future = date(2099, 12, 31)

        def _concept(cid, name, cc):
            return Concept.objects.create(
                concept_id=cid, concept_name=name, concept_code=str(cid),
                vocabulary=hemonc_vocab, domain=drug_domain, concept_class=cc,
                standard_concept='S', valid_start_date=today, valid_end_date=far_future,
            )

        regimen = _concept(cls.REGIMEN_ID, 'RVD', regimen_class)
        comp_a = _concept(cls.COMP_A_ID, 'bortezomib', regimen_class)
        comp_b = _concept(cls.COMP_B_ID, 'lenalidomide', regimen_class)
        rx_ing = _concept(cls.RXNORM_ING_ID, 'bortezomib (ingredient)', regimen_class)

        for rel_id in ('Has targeted therapy', 'Has steroid tx', 'Maps to'):
            Relationship.objects.get_or_create(
                relationship_id=rel_id,
                defaults={
                    'relationship_name': rel_id, 'is_hierarchical': 0,
                    'defines_ancestry': 0, 'reverse_relationship_id': 'rev',
                    'relationship_concept_id': 0,
                },
            )
        for c1, c2, rel in (
            (regimen, comp_a, 'Has targeted therapy'),
            (regimen, comp_b, 'Has steroid tx'),
            (comp_a, rx_ing, 'Maps to'),
        ):
            ConceptRelationship.objects.create(
                concept_1=c1, concept_2=c2, relationship_id=rel,
                valid_start_date=today, valid_end_date=far_future,
            )

    def test_upload_succeeds(self):
        self.assertIn(self._upload_response.status_code,
                      [status.HTTP_200_OK, status.HTTP_201_CREATED],
                      msg=f'Upload failed: {self._upload_response.data}')

    def test_first_line_components_expanded_from_hemonc_graph(self):
        record = PatientRecord.objects.get(person_id=self._pid)
        components = set(record.first_line_component_ids or [])
        self.assertTrue(
            {self.COMP_A_ID, self.COMP_B_ID, self.RXNORM_ING_ID} <= components,
            f'Expected HemOnc components + leveled ingredient in '
            f'first_line_component_ids, got {components}',
        )

    def test_aggregate_contains_all_line_components(self):
        record = PatientRecord.objects.get(person_id=self._pid)
        aggregate = set(record.therapy_component_ids or [])
        self.assertTrue(
            {self.COMP_A_ID, self.COMP_B_ID, self.RXNORM_ING_ID} <= aggregate,
        )
        # aggregate covers every per-line id
        for line_field in ('first_line_component_ids', 'second_line_component_ids'):
            self.assertTrue(set(getattr(record, line_field) or []) <= aggregate)

    def test_component_fields_served_on_api(self):
        resp = self.client.get(f'/api/v1/patient-records/{self._pid}/')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        components = set(resp.data['patient_info']['first_line_component_ids'] or [])
        self.assertTrue({self.COMP_A_ID, self.COMP_B_ID} <= components)


# ---------------------------------------------------------------------------
# Issue #236 P0b — FHIR upload regimen namespace hygiene
# ---------------------------------------------------------------------------

class FhirUploadRegimenHygieneTest(FhirUploadBase):
    """Unmatched regimen names are quarantined under HK-Regimen (never minted
    under HemOnc), real HemOnc name matches win, and invalid inbound HemOnc
    concept_ids are rejected rather than persisted."""

    def setUp(self):
        super().setUp()
        from unittest.mock import patch
        from omop_core.services.concept_cache import concept_cache_clear
        concept_cache_clear()
        self.addCleanup(concept_cache_clear)
        # Keep the 'Kadcyla' plain-drug path deterministic (no network).
        rxnav_patch = patch(
            'omop_core.services.rxnav_service._rxnav_lookup',
            return_value=(None, None),
        )
        rxnav_patch.start()
        self.addCleanup(rxnav_patch.stop)

    def _upload(self, mutate=None):
        bundle = _make_fhir_bundle()
        if mutate:
            mutate(bundle)
        bundle_bytes = json.dumps(bundle).encode('utf-8')
        fhir_file = io.BytesIO(bundle_bytes)
        fhir_file.name = 'test_bundle.json'
        return self.client.post(
            '/api/patient-info/upload_fhir/', {'file': fhir_file}, format='multipart',
        )

    @staticmethod
    def _add_hemonc_coding(bundle, code):
        for entry in bundle['entry']:
            resource = entry['resource']
            if (resource['resourceType'] == 'MedicationStatement'
                    and resource['id'] == 'med-ac-t'):
                resource['medicationCodeableConcept']['coding'] = [{
                    'system': 'http://ohdsi.org/omop/HemOnc',
                    'code': str(code),
                    'display': 'AC-T',
                }]

    def _make_hemonc_regimen(self, cid, name, **kwargs):
        hemonc_vocab, _ = Vocabulary.objects.get_or_create(
            vocabulary_id='HemOnc',
            defaults={'vocabulary_name': 'HemOnc', 'vocabulary_concept_id': 0},
        )
        regimen_cc, _ = ConceptClass.objects.get_or_create(
            concept_class_id='Regimen',
            defaults={'concept_class_name': 'Regimen', 'concept_class_concept_id': 0},
        )
        defaults = dict(
            concept_name=name,
            domain=Domain.objects.get(domain_id='Drug'),
            vocabulary=hemonc_vocab,
            concept_class=regimen_cc,
            standard_concept='S',
            concept_code=str(cid),
            valid_start_date=date(1970, 1, 1),
            valid_end_date=date(2099, 12, 31),
        )
        defaults.update(kwargs)
        return Concept.objects.create(concept_id=cid, **defaults)

    def test_unmatched_regimen_quarantined_not_minted_under_hemonc(self):
        from omop_core.models import RegimenMappingGap
        resp = self._upload()
        self.assertIn(resp.status_code, [200, 201], msg=getattr(resp, 'data', ''))

        self.assertFalse(
            Concept.objects.filter(
                vocabulary_id='HemOnc', concept_code__startswith='FHIR-',
            ).exists(),
            'FHIR upload minted a fake HemOnc concept',
        )
        quarantine = Concept.objects.get(
            vocabulary_id='HK-Regimen', concept_code='hkr:ac-t',
        )
        self.assertIsNone(quarantine.standard_concept)
        self.assertEqual(quarantine.source, 'HealthKey')

        de = DrugExposure.objects.get(drug_source_value='AC-T')
        self.assertEqual(de.drug_concept_id, quarantine.concept_id)

        gap = RegimenMappingGap.objects.get(normalized_name='ac-t')
        self.assertEqual(gap.quarantine_concept_id, quarantine.concept_id)
        self.assertEqual(gap.status, RegimenMappingGap.STATUS_UNMATCHED)

    def test_real_hemonc_name_match_preferred_over_quarantine(self):
        from omop_core.models import RegimenMappingGap
        matched = self._make_hemonc_regimen(9760001, 'AC-T')
        resp = self._upload()
        self.assertIn(resp.status_code, [200, 201])

        de = DrugExposure.objects.get(drug_source_value='AC-T')
        self.assertEqual(de.drug_concept_id, matched.concept_id)
        self.assertFalse(Concept.objects.filter(
            vocabulary_id='HK-Regimen', concept_code='hkr:ac-t',
        ).exists())
        self.assertFalse(RegimenMappingGap.objects.filter(
            normalized_name='ac-t',
        ).exists())

    def test_valid_inbound_hemonc_concept_id_accepted(self):
        matched = self._make_hemonc_regimen(9760002, 'Doxorubicin-Cyclophosphamide followed by Paclitaxel')
        resp = self._upload(lambda b: self._add_hemonc_coding(b, matched.concept_id))
        self.assertIn(resp.status_code, [200, 201])
        de = DrugExposure.objects.get(drug_source_value='AC-T')
        self.assertEqual(de.drug_concept_id, matched.concept_id)

    def test_deprecated_inbound_concept_id_rejected(self):
        from omop_core.models import RegimenMappingGap
        stale = self._make_hemonc_regimen(9760003, 'AC-T Deprecated', invalid_reason='D')
        resp = self._upload(lambda b: self._add_hemonc_coding(b, stale.concept_id))
        self.assertIn(resp.status_code, [200, 201])
        de = DrugExposure.objects.get(drug_source_value='AC-T')
        self.assertNotEqual(
            de.drug_concept_id, stale.concept_id,
            'Deprecated inbound HemOnc concept_id was persisted',
        )
        # Falls through the ladder: no valid 'AC-T' regimen exists → quarantined.
        self.assertTrue(RegimenMappingGap.objects.filter(normalized_name='ac-t').exists())

    def test_foreign_vocabulary_concept_id_rejected(self):
        rxnorm_vocab, _ = Vocabulary.objects.get_or_create(
            vocabulary_id='RxNorm',
            defaults={'vocabulary_name': 'RxNorm', 'vocabulary_concept_id': 0},
        )
        ingredient_cc, _ = ConceptClass.objects.get_or_create(
            concept_class_id='Ingredient',
            defaults={'concept_class_name': 'Ingredient', 'concept_class_concept_id': 0},
        )
        foreign = Concept.objects.create(
            concept_id=9760004, concept_name='Not A HemOnc Regimen',
            domain=Domain.objects.get(domain_id='Drug'),
            vocabulary=rxnorm_vocab, concept_class=ingredient_cc,
            standard_concept='S', concept_code='9760004',
            valid_start_date=date(1970, 1, 1), valid_end_date=date(2099, 12, 31),
        )
        resp = self._upload(lambda b: self._add_hemonc_coding(b, foreign.concept_id))
        self.assertIn(resp.status_code, [200, 201])
        de = DrugExposure.objects.get(drug_source_value='AC-T')
        self.assertNotEqual(
            de.drug_concept_id, foreign.concept_id,
            'Foreign-vocabulary concept_id supplied as HemOnc coding was persisted',
        )

    # -- Review-fix coverage (issue #236 follow-ups) -------------------------

    @staticmethod
    def _add_medication_request(bundle, codeable):
        bundle['entry'].append({'resource': {
            'resourceType': 'MedicationRequest',
            'id': 'medreq-extra-1',
            'status': 'active',
            'subject': {'reference': 'Patient/test-patient-jane-001'},
            'medicationCodeableConcept': codeable,
            'authoredOn': '2022-05-01',
        }})

    def test_supplemental_rxnav_miss_quarantined_not_crash(self):
        """RxNav returning nothing for a supplemental MedicationRequest must
        not crash the upload (drug_concept is NOT NULL) — the name is
        quarantined under HK-Drug instead."""
        def mutate(bundle):
            self._add_medication_request(bundle, {
                'coding': [{
                    'system': 'http://www.nlm.nih.gov/research/umls/rxnorm',
                    'code': '99999999',
                    'display': 'Unobtainium Drug',
                }],
                'text': 'Unobtainium Drug',
            })
        resp = self._upload(mutate)
        self.assertIn(resp.status_code, [200, 201], msg=getattr(resp, 'data', ''))

        de = DrugExposure.objects.get(drug_source_value='99999999')
        self.assertEqual(de.drug_concept.vocabulary_id, 'HK-Drug')
        self.assertIsNone(de.drug_concept.standard_concept)
        self.assertFalse(
            Concept.objects.filter(
                vocabulary_id='RxNorm', concept_code='99999999',
            ).exists(),
            'Unmatched supplemental drug was minted under RxNorm',
        )

    def test_empty_codeable_medication_skipped_no_crash(self):
        """A MedicationRequest with no code and no display has nothing to
        resolve or quarantine — it is skipped, not written with a NULL
        drug_concept."""
        def mutate(bundle):
            self._add_medication_request(bundle, {})
        resp = self._upload(mutate)
        self.assertIn(resp.status_code, [200, 201], msg=getattr(resp, 'data', ''))
        self.assertFalse(
            DrugExposure.objects.filter(drug_source_value='FHIR medication').exists(),
            'Empty codeable produced a junk DrugExposure',
        )

    def test_diagnostic_report_known_snomed_code_used(self):
        """DiagnosticReport codes that exist in their licensed vocabulary are
        used directly — only genuinely unmapped codes go to quarantine."""
        from omop_core.models import Observation
        snomed_vocab, _ = Vocabulary.objects.get_or_create(
            vocabulary_id='SNOMED',
            defaults={'vocabulary_name': 'SNOMED', 'vocabulary_concept_id': 0},
        )
        obs_domain, _ = Domain.objects.get_or_create(
            domain_id='Observation',
            defaults={'domain_name': 'Observation', 'domain_concept_id': 0},
        )
        report_cc, _ = ConceptClass.objects.get_or_create(
            concept_class_id='Clinical Observation',
            defaults={'concept_class_name': 'Clinical Observation', 'concept_class_concept_id': 0},
        )
        known = Concept.objects.create(
            concept_id=9760020, concept_name='Known Report',
            domain=obs_domain,
            vocabulary=snomed_vocab, concept_class=report_cc,
            standard_concept='S', concept_code='12340000',
            valid_start_date=date(1970, 1, 1), valid_end_date=date(2099, 12, 31),
        )

        def mutate(bundle):
            bundle['entry'].append({'resource': {
                'resourceType': 'DiagnosticReport',
                'id': 'dr-known-1',
                'status': 'final',
                'subject': {'reference': 'Patient/test-patient-jane-001'},
                'effectiveDateTime': '2022-04-01',
                'code': {'coding': [{
                    'system': 'http://snomed.info/sct',
                    'code': '12340000',
                    'display': 'Known Report',
                }]},
                'conclusion': 'All fine',
            }})
        resp = self._upload(mutate)
        self.assertIn(resp.status_code, [200, 201], msg=getattr(resp, 'data', ''))
        self.assertTrue(
            Observation.objects.filter(observation_concept_id=known.concept_id).exists(),
            'Known SNOMED report code was not used as the observation concept',
        )

    def test_diagnostic_report_unknown_code_quarantined(self):
        from omop_core.models import Observation

        def mutate(bundle):
            bundle['entry'].append({'resource': {
                'resourceType': 'DiagnosticReport',
                'id': 'dr-unknown-1',
                'status': 'final',
                'subject': {'reference': 'Patient/test-patient-jane-001'},
                'effectiveDateTime': '2022-04-02',
                'code': {'coding': [{
                    'system': 'http://loinc.org',
                    'code': '99999-9',
                    'display': 'Mystery Panel',
                }]},
                'conclusion': 'Unclear',
            }})
        resp = self._upload(mutate)
        self.assertIn(resp.status_code, [200, 201], msg=getattr(resp, 'data', ''))

        quarantine = Concept.objects.get(
            vocabulary_id='HK-Observation', concept_code='hko:loinc-99999-9',
        )
        self.assertIsNone(quarantine.standard_concept)
        self.assertEqual(quarantine.source, 'HealthKey')
        self.assertFalse(
            Concept.objects.filter(
                vocabulary_id='LOINC', concept_code='99999-9',
            ).exists(),
            'Unmatched DiagnosticReport code was minted under LOINC',
        )
        self.assertTrue(
            Observation.objects.filter(observation_concept_id=quarantine.concept_id).exists(),
        )

    def test_procedure_unknown_snomed_quarantined_under_hk_procedure(self):
        """The default bundle's breast-biopsy procedure (SNOMED 387713003, not
        loaded locally) must quarantine under HK-Procedure — never mint under
        SNOMED, never fall back to an arbitrary Procedure concept."""
        from omop_core.models import ProcedureOccurrence, RegimenMappingGap
        resp = self._upload()
        self.assertIn(resp.status_code, [200, 201], msg=getattr(resp, 'data', ''))

        self.assertFalse(
            Concept.objects.filter(
                vocabulary_id='SNOMED', concept_code='387713003',
            ).exists(),
            'FHIR upload minted a concept under SNOMED',
        )
        quarantine = Concept.objects.get(
            vocabulary_id='HK-Procedure', concept_code='hkp:snomed-387713003',
        )
        self.assertIsNone(quarantine.standard_concept)
        self.assertEqual(quarantine.source, 'HealthKey')
        proc = ProcedureOccurrence.objects.get(procedure_source_value='387713003')
        self.assertEqual(proc.procedure_concept_id, quarantine.concept_id)
        self.assertTrue(RegimenMappingGap.objects.filter(
            normalized_name='surgical biopsy of breast',
        ).exists())

    def test_episode_source_concept_only_for_validated_inbound_id(self):
        """episode_source_concept is set only when the regimen came from a
        validated inbound HemOnc concept_id — not for name matches or
        quarantine rows."""
        matched = self._make_hemonc_regimen(9760005, 'AC-T Validated Inbound')
        resp = self._upload(lambda b: self._add_hemonc_coding(b, matched.concept_id))
        self.assertIn(resp.status_code, [200, 201])
        episode = Episode.objects.filter(episode_number=1).first()
        self.assertIsNotNone(episode, 'LOT-1 episode not written')
        self.assertEqual(episode.episode_source_concept_id, matched.concept_id)

    def test_episode_source_concept_not_set_for_quarantine(self):
        resp = self._upload()
        self.assertIn(resp.status_code, [200, 201])
        episode = Episode.objects.filter(episode_number=1).first()
        self.assertIsNotNone(episode, 'LOT-1 episode not written')
        self.assertIsNone(
            episode.episode_source_concept_id,
            'Quarantined regimen must not be stamped as the episode source concept',
        )

    def test_mixed_case_regimen_acronym_quarantined_as_regimen(self):
        """Short mixed-case acronyms (VRd, KRd) are regimen names, not generic
        drugs — they quarantine under HK-Regimen, not HK-Drug."""
        def mutate(bundle):
            for entry in bundle['entry']:
                resource = entry['resource']
                if (resource['resourceType'] == 'MedicationStatement'
                        and resource['id'] == 'med-ac-t'):
                    resource['medicationCodeableConcept']['text'] = 'VRd'
        resp = self._upload(mutate)
        self.assertIn(resp.status_code, [200, 201], msg=getattr(resp, 'data', ''))
        quarantine = Concept.objects.get(
            vocabulary_id='HK-Regimen', concept_code='hkr:vrd',
        )
        de = DrugExposure.objects.get(drug_source_value='VRd')
        self.assertEqual(de.drug_concept_id, quarantine.concept_id)


# ---------------------------------------------------------------------------
# 5. Direct OMOP endpoint CRUD tests
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
# 6. OMOP → PatientRecord signal tests
#
# Each test writes directly to an OMOP table via the ORM (no API, no FHIR
# upload) and asserts that the post_save / post_delete signal automatically
# refreshes the PatientRecord row with the correct derived value.
# ---------------------------------------------------------------------------

class _SignalBase(TestCase):
    """Shared fixtures for signal tests.

    Uses setUpTestData (runs once per class, rolled back after the class) for
    vocab, concepts, and the test Person — matching the pattern used by
    FhirUploadBase so the remote Render DB isn't hammered with per-test creates.

    Django's TestCase wraps each individual test method in a savepoint that is
    rolled back after the test, so OMOP records and PatientRecord rows created
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
        # Each test's OMOP writes and PatientRecord rows are rolled back by TestCase.
        cls.person = Person.objects.create(
            person_id=cls.PERSON_ID,
            year_of_birth=1970,
            gender_source_value='female',
            race_source_value='unknown',
            ethnicity_source_value='unknown',
        )

    def _get_pi(self):
        return PatientRecord.objects.filter(person=self.person).first()


class ConditionToPatientRecordTest(_SignalBase):
    """ConditionOccurrence saves/deletes update PatientRecord.disease,
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
        self.assertIsNotNone(pi, 'PatientRecord not created after ConditionOccurrence save')
        self.assertEqual(pi.disease, 'Breast Cancer')

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

        self.assertEqual(self._get_pi().disease, 'Breast Cancer')

    def test_delete_cancer_condition_clears_disease(self):
        cond = ConditionOccurrence.objects.create(
            condition_occurrence_id=90101,
            person=self.person,
            condition_concept=self.cancer_concept,
            condition_start_date=date(2022, 1, 1),
            condition_type_concept=self.type_concept,
        )
        self.assertEqual(self._get_pi().disease, 'Breast Cancer')

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


class DrugExposureToPatientRecordTest(_SignalBase):
    """DrugExposure saves/deletes update PatientRecord therapy line fields."""

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
        # PatientRecord.save() sets prior_therapy to controlled vocabulary based
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


class MeasurementToPatientRecordTest(_SignalBase):
    """Measurement saves update PatientRecord lab value fields."""

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


class ObservationToPatientRecordTest(_SignalBase):
    """Observation saves update PatientRecord performance status fields."""

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


class ProcedureToPatientRecordTest(_SignalBase):
    """ProcedureOccurrence saves/deletes update PatientRecord.prior_procedures."""

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
#      that PatientRecord is automatically refreshed from the written data
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
        # PatientRecord for cls.person, scoped to the test org.  Subclasses that
        # create OMOP records for cls.person (conditions, measurements, etc.)
        # need this to exist so _ProvenanceMixin.perform_create org-check passes.
        cls.patient_info = PatientRecord.objects.create(
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
    and verifies PatientRecord is automatically refreshed."""

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
        """Writing a ConditionOccurrence via OAuth must update PatientRecord.disease."""
        PatientRecord.objects.filter(person=self.person).delete()
        payload = {
            'condition_occurrence_id': 70901,
            'person': self.person.person_id,
            'condition_concept': self.condition_concept.concept_id,
            'condition_start_date': '2024-10-01',
            'condition_type_concept': self.type_concept.concept_id,
            'condition_source_value': 'Breast cancer',
        }
        self.write_client.post('/api/conditions/', payload, format='json')
        pi = PatientRecord.objects.filter(person=self.person).first()
        self.assertIsNotNone(pi, 'PatientRecord not created after condition POST')
        self.assertIsNotNone(pi.disease, 'PatientRecord.disease not populated after condition write')

    def test_drug_exposure_write_triggers_patient_info_refresh(self):
        """Writing a DrugExposure via OAuth must update PatientRecord therapy data."""
        PatientRecord.objects.filter(person=self.person).delete()
        payload = {
            'drug_exposure_id': 71001,
            'person': self.person.person_id,
            'drug_concept': self.drug_concept.concept_id,
            'drug_exposure_start_date': '2024-11-01',
            'drug_type_concept': self.type_concept.concept_id,
            'drug_source_value': 'Capecitabine',
        }
        self.write_client.post('/api/drug-exposures/', payload, format='json')
        pi = PatientRecord.objects.filter(person=self.person).first()
        self.assertIsNotNone(pi, 'PatientRecord not created after drug exposure POST')

    def test_delete_condition_triggers_patient_info_refresh(self):
        """Deleting a ConditionOccurrence via OAuth must re-derive PatientRecord."""
        cond = ConditionOccurrence.objects.create(
            condition_occurrence_id=71101,
            person=self.person,
            condition_concept=self.condition_concept,
            condition_start_date=date(2024, 12, 1),
            condition_type_concept=self.type_concept,
            condition_source_value='Temporary staging condition',
        )
        # Verify PatientRecord exists before deletion
        from omop_core.services.patient_record_service import refresh_patient_record
        refresh_patient_record(self.person)
        self.assertTrue(PatientRecord.objects.filter(person=self.person).exists())

        self.write_client.delete(f'/api/conditions/{cond.condition_occurrence_id}/')
        # PatientRecord must still exist and be updated (not deleted)
        self.assertTrue(
            PatientRecord.objects.filter(person=self.person).exists(),
            'PatientRecord should persist after a condition is deleted',
        )

    def test_measurement_write_triggers_patient_info_refresh(self):
        """Writing a Measurement via OAuth must update the corresponding PatientRecord lab field."""
        PatientRecord.objects.filter(person=self.person).delete()
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
        pi = PatientRecord.objects.filter(person=self.person).first()
        self.assertIsNotNone(pi, 'PatientRecord not created after measurement POST')
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
        PatientRecord.objects.create(person=person_b, organization=org_b)

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


class SmartPatientRecordReadOnlyTest(_SmartBase):
    """PatientRecord endpoints are read-only regardless of the OAuth scope."""

    def test_patient_info_put_returns_405(self):
        pi = PatientRecord.objects.filter(person=self.person).first()
        if pi is None:
            from omop_core.services.patient_record_service import refresh_patient_record
            pi = refresh_patient_record(self.person)
        resp = self.write_client.put(
            f'/api/patient-info/{self.person.person_id}/',
            {'disease': 'Should not be written directly'},
            format='json',
        )
        self.assertEqual(resp.status_code, status.HTTP_405_METHOD_NOT_ALLOWED)

    def test_patient_info_patch_succeeds_with_write_token(self):
        """PATCH is now supported — write-through to OMOP was added in HKI-PDS-01."""
        PatientRecord.objects.get_or_create(person=self.person, defaults={'organization': self.organization})
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
        pi = PatientRecord.objects.filter(person=person).first()
        self.assertIsNotNone(pi, 'PatientRecord not derived after FHIR upload via OAuth')
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
        from omop_core.models import Person, Measurement, ConditionOccurrence, ProcedureOccurrence

        resp1 = self._upload('bundle_upsert_1.json')
        self.assertIn(resp1.status_code, [200, 201])
        self.assertEqual(resp1.json()['created_count'], 1)

        person_count_after_first = Person.objects.count()
        measurement_count_after_first = Measurement.objects.count()
        condition_count_after_first = ConditionOccurrence.objects.count()
        procedure_count_after_first = ProcedureOccurrence.objects.count()

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
        self.assertEqual(ProcedureOccurrence.objects.count(), procedure_count_after_first)

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
        pi = PatientRecord.objects.get(pk=pt['patient_info_id'])
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
        cls.patient_a = PatientRecord.objects.create(
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
        cls.patient_b = PatientRecord.objects.create(
            person=cls.person_b,
            organization=cls.org_b,
            disease='Lung Cancer',
        )

    def _client(self, token_str):
        c = APIClient()
        c.credentials(HTTP_AUTHORIZATION=f'Bearer {token_str}')
        return c

    def test_org_a_token_sees_only_org_a_patient_info(self):
        """Org A token must not return Org B's PatientRecord records."""
        resp = self._client(self.token_a.token).get('/api/patient-info/')
        self.assertEqual(resp.status_code, 200)
        ids = [p['id'] for p in resp.json()]
        self.assertIn(self.patient_a.id, ids)
        self.assertNotIn(self.patient_b.id, ids)

    def test_org_b_token_sees_only_org_b_patient_info(self):
        """Org B token must not return Org A's PatientRecord records."""
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
# PatientRecord PATCH write-through tests (HKI-PDS-01 / issue #59)
# ---------------------------------------------------------------------------

class PatientRecordPatchWriteThroughTest(_SmartBase):
    """PATCH /api/patient-info/{person_id}/ must update PatientRecord AND create a Measurement."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        # cls.patient_info already created by _SmartBase; just set disease.
        PatientRecord.objects.filter(person=cls.person).update(disease='Breast Cancer')
        cls.patient_info = PatientRecord.objects.get(person=cls.person)

    def test_patch_updates_patient_info(self):
        """PATCH updates the PatientRecord field value."""
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
# Account-holder data management (issue #307 — PHR-S FM PH.1.1/PH.1.2/PH.1.4/TI.1.2)
# ---------------------------------------------------------------------------

class AccountHolderDataTest(_SmartBase):
    """Issue #307: advance-directive effective status, entered-in-error,
    revision history, and consent-driven demographic redaction."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        PatientRecord.objects.filter(person=cls.person).update(
            disease='Breast Cancer', date_of_birth=date(1980, 5, 1), city='Boston',
        )
        cls.patient_info = PatientRecord.objects.get(person=cls.person)

    # --- PH.1.4#04 : advance-directive effective status -------------------

    def test_advance_directive_status_and_effective_date(self):
        """AD document exposes status + effective_date, both settable and filterable."""
        doc = PatientDocument.objects.create(
            person=self.person, doc_type='ADVANCE_DIRECTIVE', title='Living Will',
        )
        # Default status is 'active', effective_date distinct from uploaded_at.
        self.assertEqual(doc.status, PatientDocument.STATUS_ACTIVE)
        self.assertIsNone(doc.effective_date)

        # Both fields are exposed and writable via the documents viewset.
        resp = self.write_client.patch(
            f'/api/v1/documents/{doc.id}/',
            {'status': 'revoked', 'effective_date': '2026-01-15'},
            format='json',
        )
        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertEqual(resp.data['status'], 'revoked')
        self.assertEqual(resp.data['effective_date'], '2026-01-15')
        doc.refresh_from_db()
        self.assertEqual(doc.status, 'revoked')
        self.assertEqual(str(doc.effective_date), '2026-01-15')

    def test_document_status_filter(self):
        """?status= filters documents by effective status."""
        PatientDocument.objects.create(
            person=self.person, doc_type='ADVANCE_DIRECTIVE', title='Active AD',
            status='active',
        )
        PatientDocument.objects.create(
            person=self.person, doc_type='ADVANCE_DIRECTIVE', title='Old AD',
            status='superseded',
        )
        resp = self.read_client.get(
            '/api/v1/documents/', {'person_id': self.person.person_id, 'status': 'active'},
        )
        self.assertEqual(resp.status_code, 200)
        titles = [d['title'] for d in resp.data]
        self.assertIn('Active AD', titles)
        self.assertNotIn('Old AD', titles)

    # --- PH.1.1#06 : entered-in-error ------------------------------------

    def _make_condition(self, cid=70901):
        return ConditionOccurrence.objects.create(
            condition_occurrence_id=cid,
            person=self.person,
            condition_concept=self.condition_concept,
            condition_start_date=date(2024, 1, 1),
            condition_type_concept=self.type_concept,
            condition_source_value='Test condition',
        )

    def test_mark_condition_erroneous_retains_but_excludes(self):
        """A row marked entered-in-error is retained in the DB but excluded from normal reads."""
        cond = self._make_condition()
        # Visible before marking.
        resp = self.read_client.get('/api/v1/conditions/', {'person_id': self.person.person_id})
        self.assertIn(cond.pk, [r['condition_occurrence_id'] for r in resp.data])

        # Mark it erroneous via the existing viewset (does NOT delete).
        resp = self.write_client.patch(
            f'/api/v1/conditions/{cond.pk}/',
            {'is_erroneous': True, 'erroneous_reason': 'duplicate entry'},
            format='json',
        )
        self.assertEqual(resp.status_code, 200, resp.data)

        # Retained in the DB.
        cond.refresh_from_db()
        self.assertTrue(cond.is_erroneous)
        self.assertEqual(cond.erroneous_reason, 'duplicate entry')
        self.assertTrue(ConditionOccurrence.objects.filter(pk=cond.pk).exists())

        # Excluded from normal reads by default.
        resp = self.read_client.get('/api/v1/conditions/', {'person_id': self.person.person_id})
        self.assertNotIn(cond.pk, [r['condition_occurrence_id'] for r in resp.data])

        # Surfaced with ?include_erroneous=true.
        resp = self.read_client.get(
            '/api/v1/conditions/',
            {'person_id': self.person.person_id, 'include_erroneous': 'true'},
        )
        self.assertIn(cond.pk, [r['condition_occurrence_id'] for r in resp.data])

    def test_erroneous_flag_defaults_false_and_shows_existing_data(self):
        """New/existing rows default is_erroneous=False and remain visible."""
        cond = self._make_condition(cid=70902)
        self.assertFalse(cond.is_erroneous)
        resp = self.read_client.get('/api/v1/conditions/', {'person_id': self.person.person_id})
        self.assertIn(cond.pk, [r['condition_occurrence_id'] for r in resp.data])

    # --- TI.1.2#04 : revision history ------------------------------------

    def test_patient_record_update_writes_revision(self):
        """PATCHing a PatientRecord field writes a RecordRevision with old/new values."""
        RecordRevision.objects.filter(patient_record=self.patient_info).delete()
        resp = self.write_client.patch(
            f'/api/v1/patient-records/{self.person.person_id}/',
            {'disease': 'Lung Cancer'},
            format='json',
        )
        self.assertEqual(resp.status_code, 200, resp.data)
        rev = RecordRevision.objects.filter(
            patient_record=self.patient_info, field='disease',
        ).first()
        self.assertIsNotNone(rev)
        self.assertEqual(rev.old_value, 'Breast Cancer')
        self.assertEqual(rev.new_value, 'Lung Cancer')

    def test_revision_not_written_when_value_unchanged(self):
        """No revision row is created when the submitted value equals the stored value."""
        RecordRevision.objects.filter(patient_record=self.patient_info).delete()
        self.write_client.patch(
            f'/api/v1/patient-records/{self.person.person_id}/',
            {'disease': self.patient_info.disease},
            format='json',
        )
        self.assertEqual(
            RecordRevision.objects.filter(patient_record=self.patient_info, field='disease').count(),
            0,
        )

    def test_revisions_endpoint_returns_history(self):
        """GET .../revisions/ returns the field-level change history."""
        RecordRevision.objects.filter(patient_record=self.patient_info).delete()
        self.write_client.patch(
            f'/api/v1/patient-records/{self.person.person_id}/',
            {'stage': 'IV'},
            format='json',
        )
        resp = self.read_client.get(
            f'/api/v1/patient-records/{self.person.person_id}/revisions/'
        )
        self.assertEqual(resp.status_code, 200)
        fields = [r['field'] for r in resp.data]
        self.assertIn('stage', fields)
        entry = next(r for r in resp.data if r['field'] == 'stage')
        self.assertEqual(entry['new_value'], 'IV')

    def test_patch_write_through_still_succeeds_with_revision_logging(self):
        """Revision logging must not break the OMOP write-through (lab PATCH still creates Measurement)."""
        resp = self.write_client.patch(
            f'/api/v1/patient-records/{self.person.person_id}/',
            {'hemoglobin_g_dl': '12.5'},
            format='json',
        )
        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertTrue(
            Measurement.objects.filter(person=self.person, measurement_source_value='718-7').exists()
        )

    # --- PH.1.2#05 : consent-driven demographic redaction ----------------

    def test_demographics_redacted_for_non_owner_when_opted_in(self):
        """With the preference set, a non-owner reader gets DOB/location redacted."""
        PatientRecord.objects.filter(person=self.person).update(
            suppress_demographics_for_others=True,
        )
        resp = self.read_client.get(f'/api/v1/patient-records/{self.person.person_id}/')
        self.assertEqual(resp.status_code, 200)
        info = resp.data['patient_info']
        self.assertIsNone(info['date_of_birth'])
        self.assertIsNone(info['city'])
        self.assertTrue(info.get('demographics_redacted'))

    def test_demographics_not_redacted_when_preference_off(self):
        """Default (preference off) returns demographics in full."""
        PatientRecord.objects.filter(person=self.person).update(
            suppress_demographics_for_others=False,
        )
        resp = self.read_client.get(f'/api/v1/patient-records/{self.person.person_id}/')
        self.assertEqual(resp.status_code, 200)
        info = resp.data['patient_info']
        self.assertEqual(info['date_of_birth'], '1980-05-01')
        self.assertEqual(info['city'], 'Boston')

    def test_demographics_visible_to_account_holder_despite_preference(self):
        """The account holder always sees their own demographics, even with the preference set."""
        from rest_framework.test import APIRequestFactory
        from patient_portal.api.serializers import PatientRecordSerializer
        from patient_portal.models import PatientUser

        PatientRecord.objects.filter(person=self.person).update(
            suppress_demographics_for_others=True,
        )
        pr = PatientRecord.objects.get(person=self.person)

        owner_identity = Identity.objects.create_user(
            email='owner307@test.com', password='pw',
        )
        PatientUser.objects.create(identity=owner_identity, person=self.person)

        factory = APIRequestFactory()
        request = factory.get('/')
        request.user = owner_identity
        data = PatientRecordSerializer(pr, context={'request': request}).data
        self.assertEqual(data['date_of_birth'], '1980-05-01')
        self.assertEqual(data['city'], 'Boston')
        self.assertNotIn('demographics_redacted', data)


# ---------------------------------------------------------------------------
# Provenance tests (HKI-PDS-01 / issues #57 + #61)
# ---------------------------------------------------------------------------

class ProvenancePatchTest(_SmartBase):
    """PATCH with provenance headers creates ProvenanceRecord entries."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        # cls.patient_info already created by _SmartBase; just set disease.
        PatientRecord.objects.filter(person=cls.person).update(disease='Breast Cancer')
        cls.patient_info = PatientRecord.objects.get(person=cls.person)

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
        pi = PatientRecord.objects.get(person=person)
        self.assertTrue(
            ProvenanceRecord.objects.filter(object_id=pi.pk).exists(),
            'PatientRecord was not tagged with provenance',
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
    """Audit log middleware emits a JSON stdout line for every audited request
    (reads as record_view, writes classified by method)."""

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
        pi = PatientRecord.objects.create(person=person, organization=self.organization)
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
        self.assertEqual(entry['event'], 'record_update')
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

    def test_get_emits_record_view_audit_log(self):
        """GET is now audited as a record_view (TI.2 covers reads)."""
        _, pi = self._make_person_and_pi(88802)

        logs = self._capture_audit_logs(
            self.read_client.get,
            f'/api/patient-info/{pi.pk}/',
        )

        self.assertEqual(len(logs), 1, f'Expected one record_view log for GET: {logs}')
        self.assertEqual(logs[0]['event'], 'record_view')
        self.assertEqual(logs[0]['method'], 'GET')

    def test_list_get_emits_record_view_audit_log(self):
        """GET list endpoint is audited as a single record_view entry."""
        logs = self._capture_audit_logs(
            self.read_client.get,
            '/api/patient-info/',
        )
        self.assertEqual(len(logs), 1)
        self.assertEqual(logs[0]['event'], 'record_view')

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


class PatientRecordOmopSyncTest(_SmartBase):
    """PatientRecord PATCH → OMOP write-through via omop_write_service."""

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
        pi = PatientRecord.objects.create(person=person, organization=self.organization)
        before = Measurement.objects.filter(person=person).count()

        self._patch(pi, {'hemoglobin_g_dl': 12.5})

        self.assertEqual(Measurement.objects.filter(person=person).count(), before + 1)
        m = Measurement.objects.filter(person=person).latest('measurement_id')
        self.assertEqual(float(m.value_as_number), 12.5)

    def test_patch_lab_same_day_updates_not_duplicates(self):
        """Two PATCHes of the same lab on the same day → still 1 Measurement row."""
        from omop_core.models import Measurement
        person = Person.objects.create(person_id=91002)
        pi = PatientRecord.objects.create(person=person, organization=self.organization)

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
        pi = PatientRecord.objects.create(person=person, organization=self.organization)

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
        pi = PatientRecord.objects.create(person=person, organization=self.organization)

        self._patch(pi, {'disease': 'Breast Cancer'})

        self.assertEqual(
            ConditionOccurrence.objects.filter(person=person).count(), 1
        )
        co = ConditionOccurrence.objects.get(person=person)
        self.assertEqual(co.condition_source_value, 'Breast Cancer')

    def test_patch_stage_appends_condition_occurrence(self):
        """Two PATCHes of 'stage' create two separate ConditionOccurrence rows."""
        from omop_core.models import ConditionOccurrence
        from unittest.mock import patch as mock_patch
        from datetime import date
        person = Person.objects.create(person_id=91011)
        pi = PatientRecord.objects.create(person=person, organization=self.organization)

        with mock_patch('omop_core.services.omop_write_service._today', return_value=date(2024, 1, 1)):
            self._patch(pi, {'stage': 'Stage II'})
        with mock_patch('omop_core.services.omop_write_service._today', return_value=date(2024, 3, 1)):
            self._patch(pi, {'stage': 'Stage III'})

        self.assertEqual(ConditionOccurrence.objects.filter(person=person).count(), 2)

    def test_patch_demographics_updates_person(self):
        """PATCHing gender and date_of_birth updates the linked Person record."""
        person = Person.objects.create(person_id=91020)
        pi = PatientRecord.objects.create(person=person, organization=self.organization)

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
        pi = PatientRecord.objects.create(person=person, organization=self.organization)

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

    def test_patch_therapy_outcome_writes_lot_outcome_observation(self):
        """PATCHing a line's outcome persists a LOT-{n}-outcome Observation to OMOP."""
        from omop_core.models import Observation
        person = Person.objects.create(person_id=91035)
        pi = PatientRecord.objects.create(person=person, organization=self.organization)

        self._patch(pi, {
            'first_line_therapy': 'AC-T',
            'first_line_start_date': '2023-01-15',
            'first_line_end_date': '2023-07-01',
            'first_line_outcome': 'Partial Response',
        })

        obs = Observation.objects.filter(person=person, observation_source_value='LOT-1-outcome')
        self.assertEqual(obs.count(), 1)
        self.assertEqual(obs.first().value_as_string, 'Partial Response')

    def test_patch_therapy_outcome_edit_updates_observation_in_place(self):
        """Editing a line's outcome updates the existing Observation, no duplicate."""
        from omop_core.models import Observation
        person = Person.objects.create(person_id=91036)
        pi = PatientRecord.objects.create(person=person, organization=self.organization)

        self._patch(pi, {
            'first_line_therapy': 'AC-T',
            'first_line_start_date': '2023-01-15',
            'first_line_outcome': 'Partial Response',
        })
        self._patch(pi, {'first_line_outcome': 'Complete Response'})

        obs = Observation.objects.filter(person=person, observation_source_value='LOT-1-outcome')
        self.assertEqual(obs.count(), 1)
        self.assertEqual(obs.first().value_as_string, 'Complete Response')

    def test_patch_therapy_links_existing_drug_exposures(self):
        """DrugExposure rows in the episode date range are linked via EpisodeEvent."""
        from omop_oncology.models import Episode, EpisodeEvent
        from omop_core.models import DrugExposure, Concept
        person = Person.objects.create(person_id=91031)
        pi = PatientRecord.objects.create(person=person, organization=self.organization)
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
        pi = PatientRecord.objects.create(person=person, organization=self.organization)
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
        pi = PatientRecord.objects.create(person=person, organization=self.organization)
        original_status = pi.ecog_performance_status

        with mock_patch(
            'patient_portal.api.views.sync_to_omop',
            side_effect=RuntimeError('simulated DB failure'),
        ):
            response = self._patch(pi, {'ecog_performance_status': 1})

        self.assertEqual(response.status_code, 500)
        # PatientRecord must not have been updated — transaction was rolled back.
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

    def test_load_records_version_history_append_only(self):
        """The loader appends version-history rows on each load, never truncating (#305).

        (--replace itself TRUNCATEs vocab tables, which Postgres refuses inside the
        atomic test transaction; the append-only trail is what we assert here — two
        loads accumulate rows rather than overwriting.)
        """
        from omop_core.models import VocabularyVersionHistory
        from django.core.management import call_command
        with tempfile.TemporaryDirectory() as tmpdir:
            self._write_minimal_athena(tmpdir)
            call_command('load_athena_vocabularies', path=tmpdir)
            after_first = VocabularyVersionHistory.objects.filter(
                action=VocabularyVersionHistory.ACTION_LOADED).count()
            self.assertGreater(after_first, 0)
            call_command('load_athena_vocabularies', path=tmpdir)
            after_second = VocabularyVersionHistory.objects.filter(
                action=VocabularyVersionHistory.ACTION_LOADED).count()
        # Second load appends more history rows rather than replacing the trail.
        self.assertEqual(after_second, after_first * 2)


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
    """Tests for omop_core.services.lot_inference_service (ARTEMIS-lite phase-aware rules)."""

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
        pi = PatientRecord.objects.filter(person=person).first()
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

    # ── Phase / procedure tests ────────────────────────────────────────────

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
    """PATCH /api/patient-info/{person_id}/ must preserve PatientRecord.disease.

    When the user saves a disease selection the serializer writes it directly to
    PatientRecord.  _sync_condition then creates a ConditionOccurrence to mirror
    that change in the OMOP tables.  That post_save would normally trigger
    refresh_patient_record → _clear_derived_fields → disease wiped.

    The fix sets _skip_patient_record_refresh = True on the new ConditionOccurrence
    so the user's selection survives the round-trip.
    """

    PERSON_ID = 95001

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        # Fresh person and empty PatientRecord for this class
        cls.dp_person = Person.objects.create(
            person_id=cls.PERSON_ID,
            given_name='Disease',
            family_name='PersistTest',
            year_of_birth=1975,
            gender_source_value='female',
            race_source_value='unknown',
            ethnicity_source_value='unknown',
        )
        PatientRecord.objects.get_or_create(
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

        pi = PatientRecord.objects.get(person=self.dp_person)
        self.assertEqual(
            pi.disease, 'Follicular Lymphoma',
            'PatientRecord.disease was overwritten after PATCH — refresh_patient_record '
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

        pi = PatientRecord.objects.get(person=self.dp_person)
        self.assertEqual(
            pi.disease, 'Chronic Lymphocytic Leukemia (CLL)',
            'PatientRecord.disease was overwritten after PATCH — '
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

        pi = PatientRecord.objects.get(person=self.dp_person)
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
    # Issue #113: _skip_patient_record_refresh flag prevents OMOP overwrite #
    # ------------------------------------------------------------------ #

    def test_disease_survives_sync_to_omop(self):
        """disease persists after sync_to_omop runs _sync_condition directly.

        We verify this by checking that PatientRecord.disease is unchanged
        immediately after sync_to_omop runs (no extra DB write occurred).
        """
        from omop_core.services.omop_write_service import sync_to_omop
        from datetime import date

        pi = PatientRecord.objects.get(person=self.dp_person)
        pi.disease = 'Follicular Lymphoma'
        pi.save(update_fields=['disease'])

        # Call sync_to_omop directly — this runs _sync_condition internally
        sync_to_omop(pi, {'disease'}, changed_data={'disease': 'Follicular Lymphoma'})

        pi.refresh_from_db()
        self.assertEqual(
            pi.disease, 'Follicular Lymphoma',
            'sync_to_omop wiped PatientRecord.disease — _skip_patient_record_refresh '
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
                        {'url': 'https://healthkey.ai/fhir/StructureDefinition/therapy-line',
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
        cls.patient = PatientRecord.objects.create(
            person=cls.person,
            disease='Multiple Myeloma',
            stem_cell_transplant_history=['autologous SCT'],
            sct_date=date(2022, 5, 10),
            sct_eligibility=['eligible for autologous SCT'],
        )

    def test_sct_fields_saved_to_db(self):
        p = PatientRecord.objects.get(pk=self.patient.pk)
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
    """Verify that SCT extensions in a FHIR Patient resource are mapped to PatientRecord."""

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
                                'url': 'https://healthkey.ai/fhir/StructureDefinition/mm-sct-date',
                                'valueString': '2021-03-15',
                            },
                            {
                                'url': 'https://healthkey.ai/fhir/StructureDefinition/mm-sct-history',
                                'valueString': 'autologous SCT,tandem SCT',
                            },
                            {
                                'url': 'https://healthkey.ai/fhir/StructureDefinition/mm-sct-eligibility',
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
        pi = PatientRecord.objects.filter(person__family_name='Jones', person__given_name='Bob').first()
        self.assertIsNotNone(pi, 'PatientRecord not created for Bob Jones')
        self.assertEqual(str(pi.sct_date), '2021-03-15')
        self.assertIn('autologous SCT', pi.stem_cell_transplant_history)
        self.assertIn('tandem SCT', pi.stem_cell_transplant_history)
        self.assertIn('eligible for autologous SCT', pi.sct_eligibility)

    def test_invalid_sct_date_string_is_ignored(self):
        """A malformed mm-sct-date value must be silently dropped; upload must still succeed."""
        resp = self._upload_bundle_with_extensions([
            {'url': 'https://healthkey.ai/fhir/StructureDefinition/mm-sct-date',
             'valueString': 'not-a-date'},
            {'url': 'https://healthkey.ai/fhir/StructureDefinition/mm-sct-history',
             'valueString': 'autologous SCT'},
        ], patient_suffix='003')
        self.assertIn(resp.status_code, [200, 201],
                      msg=f'Upload failed: {getattr(resp, "data", resp.content)}')
        pi = PatientRecord.objects.filter(person__family_name='TestSct003').first()
        self.assertIsNotNone(pi)
        self.assertIsNone(pi.sct_date, 'Invalid sct_date should be dropped, not stored')

    def test_comma_only_sct_history_stores_empty_list(self):
        """A valueString of only commas/whitespace must produce an empty list, not error."""
        resp = self._upload_bundle_with_extensions([
            {'url': 'https://healthkey.ai/fhir/StructureDefinition/mm-sct-history',
             'valueString': ',  ,'},
        ], patient_suffix='004')
        self.assertIn(resp.status_code, [200, 201],
                      msg=f'Upload failed: {getattr(resp, "data", resp.content)}')
        pi = PatientRecord.objects.filter(person__family_name='TestSct004').first()
        self.assertIsNotNone(pi)
        self.assertEqual(pi.stem_cell_transplant_history or [], [],
                         'Comma-only valueString should produce an empty list')

    def test_unknown_vocab_tokens_filtered_from_sct_history(self):
        """Tokens not in the StemCellTransplant vocabulary are silently discarded."""
        resp = self._upload_bundle_with_extensions([
            {'url': 'https://healthkey.ai/fhir/StructureDefinition/mm-sct-history',
             'valueString': 'autologous SCT,unknown experimental SCT,allogeneic SCT'},
        ], patient_suffix='005')
        self.assertIn(resp.status_code, [200, 201],
                      msg=f'Upload failed: {getattr(resp, "data", resp.content)}')
        pi = PatientRecord.objects.filter(person__family_name='TestSct005').first()
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

        class _Shim:
            """Wraps the live registry so get_model('omop_core', 'PatientInfo') resolves to PatientRecord.

            PERMANENT: required as long as migrations 0085–0093 exist, because those
            migrations call apps.get_model('omop_core', 'PatientInfo') and are tested
            against the live registry (which only has PatientRecord after migration 0104).
            Do not remove this shim when cleaning up old migrations.

            Only get_model is intercepted; all other apps attributes pass through to
            django_apps unchanged via __getattr__.
            """
            def get_model(self, app_label, model_name, **kwargs):
                if model_name.lower() == 'patientinfo':
                    model_name = 'PatientRecord'
                return django_apps.get_model(app_label, model_name, **kwargs)
            def __getattr__(self, name):
                return getattr(django_apps, name)

        self._migrate_fn(_Shim(), None)

    def _make_patient(self, person_id, sct_history):
        person = Person.objects.create(person_id=person_id)
        return PatientRecord.objects.create(
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
        # PersonViewSet.partial_update org-check requires PatientRecord; create one
        # scoped to the test org so the write token's org matches.
        PatientRecord.objects.create(person=self.person, organization=self.organization)

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

    def test_default_response_has_no_vocabulary_versions(self):
        resp = self.client.get(self.URL, {'lookup': 'LOINC:2160-0'}, **self._auth())
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertNotIn('_vocabulary_versions', resp.json())  # frozen shape for etl

    def test_include_versions_adds_vocabulary_versions_map(self):
        from omop_core.models import Vocabulary
        Vocabulary.objects.filter(vocabulary_id='LOINC').update(vocabulary_version='LOINC 2.77')
        resp = self.client.get(
            self.URL, {'lookup': 'LOINC:2160-0', 'include_versions': '1'}, **self._auth(),
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        body = resp.json()
        self.assertEqual(body['LOINC']['2160-0'], 3013682)  # existing shape preserved
        self.assertEqual(body['_vocabulary_versions'], {'LOINC': 'LOINC 2.77'})

    def test_include_versions_does_not_clobber_requested_vocab(self):
        # Pathological: a lookup for a vocab literally named `_vocabulary_versions`
        # must keep its own bucket, not be overwritten by the meta map.
        resp = self.client.get(
            self.URL,
            {'lookup': '_vocabulary_versions:x', 'include_versions': '1'},
            **self._auth(),
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.json()['_vocabulary_versions'], {'x': None})


class ConceptGraphTest(_SmartBase):
    """GET /api/v1/concepts/{id}/ancestors|descendants and /api/v1/concepts/graph/."""

    def setUp(self):
        import datetime

        self.url_ancestors = '/api/v1/concepts/9901002/ancestors/'
        self.url_descendants = '/api/v1/concepts/9901001/descendants/'
        self.url_batch = '/api/v1/concepts/graph/'

        self.hemonc_vocab, _ = Vocabulary.objects.get_or_create(
            vocabulary_id='HemOnc',
            defaults={'vocabulary_name': 'HemOnc', 'vocabulary_reference': '', 'vocabulary_version': '', 'vocabulary_concept_id': 0},
        )
        self.rxnorm_vocab, _ = Vocabulary.objects.get_or_create(
            vocabulary_id='RxNorm',
            defaults={'vocabulary_name': 'RxNorm', 'vocabulary_reference': '', 'vocabulary_version': '', 'vocabulary_concept_id': 0},
        )
        self.domain_drug, _ = Domain.objects.get_or_create(
            domain_id='Drug',
            defaults={'domain_name': 'Drug', 'domain_concept_id': 13},
        )
        self.cc_regimen, _ = ConceptClass.objects.get_or_create(
            concept_class_id='Regimen',
            defaults={'concept_class_name': 'Regimen', 'concept_class_concept_id': 0},
        )
        self.cc_ing, _ = ConceptClass.objects.get_or_create(
            concept_class_id='Ingredient',
            defaults={'concept_class_name': 'Ingredient', 'concept_class_concept_id': 0},
        )
        self.cc_drug_class, _ = ConceptClass.objects.get_or_create(
            concept_class_id='Drug Class',
            defaults={'concept_class_name': 'Drug Class', 'concept_class_concept_id': 0},
        )
        self.today = datetime.date(1970, 1, 1)
        self.future = datetime.date(2099, 12, 31)

        self.regimen = Concept.objects.create(
            concept_id=9901001,
            concept_name='AC-T regimen',
            domain=self.domain_drug,
            vocabulary=self.hemonc_vocab,
            concept_class=self.cc_regimen,
            concept_code='REG-AC-T',
            valid_start_date=self.today,
            valid_end_date=self.future,
        )
        self.component = Concept.objects.create(
            concept_id=9901002,
            concept_name='trastuzumab',
            domain=self.domain_drug,
            vocabulary=self.rxnorm_vocab,
            concept_class=self.cc_ing,
            concept_code='RX-TRAST',
            valid_start_date=self.today,
            valid_end_date=self.future,
        )
        self.drug_class = Concept.objects.create(
            concept_id=9901003,
            concept_name='HER2 inhibitor',
            domain=self.domain_drug,
            vocabulary=self.hemonc_vocab,
            concept_class=self.cc_drug_class,
            concept_code='CLASS-HER2',
            valid_start_date=self.today,
            valid_end_date=self.future,
        )
        self.super_class = Concept.objects.create(
            concept_id=9901004,
            concept_name='Targeted therapy',
            domain=self.domain_drug,
            vocabulary=self.hemonc_vocab,
            concept_class=self.cc_drug_class,
            concept_code='CLASS-TARGETED',
            valid_start_date=self.today,
            valid_end_date=self.future,
        )

        self.rel_targeted, _ = Relationship.objects.get_or_create(
            relationship_id='Has targeted therapy',
            defaults={
                'relationship_name': 'Has targeted therapy',
                'is_hierarchical': 0,
                'defines_ancestry': 0,
                'reverse_relationship_id': 'Targeted therapy of',
                'relationship_concept_id': 0,
            },
        )
        ConceptRelationship.objects.get_or_create(
            concept_1=self.regimen,
            concept_2=self.component,
            relationship=self.rel_targeted,
            defaults={'valid_start_date': self.today, 'valid_end_date': self.future},
        )
        ConceptAncestor.objects.get_or_create(
            ancestor_concept=self.drug_class,
            descendant_concept=self.component,
            defaults={'min_levels_of_separation': 1, 'max_levels_of_separation': 1},
        )
        ConceptAncestor.objects.get_or_create(
            ancestor_concept=self.super_class,
            descendant_concept=self.component,
            defaults={'min_levels_of_separation': 2, 'max_levels_of_separation': 2},
        )

    def _auth(self):
        return {'HTTP_AUTHORIZATION': f'Bearer {self.read_token.token}'}

    def test_descendants_relationship_filter_returns_regimen_components(self):
        resp = self.client.get(
            f'{self.url_descendants}?relationship_id=Has%20targeted%20therapy',
            **self._auth(),
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.json()['count'], 1)
        node = resp.json()['results'][0]
        self.assertEqual(node['concept_id'], self.component.concept_id)
        self.assertEqual(node['relationship_id'], 'Has targeted therapy')

    def test_ancestors_uses_concept_ancestor_with_filters(self):
        resp = self.client.get(
            f'{self.url_ancestors}?max_levels=1&vocabulary_id=HemOnc',
            **self._auth(),
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.json()['count'], 1)
        node = resp.json()['results'][0]
        self.assertEqual(node['concept_id'], self.drug_class.concept_id)
        self.assertEqual(node['min_levels_of_separation'], 1)
        self.assertEqual(node['vocabulary_id'], 'HemOnc')

    def test_graph_node_carries_vocabulary_version(self):
        self.hemonc_vocab.vocabulary_version = 'HemOnc 2024-12-19'
        self.hemonc_vocab.save(update_fields=['vocabulary_version'])
        resp = self.client.get(
            f'{self.url_ancestors}?max_levels=1&vocabulary_id=HemOnc',
            **self._auth(),
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        node = resp.json()['results'][0]
        self.assertEqual(node['vocabulary_id'], 'HemOnc')
        self.assertEqual(node['vocabulary_version'], 'HemOnc 2024-12-19')

    def test_batch_endpoint_groups_results_by_source_concept(self):
        resp = self.client.get(
            f'{self.url_batch}?direction=descendants&concept_id={self.regimen.concept_id}&concept_id=999999&relationship_id=Has%20targeted%20therapy',
            **self._auth(),
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        data = resp.json()['results']
        self.assertEqual(len(data[str(self.regimen.concept_id)]), 1)
        self.assertEqual(data[str(self.regimen.concept_id)][0]['concept_id'], self.component.concept_id)
        self.assertEqual(data['999999'], [])

    def test_invalid_direction_returns_400(self):
        resp = self.client.get(
            f'{self.url_batch}?direction=sideways&concept_id={self.regimen.concept_id}',
            **self._auth(),
        )
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_unauthenticated_returns_401(self):
        resp = self.client.get(self.url_ancestors)
        self.assertIn(resp.status_code, [status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN])

    def test_ancestors_relationship_mode_returns_in_neighbors(self):
        """Pin the documented edge-direction contract: relationship-mode
        'ancestors' returns concepts with an edge pointing AT the source
        (in-neighbors), so ancestors of the component via 'Has targeted
        therapy' is the regimen (concept_1 -> concept_2 edge direction)."""
        url = f'/api/v1/concepts/{self.component.concept_id}/ancestors/'
        resp = self.client.get(
            f'{url}?relationship_id=Has%20targeted%20therapy',
            **self._auth(),
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.json()['count'], 1)
        self.assertEqual(resp.json()['results'][0]['concept_id'], self.regimen.concept_id)

    def test_unknown_concept_single_endpoint_returns_404(self):
        resp = self.client.get('/api/v1/concepts/999999/ancestors/', **self._auth())
        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)

    def test_invalid_max_levels_returns_400(self):
        for bad in ('0', '-3', 'abc'):
            resp = self.client.get(
                f'{self.url_ancestors}?max_levels={bad}',
                **self._auth(),
            )
            self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST, bad)

    def test_batch_missing_concept_id_returns_400(self):
        resp = self.client.get(f'{self.url_batch}?direction=ancestors', **self._auth())
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_batch_non_integer_concept_id_returns_400(self):
        resp = self.client.get(
            f'{self.url_batch}?direction=ancestors&concept_id=abc',
            **self._auth(),
        )
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_batch_over_cap_returns_400(self):
        from patient_portal.api.views import CONCEPT_GRAPH_MAX_BATCH_IDS
        params = '&'.join(
            f'concept_id={9902000 + i}' for i in range(CONCEPT_GRAPH_MAX_BATCH_IDS + 1)
        )
        resp = self.client.get(
            f'{self.url_batch}?direction=ancestors&{params}',
            **self._auth(),
        )
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_relationship_mode_excludes_invalid_edges(self):
        ConceptRelationship.objects.create(
            concept_1=self.regimen,
            concept_2=self.drug_class,
            relationship=self.rel_targeted,
            valid_start_date=self.today,
            valid_end_date=self.future,
            invalid_reason='D',
        )
        resp = self.client.get(
            f'{self.url_descendants}?relationship_id=Has%20targeted%20therapy',
            **self._auth(),
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        returned_ids = {n['concept_id'] for n in resp.json()['results']}
        self.assertEqual(returned_ids, {self.component.concept_id})

    def test_truncated_flag_when_results_exceed_cap(self):
        from patient_portal.api import views as api_views
        extra_components = [
            Concept(
                concept_id=9903000 + i,
                concept_name=f'component {i}',
                domain=self.domain_drug,
                vocabulary=self.rxnorm_vocab,
                concept_class=self.cc_ing,
                concept_code=f'RX-EXTRA-{i}',
                valid_start_date=self.today,
                valid_end_date=self.future,
            )
            for i in range(3)
        ]
        Concept.objects.bulk_create(extra_components)
        ConceptRelationship.objects.bulk_create([
            ConceptRelationship(
                concept_1=self.regimen,
                concept_2=c,
                relationship=self.rel_targeted,
                valid_start_date=self.today,
                valid_end_date=self.future,
            )
            for c in extra_components
        ])
        original = api_views.CONCEPT_GRAPH_MAX_RESULTS_PER_SOURCE
        api_views.CONCEPT_GRAPH_MAX_RESULTS_PER_SOURCE = 2
        try:
            resp = self.client.get(
                f'{self.url_descendants}?relationship_id=Has%20targeted%20therapy',
                **self._auth(),
            )
        finally:
            api_views.CONCEPT_GRAPH_MAX_RESULTS_PER_SOURCE = original
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        payload = resp.json()
        self.assertEqual(payload['count'], 2)
        self.assertTrue(payload['truncated'])

    def test_not_truncated_flag_when_results_within_cap(self):
        resp = self.client.get(
            f'{self.url_descendants}?relationship_id=Has%20targeted%20therapy',
            **self._auth(),
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertFalse(resp.json()['truncated'])

    def test_batch_response_includes_truncated_list(self):
        resp = self.client.get(
            f'{self.url_batch}?direction=descendants&concept_id={self.regimen.concept_id}&relationship_id=Has%20targeted%20therapy',
            **self._auth(),
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.json()['truncated'], [])


class ConceptReplacementEndpointTest(_SmartBase):
    """GET /api/v1/concepts/{id}/replacement/ — embedded-term substitution (TI.4.2#07)."""

    def setUp(self):
        import datetime
        self.vocab, _ = Vocabulary.objects.get_or_create(
            vocabulary_id='RxNorm',
            defaults={'vocabulary_name': 'RxNorm', 'vocabulary_reference': '',
                      'vocabulary_version': 'RxNorm 2026', 'vocabulary_concept_id': 0},
        )
        self.domain, _ = Domain.objects.get_or_create(
            domain_id='Drug', defaults={'domain_name': 'Drug', 'domain_concept_id': 13},
        )
        self.cc, _ = ConceptClass.objects.get_or_create(
            concept_class_id='Ingredient',
            defaults={'concept_class_name': 'Ingredient', 'concept_class_concept_id': 0},
        )
        self.today = datetime.date(1970, 1, 1)
        self.future = datetime.date(2099, 12, 31)
        self.old = Concept.objects.create(
            concept_id=770001, concept_name='Old drug', domain=self.domain,
            vocabulary=self.vocab, concept_class=self.cc, concept_code='OLD',
            valid_start_date=self.today, valid_end_date=self.future, invalid_reason='U',
        )
        self.new = Concept.objects.create(
            concept_id=770002, concept_name='New drug', domain=self.domain,
            vocabulary=self.vocab, concept_class=self.cc, concept_code='NEW',
            valid_start_date=self.today, valid_end_date=self.future,
        )
        self.rel, _ = Relationship.objects.get_or_create(
            relationship_id='Concept replaced by',
            defaults={'relationship_name': 'Concept replaced by', 'is_hierarchical': 0,
                      'defines_ancestry': 0, 'reverse_relationship_id': 'Concept replaces',
                      'relationship_concept_id': 0},
        )
        ConceptRelationship.objects.get_or_create(
            concept_1=self.old, concept_2=self.new, relationship=self.rel,
            defaults={'valid_start_date': self.today, 'valid_end_date': self.future},
        )

    def _auth(self):
        return {'HTTP_AUTHORIZATION': f'Bearer {self.read_token.token}'}

    def test_deprecated_concept_resolves_to_successor(self):
        resp = self.client.get(
            f'/api/v1/concepts/{self.old.concept_id}/replacement/', **self._auth(),
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        body = resp.json()
        self.assertTrue(body['replaced'])
        self.assertEqual(body['resolved_concept']['concept_id'], self.new.concept_id)
        self.assertEqual(body['chain'], [self.old.concept_id, self.new.concept_id])
        self.assertEqual(body['resolved_concept']['vocabulary_version'], 'RxNorm 2026')

    def test_active_concept_is_identity(self):
        resp = self.client.get(
            f'/api/v1/concepts/{self.new.concept_id}/replacement/', **self._auth(),
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        body = resp.json()
        self.assertFalse(body['replaced'])
        self.assertEqual(body['resolved_concept']['concept_id'], self.new.concept_id)

    def test_unknown_concept_returns_404(self):
        resp = self.client.get('/api/v1/concepts/99999999/replacement/', **self._auth())
        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)

    def test_unauthenticated_returns_401_or_403(self):
        resp = self.client.get(f'/api/v1/concepts/{self.old.concept_id}/replacement/')
        self.assertIn(resp.status_code, [status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN])


class _ConceptFixtureBase(_SmartBase):
    """Shared OMOP concept fixtures for the search/list endpoint tests (issue #213)."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        from oauth2_provider.models import AccessToken
        from django.utils import timezone as tz
        from omop_core.models import Concept, Vocabulary, Domain, ConceptClass
        import datetime

        # Token with no read scope — must be rejected by ScopedTokenPermission
        cls.empty_scope_token = AccessToken.objects.create(
            user=cls.foundation_user,
            application=cls.app,
            token='concept-empty-scope-token-444',
            expires=tz.now() + datetime.timedelta(hours=1),
            scope='',
        )

        for vocab_id in ('LOINC', 'SNOMED'):
            Vocabulary.objects.get_or_create(
                vocabulary_id=vocab_id,
                defaults={'vocabulary_name': vocab_id, 'vocabulary_reference': '',
                          'vocabulary_version': '', 'vocabulary_concept_id': 0},
            )
        for domain_id in ('Measurement', 'Condition'):
            Domain.objects.get_or_create(
                domain_id=domain_id,
                defaults={'domain_name': domain_id, 'domain_concept_id': 0},
            )
        for class_id in ('Lab Test', 'Clinical Finding'):
            ConceptClass.objects.get_or_create(
                concept_class_id=class_id,
                defaults={'concept_class_name': class_id, 'concept_class_concept_id': 0},
            )

        common = {
            'valid_start_date': datetime.date(1970, 1, 1),
            'valid_end_date': datetime.date(2099, 12, 31),
        }
        cls.creatinine_serum = Concept.objects.create(
            concept_id=3016723, concept_name='Creatinine [Mass/volume] in Serum or Plasma',
            vocabulary_id='LOINC', domain_id='Measurement', concept_class_id='Lab Test',
            concept_code='2160-0', standard_concept='S', **common,
        )
        cls.creatinine_renal = Concept.objects.create(
            concept_id=3016724, concept_name='Creatinine renal clearance/1.73 sq M',
            vocabulary_id='LOINC', domain_id='Measurement', concept_class_id='Lab Test',
            concept_code='35203-9', standard_concept=None, **common,
        )
        cls.creatinine_snomed = Concept.objects.create(
            concept_id=4013964, concept_name='Creatinine measurement, serum',
            vocabulary_id='SNOMED', domain_id='Measurement', concept_class_id='Lab Test',
            concept_code='113075003', standard_concept='S', **common,
        )
        cls.diabetes = Concept.objects.create(
            concept_id=201826, concept_name='Type 2 diabetes mellitus',
            vocabulary_id='SNOMED', domain_id='Condition', concept_class_id='Clinical Finding',
            concept_code='44054006', standard_concept='S', **common,
        )

    def _auth(self):
        return {'HTTP_AUTHORIZATION': f'Bearer {self.read_token.token}'}


class ConceptSearchTest(_ConceptFixtureBase):
    """GET /api/v1/concepts/search/ (issue #213)"""

    URL = '/api/v1/concepts/search/'

    def test_search_by_name_substring(self):
        # Membership assertions, not exact counts — seed migrations may add
        # concepts whose names also match (same convention as ConceptListTest).
        resp = self.client.get(self.URL, {'q': 'creatinine', 'page_size': 100}, **self._auth())
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        results = resp.json()['results']
        ids = {r['concept_id'] for r in results}
        self.assertLessEqual(
            {self.creatinine_serum.concept_id, self.creatinine_renal.concept_id,
             self.creatinine_snomed.concept_id},
            ids,
        )
        self.assertNotIn(self.diabetes.concept_id, ids)
        self.assertTrue(all('creatinine' in r['concept_name'].lower() for r in results))

    def test_search_result_shape(self):
        resp = self.client.get(
            self.URL, {'q': 'Type 2 diabetes', 'page_size': 100}, **self._auth(),
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        results = resp.json()['results']
        match = next(
            (r for r in results if r['concept_id'] == self.diabetes.concept_id), None,
        )
        self.assertEqual(match, {
            'concept_id': 201826,
            'concept_name': 'Type 2 diabetes mellitus',
            'vocabulary_id': 'SNOMED',
            'vocabulary_version': '',
            'concept_code': '44054006',
            'domain_id': 'Condition',
            'concept_class_id': 'Clinical Finding',
            'standard_concept': 'S',
        })

    def test_search_result_carries_vocabulary_version(self):
        from omop_core.models import Vocabulary
        Vocabulary.objects.filter(vocabulary_id='SNOMED').update(
            vocabulary_version='SNOMED 2024-09-01')
        resp = self.client.get(
            self.URL, {'q': 'Type 2 diabetes', 'page_size': 100}, **self._auth(),
        )
        match = next(
            (r for r in resp.json()['results'] if r['concept_id'] == self.diabetes.concept_id),
            None,
        )
        self.assertIsNotNone(match)
        self.assertEqual(match['vocabulary_version'], 'SNOMED 2024-09-01')

    def test_search_filtered_by_vocabulary(self):
        resp = self.client.get(
            self.URL,
            {'q': 'creatinine', 'vocabulary_id': 'SNOMED', 'page_size': 100},
            **self._auth(),
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        results = resp.json()['results']
        ids = {r['concept_id'] for r in results}
        self.assertIn(self.creatinine_snomed.concept_id, ids)
        self.assertNotIn(self.creatinine_serum.concept_id, ids)
        self.assertTrue(all(r['vocabulary_id'] == 'SNOMED' for r in results))

    def test_search_filtered_by_standard_concept(self):
        resp = self.client.get(
            self.URL,
            {'q': 'creatinine', 'standard_concept': 'S', 'page_size': 100},
            **self._auth(),
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        ids = {r['concept_id'] for r in resp.json()['results']}
        self.assertNotIn(self.creatinine_renal.concept_id, ids)
        self.assertLessEqual(
            {self.creatinine_serum.concept_id, self.creatinine_snomed.concept_id}, ids,
        )

    def test_search_no_match_returns_empty_page(self):
        resp = self.client.get(self.URL, {'q': 'zzz-no-such-concept'}, **self._auth())
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.json()['count'], 0)

    def test_missing_q_returns_400(self):
        resp = self.client.get(self.URL, **self._auth())
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_short_q_returns_400(self):
        resp = self.client.get(self.URL, {'q': 'c'}, **self._auth())
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_pagination_page_size(self):
        resp = self.client.get(self.URL, {'q': 'creatinine', 'page_size': 2}, **self._auth())
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        data = resp.json()
        self.assertGreaterEqual(data['count'], 3)
        self.assertEqual(len(data['results']), 2)
        self.assertIsNotNone(data['next'])

    def test_unauthenticated_returns_401(self):
        resp = self.client.get(self.URL, {'q': 'creatinine'})
        self.assertIn(resp.status_code, [status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN])

    def test_expired_token_rejected(self):
        resp = self.client.get(
            self.URL, {'q': 'creatinine'},
            HTTP_AUTHORIZATION=f'Bearer {self.expired_token.token}',
        )
        self.assertIn(resp.status_code, [status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN])

    def test_empty_scope_token_rejected(self):
        resp = self.client.get(
            self.URL, {'q': 'creatinine'},
            HTTP_AUTHORIZATION=f'Bearer {self.empty_scope_token.token}',
        )
        self.assertIn(resp.status_code, [status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN])


class ConceptListTest(_ConceptFixtureBase):
    """GET /api/v1/concepts/ (issue #213)"""

    URL = '/api/v1/concepts/'

    def test_list_by_domain(self):
        # Seed migrations may pre-populate concepts, so assert membership and
        # filter correctness rather than exact counts.
        resp = self.client.get(
            self.URL, {'domain_id': 'Condition', 'page_size': 100}, **self._auth(),
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        results = resp.json()['results']
        self.assertIn(self.diabetes.concept_id, {r['concept_id'] for r in results})
        self.assertTrue(all(r['domain_id'] == 'Condition' for r in results))

    def test_list_by_concept_class(self):
        resp = self.client.get(
            self.URL, {'concept_class_id': 'Lab Test', 'page_size': 100}, **self._auth(),
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        results = resp.json()['results']
        ids = {r['concept_id'] for r in results}
        self.assertLessEqual(
            {self.creatinine_serum.concept_id, self.creatinine_renal.concept_id,
             self.creatinine_snomed.concept_id},
            ids,
        )
        self.assertTrue(all(r['concept_class_id'] == 'Lab Test' for r in results))

    def test_list_by_combined_filters(self):
        resp = self.client.get(
            self.URL,
            {'vocabulary_id': 'LOINC', 'domain_id': 'Measurement', 'page_size': 100},
            **self._auth(),
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        results = resp.json()['results']
        ids = {r['concept_id'] for r in results}
        self.assertLessEqual(
            {self.creatinine_serum.concept_id, self.creatinine_renal.concept_id}, ids,
        )
        self.assertNotIn(self.creatinine_snomed.concept_id, ids)
        self.assertTrue(all(
            r['vocabulary_id'] == 'LOINC' and r['domain_id'] == 'Measurement'
            for r in results
        ))

    def test_list_unknown_filter_value_returns_empty_page(self):
        resp = self.client.get(self.URL, {'domain_id': 'NoSuchDomain'}, **self._auth())
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.json()['count'], 0)

    def test_list_without_filter_returns_400(self):
        resp = self.client.get(self.URL, **self._auth())
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_list_standard_concept_alone_returns_400(self):
        """standard_concept is too unselective to bound a listing by itself."""
        resp = self.client.get(self.URL, {'standard_concept': 'S'}, **self._auth())
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_list_standard_concept_combines_with_selective_filter(self):
        resp = self.client.get(
            self.URL,
            {'concept_class_id': 'Lab Test', 'standard_concept': 'S', 'page_size': 100},
            **self._auth(),
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        ids = {r['concept_id'] for r in resp.json()['results']}
        self.assertIn(self.creatinine_serum.concept_id, ids)
        self.assertNotIn(self.creatinine_renal.concept_id, ids)

    def test_unauthenticated_returns_401(self):
        resp = self.client.get(self.URL, {'domain_id': 'Condition'})
        self.assertIn(resp.status_code, [status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN])

    def test_expired_token_rejected(self):
        resp = self.client.get(
            self.URL, {'domain_id': 'Condition'},
            HTTP_AUTHORIZATION=f'Bearer {self.expired_token.token}',
        )
        self.assertIn(resp.status_code, [status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN])

    def test_empty_scope_token_rejected(self):
        resp = self.client.get(
            self.URL, {'domain_id': 'Condition'},
            HTTP_AUTHORIZATION=f'Bearer {self.empty_scope_token.token}',
        )
        self.assertIn(resp.status_code, [status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN])


# ---------------------------------------------------------------------------
# IDOR: PatientRecordViewSet row-level access (issue #134)
# ---------------------------------------------------------------------------

class PatientRecordIDORTest(TestCase):
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
        cls.patient_a = PatientRecord.objects.create(person=cls.person_a)
        cls.identity_a = Identity.objects.create_user(email='alice@test.com', password='pw')
        PatientUser.objects.create(identity=cls.identity_a, person=cls.person_a)

        # Patient B — the victim
        cls.person_b = Person.objects.create(person_id=88802, family_name='Beta', given_name='Bob')
        cls.patient_b = PatientRecord.objects.create(person=cls.person_b)
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
        PatientRecord.objects.create(person=cls.person_a)
        cls.identity_a = Identity.objects.create_user(email='attacker@test.com', password='pw')
        PatientUser.objects.create(identity=cls.identity_a, person=cls.person_a)

        # Patient B — the victim
        cls.person_b = Person.objects.create(person_id=88902, family_name='Victim', given_name='Bob')
        PatientRecord.objects.create(person=cls.person_b)
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

class PatientRecordOrganizationReadOnlyTest(TestCase):
    """
    Verify that a client cannot PATCH organization or person onto a
    PatientRecord record — these fields must be silently ignored (read-only).
    """

    @classmethod
    def setUpTestData(cls):
        from omop_core.models import Organization
        from patient_portal.models import PatientUser

        cls.org_a = Organization.objects.create(name='Org A', slug='org-a-139')
        cls.org_b = Organization.objects.create(name='Org B', slug='org-b-139')

        cls.person = Person.objects.create(person_id=89001, family_name='Test', given_name='User')
        cls.patient = PatientRecord.objects.create(person=cls.person, organization=cls.org_a)
        cls.identity = Identity.objects.create_user(email='orgtest@test.com', password='pw')
        PatientUser.objects.create(identity=cls.identity, person=cls.person)

        cls.other_person = Person.objects.create(person_id=89002, family_name='Other', given_name='Person')
        PatientRecord.objects.create(person=cls.other_person, organization=cls.org_b)

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
        If refresh_patient_record raises mid-patient, the Person and all OMOP
        rows written before the error must be rolled back.
        """
        from unittest.mock import patch
        from omop_core.services.patient_record_service import refresh_patient_record

        person_id_before = (
            Person.objects.order_by('-person_id').values_list('person_id', flat=True).first() or 0
        )

        bundle = _make_fhir_bundle()
        bundle_bytes = json.dumps(bundle).encode('utf-8')
        fhir_file = io.BytesIO(bundle_bytes)
        fhir_file.name = 'bundle.json'

        with patch(
            'patient_portal.api.views.refresh_patient_record',
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
        PatientRecord.objects.create(person=cls.person_a, organization=cls.org_a)
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
        PatientRecord.objects.create(person=cls.person_b, organization=cls.org_b)
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
# TherapyConceptIdTest — HemOnc concept_id fields on PatientRecord
# ---------------------------------------------------------------------------

class TherapyConceptIdTest(TestCase):
    """
    Verify that refresh_patient_record populates first_line_therapy_id /
    second_line_therapy_id / later_therapy_ids via the HemOnc regimen lookup,
    and that the PatientRecordSerializer display fields fall back correctly.
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
        cls.pi_krd = PatientRecord.objects.create(person=cls.person_krd)

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
        cls.pi_vrd = PatientRecord.objects.create(person=cls.person_vrd)

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
        from omop_core.services.patient_record_service import refresh_patient_record
        return refresh_patient_record(person)

    def test_krd_first_line_therapy_id_is_populated(self):
        """refresh_patient_record sets first_line_therapy_id=35806284 for KRd."""
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
        from patient_portal.api.serializers import PatientRecordSerializer
        data = PatientRecordSerializer(pi).data
        self.assertEqual(data['first_line_therapy_display'], 'KRd')

    def test_serializer_display_falls_back_to_text_when_no_concept_id(self):
        """first_line_therapy_display falls back to first_line_therapy text when id is None."""
        pi = self._refresh(self.person_vrd)
        pi.first_line_therapy = 'VRd'
        pi.first_line_therapy_id = None
        pi.save(update_fields=['first_line_therapy', 'first_line_therapy_id'])
        from patient_portal.api.serializers import PatientRecordSerializer
        data = PatientRecordSerializer(pi).data
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
            PatientRecord.objects.create(person=p, organization=self.org_a, disease_slug=slug)

        # Create patient in org_b
        p4 = Person.objects.create(person_id=9004)
        PatientRecord.objects.create(person=p4, organization=self.org_b, disease_slug='cll')

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
        GroupAccess.objects.create(identity=navigator, org=self.org_b, role='analyst')
        resp = self._get(navigator)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        slugs = {o['org_slug'] for o in resp.data}
        self.assertIn('org-b', slugs)
        self.assertNotIn('org-a', slugs)

    def test_no_grants_returns_empty_list(self):
        resp = self._get(self.nobody)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data, [])

    def test_no_grants_sees_public_aggregated_org(self):
        self.org_b.allows_public_aggregated_data = True
        self.org_b.save(update_fields=['allows_public_aggregated_data'])
        resp = self._get(self.nobody)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        slugs = {o['org_slug'] for o in resp.data}
        self.assertIn('org-b', slugs)
        self.assertNotIn('org-a', slugs)

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
        counts = {d['disease_slug']: d['count'] for d in org_a['disease_counts']}
        self.assertEqual(counts['mm'], 2)
        self.assertEqual(counts['breast-cancer'], 1)
        self.assertEqual(counts['cll'], 1)

    def test_domain_trust_inflates_accessible_count(self):
        from omop_core.models import OrgTrust
        # org_b grants access to users with @t.com — org_admin has email admin@t.com
        OrgTrust.objects.create(granting_org=self.org_b, trusted_domain='t.com')
        resp = self._get(self.org_admin)
        org_a = next(o for o in resp.data if o['org_slug'] == 'org-a')
        self.assertEqual(org_a['owned_count'], 3)
        self.assertEqual(org_a['accessible_count'], 4)  # 3 owned + 1 from org_b via domain trust
        counts = {d['disease_slug']: d['count'] for d in org_a['disease_counts']}
        self.assertEqual(counts['mm'], 2)
        self.assertEqual(counts['breast-cancer'], 1)
        self.assertEqual(counts['cll'], 1)

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
        self.pi_a1 = PatientRecord.objects.create(person=p1, organization=self.org_a)
        self.pi_a2 = PatientRecord.objects.create(person=p2, organization=self.org_a)
        self.pi_b = PatientRecord.objects.create(person=p3, organization=self.org_b)
        self.pi_none = PatientRecord.objects.create(person=p4)

        self.org_admin = Identity.objects.create_user(email='orgadmin@t.com', password='x')
        self.no_grant = Identity.objects.create_user(email='nogrant@t.com', password='x')
        self.staff = Identity.objects.create_user(email='staff2@t.com', password='x', is_staff=True)

        from django.utils import timezone
        GroupAccess.objects.create(
            identity=self.org_admin,
            org=self.org_a,
            role='org_admin',
        )
        PatientRecord.objects.filter(pk=self.pi_a1.pk).update(
            disease='Breast Cancer',
            stage='Breast Cancer Stage IIA',
            updated_at=timezone.now(),
        )
        PatientRecord.objects.filter(pk=self.pi_a2.pk).update(
            disease='Multiple Myeloma',
            stage='III',
            updated_at=timezone.now() - timedelta(days=45),
        )
        PatientRecord.objects.filter(pk=self.pi_b.pk).update(
            disease='Breast Cancer',
            stage='Stage II',
            updated_at=timezone.now(),
        )
        PatientRecord.objects.filter(pk=self.pi_none.pk).update(
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

    def test_direct_org_doctor_does_not_get_individual_patient_access(self):
        doctor = Identity.objects.create_user(email='directdoc-patient-list@t.com', password='x')
        GroupAccess.objects.create(identity=doctor, org=self.org_a, role='doctor')
        resp = self._get(doctor)
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

    def test_domain_trust_admin_sees_trusted_org_patients(self):
        trusted_admin = Identity.objects.create_user(email='trusted@t.com', password='x')
        OrgTrust.objects.create(granting_org=self.org_a, trusted_domain='t.com')
        resp = self._get(trusted_admin)
        self.assertEqual(resp.status_code, 200)
        ids = {p['id'] for p in resp.data}
        self.assertIn(self.pi_a1.id, ids)
        self.assertIn(self.pi_a2.id, ids)
        self.assertNotIn(self.pi_b.id, ids)

    def test_org_to_org_trust_admin_sees_trusted_org_patients(self):
        trusted_admin = Identity.objects.create_user(email='trusted-org-link@t.com', password='x')
        source_org = Organization.objects.create(name='Source Org', slug='source-org-patient-scope')
        GroupAccess.objects.create(identity=trusted_admin, org=source_org, role='doctor')
        OrgTrust.objects.create(granting_org=self.org_b, trusted_org=source_org)
        resp = self._get(trusted_admin)
        self.assertEqual(resp.status_code, 200)
        ids = {p['id'] for p in resp.data}
        self.assertIn(self.pi_b.id, ids)
        self.assertNotIn(self.pi_a1.id, ids)


# ---------------------------------------------------------------------------
# bulk_delete_filtered Tests
# ---------------------------------------------------------------------------

class BulkDeleteFilteredTest(TestCase):
    """Tests for DELETE /api/patient-info/bulk_delete_filtered/

    PatientRecord has a unique constraint on person_id (one row per person).
    Org scoping is enforced at the PatientRecord.organization level.
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
        self.pi_a1 = PatientRecord.objects.create(person=self.p1, organization=self.org_a, disease='Breast Cancer')
        self.pi_a2 = PatientRecord.objects.create(person=self.p2, organization=self.org_a, disease='Multiple Myeloma')
        self.pi_b  = PatientRecord.objects.create(person=self.p3, organization=self.org_b, disease='Breast Cancer')

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
        """Org A write token must not delete Org B's PatientRecord via bulk_delete_filtered."""
        c = APIClient()
        c.credentials(HTTP_AUTHORIZATION=f'Bearer {self.write_token_a.token}')
        resp = c.delete('/api/patient-info/bulk_delete_filtered/')
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.data['success'])
        # pi_b belongs to org_b — must still exist
        self.assertTrue(PatientRecord.objects.filter(pk=self.pi_b.pk).exists())
        # p3 (org_b patient) must still exist
        from omop_core.models import Person as P
        self.assertTrue(P.objects.filter(person_id=self.p3.person_id).exists())

    def test_disease_filter_scopes_deletion(self):
        """Only PatientRecord matching the disease filter should be deleted."""
        self.client.force_authenticate(user=self.staff)
        resp = self.client.delete(
            '/api/patient-info/bulk_delete_filtered/?disease=Multiple+Myeloma'
        )
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.data['success'])
        # pi_a2 (Multiple Myeloma) should be gone
        self.assertFalse(PatientRecord.objects.filter(pk=self.pi_a2.pk).exists())
        # pi_a1 and pi_b (Breast Cancer) should survive
        self.assertTrue(PatientRecord.objects.filter(pk=self.pi_a1.pk).exists())
        self.assertTrue(PatientRecord.objects.filter(pk=self.pi_b.pk).exists())

    def test_deleted_count_matches_matched_rows(self):
        """deleted_count must equal the number of PatientRecord rows that matched the filters."""
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
        """Org A token with no additional filters deletes all PatientRecord in org A only."""
        c = APIClient()
        c.credentials(HTTP_AUTHORIZATION=f'Bearer {self.write_token_a.token}')
        resp = c.delete('/api/patient-info/bulk_delete_filtered/')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data['deleted_count'], 2)
        # pi_a1 and pi_a2 should be gone; pi_b should survive
        self.assertFalse(PatientRecord.objects.filter(pk=self.pi_a1.pk).exists())
        self.assertFalse(PatientRecord.objects.filter(pk=self.pi_a2.pk).exists())
        self.assertTrue(PatientRecord.objects.filter(pk=self.pi_b.pk).exists())


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


def _make_test_concept(concept_id, concept_name, concept_code, domain_id):
    vocab, _ = Vocabulary.objects.get_or_create(
        vocabulary_id='TEST-DEL',
        defaults={
            'vocabulary_name': 'Test Delete Vocabulary',
            'vocabulary_concept_id': 0,
        },
    )
    domain, _ = Domain.objects.get_or_create(
        domain_id=domain_id,
        defaults={'domain_name': domain_id, 'domain_concept_id': 0},
    )
    concept_class, _ = ConceptClass.objects.get_or_create(
        concept_class_id='Clinical Finding',
        defaults={'concept_class_name': 'Clinical Finding', 'concept_class_concept_id': 0},
    )
    return Concept.objects.create(
        concept_id=concept_id,
        concept_name=concept_name,
        concept_code=concept_code,
        vocabulary=vocab,
        domain=domain,
        concept_class=concept_class,
        standard_concept='S',
        valid_start_date=date.today(),
        valid_end_date=date(2099, 12, 31),
    )


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
    """Access helpers include trust-based orgs."""

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

    def test_domain_trust_gives_admin_access(self):
        from omop_core.services.access import get_admin_orgs
        OrgTrust.objects.create(granting_org=self.org_b, trusted_domain='trusted.com')
        orgs = get_admin_orgs(self.domain_user)
        self.assertIn(self.org_b, orgs)

    def test_org_to_org_trust_gives_admin_access(self):
        from omop_core.services.access import get_admin_orgs
        OrgTrust.objects.create(granting_org=self.org_b, trusted_org=self.org_a)
        orgs = get_admin_orgs(self.direct_user)
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

    def test_delete_org_cascades_patient_population(self):
        patient_org = _make_org('Patient Org', 'patient-org')
        other_org = _make_org('Other Patient Org', 'other-patient-org')
        person = Person.objects.create(person_id=9001)
        other_person = Person.objects.create(person_id=9002)
        patient = PatientRecord.objects.create(person=person, organization=patient_org)
        other_patient = PatientRecord.objects.create(person=other_person, organization=other_org)

        condition_concept = _make_test_concept(9100001, 'Test Condition', 'TCOND', 'Condition')
        condition_type_concept = _make_test_concept(9100002, 'Test Type', 'TTYPE', 'Type Concept')
        drug_concept = _make_test_concept(9200001, 'Test Drug', 'TDRUG', 'Drug')
        procedure_concept = _make_test_concept(9300001, 'Test Procedure', 'TPROC', 'Procedure')

        cond = ConditionOccurrence.objects.create(
            condition_occurrence_id=9101,
            person=person,
            condition_concept=condition_concept,
            condition_start_date=date.today(),
            condition_type_concept=condition_type_concept,
        )
        drug = DrugExposure.objects.create(
            drug_exposure_id=9201,
            person=person,
            drug_concept=drug_concept,
            drug_exposure_start_date=date.today(),
            drug_type_concept=condition_type_concept,
        )
        proc = ProcedureOccurrence.objects.create(
            procedure_occurrence_id=9301,
            person=person,
            procedure_concept=procedure_concept,
            procedure_date=date.today(),
            procedure_type_concept=condition_type_concept,
        )

        resp = self.client.delete('/api/orgs/patient-org/')
        self.assertEqual(resp.status_code, 204)
        self.assertFalse(Organization.objects.filter(slug='patient-org').exists())
        self.assertFalse(PatientRecord.objects.filter(pk=patient.pk).exists())
        self.assertFalse(Person.objects.filter(pk=person.pk).exists())
        self.assertFalse(ConditionOccurrence.objects.filter(pk=cond.pk).exists())
        self.assertFalse(DrugExposure.objects.filter(pk=drug.pk).exists())
        self.assertFalse(ProcedureOccurrence.objects.filter(pk=proc.pk).exists())
        self.assertTrue(PatientRecord.objects.filter(pk=other_patient.pk).exists())
        self.assertTrue(Person.objects.filter(pk=other_person.pk).exists())


class OrganizationCleanupServiceTest(TestCase):
    """Shared org deletion helper must cascade patient data."""

    def test_delete_organization_with_patient_cascade(self):
        org = _make_org('Cleanup Org', 'cleanup-org')
        other_org = _make_org('Survivor Org', 'survivor-org')
        person = Person.objects.create(person_id=9101)
        other_person = Person.objects.create(person_id=9102)
        patient = PatientRecord.objects.create(person=person, organization=org)
        other_patient = PatientRecord.objects.create(person=other_person, organization=other_org)

        condition_concept = _make_test_concept(9400001, 'Cleanup Condition', 'CCOND', 'Condition')
        condition_type_concept = _make_test_concept(9400002, 'Cleanup Type', 'CTYPE', 'Type Concept')
        ProcedureOccurrence.objects.create(
            procedure_occurrence_id=9401,
            person=person,
            procedure_concept=_make_test_concept(9400003, 'Cleanup Procedure', 'CPROC', 'Procedure'),
            procedure_date=date.today(),
            procedure_type_concept=condition_type_concept,
        )
        ConditionOccurrence.objects.create(
            condition_occurrence_id=9402,
            person=person,
            condition_concept=condition_concept,
            condition_start_date=date.today(),
            condition_type_concept=condition_type_concept,
        )

        # Person-FK tables that are not reached by Django's ORM cascade when
        # Person is deleted via raw SQL — these caused FK violations on staging.
        ObservationPeriod.objects.create(
            observation_period_id=9403,
            person=person,
            observation_period_start_date=date(2020, 1, 1),
            observation_period_end_date=date.today(),
            period_type_concept=condition_type_concept,
        )
        CancerModifier.objects.create(
            cancer_modifier_id=9404,
            person=person,
            cancer_modifier_concept=condition_concept,
        )
        Histology.objects.create(
            histology_id=9405,
            person=person,
            concept=condition_concept,
            histology_date=date.today(),
            histology_type_concept=condition_type_concept,
        )
        StemTable.objects.create(
            id=9406,
            domain_id='Condition',
            person=person,
            concept=condition_concept,
            type_concept=condition_type_concept,
            start_date=date.today(),
        )
        PersonLanguageSkill.objects.create(
            person=person,
            language_concept=_make_test_concept(9400004, 'Cleanup Language', 'CLANG', 'Language'),
            skill_level='both',
        )
        survey = Survey.objects.create(name='cleanup-survey', title='Cleanup Survey')
        PatientSurveyResponse.objects.create(person=person, survey=survey)
        institution = Institution.objects.create(
            slug='cleanup-ehr', display_name='Cleanup EHR', fhir_base='https://ehr.example.com/fhir',
        )
        FhirOauthState.objects.create(
            state='cleanup-state-9407',
            person=person,
            institution=institution,
            code_verifier='verifier',
            nonce='nonce',
        )
        FhirConnection.objects.create(
            person=person,
            institution=institution,
            access_token_encrypted='enc-access',
            refresh_token_encrypted='enc-refresh',
            expires_at=timezone.now() + timedelta(hours=1),
        )

        delete_organization_with_patient_cascade(org)

        self.assertFalse(Organization.objects.filter(pk=org.pk).exists())
        self.assertFalse(PatientRecord.objects.filter(pk=patient.pk).exists())
        self.assertFalse(Person.objects.filter(pk=person.pk).exists())
        self.assertFalse(ConditionOccurrence.objects.filter(person_id=person.person_id).exists())
        self.assertFalse(ProcedureOccurrence.objects.filter(person_id=person.person_id).exists())
        self.assertFalse(ObservationPeriod.objects.filter(person_id=person.person_id).exists())
        self.assertFalse(CancerModifier.objects.filter(person_id=person.person_id).exists())
        self.assertFalse(Histology.objects.filter(person_id=person.person_id).exists())
        self.assertFalse(StemTable.objects.filter(person_id=person.person_id).exists())
        self.assertFalse(PersonLanguageSkill.objects.filter(person_id=person.person_id).exists())
        self.assertFalse(PatientSurveyResponse.objects.filter(person_id=person.person_id).exists())
        self.assertFalse(FhirOauthState.objects.filter(person_id=person.person_id).exists())
        self.assertFalse(FhirConnection.objects.filter(person_id=person.person_id).exists())
        self.assertTrue(PatientRecord.objects.filter(pk=other_patient.pk).exists())
        self.assertTrue(Person.objects.filter(pk=other_person.pk).exists())


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


class OrgViewSetTrustedAdminTest(TestCase):
    """Trust-based org access confers org-admin rights."""

    def setUp(self):
        self.client = APIClient()
        self.user = _make_user('trusted@partner.com')
        self.org = _make_org('Trusted Org', 'trusted-org')
        self.client.force_authenticate(user=self.user)

    def test_domain_trust_can_list_and_patch_org(self):
        OrgTrust.objects.create(granting_org=self.org, trusted_domain='partner.com')
        resp = self.client.get('/api/orgs/')
        self.assertEqual(resp.status_code, 200)
        self.assertIn('trusted-org', [o['slug'] for o in resp.data])

        resp = self.client.patch('/api/orgs/trusted-org/', {'name': 'Renamed Trusted Org'})
        self.assertEqual(resp.status_code, 200)
        self.org.refresh_from_db()
        self.assertEqual(self.org.name, 'Renamed Trusted Org')

    def test_org_to_org_trust_can_access_other_org(self):
        source_org = _make_org('Source Org', 'source-org')
        GroupAccess.objects.create(identity=self.user, org=source_org, role='doctor')
        OrgTrust.objects.create(granting_org=self.org, trusted_org=source_org)

        resp = self.client.get('/api/orgs/trusted-org/')
        self.assertEqual(resp.status_code, 200)


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
        self.assertTrue(resp.data['access_granted'])
        invitee = Identity.objects.get(email='newuser@example.com', issuer='urn:local')
        self.assertTrue(
            OrgInvitation.objects.filter(org=self.org, email='newuser@example.com').exists()
        )
        self.assertTrue(
            GroupAccess.objects.filter(identity=invitee, org=self.org, role='doctor').exists()
        )

    def test_invite_unknown_user_creates_placeholder_identity(self):
        resp = self.client.post('/api/orgs/invite-org/invite/', {
            'email': 'placeholder@example.com',
            'role': 'analyst',
        })
        self.assertEqual(resp.status_code, 201)
        invitee = Identity.objects.get(email='placeholder@example.com', issuer='urn:local')
        invitation = OrgInvitation.objects.get(org=self.org, email='placeholder@example.com')
        grant = GroupAccess.objects.get(identity=invitee, org=self.org, role='analyst')
        self.assertFalse(invitee.has_usable_password())
        self.assertEqual(invitation.redirect_url, 'https://analytics.healthkey.ai')
        self.assertEqual(grant.redirect_url, 'https://analytics.healthkey.ai')

    def test_invite_analyst_allows_custom_redirect_url(self):
        resp = self.client.post('/api/orgs/invite-org/invite/', {
            'email': 'analyst-custom@example.com',
            'role': 'analyst',
            'redirect_url': 'https://analytics.healthkey.ai/tenant/acme',
        })
        self.assertEqual(resp.status_code, 201)
        invitation = OrgInvitation.objects.get(org=self.org, email='analyst-custom@example.com')
        grant = GroupAccess.objects.get(identity__email='analyst-custom@example.com', org=self.org)
        self.assertEqual(invitation.redirect_url, 'https://analytics.healthkey.ai/tenant/acme')
        self.assertEqual(grant.redirect_url, 'https://analytics.healthkey.ai/tenant/acme')

    def test_invite_rejects_invalid_analyst_redirect_url(self):
        resp = self.client.post('/api/orgs/invite-org/invite/', {
            'email': 'analyst-invalid@example.com',
            'role': 'analyst',
            'redirect_url': 'not-a-url',
        })
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.data['error'], 'redirect_url must be a valid http(s) URL.')

    def test_partner_auth_identity_claims_placeholder_access(self):
        placeholder = Identity.objects.create_user(email='partner@example.com', password=None)
        GroupAccess.objects.create(identity=placeholder, org=self.org, role='doctor')

        from patient_portal.api.authentication import PartnerAuthentication
        from patient_portal.api.providers.base import TokenClaims
        claims = TokenClaims(
            issuer='https://issuer.example.com',
            sub='partner-sub',
            email='partner@example.com',
            name='Partner User',
            raw={},
        )

        identity = PartnerAuthentication._get_or_create_identity(claims)
        self.assertNotEqual(identity.pk, placeholder.pk)
        self.assertEqual(identity.email, 'partner@example.com')
        self.assertTrue(
            GroupAccess.objects.filter(identity=identity, org=self.org, role='doctor').exists()
        )
        self.assertFalse(GroupAccess.objects.filter(identity=placeholder).exists())

    def test_existing_partner_auth_identity_claims_later_placeholder_access(self):
        partner = Identity.objects.create(
            issuer='https://issuer.example.com',
            sub='existing-partner-sub',
            email='existing-partner@example.com',
        )
        partner.set_unusable_password()
        partner.save()
        placeholder = Identity.objects.create_user(email='existing-partner@example.com', password=None)
        GroupAccess.objects.create(identity=placeholder, org=self.org, role='doctor')

        from patient_portal.api.authentication import PartnerAuthentication
        from patient_portal.api.providers.base import TokenClaims
        claims = TokenClaims(
            issuer='https://issuer.example.com',
            sub='existing-partner-sub',
            email='existing-partner@example.com',
            name='Existing Partner',
            raw={},
        )

        identity = PartnerAuthentication._get_or_create_identity(claims)
        self.assertEqual(identity.pk, partner.pk)
        self.assertTrue(
            GroupAccess.objects.filter(identity=partner, org=self.org, role='doctor').exists()
        )
        self.assertFalse(GroupAccess.objects.filter(identity=placeholder).exists())

    def test_reinvite_after_placeholder_claim_updates_partner_identity(self):
        placeholder = Identity.objects.create_user(email='claimed@example.com', password=None)
        GroupAccess.objects.create(identity=placeholder, org=self.org, role='analyst')

        from patient_portal.api.authentication import PartnerAuthentication
        from patient_portal.api.providers.base import TokenClaims
        claims = TokenClaims(
            issuer='https://issuer.example.com',
            sub='claimed-sub',
            email='claimed@example.com',
            name='Claimed User',
            raw={},
        )
        partner = PartnerAuthentication._get_or_create_identity(claims)
        self.assertTrue(
            GroupAccess.objects.filter(identity=partner, org=self.org, role='analyst').exists()
        )

        resp = self.client.post('/api/orgs/invite-org/invite/', {
            'email': 'claimed@example.com',
            'role': 'doctor',
        })

        self.assertEqual(resp.status_code, 201)
        self.assertTrue(resp.data['access_granted'])
        partner_grant = GroupAccess.objects.get(identity=partner, org=self.org)
        self.assertEqual(partner_grant.role, 'doctor')
        self.assertFalse(GroupAccess.objects.filter(identity=placeholder).exists())

    def test_invite_existing_user_grants_access_immediately(self):
        invitee = Identity.objects.create_user(email='existing-user@example.com', password='pass')
        resp = self.client.post('/api/orgs/invite-org/invite/', {
            'email': 'existing-user@example.com',
            'role': 'analyst',
        })
        self.assertEqual(resp.status_code, 201)
        self.assertTrue(resp.data['access_granted'])
        self.assertTrue(
            GroupAccess.objects.filter(identity=invitee, org=self.org, role='analyst').exists()
        )

    def test_invite_existing_user_updates_existing_org_role(self):
        invitee = Identity.objects.create_user(email='role-update@example.com', password='pass')
        GroupAccess.objects.create(
            identity=invitee,
            org=self.org,
            role='analyst',
            redirect_url='https://analytics.healthkey.ai/old',
        )
        resp = self.client.post('/api/orgs/invite-org/invite/', {
            'email': 'role-update@example.com',
            'role': 'doctor',
        })
        self.assertEqual(resp.status_code, 201)
        self.assertTrue(resp.data['access_granted'])
        grant = GroupAccess.objects.get(identity=invitee, org=self.org)
        self.assertEqual(grant.role, 'doctor')
        self.assertEqual(grant.redirect_url, '')

    def test_invite_existing_user_does_not_downgrade_org_admin(self):
        invitee = Identity.objects.create_user(email='admin-role@example.com', password='pass')
        GroupAccess.objects.create(identity=invitee, org=self.org, role='org_admin')
        resp = self.client.post('/api/orgs/invite-org/invite/', {
            'email': 'admin-role@example.com',
            'role': 'doctor',
        })
        self.assertEqual(resp.status_code, 201)
        self.assertTrue(resp.data['access_granted'])
        grant = GroupAccess.objects.get(identity=invitee, org=self.org)
        self.assertEqual(grant.role, 'org_admin')

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

    def test_confirm_analyst_invitation_returns_redirect_url(self):
        from django.utils import timezone
        invitee = Identity.objects.create_user(email='analyst-invitee@example.com', password='pass')
        token = _secrets.token_hex(32)
        OrgInvitation.objects.create(
            org=self.org,
            email='analyst-invitee@example.com',
            role='analyst',
            redirect_url='https://analytics.healthkey.ai/org/acme',
            token=token,
            expires_at=timezone.now() + timezone.timedelta(days=7),
        )
        public_client = APIClient()
        resp = public_client.post('/api/orgs/confirm-invitation/', {'token': token})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data['redirect_url'], 'https://analytics.healthkey.ai/org/acme')
        grant = GroupAccess.objects.get(identity=invitee, org=self.org)
        self.assertEqual(grant.redirect_url, 'https://analytics.healthkey.ai/org/acme')

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
            'role': 'analyst',
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
                'role': 'analyst',
            })
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(
            resp.data['email_warning'],
            'Invitation was created, but the email could not be sent.',
        )
        existing.refresh_from_db()
        self.assertNotEqual(existing.token, token)
        self.assertEqual(existing.role, 'analyst')
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
        self.assertEqual(resp.data[0]['redirect_url'], None)

    def test_list_access_grants_includes_redirect_url(self):
        self.grant.role = 'analyst'
        self.grant.redirect_url = 'https://analytics.healthkey.ai/custom'
        self.grant.save(update_fields=['role', 'redirect_url'])
        resp = self.client.get('/api/orgs/access-org/access/')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data[0]['redirect_url'], 'https://analytics.healthkey.ai/custom')

    def test_revoke_access_grant(self):
        resp = self.client.delete(f'/api/orgs/access-org/access/{self.grant.id}/')
        self.assertEqual(resp.status_code, 204)
        self.assertFalse(GroupAccess.objects.filter(id=self.grant.id).exists())

    def test_patch_access_grant_sets_default_analyst_redirect_url(self):
        resp = self.client.patch(
            f'/api/orgs/access-org/access/{self.grant.id}/',
            {'role': 'analyst'},
            format='json',
        )
        self.assertEqual(resp.status_code, 200)
        self.grant.refresh_from_db()
        self.assertEqual(self.grant.role, 'analyst')
        self.assertEqual(self.grant.redirect_url, 'https://analytics.healthkey.ai')

    def test_patch_access_grant_clears_redirect_url_when_switching_to_doctor(self):
        self.grant.role = 'analyst'
        self.grant.redirect_url = 'https://analytics.healthkey.ai/custom'
        self.grant.save(update_fields=['role', 'redirect_url'])
        resp = self.client.patch(
            f'/api/orgs/access-org/access/{self.grant.id}/',
            {'role': 'doctor'},
            format='json',
        )
        self.assertEqual(resp.status_code, 200)
        self.grant.refresh_from_db()
        self.assertEqual(self.grant.redirect_url, '')


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

    def test_is_org_admin_true_with_domain_trust(self):
        OrgTrust.objects.create(granting_org=self.org, trusted_domain='example.com')
        trusted_user = _make_user('trusted@example.com')
        self.client.force_authenticate(user=trusted_user)
        resp = self.client.get('/api/user/')
        self.assertEqual(resp.status_code, 200)
        data = resp.data.get('user', resp.data)
        self.assertTrue(data.get('is_org_admin'))


# ---------------------------------------------------------------------------
# Wearable summary field tests
# ---------------------------------------------------------------------------

class WearablePatientRecordTest(TestCase):
    """_get_wearable_data aggregates OMOP Measurement/Observation into PatientRecord."""

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
        PatientRecord.objects.get_or_create(person=self.person)
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
        from omop_core.services.patient_record_service import refresh_patient_record
        from omop_core.services.concept_cache import concept_cache_clear
        concept_cache_clear()
        return refresh_patient_record(self.person)

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
        from patient_portal.api.serializers import PatientRecordSerializer

        pi = PatientRecord.objects.get(person=self.person)
        serializer = PatientRecordSerializer(
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


# ---------------------------------------------------------------------------
# Service-token ACL bypass integration tests
# ---------------------------------------------------------------------------

class ServiceTokenOmopAccessTest(TestCase):
    """
    Verify that service-token callers bypass row-level ACL checks in:
      - _OmopFilterMixin.get_queryset  (MeasurementViewSet list/retrieve)
      - _ProvenanceMixin.perform_create / perform_update  (MeasurementViewSet write)
      - PersonViewSet.partial_update
      - EpisodeEventViewSet.get_queryset + perform_create
      - PatientRecordViewSet.get_queryset

    Each test authenticates with the canonical service identity and
    token="service-token" (the same sentinel ServiceTokenAuthentication sets).
    """

    @classmethod
    def setUpTestData(cls):
        _make_vocab_fixtures()

        # Two patients in different orgs so cross-org visibility is meaningful.
        from omop_core.models import Organization
        cls.org_a = Organization.objects.create(name='ST Org A', slug='st-org-a')
        cls.org_b = Organization.objects.create(name='ST Org B', slug='st-org-b')

        cls.person_a = Person.objects.create(person_id=29001)
        cls.person_b = Person.objects.create(person_id=29002)
        PatientRecord.objects.create(person=cls.person_a, organization=cls.org_a)
        PatientRecord.objects.create(person=cls.person_b, organization=cls.org_b)

        # One measurement per patient.
        m_concept = Concept.objects.get(concept_id=3000963)   # Laboratory test result
        type_concept = Concept.objects.get(concept_id=32817)  # EHR
        cls.m_a = Measurement.objects.create(
            measurement_id=29001,
            person=cls.person_a,
            measurement_concept=m_concept,
            measurement_date=date(2024, 1, 1),
            measurement_type_concept=type_concept,
        )
        cls.m_b = Measurement.objects.create(
            measurement_id=29002,
            person=cls.person_b,
            measurement_concept=m_concept,
            measurement_date=date(2024, 2, 1),
            measurement_type_concept=type_concept,
        )

        # Episode + EpisodeEvent for person_a.
        ep_concept = Concept.objects.get(concept_id=32531)   # Treatment Regimen
        cls.ep_a = Episode.objects.create(
            episode_id=29001,
            person=cls.person_a,
            episode_concept=ep_concept,
            episode_object_concept=type_concept,
            episode_type_concept=type_concept,
            episode_start_date=date(2024, 1, 1),
            episode_number=1,
            episode_source_value='TEST-LOT',
        )
        drug_concept = Concept.objects.get(concept_id=19136160)
        cls.drug_a = DrugExposure.objects.create(
            drug_exposure_id=29001,
            person=cls.person_a,
            drug_concept=drug_concept,
            drug_exposure_start_date=date(2024, 1, 1),
            drug_type_concept=type_concept,
        )
        field_concept = Concept.objects.get(concept_id=1147094)
        cls.ee_a = EpisodeEvent.objects.create(
            episode_id=cls.ep_a.episode_id,
            event_id=cls.drug_a.drug_exposure_id,
            episode_event_field_concept=field_concept,
        )

        # Service identity — mirrors what ServiceTokenAuthentication creates.
        cls.service_identity = Identity.objects.get_or_create(
            issuer='urn:service', sub='hk-labs-sync',
        )[0]
        cls.service_identity.set_unusable_password()
        cls.service_identity.save()

    def setUp(self):
        self.client = APIClient()
        self.client.force_authenticate(
            user=self.service_identity, token="service-token"
        )

    # --- MeasurementViewSet (via _OmopFilterMixin + _ProvenanceMixin) ---

    def test_service_token_measurement_list_sees_all_orgs(self):
        """GET /api/measurements/ returns measurements from all orgs."""
        resp = self.client.get('/api/measurements/')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        rows = resp.data['results'] if isinstance(resp.data, dict) else resp.data
        returned_ids = {r['measurement_id'] for r in rows}
        self.assertIn(self.m_a.measurement_id, returned_ids)
        self.assertIn(self.m_b.measurement_id, returned_ids)

    def test_service_token_measurement_create(self):
        """POST /api/measurements/ creates without org or ACL error."""
        m_concept = Concept.objects.get(concept_id=3000963)
        type_concept = Concept.objects.get(concept_id=32817)
        resp = self.client.post('/api/measurements/', {
            'person': self.person_a.person_id,
            'measurement_concept': m_concept.concept_id,
            'measurement_date': '2024-03-01',
            'measurement_type_concept': type_concept.concept_id,
        }, format='json')
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)

    def test_service_token_measurement_update(self):
        """PATCH /api/measurements/{id}/ updates without org ACL error."""
        resp = self.client.patch(
            f'/api/measurements/{self.m_a.measurement_id}/',
            {'value_as_number': 7.5},
            format='json',
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.m_a.refresh_from_db()
        self.assertEqual(float(self.m_a.value_as_number), 7.5)

    # --- PersonViewSet.partial_update ---

    def test_service_token_person_patch_bypasses_acl(self):
        """PATCH /api/persons/{person_id}/ succeeds without can_access_patient."""
        resp = self.client.patch(
            f'/api/persons/{self.person_b.person_id}/',
            {'given_name': 'ServicePatched'},
            format='json',
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.person_b.refresh_from_db()
        self.assertEqual(self.person_b.given_name, 'ServicePatched')

    # --- EpisodeEventViewSet ---

    def test_service_token_episode_event_list(self):
        """GET /api/episode-events/?episode_id=X returns events without ACL error."""
        resp = self.client.get(
            f'/api/episode-events/?episode_id={self.ep_a.episode_id}'
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        rows = resp.data['results'] if isinstance(resp.data, dict) else resp.data
        returned_ids = {r['event_id'] for r in rows}
        self.assertIn(self.drug_a.drug_exposure_id, returned_ids)

    def test_service_token_episode_event_create(self):
        """POST /api/episode-events/ creates without PermissionDenied."""
        drug2 = DrugExposure.objects.create(
            drug_exposure_id=29099,
            person=self.person_a,
            drug_concept=Concept.objects.get(concept_id=19136160),
            drug_exposure_start_date=date(2024, 4, 1),
            drug_type_concept=Concept.objects.get(concept_id=32817),
        )
        resp = self.client.post('/api/episode-events/', {
            'episode_id': self.ep_a.episode_id,
            'event_id': drug2.drug_exposure_id,
            'episode_event_field_concept': 1147094,
        }, format='json')
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)

    # --- PatientRecordViewSet ---

    def test_service_token_patient_info_list_sees_all_orgs(self):
        """GET /api/patient-info/ returns patients from all orgs."""
        resp = self.client.get('/api/patient-info/')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        rows = resp.data['results'] if isinstance(resp.data, dict) else resp.data
        returned_pids = {r['person_id'] for r in rows}
        self.assertIn(self.person_a.person_id, returned_pids)
        self.assertIn(self.person_b.person_id, returned_pids)


class MeEndpointGuardTest(TestCase):
    """Tests for the /api/patient-info/me/ auto-provisioning guard (PR #190)."""

    @classmethod
    def setUpTestData(cls):
        import datetime
        from django.utils import timezone as tz
        from omop_core.models import Organization, GroupAccess

        _make_vocab_fixtures()

        cls.org = Organization.objects.create(name='Guard Test Org', slug='guard-test-org')

        # A confirmed patient: has PatientUser + Person
        cls.patient_user = Identity.objects.create_user(
            email='patient_me@test.com', password='pass'
        )
        cls.patient_person = Person.objects.create(
            person_id=88001,
            year_of_birth=1985,
            gender_source_value='female',
            race_source_value='unknown',
            ethnicity_source_value='unknown',
        )
        PatientRecord.objects.create(person=cls.patient_person, organization=cls.org)
        from patient_portal.models import PatientUser as PU
        PU.objects.create(identity=cls.patient_user, person=cls.patient_person)

        # Staff user with no patient record
        cls.staff_user = Identity.objects.create_user(
            email='staff_me@test.com', password='pass', is_staff=True
        )

        # Superuser with no patient record
        cls.super_user = Identity.objects.create_superuser(
            email='super_me@test.com', password='pass'
        )

        # org_admin with an active GroupAccess grant
        cls.org_admin_user = Identity.objects.create_user(
            email='orgadmin_me@test.com', password='pass'
        )
        GroupAccess.objects.create(
            identity=cls.org_admin_user, org=cls.org, role='org_admin'
        )

        # doctor with an active GroupAccess grant
        cls.doctor_user = Identity.objects.create_user(
            email='doctor_me@test.com', password='pass'
        )
        GroupAccess.objects.create(
            identity=cls.doctor_user, org=cls.org, role='doctor'
        )

        # navigator with an active GroupAccess grant
        cls.navigator_user = Identity.objects.create_user(
            email='navigator_me@test.com', password='pass'
        )
        GroupAccess.objects.create(
            identity=cls.navigator_user, org=cls.org, role='analyst'
        )

        # Clinical user whose grant is expired — should be allowed through
        cls.expired_clinical_user = Identity.objects.create_user(
            email='expired_doc_me@test.com', password='pass'
        )
        GroupAccess.objects.create(
            identity=cls.expired_clinical_user,
            org=cls.org,
            role='doctor',
            expires_at=tz.now() - datetime.timedelta(days=1),
        )

        # Staff user who is also a patient by email match (PatientUser deleted)
        cls.staff_patient_user = Identity.objects.create_user(
            email='staffpatient_me@test.com', password='pass', is_staff=True
        )
        staff_patient_person = Person.objects.create(
            person_id=88002,
            year_of_birth=1975,
            gender_source_value='male',
            race_source_value='unknown',
            ethnicity_source_value='unknown',
        )
        # PatientRecord with matching email exists, but no PatientUser row
        PatientRecord.objects.create(
            person=staff_patient_person,
            email='staffpatient_me@test.com',
            organization=cls.org,
        )

    def _client(self, user):
        c = APIClient()
        c.force_authenticate(user=user)
        return c

    def test_confirmed_patient_get_returns_200(self):
        resp = self._client(self.patient_user).get('/api/patient-info/me/')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertIn('patient_info', resp.data)

    def test_confirmed_patient_patch_returns_200(self):
        resp = self._client(self.patient_user).patch(
            '/api/patient-info/me/', {}, format='json'
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

    def test_staff_without_patient_record_returns_404(self):
        resp = self._client(self.staff_user).get('/api/patient-info/me/')
        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)

    def test_superuser_without_patient_record_returns_404(self):
        resp = self._client(self.super_user).get('/api/patient-info/me/')
        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)

    def test_org_admin_without_patient_record_returns_404(self):
        resp = self._client(self.org_admin_user).get('/api/patient-info/me/')
        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)

    def test_doctor_without_patient_record_returns_404(self):
        resp = self._client(self.doctor_user).get('/api/patient-info/me/')
        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)

    def test_navigator_without_patient_record_returns_404(self):
        resp = self._client(self.navigator_user).get('/api/patient-info/me/')
        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)

    def test_expired_clinical_grant_allows_auto_provisioning(self):
        """An expired clinical-role grant must not block patient self-registration."""
        resp = self._client(self.expired_clinical_user).get('/api/patient-info/me/')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

    def test_staff_with_email_match_returns_200(self):
        """Staff user whose email matches a PatientRecord row is re-linked, not blocked."""
        resp = self._client(self.staff_patient_user).get('/api/patient-info/me/')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

    def test_unauthenticated_returns_401_or_403(self):
        resp = APIClient().get('/api/patient-info/me/')
        self.assertIn(resp.status_code, [status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN])


class ApiVersioningTest(TestCase):
    """Tests for /api/v1/ versioned URL aliases and deprecation headers."""

    @classmethod
    def setUpTestData(cls):
        _make_vocab_fixtures()
        cls.user = Identity.objects.create_superuser(
            email='versioning_test@test.com', password='pass'
        )

    def _client(self):
        c = APIClient()
        c.force_authenticate(user=self.user)
        return c

    def test_versioned_patient_records_list_returns_200(self):
        resp = self._client().get('/api/v1/patient-records/')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

    def test_versioned_path_has_no_deprecation_header(self):
        resp = self._client().get('/api/v1/patient-records/')
        self.assertNotIn('Deprecation', resp)

    def test_legacy_path_has_deprecation_header(self):
        resp = self._client().get('/api/patient-info/')
        self.assertEqual(resp.get('Deprecation'), 'true')

    def test_legacy_path_has_sunset_header(self):
        resp = self._client().get('/api/patient-info/')
        self.assertEqual(resp.get('Sunset'), 'Tue, 01 Sep 2026 00:00:00 GMT')

    def test_legacy_path_has_link_header(self):
        resp = self._client().get('/api/patient-info/')
        link = resp.get('Link', '')
        self.assertIn('/api/v1/', link)
        self.assertIn('successor-version', link)

    def test_schema_endpoint_returns_openapi_json(self):
        resp = self._client().get('/api/v1/schema/')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertIn('openapi', resp.data)
        self.assertEqual(resp.data['info']['title'], 'PROMOP API')

    def test_schema_documents_v1_patient_records_not_legacy_patient_info(self):
        resp = self._client().get('/api/v1/schema/')
        paths = set(resp.data.get('paths', {}))
        self.assertIn('/api/v1/patient-records/', paths)
        self.assertNotIn('/api/v1/patient-info/', paths)
        self.assertFalse(
            any(path.startswith('/api/') and not path.startswith('/api/v1/') for path in paths),
            'v1 schema should not include legacy /api/ paths',
        )

    def test_schema_accessible_without_authentication(self):
        resp = APIClient().get('/api/v1/schema/')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

    def test_swagger_ui_accessible_without_authentication(self):
        resp = APIClient().get('/api/v1/docs/')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

    def test_legacy_path_has_deprecation_header_on_post(self):
        """Deprecation headers must appear on mutating requests, not just GET."""
        resp = self._client().post('/api/patient-info/upload_fhir/', {}, format='json')
        self.assertEqual(resp.get('Deprecation'), 'true')

    def test_v1_lab_results_path_has_no_deprecation_header(self):
        resp = self._client().get('/api/v1/lab-results/summary/')
        self.assertNotIn('Deprecation', resp)

    def test_legacy_lab_results_path_has_deprecation_header(self):
        resp = self._client().get('/api/lab-results/summary/')
        self.assertEqual(resp.get('Deprecation'), 'true')

    def test_v1_fhir_path_has_no_deprecation_header(self):
        resp = self._client().get('/api/v1/fhir/sync/')
        self.assertNotIn('Deprecation', resp)

    def test_legacy_fhir_path_has_deprecation_header(self):
        resp = self._client().get('/api/fhir/sync/')
        self.assertEqual(resp.get('Deprecation'), 'true')


class V1MeEndpointTest(TestCase):
    """Tests for /api/v1/patient-records/me/ — guard behaviour and no deprecation headers."""

    @classmethod
    def setUpTestData(cls):
        import datetime
        from django.utils import timezone as tz
        from omop_core.models import Organization, GroupAccess

        _make_vocab_fixtures()

        cls.org = Organization.objects.create(name='V1 Me Test Org', slug='v1-me-test-org')

        # Confirmed patient: has PatientUser + Person + PatientRecord
        cls.patient_user = Identity.objects.create_user(
            email='v1_patient_me@test.com', password='pass'
        )
        cls.patient_person = Person.objects.create(
            person_id=89001,
            year_of_birth=1990,
            gender_source_value='female',
            race_source_value='unknown',
            ethnicity_source_value='unknown',
        )
        PatientRecord.objects.create(person=cls.patient_person, organization=cls.org)
        from patient_portal.models import PatientUser as PU
        PU.objects.create(identity=cls.patient_user, person=cls.patient_person)

        # Org admin with no patient record
        cls.org_admin_user = Identity.objects.create_user(
            email='v1_orgadmin_me@test.com', password='pass'
        )
        GroupAccess.objects.create(
            identity=cls.org_admin_user, org=cls.org, role='org_admin'
        )

        # Clinical user with an expired grant (should be treated as a plain user)
        cls.expired_clinical_user = Identity.objects.create_user(
            email='v1_expired_doc_me@test.com', password='pass'
        )
        GroupAccess.objects.create(
            identity=cls.expired_clinical_user,
            org=cls.org,
            role='doctor',
            expires_at=tz.now() - datetime.timedelta(days=1),
        )

    def _client(self, user):
        c = APIClient()
        c.force_authenticate(user=user)
        return c

    # --- happy path -----------------------------------------------------------

    def test_v1_confirmed_patient_get_returns_200(self):
        resp = self._client(self.patient_user).get('/api/v1/patient-records/me/')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertIn('patient_info', resp.data)

    def test_v1_confirmed_patient_patch_returns_200(self):
        resp = self._client(self.patient_user).patch(
            '/api/v1/patient-records/me/', {}, format='json'
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

    # --- guard: clinical users without a patient record are blocked -----------

    def test_v1_org_admin_without_patient_record_returns_404(self):
        resp = self._client(self.org_admin_user).get('/api/v1/patient-records/me/')
        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)

    def test_v1_expired_clinical_grant_allows_auto_provisioning(self):
        """Expired grant must not block auto-provisioning at the v1 path."""
        resp = self._client(self.expired_clinical_user).get('/api/v1/patient-records/me/')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

    def test_v1_unauthenticated_returns_401_or_403(self):
        resp = APIClient().get('/api/v1/patient-records/me/')
        self.assertIn(resp.status_code, [status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN])

    # --- deprecation headers must be absent on v1 paths ----------------------

    def test_v1_me_get_has_no_deprecation_header(self):
        resp = self._client(self.patient_user).get('/api/v1/patient-records/me/')
        self.assertNotIn('Deprecation', resp)

    def test_v1_me_patch_has_no_deprecation_header(self):
        resp = self._client(self.patient_user).patch(
            '/api/v1/patient-records/me/', {}, format='json'
        )
        self.assertNotIn('Deprecation', resp)


# ---------------------------------------------------------------------------
# mCODE / Synthea FHIR import enhancements (branch fhir_import_enhancement)
# ---------------------------------------------------------------------------

def _make_loinc_concept(concept_id, concept_code, concept_name):
    """Create a Concept with vocabulary_id='LOINC' as required by concept_cache.py."""
    today = date.today()
    far_future = date(2099, 12, 31)
    loinc_vocab, _ = Vocabulary.objects.get_or_create(
        vocabulary_id='LOINC',
        defaults={'vocabulary_name': 'LOINC', 'vocabulary_concept_id': 0},
    )
    domain, _ = Domain.objects.get_or_create(
        domain_id='Measurement',
        defaults={'domain_name': 'Measurement', 'domain_concept_id': 21},
    )
    cc, _ = ConceptClass.objects.get_or_create(
        concept_class_id='Lab Test',
        defaults={'concept_class_name': 'Lab Test', 'concept_class_concept_id': 0},
    )
    obj, _ = Concept.objects.get_or_create(
        concept_id=concept_id,
        defaults={
            'concept_name': concept_name,
            'domain': domain,
            'vocabulary': loinc_vocab,
            'concept_class': cc,
            'concept_code': concept_code,
            'valid_start_date': today,
            'valid_end_date': far_future,
        },
    )
    return obj


def _upload_bundle_direct(admin_client, bundle):
    bundle_bytes = json.dumps(bundle).encode('utf-8')
    f = io.BytesIO(bundle_bytes)
    f.name = 'mcode_test.json'
    return admin_client.post(
        '/api/patient-info/upload_fhir/', {'file': f}, format='multipart'
    )


class USCoreRaceEthnicityTest(FhirUploadBase):
    """US Core nested race/ethnicity extensions (mCODE/Synthea style) are parsed."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        _client = APIClient()
        _client.force_authenticate(user=cls.admin)
        bundle = {
            'resourceType': 'Bundle',
            'type': 'collection',
            'entry': [{'resource': {
                'resourceType': 'Patient',
                'id': 'pt-uscore-001',
                'name': [{'family': 'USCoreRace', 'given': ['Test']}],
                'gender': 'female',
                'birthDate': '1980-06-01',
                'extension': [
                    {
                        'url': 'http://hl7.org/fhir/us/core/StructureDefinition/us-core-race',
                        'extension': [
                            {'url': 'ombCategory', 'valueCoding': {'display': 'Asian'}},
                            {'url': 'text', 'valueString': 'Asian'},
                        ],
                    },
                    {
                        'url': 'http://hl7.org/fhir/us/core/StructureDefinition/us-core-ethnicity',
                        'extension': [
                            {'url': 'ombCategory', 'valueCoding': {'display': 'Non Hispanic or Latino'}},
                            {'url': 'text', 'valueString': 'Non Hispanic or Latino'},
                        ],
                    },
                ],
            }}],
        }
        _upload_bundle_direct(_client, bundle)
        cls._pi = PatientRecord.objects.filter(
            person__family_name='USCoreRace'
        ).first()

    def test_us_core_race_parsed(self):
        self.assertIsNotNone(self._pi, 'PatientRecord not created')
        self.assertEqual(self._pi.race, 'Asian')

    def test_us_core_ethnicity_parsed(self):
        self.assertIsNotNone(self._pi, 'PatientRecord not created')
        self.assertEqual(self._pi.ethnicity, 'Non Hispanic or Latino')


class SyntheaFullUrlReferenceTest(FhirUploadBase):
    """Synthea/mCODE bundles may reference Patient entries by fullUrl urn:uuid."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        _make_loinc_concept(3051825, '38483-4', 'Creatinine [Mass/volume] in Blood')
        from omop_core.services import concept_cache
        concept_cache._cache.clear()

        patient_full_url = 'urn:uuid:synthea-patient-fullurl-001'
        _client = APIClient()
        _client.force_authenticate(user=cls.admin)
        bundle = {
            'resourceType': 'Bundle',
            'type': 'collection',
            'entry': [
                {
                    'fullUrl': patient_full_url,
                    'resource': {
                        'resourceType': 'Patient',
                        'id': 'synthea-resource-id-001',
                        'name': [{'family': 'SyntheaFullUrl', 'given': ['Test']}],
                        'gender': 'female',
                        'birthDate': '1974-02-03',
                    },
                },
                {
                    'fullUrl': 'urn:uuid:condition-001',
                    'resource': {
                        'resourceType': 'Condition',
                        'subject': {'reference': patient_full_url},
                        'code': {'coding': [{
                            'system': 'http://snomed.info/sct',
                            'code': '254837009',
                            'display': 'Malignant neoplasm of breast',
                        }]},
                        'onsetDateTime': '2020-04-15',
                    },
                },
                {
                    'fullUrl': 'urn:uuid:observation-creatinine-001',
                    'resource': {
                        'resourceType': 'Observation',
                        'status': 'final',
                        'subject': {'reference': patient_full_url},
                        'effectiveDateTime': '2023-05-01',
                        'code': {'coding': [{
                            'system': 'http://loinc.org',
                            'code': '38483-4',
                            'display': 'Creatinine [Mass/volume] in Blood',
                        }]},
                        'valueQuantity': {'value': 1.2, 'unit': 'mg/dL'},
                    },
                },
            ],
        }
        cls._resp = _upload_bundle_direct(_client, bundle)
        cls._person = Person.objects.filter(family_name='SyntheaFullUrl').first()
        cls._pi = PatientRecord.objects.filter(person=cls._person).first() if cls._person else None

    def test_upload_succeeds(self):
        self.assertEqual(self._resp.status_code, status.HTTP_200_OK)

    def test_condition_referenced_by_fullurl_is_attached(self):
        self.assertIsNotNone(self._person, 'Person not created')
        self.assertTrue(
            ConditionOccurrence.objects.filter(person=self._person).exists(),
            'Condition referencing Patient fullUrl was not attached',
        )

    def test_observation_referenced_by_fullurl_populates_patient_info(self):
        self.assertIsNotNone(self._pi, 'PatientRecord not created')
        self.assertIsNotNone(self._pi.serum_creatinine_mg_dl)
        self.assertAlmostEqual(float(self._pi.serum_creatinine_mg_dl), 1.2, places=1)


class BPPanelExpansionTest(FhirUploadBase):
    """BP panel observation (LOINC 85354-9) is expanded into systolic + diastolic Measurements."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        # Seed LOINC concepts required by concept_cache for component resolution
        _make_loinc_concept(3004249, '8480-6', 'Systolic blood pressure')
        _make_loinc_concept(3012888, '8462-4', 'Diastolic blood pressure')
        from omop_core.services import concept_cache
        concept_cache._cache.clear()

        _client = APIClient()
        _client.force_authenticate(user=cls.admin)
        bundle = {
            'resourceType': 'Bundle',
            'type': 'collection',
            'entry': [{'resource': {
                'resourceType': 'Patient',
                'id': 'pt-bp-001',
                'name': [{'family': 'BPPanel', 'given': ['Test']}],
                'gender': 'male',
                'birthDate': '1965-04-20',
            }}, {'resource': {
                'resourceType': 'Observation',
                'status': 'final',
                'subject': {'reference': 'Patient/pt-bp-001'},
                'effectiveDateTime': '2023-01-10',
                'code': {
                    'coding': [{'system': 'http://loinc.org', 'code': '85354-9',
                                'display': 'Blood pressure panel'}],
                },
                'component': [
                    {
                        'code': {'coding': [{'system': 'http://loinc.org', 'code': '8480-6',
                                             'display': 'Systolic blood pressure'}]},
                        'valueQuantity': {'value': 122.0, 'unit': 'mmHg'},
                    },
                    {
                        'code': {'coding': [{'system': 'http://loinc.org', 'code': '8462-4',
                                             'display': 'Diastolic blood pressure'}]},
                        'valueQuantity': {'value': 78.0, 'unit': 'mmHg'},
                    },
                ],
            }}],
        }
        _upload_bundle_direct(_client, bundle)
        cls._person = Person.objects.filter(family_name='BPPanel').first()
        cls._pi = PatientRecord.objects.filter(person=cls._person).first() if cls._person else None

    def test_systolic_measurement_written(self):
        from omop_core.models import Measurement
        m = Measurement.objects.filter(
            person=self._person, measurement_source_value='8480-6'
        ).first()
        self.assertIsNotNone(m, 'No Measurement for 8480-6 (systolic)')
        self.assertAlmostEqual(float(m.value_as_number), 122.0, places=1)

    def test_diastolic_measurement_written(self):
        from omop_core.models import Measurement
        m = Measurement.objects.filter(
            person=self._person, measurement_source_value='8462-4'
        ).first()
        self.assertIsNotNone(m, 'No Measurement for 8462-4 (diastolic)')
        self.assertAlmostEqual(float(m.value_as_number), 78.0, places=1)

    def test_panel_measurement_not_written(self):
        """The parent BP panel row (85354-9) must not become its own Measurement."""
        from omop_core.models import Measurement
        panel_m = Measurement.objects.filter(
            person=self._person, measurement_source_value='85354-9'
        ).first()
        self.assertIsNone(panel_m, 'Panel row 85354-9 should not be written as a Measurement')

    def test_patient_info_systolic_populated(self):
        self.assertIsNotNone(self._pi)
        self.assertIsNotNone(self._pi.systolic_blood_pressure)
        self.assertAlmostEqual(float(self._pi.systolic_blood_pressure), 122.0, places=1)

    def test_patient_info_diastolic_populated(self):
        self.assertIsNotNone(self._pi)
        self.assertIsNotNone(self._pi.diastolic_blood_pressure)
        self.assertAlmostEqual(float(self._pi.diastolic_blood_pressure), 78.0, places=1)


class BreastCancerSnomed254837009Test(FhirUploadBase):
    """Conditions coded with SNOMED 254837009 are treated as the primary cancer onset."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        _client = APIClient()
        _client.force_authenticate(user=cls.admin)
        bundle = {
            'resourceType': 'Bundle',
            'type': 'collection',
            'entry': [
                {'resource': {
                    'resourceType': 'Patient',
                    'id': 'pt-snomed-001',
                    'name': [{'family': 'SnomedBC', 'given': ['Test']}],
                    'gender': 'female',
                    'birthDate': '1970-11-01',
                }},
                # Non-cancer condition with an earlier date — should NOT win for diagnosis_date
                {'resource': {
                    'resourceType': 'Condition',
                    'subject': {'reference': 'Patient/pt-snomed-001'},
                    'code': {'coding': [{'system': 'http://snomed.info/sct',
                                         'code': '44054006', 'display': 'Type 2 diabetes'}]},
                    'onsetDateTime': '2015-03-01',
                }},
                # Breast cancer (SNOMED 254837009) — should set diagnosis_date
                {'resource': {
                    'resourceType': 'Condition',
                    'subject': {'reference': 'Patient/pt-snomed-001'},
                    'code': {'coding': [{'system': 'http://snomed.info/sct',
                                         'code': '254837009',
                                         'display': 'Malignant neoplasm of breast'}]},
                    'onsetDateTime': '2021-07-15',
                    'stage': [{'summary': {'text': 'Stage IIIA'}}],
                }},
                # Another non-cancer condition after the breast cancer — should NOT override
                {'resource': {
                    'resourceType': 'Condition',
                    'subject': {'reference': 'Patient/pt-snomed-001'},
                    'code': {'coding': [{'system': 'http://snomed.info/sct',
                                         'code': '73211009', 'display': 'Hypertension'}]},
                    'onsetDateTime': '2022-01-01',
                }},
            ],
        }
        _upload_bundle_direct(_client, bundle)
        cls._pi = PatientRecord.objects.filter(person__family_name='SnomedBC').first()

    def test_disease_populated(self):
        self.assertIsNotNone(self._pi, 'PatientRecord not created')
        self.assertIsNotNone(self._pi.disease)

    def test_diagnosis_date_is_breast_cancer_onset(self):
        """diagnosis_date must be 2021-07-15 (the breast cancer onset), not 2022-01-01."""
        self.assertIsNotNone(self._pi)
        self.assertEqual(self._pi.diagnosis_date, date(2021, 7, 15))

    def test_stage_from_breast_cancer_condition(self):
        self.assertIsNotNone(self._pi)
        self.assertIn('IIIA', self._pi.stage or '')


class MCODECreatinineLoincTest(FhirUploadBase):
    """mCODE uses LOINC 38483-4 (Creatinine in Blood) instead of 2160-0."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        _client = APIClient()
        _client.force_authenticate(user=cls.admin)
        bundle = {
            'resourceType': 'Bundle',
            'type': 'collection',
            'entry': [
                {'resource': {
                    'resourceType': 'Patient',
                    'id': 'pt-creat-001',
                    'name': [{'family': 'MCODECreat', 'given': ['Test']}],
                    'gender': 'female',
                    'birthDate': '1968-08-22',
                }},
                {'resource': {
                    'resourceType': 'Observation',
                    'status': 'final',
                    'subject': {'reference': 'Patient/pt-creat-001'},
                    'effectiveDateTime': '2023-05-01',
                    'code': {
                        'coding': [{'system': 'http://loinc.org', 'code': '38483-4',
                                    'display': 'Creatinine [Mass/volume] in Blood'}],
                    },
                    'valueQuantity': {'value': 1.1, 'unit': 'mg/dL'},
                }},
            ],
        }
        _upload_bundle_direct(_client, bundle)
        cls._pi = PatientRecord.objects.filter(person__family_name='MCODECreat').first()

    def test_creatinine_populated_from_loinc_38483_4(self):
        self.assertIsNotNone(self._pi, 'PatientRecord not created')
        self.assertIsNotNone(self._pi.serum_creatinine_mg_dl,
                             'serum_creatinine_mg_dl not populated from LOINC 38483-4')
        self.assertAlmostEqual(float(self._pi.serum_creatinine_mg_dl), 1.1, places=1)


# ---------------------------------------------------------------------------
# Phase 1: Patient / PHR Account Holder role surface (issue #264, FM PH.1)
# ---------------------------------------------------------------------------

class PatientRolePersonForTest(TestCase):
    """Unit tests for patient_portal.services.patient_person_for."""

    @classmethod
    def setUpTestData(cls):
        from patient_portal.models import PatientUser
        from omop_core.models import GroupAccess, Organization

        # A patient: PatientUser link, no provider grant.
        cls.person = Person.objects.create(person_id=90101, family_name='Holder', given_name='Pat')
        PatientRecord.objects.create(person=cls.person)
        cls.patient_identity = Identity.objects.create_user(email='holder@test.com', password='pw')
        PatientUser.objects.create(identity=cls.patient_identity, person=cls.person)

        # A provider: PatientUser link exists BUT also has an org_admin grant.
        cls.person_prov = Person.objects.create(person_id=90102, family_name='Doc', given_name='Dee')
        PatientRecord.objects.create(person=cls.person_prov)
        cls.provider_identity = Identity.objects.create_user(email='doc@test.com', password='pw')
        PatientUser.objects.create(identity=cls.provider_identity, person=cls.person_prov)
        cls.org = Organization.objects.create(name='Acme Onc', slug='acme-onc')
        GroupAccess.objects.create(identity=cls.provider_identity, org=cls.org, role='org_admin')

        # A plain identity with no PatientUser at all.
        cls.orphan_identity = Identity.objects.create_user(email='orphan@test.com', password='pw')

        # Staff/superuser with a PatientUser link should still not be a patient.
        cls.person_staff = Person.objects.create(person_id=90103, family_name='Staff', given_name='Sam')
        cls.staff_identity = Identity.objects.create_user(email='staff264@test.com', password='pw', is_staff=True)
        PatientUser.objects.create(identity=cls.staff_identity, person=cls.person_staff)

    def test_patient_identity_resolves_to_own_person(self):
        from patient_portal.services import patient_person_for
        self.assertEqual(patient_person_for(self.patient_identity), self.person)

    def test_provider_with_group_access_is_not_a_patient(self):
        from patient_portal.services import patient_person_for
        self.assertIsNone(patient_person_for(self.provider_identity))

    def test_identity_without_patient_user_is_not_a_patient(self):
        from patient_portal.services import patient_person_for
        self.assertIsNone(patient_person_for(self.orphan_identity))

    def test_staff_is_not_a_patient(self):
        from patient_portal.services import patient_person_for
        self.assertIsNone(patient_person_for(self.staff_identity))

    def test_none_identity_is_not_a_patient(self):
        from patient_portal.services import patient_person_for
        self.assertIsNone(patient_person_for(None))


class PatientRoleUserEndpointTest(TestCase):
    """/api/v1/user/ exposes is_patient and person_id (issue #264)."""

    @classmethod
    def setUpTestData(cls):
        from patient_portal.models import PatientUser
        from omop_core.models import GroupAccess, Organization

        cls.person = Person.objects.create(person_id=90201, family_name='Holder', given_name='Pat')
        PatientRecord.objects.create(person=cls.person)
        cls.patient_identity = Identity.objects.create_user(email='p264@test.com', password='pw')
        PatientUser.objects.create(identity=cls.patient_identity, person=cls.person)

        cls.provider_identity = Identity.objects.create_user(email='d264@test.com', password='pw')
        cls.org = Organization.objects.create(name='Beta Onc', slug='beta-onc')
        GroupAccess.objects.create(identity=cls.provider_identity, org=cls.org, role='org_admin')

    def _client_as(self, identity):
        c = APIClient()
        c.force_authenticate(user=identity)
        return c

    def test_patient_user_endpoint_reports_is_patient_and_person_id(self):
        resp = self._client_as(self.patient_identity).get('/api/v1/user/')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        user = resp.data['user']
        self.assertTrue(user['is_patient'])
        self.assertEqual(user['person_id'], self.person.person_id)

    def test_provider_user_endpoint_reports_not_patient(self):
        resp = self._client_as(self.provider_identity).get('/api/v1/user/')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        user = resp.data['user']
        self.assertFalse(user['is_patient'])
        self.assertIsNone(user['person_id'])
        self.assertTrue(user['is_org_admin'])


# ---------------------------------------------------------------------------
# Patient invitations — staff invite a patient to claim their record (#264)
# ---------------------------------------------------------------------------

from django.core import mail as _django_mail  # noqa: E402
from django.test import override_settings  # noqa: E402


@override_settings(
    EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend',
    APP_BASE_URL='https://app.test',
)
class PatientInvitationTest(TestCase):
    """Staff invite a patient; the patient sets a password and gets an account."""

    @classmethod
    def setUpTestData(cls):
        from patient_portal.models import PatientUser

        cls.person = Person.objects.create(person_id=91001, family_name='Reed', given_name='Rae')
        cls.record = PatientRecord.objects.create(person=cls.person)  # no email yet

        cls.staff = Identity.objects.create_user(email='staff-inv@test.com', password='pw', is_staff=True)

        # An unrelated patient (no access to cls.person) for negative tests.
        cls.other_person = Person.objects.create(person_id=91002, family_name='Doe', given_name='Dot')
        PatientRecord.objects.create(person=cls.other_person)
        cls.other_patient = Identity.objects.create_user(email='other@test.com', password='pw')
        PatientUser.objects.create(identity=cls.other_patient, person=cls.other_person)

    def setUp(self):
        _django_mail.outbox = []

    def _client_as(self, identity):
        c = APIClient()
        c.force_authenticate(user=identity)
        return c

    def _invite_url(self, person):
        return f'/api/v1/patients/{person.person_id}/invite/'

    # --- Invite creation ---

    def test_staff_invite_sets_email_creates_invitation_and_sends_email(self):
        resp = self._client_as(self.staff).post(
            self._invite_url(self.person), {'email': 'Rae@Example.com'}, format='json'
        )
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED, resp.data)
        from patient_portal.models import PatientInvitation
        inv = PatientInvitation.objects.get(person=self.person)
        self.assertEqual(inv.status, 'pending')
        self.assertEqual(inv.email, 'rae@example.com')
        self.record.refresh_from_db()
        self.assertEqual(self.record.email, 'rae@example.com')
        self.assertEqual(len(_django_mail.outbox), 1)
        self.assertIn(inv.token, _django_mail.outbox[0].body)
        self.assertIn('accept-patient-invite', _django_mail.outbox[0].body)

    def test_invite_without_any_email_is_rejected(self):
        resp = self._client_as(self.staff).post(self._invite_url(self.person), {}, format='json')
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_invite_uses_stored_email_when_none_provided(self):
        self.record.email = 'stored@example.com'
        self.record.save(update_fields=['email'])
        resp = self._client_as(self.staff).post(self._invite_url(self.person), {}, format='json')
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED, resp.data)
        self.assertEqual(resp.data['email'], 'stored@example.com')

    def test_reinvite_refreshes_token_without_duplicating(self):
        c = self._client_as(self.staff)
        c.post(self._invite_url(self.person), {'email': 'rae@example.com'}, format='json')
        c.post(self._invite_url(self.person), {'email': 'rae@example.com'}, format='json')
        from patient_portal.models import PatientInvitation
        self.assertEqual(PatientInvitation.objects.filter(person=self.person).count(), 1)

    def test_unprivileged_user_cannot_invite(self):
        resp = self._client_as(self.other_patient).post(
            self._invite_url(self.person), {'email': 'rae@example.com'}, format='json'
        )
        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)
        self.assertEqual(len(_django_mail.outbox), 0)

    def test_cannot_invite_patient_with_active_account(self):
        from patient_portal.models import PatientUser
        existing = Identity.objects.create_user(email='rae@example.com', password='pw')
        PatientUser.objects.create(identity=existing, person=self.person, is_active=True)
        resp = self._client_as(self.staff).post(
            self._invite_url(self.person), {'email': 'rae@example.com'}, format='json'
        )
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    # --- Lookup ---

    def test_lookup_returns_email_and_patient_name(self):
        self._client_as(self.staff).post(
            self._invite_url(self.person), {'email': 'rae@example.com'}, format='json'
        )
        from patient_portal.models import PatientInvitation
        token = PatientInvitation.objects.get(person=self.person).token
        resp = APIClient().get('/api/v1/patient-invitations/lookup/', {'token': token})
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data['email'], 'rae@example.com')
        self.assertEqual(resp.data['patient_name'], 'Rae Reed')

    def test_lookup_rejects_bad_token(self):
        resp = APIClient().get('/api/v1/patient-invitations/lookup/', {'token': 'nope'})
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    # --- Accept ---

    def _create_invite(self, email='rae@example.com'):
        self._client_as(self.staff).post(self._invite_url(self.person), {'email': email}, format='json')
        from patient_portal.models import PatientInvitation
        return PatientInvitation.objects.get(person=self.person)

    def test_accept_creates_account_and_links_patient_user(self):
        from patient_portal.models import PatientUser
        from patient_portal.services import patient_person_for
        inv = self._create_invite()
        resp = APIClient().post(
            '/api/v1/patient-invitations/accept/',
            {'token': inv.token, 'password': 'sup3rsecret'}, format='json',
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.data)
        inv.refresh_from_db()
        self.assertEqual(inv.status, 'accepted')
        pu = PatientUser.objects.get(person=self.person)
        self.assertTrue(pu.identity.has_usable_password())
        self.assertTrue(pu.identity.check_password('sup3rsecret'))
        # The new account is a first-class patient.
        self.assertEqual(patient_person_for(pu.identity), self.person)

    def test_accept_rejects_short_password(self):
        inv = self._create_invite()
        resp = APIClient().post(
            '/api/v1/patient-invitations/accept/',
            {'token': inv.token, 'password': 'short'}, format='json',
        )
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_accept_rejects_unknown_token(self):
        resp = APIClient().post(
            '/api/v1/patient-invitations/accept/',
            {'token': 'a' * 64, 'password': 'sup3rsecret'}, format='json',
        )
        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)

    def test_accept_rejects_expired_invitation(self):
        inv = self._create_invite()
        inv.expires_at = timezone.now() - timedelta(days=1)
        inv.save(update_fields=['expires_at'])
        resp = APIClient().post(
            '/api/v1/patient-invitations/accept/',
            {'token': inv.token, 'password': 'sup3rsecret'}, format='json',
        )
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_accept_cannot_be_replayed(self):
        inv = self._create_invite()
        body = {'token': inv.token, 'password': 'sup3rsecret'}
        APIClient().post('/api/v1/patient-invitations/accept/', body, format='json')
        resp = APIClient().post('/api/v1/patient-invitations/accept/', body, format='json')
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_accept_does_not_overwrite_existing_real_account(self):
        """A pre-existing local account with a real password must not be reset by accept."""
        existing = Identity.objects.create_user(email='rae@example.com', password='original-pw')
        inv = self._create_invite('rae@example.com')
        resp = APIClient().post(
            '/api/v1/patient-invitations/accept/',
            {'token': inv.token, 'password': 'attacker-chosen'}, format='json',
        )
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        existing.refresh_from_db()
        self.assertTrue(existing.check_password('original-pw'))
        self.assertFalse(existing.check_password('attacker-chosen'))
        inv.refresh_from_db()
        self.assertEqual(inv.status, 'pending')  # not consumed

    def test_accept_claims_placeholder_account(self):
        """A placeholder local account (no usable password) is claimed and gets the new password."""
        from patient_portal.models import PatientUser
        placeholder = Identity.objects.create_user(email='rae@example.com', password=None)
        placeholder.set_unusable_password()
        placeholder.save(update_fields=['password'])
        inv = self._create_invite('rae@example.com')
        resp = APIClient().post(
            '/api/v1/patient-invitations/accept/',
            {'token': inv.token, 'password': 'sup3rsecret'}, format='json',
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.data)
        placeholder.refresh_from_db()
        self.assertTrue(placeholder.check_password('sup3rsecret'))
        self.assertEqual(PatientUser.objects.get(person=self.person).identity, placeholder)

    # --- Email editable (lock-in) ---

    def test_email_is_editable_via_patch(self):
        resp = self._client_as(self.staff).patch(
            f'/api/patient-info/{self.person.person_id}/',
            {'email': 'edited@example.com'}, format='json',
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK, getattr(resp, 'data', None))
        self.record.refresh_from_db()
        self.assertEqual(self.record.email, 'edited@example.com')


# ---------------------------------------------------------------------------
# Patient signup — a trusted app creates a patient account in an org (#264, "A")
# ---------------------------------------------------------------------------

class PatientSignupTest(TestCase):
    """POST /api/v1/patients/signup/ — server-to-server patient account creation."""

    SIGNUP_URL = '/api/v1/patients/signup/'

    @classmethod
    def setUpTestData(cls):
        from omop_core.models import Organization
        from patient_portal.models import PatientUser
        cls.org = Organization.objects.create(name='Acme Oncology', slug='acme-onc')
        cls.staff = Identity.objects.create_user(email='staff-signup@test.com', password='pw', is_staff=True)

        # A plain patient (not privileged) for the negative test.
        cls.person = Person.objects.create(person_id=92001, family_name='Pat', given_name='Pat')
        cls.patient = Identity.objects.create_user(email='pat-signup@test.com', password='pw')
        PatientUser.objects.create(identity=cls.patient, person=cls.person)

    def _staff(self):
        c = APIClient()
        c.force_authenticate(user=self.staff)
        return c

    def test_staff_signup_local_creates_account_in_org(self):
        from patient_portal.models import PatientUser
        resp = self._staff().post(self.SIGNUP_URL, {
            'org': 'acme-onc', 'email': 'newpt@example.com', 'password': 'sup3rsecret',
            'given_name': 'New', 'family_name': 'Patient',
        }, format='json')
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED, resp.data)
        self.assertTrue(resp.data['created'])
        pid = resp.data['person_id']
        pu = PatientUser.objects.get(person__person_id=pid)
        self.assertTrue(pu.identity.has_usable_password())
        self.assertTrue(pu.identity.check_password('sup3rsecret'))
        record = PatientRecord.objects.get(person__person_id=pid)
        self.assertEqual(record.organization, self.org)
        self.assertEqual(record.email, 'newpt@example.com')

    def test_staff_signup_oidc_creates_linked_identity(self):
        resp = self._staff().post(self.SIGNUP_URL, {
            'org': 'acme-onc',
            'actor_iss': 'https://idp.example.com', 'actor_sub': 'oidc-123',
            'email': 'oidc@example.com',
        }, format='json')
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED, resp.data)
        identity = Identity.objects.get(issuer='https://idp.example.com', sub='oidc-123')
        # OIDC accounts authenticate via their IdP, not a local password.
        self.assertFalse(identity.has_usable_password())
        record = PatientRecord.objects.get(person__person_id=resp.data['person_id'])
        self.assertEqual(record.organization, self.org)

    def test_signup_is_idempotent_on_repeat_identity(self):
        body = {'org': 'acme-onc', 'actor_iss': 'https://idp.example.com', 'actor_sub': 'dup-1'}
        first = self._staff().post(self.SIGNUP_URL, body, format='json')
        self.assertEqual(first.status_code, status.HTTP_201_CREATED)
        second = self._staff().post(self.SIGNUP_URL, body, format='json')
        self.assertEqual(second.status_code, status.HTTP_200_OK)
        self.assertFalse(second.data['created'])
        self.assertEqual(first.data['person_id'], second.data['person_id'])
        self.assertEqual(
            Identity.objects.filter(issuer='https://idp.example.com', sub='dup-1').count(), 1
        )

    def test_signup_requires_identity_anchor(self):
        resp = self._staff().post(self.SIGNUP_URL, {'org': 'acme-onc', 'email': 'noanchor@example.com'}, format='json')
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_signup_rejects_short_password(self):
        resp = self._staff().post(self.SIGNUP_URL, {
            'org': 'acme-onc', 'email': 'shortpw@example.com', 'password': 'short',
        }, format='json')
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_staff_signup_requires_org_when_none_bound(self):
        resp = self._staff().post(self.SIGNUP_URL, {
            'email': 'noorg@example.com', 'password': 'sup3rsecret',
        }, format='json')
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_regular_patient_cannot_signup(self):
        c = APIClient()
        c.force_authenticate(user=self.patient)
        resp = c.post(self.SIGNUP_URL, {
            'org': 'acme-onc', 'email': 'x@example.com', 'password': 'sup3rsecret',
        }, format='json')
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)


# ---------------------------------------------------------------------------
# PHR-S FM TI.2 — persisted audit events + review API (issue #295)
# ---------------------------------------------------------------------------

class AuditTrailTI2Test(_SmartBase):
    """Audit events are persisted to the DB and reviewable via /api/v1/audit-events/."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        from patient_portal.models import PatientUser
        cls.staff = Identity.objects.create_user(email='auditor@test.com', password='pw', is_staff=True)
        cls.pt_person = Person.objects.create(person_id=71001, given_name='Pat', family_name='Ient')
        cls.patient_identity = Identity.objects.create_user(email='pat-audit@test.com', password='pw')
        PatientUser.objects.create(identity=cls.patient_identity, person=cls.pt_person)

    def setUp(self):
        from patient_portal.models import AuditEvent
        AuditEvent.objects.all().delete()

    def _staff_client(self):
        c = APIClient()
        c.force_authenticate(user=self.staff)
        return c

    def _patient_client(self):
        c = APIClient()
        c.force_authenticate(user=self.patient_identity)
        return c

    def _mk_event(self, **kw):
        from patient_portal.models import AuditEvent
        defaults = dict(event_type=AuditEvent.EVENT_VIEW, method='GET', path='/api/x/', status_code=200)
        defaults.update(kw)
        return AuditEvent.objects.create(**defaults)

    # --- classification (unit) ---

    def test_event_type_classification(self):
        from django.test import RequestFactory
        from patient_portal.api.middleware import _classify_event_type
        rf = RequestFactory()
        self.assertEqual(_classify_event_type(rf.post('/api/v1/auth/login/')), 'auth')
        self.assertEqual(_classify_event_type(rf.post('/api/v1/patients/signup/')), 'auth')
        self.assertEqual(_classify_event_type(rf.post('/o/token/')), 'auth')
        self.assertEqual(_classify_event_type(rf.post('/api/fhir/patient-consent/')), 'consent')
        self.assertEqual(_classify_event_type(rf.get('/api/v1/patient-records/')), 'record_view')
        self.assertEqual(_classify_event_type(rf.post('/api/v1/measurements/')), 'record_create')
        self.assertEqual(_classify_event_type(rf.patch('/api/v1/patient-records/1/')), 'record_update')
        self.assertEqual(_classify_event_type(rf.delete('/api/v1/measurements/1/')), 'record_delete')

    # --- persistence via middleware ---

    def test_get_persists_record_view_event(self):
        from patient_portal.models import AuditEvent
        self.read_client.get('/api/patient-info/')
        ev = AuditEvent.objects.filter(method='GET').latest('id')
        self.assertEqual(ev.event_type, 'record_view')
        self.assertIn('/api/patient-info', ev.path)
        self.assertEqual(ev.status_code, 200)
        self.assertEqual(ev.client_id, 'foundation-client-id')
        self.assertEqual(ev.user_id, str(self.foundation_user.pk))
        self.assertIsNotNone(ev.duration_ms)

    def test_patch_persists_record_update_event(self):
        from patient_portal.models import AuditEvent
        person = Person.objects.create(person_id=71010)
        pi = PatientRecord.objects.create(person=person, organization=self.organization)
        self.write_client.patch(f'/api/patient-info/{pi.pk}/', {'ecog_status': '1'}, format='json')
        ev = AuditEvent.objects.filter(method='PATCH').latest('id')
        self.assertEqual(ev.event_type, 'record_update')

    def test_post_persists_record_create_event(self):
        from patient_portal.models import AuditEvent
        payload = {
            'person': self.person.pk,
            'measurement_concept': self.type_concept.pk,
            'measurement_date': '2024-01-01',
            'measurement_type_concept': self.type_concept.pk,
            'measurement_id': 71901,
        }
        self.write_client.post('/api/measurements/', payload, format='json')
        ev = AuditEvent.objects.filter(method='POST').latest('id')
        self.assertEqual(ev.event_type, 'record_create')

    def test_delete_persists_record_delete_event(self):
        from omop_core.models import Measurement
        from patient_portal.models import AuditEvent
        m = Measurement.objects.create(
            measurement_id=71902, person=self.person, measurement_concept=self.type_concept,
            measurement_date='2024-01-01', measurement_type_concept=self.type_concept,
        )
        self.write_client.delete(f'/api/measurements/{m.measurement_id}/')
        ev = AuditEvent.objects.filter(method='DELETE').latest('id')
        self.assertEqual(ev.event_type, 'record_delete')

    def test_audit_log_access_is_itself_audited(self):
        """Accessing the audit trail is logged as audit_review (TI.2.2#04)."""
        from patient_portal.models import AuditEvent
        self._staff_client().get('/api/v1/audit-events/')
        rows = AuditEvent.objects.filter(path__contains='audit-events')
        self.assertGreaterEqual(rows.count(), 1)
        self.assertEqual(rows.latest('id').event_type, 'audit_review')

    def test_should_audit_scope_rules(self):
        from django.test import RequestFactory
        from patient_portal.api.middleware import _should_audit
        rf = RequestFactory()
        self.assertTrue(_should_audit(rf.get('/api/patient-info/')))
        self.assertTrue(_should_audit(rf.post('/o/token/')))
        self.assertTrue(_should_audit(rf.get('/api/v1/audit-events/')))    # audit-log access IS audited
        self.assertTrue(_should_audit(rf.post('/admin/patient_portal/identity/1/change/')))  # admin
        self.assertFalse(_should_audit(rf.get('/')))              # SPA / non-API
        self.assertFalse(_should_audit(rf.get('/static/app.js')))  # static asset
        self.assertFalse(_should_audit(rf.options('/api/patient-info/')))  # preflight

    # --- review API scoping ---

    def test_unauthenticated_cannot_review(self):
        resp = APIClient().get('/api/v1/audit-events/')
        self.assertIn(resp.status_code, (status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN))

    def test_staff_sees_all_events(self):
        self._mk_event(user_id='111')
        self._mk_event(user_id='222')
        resp = self._staff_client().get('/api/v1/audit-events/')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        user_ids = {r['user_id'] for r in resp.data['results']}
        self.assertIn('111', user_ids)
        self.assertIn('222', user_ids)

    def test_patient_sees_only_own_events(self):
        own = str(self.patient_identity.pk)
        self._mk_event(user_id=own)
        self._mk_event(user_id='999999')
        resp = self._patient_client().get('/api/v1/audit-events/')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        returned = {r['user_id'] for r in resp.data['results']}
        self.assertEqual(returned, {own})

    # --- filters ---

    def test_filter_by_event_type(self):
        from patient_portal.models import AuditEvent
        self._mk_event(user_id='111', event_type=AuditEvent.EVENT_VIEW)
        self._mk_event(user_id='111', event_type=AuditEvent.EVENT_DELETE, method='DELETE')
        resp = self._staff_client().get('/api/v1/audit-events/', {'event_type': 'record_delete'})
        types = {r['event_type'] for r in resp.data['results']}
        self.assertEqual(types, {'record_delete'})

    def test_filter_by_method_is_case_insensitive(self):
        self._mk_event(user_id='111', method='GET')
        self._mk_event(user_id='111', method='POST', event_type='record_create')
        resp = self._staff_client().get('/api/v1/audit-events/', {'method': 'post'})
        methods = {r['method'] for r in resp.data['results']}
        self.assertEqual(methods, {'POST'})

    def test_filter_by_user_id_privileged_only(self):
        self._mk_event(user_id='111')
        self._mk_event(user_id='222')
        resp = self._staff_client().get('/api/v1/audit-events/', {'user_id': '222'})
        returned = {r['user_id'] for r in resp.data['results']}
        self.assertEqual(returned, {'222'})

    def test_filter_by_timestamp_window(self):
        from datetime import timedelta
        from django.utils import timezone as tz
        now = tz.now()
        self._mk_event(user_id='111', timestamp=now - timedelta(days=3))
        self._mk_event(user_id='111', timestamp=now)
        cutoff = (now - timedelta(days=1)).isoformat()
        resp = self._staff_client().get('/api/v1/audit-events/', {'after': cutoff})
        self.assertEqual(len(resp.data['results']), 1)

    # --- resilience ---

    def test_db_write_failure_does_not_block_response(self):
        from unittest.mock import patch as mock_patch
        person = Person.objects.create(person_id=71020)
        pi = PatientRecord.objects.create(person=person, organization=self.organization)
        with mock_patch(
            'patient_portal.api.middleware.AuditLogMiddleware._persist',
            side_effect=RuntimeError('db down'),
        ):
            resp = self.write_client.patch(f'/api/patient-info/{pi.pk}/', {'ecog_status': '1'}, format='json')
        self.assertIn(resp.status_code, range(200, 600))


class ConceptSynonymApiTest(_SmartBase):
    """GET /api/v1/concepts/{id}/synonyms/ and /api/v1/concepts/synonyms/ (promop#239)."""

    def setUp(self):
        from omop_core.models import Concept, ConceptSynonym, Vocabulary, Domain, ConceptClass
        import datetime
        today, future = datetime.date(1970, 1, 1), datetime.date(2099, 12, 31)
        hemonc, _ = Vocabulary.objects.get_or_create(
            vocabulary_id='HemOnc',
            defaults={'vocabulary_name': 'HemOnc', 'vocabulary_concept_id': 0},
        )
        rxnorm, _ = Vocabulary.objects.get_or_create(
            vocabulary_id='RxNorm',
            defaults={'vocabulary_name': 'RxNorm', 'vocabulary_concept_id': 0},
        )
        drug, _ = Domain.objects.get_or_create(
            domain_id='Drug', defaults={'domain_name': 'Drug', 'domain_concept_id': 13})
        regimen, _ = ConceptClass.objects.get_or_create(
            concept_class_id='Regimen',
            defaults={'concept_class_name': 'Regimen', 'concept_class_concept_id': 0})
        ingredient, _ = ConceptClass.objects.get_or_create(
            concept_class_id='Ingredient',
            defaults={'concept_class_name': 'Ingredient', 'concept_class_concept_id': 0})

        def _c(cid, name, vocab, cc, code):
            return Concept.objects.create(
                concept_id=cid, concept_name=name, domain=drug, vocabulary=vocab,
                concept_class=cc, standard_concept='S', concept_code=code,
                valid_start_date=today, valid_end_date=future)

        self.lang = _c(4180186, 'English language', hemonc, ingredient, 'ENG')
        self.regimen = _c(7001, 'Bortezomib, Lenalidomide, Dexamethasone', hemonc, regimen, 'HO-VRD')
        self.other = _c(7002, 'bortezomib', rxnorm, ingredient, 'RX-VELC')
        for name in ('VRd', 'RVD'):
            ConceptSynonym.objects.create(
                concept=self.regimen, concept_synonym_name=name, language_concept=self.lang)
        ConceptSynonym.objects.create(
            concept=self.other, concept_synonym_name='VRd generic', language_concept=self.lang)

    def _auth(self):
        return {'HTTP_AUTHORIZATION': f'Bearer {self.read_token.token}'}

    # --- per-concept synonyms ---
    def test_per_concept_synonyms_returns_all(self):
        resp = self.client.get('/api/v1/concepts/7001/synonyms/', **self._auth())
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        body = resp.json()
        self.assertEqual(body['concept_id'], 7001)
        self.assertEqual(body['count'], 2)
        names = {r['concept_synonym_name'] for r in body['results']}
        self.assertEqual(names, {'VRd', 'RVD'})

    def test_per_concept_synonyms_unknown_concept_404(self):
        resp = self.client.get('/api/v1/concepts/999999/synonyms/', **self._auth())
        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)

    def test_per_concept_synonyms_existing_but_empty_returns_count_zero(self):
        # concept 4180186 exists but has no synonyms → 200 with count 0 (not 404)
        resp = self.client.get('/api/v1/concepts/4180186/synonyms/', **self._auth())
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.json()['count'], 0)
        self.assertEqual(resp.json()['results'], [])

    # --- synonym search (alias -> concept) ---
    def test_synonym_search_finds_concept_by_alias(self):
        resp = self.client.get('/api/v1/concepts/synonyms/?q=VRd', **self._auth())
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        results = resp.json()['results']
        match = next((r for r in results if r['concept_synonym_name'] == 'VRd'), None)
        self.assertIsNotNone(match)
        self.assertEqual(match['concept_id'], 7001)
        self.assertEqual(match['vocabulary_id'], 'HemOnc')

    def test_synonym_search_filtered_by_vocabulary(self):
        # 'VRd' matches concept 7001 (HemOnc) and 'VRd generic' concept 7002 (RxNorm);
        # the vocabulary_id filter keeps only the HemOnc match.
        resp = self.client.get(
            '/api/v1/concepts/synonyms/?q=VRd&vocabulary_id=HemOnc', **self._auth())
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        ids = {r['concept_id'] for r in resp.json()['results']}
        self.assertIn(7001, ids)
        self.assertNotIn(7002, ids)
        self.assertTrue(all(r['vocabulary_id'] == 'HemOnc' for r in resp.json()['results']))

    def test_synonym_search_filtered_by_concept_class(self):
        resp = self.client.get(
            '/api/v1/concepts/synonyms/?q=VRd&concept_class_id=Regimen', **self._auth())
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        ids = {r['concept_id'] for r in resp.json()['results']}
        self.assertIn(7001, ids)          # Regimen
        self.assertNotIn(7002, ids)       # Ingredient, excluded by the filter

    def test_synonym_search_short_q_returns_400(self):
        # 2 chars < 3-char trigram minimum
        resp = self.client.get('/api/v1/concepts/synonyms/?q=vr', **self._auth())
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_synonym_search_unauthenticated_401(self):
        resp = self.client.get('/api/v1/concepts/synonyms/?q=VRd')
        self.assertIn(resp.status_code,
                      [status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN])


# ---------------------------------------------------------------------------
# PatientSelfScopePermission tests
# ---------------------------------------------------------------------------

class PatientSelfScopePermissionTest(TestCase):
    """Test that PatientSelfScopePermission blocks cross-patient object access."""

    @classmethod
    def setUpTestData(cls):
        from patient_portal.models import PatientUser
        from omop_core.models import GroupAccess, Organization

        _make_vocab_fixtures()
        condition_concept = Concept.objects.get(concept_id=4112853)
        type_concept = Concept.objects.get(concept_id=32817)

        # Patient A
        cls.person_a = Person.objects.create(person_id=93001, family_name='Able', given_name='Amy')
        cls.patient_a_rec = PatientRecord.objects.create(person=cls.person_a)
        cls.identity_a = Identity.objects.create_user(email='scope-a@test.com', password='pw')
        PatientUser.objects.create(identity=cls.identity_a, person=cls.person_a)

        # Patient B
        cls.person_b = Person.objects.create(person_id=93002, family_name='Baker', given_name='Bob')
        cls.patient_b_rec = PatientRecord.objects.create(person=cls.person_b)
        cls.identity_b = Identity.objects.create_user(email='scope-b@test.com', password='pw')
        PatientUser.objects.create(identity=cls.identity_b, person=cls.person_b)

        # A ConditionOccurrence belonging to patient B
        cls.condition_b = ConditionOccurrence.objects.create(
            condition_occurrence_id=93901,
            person=cls.person_b,
            condition_concept=condition_concept,
            condition_start_date=date.today(),
            condition_type_concept=type_concept,
        )

        # Superuser
        cls.superuser = Identity.objects.create_superuser(email='su-scope@test.com', password='pw')

        # Staff
        cls.staff = Identity.objects.create_user(email='staff-scope@test.com', password='pw', is_staff=True)

        # Provider with GroupAccess (bypasses patient scope)
        cls.provider = Identity.objects.create_user(email='prov-scope@test.com', password='pw')
        cls.org = Organization.objects.create(name='Scope Org', slug='scope-org-93')
        GroupAccess.objects.create(identity=cls.provider, org=cls.org, role='doctor')

    def _client_as(self, identity):
        c = APIClient()
        c.force_authenticate(user=identity)
        return c

    def test_patient_can_access_own_condition(self):
        """Patient B can access their own condition via detail endpoint."""
        resp = self._client_as(self.identity_b).get(
            f'/api/v1/conditions/{self.condition_b.condition_occurrence_id}/'
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

    def test_patient_cannot_access_other_patients_condition(self):
        """Patient A cannot access patient B's condition — gets 404 (queryset filtered)."""
        resp = self._client_as(self.identity_a).get(
            f'/api/v1/conditions/{self.condition_b.condition_occurrence_id}/'
        )
        # _OmopFilterMixin filters the queryset to the user's own records,
        # so the object is not found rather than forbidden.
        self.assertIn(resp.status_code, [status.HTTP_403_FORBIDDEN, status.HTTP_404_NOT_FOUND])

    def test_superuser_bypasses_scope(self):
        """Superuser can access any patient's condition."""
        resp = self._client_as(self.superuser).get(
            f'/api/v1/conditions/{self.condition_b.condition_occurrence_id}/'
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

    def test_staff_bypasses_scope(self):
        """Staff can access any patient's condition."""
        resp = self._client_as(self.staff).get(
            f'/api/v1/conditions/{self.condition_b.condition_occurrence_id}/'
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

    def test_provider_bypasses_patient_scope(self):
        """Provider with GroupAccess is not treated as a patient by PatientSelfScopePermission.

        Note: providers access data via org-scoped OAuth tokens in production.
        With session auth, _OmopFilterMixin filters by PatientUser (returning 404
        if the provider has no PatientUser link). This test verifies that
        PatientSelfScopePermission itself does not block the provider — the 404
        comes from queryset filtering, not from the object-level permission.
        """
        resp = self._client_as(self.provider).get(
            f'/api/v1/conditions/{self.condition_b.condition_occurrence_id}/'
        )
        # 404 from queryset filtering (no PatientUser link, no org token) — NOT 403 from scope
        self.assertIn(resp.status_code, [status.HTTP_200_OK, status.HTTP_404_NOT_FOUND])


# ---------------------------------------------------------------------------
# Patient Account Deletion tests
# ---------------------------------------------------------------------------

class PatientAccountDeletionTest(TestCase):
    """Test DELETE /api/v1/patient-records/me/ for GDPR right to erasure."""

    @classmethod
    def setUpTestData(cls):
        from patient_portal.models import PatientUser
        from omop_core.models import Organization

        _make_vocab_fixtures()
        condition_concept = Concept.objects.get(concept_id=4112853)
        type_concept = Concept.objects.get(concept_id=32817)

        # Patient to be deleted
        cls.person = Person.objects.create(person_id=94001, family_name='Doomed', given_name='Dan')
        cls.patient_rec = PatientRecord.objects.create(person=cls.person)
        cls.identity = Identity.objects.create_user(email='doomed@test.com', password='pw')
        PatientUser.objects.create(identity=cls.identity, person=cls.person)

        # Clinical data for the patient
        cls.condition = ConditionOccurrence.objects.create(
            condition_occurrence_id=94901,
            person=cls.person,
            condition_concept=condition_concept,
            condition_start_date=date.today(),
            condition_type_concept=type_concept,
        )

        # Unrelated patient (should be untouched)
        cls.other_person = Person.objects.create(person_id=94002, family_name='Safe', given_name='Sue')
        cls.other_rec = PatientRecord.objects.create(person=cls.other_person)
        cls.other_identity = Identity.objects.create_user(email='safe@test.com', password='pw')
        PatientUser.objects.create(identity=cls.other_identity, person=cls.other_person)

        # Staff user
        cls.staff = Identity.objects.create_user(email='staff-del@test.com', password='pw', is_staff=True)

    def _client_as(self, identity):
        c = APIClient()
        c.force_authenticate(user=identity)
        return c

    def test_delete_account_success(self):
        """DELETE with valid confirm removes all patient data."""
        from patient_portal.models import PatientUser

        # Create fresh data for this test (setUpTestData data is shared, can't delete once)
        person = Person.objects.create(person_id=94101, family_name='Fresh', given_name='Fran')
        PatientRecord.objects.create(person=person)
        identity = Identity.objects.create_user(email='fresh-del@test.com', password='pw')
        PatientUser.objects.create(identity=identity, person=person)
        identity_pk = identity.pk

        resp = self._client_as(identity).delete(
            '/api/v1/patient-records/me/',
            data={'confirm': 'DELETE'},
            format='json',
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

        # Person and all clinical data gone
        self.assertFalse(Person.objects.filter(person_id=94101).exists())
        self.assertFalse(PatientRecord.objects.filter(person_id=94101).exists())
        self.assertFalse(PatientUser.objects.filter(person__person_id=94101).exists())
        # Identity gone
        self.assertFalse(Identity.objects.filter(pk=identity_pk).exists())

    def test_delete_missing_confirm(self):
        """DELETE without confirm body → 400."""
        person = Person.objects.create(person_id=94102, family_name='NoConf', given_name='Ned')
        PatientRecord.objects.create(person=person)
        identity = Identity.objects.create_user(email='noconf-del@test.com', password='pw')
        from patient_portal.models import PatientUser
        PatientUser.objects.create(identity=identity, person=person)

        resp = self._client_as(identity).delete(
            '/api/v1/patient-records/me/',
            data={},
            format='json',
        )
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        # Person still exists
        self.assertTrue(Person.objects.filter(person_id=94102).exists())

    def test_delete_wrong_confirm(self):
        """DELETE with wrong confirm value → 400."""
        person = Person.objects.create(person_id=94103, family_name='Wrong', given_name='Will')
        PatientRecord.objects.create(person=person)
        identity = Identity.objects.create_user(email='wrong-del@test.com', password='pw')
        from patient_portal.models import PatientUser
        PatientUser.objects.create(identity=identity, person=person)

        resp = self._client_as(identity).delete(
            '/api/v1/patient-records/me/',
            data={'confirm': 'delete'},  # lowercase — should fail
            format='json',
        )
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_staff_cannot_delete_own_account(self):
        """Non-patient (staff) cannot use the account deletion endpoint."""
        resp = self._client_as(self.staff).delete(
            '/api/v1/patient-records/me/',
            data={'confirm': 'DELETE'},
            format='json',
        )
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)

    def test_other_patients_data_untouched(self):
        """Deleting one patient does not affect another patient's data."""
        from patient_portal.models import PatientUser

        person = Person.objects.create(person_id=94104, family_name='Gone', given_name='Gus')
        PatientRecord.objects.create(person=person)
        identity = Identity.objects.create_user(email='gone-del@test.com', password='pw')
        PatientUser.objects.create(identity=identity, person=person)

        self._client_as(identity).delete(
            '/api/v1/patient-records/me/',
            data={'confirm': 'DELETE'},
            format='json',
        )

        # Other patient still intact
        self.assertTrue(Person.objects.filter(person_id=94002).exists())
        self.assertTrue(PatientRecord.objects.filter(person=self.other_person).exists())


# ---------------------------------------------------------------------------
# Phase 2 — FHIR Export tests
# ---------------------------------------------------------------------------

class FhirExportServiceTest(TestCase):
    """Unit tests for omop_core.services.fhir_export.build_fhir_bundle."""

    @classmethod
    def setUpTestData(cls):
        _make_vocab_fixtures()
        cls.condition_concept = Concept.objects.get(concept_id=4112853)  # Breast cancer
        cls.type_concept = Concept.objects.get(concept_id=32817)  # EHR
        cls.lab_concept = Concept.objects.get(concept_id=3000963)  # Lab test result

        cls.person = Person.objects.create(
            person_id=95001, family_name='Export', given_name='Eve',
            year_of_birth=1985, month_of_birth=3, day_of_birth=15,
            gender_concept_id=8532,
        )
        cls.patient_rec = PatientRecord.objects.create(person=cls.person)

        # Clinical data
        cls.condition = ConditionOccurrence.objects.create(
            condition_occurrence_id=95901,
            person=cls.person,
            condition_concept=cls.condition_concept,
            condition_start_date=date(2022, 6, 1),
            condition_type_concept=cls.type_concept,
            condition_source_value='Breast cancer',
        )
        cls.measurement = Measurement.objects.create(
            measurement_id=95902,
            person=cls.person,
            measurement_concept=cls.lab_concept,
            measurement_date=date(2023, 1, 10),
            measurement_type_concept=cls.type_concept,
            value_as_number=12.5,
            unit_source_value='g/dL',
            measurement_source_value='Hemoglobin',
        )

    def test_bundle_structure(self):
        from omop_core.services.fhir_export import build_fhir_bundle
        bundle = build_fhir_bundle(self.person)

        self.assertEqual(bundle['resourceType'], 'Bundle')
        self.assertEqual(bundle['type'], 'searchset')
        self.assertIsInstance(bundle['total'], int)
        self.assertIsInstance(bundle['entry'], list)
        self.assertGreater(bundle['total'], 0)

    def test_patient_resource_present(self):
        from omop_core.services.fhir_export import build_fhir_bundle
        bundle = build_fhir_bundle(self.person)

        patient_entries = [
            e for e in bundle['entry']
            if e['resource']['resourceType'] == 'Patient'
        ]
        self.assertEqual(len(patient_entries), 1)
        patient = patient_entries[0]['resource']
        self.assertEqual(patient['name'][0]['family'], 'Export')
        self.assertEqual(patient['name'][0]['given'], ['Eve'])
        self.assertEqual(patient['birthDate'], '1985-03-15')
        self.assertEqual(patient['gender'], 'female')

    def test_condition_exported(self):
        from omop_core.services.fhir_export import build_fhir_bundle
        bundle = build_fhir_bundle(self.person)

        conditions = [
            e for e in bundle['entry']
            if e['resource']['resourceType'] == 'Condition'
        ]
        self.assertGreaterEqual(len(conditions), 1)
        cond = conditions[0]['resource']
        self.assertIn('code', cond)
        self.assertEqual(cond['onsetDateTime'], '2022-06-01')

    def test_measurement_exported_as_observation(self):
        from omop_core.services.fhir_export import build_fhir_bundle
        bundle = build_fhir_bundle(self.person)

        observations = [
            e for e in bundle['entry']
            if e['resource']['resourceType'] == 'Observation'
        ]
        self.assertGreaterEqual(len(observations), 1)
        # Find the one with a valueQuantity
        quant_obs = [o for o in observations if 'valueQuantity' in o['resource']]
        self.assertGreaterEqual(len(quant_obs), 1)
        obs = quant_obs[0]['resource']
        self.assertEqual(obs['valueQuantity']['value'], 12.5)
        self.assertEqual(obs['valueQuantity']['unit'], 'g/dL')

    def test_empty_patient_returns_patient_only(self):
        from omop_core.services.fhir_export import build_fhir_bundle
        empty_person = Person.objects.create(
            person_id=95099, family_name='Empty', given_name='Em',
            gender_concept_id=8507,
        )
        bundle = build_fhir_bundle(empty_person)
        self.assertEqual(bundle['total'], 1)
        self.assertEqual(bundle['entry'][0]['resource']['resourceType'], 'Patient')


class FhirExportApiTest(TestCase):
    """Test the export-fhir API endpoint."""

    @classmethod
    def setUpTestData(cls):
        from patient_portal.models import PatientUser

        _make_vocab_fixtures()
        cls.condition_concept = Concept.objects.get(concept_id=4112853)
        cls.type_concept = Concept.objects.get(concept_id=32817)

        # Patient A — will export their own record
        cls.person_a = Person.objects.create(
            person_id=96001, family_name='Able', given_name='Amy',
            gender_concept_id=8532,
        )
        cls.patient_a_rec = PatientRecord.objects.create(person=cls.person_a)
        cls.identity_a = Identity.objects.create_user(email='export-a@test.com', password='pw')
        PatientUser.objects.create(identity=cls.identity_a, person=cls.person_a)

        ConditionOccurrence.objects.create(
            condition_occurrence_id=96901,
            person=cls.person_a,
            condition_concept=cls.condition_concept,
            condition_start_date=date.today(),
            condition_type_concept=cls.type_concept,
        )

        # Patient B — another patient
        cls.person_b = Person.objects.create(
            person_id=96002, family_name='Baker', given_name='Bob',
            gender_concept_id=8507,
        )
        cls.patient_b_rec = PatientRecord.objects.create(person=cls.person_b)
        cls.identity_b = Identity.objects.create_user(email='export-b@test.com', password='pw')
        PatientUser.objects.create(identity=cls.identity_b, person=cls.person_b)

        # Staff
        cls.staff = Identity.objects.create_user(
            email='export-staff@test.com', password='pw', is_staff=True,
        )

    def _client_as(self, identity):
        c = APIClient()
        c.force_authenticate(user=identity)
        return c

    def test_patient_can_export_own_record(self):
        """Patient A can export their own FHIR bundle."""
        resp = self._client_as(self.identity_a).get(
            f'/api/v1/patient-records/{self.person_a.person_id}/export-fhir/'
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        bundle = resp.json()
        self.assertEqual(bundle['resourceType'], 'Bundle')
        self.assertEqual(bundle['type'], 'searchset')
        resource_types = {e['resource']['resourceType'] for e in bundle['entry']}
        self.assertIn('Patient', resource_types)
        self.assertIn('Condition', resource_types)

    def test_patient_cannot_export_other_patients_record(self):
        """Patient A cannot export patient B's record."""
        resp = self._client_as(self.identity_a).get(
            f'/api/v1/patient-records/{self.person_b.person_id}/export-fhir/'
        )
        self.assertIn(resp.status_code, [
            status.HTTP_403_FORBIDDEN, status.HTTP_404_NOT_FOUND,
        ])

    def test_staff_can_export_any_record(self):
        """Staff can export any patient's record."""
        resp = self._client_as(self.staff).get(
            f'/api/v1/patient-records/{self.person_a.person_id}/export-fhir/'
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        bundle = resp.json()
        self.assertEqual(bundle['resourceType'], 'Bundle')

    def test_nonexistent_person_returns_404(self):
        """Export of nonexistent person_id returns 404."""
        resp = self._client_as(self.staff).get(
            '/api/v1/patient-records/999999/export-fhir/'
        )
        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)

    def test_unauthenticated_returns_403(self):
        """Unauthenticated request to export returns 401/403."""
        c = APIClient()
        resp = c.get(
            f'/api/v1/patient-records/{self.person_a.person_id}/export-fhir/'
        )
        self.assertIn(resp.status_code, [
            status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN,
        ])


# ---------------------------------------------------------------------------
# Patient Consent ViewSet tests (PHR-S FM Phase 3)
# ---------------------------------------------------------------------------

class PatientConsentViewSetTest(TestCase):
    """Test PatientConsentViewSet — auto-create, toggle, and self-scoping."""

    @classmethod
    def setUpTestData(cls):
        from patient_portal.models import PatientUser

        _make_vocab_fixtures()

        # Patient A
        cls.person_a = Person.objects.create(person_id=96001, family_name='Alpha', given_name='Ann')
        cls.patient_a_rec = PatientRecord.objects.create(person=cls.person_a)
        cls.identity_a = Identity.objects.create_user(email='consent-a@test.com', password='pw')
        PatientUser.objects.create(identity=cls.identity_a, person=cls.person_a)

        # Patient B
        cls.person_b = Person.objects.create(person_id=96002, family_name='Bravo', given_name='Ben')
        cls.patient_b_rec = PatientRecord.objects.create(person=cls.person_b)
        cls.identity_b = Identity.objects.create_user(email='consent-b@test.com', password='pw')
        PatientUser.objects.create(identity=cls.identity_b, person=cls.person_b)

        # Staff user
        cls.staff = Identity.objects.create_user(email='consent-staff@test.com', password='pw', is_staff=True)

    def _client_as(self, identity):
        c = APIClient()
        c.force_authenticate(user=identity)
        return c

    def test_list_creates_all_consent_types(self):
        """GET /api/v1/consents/ auto-creates all 3 consent types, all granted=False."""
        resp = self._client_as(self.identity_a).get('/api/v1/consents/')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        data = resp.json()
        self.assertEqual(len(data), 3)
        types = {c['consent_type'] for c in data}
        self.assertEqual(types, {'data_sharing', 'clinical_trial', 'research'})
        for c in data:
            self.assertFalse(c['consent_granted'])

    def test_list_returns_only_own_consents(self):
        """Patient A only sees their own 3 consents, not patient B's."""
        # Ensure both patients have consents auto-created
        self._client_as(self.identity_a).get('/api/v1/consents/')
        self._client_as(self.identity_b).get('/api/v1/consents/')

        resp = self._client_as(self.identity_a).get('/api/v1/consents/')
        data = resp.json()
        self.assertEqual(len(data), 3)

    def test_grant_consent(self):
        """PATCH with consent_granted=true updates the consent."""
        from patient_portal.models import PatientConsent

        # Auto-create consents
        resp = self._client_as(self.identity_a).get('/api/v1/consents/')
        consent_id = resp.json()[0]['id']

        resp = self._client_as(self.identity_a).patch(
            f'/api/v1/consents/{consent_id}/',
            {'consent_granted': True},
            format='json',
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertTrue(PatientConsent.objects.get(pk=consent_id).consent_granted)

    def test_revoke_consent(self):
        """Grant then revoke — toggle works both ways."""
        from patient_portal.models import PatientConsent

        resp = self._client_as(self.identity_a).get('/api/v1/consents/')
        consent_id = resp.json()[0]['id']

        client = self._client_as(self.identity_a)
        client.patch(f'/api/v1/consents/{consent_id}/', {'consent_granted': True}, format='json')
        self.assertTrue(PatientConsent.objects.get(pk=consent_id).consent_granted)

        client.patch(f'/api/v1/consents/{consent_id}/', {'consent_granted': False}, format='json')
        self.assertFalse(PatientConsent.objects.get(pk=consent_id).consent_granted)

    def test_cannot_patch_other_patients_consent(self):
        """Patient A cannot PATCH patient B's consent — 404 from queryset filtering."""
        from patient_portal.models import PatientConsent

        # Auto-create B's consents
        self._client_as(self.identity_b).get('/api/v1/consents/')
        b_consent = PatientConsent.objects.filter(
            patient_user__person=self.person_b,
        ).first()

        resp = self._client_as(self.identity_a).patch(
            f'/api/v1/consents/{b_consent.pk}/',
            {'consent_granted': True},
            format='json',
        )
        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)

    def test_staff_can_list_all_consents(self):
        """Staff user sees all consents across patients."""
        # Ensure both patients have consents
        self._client_as(self.identity_a).get('/api/v1/consents/')
        self._client_as(self.identity_b).get('/api/v1/consents/')

        resp = self._client_as(self.staff).get('/api/v1/consents/')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        data = resp.json()
        # Staff sees at least both patients' consents (3 each = 6+)
        self.assertGreaterEqual(len(data), 6)

    def test_unauthenticated_returns_403(self):
        """Unauthenticated request returns 401 or 403."""
        c = APIClient()
        resp = c.get('/api/v1/consents/')
        self.assertIn(resp.status_code, [
            status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN,
        ])


# ---------------------------------------------------------------------------
# Patient Survey — session-auth patient tests (PHR-S FM Phase 4a)
# ---------------------------------------------------------------------------

class PatientSurveySessionAuthTest(TestCase):
    """Test that session-auth patients can list surveys, create responses,
    autosave via PATCH, and are self-scoped (cannot see other patients' data).
    """

    @classmethod
    def setUpTestData(cls):
        from patient_portal.models import PatientUser

        _make_vocab_fixtures()

        # Patient A
        cls.person_a = Person.objects.create(person_id=97001, family_name='SurvAlpha', given_name='Alice')
        cls.patient_a_rec = PatientRecord.objects.create(person=cls.person_a)
        cls.identity_a = Identity.objects.create_user(email='survey-a@test.com', password='pw')
        PatientUser.objects.create(identity=cls.identity_a, person=cls.person_a)

        # Patient B
        cls.person_b = Person.objects.create(person_id=97002, family_name='SurvBravo', given_name='Bob')
        cls.patient_b_rec = PatientRecord.objects.create(person=cls.person_b)
        cls.identity_b = Identity.objects.create_user(email='survey-b@test.com', password='pw')
        PatientUser.objects.create(identity=cls.identity_b, person=cls.person_b)

        # Survey
        cls.survey = Survey.objects.create(
            name='test-survey-phase4a',
            title='Test Survey',
            description='A test survey for Phase 4a',
            status=Survey.STATUS_ACTIVE,
            pages=[
                {
                    'name': 'page1',
                    'title': 'Page 1',
                    'inputs': [
                        {'name': 'q1', 'type': 'text', 'label': 'Question 1'},
                        {'name': 'q2', 'type': 'text', 'label': 'Question 2'},
                    ],
                }
            ],
        )

    def _client_as(self, identity):
        c = APIClient()
        c.force_authenticate(user=identity)
        return c

    def test_patient_can_list_surveys(self):
        """GET /api/v1/surveys/ returns available surveys for a patient."""
        resp = self._client_as(self.identity_a).get('/api/v1/surveys/')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        data = resp.json()
        # Should contain at least the survey we created
        names = [s['name'] for s in data]
        self.assertIn('test-survey-phase4a', names)

    def test_patient_can_create_survey_response(self):
        """POST /api/v1/survey-responses/ creates a new response (201)."""
        resp = self._client_as(self.identity_a).post(
            '/api/v1/survey-responses/',
            {
                'person': self.person_a.person_id,
                'survey': self.survey.pk,
            },
            format='json',
        )
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)
        data = resp.json()
        self.assertEqual(data['survey'], self.survey.pk)
        self.assertEqual(data['person'], self.person_a.person_id)
        self.assertEqual(data['values'], {})
        self.assertEqual(data['percent_complete'], 0)

    def test_patient_can_patch_own_response(self):
        """PATCH /api/v1/survey-responses/{id}/ merges values (autosave)."""
        client = self._client_as(self.identity_a)
        # Create
        resp = client.post(
            '/api/v1/survey-responses/',
            {'person': self.person_a.person_id, 'survey': self.survey.pk},
            format='json',
        )
        response_id = resp.json()['id']

        # Autosave first answer
        resp = client.patch(
            f'/api/v1/survey-responses/{response_id}/',
            {'values': {'q1': 'answer-one'}, 'percent_complete': 50},
            format='json',
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        data = resp.json()
        self.assertEqual(data['values']['q1'], 'answer-one')
        self.assertEqual(data['percent_complete'], 50)

    def test_patient_cannot_access_other_patients_response(self):
        """Patient A cannot GET patient B's survey response — returns 404."""
        # Create a response for patient B
        client_b = self._client_as(self.identity_b)
        resp = client_b.post(
            '/api/v1/survey-responses/',
            {'person': self.person_b.person_id, 'survey': self.survey.pk},
            format='json',
        )
        response_b_id = resp.json()['id']

        # Patient A tries to access it
        client_a = self._client_as(self.identity_a)
        resp = client_a.get(f'/api/v1/survey-responses/{response_b_id}/')
        self.assertIn(resp.status_code, [
            status.HTTP_403_FORBIDDEN, status.HTTP_404_NOT_FOUND,
        ])

    def test_patient_list_is_self_scoped(self):
        """GET /api/v1/survey-responses/?person_id={own} returns only own responses."""
        client_a = self._client_as(self.identity_a)
        client_b = self._client_as(self.identity_b)

        # Ensure both patients have responses
        client_a.post(
            '/api/v1/survey-responses/',
            {'person': self.person_a.person_id, 'survey': self.survey.pk},
            format='json',
        )
        client_b.post(
            '/api/v1/survey-responses/',
            {'person': self.person_b.person_id, 'survey': self.survey.pk},
            format='json',
        )

        # Patient A lists their own
        resp = client_a.get(f'/api/v1/survey-responses/?person_id={self.person_a.person_id}')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        data = resp.json()
        # All returned responses belong to patient A
        for entry in data:
            self.assertEqual(entry['person'], self.person_a.person_id)

    def test_patient_cannot_create_response_for_other_patient(self):
        """POST with person=other patient's person_id is rejected (403)."""
        resp = self._client_as(self.identity_a).post(
            '/api/v1/survey-responses/',
            {'person': self.person_b.person_id, 'survey': self.survey.pk},
            format='json',
        )
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)


# ---------------------------------------------------------------------------
# Patient messaging — bidirectional messaging (PHR-S FM Phase 4b)
# ---------------------------------------------------------------------------

class PatientMessageViewSetTest(TestCase):
    """Test PatientMessageViewSet — threading, mark-read, and self-scoping."""

    @classmethod
    def setUpTestData(cls):
        from patient_portal.models import PatientUser, PatientMessage

        _make_vocab_fixtures()

        # Patient A
        cls.person_a = Person.objects.create(
            person_id=98001, family_name='MsgAlpha', given_name='Alice',
        )
        cls.patient_a_rec = PatientRecord.objects.create(person=cls.person_a)
        cls.identity_a = Identity.objects.create_user(
            email='msg-a@test.com', password='pw',
        )
        cls.pu_a = PatientUser.objects.create(
            identity=cls.identity_a, person=cls.person_a,
        )

        # Patient B
        cls.person_b = Person.objects.create(
            person_id=98002, family_name='MsgBravo', given_name='Bob',
        )
        cls.patient_b_rec = PatientRecord.objects.create(person=cls.person_b)
        cls.identity_b = Identity.objects.create_user(
            email='msg-b@test.com', password='pw',
        )
        cls.pu_b = PatientUser.objects.create(
            identity=cls.identity_b, person=cls.person_b,
        )

        # Staff user
        cls.staff = Identity.objects.create_user(
            email='msg-staff@test.com', password='pw', is_staff=True,
        )

        # Pre-create messages for A
        cls.msg_a1 = PatientMessage.objects.create(
            patient_user=cls.pu_a,
            sender=cls.identity_a,
            subject='Question from A',
            message='Hello doctor',
            sender_is_patient=True,
        )
        cls.msg_a2 = PatientMessage.objects.create(
            patient_user=cls.pu_a,
            sender=cls.staff,
            subject='Reply from staff',
            message='Hi Alice',
            sender_is_patient=False,
        )

        # Pre-create message for B
        cls.msg_b1 = PatientMessage.objects.create(
            patient_user=cls.pu_b,
            sender=cls.identity_b,
            subject='Question from B',
            message='Hello from Bob',
            sender_is_patient=True,
        )

    def _client_as(self, identity):
        c = APIClient()
        c.force_authenticate(user=identity)
        return c

    # ---- 1. List own messages ----
    def test_patient_can_list_own_messages(self):
        """GET /api/v1/messages/ returns only the patient's own messages."""
        resp = self._client_as(self.identity_a).get('/api/v1/messages/')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        data = resp.json()['results']
        self.assertEqual(len(data), 2)
        ids = {m['id'] for m in data}
        self.assertIn(self.msg_a1.pk, ids)
        self.assertIn(self.msg_a2.pk, ids)
        # B's message should NOT be present
        self.assertNotIn(self.msg_b1.pk, ids)

    # ---- 2. Create message ----
    def test_patient_can_create_message(self):
        """POST /api/v1/messages/ creates a message with sender auto-set."""
        resp = self._client_as(self.identity_a).post(
            '/api/v1/messages/',
            {'subject': 'New question', 'message': 'What about my labs?'},
            format='json',
        )
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)
        data = resp.json()
        self.assertEqual(data['subject'], 'New question')
        self.assertTrue(data['sender_is_patient'])
        self.assertEqual(data['sender'], self.identity_a.pk)
        self.assertEqual(data['patient_user'], self.pu_a.pk)
        self.assertIsNone(data['parent'])

    # ---- 3. Reply (threading) ----
    def test_patient_can_reply(self):
        """POST with parent FK creates a threaded reply."""
        resp = self._client_as(self.identity_a).post(
            '/api/v1/messages/',
            {
                'parent': self.msg_a1.pk,
                'subject': 'Re: Question from A',
                'message': 'Follow-up question',
            },
            format='json',
        )
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)
        data = resp.json()
        self.assertEqual(data['parent'], self.msg_a1.pk)

        # Verify reply_count on parent
        resp2 = self._client_as(self.identity_a).get(
            f'/api/v1/messages/{self.msg_a1.pk}/',
        )
        self.assertEqual(resp2.json()['reply_count'], 1)

    # ---- 4. Mark as read ----
    def test_patient_can_mark_as_read(self):
        """PATCH /api/v1/messages/{id}/mark-read/ sets read_at."""
        resp = self._client_as(self.identity_a).patch(
            f'/api/v1/messages/{self.msg_a2.pk}/mark-read/',
            format='json',
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        data = resp.json()
        self.assertIsNotNone(data['read_at'])
        self.assertTrue(data['is_read'])

    # ---- 5. Cross-patient isolation ----
    def test_cross_patient_isolation(self):
        """Patient A cannot see patient B's messages."""
        resp = self._client_as(self.identity_a).get('/api/v1/messages/')
        ids = {m['id'] for m in resp.json()['results']}
        self.assertNotIn(self.msg_b1.pk, ids)

    def test_cross_patient_detail_blocked(self):
        """Patient A cannot GET patient B's message detail — 404."""
        resp = self._client_as(self.identity_a).get(
            f'/api/v1/messages/{self.msg_b1.pk}/',
        )
        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)

    # ---- 6. Cross-patient create blocked ----
    def test_cross_patient_create_blocked(self):
        """Patient A cannot create a message for patient B's patient_user.

        perform_create auto-sets patient_user to the requesting patient's own,
        so the patient_user field in the request body is ignored for patients.
        """
        resp = self._client_as(self.identity_a).post(
            '/api/v1/messages/',
            {
                'patient_user': self.pu_b.pk,
                'subject': 'Sneaky',
                'message': 'This should fail',
            },
            format='json',
        )
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)
        self.assertEqual(resp.json()['patient_user'], self.pu_a.pk)

    # ---- 7. Filter top-level threads ----
    def test_filter_top_level_threads(self):
        """GET ?parent=null returns only top-level messages."""
        # Create a reply first
        self._client_as(self.identity_a).post(
            '/api/v1/messages/',
            {
                'parent': self.msg_a1.pk,
                'subject': 'Re: thread test',
                'message': 'Reply body',
            },
            format='json',
        )
        resp = self._client_as(self.identity_a).get(
            '/api/v1/messages/?parent=null',
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        for m in resp.json()['results']:
            self.assertIsNone(m['parent'])

    # ---- 8. Filter unread ----
    def test_filter_unread(self):
        """GET ?is_read=false returns only unread messages."""
        # Mark one as read first
        self._client_as(self.identity_a).patch(
            f'/api/v1/messages/{self.msg_a1.pk}/mark-read/',
            format='json',
        )
        resp = self._client_as(self.identity_a).get(
            '/api/v1/messages/?is_read=false',
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        for m in resp.json()['results']:
            self.assertIsNone(m['read_at'])
            # The marked-as-read message should not appear
            self.assertNotEqual(m['id'], self.msg_a1.pk)

    # ---- 9. Unauthenticated ----
    def test_cross_patient_reply_blocked(self):
        """Patient A cannot reply to patient B's message."""
        resp = self._client_as(self.identity_a).post(
            '/api/v1/messages/',
            {
                'parent': self.msg_b1.pk,
                'subject': 'Cross-patient reply',
                'message': 'This should fail',
            },
            format='json',
        )
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)

    def test_unauthenticated_returns_403(self):
        """Unauthenticated request returns 401 or 403."""
        c = APIClient()
        resp = c.get('/api/v1/messages/')
        self.assertIn(resp.status_code, [
            status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN,
        ])


# ---------------------------------------------------------------------------
# 10. Phase 5 — Clinical Lists (Advance Directives, Immunizations, Allergies)
# ---------------------------------------------------------------------------

class AdvanceDirectiveTest(TestCase):
    """Test ADVANCE_DIRECTIVE doc_type on PatientDocumentViewSet."""

    @classmethod
    def setUpTestData(cls):
        _make_vocab_fixtures()
        cls.admin = Identity.objects.create_superuser(
            email='ad_admin@test.com', password='testpass',
        )
        cls.person = Person.objects.create(
            person_id=80001, given_name='Ada', family_name='Directive',
            year_of_birth=1960, gender_source_value='female',
            race_source_value='unknown', ethnicity_source_value='unknown',
        )

    def setUp(self):
        self.client = APIClient()
        self.client.force_authenticate(user=self.admin)

    def test_create_advance_directive(self):
        """Can create a document with doc_type=ADVANCE_DIRECTIVE."""
        resp = self.client.post('/api/v1/documents/', {
            'person': self.person.person_id,
            'doc_type': 'ADVANCE_DIRECTIVE',
            'title': 'Living Will',
        }, format='json')
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)
        self.assertEqual(resp.data['doc_type'], 'ADVANCE_DIRECTIVE')

    def test_filter_by_doc_type(self):
        """Filtering by doc_type=ADVANCE_DIRECTIVE returns only ADs."""
        PatientDocument.objects.create(
            person=self.person, doc_type='ADVANCE_DIRECTIVE', title='AD Doc',
        )
        PatientDocument.objects.create(
            person=self.person, doc_type='OTHER', title='Other Doc',
        )
        resp = self.client.get('/api/v1/documents/', {'doc_type': 'ADVANCE_DIRECTIVE'})
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        results = resp.data if isinstance(resp.data, list) else resp.data.get('results', [])
        self.assertTrue(all(d['doc_type'] == 'ADVANCE_DIRECTIVE' for d in results))


class ImmunizationListTest(TestCase):
    """Test the immunization list endpoint (route_source_value='VACCINE')."""

    @classmethod
    def setUpTestData(cls):
        _make_vocab_fixtures()
        cls.admin = Identity.objects.create_superuser(
            email='imm_admin@test.com', password='testpass',
        )
        cls.person = Person.objects.create(
            person_id=80002, given_name='Ivy', family_name='Vaccine',
            year_of_birth=1985, gender_source_value='female',
            race_source_value='unknown', ethnicity_source_value='unknown',
        )
        drug_concept = Concept.objects.get(concept_id=19136160)
        type_concept = Concept.objects.get(concept_id=32817)

        from omop_core.services.pk import next_pk
        # Immunization (tagged)
        cls.imm_de = DrugExposure.objects.create(
            drug_exposure_id=next_pk(DrugExposure, 'drug_exposure_id'),
            person=cls.person,
            drug_concept=drug_concept,
            drug_exposure_start_date=date(2024, 3, 15),
            drug_type_concept=type_concept,
            drug_source_value='COVID-19 Vaccine',
            route_source_value='VACCINE',
            lot_number='LOT-ABC-123',
        )
        # Therapeutic drug (not tagged)
        cls.drug_de = DrugExposure.objects.create(
            drug_exposure_id=next_pk(DrugExposure, 'drug_exposure_id'),
            person=cls.person,
            drug_concept=drug_concept,
            drug_exposure_start_date=date(2024, 1, 10),
            drug_type_concept=type_concept,
            drug_source_value='Rituximab',
        )

    def setUp(self):
        self.client = APIClient()
        self.client.force_authenticate(user=self.admin)

    def test_immunization_endpoint_returns_vaccines_only(self):
        """GET /v1/immunizations/ returns only VACCINE-tagged DrugExposures."""
        resp = self.client.get('/api/v1/immunizations/', {'person_id': self.person.person_id})
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        results = resp.data if isinstance(resp.data, list) else resp.data.get('results', [])
        ids = [r['drug_exposure_id'] for r in results]
        self.assertIn(self.imm_de.drug_exposure_id, ids)
        self.assertNotIn(self.drug_de.drug_exposure_id, ids)

    def test_immunization_serializer_fields(self):
        """Response includes vaccine_name, date, lot_number."""
        resp = self.client.get('/api/v1/immunizations/', {'person_id': self.person.person_id})
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        results = resp.data if isinstance(resp.data, list) else resp.data.get('results', [])
        self.assertGreater(len(results), 0)
        item = results[0]
        self.assertIn('vaccine_name', item)
        self.assertIn('date', item)
        self.assertIn('lot_number', item)
        self.assertEqual(item['lot_number'], 'LOT-ABC-123')

    def test_therapeutic_drug_excluded(self):
        """Non-vaccine DrugExposure does not appear at /v1/immunizations/."""
        resp = self.client.get('/api/v1/immunizations/', {'person_id': self.person.person_id})
        results = resp.data if isinstance(resp.data, list) else resp.data.get('results', [])
        for r in results:
            self.assertNotEqual(r['drug_exposure_id'], self.drug_de.drug_exposure_id)


class AllergyListTest(TestCase):
    """Test the allergy list endpoint (qualifier_source_value='ALLERGY')."""

    @classmethod
    def setUpTestData(cls):
        _make_vocab_fixtures()
        cls.admin = Identity.objects.create_superuser(
            email='allergy_admin@test.com', password='testpass',
        )
        cls.person = Person.objects.create(
            person_id=80003, given_name='Alma', family_name='Allergen',
            year_of_birth=1990, gender_source_value='female',
            race_source_value='unknown', ethnicity_source_value='unknown',
        )
        obs_domain, _ = Domain.objects.get_or_create(
            domain_id='Observation',
            defaults={'domain_name': 'Observation', 'domain_concept_id': 27},
        )
        obs_cc, _ = ConceptClass.objects.get_or_create(
            concept_class_id='Clinical Obs',
            defaults={'concept_class_name': 'Clinical Observation', 'concept_class_concept_id': 0},
        )
        vocab = Vocabulary.objects.get(vocabulary_id='TEST')
        cls.allergy_concept, _ = Concept.objects.get_or_create(
            concept_id=90001,
            defaults={
                'concept_name': 'Penicillin allergy',
                'domain': obs_domain,
                'vocabulary': vocab,
                'concept_class': obs_cc,
                'concept_code': '90001',
                'valid_start_date': date.today(),
                'valid_end_date': date(2099, 12, 31),
            },
        )
        type_concept = Concept.objects.get(concept_id=32817)

        from omop_core.services.pk import next_pk
        # Allergy observation (tagged)
        cls.allergy_obs = Observation.objects.create(
            observation_id=next_pk(Observation, 'observation_id'),
            person=cls.person,
            observation_concept=cls.allergy_concept,
            observation_date=date(2023, 6, 1),
            observation_type_concept=type_concept,
            value_as_string='high',
            observation_source_value='Penicillin',
            qualifier_source_value='ALLERGY',
            value_source_value='active',
        )
        # Non-allergy observation (not tagged)
        cls.other_obs = Observation.objects.create(
            observation_id=next_pk(Observation, 'observation_id'),
            person=cls.person,
            observation_concept=cls.allergy_concept,
            observation_date=date(2023, 7, 1),
            observation_type_concept=type_concept,
            value_as_string='some diagnostic',
            observation_source_value='DiagReport',
        )

    def setUp(self):
        self.client = APIClient()
        self.client.force_authenticate(user=self.admin)

    def test_allergy_endpoint_returns_allergies_only(self):
        """GET /v1/allergies/ returns only ALLERGY-tagged Observations."""
        resp = self.client.get('/api/v1/allergies/', {'person_id': self.person.person_id})
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        results = resp.data if isinstance(resp.data, list) else resp.data.get('results', [])
        ids = [r['observation_id'] for r in results]
        self.assertIn(self.allergy_obs.observation_id, ids)
        self.assertNotIn(self.other_obs.observation_id, ids)

    def test_allergy_serializer_fields(self):
        """Response includes allergen_name, criticality, clinical_status, recorded_date."""
        resp = self.client.get('/api/v1/allergies/', {'person_id': self.person.person_id})
        results = resp.data if isinstance(resp.data, list) else resp.data.get('results', [])
        self.assertGreater(len(results), 0)
        item = results[0]
        self.assertIn('allergen_name', item)
        self.assertIn('criticality', item)
        self.assertIn('clinical_status', item)
        self.assertIn('recorded_date', item)
        self.assertEqual(item['criticality'], 'high')
        self.assertEqual(item['clinical_status'], 'active')

    def test_non_allergy_observation_excluded(self):
        """Non-allergy Observations do not appear at /v1/allergies/."""
        resp = self.client.get('/api/v1/allergies/', {'person_id': self.person.person_id})
        results = resp.data if isinstance(resp.data, list) else resp.data.get('results', [])
        for r in results:
            self.assertNotEqual(r['observation_id'], self.other_obs.observation_id)

    def test_legacy_fhir_upload_creates_allergy(self):
        """FHIR bundle with AllergyIntolerance creates tagged Observation."""
        bundle = {
            "resourceType": "Bundle",
            "type": "collection",
            "entry": [
                {
                    "resource": {
                        "resourceType": "Patient",
                        "id": "allergy-test-pt-1",
                        "name": [{"family": "AllergyTest", "given": ["Pat"]}],
                        "gender": "female",
                        "birthDate": "1980-05-20",
                    }
                },
                {
                    "resource": {
                        "resourceType": "AllergyIntolerance",
                        "patient": {"reference": "Patient/allergy-test-pt-1"},
                        "code": {
                            "coding": [{"system": "http://snomed.info/sct", "code": "91936005", "display": "Penicillin allergy"}],
                            "text": "Penicillin allergy",
                        },
                        "criticality": "high",
                        "clinicalStatus": {
                            "coding": [{"code": "active"}],
                            "text": "Active",
                        },
                        "recordedDate": "2023-01-15",
                    }
                },
            ],
        }
        import io
        fhir_file = io.BytesIO(json.dumps(bundle).encode('utf-8'))
        fhir_file.name = 'allergy_test.json'
        resp = self.client.post(
            '/api/patient-info/upload_fhir/',
            {'file': fhir_file},
            format='multipart',
        )
        self.assertIn(resp.status_code, [200, 201], msg=f'Upload failed: {resp.data}')
        # Find the person created by the upload
        person = Person.objects.filter(family_name='AllergyTest').first()
        self.assertIsNotNone(person, 'Person not created from allergy upload')
        # Check that an ALLERGY-tagged observation was created
        allergy_obs = Observation.objects.filter(
            person=person, qualifier_source_value='ALLERGY',
        )
        self.assertGreater(allergy_obs.count(), 0, 'No ALLERGY-tagged observation created')
        obs = allergy_obs.first()
        self.assertEqual(obs.value_as_string, 'high')


class AuditRetentionTest(TestCase):
    """prune_audit_events management command — HL7 PHR-S FM TI.2.2.

    Verifies retention-window pruning: old rows deleted, newer rows kept,
    --dry-run is a no-op, --days overrides the setting, --archive writes JSONL
    before deleting, and batching handles more rows than the batch size.
    """

    def _make_event(self, days_ago, **kwargs):
        from patient_portal.models import AuditEvent
        ts = timezone.now() - timedelta(days=days_ago)
        defaults = dict(
            event_type=AuditEvent.EVENT_VIEW,
            method='GET',
            path='/api/v1/patient-records/',
            status_code=200,
        )
        defaults.update(kwargs)
        return AuditEvent.objects.create(timestamp=ts, **defaults)

    def _run(self, **opts):
        from django.core.management import call_command
        out = io.StringIO()
        call_command('prune_audit_events', stdout=out, **opts)
        return out.getvalue()

    def test_deletes_older_and_keeps_newer(self):
        from patient_portal.models import AuditEvent
        old = self._make_event(days_ago=3000)
        recent = self._make_event(days_ago=10)
        self._run(days=2190)
        self.assertFalse(AuditEvent.objects.filter(pk=old.pk).exists())
        self.assertTrue(AuditEvent.objects.filter(pk=recent.pk).exists())

    def test_boundary_row_just_inside_window_is_kept(self):
        from patient_portal.models import AuditEvent
        # 100 days ago, window 200 days -> not older than cutoff -> kept.
        row = self._make_event(days_ago=100)
        self._run(days=200)
        self.assertTrue(AuditEvent.objects.filter(pk=row.pk).exists())

    def test_dry_run_deletes_nothing(self):
        from patient_portal.models import AuditEvent
        old = self._make_event(days_ago=3000)
        out = self._run(days=2190, dry_run=True)
        self.assertTrue(AuditEvent.objects.filter(pk=old.pk).exists())
        self.assertIn('Dry run', out)

    def test_days_override(self):
        from patient_portal.models import AuditEvent
        # 400 days old; default 2190 would keep it, but --days 365 prunes it.
        old = self._make_event(days_ago=400)
        self._run(days=365)
        self.assertFalse(AuditEvent.objects.filter(pk=old.pk).exists())

    def test_empty_table_is_noop(self):
        from patient_portal.models import AuditEvent
        self.assertEqual(AuditEvent.objects.count(), 0)
        out = self._run(days=2190)
        self.assertIn('Nothing to prune', out)

    def test_all_newer_keeps_everything(self):
        from patient_portal.models import AuditEvent
        for _ in range(5):
            self._make_event(days_ago=1)
        self._run(days=30)
        self.assertEqual(AuditEvent.objects.count(), 5)

    def test_archive_writes_jsonl_then_deletes(self):
        from patient_portal.models import AuditEvent
        e1 = self._make_event(days_ago=3000, user_id='42', user_email='a@example.org',
                              detail={'note': 'x'})
        e2 = self._make_event(days_ago=2500, resource_id='rec-1')
        with tempfile.TemporaryDirectory() as tmpdir:
            archive_path = os.path.join(tmpdir, 'audit-archive.jsonl')
            self._run(days=2190, archive=archive_path)
            with open(archive_path, encoding='utf-8') as fh:
                lines = [line for line in fh.read().splitlines() if line]
        self.assertEqual(len(lines), 2)
        records = [json.loads(line) for line in lines]
        ids = {r['id'] for r in records}
        self.assertEqual(ids, {e1.pk, e2.pk})
        # timestamp serialized as ISO string
        for r in records:
            self.assertIsInstance(r['timestamp'], str)
            self.assertIn('T', r['timestamp'])
        # rows actually deleted
        self.assertFalse(AuditEvent.objects.filter(pk__in=[e1.pk, e2.pk]).exists())

    def test_archive_only_contains_matched_rows(self):
        from patient_portal.models import AuditEvent
        old = self._make_event(days_ago=3000)
        recent = self._make_event(days_ago=5)
        with tempfile.TemporaryDirectory() as tmpdir:
            archive_path = os.path.join(tmpdir, 'a.jsonl')
            self._run(days=2190, archive=archive_path)
            with open(archive_path, encoding='utf-8') as fh:
                records = [json.loads(line) for line in fh.read().splitlines() if line]
        self.assertEqual({r['id'] for r in records}, {old.pk})
        self.assertTrue(AuditEvent.objects.filter(pk=recent.pk).exists())

    def test_batching_handles_more_than_batch_size(self):
        from patient_portal.models import AuditEvent
        for _ in range(7):
            self._make_event(days_ago=3000)
        self._run(days=2190, batch_size=2)
        # All pruned; the prune records its own system audit event (#303), so
        # exclude that when asserting the old rows are gone.
        self.assertEqual(AuditEvent.objects.exclude(path='manage.py prune_audit_events').count(), 0)

    def test_batching_with_archive_captures_all_rows(self):
        from patient_portal.models import AuditEvent
        created = [self._make_event(days_ago=3000).pk for _ in range(5)]
        with tempfile.TemporaryDirectory() as tmpdir:
            archive_path = os.path.join(tmpdir, 'a.jsonl')
            self._run(days=2190, batch_size=2, archive=archive_path)
            with open(archive_path, encoding='utf-8') as fh:
                records = [json.loads(line) for line in fh.read().splitlines() if line]
        self.assertEqual({r['id'] for r in records}, set(created))
        self.assertEqual(AuditEvent.objects.exclude(path='manage.py prune_audit_events').count(), 0)

    def test_uses_settings_default_when_no_days(self):
        from django.test import override_settings
        from patient_portal.models import AuditEvent
        old = self._make_event(days_ago=400)
        recent = self._make_event(days_ago=100)
        with override_settings(AUDIT_EVENT_RETENTION_DAYS=200):
            self._run()
        self.assertFalse(AuditEvent.objects.filter(pk=old.pk).exists())
        self.assertTrue(AuditEvent.objects.filter(pk=recent.pk).exists())


# ---------------------------------------------------------------------------
# WS0 conformance fixes: password validators (#301) + proxy-auth render (#308)
# ---------------------------------------------------------------------------

class WS0PasswordValidationTest(TestCase):
    """Self-service password-set paths enforce AUTH_PASSWORD_VALIDATORS (PHR-S FM TI.1.1#06, #301)."""

    @classmethod
    def setUpTestData(cls):
        from omop_core.models import Organization
        cls.org = Organization.objects.create(name='WS0 Org', slug='ws0-org')
        cls.staff = Identity.objects.create_user(email='ws0-staff@test.com', password='pw', is_staff=True)
        cls.person = Person.objects.create(person_id=93001, family_name='Doe', given_name='Ada')
        PatientRecord.objects.create(person=cls.person, email='ws0pt@example.com')

    def _staff(self):
        c = APIClient()
        c.force_authenticate(user=self.staff)
        return c

    # --- signup ---

    def test_signup_rejects_common_password(self):
        resp = self._staff().post('/api/v1/patients/signup/', {
            'org': 'ws0-org', 'email': 'newpt@example.com', 'password': 'password',
        }, format='json')
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_signup_rejects_all_numeric_password(self):
        resp = self._staff().post('/api/v1/patients/signup/', {
            'org': 'ws0-org', 'email': 'newpt2@example.com', 'password': '48815762',
        }, format='json')
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_signup_accepts_strong_password(self):
        resp = self._staff().post('/api/v1/patients/signup/', {
            'org': 'ws0-org', 'email': 'strongpt@example.com', 'password': 'Zr7-quokka-vale',
        }, format='json')
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED, resp.data)

    # --- invite accept ---

    def _make_invite(self):
        self._staff().post(f'/api/v1/patients/{self.person.person_id}/invite/',
                           {'email': 'ws0pt@example.com'}, format='json')
        from patient_portal.models import PatientInvitation
        return PatientInvitation.objects.get(person=self.person)

    def test_invite_accept_rejects_common_password(self):
        inv = self._make_invite()
        resp = APIClient().post('/api/v1/patient-invitations/accept/',
                                {'token': inv.token, 'password': 'password'}, format='json')
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_invite_accept_accepts_strong_password(self):
        inv = self._make_invite()
        resp = APIClient().post('/api/v1/patient-invitations/accept/',
                                {'token': inv.token, 'password': 'Zr7-quokka-vale'}, format='json')
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.data)


class PersonalRepresentativeApiTest(TestCase):
    """Read-only proxy-authorization render endpoint (PHR-S FM PH.6.3#04, #308)."""

    @classmethod
    def setUpTestData(cls):
        from patient_portal.models import PatientUser
        from omop_core.models import PersonalRepresentative

        # Account holder A and their linked identity.
        cls.person_a = Person.objects.create(person_id=94001, family_name='Alpha', given_name='Ann')
        PatientRecord.objects.create(person=cls.person_a)
        cls.holder_a = Identity.objects.create_user(email='holder-a@test.com', password='pw')
        PatientUser.objects.create(identity=cls.holder_a, person=cls.person_a)

        # Unrelated account holder B.
        cls.person_b = Person.objects.create(person_id=94002, family_name='Beta', given_name='Bob')
        PatientRecord.objects.create(person=cls.person_b)
        cls.holder_b = Identity.objects.create_user(email='holder-b@test.com', password='pw')
        PatientUser.objects.create(identity=cls.holder_b, person=cls.person_b)

        # Representative R authorized over person A.
        cls.rep = Identity.objects.create_user(email='rep@test.com', password='pw')
        cls.grant = PersonalRepresentative.objects.create(
            representative=cls.rep, person_id=cls.person_a.person_id,
            relationship='caregiver', verification_status='VERIFIED',
        )
        cls.staff = Identity.objects.create_user(email='rep-staff@test.com', password='pw', is_staff=True)

    def _as(self, identity):
        c = APIClient()
        if identity is not None:
            c.force_authenticate(user=identity)
        return c

    def _rows(self, resp):
        return resp.data['results'] if isinstance(resp.data, dict) and 'results' in resp.data else resp.data

    def test_account_holder_sees_grants_over_own_record(self):
        resp = self._as(self.holder_a).get('/api/v1/personal-representatives/')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        rows = self._rows(resp)
        self.assertEqual([r['id'] for r in rows], [self.grant.id])

    def test_representative_sees_own_grant(self):
        resp = self._as(self.rep).get('/api/v1/personal-representatives/')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        rows = self._rows(resp)
        self.assertEqual([r['id'] for r in rows], [self.grant.id])
        self.assertEqual(rows[0]['representative_email'], 'rep@test.com')

    def test_unrelated_holder_sees_nothing(self):
        resp = self._as(self.holder_b).get('/api/v1/personal-representatives/')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(len(self._rows(resp)), 0)

    def test_staff_sees_all(self):
        resp = self._as(self.staff).get('/api/v1/personal-representatives/')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertGreaterEqual(len(self._rows(resp)), 1)

    def test_unauthenticated_denied(self):
        resp = self._as(None).get('/api/v1/personal-representatives/')
        self.assertIn(resp.status_code, (status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN))

    def test_endpoint_is_read_only(self):
        resp = self._as(self.staff).post('/api/v1/personal-representatives/', {
            'representative': self.rep.pk, 'person_id': self.person_b.person_id, 'relationship': 'other',
        }, format='json')
        self.assertEqual(resp.status_code, status.HTTP_405_METHOD_NOT_ALLOWED)


# ---------------------------------------------------------------------------
# #302 — TI.1.1 authentication controls: lockout, reuse policy, force-change
# ---------------------------------------------------------------------------

@override_settings(
    AUTH_LOCKOUT_THRESHOLD=3, AUTH_LOCKOUT_SECONDS=900,
    PASSWORD_HISTORY_SIZE=3, PASSWORD_REUSE_DAYS=180,
)
class AuthControlsTest(TestCase):
    """Account lockout, no-reuse policy, force-change, and change-password (#302)."""

    def setUp(self):
        from patient_portal.services import record_password
        self.email = 'auth-ctrl@test.com'
        self.password = 'Zr7-quokka-vale'
        self.identity = Identity.objects.create_user(email=self.email, password=self.password)
        record_password(self.identity)  # seed initial history
        self.client = APIClient()

    def _login(self, password):
        return self.client.post('/api/v1/auth/login/',
                                {'username': self.email, 'password': password}, format='json')

    def _authed(self):
        c = APIClient()
        c.force_authenticate(user=self.identity)
        return c

    # --- lockout (TI.1.1#03) ---

    def test_lockout_after_threshold_failures(self):
        for _ in range(3):
            self.assertEqual(self._login('wrong-password').status_code, status.HTTP_401_UNAUTHORIZED)
        self.identity.refresh_from_db()
        self.assertTrue(self.identity.is_locked)
        # Correct password is refused while locked.
        self.assertEqual(self._login(self.password).status_code, status.HTTP_401_UNAUTHORIZED)

    def test_successful_login_resets_failure_count(self):
        self._login('wrong-password')
        self._login('wrong-password')
        self.assertEqual(self._login(self.password).status_code, status.HTTP_200_OK)
        self.identity.refresh_from_db()
        self.assertEqual(self.identity.failed_login_count, 0)

    def test_lockout_expires(self):
        for _ in range(3):
            self._login('wrong-password')
        self.identity.refresh_from_db()
        self.identity.locked_until = timezone.now() - timedelta(seconds=1)
        self.identity.save(update_fields=['locked_until'])
        self.assertEqual(self._login(self.password).status_code, status.HTTP_200_OK)

    # --- no-reuse policy (TI.1.1#04/#05) ---

    def test_change_password_rejects_reuse_of_current(self):
        resp = self._authed().post('/api/v1/auth/change-password/',
                                   {'current_password': self.password, 'new_password': self.password}, format='json')
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_change_password_rejects_recently_used(self):
        c = self._authed()
        p2 = 'Br9-wombat-keel'
        self.assertEqual(c.post('/api/v1/auth/change-password/',
                                {'current_password': self.password, 'new_password': p2}, format='json').status_code, 200)
        # Going back to the original (still in history) is rejected.
        resp = c.post('/api/v1/auth/change-password/',
                      {'current_password': p2, 'new_password': self.password}, format='json')
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_change_password_accepts_new_unique(self):
        resp = self._authed().post('/api/v1/auth/change-password/',
                                   {'current_password': self.password, 'new_password': 'Cq2-badger-mint'}, format='json')
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.data)
        self.identity.refresh_from_db()
        self.assertTrue(self.identity.check_password('Cq2-badger-mint'))

    # --- change-password guards ---

    def test_change_password_wrong_current_rejected(self):
        resp = self._authed().post('/api/v1/auth/change-password/',
                                   {'current_password': 'nope', 'new_password': 'Cq2-badger-mint'}, format='json')
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_change_password_enforces_validators(self):
        resp = self._authed().post('/api/v1/auth/change-password/',
                                   {'current_password': self.password, 'new_password': 'password'}, format='json')
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_change_password_requires_auth(self):
        resp = APIClient().post('/api/v1/auth/change-password/',
                                {'current_password': self.password, 'new_password': 'Cq2-badger-mint'}, format='json')
        self.assertIn(resp.status_code, (status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN))

    # --- force-change (TI.1.1#09) ---

    def test_force_change_flag_set_and_cleared_by_change_password(self):
        from patient_portal.services import set_new_password
        set_new_password(self.identity, 'Tmp-reset-9021', must_change=True)
        self.identity.refresh_from_db()
        self.assertTrue(self.identity.must_change_password)
        # Surfaced to the client via /user/.
        r = self._authed().get('/api/v1/user/')
        self.assertTrue(r.data['user']['must_change_password'])
        # Changing the password clears the flag.
        r = self._authed().post('/api/v1/auth/change-password/',
                                {'current_password': 'Tmp-reset-9021', 'new_password': 'Cq2-badger-mint'}, format='json')
        self.assertEqual(r.status_code, status.HTTP_200_OK, r.data)
        self.identity.refresh_from_db()
        self.assertFalse(self.identity.must_change_password)

    # --- force-change enforcement (TI.1.1#09) — via a real session so the
    #     ForcePasswordChangeMiddleware sees the resolved request.user. ---

    def test_force_change_blocks_api_and_change_clears_it(self):
        from patient_portal.services import set_new_password
        set_new_password(self.identity, 'Tmp-reset-9021', must_change=True)
        # Login itself is allowed; the block applies to subsequent /api/ calls.
        self.assertEqual(self._login('Tmp-reset-9021').status_code, status.HTTP_200_OK)
        blocked = self.client.get('/api/v1/patient-records/')
        self.assertEqual(blocked.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(blocked.json().get('code'), 'password_change_required')
        # Exempt endpoints stay reachable so the client can resolve the state.
        self.assertEqual(self.client.get('/api/v1/user/').status_code, status.HTTP_200_OK)
        # Changing the password clears the flag and unblocks the API.
        changed = self.client.post('/api/v1/auth/change-password/',
                                   {'current_password': 'Tmp-reset-9021', 'new_password': 'Cq2-badger-mint'},
                                   format='json')
        self.assertEqual(changed.status_code, status.HTTP_200_OK, changed.data)
        self.identity.refresh_from_db()
        self.assertFalse(self.identity.must_change_password)
        self.assertNotEqual(self.client.get('/api/v1/patient-records/').status_code, status.HTTP_403_FORBIDDEN)

    def test_unflagged_session_is_not_blocked(self):
        self.assertEqual(self._login(self.password).status_code, status.HTTP_200_OK)
        resp = self.client.get('/api/v1/patient-records/')
        self.assertNotEqual(resp.status_code, status.HTTP_403_FORBIDDEN)

    def test_admin_action_sets_force_change_flag(self):
        from django.contrib.admin.sites import site
        from django.contrib.messages.storage.fallback import FallbackStorage
        from django.test import RequestFactory
        from patient_portal.admin import IdentityAdmin
        request = RequestFactory().post('/admin/')
        request.user = self.identity
        setattr(request, 'session', 'session')
        setattr(request, '_messages', FallbackStorage(request))
        IdentityAdmin(Identity, site).require_password_change(
            request, Identity.objects.filter(pk=self.identity.pk))
        self.identity.refresh_from_db()
        self.assertTrue(self.identity.must_change_password)


@override_settings(
    EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend',
    APP_BASE_URL='https://app.test',
    PASSWORD_HISTORY_SIZE=3, PASSWORD_REUSE_DAYS=180,
)
class PasswordResetFlowTest(TestCase):
    """Admin-initiated password reset via emailed single-use link (#302, TI.1.1#08)."""

    def setUp(self):
        from django.core import mail
        self.identity = Identity.objects.create_user(email='reset-me@test.com', password='Zr7-quokka-vale')
        mail.outbox = []

    def _uid_token(self):
        from django.contrib.auth.tokens import default_token_generator
        from django.utils.encoding import force_bytes
        from django.utils.http import urlsafe_base64_encode
        return (urlsafe_base64_encode(force_bytes(self.identity.pk)),
                default_token_generator.make_token(self.identity))

    def test_admin_action_emails_reset_link(self):
        from django.core import mail
        from patient_portal.api.password_reset import send_password_reset_email
        send_password_reset_email(self.identity)
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn('reset-me@test.com', mail.outbox[0].to)
        self.assertIn('/reset-password?uid=', mail.outbox[0].body)

    def test_reset_with_valid_link_sets_password(self):
        uid, token = self._uid_token()
        resp = APIClient().post('/api/v1/auth/reset-password/',
                                {'uid': uid, 'token': token, 'new_password': 'Cq2-badger-mint'}, format='json')
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.data)
        self.identity.refresh_from_db()
        self.assertTrue(self.identity.check_password('Cq2-badger-mint'))

    def test_reset_link_is_single_use(self):
        uid, token = self._uid_token()
        body = {'uid': uid, 'token': token, 'new_password': 'Cq2-badger-mint'}
        self.assertEqual(APIClient().post('/api/v1/auth/reset-password/', body, format='json').status_code, 200)
        # Token is tied to the (now-changed) password hash, so it no longer validates.
        resp = APIClient().post('/api/v1/auth/reset-password/',
                                {'uid': uid, 'token': token, 'new_password': 'Dp4-otter-lime'}, format='json')
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_reset_rejects_invalid_token(self):
        uid, _ = self._uid_token()
        resp = APIClient().post('/api/v1/auth/reset-password/',
                                {'uid': uid, 'token': 'bogus-token', 'new_password': 'Cq2-badger-mint'}, format='json')
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_reset_enforces_validators(self):
        uid, token = self._uid_token()
        resp = APIClient().post('/api/v1/auth/reset-password/',
                                {'uid': uid, 'token': token, 'new_password': 'password'}, format='json')
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)


# ---------------------------------------------------------------------------
# #303 — standards-based audit format + audit-log-access + admin/background triggers
# ---------------------------------------------------------------------------

class AuditStandardsTest(TestCase):
    """FHIR AuditEvent output (TI.2.2#01), audit-review + admin classification,
    and background-command auditing (TI.2.1)."""

    @classmethod
    def setUpTestData(cls):
        cls.staff = Identity.objects.create_user(email='audit-std@test.com', password='pw', is_staff=True)

    def _staff(self):
        c = APIClient()
        c.force_authenticate(user=self.staff)
        return c

    def _mk(self, **kw):
        from patient_portal.models import AuditEvent
        d = dict(event_type=AuditEvent.EVENT_VIEW, method='GET', path='/api/x/', status_code=200)
        d.update(kw)
        return AuditEvent.objects.create(**d)

    def test_classification_of_admin_and_audit_review(self):
        from django.test import RequestFactory
        from patient_portal.api.middleware import _classify_event_type
        rf = RequestFactory()
        self.assertEqual(_classify_event_type(rf.get('/api/v1/audit-events/')), 'audit_review')
        self.assertEqual(_classify_event_type(rf.get('/api/v1/audit-events/fhir/')), 'audit_review')
        self.assertEqual(_classify_event_type(rf.post('/admin/patient_portal/identity/1/change/')), 'admin')

    def test_fhir_endpoint_returns_auditevent_bundle(self):
        self._mk(event_type='record_create', method='POST', path='/api/v1/measurements/',
                 status_code=201, user_id='7', user_email='u@test.com', ip_address='10.0.0.1')
        resp = self._staff().get('/api/v1/audit-events/fhir/')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data['resourceType'], 'Bundle')
        self.assertEqual(resp.data['type'], 'searchset')
        resources = [e['resource'] for e in resp.data['entry']]
        self.assertTrue(all(r['resourceType'] == 'AuditEvent' for r in resources))
        created = next(r for r in resources if r['action'] == 'C')
        self.assertEqual(created['outcome'], '0')  # 201 -> success
        self.assertEqual(created['agent'][0]['who']['identifier']['value'], '7')
        self.assertEqual(created['agent'][0]['network']['address'], '10.0.0.1')

    def test_fhir_outcome_maps_failure_status(self):
        self._mk(event_type='record_view', method='GET', status_code=403, user_id='7')
        resp = self._staff().get('/api/v1/audit-events/fhir/', {'method': 'GET'})
        outcomes = {e['resource']['outcome'] for e in resp.data['entry']}
        self.assertIn('4', outcomes)  # 403 -> minor failure

    def test_fhir_output_is_scoped_for_non_staff(self):
        from patient_portal.models import PatientUser
        person = Person.objects.create(person_id=95001)
        patient = Identity.objects.create_user(email='pt-fhir@test.com', password='pw')
        PatientUser.objects.create(identity=patient, person=person)
        self._mk(user_id=str(patient.pk))
        self._mk(user_id='99999')  # someone else
        c = APIClient()
        c.force_authenticate(user=patient)
        resp = c.get('/api/v1/audit-events/fhir/')
        who = {e['resource']['agent'][0]['who']['identifier']['value'] for e in resp.data['entry']}
        self.assertEqual(who, {str(patient.pk)})

    def test_prune_command_records_system_audit_event(self):
        from datetime import timedelta
        from django.utils import timezone as tz
        from django.core.management import call_command
        from patient_portal.models import AuditEvent
        old = self._mk(user_id='1')
        AuditEvent.objects.filter(pk=old.pk).update(timestamp=tz.now() - timedelta(days=4000))
        call_command('prune_audit_events', '--days', '30')
        sys_events = AuditEvent.objects.filter(event_type='admin', path='manage.py prune_audit_events')
        self.assertEqual(sys_events.count(), 1)
        self.assertGreaterEqual(sys_events.first().detail['deleted'], 1)
        self.assertEqual(sys_events.first().user_id, 'system')


# ---------------------------------------------------------------------------
# #304 — audit indelibility / tamper-evidence (TI.2.2.1) + break-glass (TI.2.3#04)
# ---------------------------------------------------------------------------

class AuditIndelibilityTest(TestCase):
    """Per-row HMAC tamper-evidence + verify command + delete restriction."""

    def _mk(self, **kw):
        from patient_portal.models import AuditEvent
        d = dict(event_type='record_view', method='GET', path='/api/x/', status_code=200)
        d.update(kw)
        return AuditEvent.objects.create(**d)

    def test_new_event_is_signed_and_valid(self):
        row = self._mk()
        self.assertTrue(row.signature)
        self.assertTrue(row.signature_valid())

    def test_tampering_breaks_signature(self):
        from patient_portal.models import AuditEvent
        row = self._mk(path='/api/original/')
        AuditEvent.objects.filter(pk=row.pk).update(path='/api/HACKED/')  # bypasses save()
        row.refresh_from_db()
        self.assertFalse(row.signature_valid())

    def test_verify_command_passes_when_clean(self):
        from django.core.management import call_command
        self._mk()
        self._mk()
        call_command('verify_audit_integrity')  # must not raise

    def test_verify_command_detects_tampering(self):
        from django.core.management import call_command
        from patient_portal.models import AuditEvent
        row = self._mk()
        AuditEvent.objects.filter(pk=row.pk).update(status_code=500)  # tamper, signature untouched
        with self.assertRaises(SystemExit):
            call_command('verify_audit_integrity')

    def test_admin_delete_is_denied(self):
        from django.contrib.admin.sites import AdminSite
        from patient_portal.admin import AuditEventAdmin
        from patient_portal.models import AuditEvent
        admin_obj = AuditEventAdmin(AuditEvent, AdminSite())
        self.assertFalse(admin_obj.has_delete_permission(None))
        self.assertFalse(admin_obj.has_change_permission(None))


class BreakGlassTest(TestCase):
    """Emergency-access authorization for audit review (TI.2.3#04)."""

    @classmethod
    def setUpTestData(cls):
        from omop_core.models import Organization, GroupAccess
        from patient_portal.models import PatientUser
        cls.org = Organization.objects.create(name='BG Org', slug='bg-org')
        cls.org_admin = Identity.objects.create_user(email='bg-admin@test.com', password='pw')
        GroupAccess.objects.create(identity=cls.org_admin, org=cls.org, role='org_admin')
        cls.person = Person.objects.create(person_id=96001)
        cls.patient = Identity.objects.create_user(email='bg-patient@test.com', password='pw')
        PatientUser.objects.create(identity=cls.patient, person=cls.person)

    def _c(self, ident):
        c = APIClient()
        c.force_authenticate(user=ident)
        return c

    def _rows(self, resp):
        return resp.data['results'] if isinstance(resp.data, dict) and 'results' in resp.data else resp.data

    def test_patient_cannot_break_glass(self):
        resp = self._c(self.patient).post('/api/v1/break-glass/',
                                          {'person_id': 96001, 'reason': 'x'}, format='json')
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)

    def test_break_glass_requires_reason(self):
        resp = self._c(self.org_admin).post('/api/v1/break-glass/', {'person_id': 96001}, format='json')
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_break_glass_creates_grant_with_reason(self):
        from patient_portal.models import BreakGlassGrant
        resp = self._c(self.org_admin).post('/api/v1/break-glass/',
                                            {'person_id': 96001, 'reason': 'ED admission'}, format='json')
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED, resp.data)
        grant = BreakGlassGrant.objects.get(identity=self.org_admin, person_id=96001)
        self.assertEqual(grant.reason, 'ED admission')
        self.assertTrue(grant.is_active)

    def test_break_glass_grants_audit_visibility(self):
        from patient_portal.models import AuditEvent, BreakGlassGrant
        from datetime import timedelta
        from django.utils import timezone as tz
        # An audit entry about the patient's record, recorded for someone else.
        AuditEvent.objects.create(
            event_type='record_view', method='GET', path='/api/patient-info/96001/',
            status_code=200, user_id='99999', resource_id='96001',
        )
        # Before break-glass: the org_admin (non-privileged for audit) sees none of it.
        before = self._rows(self._c(self.org_admin).get('/api/v1/audit-events/'))
        self.assertNotIn('96001', {r['resource_id'] for r in before})
        # Break glass, then the patient's audit entry becomes visible.
        self._c(self.org_admin).post('/api/v1/break-glass/',
                                     {'person_id': 96001, 'reason': 'emergency'}, format='json')
        after = self._rows(self._c(self.org_admin).get('/api/v1/audit-events/'))
        self.assertIn('96001', {r['resource_id'] for r in after})
        # Once the grant expires, visibility is revoked.
        BreakGlassGrant.objects.filter(identity=self.org_admin, person_id=96001).update(
            expires_at=tz.now() - timedelta(seconds=1))
        expired = self._rows(self._c(self.org_admin).get('/api/v1/audit-events/'))
        self.assertNotIn('96001', {r['resource_id'] for r in expired})


# ---------------------------------------------------------------------------
# Data-exchange integrity, non-repudiation, multi-version interchange, and
# interchange agreements (PHR-S FM S.3.6#10 / PH.2.3#09 / TI.5.2#01 / TI.5.4#01,
# issue #306).
# ---------------------------------------------------------------------------

class ExchangeIntegrityTest(FhirUploadBase):
    """Content-integrity verification on import, digest/signature on export, FHIR
    version negotiation."""

    def _post_bundle(self, extra_headers=None, query=''):
        bundle_bytes = json.dumps(_make_fhir_bundle()).encode('utf-8')
        fhir_file = io.BytesIO(bundle_bytes)
        fhir_file.name = 'test_bundle.json'
        headers = extra_headers or {}
        return bundle_bytes, self.client.post(
            f'/api/patient-info/upload_fhir/{query}',
            {'file': fhir_file},
            format='multipart',
            **headers,
        )

    # --- Import: content integrity (S.3.6#10 / PH.2.3#09) ---

    def test_import_without_digest_header_unchanged(self):
        """No integrity header -> current behavior preserved (opt-in)."""
        _, resp = self._post_bundle()
        self.assertIn(resp.status_code,
                      [status.HTTP_200_OK, status.HTTP_201_CREATED],
                      msg=f'Upload without digest failed: {resp.data}')

    def test_import_matching_digest_accepted(self):
        import hashlib as _hashlib
        bundle_bytes = json.dumps(_make_fhir_bundle()).encode('utf-8')
        digest = _hashlib.sha256(bundle_bytes).hexdigest()
        fhir_file = io.BytesIO(bundle_bytes)
        fhir_file.name = 'test_bundle.json'
        resp = self.client.post(
            '/api/patient-info/upload_fhir/',
            {'file': fhir_file},
            format='multipart',
            HTTP_X_CONTENT_SHA256=digest,
        )
        self.assertIn(resp.status_code,
                      [status.HTTP_200_OK, status.HTTP_201_CREATED],
                      msg=f'Upload with matching digest failed: {resp.data}')

    def test_import_mismatched_digest_rejected(self):
        bundle_bytes = json.dumps(_make_fhir_bundle()).encode('utf-8')
        fhir_file = io.BytesIO(bundle_bytes)
        fhir_file.name = 'test_bundle.json'
        resp = self.client.post(
            '/api/patient-info/upload_fhir/',
            {'file': fhir_file},
            format='multipart',
            HTTP_X_CONTENT_SHA256='deadbeef' * 8,  # wrong 64-char hex
        )
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('integrity', str(resp.data).lower())
        # And nothing was ingested.
        self.assertIsNone(self._get_person())

    def test_import_rfc3230_base64_digest_accepted(self):
        import hashlib as _hashlib, base64 as _b64
        bundle_bytes = json.dumps(_make_fhir_bundle()).encode('utf-8')
        b64 = _b64.b64encode(_hashlib.sha256(bundle_bytes).digest()).decode()
        fhir_file = io.BytesIO(bundle_bytes)
        fhir_file.name = 'test_bundle.json'
        resp = self.client.post(
            '/api/patient-info/upload_fhir/',
            {'file': fhir_file},
            format='multipart',
            HTTP_DIGEST=f'sha-256={b64}',
        )
        self.assertIn(resp.status_code,
                      [status.HTTP_200_OK, status.HTTP_201_CREATED],
                      msg=f'RFC3230 digest upload failed: {resp.data}')

    # --- Export: digest + signature (S.3.6#10 / PH.2.3#09) ---

    def _uploaded_person_id(self):
        _, resp = self._post_bundle()
        self.assertIn(resp.status_code,
                      [status.HTTP_200_OK, status.HTTP_201_CREATED])
        person = self._get_person()
        self.assertIsNotNone(person)
        return person.person_id

    def test_export_emits_digest_and_signature(self):
        import hashlib as _hashlib
        from patient_portal.api.fhir.integrity import (
            EXPORT_DIGEST_HEADER, EXPORT_SIGNATURE_HEADER, signature_valid,
        )
        person_id = self._uploaded_person_id()
        resp = self.client.get(f'/api/v1/patient-records/{person_id}/export-fhir/')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        digest = resp.headers.get(EXPORT_DIGEST_HEADER)
        signature = resp.headers.get(EXPORT_SIGNATURE_HEADER)
        self.assertIsNotNone(digest, 'export missing digest header')
        self.assertIsNotNone(signature, 'export missing signature header')
        # Digest must match the exact serialized body.
        self.assertEqual(digest, _hashlib.sha256(resp.content).hexdigest())
        # Signature must verify against the body.
        self.assertTrue(signature_valid(resp.content, signature))

    def test_export_body_is_fhir_bundle(self):
        person_id = self._uploaded_person_id()
        resp = self.client.get(f'/api/v1/patient-records/{person_id}/export-fhir/')
        body = json.loads(resp.content)
        self.assertEqual(body.get('resourceType'), 'Bundle')

    # --- Multi-version interchange (TI.5.2#01) ---

    def test_import_unsupported_version_rejected(self):
        _, resp = self._post_bundle(query='?fhirVersion=STU3')
        self.assertEqual(resp.status_code, status.HTTP_406_NOT_ACCEPTABLE)

    def test_import_supported_version_accepted(self):
        _, resp = self._post_bundle(query='?fhirVersion=R4')
        self.assertIn(resp.status_code,
                      [status.HTTP_200_OK, status.HTTP_201_CREATED])

    def test_export_unsupported_version_rejected(self):
        person_id = self._uploaded_person_id()
        resp = self.client.get(
            f'/api/v1/patient-records/{person_id}/export-fhir/?fhirVersion=3.0')
        self.assertEqual(resp.status_code, status.HTTP_406_NOT_ACCEPTABLE)

    def test_smart_configuration_advertises_fhir_version(self):
        from patient_portal.api.fhir.integrity import SUPPORTED_FHIR_VERSION
        resp = self.client.get('/.well-known/smart-configuration')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data.get('fhirVersion'), SUPPORTED_FHIR_VERSION)


class IntegrityHelperUnitTest(TestCase):
    """Unit coverage for the integrity helper (shared by upload + sync paths)."""

    def setUp(self):
        from django.test import RequestFactory
        self.factory = RequestFactory()

    def test_verify_no_header_returns_none(self):
        from patient_portal.api.fhir.integrity import verify_content_digest
        req = self.factory.post('/x')
        self.assertIsNone(verify_content_digest(req, b'{"a":1}'))

    def test_verify_matching_hex_ok(self):
        import hashlib as _hashlib
        from patient_portal.api.fhir.integrity import verify_content_digest
        payload = b'{"resourceType":"Bundle"}'
        digest = _hashlib.sha256(payload).hexdigest()
        req = self.factory.post('/x', HTTP_X_CONTENT_SHA256=digest)
        self.assertIsNone(verify_content_digest(req, payload))

    def test_verify_mismatch_returns_error(self):
        from patient_portal.api.fhir.integrity import verify_content_digest
        req = self.factory.post('/x', HTTP_X_CONTENT_SHA256='00' * 32)
        self.assertIsNotNone(verify_content_digest(req, b'payload'))

    def test_signature_roundtrip(self):
        from patient_portal.api.fhir.integrity import export_signature, signature_valid
        data = b'some bundle bytes'
        sig = export_signature(data)
        self.assertTrue(signature_valid(data, sig))
        self.assertFalse(signature_valid(b'tampered', sig))

    def test_check_fhir_version(self):
        from patient_portal.api.fhir.integrity import check_fhir_version
        self.assertIsNone(check_fhir_version(self.factory.get('/x')))
        self.assertIsNone(check_fhir_version(self.factory.get('/x?fhirVersion=4.0.1')))
        self.assertIsNone(check_fhir_version(self.factory.get('/x?fhirVersion=R4')))
        self.assertIsNotNone(check_fhir_version(self.factory.get('/x?fhirVersion=STU3')))


class InterchangeAgreementTest(TestCase):
    """Documented interchange-agreement artifact (TI.5.4#01)."""

    @classmethod
    def setUpTestData(cls):
        cls.admin = Identity.objects.create_superuser(
            email='ia-admin@test.com', password='testpass')
        from omop_core.models import InterchangeAgreement
        cls.agreement = InterchangeAgreement.objects.create(
            partner_name='Acme Health Information Exchange',
            standards_supported=['FHIR'],
            standard_versions=['R4'],
            effective_date=date(2026, 1, 1),
            status=InterchangeAgreement.STATUS_ACTIVE,
            active=True,
        )

    def setUp(self):
        self.client = APIClient()

    def _rows(self, resp):
        data = resp.data
        return data['results'] if isinstance(data, dict) and 'results' in data else data

    def test_list_requires_authentication(self):
        resp = self.client.get('/api/v1/interchange-agreements/')
        self.assertIn(resp.status_code,
                      [status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN])

    def test_list_returns_agreements(self):
        self.client.force_authenticate(user=self.admin)
        resp = self.client.get('/api/v1/interchange-agreements/')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        names = [r['partner_name'] for r in self._rows(resp)]
        self.assertIn('Acme Health Information Exchange', names)

    def test_detail_includes_in_effect_and_standards(self):
        self.client.force_authenticate(user=self.admin)
        resp = self.client.get(
            f'/api/v1/interchange-agreements/{self.agreement.id}/')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data['standard_versions'], ['R4'])
        self.assertTrue(resp.data['in_effect'])

    def test_endpoint_is_read_only(self):
        self.client.force_authenticate(user=self.admin)
        resp = self.client.post(
            '/api/v1/interchange-agreements/',
            {'partner_name': 'X', 'effective_date': '2026-01-01'},
            format='json')
        self.assertEqual(resp.status_code, status.HTTP_405_METHOD_NOT_ALLOWED)

    def test_is_in_effect_logic(self):
        from omop_core.models import InterchangeAgreement
        expired = InterchangeAgreement.objects.create(
            partner_name='Expired Partner',
            effective_date=date(2020, 1, 1),
            expiry_date=date(2020, 12, 31),
            status=InterchangeAgreement.STATUS_ACTIVE,
            active=True,
        )
        self.assertFalse(expired.is_in_effect())
        suspended = InterchangeAgreement.objects.create(
            partner_name='Suspended Partner',
            effective_date=date(2020, 1, 1),
            status=InterchangeAgreement.STATUS_SUSPENDED,
            active=False,
        )
        self.assertFalse(suspended.is_in_effect())
        self.assertTrue(self.agreement.is_in_effect())

    def test_registered_in_admin(self):
        from django.contrib import admin as dj_admin
        from omop_core.models import InterchangeAgreement
        self.assertIn(InterchangeAgreement, dj_admin.site._registry)


# ---------------------------------------------------------------------------
# #308 remainder — message confidentiality levels (PHR-S FM PH.6.3#08)
# ---------------------------------------------------------------------------

class PatientMessageConfidentialityTest(TestCase):
    """Confidentiality tagging restricts sensitive messages to their sender + the patient."""

    @classmethod
    def setUpTestData(cls):
        from patient_portal.models import PatientUser, PatientMessage
        _make_vocab_fixtures()
        cls.person = Person.objects.create(person_id=98501, family_name='Conf', given_name='Cara')
        PatientRecord.objects.create(person=cls.person)
        cls.patient = Identity.objects.create_user(email='conf-pt@test.com', password='pw')
        cls.pu = PatientUser.objects.create(identity=cls.patient, person=cls.person)
        cls.staff_a = Identity.objects.create_user(email='conf-staffa@test.com', password='pw', is_staff=True)
        cls.staff_b = Identity.objects.create_user(email='conf-staffb@test.com', password='pw', is_staff=True)
        cls.restricted = PatientMessage.objects.create(
            patient_user=cls.pu, sender=cls.staff_a, subject='sensitive', message='...',
            sender_is_patient=False, confidentiality=PatientMessage.CONFIDENTIALITY_RESTRICTED,
        )
        cls.normal = PatientMessage.objects.create(
            patient_user=cls.pu, sender=cls.staff_a, subject='routine', message='...',
            sender_is_patient=False, confidentiality=PatientMessage.CONFIDENTIALITY_NORMAL,
        )

    def _c(self, ident):
        c = APIClient()
        c.force_authenticate(user=ident)
        return c

    def _ids(self, resp):
        rows = resp.data['results'] if isinstance(resp.data, dict) and 'results' in resp.data else resp.data
        return {r['id'] for r in rows}

    def test_default_confidentiality_is_normal_and_exposed(self):
        resp = self._c(self.patient).post('/api/v1/messages/', {'subject': 'q', 'message': 'hi'}, format='json')
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED, resp.data)
        self.assertEqual(resp.data['confidentiality'], 'normal')

    def test_patient_can_set_confidentiality(self):
        resp = self._c(self.patient).post(
            '/api/v1/messages/', {'subject': 'q', 'message': 'hi', 'confidentiality': 'very_restricted'}, format='json')
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED, resp.data)
        self.assertEqual(resp.data['confidentiality'], 'very_restricted')

    def test_restricted_hidden_from_other_staff(self):
        ids = self._ids(self._c(self.staff_b).get('/api/v1/messages/'))
        self.assertNotIn(self.restricted.id, ids)   # not the sender → hidden
        self.assertIn(self.normal.id, ids)          # normal still visible

    def test_restricted_visible_to_sender_staff(self):
        ids = self._ids(self._c(self.staff_a).get('/api/v1/messages/'))
        self.assertIn(self.restricted.id, ids)

    def test_restricted_visible_to_account_holder(self):
        ids = self._ids(self._c(self.patient).get('/api/v1/messages/'))
        self.assertIn(self.restricted.id, ids)      # the patient always sees their own thread

    def test_other_staff_cannot_retrieve_restricted(self):
        resp = self._c(self.staff_b).get(f'/api/v1/messages/{self.restricted.id}/')
        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)


# ---------------------------------------------------------------------------
# #318 — audit hash chain (row-deletion detection, PHR-S FM TI.2.2.1)
# ---------------------------------------------------------------------------

class AuditHashChainTest(TestCase):
    """Hash-chaining makes audit-row deletion/insertion detectable."""

    def _mk(self, **kw):
        from patient_portal.models import AuditEvent
        d = dict(event_type='record_view', method='GET', path='/api/x/', status_code=200)
        d.update(kw)
        return AuditEvent.objects.create(**d)

    def test_rows_are_chained_and_verify_clean(self):
        from django.core.management import call_command
        a, b, c = self._mk(), self._mk(), self._mk()
        self.assertTrue(a.chain_hash and b.chain_hash and c.chain_hash)
        self.assertNotEqual(a.chain_hash, b.chain_hash)  # chain advances even for identical content
        call_command('verify_audit_integrity')  # must not raise

    def test_middle_row_deletion_is_detected(self):
        from django.core.management import call_command
        from patient_portal.models import AuditEvent
        a, b, c = self._mk(), self._mk(), self._mk()
        AuditEvent.objects.filter(pk=b.pk).delete()  # excise the middle row
        with self.assertRaises(SystemExit):
            call_command('verify_audit_integrity')

    def test_oldest_deletion_is_tolerated(self):
        """Pruning the oldest rows (retention) must NOT be flagged — the earliest
        surviving row is the chain anchor."""
        from django.core.management import call_command
        from patient_portal.models import AuditEvent
        a, b, c = self._mk(), self._mk(), self._mk()
        AuditEvent.objects.filter(pk=a.pk).delete()
        call_command('verify_audit_integrity')  # must not raise

    def test_alteration_still_detected_alongside_chain(self):
        from django.core.management import call_command
        from patient_portal.models import AuditEvent
        a = self._mk()
        self._mk()
        AuditEvent.objects.filter(pk=a.pk).update(path='/api/HACKED/')  # content tamper
        with self.assertRaises(SystemExit):
            call_command('verify_audit_integrity')

    def test_chaining_can_be_disabled(self):
        from django.test import override_settings
        with override_settings(AUDIT_HASH_CHAIN_ENABLED=False):
            row = self._mk()
        self.assertEqual(row.chain_hash, '')


# ---------------------------------------------------------------------------
# Patient Role Phase 1 Tests
# ---------------------------------------------------------------------------

class PatientRoleModelTest(TestCase):
    """Test patient role in GroupAccess and allows_patient_signup on Organization."""

    def test_patient_role_choice_valid(self):
        org = Organization.objects.create(name='PR Org', slug='pr-org')
        user = Identity.objects.create_user(email='patient-role@test.com', password='pw')
        grant = GroupAccess.objects.create(identity=user, org=org, role='patient')
        self.assertEqual(grant.role, 'patient')

    def test_allows_patient_signup_default_false(self):
        org = Organization.objects.create(name='NoSignup', slug='no-signup')
        self.assertFalse(org.allows_patient_signup)

    def test_allows_patient_signup_settable(self):
        org = Organization.objects.create(name='SignupOrg', slug='signup-org', allows_patient_signup=True)
        org.refresh_from_db()
        self.assertTrue(org.allows_patient_signup)

    def test_org_invitation_patient_role(self):
        org = Organization.objects.create(name='InvOrg', slug='inv-org')
        inv = OrgInvitation.objects.create(
            org=org, email='pt@test.com', role='patient',
            token='a' * 64,
            expires_at=timezone.now() + timezone.timedelta(days=7),
        )
        self.assertEqual(inv.role, 'patient')

    def test_org_invitation_person_fk(self):
        from omop_core.models import Person
        org = Organization.objects.create(name='FKOrg', slug='fk-org')
        person = Person.objects.create(person_id=99990, year_of_birth=1990,
                                       gender_source_value='F', race_source_value='unknown',
                                       ethnicity_source_value='unknown')
        inv = OrgInvitation.objects.create(
            org=org, email='linked@test.com', role='patient', person=person,
            token='b' * 64,
            expires_at=timezone.now() + timezone.timedelta(days=7),
        )
        inv.refresh_from_db()
        self.assertEqual(inv.person_id, person.pk)


class PatientPersonForWithPatientGrantTest(TestCase):
    """patient_person_for() should still resolve patients who have a 'patient' GroupAccess."""

    def setUp(self):
        from omop_core.models import Person
        from patient_portal.models import PatientUser
        self.org = Organization.objects.create(name='PPF Org', slug='ppf-org')
        self.identity = Identity.objects.create_user(email='ppf-patient@test.com', password='pw')
        self.person = Person.objects.create(
            person_id=99991, year_of_birth=1985,
            gender_source_value='M', race_source_value='unknown',
            ethnicity_source_value='unknown',
        )
        PatientUser.objects.create(identity=self.identity, person=self.person)

    def test_no_grant_is_patient(self):
        from patient_portal.services import patient_person_for
        self.assertEqual(patient_person_for(self.identity), self.person)

    def test_patient_grant_still_patient(self):
        from patient_portal.services import patient_person_for
        GroupAccess.objects.create(identity=self.identity, org=self.org, role='patient')
        self.assertEqual(patient_person_for(self.identity), self.person)

    def test_doctor_grant_not_patient(self):
        from patient_portal.services import patient_person_for
        GroupAccess.objects.create(identity=self.identity, org=self.org, role='doctor')
        self.assertIsNone(patient_person_for(self.identity))

    def test_mixed_patient_and_doctor_grant_not_patient(self):
        from patient_portal.services import patient_person_for
        org2 = Organization.objects.create(name='PPF Org2', slug='ppf-org2')
        GroupAccess.objects.create(identity=self.identity, org=self.org, role='patient')
        GroupAccess.objects.create(identity=self.identity, org=org2, role='doctor')
        self.assertIsNone(patient_person_for(self.identity))


@override_settings(
    EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend',
    APP_BASE_URL='https://app.test',
)
class PatientInviteViaOrgTest(TestCase):
    """Test inviting a patient via /api/orgs/{slug}/invite/ with role=patient."""

    def setUp(self):
        self.client = APIClient()
        self.staff = _make_user('pt-inv-staff@test.com', is_staff=True)
        self.org = _make_org('Pt Inv Org', 'pt-inv-org')
        self.client.force_authenticate(user=self.staff)

    def test_invite_patient_creates_invitation(self):
        resp = self.client.post('/api/orgs/pt-inv-org/invite/', {
            'email': 'newpatient@test.com',
            'role': 'patient',
        })
        self.assertEqual(resp.status_code, 201)
        inv = OrgInvitation.objects.get(org=self.org, email='newpatient@test.com')
        self.assertEqual(inv.role, 'patient')
        self.assertIsNone(inv.person)

    def test_invite_patient_with_person_id(self):
        from omop_core.models import Person
        person = Person.objects.create(
            person_id=99992, year_of_birth=1970,
            gender_source_value='F', race_source_value='unknown',
            ethnicity_source_value='unknown',
        )
        resp = self.client.post('/api/orgs/pt-inv-org/invite/', {
            'email': 'linkedpt@test.com',
            'role': 'patient',
            'person_id': person.person_id,
        })
        self.assertEqual(resp.status_code, 201)
        inv = OrgInvitation.objects.get(org=self.org, email='linkedpt@test.com')
        self.assertEqual(inv.person_id, person.pk)

    def test_person_id_rejected_for_non_patient_role(self):
        from omop_core.models import Person
        person = Person.objects.create(
            person_id=99993, year_of_birth=1970,
            gender_source_value='F', race_source_value='unknown',
            ethnicity_source_value='unknown',
        )
        resp = self.client.post('/api/orgs/pt-inv-org/invite/', {
            'email': 'doc@test.com',
            'role': 'doctor',
            'person_id': person.person_id,
        })
        self.assertEqual(resp.status_code, 400)
        self.assertIn('person_id', resp.data['error'])

    def test_confirm_patient_invite_creates_patient_user(self):
        from patient_portal.models import PatientUser
        # Create invitation
        resp = self.client.post('/api/orgs/pt-inv-org/invite/', {
            'email': 'confirm-pt@test.com',
            'role': 'patient',
        })
        self.assertEqual(resp.status_code, 201)
        token = OrgInvitation.objects.get(email='confirm-pt@test.com').token

        # Confirm (unauthenticated)
        anon = APIClient()
        resp = anon.post('/api/orgs/confirm-invitation/', {'token': token})
        self.assertEqual(resp.status_code, 200)
        self.assertIn('/org/pt-inv-org/', resp.data.get('redirect_url', ''))

        identity = Identity.objects.get(email='confirm-pt@test.com', issuer='urn:local')
        self.assertTrue(PatientUser.objects.filter(identity=identity).exists())
        self.assertTrue(
            GroupAccess.objects.filter(identity=identity, org=self.org, role='patient').exists()
        )

    def test_confirm_patient_invite_with_person_links_existing(self):
        from omop_core.models import Person
        from patient_portal.models import PatientUser
        person = Person.objects.create(
            person_id=99994, year_of_birth=1980,
            gender_source_value='M', race_source_value='unknown',
            ethnicity_source_value='unknown',
        )
        resp = self.client.post('/api/orgs/pt-inv-org/invite/', {
            'email': 'link-pt@test.com',
            'role': 'patient',
            'person_id': person.person_id,
        })
        self.assertEqual(resp.status_code, 201)
        token = OrgInvitation.objects.get(email='link-pt@test.com').token

        anon = APIClient()
        resp = anon.post('/api/orgs/confirm-invitation/', {'token': token})
        self.assertEqual(resp.status_code, 200)

        identity = Identity.objects.get(email='link-pt@test.com', issuer='urn:local')
        pu = PatientUser.objects.get(identity=identity)
        self.assertEqual(pu.person_id, person.pk)

    def test_invitation_email_uses_org_scoped_url(self):
        from django.core import mail
        self.client.post('/api/orgs/pt-inv-org/invite/', {
            'email': 'email-check@test.com',
            'role': 'doctor',
        })
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn('/org/pt-inv-org/accept-invite?token=', mail.outbox[0].body)


@override_settings(
    EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend',
    APP_BASE_URL='https://app.test',
)
class OrgPatientSignupTest(TestCase):
    """Test the public patient self-signup endpoint."""

    def setUp(self):
        self.org = Organization.objects.create(
            name='Signup Org', slug='signup-org', allows_patient_signup=True,
        )
        self.no_signup_org = Organization.objects.create(
            name='No Signup', slug='no-signup-org', allows_patient_signup=False,
        )

    def test_signup_creates_account(self):
        from patient_portal.models import PatientUser
        resp = APIClient().post('/api/v1/orgs/signup-org/patient-signup/', {
            'email': 'self-signup@test.com',
            'password': 'Str0ng!Pass99',
            'given_name': 'Test',
            'family_name': 'Patient',
        })
        self.assertEqual(resp.status_code, 201)
        self.assertIn('person_id', resp.data)
        self.assertEqual(resp.data['redirect_url'], '/org/signup-org/')

        identity = Identity.objects.get(email='self-signup@test.com')
        self.assertTrue(identity.has_usable_password())
        self.assertTrue(PatientUser.objects.filter(identity=identity).exists())
        self.assertTrue(
            GroupAccess.objects.filter(identity=identity, org=self.org, role='patient').exists()
        )

    def test_signup_disabled_returns_403(self):
        resp = APIClient().post('/api/v1/orgs/no-signup-org/patient-signup/', {
            'email': 'blocked@test.com',
            'password': 'Str0ng!Pass99',
        })
        self.assertEqual(resp.status_code, 403)

    def test_signup_duplicate_email_returns_409(self):
        Identity.objects.create_user(email='dup@test.com', password='existing')
        resp = APIClient().post('/api/v1/orgs/signup-org/patient-signup/', {
            'email': 'dup@test.com',
            'password': 'Str0ng!Pass99',
        })
        self.assertEqual(resp.status_code, 409)

    def test_signup_weak_password_returns_400(self):
        resp = APIClient().post('/api/v1/orgs/signup-org/patient-signup/', {
            'email': 'weak@test.com',
            'password': '123',
        })
        self.assertEqual(resp.status_code, 400)

    def test_signup_missing_email_returns_400(self):
        resp = APIClient().post('/api/v1/orgs/signup-org/patient-signup/', {
            'email': '',
            'password': 'Str0ng!Pass99',
        })
        self.assertEqual(resp.status_code, 400)


class OrgPublicInfoTest(TestCase):
    """Test the public org info endpoint."""

    def test_returns_public_fields(self):
        Organization.objects.create(
            name='Public Org', slug='public-org', allows_patient_signup=True,
        )
        resp = APIClient().get('/api/v1/orgs/public-org/public/')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data['name'], 'Public Org')
        self.assertEqual(resp.data['slug'], 'public-org')
        self.assertTrue(resp.data['allows_patient_signup'])

    def test_inactive_org_returns_404(self):
        Organization.objects.create(
            name='Inactive', slug='inactive-org', is_active=False,
        )
        resp = APIClient().get('/api/v1/orgs/inactive-org/public/')
        self.assertEqual(resp.status_code, 404)

    def test_nonexistent_org_returns_404(self):
        resp = APIClient().get('/api/v1/orgs/nonexistent/public/')
        self.assertEqual(resp.status_code, 404)


class OrganizationSerializerTest(TestCase):
    """Test that allows_patient_signup is in the serializer output and writable."""

    def setUp(self):
        self.staff = _make_user('ser-staff@test.com', is_staff=True)
        self.client = APIClient()
        self.client.force_authenticate(user=self.staff)
        self.org = Organization.objects.create(
            name='Ser Org', slug='ser-org',
        )

    def test_allows_patient_signup_in_response(self):
        resp = self.client.get(f'/api/orgs/ser-org/')
        self.assertEqual(resp.status_code, 200)
        self.assertIn('allows_patient_signup', resp.data)
        self.assertFalse(resp.data['allows_patient_signup'])

    def test_allows_patient_signup_patchable(self):
        resp = self.client.patch('/api/orgs/ser-org/', {'allows_patient_signup': True}, format='json')
        self.assertEqual(resp.status_code, 200)
        self.org.refresh_from_db()
        self.assertTrue(self.org.allows_patient_signup)


# ---------------------------------------------------------------------------
# Vocabulary Release API tests
# ---------------------------------------------------------------------------

class VocabReleaseAPITest(_SmartBase):
    """Test /api/v1/vocab-releases/ endpoints and ETag on concept endpoints."""

    def tearDown(self):
        super().tearDown()
        # Reset module-level cache so stale release PKs don't leak into other tests
        from patient_portal.api.views import _vocab_version_cache
        _vocab_version_cache['release_pk'] = None
        _vocab_version_cache['map'] = None

    def _make_release(self, **kwargs):
        from omop_core.models import VocabularyRelease
        from django.utils import timezone
        defaults = {
            'build_timestamp': timezone.now(),
            'status': 'published',
            'published_at': timezone.now(),
            'scope': ['SNOMED', 'LOINC'],
            'vocab_versions': {'SNOMED': '20240701'},
            'row_counts': {'concept': 100},
            'checksums': {'concept': {'count': 100}},
        }
        defaults.update(kwargs)
        return VocabularyRelease.objects.create(**defaults)

    def test_list_returns_published_only(self):
        from omop_core.models import VocabularyRelease
        from django.utils import timezone
        self._make_release()
        VocabularyRelease.objects.create(
            build_timestamp=timezone.now(), status='staged',
        )
        VocabularyRelease.objects.create(
            build_timestamp=timezone.now(), status='retired',
            published_at=timezone.now(),
        )
        resp = self.read_client.get('/api/v1/vocab-releases/')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data['count'], 1)

    def test_detail_returns_checksums(self):
        release = self._make_release()
        resp = self.read_client.get(f'/api/v1/vocab-releases/{release.pk}/')
        self.assertEqual(resp.status_code, 200)
        self.assertIn('checksums', resp.data)
        self.assertEqual(resp.data['checksums']['concept']['count'], 100)

    def test_detail_404_for_nonexistent(self):
        resp = self.read_client.get('/api/v1/vocab-releases/99999/')
        self.assertEqual(resp.status_code, 404)

    def test_latest_returns_most_recent_published(self):
        from datetime import timedelta
        from django.utils import timezone
        now = timezone.now()
        self._make_release(published_at=now - timedelta(days=1))
        newer = self._make_release(published_at=now)
        resp = self.read_client.get('/api/v1/vocab-releases/latest/')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data['id'], newer.pk)

    def test_latest_returns_etag(self):
        self._make_release()
        resp = self.read_client.get('/api/v1/vocab-releases/latest/')
        self.assertEqual(resp.status_code, 200)
        self.assertIn('ETag', resp)
        self.assertTrue(resp['ETag'].startswith('"vr-'))

    def test_latest_304_on_matching_etag(self):
        self._make_release()
        resp1 = self.read_client.get('/api/v1/vocab-releases/latest/')
        etag = resp1['ETag']
        resp2 = self.read_client.get(
            '/api/v1/vocab-releases/latest/', HTTP_IF_NONE_MATCH=etag,
        )
        self.assertEqual(resp2.status_code, 304)

    def test_latest_404_when_empty(self):
        resp = self.read_client.get('/api/v1/vocab-releases/latest/')
        self.assertEqual(resp.status_code, 404)

    def test_concept_list_returns_etag_when_release_exists(self):
        self._make_release()
        resp = self.read_client.get('/api/v1/concepts/?vocabulary_id=SNOMED')
        self.assertEqual(resp.status_code, 200)
        self.assertIn('ETag', resp)

    def test_concept_list_304_on_matching_etag(self):
        self._make_release()
        resp1 = self.read_client.get('/api/v1/concepts/?vocabulary_id=SNOMED')
        etag = resp1['ETag']
        resp2 = self.read_client.get(
            '/api/v1/concepts/?vocabulary_id=SNOMED', HTTP_IF_NONE_MATCH=etag,
        )
        self.assertEqual(resp2.status_code, 304)

    def test_concept_search_returns_etag(self):
        self._make_release()
        resp = self.read_client.get('/api/v1/concepts/search/?q=test')
        self.assertEqual(resp.status_code, 200)
        self.assertIn('ETag', resp)
