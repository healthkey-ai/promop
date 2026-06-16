"""
Tests for the lab results API (ResultsSummary + Sync endpoints).
"""
from datetime import date
from decimal import Decimal

from patient_portal.models import Identity, PatientUser
from django.test import TestCase
from rest_framework import status
from rest_framework.test import APIClient

from omop_core.models import (
    CareSite, Concept, ConceptClass, Domain, LoincClass, LoincCodeClass,
    Measurement, MeasurementOwnership, Person, PatientInfo, ProvenanceRecord,
    Vocabulary, VisitOccurrence,
)


def _setup_vocab():
    """Create minimum vocabulary fixtures for lab results tests."""
    Vocabulary.objects.get_or_create(
        vocabulary_id='LOINC',
        defaults={'vocabulary_name': 'LOINC', 'vocabulary_concept_id': 0},
    )
    Vocabulary.objects.get_or_create(
        vocabulary_id='UCUM',
        defaults={'vocabulary_name': 'UCUM', 'vocabulary_concept_id': 0},
    )
    Vocabulary.objects.get_or_create(
        vocabulary_id='HK-Labs',
        defaults={'vocabulary_name': 'HealthKey Labs', 'vocabulary_concept_id': 0},
    )
    Domain.objects.get_or_create(
        domain_id='Measurement',
        defaults={'domain_name': 'Measurement', 'domain_concept_id': 21},
    )
    Domain.objects.get_or_create(
        domain_id='Visit',
        defaults={'domain_name': 'Visit', 'domain_concept_id': 8},
    )
    Domain.objects.get_or_create(
        domain_id='Type Concept',
        defaults={'domain_name': 'Type Concept', 'domain_concept_id': 58},
    )
    ConceptClass.objects.get_or_create(
        concept_class_id='Lab Test',
        defaults={'concept_class_name': 'Lab Test', 'concept_class_concept_id': 0},
    )
    ConceptClass.objects.get_or_create(
        concept_class_id='Visit',
        defaults={'concept_class_name': 'Visit', 'concept_class_concept_id': 0},
    )
    ConceptClass.objects.get_or_create(
        concept_class_id='Clinical Finding',
        defaults={'concept_class_name': 'Clinical Finding', 'concept_class_concept_id': 0},
    )

    today = date.today()
    far_future = date(2099, 12, 31)

    def _c(cid, name, domain_id, vocab_id, code=None):
        Concept.objects.get_or_create(
            concept_id=cid,
            defaults={
                'concept_name': name,
                'domain_id': domain_id,
                'vocabulary_id': vocab_id,
                'concept_class_id': 'Lab Test',
                'concept_code': code or str(cid),
                'valid_start_date': today,
                'valid_end_date': far_future,
            },
        )

    # Concept 0 — required by OMOP as the "no matching concept" sentinel
    _c(0, 'No matching concept', 'Measurement', 'LOINC', '0')
    # LOINC concepts
    _c(3000963, 'Hemoglobin [Mass/volume] in Blood', 'Measurement', 'LOINC', '718-7')
    _c(3004249, 'Creatinine [Mass/volume] in Serum', 'Measurement', 'LOINC', '2160-0')
    # UCUM unit
    _c(8713, 'gram per deciliter', 'Measurement', 'UCUM', 'g/dL')
    # Type concepts
    _c(32865, 'Patient self-report', 'Type Concept', 'LOINC', '32865')
    _c(32883, 'Document extraction', 'Type Concept', 'LOINC', '32883')
    # Visit concept
    _c(9202, 'Outpatient Visit', 'Visit', 'LOINC', '9202')

    # LoincClass data
    LoincClass.objects.get_or_create(code='HEM/BC', defaults={'display_name': 'Hematology'})
    LoincClass.objects.get_or_create(code='CHEM', defaults={'display_name': 'Chemistry'})
    LoincCodeClass.objects.get_or_create(loinc_num='718-7', defaults={'loinc_class_id': 'HEM/BC'})
    LoincCodeClass.objects.get_or_create(loinc_num='2160-0', defaults={'loinc_class_id': 'CHEM'})


class SyncViewTest(TestCase):
    def setUp(self):
        _setup_vocab()
        self.user = Identity.objects.create_user(email='labsync@test.com', password='test')
        self.user.is_superuser = True
        self.user.save()
        self.person = Person.objects.create(person_id=1001)
        PatientInfo.objects.create(person=self.person)
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    def test_sync_loinc_matched(self):
        resp = self.client.post('/api/lab-results/sync/', {
            'person_id': 1001,
            'measurements': [
                {
                    'loinc_code': '718-7',
                    'test_name': 'Hemoglobin',
                    'value': '13.5',
                    'unit': 'g/dL',
                    'measured_at': '2026-05-15',
                    'range_low': '12.0',
                    'range_high': '15.5',
                },
            ],
            'lab_name': 'Quest Diagnostics',
            'lab_date': '2026-05-15',
            'report_filename': 'bloodwork.pdf',
            'source_type': 'document_extraction',
        }, format='json')
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)
        self.assertEqual(resp.data['count'], 1)
        self.assertIn('visit_occurrence_id', resp.data)
        self.assertEqual(len(resp.data['measurement_ids']), 1)

        m = Measurement.objects.get(measurement_id=resp.data['measurement_ids'][0])
        self.assertEqual(m.person_id, 1001)
        self.assertEqual(m.measurement_concept_id, 3000963)
        self.assertEqual(m.value_as_number, Decimal('13.50000'))
        self.assertEqual(m.range_low, Decimal('12.00000'))
        self.assertEqual(m.range_high, Decimal('15.50000'))

        visit = VisitOccurrence.objects.get(
            visit_occurrence_id=resp.data['visit_occurrence_id']
        )
        self.assertEqual(visit.visit_source_value, 'bloodwork.pdf')

        care_site = CareSite.objects.get(care_site_name='Quest Diagnostics')
        self.assertEqual(visit.care_site_id, care_site.care_site_id)

    def test_sync_loinc_unmatched_creates_hk_concept(self):
        resp = self.client.post('/api/lab-results/sync/', {
            'person_id': 1001,
            'measurements': [
                {
                    'loinc_code': '',
                    'test_name': 'Obscure Regional Panel',
                    'value': '42.0',
                    'measured_at': '2026-05-10',
                },
            ],
            'source_type': 'patient_self_report',
        }, format='json')
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)

        m = Measurement.objects.get(measurement_id=resp.data['measurement_ids'][0])
        self.assertEqual(m.measurement_concept_id, 0)
        self.assertIsNotNone(m.measurement_source_concept_id)

        hk_concept = Concept.objects.get(concept_id=m.measurement_source_concept_id)
        self.assertEqual(hk_concept.vocabulary_id, 'HK-Labs')
        self.assertEqual(hk_concept.concept_code, 'hkl:obscure-regional-panel')

    def test_sync_invalid_person_404(self):
        resp = self.client.post('/api/lab-results/sync/', {
            'person_id': 9999,
            'measurements': [{'test_name': 'X', 'measured_at': '2026-01-01'}],
        }, format='json')
        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)


