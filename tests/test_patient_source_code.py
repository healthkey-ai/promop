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

    def test_mapping_fields_come_from_the_lowest_id_mapping(self, smart_env):
        """A code matches one mapping per org; one row is shown, deterministically (#1789)."""
        from omop_core.models import Organization
        person = smart_env['person']
        concept = smart_env['condition_concept']
        PatientSourceCode.objects.create(
            person=person, source_value='HGB', source_vocabulary_id='LOINC',
            omop_table='measurement', occurrence_count=3,
        )
        first = SourceCodeConceptMapping.objects.create(
            source_code='HGB', omop_table='measurement', source_vocabulary_id='LOINC',
            status='approved', target_concept=concept, origin='manual',
            origin_system='HT-One', created_by=smart_env['user'],
            source_code_description='Hemoglobin',
        )
        SourceCodeConceptMapping.objects.create(
            source_code='HGB', omop_table='measurement', source_vocabulary_id='LOINC',
            status='proposed', organization=Organization.objects.create(name='Other', slug='psc-other'),
        )

        rows = [sc for sc in smart_env['client'].get(_url(person.person_id)).json()['source_codes']
                if sc['source_value'] == 'HGB']
        assert len(rows) == 1
        hgb = rows[0]
        assert hgb['mapping_id'] == first.id
        assert hgb['mapping_status'] == 'approved'
        assert hgb['mapping_target_concept_id'] == concept.concept_id
        assert hgb['mapping_target_concept_name'] == concept.concept_name
        assert hgb['mapping_destination_concept_code'] == concept.concept_code
        assert hgb['mapping_destination_vocabulary_id'] == concept.vocabulary_id
        assert hgb['mapping_created_by'] == smart_env['user'].email
        assert hgb['mapping_origin'] == 'manual'
        assert hgb['mapping_origin_system'] == 'HT-One'
        assert hgb['source_code_description'] == 'Hemoglobin'

    def test_query_count_is_flat_in_code_count(self, smart_env):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext
        person = smart_env['person']
        client = smart_env['client']

        def add(n, start):
            for i in range(start, start + n):
                PatientSourceCode.objects.create(
                    person=person, source_value=f'C{i}', omop_table='measurement', occurrence_count=1,
                )
                SourceCodeConceptMapping.objects.create(
                    source_code=f'C{i}', omop_table='measurement', status='approved',
                    target_concept=smart_env['condition_concept'], created_by=smart_env['user'],
                )

        add(2, 0)
        with CaptureQueriesContext(connection) as small:
            assert client.get(_url(person.person_id)).status_code == 200
        add(30, 2)
        with CaptureQueriesContext(connection) as large:
            assert client.get(_url(person.person_id)).status_code == 200
        assert len(large) == len(small)


# ---------------------------------------------------------------------------
# #1778: description default, NUL bytes, oversized bodies
# ---------------------------------------------------------------------------

def _description(smart_env, source_value, omop_table='measurement'):
    resp = smart_env['client'].get(_url(smart_env['person'].person_id))
    assert resp.status_code == 200
    row = next(
        sc for sc in resp.json()['source_codes']
        if sc['source_value'] == source_value and sc['omop_table'] == omop_table
    )
    return row['source_code_description']


