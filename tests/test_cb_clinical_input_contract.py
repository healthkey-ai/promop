"""Fail-closed source contract; approved mappings here are synthetic fixtures."""
from datetime import date, timedelta
from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError
from django.db import transaction

from omop_core.models import Concept, FieldConceptMapping, Observation, PatientRecord
from omop_core.services.clinical_input_contract import (
    CLINICAL_INPUT_CONTRACT_VERSION, RECIPES, capabilities, write_clinical_inputs,
)
from omop_core.services.patient_record_service import refresh_patient_record
from tests.factories import (
    ConceptFactory, ObservationFactory, PatientRecordFactory, PersonFactory, VocabularyFactory,
)

pytestmark = pytest.mark.django_db
RATIO = 'prothrombin_time_uln'
BOOL = 'able_to_swallow_tablets_capsules'


@pytest.fixture
def patient():
    person = PersonFactory()
    PatientRecordFactory(person=person)
    ConceptFactory(concept_id=0, concept_code='0', concept_name='No matching concept')
    vocab = VocabularyFactory(vocabulary_id='Type Concept')
    ConceptFactory(concept_id=32865, concept_code='32865', vocabulary=vocab)
    return person


def approve(field, **overrides):
    source, kind, unit = RECIPES[field]
    values = dict(status='approved', concept_id=0, omop_table='Observation',
                  source_value=source, value_kind=kind, multiple=False,
                  type_concept_id=32865, unit=unit or '')
    values.update(overrides)
    return FieldConceptMapping.objects.create(field_name=field, **values)


def fact(person, field, value=None, **overrides):
    from omop_core.services.pk import next_pk
    source, kind, unit = RECIPES[field]
    values = dict(person=person, observation_concept_id=0,
                  observation_type_concept_id=32865, observation_date=date.today(),
                  observation_source_value=source, unit_source_value=unit,
                  value_as_number=value if kind == 'number' else None,
                  value_as_string=value if kind != 'number' else None)
    values.update(overrides)
    explicit, alias = values.get('observation_id'), values.pop('pk', None)
    if explicit is not None and alias is not None and explicit != alias:
        raise ValueError('Conflicting primary keys in fact fixture.')
    if explicit is None:
        values['observation_id'] = alias if alias is not None else next_pk(Observation, 'observation_id')
    row = ObservationFactory.build(**values)
    row._skip_patient_record_refresh = True
    row.save(force_insert=True)
    return row


@pytest.mark.parametrize('field', list(RECIPES))
def test_unapproved_inputs_fail_closed(patient, field):
    assert CLINICAL_INPUT_CONTRACT_VERSION == 2
    assert capabilities()[field]['writable'] is False
    with pytest.raises(ValidationError):
        write_clinical_inputs(patient, {field: None})
    assert not Observation.objects.filter(person=patient).exists()


@pytest.mark.parametrize('override', [
    {'status': 'proposed'}, {'status': 'rejected'}, {'omop_table': 'Measurement'},
    {'concept_id': None}, {'source_value': '6301-6'}, {'value_kind': 'boolean'},
    {'multiple': True}, {'type_concept_id': None}, {'unit': 's'},
    {'value_vocabulary': 'SctEligibility'},
])
def test_mapping_must_match_entire_recipe(patient, override):
    approve(RATIO, **override)
    assert not capabilities()[RATIO]['writable']
    with pytest.raises(ValidationError):
        write_clinical_inputs(patient, {RATIO: '1.2'})


def test_three_same_day_concept_zero_fields_do_not_collide(patient):
    values = {RATIO: Decimal('1.2345'), 'activated_partial_thromboplastin_time_uln': Decimal('2.5'), BOOL: False}
    for field in values:
        approve(field)
    record = write_clinical_inputs(patient, values)
    assert all(getattr(record, field) == value for field, value in values.items())
    assert Observation.objects.filter(person=patient).count() == 3
    assert not record.user_edited_fields
    record = refresh_patient_record(patient)
    assert all(getattr(record, field) == value for field, value in values.items())


@pytest.mark.parametrize('value', ['0', '-1', 'NaN', 'Infinity', True, '1.234567', '100000000'])
def test_invalid_batch_has_no_partial_facts(patient, value):
    approve(RATIO)
    approve(BOOL)
    with pytest.raises((ValidationError, ValueError)):
        write_clinical_inputs(patient, {BOOL: False, RATIO: value})
    assert not Observation.objects.filter(person=patient).exists()