class ResultsSummaryViewTest(TestCase):
    def setUp(self):
        _setup_vocab()
        self.user = Identity.objects.create_user(email='reader@test.com', password='test')
        self.person = Person.objects.create(person_id=2001)
        PatientInfo.objects.create(person=self.person)
        PatientUser.objects.create(identity=self.user, person=self.person)
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

        type_concept = Concept.objects.get(concept_id=32883)
        visit_concept = Concept.objects.get(concept_id=9202)
        hgb_concept = Concept.objects.get(concept_id=3000963)

        self.visit = VisitOccurrence.objects.create(
            visit_occurrence_id=1,
            person_id=2001,
            visit_concept=visit_concept,
            visit_start_date=date(2026, 5, 15),
            visit_end_date=date(2026, 5, 15),
            visit_type_concept=type_concept,
            visit_source_value='report.pdf',
        )

        Measurement.objects.create(
            measurement_id=1,
            person_id=2001,
            measurement_concept=hgb_concept,
            measurement_date=date(2026, 5, 15),
            measurement_type_concept=type_concept,
            value_as_number=Decimal('13.5'),
            range_low=Decimal('12.0'),
            range_high=Decimal('15.5'),
            visit_occurrence=self.visit,
            unit_source_value='g/dL',
        )
        Measurement.objects.create(
            measurement_id=2,
            person_id=2001,
            measurement_concept=hgb_concept,
            measurement_date=date(2026, 4, 10),
            measurement_type_concept=type_concept,
            value_as_number=Decimal('11.0'),
            range_low=Decimal('12.0'),
            range_high=Decimal('15.5'),
            unit_source_value='g/dL',
        )

    def test_summary_returns_grouped_card(self):
        resp = self.client.get('/api/lab-results/summary/', {'person_id': 2001})
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        results = resp.data['results']
        self.assertEqual(len(results), 1)

        card = results[0]
        self.assertEqual(card['concept_id'], 3000963)
        self.assertEqual(card['concept_code'], '718-7')
        self.assertEqual(card['category'], 'Hematology')
        self.assertEqual(len(card['values']), 2)

        v1 = card['values'][0]
        self.assertEqual(v1['status'], 'in_range')
        self.assertEqual(str(v1['measured_at']), '2026-05-15')
        self.assertEqual(v1['report_filename'], 'report.pdf')

        v2 = card['values'][1]
        self.assertEqual(v2['status'], 'below')

    def test_summary_resolves_person_from_email(self):
        self.user.email = 'reader_resolve@example.com'
        self.user.save()
        PatientInfo.objects.filter(person=self.person).update(email='reader_resolve@example.com')
        resp = self.client.get('/api/lab-results/summary/')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(len(resp.data['results']), 1)

    def test_summary_no_linked_patient_404(self):
        unlinked = Identity.objects.create_user(email='unlinked@test.com', password='test')
        self.client.force_authenticate(user=unlinked)
        resp = self.client.get('/api/lab-results/summary/')
        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)

    def test_summary_forbidden_for_other_person(self):
        resp = self.client.get('/api/lab-results/summary/', {'person_id': 9999})
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)


class ValuesViewTest(TestCase):
    def setUp(self):
        _setup_vocab()
        self.user = Identity.objects.create_user(email='valreader@test.com', password='test')
        self.person = Person.objects.create(person_id=3001)
        PatientInfo.objects.create(person=self.person)
        PatientUser.objects.create(identity=self.user, person=self.person)
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

        type_concept = Concept.objects.get(concept_id=32883)
        hgb_concept = Concept.objects.get(concept_id=3000963)

        for i in range(3):
            Measurement.objects.create(
                measurement_id=100 + i,
                person_id=3001,
                measurement_concept=hgb_concept,
                measurement_date=date(2026, 5, 10 + i),
                measurement_type_concept=type_concept,
                value_as_number=Decimal(f'{12 + i}.0'),
                range_low=Decimal('12.0'),
                range_high=Decimal('15.5'),
                unit_source_value='g/dL',
            )

    def test_values_returns_paginated_list(self):
        resp = self.client.get('/api/lab-results/values/', {
            'person_id': 3001,
            'concept_code': '718-7',
        })
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data['count'], 3)
        self.assertEqual(len(resp.data['results']), 3)
        self.assertEqual(resp.data['results'][0]['measurement_id'], 102)

    def test_values_includes_concept_metadata(self):
        resp = self.client.get('/api/lab-results/values/', {
            'person_id': 3001,
            'concept_code': '718-7',
        })
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data['concept_id'], 3000963)
        self.assertEqual(resp.data['concept_code'], '718-7')
        self.assertEqual(resp.data['concept_name'], 'Hemoglobin [Mass/volume] in Blood')
        self.assertEqual(resp.data['vocabulary_id'], 'LOINC')
        self.assertEqual(resp.data['category'], 'Hematology')

    def test_values_resolves_person_from_email(self):
        self.user.email = 'valreader_resolve@example.com'
        self.user.save()
        PatientInfo.objects.filter(person=self.person).update(email='valreader_resolve@example.com')
        resp = self.client.get('/api/lab-results/values/', {'concept_code': '718-7'})
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data['count'], 3)

    def test_values_no_linked_patient_404(self):
        unlinked = Identity.objects.create_user(email='unlinked2@test.com', password='test')
        self.client.force_authenticate(user=unlinked)
        resp = self.client.get('/api/lab-results/values/', {'concept_code': '718-7'})
        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)

    def test_values_requires_concept_code(self):
        resp = self.client.get('/api/lab-results/values/', {'person_id': 3001})
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_values_unknown_concept_404(self):
        resp = self.client.get('/api/lab-results/values/', {
            'person_id': 3001,
            'concept_code': 'NOPE-0',
        })
        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)


