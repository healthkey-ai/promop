"""Tests for POST /api/fhir/sync/ — identity-resolved FHIR ingest."""
from django.db import connection
from django.test import TestCase
from rest_framework.test import APIClient

from patient_portal.models import Identity, PatientUser
from omop_core.models import (
    ConditionOccurrence, DrugExposure, Measurement, ProvenanceRecord,
)

# OMOP tables use manually-assigned integer PKs fed by Postgres sequences that
# migration 0074 creates. The default test config runs --no-migrations, so
# recreate them here (idempotent; no-op when migrations did run).
_PK_SEQUENCES = [
    ('person', 'person_id'),
    ('measurement', 'measurement_id'),
    ('condition_occurrence', 'condition_occurrence_id'),
    ('drug_exposure', 'drug_exposure_id'),
    ('concept', 'concept_id'),
    ('visit_occurrence', 'visit_occurrence_id'),
    ('care_site', 'care_site_id'),
    ('observation', 'observation_id'),
    ('procedure_occurrence', 'procedure_occurrence_id'),
]


def _ensure_pk_sequences():
    with connection.cursor() as cur:
        for table, pk in _PK_SEQUENCES:
            seq = f'{table}_{pk}_seq'
            cur.execute(f'CREATE SEQUENCE IF NOT EXISTS "{seq}"')
            cur.execute(
                f'SELECT setval(%s, COALESCE(MAX("{pk}"), 0) + 1, false) FROM "{table}"',
                [seq],
            )

SAMPLE_BUNDLE = {
    "resourceType": "Bundle",
    "type": "collection",
    "entry": [
        {"resource": {
            "resourceType": "Patient", "id": "p1",
            "name": [{"family": "Smith", "given": ["Jane"]}],
            "birthDate": "1970-04-01", "gender": "female",
        }},
        {"resource": {
            "resourceType": "Observation",
            "subject": {"reference": "Patient/p1"},
            "code": {"coding": [{"system": "http://loinc.org", "code": "718-7",
                                 "display": "Hemoglobin"}]},
            "effectiveDateTime": "2026-02-01",
            "valueQuantity": {"value": 13.2, "unit": "g/dL"},
        }},
        {"resource": {
            "resourceType": "Condition",
            "subject": {"reference": "Patient/p1"},
            "code": {"coding": [{"system": "http://snomed.info/sct", "code": "254837009"}],
                     "text": "Malignant neoplasm of breast"},
            "onsetDateTime": "2025-11-15",
        }},
        {"resource": {
            "resourceType": "MedicationStatement",
            "subject": {"reference": "Patient/p1"},
            "medicationCodeableConcept": {"text": "AC-T"},
            "effectivePeriod": {"start": "2025-12-01"},
        }},
    ],
}


class FhirSyncTests(TestCase):
    def setUp(self):
        _ensure_pk_sequences()
        self.user = Identity.objects.create_user(email='fhirsync@test.com', password='test')
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    def _sync(self):
        return self.client.post('/api/fhir/sync/', {'bundle': SAMPLE_BUNDLE}, format='json')

    def test_rejects_non_bundle(self):
        resp = self.client.post('/api/fhir/sync/', {'bundle': {'resourceType': 'Patient'}}, format='json')
        self.assertEqual(resp.status_code, 400)

    def test_requires_auth(self):
        anon = APIClient()
        resp = anon.post('/api/fhir/sync/', {'bundle': SAMPLE_BUNDLE}, format='json')
        self.assertIn(resp.status_code, (401, 403))

    def test_ingests_bundle_bound_to_resolved_person(self):
        resp = self._sync()
        self.assertEqual(resp.status_code, 201, resp.content)
        body = resp.json()

        # Person resolved from the authenticated identity (not demographic upsert).
        person_id = body['person_id']
        self.assertEqual(
            PatientUser.objects.get(identity=self.user).person_id, person_id,
        )

        self.assertEqual(len(body['measurement_ids']), 1)
        self.assertEqual(len(body['condition_ids']), 1)
        self.assertEqual(len(body['drug_exposure_ids']), 1)
        self.assertTrue(body['demographics_updated'])

        self.assertEqual(Measurement.objects.filter(person_id=person_id).count(), 1)
        self.assertEqual(ConditionOccurrence.objects.filter(person_id=person_id).count(), 1)
        self.assertEqual(DrugExposure.objects.filter(person_id=person_id).count(), 1)

        # Every clinical row gets EHR_SYNC provenance.
        self.assertEqual(ProvenanceRecord.objects.filter(source='EHR_SYNC').count(), 3)

        # Demographics filled onto the resolved Person.
        from omop_core.models import Person
        person = Person.objects.get(person_id=person_id)
        self.assertEqual(person.family_name, 'Smith')
        self.assertEqual(person.year_of_birth, 1970)

    def test_resync_is_idempotent(self):
        first = self._sync().json()
        second = self._sync().json()
        # Same person, no new rows on re-sync.
        self.assertEqual(first['person_id'], second['person_id'])
        self.assertEqual(second['measurement_ids'], [])
        self.assertEqual(second['condition_ids'], [])
        self.assertEqual(second['drug_exposure_ids'], [])
        self.assertEqual(Measurement.objects.filter(person_id=first['person_id']).count(), 1)