@pytest.mark.parametrize('field,value', [(RATIO, '1.5'), (BOOL, False), ('sct_date', '2024-01-15')])
def test_corrections_and_clears_append_and_preserve_imports(patient, field, value):
    approve(field)
    original = fact(patient, field, 'true' if field == BOOL else value,
                    observation_date=date.today() - timedelta(days=1))
    original_values = Observation.objects.filter(pk=original.pk).values().get()
    write_clinical_inputs(patient, {field: value})
    record = write_clinical_inputs(patient, {field: None})
    assert getattr(record, field) is None
    assert getattr(refresh_patient_record(patient), field) is None
    assert Observation.objects.filter(person=patient).count() == 3
    assert Observation.objects.filter(pk=original.pk).values().get() == original_values


@pytest.mark.parametrize('bad', [
    {'unit_source_value': 's'}, {'value_as_number': -1},
    {'value_as_number': None}, {'value_source_value': 'PatientRecord:cleared'},
])
def test_latest_invalid_fact_does_not_resurrect_older_answer(patient, bad):
    fact(patient, RATIO, 1.2, observation_date=date.today() - timedelta(days=1))
    fact(patient, RATIO, 2, **bad)
    assert getattr(refresh_patient_record(patient), RATIO) is None


def test_mapping_revocation_does_not_disable_historical_reader(patient):
    mapping = approve(RATIO)
    write_clinical_inputs(patient, {RATIO: '1.2'})
    mapping.status = 'rejected'
    mapping.save()
    assert not capabilities()[RATIO]['writable']
    assert getattr(refresh_patient_record(patient), RATIO) == Decimal('1.2')


def test_future_fact_rolls_back_write(patient):
    approve(RATIO)
    fact(patient, RATIO, 2, observation_date=date.today() + timedelta(days=1))
    with pytest.raises(ValidationError):
        write_clinical_inputs(patient, {RATIO: '1.2'})
    assert Observation.objects.filter(person=patient).count() == 1


@pytest.mark.parametrize('future_kind', ['tomorrow', 'far_future'])
@pytest.mark.parametrize('clear_first', [False, True])
def test_sdk_rejects_future_sct_date_without_partial_batch_writes(patient, future_kind, clear_first):
    from django.utils.timezone import localdate
    future = localdate() + timedelta(days=1) if future_kind == 'tomorrow' else date(2099, 1, 1)
    approve('sct_date')
    approve(BOOL)
    if clear_first:
        write_clinical_inputs(patient, {BOOL: True})
    with pytest.raises(ValidationError, match='SCT date cannot be in the future'):
        write_clinical_inputs(patient, {BOOL: None if clear_first else False, 'sct_date': future})
    assert Observation.objects.filter(person=patient).count() == int(clear_first)
    record = PatientRecord.objects.get(person=patient)
    assert record.sct_date is None
    assert getattr(record, BOOL) is (True if clear_first else None)


def test_historical_future_sct_read_is_not_changed_by_write_date_validation(patient):
    approve('sct_date')
    fact(patient, 'sct_date', '2099-01-01')
    assert refresh_patient_record(patient).sct_date == date(2099, 1, 1)
    with pytest.raises(ValidationError, match='SCT date cannot be in the future'):
        write_clinical_inputs(patient, {'sct_date': date(2099, 1, 1)})
    assert Observation.objects.filter(person=patient).count() == 1


@pytest.mark.parametrize('raw', ['not-a-date', '2099-01-01 trailing text'])
def test_read_date_policy_bypass_still_rejects_invalid_date_syntax(patient, raw):
    fact(patient, 'sct_date', raw)
    assert refresh_patient_record(patient).sct_date is None


@pytest.mark.parametrize('days_ago', [0, 1, None])
def test_sdk_accepts_current_past_and_cleared_sct_dates(patient, days_ago):
    from django.utils.timezone import localdate
    approve('sct_date')
    value = None if days_ago is None else localdate() - timedelta(days=days_ago)
    assert write_clinical_inputs(patient, {'sct_date': value}).sct_date == value


def test_sct_write_limit_uses_localdate_not_fact_event_date(patient, monkeypatch):
    approve('sct_date')
    monkeypatch.setattr('django.utils.timezone.localdate', lambda: date(2026, 1, 1))
    for event_date in (date(2025, 1, 1), date(2099, 1, 1)):
        with pytest.raises(ValidationError, match='SCT date cannot be in the future'):
            write_clinical_inputs(patient, {'sct_date': date(2026, 1, 2)}, today=event_date)
    assert not Observation.objects.filter(person=patient).exists()


def test_conflicting_pending_value_cannot_mask_failed_roundtrip(patient):
    approve(RATIO)
    PatientRecord.objects.filter(person=patient).update(
        prothrombin_time_uln=Decimal('1.2'), user_edited_fields=[RATIO])
    fact(patient, RATIO, 2, observation_date=date.today() + timedelta(days=1))
    with pytest.raises(ValidationError):
        write_clinical_inputs(patient, {RATIO: '1.2'})
    assert Observation.objects.filter(person=patient).count() == 1