class MeasurementDetailViewTest(TestCase):
    def setUp(self):
        _setup_vocab()
        # DELETE is privileged since the ScopedTokenPermission role change; this
        # suite verifies delete behaviour, so the caller is staff.
        self.user = Identity.objects.create_user(email='measuser@test.com', password='test', is_staff=True)
        self.person = Person.objects.create(person_id=4001)
        PatientInfo.objects.create(person=self.person)
        PatientUser.objects.create(identity=self.user, person=self.person)
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

        type_concept = Concept.objects.get(concept_id=32883)
        hgb_concept = Concept.objects.get(concept_id=3000963)

        self.measurement = Measurement.objects.create(
            measurement_id=200,
            person_id=4001,
            measurement_concept=hgb_concept,
            measurement_date=date(2026, 5, 15),
            measurement_type_concept=type_concept,
            value_as_number=Decimal('13.5'),
            range_low=Decimal('12.0'),
            range_high=Decimal('15.5'),
            unit_source_value='g/dL',
        )

    def test_get_measurement(self):
        resp = self.client.get('/api/lab-results/measurements/200/')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data['measurement_id'], 200)
        self.assertEqual(resp.data['status'], 'in_range')

    def test_get_measurement_not_found(self):
        resp = self.client.get('/api/lab-results/measurements/999/')
        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)

    def test_patch_value(self):
        resp = self.client.patch(
            '/api/lab-results/measurements/200/',
            {'value': '11.0'},
            format='json',
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.measurement.refresh_from_db()
        self.assertEqual(self.measurement.value_as_number, Decimal('11.0'))

    def test_patch_range(self):
        resp = self.client.patch(
            '/api/lab-results/measurements/200/',
            {'range_low': '10.0', 'range_high': '16.0'},
            format='json',
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.measurement.refresh_from_db()
        self.assertEqual(self.measurement.range_low, Decimal('10.0'))
        self.assertEqual(self.measurement.range_high, Decimal('16.0'))

    def test_patch_invalid_value(self):
        resp = self.client.patch(
            '/api/lab-results/measurements/200/',
            {'value': 'not-a-number'},
            format='json',
        )
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_patch_measured_at(self):
        resp = self.client.patch(
            '/api/lab-results/measurements/200/',
            {'measured_at': '2026-06-01'},
            format='json',
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.measurement.refresh_from_db()
        self.assertEqual(self.measurement.measurement_date, date(2026, 6, 1))

    def test_delete_measurement(self):
        resp = self.client.delete('/api/lab-results/measurements/200/')
        self.assertEqual(resp.status_code, status.HTTP_204_NO_CONTENT)
        self.assertFalse(Measurement.objects.filter(measurement_id=200).exists())

    def test_delete_not_found(self):
        resp = self.client.delete('/api/lab-results/measurements/999/')
        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)


class AutoProvisionTest(TestCase):
    """Test that _get_or_create auto-provisions Person + PatientInfo."""

    def setUp(self):
        _setup_vocab()

    def test_new_user_gets_person_and_patient_info(self):
        from patient_portal.api.authentication import _ensure_person

        user = Identity.objects.create_user(
            email='newpatient@example.com',
        )
        _ensure_person(user)

        pi = PatientInfo.objects.get(email='newpatient@example.com')
        self.assertIsNotNone(pi.person_id)
        self.assertTrue(Person.objects.filter(person_id=pi.person_id).exists())

    def test_existing_patient_info_not_duplicated(self):
        from patient_portal.api.authentication import _ensure_person

        person = Person.objects.create(person_id=9001)
        PatientInfo.objects.create(person=person, email='existing@example.com')

        user = Identity.objects.create_user(
            email='existing@example.com',
        )
        _ensure_person(user)

        self.assertEqual(PatientInfo.objects.filter(email='existing@example.com').count(), 1)

    def test_autoprovisioned_user_can_access_summary(self):
        from patient_portal.api.authentication import _ensure_person

        user = Identity.objects.create_user(
            email='autouser@example.com',
        )
        _ensure_person(user)

        client = APIClient()
        client.force_authenticate(user=user)
        resp = client.get('/api/lab-results/summary/')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data['results'], [])


class VisitDeleteViewTest(TestCase):
    def setUp(self):
        _setup_vocab()
        # DELETE is privileged since the ScopedTokenPermission role change; this
        # suite verifies delete behaviour, so the caller is staff.
        self.user = Identity.objects.create_user(email='visitdel@test.com', password='test', is_staff=True)
        self.person = Person.objects.create(person_id=5001)
        PatientInfo.objects.create(person=self.person)
        PatientUser.objects.create(identity=self.user, person=self.person)
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

        type_concept = Concept.objects.get(concept_id=32883)
        visit_concept = Concept.objects.get(concept_id=9202)
        hgb_concept = Concept.objects.get(concept_id=3000963)

        self.visit = VisitOccurrence.objects.create(
            visit_occurrence_id=500,
            person_id=5001,
            visit_concept=visit_concept,
            visit_start_date=date(2026, 5, 15),
            visit_end_date=date(2026, 5, 15),
            visit_type_concept=type_concept,
            visit_source_value='bloodwork.pdf',
        )

        for i in range(3):
            Measurement.objects.create(
                measurement_id=500 + i,
                person_id=5001,
                measurement_concept=hgb_concept,
                measurement_date=date(2026, 5, 15),
                measurement_type_concept=type_concept,
                value_as_number=Decimal('13.5'),
                visit_occurrence=self.visit,
            )

    def test_delete_visit_cascades_measurements(self):
        resp = self.client.delete('/api/lab-results/visits/500/')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data['deleted_measurements'], 3)
        self.assertFalse(VisitOccurrence.objects.filter(visit_occurrence_id=500).exists())
        self.assertFalse(Measurement.objects.filter(visit_occurrence_id=500).exists())

    def test_delete_visit_not_found(self):
        resp = self.client.delete('/api/lab-results/visits/9999/')
        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)


class SyncOnBehalfOfTest(TestCase):
    """Tests for actor_iss/actor_sub on-behalf-of sync flow."""

    def setUp(self):
        _setup_vocab()
        self.service_user = Identity.objects.create_user(email='service@test.com', password='test')
        self.service_user.is_superuser = True
        self.service_user.save()

        self.actor = Identity.objects.create_user(email='actor@test.com', password='test')

        self.person = Person.objects.create(person_id=2001)
        PatientUser.objects.create(identity=self.actor, person=self.person)

        self.client = APIClient()
        self.client.force_authenticate(user=self.service_user)

    def _sync_payload(self, **overrides):
        base = {
            'person_id': 2001,
            'actor_iss': 'urn:local',
            'actor_sub': self.actor.sub,
            'measurements': [{
                'test_name': 'Glucose',
                'loinc_code': '718-7',
                'value': '100.0',
                'unit': 'mg/dL',
                'measured_at': '2026-05-01',
            }],
            'source_type': 'document_extraction',
        }
        base.update(overrides)
        return base

    def test_on_behalf_of_with_valid_actor(self):
        resp = self.client.post('/api/lab-results/sync/', self._sync_payload(), format='json')
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)

    def test_on_behalf_of_actor_not_found(self):
        resp = self.client.post('/api/lab-results/sync/', self._sync_payload(
            actor_iss='urn:unknown', actor_sub='nonexistent',
        ), format='json')
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)
        self.assertIn('Actor identity not found', resp.data['detail'])

    def test_on_behalf_of_actor_no_access(self):
        other_person = Person.objects.create(person_id=2002)
        no_access_actor = Identity.objects.create_user(email='noaccess@test.com', password='test')
        resp = self.client.post('/api/lab-results/sync/', self._sync_payload(
            person_id=other_person.person_id,
            actor_iss='urn:local',
            actor_sub=no_access_actor.sub,
        ), format='json')
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)
        self.assertIn('does not have access', resp.data['detail'])

    def test_on_behalf_of_without_actor_fields_non_superuser(self):
        non_su = Identity.objects.create_user(email='nonsu@test.com', password='test')
        client = APIClient()
        client.force_authenticate(user=non_su)
        resp = client.post('/api/lab-results/sync/', {
            'person_id': 2001,
            'measurements': [{
                'test_name': 'Glucose',
                'loinc_code': '718-7',
                'value': '100.0',
                'unit': 'mg/dL',
                'measured_at': '2026-05-01',
            }],
            'source_type': 'document_extraction',
        }, format='json')
        # Since the ScopedTokenPermission role change a non-superuser/non-service
        # caller is denied at the permission layer (before the actor-field
        # validation is even reached).
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)

    def test_on_behalf_of_superuser_without_actor_fields_succeeds(self):
        resp = self.client.post('/api/lab-results/sync/', {
            'person_id': 2001,
            'measurements': [{
                'test_name': 'Glucose',
                'loinc_code': '718-7',
                'value': '100.0',
                'unit': 'mg/dL',
                'measured_at': '2026-05-01',
            }],
            'source_type': 'document_extraction',
        }, format='json')
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)


