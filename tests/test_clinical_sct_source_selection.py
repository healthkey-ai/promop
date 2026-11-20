"""SCT selection identity and native autosave are compatible with historical reads."""
from datetime import date, timedelta

import pytest

from omop_core.models import Measurement, Observation, PatientRecord
from omop_core.services.patient_record_service import refresh_patient_record
from omop_core.services.provenance_service import get_field_provenance
from tests.factories import ConceptFactory, MeasurementFactory, PersonFactory
from tests.test_cb_clinical_input_contract import approve, fact, patient, staff_client

pytestmark = pytest.mark.django_db


@pytest.mark.parametrize('revoked', [False, True])
def test_historical_future_sct_echo_does_not_block_unrelated_native_patch(patient, staff_client, revoked):
    mapping = approve('sct_date')
    fact(patient, 'sct_date', '2099-01-01')
    assert refresh_patient_record(patient).sct_date == date(2099, 1, 1)
    if revoked:
        mapping.status = 'rejected'
        mapping.save()
    before = list(Observation.objects.filter(person=patient).order_by('pk').values())
    url = f'/api/patient-info/{patient.pk}/'
    old = staff_client.get(url).data['patient_info']
    response = staff_client.patch(url, {'sct_date': old['sct_date'], 'planned_therapies': 'New plan'}, format='json')
    assert response.status_code == 200, response.data
    record = PatientRecord.objects.get(person=patient)
    assert record.sct_date == date(2099, 1, 1)
    assert record.planned_therapies == 'New plan'
    assert 'sct_date' not in record.user_edited_fields
    assert list(Observation.objects.filter(person=patient).order_by('pk').values()) == before


@pytest.mark.parametrize('pending', [False, True])
def test_new_or_pending_equal_future_sct_still_rejects_entire_patch(patient, staff_client, pending):
    approve('sct_date')
    fact(patient, 'sct_date', '2099-01-01')
    refresh_patient_record(patient)
    if pending:
        PatientRecord.objects.filter(person=patient).update(user_edited_fields=['sct_date'])
    before = list(Observation.objects.filter(person=patient).values())
    response = staff_client.patch(f'/api/patient-info/{patient.pk}/', {
        'sct_date': '2099-01-01' if pending else '2099-01-02', 'planned_therapies': 'Must rollback',
    }, format='json')
    assert response.status_code == 400, response.data
    record = PatientRecord.objects.get(person=patient)
    assert record.planned_therapies is None and record.sct_date == date(2099, 1, 1)
    assert ('sct_date' in record.user_edited_fields) is pending
    assert list(Observation.objects.filter(person=patient).values()) == before


def test_provenance_selects_older_custom_winner_not_newer_native_fact(patient):
    ConceptFactory(concept_id=32817, concept_code='32817')
    approve('sct_date', source_value='custom:sct-date', concept_id=32817, type_concept_id=32817)
    native = fact(patient, 'sct_date', '2024-01-01')
    custom = fact(patient, 'sct_date', '2020-01-01', observation_concept_id=32817,
                  observation_type_concept_id=32817, observation_source_value='custom:sct-date',
                  observation_date=date.today() - timedelta(days=1))
    assert refresh_patient_record(patient).sct_date == date(2020, 1, 1)
    rows = get_field_provenance(patient, 'sct_date')['source_rows']
    assert {row['id'] for row in rows} == {native.pk, custom.pk}
    assert [(row['table'], row['id']) for row in rows if row['selected']] == [('Observation', custom.pk)]


def custom_fact(person, value, *, table='Observation', source='custom:sct-date', **overrides):
    if table == 'Observation':
        values = dict(observation_concept_id=32817, observation_type_concept_id=32817,
                      observation_source_value=source)
        values.update(overrides)
        return fact(person, 'sct_date', value, **values)
    from omop_core.services.pk import next_pk
    values = dict(
        measurement_id=next_pk(Measurement, 'measurement_id'), person=person,
        measurement_concept_id=32817, measurement_type_concept_id=32817,
        measurement_date=date.today(), measurement_source_value=source,
        value_as_number=None, value_as_string=value,
    )
    values.update(overrides)
    row = MeasurementFactory.build(**values)
    row._skip_patient_record_refresh = True
    row.save(force_insert=True)
    return row


@pytest.mark.parametrize('table', ['Observation', 'Measurement'])
@pytest.mark.parametrize('fallback_source', [False, True])
def test_custom_only_source_and_concept_code_fallback_are_shared(patient, table, fallback_source):
    concept = ConceptFactory(concept_id=32817, concept_code='custom:sct-date')
    approve('sct_date', omop_table=table, source_value='' if fallback_source else 'custom:sct-date',
            concept_id=32817, type_concept_id=32817)
    row = custom_fact(patient, '2020-01-01', table=table,
                      source=concept.concept_code if fallback_source else 'custom:sct-date')
    assert refresh_patient_record(patient).sct_date == date(2020, 1, 1)
    provenance = get_field_provenance(patient, 'sct_date')
    assert [(item['table'], item['id']) for item in provenance['source_rows'] if item['selected']] == [(table, row.pk)]