def test_approval_does_not_backfill_pending_values_as_self_report(patient):
    PatientRecord.objects.filter(person=patient).update(
        prothrombin_time_uln=Decimal('1.2'), user_edited_fields=[RATIO])
    mapping = approve(RATIO)
    from omop_core.services.omop_projection import project_field_to_omop
    assert project_field_to_omop(mapping) == 0
    assert not Observation.objects.filter(person=patient).exists()


def test_built_source_fixture_cannot_overwrite_an_existing_runtime_fact(patient):
    approve(BOOL)
    write_clinical_inputs(patient, {BOOL: True})
    original = Observation.objects.get(person=patient)
    before = Observation.objects.filter(pk=original.pk).values().get()
    # Local override forces the original process-sequence collision; real IDs
    # must still come from the runtime allocator before a built fact is saved.
    added = fact(patient, BOOL, 'false', __sequence=original.pk - 1)
    assert added.pk != original.pk
    assert Observation.objects.filter(person=patient).count() == 2
    assert Observation.objects.filter(pk=original.pk).values().get() == before


def test_legacy_sct_mapping_is_not_sdk_permission(patient):
    ConceptFactory(concept_id=32817, concept_code='32817')
    approve('sct_date', concept_id=32817, type_concept_id=32817)
    assert not capabilities()['sct_date']['writable']
    from omop_core.services.write_descriptor import build_writable_field_descriptor
    assert build_writable_field_descriptor()['sct_date']['writable']
    with pytest.raises(ValidationError):
        write_clinical_inputs(patient, {'sct_date': None})


def test_applier_does_not_accept_spoofed_descriptor(patient):
    from omop_core.services.field_write_service import apply_field_writes
    descriptor = {RATIO: {'kind': 'editable', 'writable': True, 'target': 'observation',
                          'concept_id': 0, 'source_value': 'wrong'}}
    with transaction.atomic(), pytest.raises(ValidationError):
        apply_field_writes(patient, {RATIO: 1.2}, descriptor=descriptor)
    assert not Observation.objects.filter(person=patient).exists()


@pytest.mark.parametrize('entry', [None, {'writable': False}, {'writable': True, 'read_only_for_caller': True}])
def test_approved_recipe_does_not_override_narrowed_caller_descriptor(patient, entry):
    from omop_core.services.field_write_service import apply_field_writes
    approve(BOOL)
    descriptor = {} if entry is None else {BOOL: entry}
    with pytest.raises(ValidationError):
        apply_field_writes(patient, {BOOL: False}, descriptor=descriptor)
    assert not Observation.objects.filter(person=patient).exists()


def test_one_denied_clinical_field_rolls_back_allowed_clinical_and_profile_edits(patient):
    from omop_core.services.field_write_service import apply_field_writes
    from omop_core.services.write_descriptor import build_writable_field_descriptor
    approve(RATIO)
    approve(BOOL)
    descriptor = build_writable_field_descriptor()
    descriptor[BOOL]['writable'] = False
    original = patient.gender_source_value
    with pytest.raises(ValidationError):
        apply_field_writes(patient, {'gender': 'Female', RATIO: '1.2', BOOL: False}, descriptor=descriptor)
    patient.refresh_from_db()
    assert patient.gender_source_value == original
    assert not Observation.objects.filter(person=patient).exists()


@pytest.fixture
def staff_client():
    from patient_portal.models import Identity
    from rest_framework.test import APIClient
    client = APIClient()
    client.force_authenticate(Identity.objects.create_user(email='clinical-contract@example.test', is_staff=True))
    return client


@pytest.mark.parametrize('field,value', [(RATIO, '1.5'), (BOOL, False)])
def test_native_mixed_patch_does_not_lose_other_edits(patient, staff_client, field, value):
    approve(field)
    url = f'/api/patient-info/{patient.pk}/'
    response = staff_client.patch(url, {field: value, 'planned_therapies': 'New patient plan', 'weight': 73}, format='json')
    assert response.status_code == 200, response.data
    record = PatientRecord.objects.get(person=patient)
    assert record.planned_therapies == 'New patient plan'
    assert record.weight == 73
    assert record.weight_units == 'kg'
    assert 'planned_therapies' in record.user_edited_fields
    assert field not in record.user_edited_fields
    assert staff_client.get(url).data['patient_info'][field] == response.data[field]


