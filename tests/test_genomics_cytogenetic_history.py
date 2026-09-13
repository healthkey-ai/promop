"""Genomics owns new findings; legacy cytogenetic evidence stays lossless."""
from datetime import date

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from rest_framework.exceptions import ValidationError
from rest_framework.test import APIClient

from omop_core.models import Measurement, Note, Observation, PatientRecord
from omop_core.services.cytogenetic_history import history_page
from omop_core.services.cytogenetics import normalise_cytogenetic_markers
from omop_core.services.genomics import list_variants, save_variant
from omop_core.services.omop_projection import CLEAR_VALUE
from omop_core.services.patient_record_service import refresh_patient_record
from omop_core.services.write_descriptor import build_writable_field_descriptor
from tests.factories import ConceptFactory, MeasurementFactory, ObservationFactory, PersonFactory
from tests.test_genomics_crud import setup  # noqa: F401

pytestmark = pytest.mark.django_db


def client_for(user):
    client = APIClient()
    client.force_authenticate(user)
    return client


def test_original_individual_and_aggregate_text_is_not_reclassified(setup):
    person, record, staff = setup
    raw = '1q21 gain/amplification, del(17p13), complex source prose'
    aggregate = ObservationFactory(person=person, observation_source_value='mm-cytogenetic-markers',
                                   observation_date=date(2018, 4, 3), value_as_string=raw)
    individual = ObservationFactory(person=person, observation_source_value='cytogenetic:1q_amp',
                                    observation_date=date(2019, 1, 2), value_as_string='1q_amp')
    old_measurement = MeasurementFactory(person=person, measurement_source_value='mm-cytogenetic-markers',
                                         measurement_date=date(2017, 1, 2), value_as_string='1q_gain')
    unrelated = ObservationFactory(person=person, value_as_string='Unrelated')
    another = ObservationFactory(observation_source_value='mm-cytogenetic-markers', value_as_string='Other patient')
    before = (Measurement.objects.filter(person=person).count(), Observation.objects.filter(person=person).count())
    response = client_for(staff).get(f'/api/v1/patient-records/{person.pk}/genomics-legacy-cytogenetics/')
    assert response.status_code == 200
    rows = response.data['results']
    assert [row['date'] for row in rows] == ['2019-01-02', '2018-04-03', '2017-01-02']
    assert {row['text'] for row in rows} == {raw, '1q_amp', '1q_gain'}
    assert {row['id'] for row in rows} == {f'observation:{aggregate.pk}', f'observation:{individual.pk}', f'measurement:{old_measurement.pk}'}
    assert all(row['id'] not in (f'observation:{unrelated.pk}', f'observation:{another.pk}') for row in rows)
    assert list_variants(person) == []
    assert before == (Measurement.objects.filter(person=person).count(), Observation.objects.filter(person=person).count())
    assert normalise_cytogenetic_markers('1q21 gain/amplification') == '1q21 gain/amplification'
    record.refresh_from_db()
    assert not record.genomics_gain1q


def test_legacy_clears_and_errors_remain_editing_history_not_negative_findings(setup):
    person, _, _ = setup
    cleared = ObservationFactory(person=person, observation_source_value='cytogenetic:del17p',
                                  value_as_string=None, value_source_value=CLEAR_VALUE)
    bad = ObservationFactory(person=person, observation_source_value='cytogenetic:1q_amp',
                              value_as_string='1q_amp', is_erroneous=True)
    rows = {r['id']: r for r in history_page(person)['results']}
    assert rows[f'observation:{cleared.pk}']['state'] == 'selection_cleared'
    assert rows[f'observation:{bad.pk}']['state'] == 'marked_in_error'
    assert list_variants(person) == []


def test_code_only_and_curator_source_rows_remain_visible_without_current_approval(setup):
    from omop_core.models import FieldConceptMapping
    person, _, _ = setup
    concept = ConceptFactory(vocabulary__vocabulary_id='SNOMED', concept_code='107675007')
    expected = set()
    for source in (None, '', '107675007'):
        row = ObservationFactory(person=person, observation_concept=concept,
            observation_source_value=source, value_as_string='Original aggregate')
        expected.add(f'observation:{row.pk}')
    unrelated = ObservationFactory(person=person, observation_concept=concept,
        observation_source_value='other-clinical-field', value_as_string='Unrelated')
    FieldConceptMapping.objects.update_or_create(field_name='cytogenetic_markers', defaults={
        'omop_table': 'measurement', 'source_value': 'imported-cytogenetic-summary', 'status': 'rejected'})
    row = MeasurementFactory(person=person, measurement_source_value='imported-cytogenetic-summary',
        value_as_string='1q21 amplification')
    expected.add(f'measurement:{row.pk}')
    page = history_page(person)
    assert {row['id'] for row in page['results']} == expected
    assert f'observation:{unrelated.pk}' not in expected


def test_owned_notes_are_batched_and_cross_patient_or_fact_references_stay_raw(setup):
    person, _, _ = setup
    rows = [ObservationFactory(person=person, observation_source_value='mm-cytogenetic-markers') for _ in range(4)]
    for i, row in enumerate(rows):
        note = Note.objects.create(note_id=850000 + i, person=person if i != 2 else PersonFactory(),
            note_date=row.observation_date, note_type_concept_id=0, note_text=f'Original long report {i} ' * 30,
            note_source_value=f'cytogenetics:observation:{row.pk if i != 1 else rows[0].pk}')
        Observation.objects.filter(pk=row.pk).update(value_as_string=f'[note:{note.pk}]')
    Observation.objects.filter(pk=rows[3].pk).update(value_as_string='[note:999999999999999999999999]')
    with CaptureQueriesContext(connection) as queries:
        page = history_page(person)
    assert len(queries) == 5  # mapping + two fact tables + one NOTE batch + legacy cache
    by_id = {r['id']: r for r in page['results']}
    assert by_id[f'observation:{rows[0].pk}']['text'].startswith('Original long report 0')
    for row in rows[1:3]:
        assert by_id[f'observation:{row.pk}']['unresolved_note']
        assert by_id[f'observation:{row.pk}']['text'].startswith('[note:')
    assert by_id[f'observation:{rows[3].pk}']['text'] == '[note:999999999999999999999999]'