class PipeCharacterValidationTest(TestCase):
    """Tests for pipe character rejection in actor fields."""

    def setUp(self):
        _setup_vocab()
        self.user = Identity.objects.create_user(email='pipe@test.com', password='test')
        self.user.is_superuser = True
        self.user.save()
        Person.objects.create(person_id=3001)
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    def test_pipe_in_actor_iss_rejected(self):
        resp = self.client.post('/api/lab-results/sync/', {
            'person_id': 3001,
            'actor_iss': 'urn:local|evil',
            'actor_sub': 'test',
            'measurements': [{
                'test_name': 'WBC', 'value': '5.0',
                'unit': 'K/uL', 'measured_at': '2026-05-01',
            }],
            'source_type': 'document_extraction',
        }, format='json')
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_pipe_in_actor_sub_rejected(self):
        resp = self.client.post('/api/lab-results/sync/', {
            'person_id': 3001,
            'actor_iss': 'urn:local',
            'actor_sub': 'test|evil',
            'measurements': [{
                'test_name': 'WBC', 'value': '5.0',
                'unit': 'K/uL', 'measured_at': '2026-05-01',
            }],
            'source_type': 'document_extraction',
        }, format='json')
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)


class PatchInvalidDateTest(TestCase):
    """Test PATCH with invalid date returns 400."""

    def setUp(self):
        _setup_vocab()
        self.user = Identity.objects.create_user(email='patchdate@test.com', password='test')
        self.person = Person.objects.create(person_id=4001)
        PatientUser.objects.create(identity=self.user, person=self.person)
        type_concept = Concept.objects.get(concept_id=32883)
        hgb_concept = Concept.objects.get(concept_id=3000963)
        self.measurement = Measurement.objects.create(
            measurement_id=4000,
            person=self.person,
            measurement_concept=hgb_concept,
            measurement_date=date(2026, 1, 1),
            measurement_type_concept=type_concept,
            value_as_number=14.0,
        )
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    def test_patch_invalid_date_string(self):
        resp = self.client.patch(
            '/api/lab-results/measurements/4000/',
            {'measured_at': 'not-a-date'},
            format='json',
        )
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('measured_at', resp.data)

    def test_patch_empty_date_string(self):
        resp = self.client.patch(
            '/api/lab-results/measurements/4000/',
            {'measured_at': ''},
            format='json',
        )
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)


class PersonalRepresentativeAccessTest(TestCase):
    """Tests for PersonalRepresentative verification_status enforcement."""

    def setUp(self):
        from omop_core.models import PersonalRepresentative
        _setup_vocab()
        self.patient_identity = Identity.objects.create_user(
            email='patient@test.com', password='test',
        )
        self.rep_identity = Identity.objects.create_user(
            email='rep@test.com', password='test',
        )
        self.person = Person.objects.create(person_id=5001)
        PatientUser.objects.create(identity=self.patient_identity, person=self.person)
        PatientInfo.objects.create(person=self.person, email='patient@test.com')

        type_concept = Concept.objects.get(concept_id=32883)
        hgb_concept = Concept.objects.get(concept_id=3000963)
        Measurement.objects.create(
            measurement_id=5000,
            person=self.person,
            measurement_concept=hgb_concept,
            measurement_date=date(2026, 1, 1),
            measurement_type_concept=type_concept,
            value_as_number=12.0,
        )

        self.rep = PersonalRepresentative.objects.create(
            representative=self.rep_identity,
            person_id=self.person.person_id,
            relationship='parent',
            verification_status='PENDING',
        )
        self.client = APIClient()

    def test_pending_representative_denied_summary(self):
        self.client.force_authenticate(user=self.rep_identity)
        resp = self.client.get(f'/api/lab-results/summary/?person_id={self.person.person_id}')
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)

    def test_verified_representative_allowed_summary(self):
        self.rep.verification_status = 'VERIFIED'
        self.rep.save()
        self.client.force_authenticate(user=self.rep_identity)
        resp = self.client.get(f'/api/lab-results/summary/?person_id={self.person.person_id}')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

    def test_rejected_representative_denied_summary(self):
        self.rep.verification_status = 'REJECTED'
        self.rep.save()
        self.client.force_authenticate(user=self.rep_identity)
        resp = self.client.get(f'/api/lab-results/summary/?person_id={self.person.person_id}')
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)


class ProfessionalGroupAccessTest(TestCase):
    """Tests for ProfessionalGroupAccess expires_at enforcement."""

    def setUp(self):
        from omop_core.models import (
            Organization, PatientGroup, PatientGroupMembership,
            ProfessionalGroupAccess,
        )
        from django.utils import timezone
        from datetime import timedelta
        _setup_vocab()

        self.org = Organization.objects.create(name='Test Org', slug='test-org')
        self.group = PatientGroup.objects.create(
            organization=self.org, name='Oncology', slug='oncology',
        )
        self.doctor = Identity.objects.create_user(email='doctor@test.com', password='test')
        self.person = Person.objects.create(person_id=6001)
        PatientInfo.objects.create(person=self.person, email='p6001@test.com')

        PatientGroupMembership.objects.create(group=self.group, person_id=self.person.person_id)

        type_concept = Concept.objects.get(concept_id=32883)
        hgb_concept = Concept.objects.get(concept_id=3000963)
        Measurement.objects.create(
            measurement_id=6000,
            person=self.person,
            measurement_concept=hgb_concept,
            measurement_date=date(2026, 1, 1),
            measurement_type_concept=type_concept,
            value_as_number=11.0,
        )

        self.grant = ProfessionalGroupAccess.objects.create(
            identity=self.doctor,
            group=self.group,
            role='doctor',
            expires_at=timezone.now() + timedelta(days=30),
        )
        self.client = APIClient()
        self.client.force_authenticate(user=self.doctor)

    def test_active_grant_allows_access(self):
        resp = self.client.get(f'/api/lab-results/summary/?person_id={self.person.person_id}')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

    def test_expired_grant_denied(self):
        from django.utils import timezone
        from datetime import timedelta
        self.grant.expires_at = timezone.now() - timedelta(days=1)
        self.grant.save()
        resp = self.client.get(f'/api/lab-results/summary/?person_id={self.person.person_id}')
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)

    def test_null_expires_at_allows_access(self):
        self.grant.expires_at = None
        self.grant.save()
        resp = self.client.get(f'/api/lab-results/summary/?person_id={self.person.person_id}')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)