@pytest.mark.parametrize('clinical', [False, True])
@pytest.mark.parametrize('weight,weight_unit,height,height_unit', [(180, 'lb', 70, 'in'), (80, 'kg', 1.8, 'm')])
def test_unchanged_body_measurement_echo_never_relabels_units(patient, staff_client, clinical,
                                                           weight, weight_unit, height, height_unit):
    record = PatientRecord.objects.get(person=patient)
    record.weight, record.weight_units = weight, weight_unit
    record.height, record.height_units = height, height_unit
    record.save()
    before_bmi = record.bmi
    payload = {'weight': weight, 'height': height}
    if clinical:
        approve(RATIO)
        payload[RATIO] = '1.2'
    response = staff_client.patch(f'/api/patient-info/{patient.pk}/', payload, format='json')
    assert response.status_code == 200, response.data
    record.refresh_from_db()
    assert (record.weight, record.weight_units, record.height, record.height_units) == (weight, weight_unit, height, height_unit)
    assert record.bmi == before_bmi


def test_new_sct_import_keeps_precedence_over_older_sdk_mapping(patient):
    ConceptFactory(concept_id=32817, concept_code='32817')
    approve('sct_date')
    fact(patient, 'sct_date', '2020-01-01', observation_date=date.today() - timedelta(days=1))
    fact(patient, 'sct_date', '2024-01-01', observation_concept_id=32817,
         observation_type_concept_id=32817)
    assert refresh_patient_record(patient).sct_date == date(2024, 1, 1)


def test_body_units_compare_against_locked_record_not_stale_serializer_instance(patient):
    from patient_portal.api.serializers import PatientRecordSerializer
    record = PatientRecord.objects.get(person=patient)
    record.weight, record.weight_units = 180, 'kg'
    record.save()
    serializer = PatientRecordSerializer(record, data={'weight': 180}, partial=True)
    assert serializer.is_valid(), serializer.errors
    # A concurrent edit committed after validation but before the update lock.
    PatientRecord.objects.filter(pk=record.pk).update(weight_units='lb')
    serializer.save()
    record.refresh_from_db()
    assert record.weight == 180 and record.weight_units == 'lb'


def test_actual_body_value_edits_keep_native_canonical_unit_policy(patient, staff_client):
    PatientRecord.objects.filter(person=patient).update(weight=180, weight_units='lb', height=70, height_units='in')
    response = staff_client.patch(f'/api/patient-info/{patient.pk}/', {'weight': 80, 'height': 180}, format='json')
    assert response.status_code == 200, response.data
    record = PatientRecord.objects.get(person=patient)
    assert (record.weight, record.weight_units, record.height, record.height_units) == (80, 'kg', 180, 'cm')


def test_no_native_sct_source_keeps_custom_mapping_read_fallback(patient):
    approve('sct_date', source_value='custom:sct-date')
    fact(patient, 'sct_date', '2024-01-01', observation_source_value='custom:sct-date')
    assert refresh_patient_record(patient).sct_date == date(2024, 1, 1)


@pytest.mark.parametrize('old_concept,old_type', [(0, 32865), (32817, 32817)])
def test_custom_native_sct_edit_roundtrips_with_existing_native_source_history(patient, staff_client,
                                                                            old_concept, old_type):
    ConceptFactory(concept_id=32817, concept_code='32817')
    approve('sct_date', source_value='custom:sct-date', concept_id=32817, type_concept_id=32817)
    fact(patient, 'sct_date', '2020-01-01', observation_concept_id=old_concept,
         observation_type_concept_id=old_type, observation_date=date.today() - timedelta(days=1))
    response = staff_client.patch(f'/api/patient-info/{patient.pk}/', {'sct_date': '2024-01-01'}, format='json')
    assert response.status_code == 200, response.data
    assert response.data['sct_date'] == '2024-01-01'
    assert refresh_patient_record(patient).sct_date == date(2024, 1, 1)
    assert staff_client.get(f'/api/patient-info/{patient.pk}/').data['patient_info']['sct_date'] == '2024-01-01'


def test_other_sct_source_retains_existing_curated_priority_not_new_cross_source_recency(patient):
    ConceptFactory(concept_id=32817, concept_code='32817')
    approve('sct_date', source_value='custom:sct-date', concept_id=32817, type_concept_id=32817)
    fact(patient, 'sct_date', '2024-01-01')
    fact(patient, 'sct_date', '2020-01-01', observation_concept_id=32817, observation_type_concept_id=32817,
         observation_source_value='custom:sct-date', observation_date=date.today() - timedelta(days=1))
    assert refresh_patient_record(patient).sct_date == date(2020, 1, 1)


