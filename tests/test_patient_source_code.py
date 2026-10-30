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


@pytest.mark.parametrize('mapped', [False, True])
def test_patient_source_context_keeps_quantity_and_based_on_from_the_patient(smart_env, mapped):
    PatientSourceCode.objects.create(
        person=smart_env['person'], source_value='QTY', omop_table='measurement',
        source_unit='mg/dL', example_quantity='12', example_quantity_override='0',
        source_metadata={'fhir': {'basedOn': [
            {'display': 'Blood test', 'reference': 'ServiceRequest/123'},
            {'reference': 'ServiceRequest/456'},
        ]}},
    )
    PatientSourceCode.objects.create(person=smart_env['person'], source_value='EMPTY', omop_table='measurement')
    if mapped:
        for code in ('QTY', 'EMPTY'):
            SourceCodeConceptMapping.objects.create(
                source_code=code, omop_table='measurement', origin='curator',
                source_metadata={'fhir': {'basedOn': [{'display': 'Another patient order'}]}},
            )

    response = smart_env['client'].get(_url(smart_env['person'].person_id))
    assert response.status_code == 200
    rows = {r['source_value']: r for r in response.data['source_codes']}
    assert rows['QTY']['example_quantity'] == '0'
    assert rows['QTY']['source_unit'] == 'mg/dL'
    assert rows['QTY']['based_on'] == ['Blood test', 'ServiceRequest/456']
    assert rows['EMPTY']['based_on'] == []


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

    def test_the_upload_sets_the_codes_seen_count_from_its_rows(self, smart_env):
        """A lookup's bump was provisional; the patient rows are the count (#2123)."""
        from omop_core.models import SourceCodeConceptMapping
        mapping = SourceCodeConceptMapping.objects.create(
            source_vocabulary_id='LOINC', source_code='GLU', omop_table='measurement', occurrence_count=7)
        other = SourceCodeConceptMapping.objects.create(
            source_vocabulary_id='LOINC', source_code='HGB', omop_table='measurement', occurrence_count=7)
        resp = smart_env['client'].post(_url(smart_env['person'].person_id), {
            'source_codes': [
                {'source_value': 'GLU', 'source_vocabulary_id': 'LOINC',
                 'omop_table': 'measurement', 'occurrence_count': 3},
            ],
        }, format='json')
        assert resp.status_code == 201
        mapping.refresh_from_db(); other.refresh_from_db()
        assert (mapping.occurrence_count, other.occurrence_count) == (3, 7)

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


# ---------------------------------------------------------------------------
# etl#42: the source vocabulary is part of the mapping key
# ---------------------------------------------------------------------------

_SITE_A = 'urn:oid:1.2.840.114350.1.13.159.2.7.2.696570'
_SITE_B = 'urn:oid:1.2.840.114350.1.13.129.2.7.2.696570'


def _row(smart_env, source_value, omop_table='measurement'):
    resp = smart_env['client'].get(_url(smart_env['person'].person_id))
    assert resp.status_code == 200
    return next(
        sc for sc in resp.json()['source_codes']
        if sc['source_value'] == source_value and sc['omop_table'] == omop_table
    )