class OrgScopedSyncRejectionTest(TestCase):
    """Tests for org-scoped sync rejection via OAuth2 token."""

    def setUp(self):
        from omop_core.models import Organization, ApplicationOrganization
        from oauth2_provider.models import Application
        _setup_vocab()

        self.org = Organization.objects.create(name='Org A', slug='org-a')
        self.user = Identity.objects.create_user(email='orguser@test.com', password='test')
        PatientUser.objects.create(
            identity=self.user,
            person=Person.objects.create(person_id=7099),
        )

        self.app = Application.objects.create(
            name='Test App',
            user=self.user,
            client_type=Application.CLIENT_CONFIDENTIAL,
            authorization_grant_type=Application.GRANT_CLIENT_CREDENTIALS,
        )
        ApplicationOrganization.objects.create(application=self.app, organization=self.org)

        self.person_in_org = Person.objects.create(person_id=7001)
        PatientInfo.objects.create(
            person=self.person_in_org, email='p7001@test.com', organization=self.org,
        )

        self.person_outside_org = Person.objects.create(person_id=7002)
        PatientInfo.objects.create(person=self.person_outside_org, email='p7002@test.com')

        self.client = APIClient()

    def _make_token(self, suffix):
        from oauth2_provider.models import AccessToken
        from django.utils import timezone
        from datetime import timedelta
        return AccessToken.objects.create(
            user=self.user,
            application=self.app,
            token=f'test-token-{suffix}',
            expires=timezone.now() + timedelta(hours=1),
            scope='patient/*.write',
        )

    def _sync_payload(self, person_id):
        return {
            'person_id': person_id,
            'actor_iss': self.user.issuer,
            'actor_sub': self.user.sub,
            'measurements': [{
                'test_name': 'WBC', 'value': '5.0',
                'unit': 'K/uL', 'measured_at': '2026-05-01',
            }],
            'source_type': 'document_extraction',
        }

    def test_sync_allowed_for_person_in_org(self):
        from omop_core.models import PersonalRepresentative
        PersonalRepresentative.objects.create(
            representative=self.user, person_id=7001,
            relationship='caregiver', verification_status='VERIFIED',
        )
        token = self._make_token('in-org')
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {token.token}')
        resp = self.client.post('/api/lab-results/sync/', self._sync_payload(7001), format='json')
        self.assertIn(resp.status_code, [status.HTTP_201_CREATED, status.HTTP_200_OK])

    def test_sync_rejected_for_person_outside_org(self):
        from omop_core.models import PersonalRepresentative
        PersonalRepresentative.objects.create(
            representative=self.user, person_id=7002,
            relationship='caregiver', verification_status='VERIFIED',
        )
        token = self._make_token('outside-org')
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {token.token}')
        resp = self.client.post('/api/lab-results/sync/', self._sync_payload(7002), format='json')
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)
        self.assertIn('Person not in your organization', resp.data['detail'])


class FirebaseAuthedSyncTest(TestCase):
    """Tests for sync endpoint when authenticated via Firebase token (PartnerAuth).

    When request.user is a real Identity (issuer != 'urn:service'), the sync
    endpoint should resolve the person from request.user directly — not from
    actor_iss/actor_sub in the payload.
    """

    def setUp(self):
        _setup_vocab()
        self.firebase_user = Identity.objects.get_or_create(
            issuer='https://securetoken.google.com/healthtree-test',
            sub='firebase-uid-abc123',
            defaults={'email': 'patient@example.com'},
        )[0]
        self.firebase_user.set_unusable_password()
        # POST sync is privileged since the ScopedTokenPermission role change;
        # this suite exercises the request.user (non-service) person-resolution
        # path, so the firebase identity is staff to retain write access.
        self.firebase_user.is_staff = True
        self.firebase_user.save()

        self.person = Person.objects.create(person_id=9001)
        PatientInfo.objects.create(person=self.person, email='patient@example.com')
        PatientUser.objects.create(identity=self.firebase_user, person=self.person)

        self.client = APIClient()
        self.client.force_authenticate(user=self.firebase_user)

    def _sync_payload(self, **overrides):
        base = {
            'measurements': [{
                'test_name': 'Hemoglobin',
                'loinc_code': '718-7',
                'value': '14.0',
                'unit': 'g/dL',
                'measured_at': '2026-05-15',
            }],
            'source_type': 'document_extraction',
        }
        base.update(overrides)
        return base

    def test_firebase_authed_sync_resolves_person_from_request_user(self):
        resp = self.client.post(
            '/api/lab-results/sync/', self._sync_payload(), format='json',
        )
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)
        m = Measurement.objects.get(measurement_id=resp.data['measurement_ids'][0])
        self.assertEqual(m.person_id, self.person.person_id)

    def test_firebase_authed_sync_ignores_actor_fields_in_body(self):
        resp = self.client.post(
            '/api/lab-results/sync/',
            self._sync_payload(
                actor_iss='urn:different', actor_sub='different-sub',
            ),
            format='json',
        )
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)
        m = Measurement.objects.get(measurement_id=resp.data['measurement_ids'][0])
        self.assertEqual(m.person_id, self.person.person_id)

    def test_firebase_authed_sync_same_person_as_me_endpoint(self):
        from patient_portal.api.views import PatientInfoViewSet
        resp = self.client.post(
            '/api/lab-results/sync/', self._sync_payload(), format='json',
        )
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)
        synced_person_id = Measurement.objects.get(
            measurement_id=resp.data['measurement_ids'][0],
        ).person_id

        pu = PatientUser.objects.get(identity=self.firebase_user)
        self.assertEqual(synced_person_id, pu.person_id)

    def test_firebase_authed_with_explicit_person_id_for_other_person_denied(self):
        other_person = Person.objects.create(person_id=8888)
        PatientInfo.objects.create(person=other_person)
        resp = self.client.post(
            '/api/lab-results/sync/',
            self._sync_payload(person_id=other_person.person_id),
            format='json',
        )
        self.assertIn(resp.status_code, [
            status.HTTP_400_BAD_REQUEST,
            status.HTTP_403_FORBIDDEN,
        ])

    def test_firebase_authed_existing_user_no_patientuser_links_via_email(self):
        new_user = Identity.objects.get_or_create(
            issuer='https://securetoken.google.com/healthtree-test',
            sub='firebase-uid-brand-new',
            defaults={'email': 'emailmatch@example.com'},
        )[0]
        new_user.set_unusable_password()
        new_user.is_staff = True  # privileged caller; see setUp note
        new_user.save()
        person2 = Person.objects.create(person_id=9002)
        PatientInfo.objects.create(person=person2, email='emailmatch@example.com')
        self.client.force_authenticate(user=new_user)

        resp = self.client.post(
            '/api/lab-results/sync/', self._sync_payload(), format='json',
        )
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)
        pu = PatientUser.objects.get(identity=new_user)
        self.assertEqual(pu.person_id, person2.person_id)
        m = Measurement.objects.get(measurement_id=resp.data['measurement_ids'][0])
        self.assertEqual(m.person_id, person2.person_id)