@pytest.mark.parametrize('custom_value,pending,unresolved', [('2024-01-01', date(2024, 1, 1), False), ('invalid', None, True)])
def test_selected_custom_sct_fact_controls_pending_confirmation(patient, custom_value, pending, unresolved):
    ConceptFactory(concept_id=32817, concept_code='32817')
    approve('sct_date', source_value='custom:sct-date', concept_id=32817, type_concept_id=32817)
    fact(patient, 'sct_date', '2020-01-01', unit_source_value='s',
         observation_date=date.today() - timedelta(days=1))
    fact(patient, 'sct_date', custom_value, observation_concept_id=32817, observation_type_concept_id=32817,
         observation_source_value='custom:sct-date')
    PatientRecord.objects.filter(person=patient).update(sct_date=pending, user_edited_fields=['sct_date'])
    record = refresh_patient_record(patient)
    assert record.sct_date == pending
    assert ('sct_date' in record.user_edited_fields) is unresolved


def test_future_legacy_sct_blocks_sdk_command_without_resurrecting_old_sdk_date(patient):
    ConceptFactory(concept_id=32817, concept_code='32817')
    approve('sct_date')
    fact(patient, 'sct_date', '2020-01-01', observation_date=date.today() - timedelta(days=1))
    fact(patient, 'sct_date', '2024-01-01', observation_concept_id=32817,
         observation_type_concept_id=32817, observation_date=date.today() + timedelta(days=1))
    with pytest.raises(ValidationError):
        write_clinical_inputs(patient, {'sct_date': '2025-01-01'})
    assert refresh_patient_record(patient).sct_date == date(2024, 1, 1)
    assert Observation.objects.filter(person=patient).count() == 2


def test_native_autosave_echoes_do_not_require_write_approval(patient, staff_client):
    url = f'/api/patient-info/{patient.pk}/'
    get = staff_client.get(url)
    payload = {field: get.data['patient_info'][field] for field in RECIPES if field != 'sct_date'}
    payload['planned_therapies'] = 'New patient plan'
    response = staff_client.patch(url, payload, format='json')
    assert response.status_code == 200, response.data
    assert not Observation.objects.filter(person=patient).exists()
    assert PatientRecord.objects.get(person=patient).planned_therapies == 'New patient plan'


def test_native_unavailable_changed_field_rejects_entire_patch(patient, staff_client):
    response = staff_client.patch(f'/api/patient-info/{patient.pk}/',
                                  {BOOL: False, 'planned_therapies': 'Must roll back'}, format='json')
    assert response.status_code == 400
    assert PatientRecord.objects.get(person=patient).planned_therapies is None
    assert not Observation.objects.filter(person=patient).exists()


@pytest.mark.parametrize('payload', [{BOOL: 'false'}, {RATIO: True}, {RATIO: '1.234567'}])
def test_native_api_does_not_coerce_invalid_raw_inputs(patient, staff_client, payload):
    for field in payload:
        approve(field)
    response = staff_client.patch(f'/api/patient-info/{patient.pk}/', payload, format='json')
    assert response.status_code == 400
    assert not Observation.objects.filter(person=patient).exists()


@pytest.mark.parametrize('next_value', ['1.5', None])
def test_new_authored_intent_supersedes_only_its_pending_value(patient, next_value):
    approve(RATIO)
    PatientRecord.objects.filter(person=patient).update(
        prothrombin_time_uln=Decimal('1.2'), user_edited_fields=[RATIO, 'planned_therapies'],
        planned_therapies='Keep pending plan')
    record = write_clinical_inputs(patient, {RATIO: next_value})
    assert getattr(record, RATIO) == (None if next_value is None else Decimal(next_value))
    assert record.user_edited_fields == ['planned_therapies']
    assert record.planned_therapies == 'Keep pending plan'


@pytest.mark.parametrize('bad', [
    {'observation_concept_id': 32865}, {'observation_type_concept_id': 0},
    {'value_as_string': 'also text'}, {'unit_concept_id': 32865},
])
def test_latest_incompatible_recipe_is_unknown_even_when_pending_matches(patient, bad):
    fact(patient, RATIO, 1, observation_date=date.today() - timedelta(days=1))
    fact(patient, RATIO, 1.2, **bad)
    record = refresh_patient_record(patient)
    assert getattr(record, RATIO) is None
    PatientRecord.objects.filter(person=patient).update(
        prothrombin_time_uln=Decimal('1.2'), user_edited_fields=[RATIO])
    record = refresh_patient_record(patient)
    assert RATIO in record.user_edited_fields


@pytest.mark.parametrize('bad', [{'unit_source_value': 's'}, {'value_as_number': 1}])
def test_latest_boolean_bad_shape_is_unknown(patient, bad):
    fact(patient, BOOL, 'true', **bad)
    assert getattr(refresh_patient_record(patient), BOOL) is None


def test_native_mixed_patch_readback_failure_rolls_back_everything(patient, staff_client):
    approve(RATIO)
    fact(patient, RATIO, 2, observation_date=date.today() + timedelta(days=1))
    response = staff_client.patch(f'/api/patient-info/{patient.pk}/',
                                  {RATIO: '1.2', 'planned_therapies': 'Must roll back'}, format='json')
    assert response.status_code == 400
    assert PatientRecord.objects.get(person=patient).planned_therapies is None
    assert Observation.objects.filter(person=patient).count() == 1