class TestDescriptionIsScopedToTheSourceVocabulary:
    """Epic codes are local to one hospital, so code 6 is not one code."""

    def _code(self, smart_env, vocabulary, metadata=None, source_value='6'):
        PatientSourceCode.objects.create(
            person=smart_env['person'], source_value=source_value,
            source_vocabulary_id=vocabulary, omop_table='measurement',
            occurrence_count=1, source_metadata=metadata or {},
        )

    def _mapping(self, vocabulary, description, source_value='6'):
        return SourceCodeConceptMapping.objects.create(
            source_code=source_value, omop_table='measurement',
            source_vocabulary_id=vocabulary, status='proposed',
            source_code_description=description,
        )

    def test_another_sites_label_is_not_used(self, smart_env):
        first = self._mapping(_SITE_A, 'erythema')
        mine = self._mapping(_SITE_B, 'temperature')
        assert first.id < mine.id
        self._code(smart_env, _SITE_B)

        row = _row(smart_env, '6')
        assert row['source_code_description'] == 'temperature'
        assert row['mapping_id'] == mine.id
        assert row['source_vocabulary_id'] == _SITE_B

    def test_loinc_row_does_not_wear_an_epic_label(self, smart_env):
        self._mapping(_SITE_A, 'rainbow tube', source_value='785-6')
        self._code(smart_env, 'LOINC', {'text': ['MCH']}, source_value='785-6')

        row = _row(smart_env, '785-6')
        assert row['source_code_description'] == 'MCH'
        assert row['mapping_id'] is None
        assert row['mapping_status'] == 'unmapped'
        assert row['source_vocabulary_id'] == 'LOINC'

    def test_no_mapping_for_this_vocabulary_keeps_the_rows_own_identity(self, smart_env):
        self._mapping(_SITE_A, 'erythema')
        self._code(smart_env, _SITE_B, {'text': ['Temp']})

        row = _row(smart_env, '6')
        assert row['source_code_description'] == 'Temp'
        assert row['mapping_id'] is None
        assert row['source_vocabulary_id'] == _SITE_B

    def test_own_vocabulary_still_matches(self, smart_env):
        mine = self._mapping(_SITE_A, 'erythema')
        self._code(smart_env, _SITE_A, {'text': ['Temp']})

        row = _row(smart_env, '6')
        assert row['mapping_id'] == mine.id
        assert row['source_code_description'] == 'erythema'

    def test_oid_alias_matches_the_canonical_mapping(self, smart_env):
        """Ingest stores SNOMED; the ETL may post the OID for the same system."""
        mine = self._mapping('SNOMED', 'Fever', source_value='386661006')
        self._code(smart_env, 'urn:oid:2.16.840.1.113883.6.96',
                   source_value='386661006')

        row = _row(smart_env, '386661006')
        assert row['mapping_id'] == mine.id
        assert row['source_code_description'] == 'Fever'

    def test_uncoded_rows_match_only_uncoded_mappings(self, smart_env):
        self._mapping(_SITE_A, 'erythema', source_value='M-Protein')
        mine = self._mapping('', 'M-Protein, Serum', source_value='M-Protein')
        self._code(smart_env, '', source_value='M-Protein')

        row = _row(smart_env, 'M-Protein')
        assert row['mapping_id'] == mine.id
        assert row['source_code_description'] == 'M-Protein, Serum'

    def test_clinical_fallback_still_matches_on_the_code_alone(self, smart_env):
        """Those rows carry no vocabulary at all, so the code is all there is."""
        from omop_core.models import Measurement

        person = smart_env['person']
        mine = self._mapping(_SITE_A, 'erythema')
        Measurement.objects.create(
            measurement_id=9900001, person=person,
            measurement_concept_id=0,
            measurement_date=date(2026, 1, 1),
            measurement_type_concept=smart_env['type_concept'],
            measurement_source_value='6',
        )
        assert not PatientSourceCode.objects.filter(person=person).exists()

        row = _row(smart_env, '6')
        assert row['mapping_id'] == mine.id
        assert row['source_code_description'] == 'erythema'


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


# ---------------------------------------------------------------------------
# Chunked uploads: upload_id + final (#1824)
# ---------------------------------------------------------------------------

def _entry(source_value, **kwargs):
    entry = {
        'source_value': source_value,
        'source_vocabulary_id': 'LOINC',
        'omop_table': 'measurement',
    }
    entry.update(kwargs)
    return entry


def _age_open_uploads(person, hours=25):
    """Backdate open runs so the staleness cut-off treats them as abandoned."""
    from datetime import timedelta
    from django.utils import timezone
    from omop_core.models import PatientSourceCodeUpload

    PatientSourceCodeUpload.objects.filter(
        person=person, finalized_at__isnull=True,
    ).update(last_chunk_at=timezone.now() - timedelta(hours=hours))


def _codes(person):
    return set(
        PatientSourceCode.objects
        .filter(person=person)
        .values_list('source_value', flat=True)
    )


