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
    Measurement, Person, PatientInfo, Vocabulary, VisitOccurrence,
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
        self.user = Identity.objects.create_user(email='measuser@test.com', password='test')
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
        self.user = Identity.objects.create_user(email='visitdel@test.com', password='test')
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
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('actor_iss and actor_sub required', resp.data['detail'])

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
        self.assertIn('Invalid measured_at date', resp.data['detail'])

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