def test_applier_rolls_back_demographic_change_when_clinical_gate_fails(patient):
    from omop_core.services.field_write_service import apply_field_writes
    original = patient.gender_source_value
    with pytest.raises(ValidationError):
        apply_field_writes(patient, {'gender': 'Female', RATIO: '1.2'})
    patient.refresh_from_db()
    assert patient.gender_source_value == original


def test_native_create_and_bulk_reject_clinical_inputs(patient):
    from patient_portal.api.serializers import PatientRecordSerializer
    approve(BOOL)
    for kwargs in ({'data': {BOOL: False}}, {'data': [{BOOL: False}], 'many': True}):
        serializer = PatientRecordSerializer(**kwargs)
        assert not serializer.is_valid()
    assert not Observation.objects.filter(person=patient).exists()


@pytest.mark.parametrize('value,future_value,bad', [
    ('1.2', 1.2, {}),
    (None, 2, {'unit_source_value': 's'}),
    (None, None, {'value_source_value': 'PatientRecord:cleared'}),
])
def test_equal_or_invalid_future_fact_cannot_ack_unselected_new_fact(patient, value, future_value, bad):
    approve(RATIO)
    fact(patient, RATIO, future_value, observation_date=date.today() + timedelta(days=1), **bad)
    with pytest.raises(ValidationError):
        write_clinical_inputs(patient, {RATIO: value})
    assert Observation.objects.filter(person=patient).count() == 1


@pytest.mark.parametrize('restriction', ['anonymous', 'foreign_patient', 'analyst', 'untrusted'])
def test_curated_mapping_never_grants_caller_access(patient, restriction):
    from omop_core.models import GroupAccess
    from patient_portal.models import Identity, PatientUser
    from rest_framework.test import APIClient
    from tests.factories import OrganizationFactory

    approve(BOOL)
    actor = Identity.objects.create_user(email='restricted-clinical@example.test')
    client = APIClient()
    if restriction != 'anonymous':
        client.force_authenticate(actor)
    if restriction == 'foreign_patient':
        PatientUser.objects.create(identity=actor, person=PersonFactory())
    elif restriction == 'analyst':
        GroupAccess.objects.create(identity=actor, org=patient.patient_record.organization, role='analyst')
    elif restriction == 'untrusted':
        GroupAccess.objects.create(identity=actor, org=OrganizationFactory(), role='org_admin')
    response = client.patch(f'/api/patient-info/{patient.pk}/', {BOOL: False, 'person': patient.pk}, format='json')
    assert response.status_code in {401, 403, 404}
    assert not Observation.objects.filter(person=patient).exists()


def test_self_service_routes_to_same_dedicated_source(patient):
    from patient_portal.models import Identity, PatientUser
    from rest_framework.test import APIClient
    approve(BOOL)
    actor = Identity.objects.create_user(email='self-clinical@example.test')
    PatientUser.objects.create(identity=actor, person=patient)
    client = APIClient()
    client.force_authenticate(actor)
    for value in (False, None, True):
        response = client.patch('/api/patient-info/me/', {BOOL: value}, format='json')
        assert response.status_code == 200, response.data
        assert response.data['patient_info'][BOOL] is value
        assert getattr(refresh_patient_record(patient), BOOL) is value
    assert Observation.objects.filter(person=patient).count() == 3


def test_legacy_sct_native_path_still_writes_its_original_recipe(patient, staff_client):
    ConceptFactory(concept_id=32817, concept_code='32817')
    approve('sct_date', concept_id=32817, type_concept_id=32817)
    response = staff_client.patch(f'/api/patient-info/{patient.pk}/', {'sct_date': '2024-01-15'}, format='json')
    assert response.status_code == 200, response.data
    row = Observation.objects.get(person=patient, observation_source_value='mm-sct-date')
    assert row.observation_concept_id == row.observation_type_concept_id == 32817
    assert str(refresh_patient_record(patient).sct_date) == '2024-01-15'
    assert not capabilities()['sct_date']['writable']


def test_revoked_mapping_cannot_use_stale_descriptor_or_generic_projection(patient):
    from omop_core.services.field_write_service import apply_field_writes
    from omop_core.services.omop_projection import project_single_value
    from omop_core.services.write_descriptor import build_writable_field_descriptor
    mapping = approve(RATIO)
    descriptor = build_writable_field_descriptor()
    mapping.status = 'rejected'
    mapping.save()
    with pytest.raises(ValidationError):
        apply_field_writes(patient, {RATIO: 1.2}, descriptor=descriptor)
    with pytest.raises(ValidationError):
        project_single_value(patient, RATIO, 1.2, descriptor[RATIO]['projection'])
    assert not Observation.objects.filter(person=patient).exists()


