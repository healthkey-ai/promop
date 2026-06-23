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
        # POST /api/fhir/sync/ is privileged since the ScopedTokenPermission role
        # change (a5e0ac6): only service-token / staff / superuser may write —
        # plain patient identities get 403. The connector calls it with a service
        # token; these tests exercise the request.user person-resolution path, so
        # authenticate as staff to retain write access.
        self.user = Identity.objects.create_user(
            email='fhirsync@test.com', password='test', is_staff=True)
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    def _sync(self):
        return self.client.post('/api/fhir/sync/', {'bundle': SAMPLE_BUNDLE}, format='json')

    def test_next_pk_batch_self_heals_after_legacy_explicit_pk_insert(self):
        """Legacy MAX(id)+1 writers (views.py, lot_inference) set explicit PKs
        without advancing the sequence; the sequence-based sync path must not
        then hand out an already-used id (the duplicate-key 500 we hit in prod)."""
        from omop_core.services.pk import next_pk_batch

        self.assertEqual(self._sync().status_code, 201)
        existing = ConditionOccurrence.objects.first()
        self.assertIsNotNone(existing)

        # Simulate the legacy path: explicit PK far ahead, sequence NOT advanced.
        stranded_id = existing.condition_occurrence_id + 500
        legacy = ConditionOccurrence(
            condition_occurrence_id=stranded_id,
            person=existing.person,
            condition_concept=existing.condition_concept,
            condition_start_date=existing.condition_start_date,
            condition_type_concept=existing.condition_type_concept,
            condition_source_value='legacy',
        )
        legacy._skip_patient_info_refresh = True
        legacy.save()

        # Sequence is now behind the table max → next_pk_batch must self-heal.
        ids = next_pk_batch(ConditionOccurrence, 'condition_occurrence_id', 3)
        self.assertTrue(all(i > stranded_id for i in ids), ids)
        self.assertEqual(len(set(ids)), 3)

    def test_rejects_non_bundle(self):
        resp = self.client.post('/api/fhir/sync/', {'bundle': {'resourceType': 'Patient'}}, format='json')
        self.assertEqual(resp.status_code, 400)

    def test_requires_auth(self):
        anon = APIClient()
        resp = anon.post('/api/fhir/sync/', {'bundle': SAMPLE_BUNDLE}, format='json')
        self.assertIn(resp.status_code, (401, 403))

    # ---- B0 connector: patient self-service ingest ---------------------- #

    def test_patient_sync_self_writes_with_patient_self_provenance(self):
        """A patient ingests their OWN data with a (non-staff) identity: the
        Person is resolved from that identity, any supplied person_id is ignored,
        and provenance is PATIENT_SELF (not EHR_SYNC)."""
        patient = Identity.objects.create(
            issuer='https://securetoken.google.com/healthtree-test', sub='patient-abc',
            email='patient@test.com')
        patient.set_unusable_password()
        patient.save()
        client = APIClient()
        client.force_authenticate(user=patient)

        bundle = {"resourceType": "Bundle", "type": "collection", "entry": [{"resource": {
            "resourceType": "Observation",
            "code": {"coding": [{"system": "http://loinc.org", "code": "8867-4",
                                 "display": "Heart rate"}]},
            "effectiveDateTime": "2026-05-01T08:00:00Z",
            "valueQuantity": {"value": 61, "unit": "/min"},
        }}]}
        # A malicious person_id must be ignored — a patient can only write self.
        resp = client.post('/api/fhir/patient-sync/',
                           {'bundle': bundle, 'person_id': 999999}, format='json')
        self.assertEqual(resp.status_code, 201, resp.content)
        pid = resp.json()['person_id']

        self.assertEqual(PatientUser.objects.get(identity=patient).person_id, pid)
        self.assertNotEqual(pid, 999999, "supplied person_id ignored; resolved from identity")
        self.assertEqual(Measurement.objects.filter(person_id=pid).count(), 1)
        self.assertTrue(ProvenanceRecord.objects.filter(
            source='PATIENT_SELF', target_patient_id=str(pid)).exists())
        self.assertFalse(ProvenanceRecord.objects.filter(
            source='EHR_SYNC', target_patient_id=str(pid)).exists())

    def test_patient_sync_requires_authentication(self):
        resp = APIClient().post('/api/fhir/patient-sync/', {'bundle': SAMPLE_BUNDLE}, format='json')
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

        # Current "records on file" totals returned for the connector to display.
        self.assertEqual(body['totals'],
                         {'measurements': 1, 'conditions': 1, 'medications': 1})

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

    def test_daily_rollup_upserts_by_person_concept_date(self):
        """An Observation flagged as a daily rollup replaces the prior
        (person, concept, date) row when its value changes, and collapses any
        stale stacked rows — instead of accumulating a row per changed value."""
        AGG_EXT = [{"url": "https://healthkey.ai/fhir/aggregation", "valueCode": "daily"}]

        def steps(value, ext):
            entry = {"resource": {
                "resourceType": "Observation",
                "subject": {"reference": "Patient/p1"},
                "code": {"coding": [{"system": "http://loinc.org", "code": "41950-7",
                                     "display": "Steps 24h"}]},
                "effectiveDateTime": "2026-04-01T00:00:00Z",
                "valueQuantity": {"value": value, "unit": "{steps}"},
            }}
            if ext:
                entry["resource"]["extension"] = AGG_EXT
            return {"resourceType": "Bundle", "type": "collection", "entry": [entry]}

        # Seed two stale stacked rows the old value-dedup behaviour would have left.
        self.assertEqual(self.client.post('/api/fhir/sync/', {'bundle': steps(5000, False)},
                                          format='json').status_code, 201)
        pid = self.client.post('/api/fhir/sync/', {'bundle': steps(8000, False)},
                               format='json').json()['person_id']
        self.assertEqual(Measurement.objects.filter(
            person_id=pid, measurement_source_value='Steps 24h').count(), 2)

        # Rollup sync with a new value → collapses to a single row at that value.
        self.client.post('/api/fhir/sync/', {'bundle': steps(9500, True)}, format='json')
        rows = Measurement.objects.filter(person_id=pid, measurement_source_value='Steps 24h')
        self.assertEqual(rows.count(), 1, "stacked rows collapsed to one")
        self.assertEqual(float(rows.first().value_as_number), 9500.0)

        # Re-sync identical rollup → idempotent (no new rows, nothing reported).
        again = self.client.post('/api/fhir/sync/', {'bundle': steps(9500, True)}, format='json')
        self.assertEqual(again.json()['measurement_ids'], [])
        self.assertEqual(rows.count(), 1)

    def test_per_reading_timestamps_coexist_and_resync_is_idempotent(self):
        """Multiple same-day Observations with distinct effectiveDateTime times
        (e.g. per-reading heart rate) land as separate rows with
        measurement_datetime set; re-syncing the identical bundle adds nothing."""
        def hr(time, value):
            return {"resource": {
                "resourceType": "Observation",
                "subject": {"reference": "Patient/p1"},
                "code": {"coding": [{"system": "http://loinc.org", "code": "8867-4",
                                     "display": "Heart rate"}]},
                "effectiveDateTime": time,
                "valueQuantity": {"value": value, "unit": "/min"},
            }}
        bundle = {"resourceType": "Bundle", "type": "collection", "entry": [
            hr("2026-03-01T08:00:00Z", 62),
            hr("2026-03-01T12:30:00Z", 88),
            hr("2026-03-01T20:15:00Z", 71),
        ]}

        first = self.client.post('/api/fhir/sync/', {'bundle': bundle}, format='json')
        self.assertEqual(first.status_code, 201, first.content)
        pid = first.json()['person_id']

        rows = Measurement.objects.filter(person_id=pid).order_by('measurement_datetime')
        self.assertEqual(rows.count(), 3, "distinct same-day times kept as separate rows")
        self.assertTrue(all(r.measurement_datetime is not None for r in rows),
                        "effectiveDateTime persisted to measurement_datetime")
        self.assertEqual([r.measurement_datetime.hour for r in rows], [8, 12, 20])
        self.assertTrue(all(r.measurement_date.isoformat() == '2026-03-01' for r in rows))

        # Re-sync the identical bundle → idempotent (dedup includes datetime).
        second = self.client.post('/api/fhir/sync/', {'bundle': bundle}, format='json')
        self.assertEqual(second.json()['measurement_ids'], [])
        self.assertEqual(Measurement.objects.filter(person_id=pid).count(), 3)

    def test_medication_request_maps_to_drug_exposure(self):
        # Epic R4 returns MedicationRequest (authoredOn), not MedicationStatement.
        bundle = {
            "resourceType": "Bundle", "type": "collection",
            "entry": [{"resource": {
                "resourceType": "MedicationRequest",
                "subject": {"reference": "Patient/p1"},
                "medicationCodeableConcept": {"text": "Lisinopril 10 MG"},
                "authoredOn": "2025-09-10",
            }}],
        }
        resp = self.client.post('/api/fhir/sync/', {'bundle': bundle}, format='json')
        self.assertEqual(resp.status_code, 201, resp.content)
        pid = resp.json()['person_id']
        self.assertEqual(len(resp.json()['drug_exposure_ids']), 1)
        de = DrugExposure.objects.get(person_id=pid)
        self.assertEqual(de.drug_source_value, 'Lisinopril 10 MG')
        self.assertEqual(de.drug_exposure_start_date.isoformat(), '2025-09-10')

    def test_batched_ingest_does_not_scale_queries_with_bundle_size(self):
        from datetime import date, timedelta
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        def make_bundle(n):
            entries = [{"resource": {
                "resourceType": "Patient", "id": "p",
                "name": [{"family": "Quant"}], "birthDate": "1980-01-01", "gender": "male",
            }}]
            for i in range(n):
                entries.append({"resource": {
                    "resourceType": "Observation",
                    "code": {"coding": [{"system": "http://loinc.org", "code": "718-7"}],
                             "text": "Hemoglobin"},
                    "effectiveDateTime": (date(2026, 1, 1) + timedelta(days=i)).isoformat(),
                    "valueQuantity": {"value": 13.0 + i, "unit": "g/dL"},
                }})
            return {"resourceType": "Bundle", "type": "collection", "entry": entries}

        def run(email, n):
            user = Identity.objects.create_user(email=email, password="test", is_staff=True)
            client = APIClient()
            client.force_authenticate(user=user)
            with CaptureQueriesContext(connection) as ctx:
                resp = client.post('/api/fhir/sync/', {'bundle': make_bundle(n)}, format='json')
            self.assertEqual(resp.status_code, 201, resp.content)
            self.assertEqual(len(resp.json()['measurement_ids']), n)
            return len(ctx.captured_queries)

        run("warmup@example.com", 1)        # create fallback concepts/content types once
        q_small = run("small@example.com", 5)
        q_large = run("large@example.com", 40)
        # 35 extra observations must add only a tiny, bounded number of queries —
        # per-row ingest would add ~140. This is the real "doesn't scale" proof.
        self.assertLess(q_large - q_small, 10, (q_small, q_large))