class ServiceTokenSyncFallbackTest(TestCase):
    """Tests that service-token auth still uses actor_iss/actor_sub from payload."""

    def setUp(self):
        _setup_vocab()
        self.service_user = Identity.objects.get_or_create(
            issuer='urn:service', sub='hk-labs-sync',
        )[0]
        self.service_user.set_unusable_password()
        self.service_user.save()

        self.patient = Identity.objects.create_user(
            email='patient-svc@test.com', password='test',
        )
        self.person = Person.objects.create(person_id=9010)
        PatientInfo.objects.create(person=self.person, email='patient-svc@test.com')
        PatientUser.objects.create(identity=self.patient, person=self.person)

        self.client = APIClient()
        # Production path: hk-labs calls this with a service token (request.auth
        # == "service-token"), which ScopedTokenPermission grants full access.
        self.client.force_authenticate(user=self.service_user, token="service-token")

    def test_service_token_resolves_person_from_actor_fields(self):
        resp = self.client.post('/api/lab-results/sync/', {
            'actor_iss': self.patient.issuer,
            'actor_sub': self.patient.sub,
            'measurements': [{
                'test_name': 'WBC', 'loinc_code': '718-7',
                'value': '7.0', 'unit': 'g/dL', 'measured_at': '2026-05-15',
            }],
            'source_type': 'document_extraction',
        }, format='json')
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)

    def test_service_token_empty_actor_and_no_person_id_returns_400(self):
        resp = self.client.post('/api/lab-results/sync/', {
            'actor_iss': '',
            'actor_sub': '',
            'measurements': [{
                'test_name': 'WBC', 'value': '7.0',
                'unit': 'K/uL', 'measured_at': '2026-05-15',
            }],
            'source_type': 'document_extraction',
        }, format='json')
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_service_token_known_actor_resolves_correctly(self):
        resp = self.client.post('/api/lab-results/sync/', {
            'actor_iss': self.patient.issuer,
            'actor_sub': self.patient.sub,
            'measurements': [{
                'test_name': 'WBC', 'loinc_code': '718-7',
                'value': '7.0', 'unit': 'g/dL', 'measured_at': '2026-05-15',
            }],
            'source_type': 'document_extraction',
        }, format='json')
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)
        m = Measurement.objects.get(measurement_id=resp.data['measurement_ids'][0])
        self.assertEqual(m.person_id, self.person.person_id)


class SyncNonSuperuserTest(TestCase):
    """A non-superuser/non-service identity is denied POST sync entirely.

    Since the ScopedTokenPermission role change, write access requires a service
    token or staff/superuser; a plain patient identity (even for its own person)
    is rejected at the permission layer. Service-side ingest is covered by
    ServiceTokenSyncFallbackTest / SyncOnBehalfOfTest.
    """

    def setUp(self):
        _setup_vocab()
        self.user = Identity.objects.create_user(email='patient@test.com', password='test')
        self.person = Person.objects.create(person_id=2001)
        PatientInfo.objects.create(person=self.person)
        PatientUser.objects.create(identity=self.user, person=self.person)
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    def _payload(self, person_id=2001):
        return {
            'person_id': person_id,
            'actor_iss': self.user.issuer,
            'actor_sub': self.user.sub,
            'measurements': [
                {
                    'loinc_code': '718-7',
                    'test_name': 'Hemoglobin',
                    'value': '14.0',
                    'unit': 'g/dL',
                    'measured_at': '2026-05-20',
                },
            ],
            'source_type': 'patient_self_report',
        }

    def test_sync_own_data_denied(self):
        # Own person, but a non-privileged caller can no longer POST sync.
        resp = self.client.post('/api/lab-results/sync/', self._payload(), format='json')
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)

    def test_sync_other_person_denied(self):
        other = Person.objects.create(person_id=2002)
        PatientInfo.objects.create(person=other)
        resp = self.client.post('/api/lab-results/sync/', self._payload(person_id=2002), format='json')
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)

    def test_sync_nonexistent_person_denied(self):
        # Permission denial (403) now precedes the person lookup that previously
        # produced a 404 for a non-privileged caller.
        resp = self.client.post('/api/lab-results/sync/', self._payload(person_id=9999), format='json')
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)