class TestChunkedUpload:
    def test_final_prunes_rows_from_earlier_runs(self, smart_env):
        """The bug in #1824: chunk 2's rows outlived every later import."""
        url, person = _url(smart_env['person'].person_id), smart_env['person']
        client = smart_env['client']

        client.post(url, {'source_codes': [_entry('A'), _entry('B')],
                          'upload_id': 'run-1'}, format='json')
        client.post(url, {'source_codes': [_entry('C')],
                          'upload_id': 'run-1', 'final': True}, format='json')
        assert _codes(person) == {'A', 'B', 'C'}

        # Second run drops B and C, adds D, in two chunks.
        client.post(url, {'source_codes': [_entry('A')],
                          'upload_id': 'run-2'}, format='json')
        resp = client.post(url, {'source_codes': [_entry('D')],
                                 'upload_id': 'run-2', 'final': True},
                           format='json')
        assert resp.status_code == 201
        assert resp.json()['deleted'] == 2
        assert _codes(person) == {'A', 'D'}

    def test_non_final_chunk_prunes_nothing(self, smart_env):
        url, person = _url(smart_env['person'].person_id), smart_env['person']
        PatientSourceCode.objects.create(
            person=person, source_value='STALE',
            source_vocabulary_id='LOINC', omop_table='measurement',
        )
        resp = smart_env['client'].post(
            url, {'source_codes': [_entry('A')], 'upload_id': 'run-1'},
            format='json',
        )
        assert resp.status_code == 201
        assert 'deleted' not in resp.json()
        assert _codes(person) == {'STALE', 'A'}

    def test_abandoned_run_leaves_the_set_alone(self, smart_env):
        """A run that dies before final is stale, never truncated."""
        url, person = _url(smart_env['person'].person_id), smart_env['person']
        client = smart_env['client']

        client.post(url, {'source_codes': [_entry('A'), _entry('B')],
                          'upload_id': 'run-1', 'final': True}, format='json')
        client.post(url, {'source_codes': [_entry('A')],
                          'upload_id': 'run-2'}, format='json')
        assert _codes(person) == {'A', 'B'}

        # run-2 is still open, so the next run has to wait for it to go stale.
        _age_open_uploads(person)
        client.post(url, {'source_codes': [_entry('A')],
                          'upload_id': 'run-3', 'final': True}, format='json')
        assert _codes(person) == {'A'}

    def test_retried_chunk_is_idempotent(self, smart_env):
        url, person = _url(smart_env['person'].person_id), smart_env['person']
        client = smart_env['client']
        chunk = {'source_codes': [_entry('A')], 'upload_id': 'run-1'}

        client.post(url, chunk, format='json')
        resp = client.post(url, chunk, format='json')
        assert resp.json() == {'created': 0, 'updated': 1}

        client.post(url, {'source_codes': [_entry('B')],
                          'upload_id': 'run-1', 'final': True}, format='json')
        assert _codes(person) == {'A', 'B'}

    def test_final_retags_a_row_an_earlier_run_wrote(self, smart_env):
        """A code present in both runs survives on the new token."""
        url, person = _url(smart_env['person'].person_id), smart_env['person']
        client = smart_env['client']

        client.post(url, {'source_codes': [_entry('A')],
                          'upload_id': 'run-1', 'final': True}, format='json')
        client.post(url, {'source_codes': [_entry('A')],
                          'upload_id': 'run-2', 'final': True}, format='json')

        row = PatientSourceCode.objects.get(person=person, source_value='A')
        assert row.upload_id == 'run-2'
        assert _codes(person) == {'A'}

    def test_final_prunes_untagged_rows_from_before_the_feature(self, smart_env):
        url, person = _url(smart_env['person'].person_id), smart_env['person']
        PatientSourceCode.objects.create(
            person=person, source_value='LEGACY',
            source_vocabulary_id='LOINC', omop_table='measurement',
        )
        resp = smart_env['client'].post(
            url, {'source_codes': [_entry('A')], 'upload_id': 'run-1',
                  'final': True}, format='json',
        )
        assert resp.json()['deleted'] == 1
        assert _codes(person) == {'A'}

    def test_other_patients_are_untouched(self, smart_env):
        other = Person.objects.create(
            person_id=80002, year_of_birth=1970,
            gender_source_value='male', race_source_value='unknown',
            ethnicity_source_value='unknown',
        )
        PatientSourceCode.objects.create(
            person=other, source_value='THEIRS',
            source_vocabulary_id='LOINC', omop_table='measurement',
        )
        smart_env['client'].post(
            _url(smart_env['person'].person_id),
            {'source_codes': [_entry('A')], 'upload_id': 'run-1',
             'final': True}, format='json',
        )
        assert _codes(other) == {'THEIRS'}

    def test_replace_still_prunes_against_the_payload(self, smart_env):
        """The single-request form keeps its meaning."""
        url, person = _url(smart_env['person'].person_id), smart_env['person']
        client = smart_env['client']

        client.post(url, {'source_codes': [_entry('A'), _entry('B')]},
                    format='json')
        resp = client.post(url, {'source_codes': [_entry('A')],
                                 'replace': True}, format='json')
        assert resp.json()['deleted'] == 1
        assert _codes(person) == {'A'}

    def test_replace_with_upload_id_is_a_400(self, smart_env):
        resp = smart_env['client'].post(
            _url(smart_env['person'].person_id),
            {'source_codes': [_entry('A')], 'replace': True,
             'upload_id': 'run-1'}, format='json',
        )
        assert resp.status_code == 400
        assert 'cannot be combined' in resp.json()['detail']
        assert not _codes(smart_env['person'])

    def test_final_without_upload_id_is_a_400(self, smart_env):
        resp = smart_env['client'].post(
            _url(smart_env['person'].person_id),
            {'source_codes': [_entry('A')], 'final': True}, format='json',
        )
        assert resp.status_code == 400
        assert 'final requires an upload_id' in resp.json()['detail']
        assert not _codes(smart_env['person'])

    @pytest.mark.parametrize('bad', ['', '   ', 17, True, ['run-1'], {}])
    def test_malformed_upload_id_is_a_400(self, smart_env, bad):
        resp = smart_env['client'].post(
            _url(smart_env['person'].person_id),
            {'source_codes': [_entry('A')], 'upload_id': bad}, format='json',
        )
        assert resp.status_code == 400
        assert not _codes(smart_env['person'])

    def test_overlong_upload_id_is_a_400(self, smart_env):
        resp = smart_env['client'].post(
            _url(smart_env['person'].person_id),
            {'source_codes': [_entry('A')], 'upload_id': 'x' * 65},
            format='json',
        )
        assert resp.status_code == 400
        assert '64 characters' in resp.json()['detail']
        assert not _codes(smart_env['person'])

    def test_upload_id_is_stripped_and_stored(self, smart_env):
        smart_env['client'].post(
            _url(smart_env['person'].person_id),
            {'source_codes': [_entry('A')], 'upload_id': '  run-1  '},
            format='json',
        )
        row = PatientSourceCode.objects.get(
            person=smart_env['person'], source_value='A',
        )
        assert row.upload_id == 'run-1'

    def test_untagged_post_leaves_the_token_empty(self, smart_env):
        smart_env['client'].post(
            _url(smart_env['person'].person_id),
            {'source_codes': [_entry('A')]}, format='json',
        )
        row = PatientSourceCode.objects.get(
            person=smart_env['person'], source_value='A',
        )
        assert row.upload_id == ''

    def test_chunked_set_over_the_cap_prunes_exactly(self, smart_env):
        """The reported shape: a set that does not fit in one request."""
        url, person = _url(smart_env['person'].person_id), smart_env['person']
        client = smart_env['client']
        # Shrinking the cap tests the same code path as 5,000 would.
        from patient_portal.api.views import PatientRecordV1ViewSet
        original = PatientRecordV1ViewSet._SOURCE_CODES_MAX
        PatientRecordV1ViewSet._SOURCE_CODES_MAX = 10
        try:
            first = [_entry(f'C{i}') for i in range(10)]
            second = [_entry(f'C{i}') for i in range(10, 15)]
            client.post(url, {'source_codes': first, 'upload_id': 'run-1'},
                        format='json')
            client.post(url, {'source_codes': second, 'upload_id': 'run-1',
                              'final': True}, format='json')
            assert len(_codes(person)) == 15

            # Re-import: 12 codes, overlapping but not identical.
            first = [_entry(f'C{i}') for i in range(3, 13)]
            second = [_entry(f'C{i}') for i in range(13, 15)]
            client.post(url, {'source_codes': first, 'upload_id': 'run-2'},
                        format='json')
            resp = client.post(url, {'source_codes': second,
                                     'upload_id': 'run-2', 'final': True},
                               format='json')
            assert resp.json()['deleted'] == 3
            assert _codes(person) == {f'C{i}' for i in range(3, 15)}
        finally:
            PatientRecordV1ViewSet._SOURCE_CODES_MAX = original