def test_invalid_null_source_does_not_resolve_pending_clear(patient):
    fact(patient, RATIO, None, unit_source_value='s')
    PatientRecord.objects.filter(person=patient).update(user_edited_fields=[RATIO])
    assert RATIO in refresh_patient_record(patient).user_edited_fields


def test_curator_availability_uses_same_contract_resolver(patient):
    from patient_portal.api.serializers import FieldConceptMappingSerializer
    mapping = approve(RATIO)
    assert FieldConceptMappingSerializer(mapping).data['makes_field_writable']
    mapping.unit = 's'
    mapping.save()
    assert not FieldConceptMappingSerializer(mapping).data['makes_field_writable']


def test_self_report_type_in_wrong_vocabulary_fails_closed(patient):
    Concept.objects.filter(pk=32865).update(vocabulary_id='LOINC')
    approve(RATIO)
    assert not capabilities()[RATIO]['writable']
    with pytest.raises(ValidationError):
        write_clinical_inputs(patient, {RATIO: '1.2'})
    assert not Observation.objects.filter(person=patient).exists()


@pytest.mark.parametrize('old_version', [16, 18])
def test_normal_backfill_selects_older_records_for_source_reader_update(patient, old_version):
    from django.core.management import call_command
    from omop_core.services.patient_record_service import DERIVATION_VERSION
    fact(patient, RATIO, 1.2)
    original = list(Observation.objects.filter(person=patient).values())
    PatientRecord.objects.filter(person=patient).update(derivation_version=old_version, prothrombin_time_uln=None)
    call_command('backfill_patient_records', organization=patient.patient_record.organization.slug)
    record = PatientRecord.objects.get(person=patient)
    assert record.derivation_version == DERIVATION_VERSION == 19
    assert getattr(record, RATIO) == Decimal('1.2')
    assert list(Observation.objects.filter(person=patient).values()) == original


def test_pending_echo_is_not_silently_acknowledged_without_approval(patient, staff_client):
    PatientRecord.objects.filter(person=patient).update(
        able_to_swallow_tablets_capsules=False, user_edited_fields=[BOOL])
    response = staff_client.patch(f'/api/patient-info/{patient.pk}/', {BOOL: False}, format='json')
    assert response.status_code == 400
    assert BOOL in PatientRecord.objects.get(person=patient).user_edited_fields
    assert not Observation.objects.filter(person=patient).exists()


def test_authorized_route_owns_fact_not_submitted_person(patient, staff_client):
    approve(BOOL)
    other = PersonFactory()
    response = staff_client.patch(f'/api/patient-info/{patient.pk}/',
                                  {BOOL: False, 'person': other.pk}, format='json')
    assert response.status_code == 200, response.data
    assert Observation.objects.get(observation_source_value=RECIPES[BOOL][0]).person_id == patient.pk
    assert PatientRecord.objects.get(person=patient).person_id == patient.pk


def test_sdk_sct_clear_masks_but_never_mutates_legacy_history(patient):
    ConceptFactory(concept_id=32817, concept_code='32817')
    legacy = fact(patient, 'sct_date', '2023-01-01', observation_concept_id=32817,
                  observation_type_concept_id=32817, observation_date=date.today() - timedelta(days=1))
    original = Observation.objects.filter(pk=legacy.pk).values().get()
    approve('sct_date')  # Synthetic compatible binding, NOT legacy compatibility permission.
    record = write_clinical_inputs(patient, {'sct_date': None})
    assert record.sct_date is None
    assert refresh_patient_record(patient).sct_date is None
    assert Observation.objects.filter(pk=legacy.pk).values().get() == original


def test_valid_tombstone_resolves_pending_clear_after_refresh(patient):
    fact(patient, RATIO, None, value_source_value='PatientRecord:cleared')
    PatientRecord.objects.filter(person=patient).update(user_edited_fields=[RATIO])
    record = refresh_patient_record(patient)
    assert getattr(record, RATIO) is None
    assert RATIO not in record.user_edited_fields


@pytest.mark.parametrize('field', list(RECIPES))
def test_source_provenance_contains_corrections_and_tombstones_only_for_this_patient(patient, field):
    from omop_core.services.provenance_service import get_field_provenance
    approve(field)
    value = {RATIO: '1.2', 'activated_partial_thromboplastin_time_uln': '1.5',
             BOOL: False, 'sct_date': '2024-01-15'}[field]
    write_clinical_inputs(patient, {field: value})
    write_clinical_inputs(patient, {field: value})
    write_clinical_inputs(patient, {field: None})
    expected = list(Observation.objects.filter(person=patient).order_by('-observation_id').values_list('pk', flat=True))
    fact(PersonFactory(), field, value)
    fact(patient, field, value, observation_source_value='unrelated-source')
    rows = get_field_provenance(patient, field)['source_rows']
    assert [row['id'] for row in rows] == expected
    assert rows[0]['selected'] and not any(row['selected'] for row in rows[1:])