class TestSourceDescriptionDefault:
    def _code(self, smart_env, source_value, metadata):
        PatientSourceCode.objects.create(
            person=smart_env['person'], source_value=source_value,
            source_vocabulary_id='LOINC', omop_table='measurement',
            occurrence_count=1, source_metadata=metadata,
        )

    def test_unmapped_code_defaults_to_text(self, smart_env):
        """Epic's 8716-3 displays as "Vital signs"; the text names the measurement."""
        self._code(smart_env, '8716-3',
                   {'display': ['Vital signs'], 'text': ['Blood Pressure']})
        assert _description(smart_env, '8716-3') == 'Blood Pressure'

    def test_falls_back_to_display(self, smart_env):
        self._code(smart_env, 'LC', {'display': ['Lab comment'], 'text': ['  ']})
        assert _description(smart_env, 'LC') == 'Lab comment'

    def test_several_texts_fall_back_to_display(self, smart_env):
        """The mapping is shared: one patient's first vital is not 8716-3's name."""
        self._code(smart_env, '8716-3', {
            'display': ['Vital signs', 'Vital signs'],
            'text': ['Blood Pressure', 'Heart Rate', 'Blood Pressure'],
        })
        assert _description(smart_env, '8716-3') == 'Vital signs'

    def test_repeated_text_counts_once(self, smart_env):
        self._code(smart_env, 'BP', {'text': ['Blood Pressure', ' Blood Pressure ']})
        assert _description(smart_env, 'BP') == 'Blood Pressure'

    def test_ambiguous_everywhere_is_empty(self, smart_env):
        self._code(smart_env, 'MIX', {'text': ['A', 'B'], 'display': ['C', 'D']})
        assert _description(smart_env, 'MIX') == ''

    def test_empty_without_metadata(self, smart_env):
        self._code(smart_env, '301070', {})
        assert _description(smart_env, '301070') == ''

    def test_malformed_metadata_is_ignored(self, smart_env):
        self._code(smart_env, 'ODD', {'text': [None, 7], 'display': {'x': 1}})
        assert _description(smart_env, 'ODD') == ''

    def test_truncated_to_the_mapping_column(self, smart_env):
        self._code(smart_env, 'LONG', {'text': ['x' * 400]})
        assert _description(smart_env, 'LONG') == 'x' * 255

    def test_mapping_description_wins(self, smart_env):
        self._code(smart_env, 'GLU', {'text': ['Glucose [Mass/volume]']})
        SourceCodeConceptMapping.objects.create(
            source_code='GLU', omop_table='measurement', source_vocabulary_id='LOINC',
            status='proposed', source_code_description='Curated glucose',
        )
        assert _description(smart_env, 'GLU') == 'Curated glucose'

    def test_blank_mapping_description_falls_back(self, smart_env):
        """A queue row created at ingest has no description; the source's name
        is the better starting value, and the dialog would otherwise save ''."""
        self._code(smart_env, 'GLU', {'text': ['Glucose [Mass/volume]']})
        SourceCodeConceptMapping.objects.create(
            source_code='GLU', omop_table='measurement', source_vocabulary_id='LOINC',
            status='proposed', source_code_description='',
        )
        assert _description(smart_env, 'GLU') == 'Glucose [Mass/volume]'


class TestPostSourceCodesRejections:
    def test_nul_in_metadata_is_a_400(self, smart_env):
        resp = smart_env['client'].post(_url(smart_env['person'].person_id), {
            'source_codes': [
                {'source_value': 'OK'},
                {'source_value': 'NOTE', 'source_metadata': {'text': ['Cerner note\x00']}},
            ],
        }, format='json')
        assert resp.status_code == 400
        detail = resp.json()['detail']
        assert detail.startswith('Entry 1:')
        assert 'source_metadata' in detail
        assert not PatientSourceCode.objects.filter(person=smart_env['person']).exists()

    def test_nul_in_metadata_key_is_a_400(self, smart_env):
        resp = smart_env['client'].post(_url(smart_env['person'].person_id), {
            'source_codes': [{'source_value': 'X', 'source_metadata': {'a\x00': 'b'}}],
        }, format='json')
        assert resp.status_code == 400

    def test_nul_in_source_value_is_a_400(self, smart_env):
        resp = smart_env['client'].post(_url(smart_env['person'].person_id), {
            'source_codes': [{'source_value': 'A\x00B', 'source_unit': 'mg\x00'}],
        }, format='json')
        assert resp.status_code == 400
        assert 'source_value, source_unit' in resp.json()['detail']

    def test_body_over_upload_limit_is_a_json_413(self, smart_env, settings):
        settings.DATA_UPLOAD_MAX_MEMORY_SIZE = 10_000
        entries = [
            {'source_value': f'C{i}', 'source_metadata': {'resource': 'x' * 500}}
            for i in range(50)
        ]
        resp = smart_env['client'].post(
            _url(smart_env['person'].person_id), {'source_codes': entries}, format='json',
        )
        assert resp.status_code == 413
        assert resp['Content-Type'].startswith('application/json')
        body = resp.json()
        assert body['max_bytes'] == 10_000
        assert '10000-byte' in body['detail']
