"""Tests for PatientSourceCode model and ETL POST endpoint."""
import pytest
from datetime import date

from rest_framework.test import APIClient

from omop_core.models import (
    Concept, Measurement, PatientRecord, PatientSourceCode, Person,
    SourceCodeConceptMapping,
)


@pytest.fixture()
def smart_env(db):
    """Set up person, org, patient record, and staff client."""
    from patient_portal.tests import _make_vocab_fixtures
    from omop_core.models import Organization
    from patient_portal.models import Identity

    _make_vocab_fixtures()

    user = Identity.objects.create_user(
        email='psc_test@test.com', password='pass', is_staff=True,
    )
    client = APIClient()
    client.force_authenticate(user=user)

    org = Organization.objects.create(name='PSC Test Org', slug='psc-test-org')
    person = Person.objects.create(
        person_id=80001,
        given_name='Source', family_name='Codes',
        year_of_birth=1985,
        gender_source_value='female',
        race_source_value='unknown',
        ethnicity_source_value='unknown',
    )
    pr = PatientRecord.objects.create(person=person, organization=org)
    condition_concept = Concept.objects.get(concept_id=4112853)
    type_concept = Concept.objects.get(concept_id=32817)

    return {
        'client': client, 'user': user, 'person': person, 'org': org,
        'patient_record': pr,
        'condition_concept': condition_concept, 'type_concept': type_concept,
    }


def _url(person_id):
    return f'/api/v1/patient-records/{person_id}/source-codes/'


# ---------------------------------------------------------------------------
# POST tests
# ---------------------------------------------------------------------------

class TestPostSourceCodes:
    def test_creates_rows(self, smart_env):
        resp = smart_env['client'].post(_url(smart_env['person'].person_id), {
            'source_codes': [
                {'source_value': 'GLU', 'source_vocabulary_id': 'LOINC',
                 'omop_table': 'measurement', 'occurrence_count': 3},
                {'source_value': 'ASPIRIN 81MG', 'source_vocabulary_id': '',
                 'omop_table': 'drug_exposure', 'occurrence_count': 1},
            ],
        }, format='json')
        assert resp.status_code == 201
        data = resp.json()
        assert data['created'] == 2
        assert data['updated'] == 0

        rows = PatientSourceCode.objects.filter(person=smart_env['person'])
        assert rows.count() == 2
        glu = rows.get(source_value='GLU')
        assert glu.source_vocabulary_id == 'LOINC'
        assert glu.omop_table == 'measurement'
        assert glu.occurrence_count == 3

    def test_upserts_on_second_post(self, smart_env):
        url = _url(smart_env['person'].person_id)
        payload = {
            'source_codes': [
                {'source_value': 'GLU', 'source_vocabulary_id': 'LOINC',
                 'omop_table': 'measurement', 'occurrence_count': 3},
            ],
        }
        smart_env['client'].post(url, payload, format='json')

        payload['source_codes'][0]['occurrence_count'] = 7
        resp = smart_env['client'].post(url, payload, format='json')
        assert resp.status_code == 200
        data = resp.json()
        assert data['created'] == 0
        assert data['updated'] == 1

        row = PatientSourceCode.objects.get(
            person=smart_env['person'], source_value='GLU',
            source_vocabulary_id='LOINC', omop_table='measurement',
        )
        assert row.occurrence_count == 7

    def test_empty_list_rejected(self, smart_env):
        resp = smart_env['client'].post(
            _url(smart_env['person'].person_id),
            {'source_codes': []}, format='json',
        )
        assert resp.status_code == 400

    def test_missing_source_value_rejected(self, smart_env):
        resp = smart_env['client'].post(
            _url(smart_env['person'].person_id),
            {'source_codes': [{'omop_table': 'measurement'}]}, format='json',
        )
        assert resp.status_code == 400
        assert 'source_value' in resp.json()['detail']

    def test_max_limit(self, smart_env):
        entries = [{'source_value': f'CODE_{i}'} for i in range(5001)]
        resp = smart_env['client'].post(
            _url(smart_env['person'].person_id),
            {'source_codes': entries}, format='json',
        )
        assert resp.status_code == 413

    def test_unauthenticated_rejected(self, smart_env):
        anon = APIClient()
        resp = anon.post(
            _url(smart_env['person'].person_id),
            {'source_codes': [{'source_value': 'X'}]}, format='json',
        )
        assert resp.status_code in (401, 403)


# ---------------------------------------------------------------------------
# GET tests
# ---------------------------------------------------------------------------

class TestGetSourceCodes:
    def test_reads_from_patient_source_code_table(self, smart_env):
        person = smart_env['person']
        PatientSourceCode.objects.create(
            person=person, source_value='GLU',
            source_vocabulary_id='LOINC', omop_table='measurement',
            occurrence_count=5,
        )
        SourceCodeConceptMapping.objects.get_or_create(
            source_code='GLU', omop_table='measurement',
            defaults={
                'source_vocabulary_id': 'LOINC', 'status': 'approved',
                'target_concept': smart_env['condition_concept'],
            },
        )

        resp = smart_env['client'].get(_url(person.person_id))
        assert resp.status_code == 200
        data = resp.json()
        glu = next((sc for sc in data['source_codes'] if sc['source_value'] == 'GLU'), None)
        assert glu is not None
        assert glu['row_count'] == 5
        assert glu['mapping_status'] == 'approved'

    def test_fallback_to_clinical_aggregation(self, smart_env):
        """When no PatientSourceCode rows exist, falls back to clinical aggregation."""
        person2 = Person.objects.create(
            person_id=80099, given_name='Fallback', family_name='Patient',
            year_of_birth=1990, gender_source_value='male',
            race_source_value='unknown', ethnicity_source_value='unknown',
        )
        PatientRecord.objects.create(person=person2, organization=smart_env['org'])
        Measurement.objects.create(
            measurement_id=80201, person=person2,
            measurement_concept=smart_env['condition_concept'],
            measurement_date=date(2023, 6, 1),
            measurement_type_concept=smart_env['type_concept'],
            measurement_source_value='CREATININE',
        )

        assert PatientSourceCode.objects.filter(person=person2).count() == 0
        resp = smart_env['client'].get(_url(person2.person_id))
        assert resp.status_code == 200
        data = resp.json()
        assert len(data['source_codes']) > 0
        sv = [sc['source_value'] for sc in data['source_codes']]
        assert 'CREATININE' in sv

    def test_summary_counts(self, smart_env):
        person = smart_env['person']
        PatientSourceCode.objects.create(
            person=person, source_value='MAPPED_CODE',
            source_vocabulary_id='', omop_table='measurement',
            occurrence_count=2,
        )
        PatientSourceCode.objects.create(
            person=person, source_value='UNMAPPED_CODE',
            source_vocabulary_id='', omop_table='measurement',
            occurrence_count=1,
        )
        SourceCodeConceptMapping.objects.get_or_create(
            source_code='MAPPED_CODE', omop_table='measurement',
            defaults={
                'source_vocabulary_id': '', 'status': 'approved',
                'target_concept': smart_env['condition_concept'],
            },
        )

        resp = smart_env['client'].get(_url(person.person_id))
        assert resp.status_code == 200
        summary = resp.json()['summary']
        assert summary['total'] == 2
        assert summary['approved'] >= 1
        assert summary['unmapped'] >= 1
