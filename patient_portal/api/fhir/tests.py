"""Tests for POST /api/fhir/sync/ — identity-resolved FHIR ingest."""
from copy import deepcopy
from decimal import Decimal

from django.db import connection
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from patient_portal.models import Identity, PatientUser
from omop_core.models import (
    ConditionOccurrence, DrugExposure, Measurement, Person, ProvenanceRecord,
    PatientDocument, PatientRecord, Organization, GroupAccess,
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
        legacy._skip_patient_record_refresh = True
        legacy.save()

        # Sequence is now behind the table max → next_pk_batch must self-heal.
        ids = next_pk_batch(ConditionOccurrence, 'condition_occurrence_id', 3)
        self.assertTrue(all(i > stranded_id for i in ids), ids)
        self.assertEqual(len(set(ids)), 3)

    def test_rejects_non_bundle(self):
        resp = self.client.post('/api/fhir/sync/', {'bundle': {'resourceType': 'Patient'}}, format='json')
        self.assertEqual(resp.status_code, 400)

    def test_rejects_non_numeric_observation_quantity(self):
        bundle = deepcopy(SAMPLE_BUNDLE)
        bundle['entry'][1]['resource']['valueQuantity']['value'] = '13.2'

        resp = self.client.post('/api/fhir/sync/', {'bundle': bundle}, format='json')

        self.assertEqual(resp.status_code, 400)
        self.assertIn('finite number', str(resp.data))

    def test_requires_auth(self):
        anon = APIClient()
        resp = anon.post('/api/fhir/sync/', {'bundle': SAMPLE_BUNDLE}, format='json')
        self.assertIn(resp.status_code, (401, 403))

    def test_response_reports_unsupported_resources(self):
        bundle = deepcopy(SAMPLE_BUNDLE)
        bundle['entry'].append({'resource': {
            'resourceType': 'Encounter',
            'id': 'enc-1',
            'status': 'finished',
        }})

        resp = self.client.post('/api/fhir/sync/', {'bundle': bundle}, format='json')

        self.assertEqual(resp.status_code, 201, resp.content)
        self.assertIn(
            {'resourceType': 'Encounter', 'reason': 'unsupported_resource_type', 'count': 1},
            resp.json()['skipped'],
        )
        self.assertEqual(Measurement.objects.count(), 1)

    def test_response_reports_supported_resources_skipped_for_missing_dates(self):
        bundle = deepcopy(SAMPLE_BUNDLE)
        bundle['entry'].append({'resource': {
            'resourceType': 'Observation',
            'code': {'coding': [{'system': 'http://loinc.org', 'code': '8867-4'}]},
            'valueQuantity': {'value': 61, 'unit': '/min'},
        }})

        resp = self.client.post('/api/fhir/sync/', {'bundle': bundle}, format='json')

        self.assertEqual(resp.status_code, 201, resp.content)
        self.assertIn(
            {'resourceType': 'Observation', 'reason': 'missing_effective_date', 'count': 1},
            resp.json()['skipped'],
        )
        self.assertEqual(len(resp.json()['measurement_ids']), 1)

    def test_observation_reference_range_and_interpretation_are_stored(self):
        from datetime import date
        from omop_core.models import Concept, ConceptClass, Domain, Vocabulary

        vocab, _ = Vocabulary.objects.get_or_create(
            vocabulary_id='SNOMED',
            defaults={'vocabulary_name': 'SNOMED', 'vocabulary_concept_id': 0})
        domain, _ = Domain.objects.get_or_create(
            domain_id='Meas Value',
            defaults={'domain_name': 'Meas Value', 'domain_concept_id': 0})
        concept_class, _ = ConceptClass.objects.get_or_create(
            concept_class_id='Qualifier Value',
            defaults={'concept_class_name': 'Qualifier Value',
                      'concept_class_concept_id': 0})
        positive = Concept.objects.create(
            concept_id=99006201,
            concept_name='Positive',
            domain=domain,
            vocabulary=vocab,
            concept_class=concept_class,
            standard_concept='S',
            concept_code='10828004',
            valid_start_date=date(1970, 1, 1),
            valid_end_date=date(2099, 12, 31),
        )
        bundle = {'resourceType': 'Bundle', 'type': 'collection', 'entry': [
            {'resource': {
                'resourceType': 'Observation',
                'code': {'coding': [{'system': 'http://loinc.org', 'code': '718-7',
                                      'display': 'Hemoglobin'}]},
                'effectiveDateTime': '2026-07-01T09:00:00Z',
                'valueQuantity': {'value': 13.2, 'unit': 'g/dL'},
                'referenceRange': [{'low': {'value': 12.0}, 'high': {'value': 16.0}}],
                'interpretation': [{'coding': [{
                    'system': 'http://snomed.info/sct',
                    'code': '10828004',
                    'display': 'Positive',
                }]}],
            }},
        ]}

        resp = self.client.post('/api/fhir/sync/', {'bundle': bundle}, format='json')

        self.assertEqual(resp.status_code, 201, resp.content)
        row = Measurement.objects.get(person_id=resp.json()['person_id'])
        self.assertEqual(row.range_low, Decimal('12.0'))
        self.assertEqual(row.range_high, Decimal('16.0'))
        self.assertEqual(row.value_as_concept_id, positive.concept_id)
        self.assertEqual(row.value_source_value, 'Positive')

    def test_unresolved_observation_interpretation_is_preserved_as_source(self):
        bundle = {'resourceType': 'Bundle', 'type': 'collection', 'entry': [
            {'resource': {
                'resourceType': 'Observation',
                'code': {'coding': [{'system': 'http://loinc.org', 'code': '718-7',
                                      'display': 'Hemoglobin'}]},
                'effectiveDateTime': '2026-07-01T09:00:00Z',
                'valueQuantity': {'value': 17.2, 'unit': 'g/dL'},
                'interpretation': [{'coding': [{
                    'system': 'http://terminology.hl7.org/CodeSystem/v3-ObservationInterpretation',
                    'code': 'H',
                    'display': 'High',
                }]}],
            }},
        ]}

        resp = self.client.post('/api/fhir/sync/', {'bundle': bundle}, format='json')

        self.assertEqual(resp.status_code, 201, resp.content)
        row = Measurement.objects.get(person_id=resp.json()['person_id'])
        self.assertIsNone(row.value_as_concept_id)
        self.assertEqual(row.value_source_value, 'High')

    def test_service_token_syncs_explicit_person_without_actor_identity(self):
        """Trusted service callers may sync an explicit person without actor fields."""
        from omop_core.services.pk import next_pk_batch

        person = Person.objects.create(
            person_id=next_pk_batch(Person, 'person_id', 1)[0],
        )
        service_user = Identity.objects.create(issuer='urn:service', sub='fhir-sync')
        service_user.set_unusable_password()
        service_user.save(update_fields=['password'])
        client = APIClient()
        client.force_authenticate(user=service_user, token='service-token')

        resp = client.post('/api/fhir/sync/', {
            'person_id': person.person_id,
            'bundle': SAMPLE_BUNDLE,
        }, format='json')

        self.assertEqual(resp.status_code, 201, resp.content)
        self.assertEqual(resp.json()['person_id'], person.person_id)
        self.assertEqual(Measurement.objects.filter(person=person).count(), 1)

    def test_service_token_explicit_read_only_actor_rejected(self):
        from omop_core.services.pk import next_pk_batch

        org = Organization.objects.create(name='FHIR Analyst Org', slug='fhir-analyst-org')
        person = Person.objects.create(
            person_id=next_pk_batch(Person, 'person_id', 1)[0],
        )
        PatientRecord.objects.create(person=person, organization=org)
        analyst = Identity.objects.create_user(email='fhir-analyst@test.com', password='test')
        GroupAccess.objects.create(identity=analyst, org=org, role='analyst')
        service_user = Identity.objects.create(issuer='urn:service', sub='fhir-sync-analyst')
        service_user.set_unusable_password()
        service_user.save(update_fields=['password'])
        client = APIClient()
        client.force_authenticate(user=service_user, token='service-token')

        resp = client.post('/api/fhir/sync/', {
            'person_id': person.person_id,
            'actor_iss': analyst.issuer,
            'actor_sub': analyst.sub,
            'bundle': SAMPLE_BUNDLE,
        }, format='json')

        self.assertEqual(resp.status_code, 403, resp.content)
        self.assertIn('write access', resp.json()['detail'])
        self.assertEqual(Measurement.objects.filter(person=person).count(), 0)

    def test_userless_oauth_org_token_ignores_body_actor_for_provenance(self):
        from datetime import timedelta
        from oauth2_provider.models import AccessToken, Application
        from omop_core.models import ApplicationOrganization
        from omop_core.services.pk import next_pk_batch

        org = Organization.objects.create(name='FHIR OAuth Org', slug='fhir-oauth-org')
        owner = Identity.objects.create_user(email='fhir-oauth-owner@test.com', password='test')
        app = Application.objects.create(
            name='FHIR OAuth App',
            user=owner,
            client_type=Application.CLIENT_CONFIDENTIAL,
            authorization_grant_type=Application.GRANT_CLIENT_CREDENTIALS,
        )
        ApplicationOrganization.objects.create(application=app, organization=org)
        token = AccessToken.objects.create(
            user=None,
            application=app,
            token='fhir-userless-write-token',
            expires=timezone.now() + timedelta(hours=1),
            scope='patient/*.write',
        )
        person = Person.objects.create(
            person_id=next_pk_batch(Person, 'person_id', 1)[0],
        )
        PatientRecord.objects.create(person=person, organization=org)
        spoofed_actor = Identity.objects.create_user(email='spoofed-fhir@test.com', password='test')
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f'Bearer {token.token}')

        resp = client.post('/api/fhir/sync/', {
            'person_id': person.person_id,
            'actor_iss': spoofed_actor.issuer,
            'actor_sub': spoofed_actor.sub,
            'bundle': SAMPLE_BUNDLE,
        }, format='json')

        self.assertEqual(resp.status_code, 201, resp.content)
        measurement = Measurement.objects.get(person=person)
        provenance = ProvenanceRecord.objects.get(
            content_type__model='measurement',
            object_id=measurement.measurement_id,
        )
        self.assertEqual(provenance.source_user_id, '')
        self.assertNotEqual(
            provenance.source_user_id,
            f'{spoofed_actor.issuer}|{spoofed_actor.sub}',
        )

    # ---- B0 connector: patient self-service ingest ---------------------- #

    def test_patient_sync_self_writes_with_patient_self_provenance(self):
        """A patient ingests their OWN data with a (non-staff) identity: the
        Person is resolved from that identity, any supplied person_id is ignored,
        and provenance is PATIENT_SELF (not EHR_SYNC)."""
        patient = Identity.objects.create(
            issuer='https://securetoken.google.com/promop-test', sub='patient-abc',
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

    def test_patient_delete_removes_only_targeted_own_measurements(self):
        """B4: a patient deletes their own measurement by source_value + datetime
        + value; other rows and provenance are untouched."""
        patient = Identity.objects.create(
            issuer='https://securetoken.google.com/promop-test', sub='del-patient',
            email='del@test.com')
        patient.set_unusable_password()
        patient.save()
        client = APIClient()
        client.force_authenticate(user=patient)

        def hr(time, value):
            return {"resource": {
                "resourceType": "Observation",
                "code": {"coding": [{"system": "http://loinc.org", "code": "8867-4",
                                     "display": "Heart rate"}]},
                "effectiveDateTime": time, "valueQuantity": {"value": value, "unit": "/min"}}}
        bundle = {"resourceType": "Bundle", "type": "collection",
                  "entry": [hr("2026-05-01T08:00:00Z", 61), hr("2026-05-01T12:00:00Z", 88)]}
        pid = client.post('/api/fhir/patient-sync/', {'bundle': bundle}, format='json').json()['person_id']
        self.assertEqual(Measurement.objects.filter(person_id=pid).count(), 2)

        resp = client.post('/api/fhir/patient-delete/', {'targets': [
            {"source_value": "Heart rate", "date": "2026-05-01",
             "datetime": "2026-05-01T08:00:00Z", "value": 61},
        ]}, format='json')
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(resp.json()['deleted'], 1)

        remaining = Measurement.objects.filter(person_id=pid)
        self.assertEqual(remaining.count(), 1)
        self.assertEqual(float(remaining.first().value_as_number), 88.0)
        # provenance for the deleted row is gone; the surviving one remains.
        self.assertEqual(ProvenanceRecord.objects.filter(
            source='PATIENT_SELF', target_patient_id=str(pid)).count(), 1)

    def test_patient_delete_requires_authentication(self):
        resp = APIClient().post('/api/fhir/patient-delete/', {'targets': []}, format='json')
        self.assertIn(resp.status_code, (401, 403))

    def test_patient_consent_records_reads_and_updates(self):
        """B6: per-category data-sharing consent persists in PatientConsent."""
        patient = Identity.objects.create(
            issuer='https://securetoken.google.com/promop-test', sub='consent-patient',
            email='consent@test.com')
        patient.set_unusable_password()
        patient.save()
        client = APIClient()
        client.force_authenticate(user=patient)

        resp = client.post('/api/fhir/patient-consent/',
                           {'granted': True, 'categories': ['vitals', 'activity']}, format='json')
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertTrue(resp.json()['granted'])
        self.assertEqual(resp.json()['categories'], ['vitals', 'activity'])

        got = client.get('/api/fhir/patient-consent/')
        self.assertTrue(got.json()['granted'])
        self.assertEqual(got.json()['categories'], ['vitals', 'activity'])

        # Revoke updates the same record (no duplicate).
        client.post('/api/fhir/patient-consent/', {'granted': False, 'categories': []}, format='json')
        self.assertFalse(client.get('/api/fhir/patient-consent/').json()['granted'])

        from patient_portal.models import PatientConsent, PatientUser
        pu = PatientUser.objects.get(identity=patient)
        self.assertEqual(PatientConsent.objects.filter(
            patient_user=pu, consent_type='data_sharing').count(), 1)

    def test_patient_consent_requires_authentication(self):
        self.assertIn(APIClient().get('/api/fhir/patient-consent/').status_code, (401, 403))

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
                         {'measurements': 1, 'conditions': 1, 'medications': 1,
                          'procedures': 0, 'observations': 0, 'documents': 0})

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

    def test_document_reference_ingests_patient_document(self):
        bundle = {"resourceType": "Bundle", "type": "collection", "entry": [
            {"resource": {
                "resourceType": "DocumentReference",
                "id": "doc-1",
                "status": "current",
                "type": {"text": "Radiology report"},
                "description": "CT chest report",
                "date": "2026-07-15T10:30:00Z",
                "content": [{
                    "attachment": {
                        "contentType": "application/pdf",
                        "url": "https://ehr.example.test/reports/ct-chest.pdf",
                        "title": "ct-chest.pdf",
                    },
                }],
            }},
        ]}

        first = self.client.post('/api/fhir/sync/', {'bundle': bundle}, format='json')
        second = self.client.post('/api/fhir/sync/', {'bundle': bundle}, format='json')

        self.assertEqual(first.status_code, 201, first.content)
        self.assertEqual(second.status_code, 201, second.content)
        self.assertEqual(first.json()['document_ids'], second.json()['document_ids'])
        self.assertEqual(first.json()['skipped'], [])
        self.assertEqual(PatientDocument.objects.count(), 1)
        doc = PatientDocument.objects.get()
        self.assertEqual(doc.person_id, first.json()['person_id'])
        self.assertEqual(doc.doc_type, 'IMAGING')
        self.assertEqual(doc.title, 'ct-chest.pdf')
        self.assertEqual(doc.file_url, 'https://ehr.example.test/reports/ct-chest.pdf')
        self.assertEqual(doc.file_name, 'ct-chest.pdf')
        self.assertEqual(doc.status, PatientDocument.STATUS_ACTIVE)
        self.assertEqual(doc.effective_date.isoformat(), '2026-07-15')
        self.assertEqual(first.json()['totals']['documents'], 1)
        self.assertEqual(ProvenanceRecord.objects.filter(
            content_type__model='patientdocument',
            object_id=doc.id,
            source='EHR_SYNC',
        ).count(), 1)

    def test_document_reference_without_content_is_reported_skipped(self):
        bundle = {"resourceType": "Bundle", "type": "collection", "entry": [
            {"resource": {"resourceType": "DocumentReference", "status": "current"}},
        ]}

        resp = self.client.post('/api/fhir/sync/', {'bundle': bundle}, format='json')

        self.assertEqual(resp.status_code, 201, resp.content)
        self.assertEqual(resp.json()['document_ids'], [])
        self.assertIn(
            {'resourceType': 'DocumentReference', 'reason': 'missing_content', 'count': 1},
            resp.json()['skipped'],
        )
        self.assertEqual(PatientDocument.objects.count(), 0)

    def test_mapped_observation_dedup_uses_resolved_concept_not_display(self):
        from datetime import date
        from omop_core.models import Concept, ConceptClass, Domain, Vocabulary

        vocab, _ = Vocabulary.objects.get_or_create(
            vocabulary_id='LOINC',
            defaults={'vocabulary_name': 'LOINC', 'vocabulary_concept_id': 0})
        domain, _ = Domain.objects.get_or_create(
            domain_id='Measurement',
            defaults={'domain_name': 'Measurement', 'domain_concept_id': 21})
        concept_class, _ = ConceptClass.objects.get_or_create(
            concept_class_id='Lab Test',
            defaults={'concept_class_name': 'Lab Test', 'concept_class_concept_id': 0})
        Concept.objects.create(
            concept_id=99007187,
            concept_name='Albumin lab test',
            domain=domain,
            vocabulary=vocab,
            concept_class=concept_class,
            standard_concept='S',
            concept_code='9999-9',
            valid_start_date=date(1970, 1, 1),
            valid_end_date=date(2099, 12, 31),
        )

        def albumin(display):
            return {'resource': {
                'resourceType': 'Observation',
                'code': {'coding': [{'system': 'http://loinc.org', 'code': '9999-9',
                                      'display': display}]},
                'effectiveDateTime': '2026-06-01T08:00:00Z',
                'valueQuantity': {'value': 4.2, 'unit': 'g/dL'},
            }}

        bundle = {'resourceType': 'Bundle', 'type': 'collection',
                  'entry': [albumin('Serum albumin'), albumin('Albumin [Mass/volume]')]}
        first = self.client.post('/api/fhir/sync/', {'bundle': bundle}, format='json')
        self.assertEqual(first.status_code, 201, first.content)
        pid = first.json()['person_id']

        rows = Measurement.objects.filter(person_id=pid)
        self.assertEqual(rows.count(), 1)
        self.assertEqual(rows.get().measurement_concept_id, 99007187)
        self.assertIn(rows.get().measurement_source_value,
                      {'Serum albumin', 'Albumin [Mass/volume]'})

        second = self.client.post(
            '/api/fhir/sync/',
            {'bundle': {'resourceType': 'Bundle', 'type': 'collection',
                        'entry': [albumin('Albumin lab result')]}},
            format='json',
        )
        self.assertEqual(second.json()['measurement_ids'], [])
        self.assertEqual(Measurement.objects.filter(person_id=pid).count(), 1)

    def test_unmapped_observation_dedup_keeps_source_text(self):
        def local_metric(display):
            return {'resource': {
                'resourceType': 'Observation',
                'code': {'coding': [{'system': 'http://loinc.org', 'code': 'hk-local',
                                      'display': display}]},
                'effectiveDateTime': '2026-06-01T08:00:00Z',
                'valueQuantity': {'value': 4.2, 'unit': 'score'},
            }}

        bundle = {'resourceType': 'Bundle', 'type': 'collection',
                  'entry': [local_metric('Local score A'), local_metric('Local score B')]}
        resp = self.client.post('/api/fhir/sync/', {'bundle': bundle}, format='json')
        self.assertEqual(resp.status_code, 201, resp.content)

        rows = Measurement.objects.filter(person_id=resp.json()['person_id'])
        self.assertEqual(rows.count(), 2)
        self.assertEqual(set(rows.values_list('measurement_source_value', flat=True)),
                         {'Local score A', 'Local score B'})

    def test_clinical_concept_upgrade_updates_in_place(self):
        """Re-syncing a clinical record after its code becomes resolvable (e.g. a
        vocabulary load) updates the existing row's concept in place keyed on
        (source_value, date) — instead of leaving the old 'No matching concept'
        row stranded next to a new resolved duplicate."""
        from datetime import date
        from omop_core.models import Vocabulary, Domain, ConceptClass, Concept
        bundle = {"resourceType": "Bundle", "type": "collection", "entry": [
            {"resource": {"resourceType": "Condition",
                "subject": {"reference": "Patient/p1"},
                "code": {"coding": [{"system": "http://snomed.info/sct", "code": "38341003"}],
                         "text": "Hypertension"},
                "onsetDateTime": "2024-11-01"}},
        ]}

        # First sync: SNOMED 38341003 isn't loaded → "No matching concept" (0).
        pid = self.client.post('/api/fhir/sync/', {'bundle': bundle}, format='json').json()['person_id']
        row = ConditionOccurrence.objects.get(person_id=pid)
        self.assertEqual(row.condition_concept_id, 0)

        # Load the concept (simulating the Athena vocabulary load).
        vocab, _ = Vocabulary.objects.get_or_create(
            vocabulary_id='SNOMED',
            defaults={'vocabulary_name': 'SNOMED', 'vocabulary_concept_id': 0})
        domain, _ = Domain.objects.get_or_create(
            domain_id='Condition',
            defaults={'domain_name': 'Condition', 'domain_concept_id': 19})
        cc, _ = ConceptClass.objects.get_or_create(
            concept_class_id='Clinical Finding',
            defaults={'concept_class_name': 'Clinical Finding', 'concept_class_concept_id': 0})
        Concept.objects.create(
            concept_id=316866, concept_name='Hypertensive disorder', domain=domain,
            vocabulary=vocab, concept_class=cc, standard_concept='S', concept_code='38341003',
            valid_start_date=date(1970, 1, 1), valid_end_date=date(2099, 12, 31))

        # Re-sync the identical record → in-place concept upgrade, no duplicate.
        self.client.post('/api/fhir/sync/', {'bundle': bundle}, format='json')
        rows = ConditionOccurrence.objects.filter(person_id=pid)
        self.assertEqual(rows.count(), 1, "no duplicate row after concept upgrade")
        self.assertEqual(rows.first().condition_concept_id, 316866, "concept updated in place")

    def test_ingests_extended_clinical_record_types(self):
        """Procedure → procedure_occurrence, Immunization → drug_exposure,
        AllergyIntolerance + DiagnosticReport → observation (B3)."""
        bundle = {"resourceType": "Bundle", "type": "collection", "entry": [
            {"resource": {"resourceType": "Procedure",
                "code": {"coding": [{"system": "http://snomed.info/sct", "code": "80146002",
                                     "display": "Appendectomy"}]},
                "performedDateTime": "2025-03-10"}},
            {"resource": {"resourceType": "Immunization",
                "vaccineCode": {"coding": [{"system": "http://hl7.org/fhir/sid/cvx", "code": "208",
                                            "display": "COVID-19"}]},
                "occurrenceDateTime": "2025-09-01"}},
            {"resource": {"resourceType": "AllergyIntolerance",
                "code": {"coding": [{"system": "http://snomed.info/sct", "code": "227493005",
                                     "display": "Cashew nuts"}]},
                "recordedDate": "2024-01-15", "criticality": "high"}},
            {"resource": {"resourceType": "DiagnosticReport",
                "code": {"coding": [{"system": "http://loinc.org", "code": "58410-2",
                                     "display": "CBC panel"}]},
                "effectiveDateTime": "2025-06-01", "conclusion": "Within normal limits"}},
        ]}
        resp = self.client.post('/api/fhir/sync/', {'bundle': bundle}, format='json')
        self.assertEqual(resp.status_code, 201, resp.content)
        body = resp.json()
        pid = body['person_id']

        self.assertEqual(len(body['procedure_ids']), 1)
        self.assertEqual(len(body['immunization_ids']), 1)
        self.assertEqual(len(body['observation_ids']), 2)  # allergy + diagnostic report

        from omop_core.models import ProcedureOccurrence, Observation
        self.assertEqual(ProcedureOccurrence.objects.filter(person_id=pid).count(), 1)
        self.assertEqual(DrugExposure.objects.filter(person_id=pid).count(), 1)  # immunization
        self.assertEqual(Observation.objects.filter(person_id=pid).count(), 2)
        self.assertEqual(body['totals']['procedures'], 1)
        self.assertEqual(body['totals']['observations'], 2)

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

    def test_unmapped_daily_rollups_coexist_by_source_value(self):
        """Two daily rollups with no resolvable concept (both concept_id 0) but
        distinct source_values must NOT collide on the (concept, date) key — for
        cid 0 the key also includes source_value, so e.g. basal-energy and
        mindful-minutes coexist as separate rows instead of overwriting per day."""
        AGG_EXT = [{"url": "https://healthkey.ai/fhir/aggregation", "valueCode": "daily"}]

        def rollup(code, display, unit, value):
            return {"resource": {
                "resourceType": "Observation",
                "subject": {"reference": "Patient/p1"},
                "code": {"coding": [{"system": "http://loinc.org", "code": code,
                                     "display": display}]},
                "effectiveDateTime": "2026-05-01T00:00:00Z",
                "valueQuantity": {"value": value, "unit": unit},
                "extension": AGG_EXT,
            }}

        # Unresolvable codes → concept_id 0 regardless of loaded vocabulary.
        bundle = {"resourceType": "Bundle", "type": "collection", "entry": [
            rollup("hk-basal", "Basal energy burned", "kcal", 1500),
            rollup("hk-mind", "Mindful minutes", "min", 10),
        ]}
        resp = self.client.post('/api/fhir/sync/', {'bundle': bundle}, format='json')
        self.assertEqual(resp.status_code, 201, resp.content)
        pid = resp.json()['person_id']

        rows = Measurement.objects.filter(person_id=pid, measurement_concept_id=0)
        self.assertEqual(rows.count(), 2, "distinct unmapped rollups coexist (not collapsed)")
        self.assertEqual(
            set(rows.values_list('measurement_source_value', flat=True)),
            {"Basal energy burned", "Mindful minutes"})

        # Re-sync identical bundle → idempotent per source_value.
        again = self.client.post('/api/fhir/sync/', {'bundle': bundle}, format='json')
        self.assertEqual(again.json()['measurement_ids'], [])
        self.assertEqual(rows.count(), 2)

        # Changing one unmapped metric updates only its row, still two rows.
        bump = {"resourceType": "Bundle", "type": "collection", "entry": [
            rollup("hk-basal", "Basal energy burned", "kcal", 1800)]}
        self.client.post('/api/fhir/sync/', {'bundle': bump}, format='json')
        self.assertEqual(rows.count(), 2)
        self.assertEqual(
            float(rows.get(measurement_source_value="Basal energy burned").value_as_number), 1800.0)
        self.assertEqual(
            float(rows.get(measurement_source_value="Mindful minutes").value_as_number), 10.0)

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