def test_raw_clinical_snapshot_empty_is_not_a_fallback_instruction(patient):
    from types import SimpleNamespace
    from omop_core.services.clinical_input_contract import values_from_snapshot
    row = fact(patient, RATIO, 1.2)
    fallback = dict(observations=[row], obs_by_source={RECIPES[RATIO][0]: [row]})
    assert values_from_snapshot(SimpleNamespace(**fallback, clinical_input_observations=None))[RATIO] == Decimal('1.2')
    assert values_from_snapshot(SimpleNamespace(**fallback, clinical_input_observations=[])) == {}


def test_raw_latest_invalid_sct_masks_legacy_date_without_resolving_pending(patient):
    ConceptFactory(concept_id=32817, concept_code='32817')
    fact(patient, 'sct_date', '2023-01-01', observation_concept_id=32817,
         observation_type_concept_id=32817, observation_date=date.today() - timedelta(days=1))
    fact(patient, 'sct_date', None, value_source_value='PatientRecord:cleared', unit_source_value='s')
    assert refresh_patient_record(patient).sct_date is None
    PatientRecord.objects.filter(person=patient).update(user_edited_fields=['sct_date'])
    assert 'sct_date' in refresh_patient_record(patient).user_edited_fields


@pytest.mark.django_db(transaction=True)
def test_mapping_cannot_be_revoked_between_approval_check_and_fact_write(patient, monkeypatch):
    import threading
    from django.db import OperationalError, connection, connections
    from omop_core.services import clinical_input_contract as contract

    mapping = approve(RATIO)
    original = contract._append_inputs
    outcome = []

    def revoke():
        try:
            with connection.cursor() as cursor:
                cursor.execute("SET lock_timeout = '100ms'")
            FieldConceptMapping.objects.filter(pk=mapping.pk).update(status='rejected')
            outcome.append('revoked')
        except OperationalError:
            outcome.append('locked')
        finally:
            connections.close_all()

    def append_after_competing_revocation(*args):
        thread = threading.Thread(target=revoke)
        thread.start()
        thread.join(timeout=5)
        assert not thread.is_alive()
        assert outcome == ['locked']
        return original(*args)

    monkeypatch.setattr(contract, '_append_inputs', append_after_competing_revocation)
    record = write_clinical_inputs(patient, {RATIO: '1.2'})
    assert getattr(record, RATIO) == Decimal('1.2')
    mapping.refresh_from_db()
    assert mapping.status == 'approved'


def test_clinical_writes_preserve_current_therapy_history_attestation_contract(patient):
    from omop_core.services.therapy_history import confirm_therapy_history, withdraw_therapy_history
    approve(BOOL)
    confirm_therapy_history(patient)
    assert PatientRecord.objects.get(person=patient).therapy_history_complete is True
    assert write_clinical_inputs(patient, {BOOL: False}).therapy_history_complete is True
    withdraw_therapy_history(patient)
    assert PatientRecord.objects.get(person=patient).therapy_history_complete is None
    assert write_clinical_inputs(patient, {BOOL: True}).therapy_history_complete is None


@pytest.mark.parametrize('paired', [True, False])
def test_clinical_write_preserves_current_flc_pairing_and_imwg_projection(patient, paired):
    from omop_core.services.patient_record_service import _get_laboratory_data
    from tests.test_mm_lab_loinc_mapping import _measure
    approve(BOOL)
    _measure(patient, '36916-5', 10.23, unit='mg/dL', date='2024-01-15')
    _measure(patient, '33944-0', 6.2, unit='mg/dL', date='2024-01-15' if paired else '2024-01-16')
    fields = ('kappa_flc', 'lambda_flc', 'kappa_lambda_ratio', 'measurable_disease_imwg', 'meets_slim')
    before = refresh_patient_record(patient)
    values = {field: getattr(before, field) for field in fields}
    pair = _get_laboratory_data(patient)['_flc_pair']
    if paired:
        assert pair == (Decimal('102.3'), Decimal('62'))
        assert pair[0] / pair[1] == Decimal('1.65')
    else:
        assert pair is None
    after = write_clinical_inputs(patient, {BOOL: False})
    assert {field: getattr(after, field) for field in fields} == values
    assert _get_laboratory_data(patient)['_flc_pair'] == pair