class DedupSyncTest(TestCase):
    """Tests for measurement deduplication on sync."""

    def setUp(self):
        _setup_vocab()
        self.user = Identity.objects.create_user(email='dedup@test.com', password='test')
        self.user.is_superuser = True
        self.user.save()
        self.person = Person.objects.create(person_id=3001)
        PatientInfo.objects.create(person=self.person)
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    def _payload(self, **overrides):
        data = {
            'person_id': 3001,
            'measurements': [
                {
                    'loinc_code': '718-7',
                    'test_name': 'Hemoglobin',
                    'value': '13.5',
                    'unit': 'g/dL',
                    'measured_at': '2026-05-15',
                },
            ],
            'lab_name': 'Quest',
            'lab_date': '2026-05-15',
            'report_filename': 'report.pdf',
            'source_type': 'document_extraction',
        }
        data.update(overrides)
        return data

    def test_duplicate_upload_deduplicates(self):
        """Same report uploaded twice creates one measurement, one visit, one ownership record."""
        r1 = self.client.post('/api/lab-results/sync/', self._payload(), format='json')
        self.assertEqual(r1.status_code, 201)
        self.assertEqual(r1.data['created_count'], 1)
        self.assertEqual(r1.data['deduplicated_count'], 0)
        m_id = r1.data['measurement_ids'][0]

        r2 = self.client.post('/api/lab-results/sync/', self._payload(), format='json')
        self.assertEqual(r2.status_code, 201)
        self.assertEqual(r2.data['created_count'], 0)
        self.assertEqual(r2.data['deduplicated_count'], 1)
        self.assertEqual(r2.data['measurement_ids'][0], m_id)
        # Idempotent visit: same report_filename returns the same VisitOccurrence.
        self.assertEqual(r1.data['visit_occurrence_id'], r2.data['visit_occurrence_id'])

        self.assertEqual(Measurement.objects.filter(person_id=3001).count(), 1)
        # Only one ownership record — ignore_conflicts prevents a second row.
        self.assertEqual(MeasurementOwnership.objects.filter(measurement_id=m_id).count(), 1)

    def test_different_value_not_deduplicated(self):
        """Same test on same day with different value creates two measurements."""
        r1 = self.client.post('/api/lab-results/sync/', self._payload(), format='json')
        self.assertEqual(r1.data['created_count'], 1)

        payload2 = self._payload()
        payload2['measurements'][0]['value'] = '14.0'
        r2 = self.client.post('/api/lab-results/sync/', payload2, format='json')
        self.assertEqual(r2.data['created_count'], 1)
        self.assertEqual(r2.data['deduplicated_count'], 0)

        self.assertEqual(Measurement.objects.filter(person_id=3001).count(), 2)

    def test_qualitative_dedup(self):
        """Qualitative results (value=null, value_string set) are deduplicated correctly."""
        payload = self._payload()
        payload['measurements'] = [{
            'loinc_code': '718-7',
            'test_name': 'Hemoglobin',
            'value': None,
            'value_string': 'Negative',
            'measured_at': '2026-05-15',
        }]
        r1 = self.client.post('/api/lab-results/sync/', payload, format='json')
        self.assertEqual(r1.data['created_count'], 1)

        r2 = self.client.post('/api/lab-results/sync/', payload, format='json')
        self.assertEqual(r2.data['created_count'], 0)
        self.assertEqual(r2.data['deduplicated_count'], 1)
        self.assertEqual(r2.data['measurement_ids'], r1.data['measurement_ids'])

    def test_qualitative_different_string_not_deduplicated(self):
        """Same test, same day, null value, different value_string creates two measurements."""
        payload1 = self._payload()
        payload1['measurements'] = [{
            'loinc_code': '718-7',
            'test_name': 'Hemoglobin',
            'value': None,
            'value_string': 'Negative',
            'measured_at': '2026-05-15',
        }]
        self.client.post('/api/lab-results/sync/', payload1, format='json')

        payload2 = self._payload()
        payload2['measurements'] = [{
            'loinc_code': '718-7',
            'test_name': 'Hemoglobin',
            'value': None,
            'value_string': 'Positive',
            'measured_at': '2026-05-15',
        }]
        r2 = self.client.post('/api/lab-results/sync/', payload2, format='json')
        self.assertEqual(r2.data['created_count'], 1)
        self.assertEqual(Measurement.objects.filter(person_id=3001).count(), 2)

    def test_delete_one_of_two_owners_preserves_measurement(self):
        """Deleting one visit when two distinct visits own the measurement preserves the measurement."""
        # Use two DIFFERENT report filenames to get two distinct visits.
        r1 = self.client.post('/api/lab-results/sync/', self._payload(report_filename='r1.pdf'), format='json')
        r2 = self.client.post('/api/lab-results/sync/', self._payload(report_filename='r2.pdf'), format='json')
        m_id = r1.data['measurement_ids'][0]
        v1 = r1.data['visit_occurrence_id']

        del_resp = self.client.delete(f'/api/lab-results/visits/{v1}/')
        self.assertEqual(del_resp.status_code, 200)
        self.assertEqual(del_resp.data['deleted_measurements'], 0)

        # Measurement still exists, owned by second visit
        self.assertTrue(Measurement.objects.filter(measurement_id=m_id).exists())
        self.assertEqual(MeasurementOwnership.objects.filter(measurement_id=m_id).count(), 1)

    def test_delete_last_owner_deletes_measurement(self):
        """Deleting the last owning visit removes the measurement."""
        r1 = self.client.post('/api/lab-results/sync/', self._payload(report_filename='only.pdf'), format='json')
        m_id = r1.data['measurement_ids'][0]

        self.client.delete(f'/api/lab-results/visits/{r1.data["visit_occurrence_id"]}/')
        self.assertFalse(Measurement.objects.filter(measurement_id=m_id).exists())

    def test_ownership_records_created_on_sync(self):
        """Every sync creates ownership records for all measurements."""
        r = self.client.post('/api/lab-results/sync/', self._payload(), format='json')
        visit_id = r.data['visit_occurrence_id']
        m_id = r.data['measurement_ids'][0]

        self.assertTrue(
            MeasurementOwnership.objects.filter(
                measurement_id=m_id, visit_occurrence_id=visit_id,
            ).exists()
        )


# ---------------------------------------------------------------------------
# _resolve_person_id — email fallback cross-org safety (#17)
# ---------------------------------------------------------------------------

class ResolvePersonIdEmailFallbackTest(TestCase):
    """
    Verify that the email fallback in _resolve_person_id cannot match a
    patient from a different organisation when the caller has no org scope.
    """

    def setUp(self):
        _setup_vocab()
        self.person = Person.objects.create(person_id=17001)
        self.patient = PatientInfo.objects.create(
            person=self.person, email='shared@example.com',
        )

    def _user(self, **kwargs):
        import uuid
        return Identity.objects.create_user(
            email='shared@example.com',
            password='x',
            **kwargs,
        )

    def test_non_superuser_without_org_cannot_use_email_fallback(self):
        """Non-superuser with no PatientUser link and no org scope gets 404."""
        user = self._user()
        client = APIClient()
        client.force_authenticate(user=user)
        resp = client.get('/api/lab-results/summary/')
        # No PatientUser link + no org + not superuser → 404
        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)

    def test_superuser_without_org_can_use_email_fallback(self):
        """Superuser with no PatientUser link may still resolve by email."""
        user = self._user(is_superuser=True, is_staff=True)
        client = APIClient()
        client.force_authenticate(user=user)
        resp = client.get('/api/lab-results/summary/')
        # Superuser email fallback succeeds (200 even if no measurements)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

    def test_patient_with_patientuser_link_still_resolves(self):
        """PatientUser link always works regardless of org scope."""
        user = self._user()
        PatientUser.objects.create(identity=user, person=self.person)
        client = APIClient()
        client.force_authenticate(user=user)
        resp = client.get('/api/lab-results/summary/')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

    def test_ambiguous_email_across_orgs_blocked_for_non_superuser(self):
        """If two patients share an email across orgs, non-superuser is blocked."""
        from omop_core.models import Organization
        org2 = Organization.objects.create(name='OtherOrg')
        person2 = Person.objects.create(person_id=17002)
        PatientInfo.objects.create(
            person=person2, email='shared@example.com', organization=org2,
        )
        user = self._user()
        client = APIClient()
        client.force_authenticate(user=user)
        resp = client.get('/api/lab-results/summary/')
        # Two patients with same email, no org scope, non-superuser → 404
        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)


# ---------------------------------------------------------------------------
# _resolve_person_id — org isolation cannot be bypassed via can_access_patient
# ---------------------------------------------------------------------------