@pytest.mark.parametrize('table', ['Observation', 'Measurement'])
@pytest.mark.parametrize('clear', [False, True])
def test_bad_latest_custom_does_not_resurrect_older_custom_answer(patient, table, clear):
    ConceptFactory(concept_id=32817, concept_code='32817')
    approve('sct_date', omop_table=table, source_value='custom:sct-date', concept_id=32817, type_concept_id=32817)
    native = fact(patient, 'sct_date', '2024-01-01')
    custom_fact(patient, '2018-01-01', table=table, **{f'{table.lower()}_date': date.today() - timedelta(days=1)})
    bad = custom_fact(patient, None if clear else 'invalid', table=table,
                      **({'value_source_value': 'PatientRecord:cleared'} if clear else {}))
    assert refresh_patient_record(patient).sct_date == date(2024, 1, 1)
    rows = get_field_provenance(patient, 'sct_date')['source_rows']
    assert [(row['table'], row['id']) for row in rows if row['selected']] == [('Observation', native.pk)]
    assert not next(row for row in rows if row['table'] == table and row['id'] == bad.pk)['selected']


@pytest.mark.parametrize('sdk,clear,invalid', [(False, False, False), (True, False, False),
                                            (True, True, False), (True, True, True)])
def test_native_sdk_and_masking_source_identity_survive_generic_clear_filtering(patient, sdk, clear, invalid):
    ConceptFactory(concept_id=32817, concept_code='32817')
    older = fact(patient, 'sct_date', '2020-01-01', observation_concept_id=32817,
                 observation_type_concept_id=32817, observation_date=date.today() - timedelta(days=1))
    newest = fact(patient, 'sct_date', None if clear else '2024-01-01',
                  observation_concept_id=0 if sdk else 32817,
                  observation_type_concept_id=32865 if sdk else 32817,
                  value_source_value='PatientRecord:cleared' if clear else None,
                  unit_source_value='s' if invalid else None)
    assert refresh_patient_record(patient).sct_date == (None if clear else date(2024, 1, 1))
    rows = get_field_provenance(patient, 'sct_date')['source_rows']
    assert {row['id'] for row in rows} == {older.pk, newest.pk}
    assert [row['id'] for row in rows if row['selected']] == [newest.pk]


def test_provenance_tracks_source_not_pending_projection_or_equal_value(patient):
    ConceptFactory(concept_id=32817, concept_code='32817')
    approve('sct_date', omop_table='Measurement', source_value='custom:sct-date', concept_id=32817, type_concept_id=32817)
    native = fact(patient, 'sct_date', '2020-01-01')
    # Same numeric PK and value in different tables are not the same evidence.
    custom = custom_fact(patient, '2020-01-01', table='Measurement', measurement_id=native.pk)
    PatientRecord.objects.filter(person=patient).update(sct_date=date(2010, 1, 1), user_edited_fields=['sct_date'])
    assert refresh_patient_record(patient).sct_date == date(2010, 1, 1)
    provenance = get_field_provenance(patient, 'sct_date')
    assert provenance['current_value'] == '2010-01-01'
    assert [(row['table'], row['id']) for row in provenance['source_rows'] if row['selected']] == [('Measurement', custom.pk)]


@pytest.mark.parametrize('table', ['Observation', 'Measurement'])
def test_exact_mapping_and_patient_boundary_exclude_other_facts(patient, table):
    ConceptFactory(concept_id=32817, concept_code='32817')
    approve('sct_date', omop_table=table, source_value='custom:sct-date', concept_id=32817, type_concept_id=32817)
    selected = custom_fact(patient, '2020-01-01', table=table)
    custom_fact(PersonFactory(), '2021-01-01', table=table)
    custom_fact(patient, '2022-01-01', table=table, **{f'{table.lower()}_concept_id': 0})
    custom_fact(patient, '2023-01-01', table=table, source='unrelated-source')
    custom_fact(patient, '2024-01-01', table=table, is_erroneous=True)
    assert refresh_patient_record(patient).sct_date == date(2020, 1, 1)
    rows = get_field_provenance(patient, 'sct_date')['source_rows']
    assert [(row['table'], row['id']) for row in rows] == [(table, selected.pk)]


def test_display_cap_keeps_old_custom_winner_and_not_twenty_unselected_corrections(patient):
    ConceptFactory(concept_id=32817, concept_code='32817')
    approve('sct_date', source_value='custom:sct-date', concept_id=32817, type_concept_id=32817)
    custom = custom_fact(patient, '2020-01-01', observation_date=date.today() - timedelta(days=400))
    for _ in range(25):
        fact(patient, 'sct_date', '2024-01-01')
    assert refresh_patient_record(patient).sct_date == date(2020, 1, 1)
    rows = get_field_provenance(patient, 'sct_date')['source_rows']
    assert len(rows) == 20
    assert [row['id'] for row in rows if row['selected']] == [custom.pk]


def test_source_selector_is_pure_and_empty_history_has_no_winner(patient, django_assert_num_queries):
    from types import SimpleNamespace
    from omop_core.services.clinical_input_contract import select_sct_source
    snapshot = SimpleNamespace(observations=[], measurements=[], clinical_input_observations=[])
    with django_assert_num_queries(0):
        selected = select_sct_source(snapshot)
    assert selected.has_value is False and selected.selected_key is None and not selected.confirmed
    assert get_field_provenance(patient, 'sct_date')['source_rows'] == []


@pytest.mark.parametrize('table,queries', [('Observation', 2), ('Measurement', 3)])
def test_sct_provenance_has_no_per_row_queries(patient, django_assert_num_queries, table, queries):
    from omop_core.services.provenance_service import _lookup_sct
    ConceptFactory(concept_id=32817, concept_code='32817')
    approve('sct_date', omop_table=table, source_value='custom:sct-date', concept_id=32817, type_concept_id=32817)
    for _ in range(4):
        custom_fact(patient, '2020-01-01', table=table)
    with django_assert_num_queries(queries):
        rows = _lookup_sct(patient)
    assert len(rows) == 4 and sum(row['selected'] for row in rows) == 1