class TestOverlappingUploads:
    """One patient takes one run at a time (#1824 review)."""

    def test_second_run_is_refused_while_the_first_is_open(self, smart_env):
        url, person = _url(smart_env['person'].person_id), smart_env['person']
        client = smart_env['client']

        client.post(url, {'source_codes': [_entry('X')],
                          'upload_id': 'run-a'}, format='json')
        resp = client.post(url, {'source_codes': [_entry('X')],
                                 'upload_id': 'run-b'}, format='json')
        assert resp.status_code == 409
        assert resp.json()['open_upload_id'] == 'run-a'
        assert _codes(person) == {'X'}

    def test_interleaved_runs_cannot_truncate_the_patient(self, smart_env):
        """Run B retagging X would make A's final chunk delete A's own code."""
        url, person = _url(smart_env['person'].person_id), smart_env['person']
        client = smart_env['client']

        client.post(url, {'source_codes': [_entry('X')],
                          'upload_id': 'run-a'}, format='json')
        # Everything run B tries is refused while A is open.
        for body in (
            {'source_codes': [_entry('X')], 'upload_id': 'run-b'},
            {'source_codes': [_entry('Z')], 'upload_id': 'run-b', 'final': True},
        ):
            assert client.post(url, body, format='json').status_code == 409

        resp = client.post(url, {'source_codes': [_entry('Y')],
                                 'upload_id': 'run-a', 'final': True},
                           format='json')
        assert resp.status_code == 201
        assert _codes(person) == {'X', 'Y'}

    def test_a_finalized_token_cannot_be_reused(self, smart_env):
        url, person = _url(smart_env['person'].person_id), smart_env['person']
        client = smart_env['client']

        client.post(url, {'source_codes': [_entry('A')],
                          'upload_id': 'run-1', 'final': True}, format='json')
        resp = client.post(url, {'source_codes': [_entry('B')],
                                 'upload_id': 'run-1'}, format='json')
        assert resp.status_code == 409
        assert 'already been finalized' in resp.json()['detail']
        assert _codes(person) == {'A'}

    def test_a_stale_run_is_superseded_and_then_refused(self, smart_env):
        url, person = _url(smart_env['person'].person_id), smart_env['person']
        client = smart_env['client']

        client.post(url, {'source_codes': [_entry('A')],
                          'upload_id': 'run-a'}, format='json')
        _age_open_uploads(person)

        assert client.post(url, {'source_codes': [_entry('B')],
                                 'upload_id': 'run-b'},
                           format='json').status_code == 201
        # run-a was closed as abandoned, so its late chunk is not a retry:
        # first run-b holds the patient, and once run-b is done the token
        # itself is spent.
        resp = client.post(url, {'source_codes': [_entry('C')],
                                 'upload_id': 'run-a'}, format='json')
        assert resp.status_code == 409
        assert resp.json()['open_upload_id'] == 'run-b'

        client.post(url, {'source_codes': [_entry('B')],
                          'upload_id': 'run-b', 'final': True}, format='json')
        resp = client.post(url, {'source_codes': [_entry('C')],
                                 'upload_id': 'run-a'}, format='json')
        assert resp.status_code == 409
        assert 'already been finalized' in resp.json()['detail']

    def test_runs_on_different_patients_do_not_block_each_other(self, smart_env):
        other = Person.objects.create(
            person_id=80003, year_of_birth=1960,
            gender_source_value='male', race_source_value='unknown',
            ethnicity_source_value='unknown',
        )
        PatientRecord.objects.create(person=other, organization=smart_env['org'])
        client = smart_env['client']
        client.post(_url(smart_env['person'].person_id),
                    {'source_codes': [_entry('A')], 'upload_id': 'run-a'},
                    format='json')
        resp = client.post(_url(other.person_id),
                           {'source_codes': [_entry('A')], 'upload_id': 'run-b'},
                           format='json')
        assert resp.status_code == 201

    def test_ledger_records_the_run(self, smart_env):
        from omop_core.models import PatientSourceCodeUpload

        url, person = _url(smart_env['person'].person_id), smart_env['person']
        client = smart_env['client']

        client.post(url, {'source_codes': [_entry('A')],
                          'upload_id': 'run-1'}, format='json')
        run = PatientSourceCodeUpload.objects.get(person=person,
                                                  upload_id='run-1')
        assert run.finalized_at is None

        client.post(url, {'source_codes': [_entry('B')],
                          'upload_id': 'run-1', 'final': True}, format='json')
        run.refresh_from_db()
        assert run.finalized_at is not None

    def test_untagged_posts_keep_no_ledger_row(self, smart_env):
        from omop_core.models import PatientSourceCodeUpload

        smart_env['client'].post(
            _url(smart_env['person'].person_id),
            {'source_codes': [_entry('A')], 'replace': True}, format='json',
        )
        assert not PatientSourceCodeUpload.objects.exists()

    def test_nul_in_upload_id_is_a_400(self, smart_env):
        resp = smart_env['client'].post(
            _url(smart_env['person'].person_id),
            {'source_codes': [_entry('A')], 'upload_id': 'run\x001'},
            format='json',
        )
        assert resp.status_code == 400
        assert 'NUL' in resp.json()['detail']
        assert not _codes(smart_env['person'])