class ResolvePersonIdOrgBypassTest(TestCase):
    """
    Verify that an org-scoped token cannot access a patient from a different
    org even when can_access_patient() would return True (e.g. via
    ProfessionalGroupAccess that spans organisations).

    Before the fix, _resolve_person_id only ran the org check when
    can_access_patient() returned False.  A cross-org group grant therefore
    caused the org check to be skipped entirely, granting access.
    """

    def setUp(self):
        from omop_core.models import Organization, ApplicationOrganization, PatientGroupMembership, ProfessionalGroupAccess
        from oauth2_provider.models import Application, AccessToken
        from django.utils import timezone
        from datetime import timedelta
        _setup_vocab()

        # Two orgs
        self.org_a = Organization.objects.create(name='Org A', slug='rp-org-a')
        self.org_b = Organization.objects.create(name='Org B', slug='rp-org-b')

        # Provider user whose token is scoped to org-A
        self.provider = Identity.objects.create_user(email='provider@test.com', password='x')

        self.app = Application.objects.create(
            name='Org A App',
            user=self.provider,
            client_type=Application.CLIENT_CONFIDENTIAL,
            authorization_grant_type=Application.GRANT_CLIENT_CREDENTIALS,
        )
        ApplicationOrganization.objects.create(application=self.app, organization=self.org_a)

        self.token = AccessToken.objects.create(
            user=self.provider,
            application=self.app,
            token='rp-bypass-test-token',
            expires=timezone.now() + timedelta(hours=1),
            scope='patient/*.read',
        )

        # Patient in org-A (accessible)
        self.person_in_a = Person.objects.create(person_id=18001)
        PatientInfo.objects.create(person=self.person_in_a, organization=self.org_a)

        # Patient in org-B (must NOT be accessible via org-A token)
        self.person_in_b = Person.objects.create(person_id=18002)
        PatientInfo.objects.create(person=self.person_in_b, organization=self.org_b)

        # Give the provider a ProfessionalGroupAccess that covers the org-B patient.
        # This simulates a cross-org group grant that must not bypass org isolation.
        from omop_core.models import PatientGroup
        self.group = PatientGroup.objects.create(
            name='Cross-org group', slug='cross-org-group', organization=self.org_b,
        )
        PatientGroupMembership.objects.create(group=self.group, person_id=self.person_in_b.person_id)
        ProfessionalGroupAccess.objects.create(
            identity=self.provider,
            group=self.group,
            role='navigator',
        )

        self.client = APIClient()
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {self.token.token}')

    def test_org_token_denied_for_patient_in_other_org_even_with_group_access(self):
        """Org-A token cannot read org-B patient even if ProfessionalGroupAccess covers them."""
        resp = self.client.get(f'/api/lab-results/summary/?person_id={self.person_in_b.person_id}')
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)

    def test_org_token_allowed_for_patient_in_own_org(self):
        """Org-A token can read org-A patient normally."""
        resp = self.client.get(f'/api/lab-results/summary/?person_id={self.person_in_a.person_id}')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

    def test_org_token_denied_on_trend_endpoint_for_other_org_patient(self):
        """Same isolation applies to the trend endpoint."""
        resp = self.client.get(
            f'/api/lab-results/values/?person_id={self.person_in_b.person_id}&concept_code=718-7'
        )
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)

    def test_org_token_allowed_on_trend_endpoint_for_own_org_patient(self):
        """Org-A token can access the trend endpoint for an org-A patient."""
        resp = self.client.get(
            f'/api/lab-results/values/?person_id={self.person_in_a.person_id}&concept_code=718-7'
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)


# ---------------------------------------------------------------------------
# Sync visit idempotency (#148)
# ---------------------------------------------------------------------------

class SyncVisitIdempotencyTest(TestCase):
    """
    Verify that re-submitting the same report_filename does not create a
    second VisitOccurrence row (hk-labs re-commit after a failed sync).
    """

    def setUp(self):
        _setup_vocab()
        self.user = Identity.objects.create_user(email='idemp@test.com', password='test')
        self.user.is_superuser = True
        self.user.save()
        self.person = Person.objects.create(person_id=19001)
        PatientInfo.objects.create(person=self.person)
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    def _sync(self, filename='report-2026-01.pdf'):
        return self.client.post('/api/lab-results/sync/', {
            'person_id': self.person.person_id,
            'measurements': [
                {
                    'loinc_code': '718-7',
                    'test_name': 'Hemoglobin',
                    'value': '13.5',
                    'unit': 'g/dL',
                    'measured_at': '2026-01-15',
                },
            ],
            'lab_name': 'Test Lab',
            'lab_date': '2026-01-15',
            'report_filename': filename,
            'source_type': 'document_extraction',
        }, format='json')

    def test_resubmit_same_filename_reuses_visit(self):
        """Two syncs with the same report_filename must produce exactly one VisitOccurrence."""
        resp1 = self._sync()
        self.assertEqual(resp1.status_code, status.HTTP_201_CREATED)
        visit_id_first = resp1.data['visit_occurrence_id']

        resp2 = self._sync()
        self.assertEqual(resp2.status_code, status.HTTP_201_CREATED)
        visit_id_second = resp2.data['visit_occurrence_id']

        self.assertEqual(visit_id_first, visit_id_second, 'Re-commit created a second VisitOccurrence')
        self.assertEqual(
            VisitOccurrence.objects.filter(
                person_id=self.person.person_id,
                visit_source_value='report-2026-01.pdf',
            ).count(),
            1,
            'Orphan VisitOccurrence rows found after re-commit',
        )

    def test_different_filename_creates_new_visit(self):
        """Different report_filename values must produce separate VisitOccurrence rows."""
        resp1 = self._sync(filename='report-jan.pdf')
        resp2 = self._sync(filename='report-feb.pdf')
        self.assertEqual(resp1.status_code, status.HTTP_201_CREATED)
        self.assertEqual(resp2.status_code, status.HTTP_201_CREATED)
        self.assertNotEqual(
            resp1.data['visit_occurrence_id'],
            resp2.data['visit_occurrence_id'],
        )


# Provenance dedup on re-commit
# ---------------------------------------------------------------------------

class SyncProvenanceDedupTest(TestCase):
    """
    Verify that re-committing the same report does not create duplicate
    ProvenanceRecord rows for already-existing measurements.
    """

    def setUp(self):
        _setup_vocab()
        self.user = Identity.objects.create_user(email='prov@test.com', password='test')
        self.user.is_superuser = True
        self.user.save()
        self.person = Person.objects.create(person_id=20001)
        PatientInfo.objects.create(person=self.person)
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    def _sync(self):
        return self.client.post('/api/lab-results/sync/', {
            'person_id': self.person.person_id,
            'measurements': [
                {
                    'loinc_code': '718-7',
                    'test_name': 'Hemoglobin',
                    'value': '13.5',
                    'unit': 'g/dL',
                    'measured_at': '2026-01-15',
                },
            ],
            'lab_name': 'Test Lab',
            'lab_date': '2026-01-15',
            'report_filename': 'cbc-2026-01.pdf',
            'source_type': 'document_extraction',
        }, format='json')

    def test_resubmit_does_not_duplicate_provenance(self):
        """Two commits of the same file must produce exactly one ProvenanceRecord per measurement."""
        resp1 = self._sync()
        self.assertEqual(resp1.status_code, status.HTTP_201_CREATED)
        m_id = resp1.data['measurement_ids'][0]

        resp2 = self._sync()
        self.assertEqual(resp2.status_code, status.HTTP_201_CREATED)

        prov_count = ProvenanceRecord.objects.filter(object_id=m_id).count()
        self.assertEqual(prov_count, 1, (
            f'Expected 1 ProvenanceRecord for measurement {m_id} after re-commit, got {prov_count}'
        ))