def test_cursor_pagination_preserves_same_date_overlapping_table_ids(setup, monkeypatch):
    person, _, _ = setup
    monkeypatch.setattr('omop_core.services.cytogenetic_history.PAGE_SIZE', 2)
    expected = set()
    for i in range(3):
        observation = ObservationFactory(person=person, observation_id=750000+i,
            observation_source_value='cytogenetic:1q_amp', observation_date=date(2020, 2, 2), value_as_string='1q_amp')
        measurement = MeasurementFactory(person=person, measurement_id=750000+i,
            measurement_source_value='mm-cytogenetic-markers', measurement_date=date(2020, 2, 2), value_as_string='1q_gain')
        expected |= {f'observation:{observation.pk}', f'measurement:{measurement.pk}'}
    seen, cursor = [], None
    for _ in range(4):
        page = history_page(person, cursor)
        seen.extend(row['id'] for row in page['results'])
        cursor = page['next_cursor']
        if cursor is None:
            break
    assert len(seen) == len(expected)
    assert set(seen) == expected
    assert cursor is None


@pytest.mark.parametrize('cursor', ['bad', '2020-99-01:observation:1', '2020-01-01:person:1',
                                   '2020-01-01:measurement:-1', '2020-01-01:measurement:99999999999999999999'])
def test_invalid_cursors_fail_without_unbounded_queries(setup, cursor):
    person, _, _ = setup
    with CaptureQueriesContext(connection) as queries, pytest.raises(ValidationError):
        history_page(person, cursor)
    assert len(queries) == 0


@pytest.mark.parametrize('value', ['1q_amp', [], None])
def test_legacy_summary_edits_rejected_but_unchanged_echo_has_no_writes(setup, value):
    person, record, staff = setup
    ObservationFactory(person=person, observation_source_value='mm-cytogenetic-markers', value_as_string='del17p')
    refresh_patient_record(person)
    client = client_for(staff)
    response = client.patch(f'/api/v1/patient-records/{person.pk}/', {'cytogenetic_markers': value}, format='json')
    assert response.status_code in (400, 405), response.data
    assert 'Genomics' in str(response.data)
    before = list(Observation.objects.filter(person=person).values())
    for field in ('cytogenetic_markers', 'cytogenic_markers'):
        response = client.patch(f'/api/v1/patient-records/{person.pk}/', {field: 'del17p'}, format='json')
        assert response.status_code == 200, response.data
    assert list(Observation.objects.filter(person=person).values()) == before
    assert not build_writable_field_descriptor()['cytogenetic_markers']['writable']
    assert list_variants(person) == []


def test_new_genomic_findings_do_not_rewrite_legacy_summary_or_history(setup):
    person, record, _ = setup
    legacy = ObservationFactory(person=person, observation_source_value='mm-cytogenetic-markers', value_as_string='1q21 gain/amplification')
    before = history_page(person)
    save_variant(person, {'gene': '1q21', 'marker_key': 'gain1q', 'variant_name': '1q_gain',
                          'test_date': '2021-05-06', 'status': 'present'})
    assert history_page(person) == before
    legacy.refresh_from_db()
    assert legacy.value_as_string == '1q21 gain/amplification'
    assert not legacy.is_erroneous
    record.refresh_from_db()
    assert record.genomics_gain1q[0]['test_date'] == '2021-05-06'


def test_cache_only_legacy_summary_is_visible_without_fabricating_date_or_fact(setup):
    person, record, _ = setup
    raw = 'Legacy aggregate prose with 1q gain/amplification'
    PatientRecord.objects.filter(pk=record.pk).update(cytogenetic_markers=raw, user_edited_fields=['cytogenetic_markers'])
    page = history_page(person)
    assert page['legacy_summary'] == {'text': raw, 'date': None}
    assert page['results'] == []
    assert not Observation.objects.filter(person=person).exists()
    assert not Measurement.objects.filter(person=person).exists()


def test_history_endpoint_rejects_another_patients_self_session(setup):
    from patient_portal.models import Identity, PatientUser
    person, _, _ = setup
    other_person = PersonFactory()
    user = Identity.objects.create_user(email='history-other@example.test', password='pw')
    PatientUser.objects.create(identity=user, person=other_person)
    response = client_for(user).get(f'/api/v1/patient-records/{person.pk}/genomics-legacy-cytogenetics/')
    assert response.status_code in (403, 404)
    assert 'results' not in response.data


def test_history_endpoint_allows_own_patient_reads_and_rejects_writes(setup):
    from patient_portal.models import Identity, PatientUser
    person, _, _ = setup
    user = Identity.objects.create_user(email='history-self@example.test', password='pw')
    PatientUser.objects.create(identity=user, person=person)
    client = client_for(user)
    url = f'/api/v1/patient-records/{person.pk}/genomics-legacy-cytogenetics/'
    assert client.get(url).status_code == 200
    assert client.post(url, {}, format='json').status_code == 405
    assert APIClient().get(url).status_code in (401, 403)
