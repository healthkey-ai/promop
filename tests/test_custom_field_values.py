"""Created fields take a value from the patient tab (#2669).

The edit follows the PatientRecord-first architecture: it lands in
``PatientRecord.custom_fields``, is tracked as a pending edit under
``custom_fields.<name>``, is projected into OMOP through the field's own
mapping, and survives a derivation until OMOP represents it.
"""
from datetime import timedelta
from decimal import Decimal

import pytest
from django.contrib.contenttypes.models import ContentType
from django.utils import timezone
from rest_framework.test import APIClient

from omop_core.models import (
    ConditionOccurrence, CustomPatientField, FieldConceptMapping, FieldDiseaseMembership,
    FieldFormula, GroupAccess, Measurement, Observation, PatientRecord, ProvenanceRecord,
    RecordRevision,
)
from omop_core.services import custom_field_values
from omop_core.services.omop_projection import CLEAR_VALUE, PATIENT_SELF_REPORT_TYPE
from omop_core.services.patient_record_service import refresh_patient_record
from omop_core.signals import suppress_patient_record_refresh
from patient_portal.models import Identity, PatientUser
from tests.factories import (
    ConceptFactory, MeasurementFactory, ObservationFactory, OrganizationFactory,
    PatientRecordFactory, VocabularyFactory,
)

pytestmark = pytest.mark.django_db

EHR = 32817
WRITABLE = '/api/v1/patient-records/writable-fields/'


@pytest.fixture
def record(settings):
    settings.CELERY_BROKER_URL = ''
    ConceptFactory(concept_id=EHR, concept_code='ehr-type')
    ConceptFactory(concept_id=PATIENT_SELF_REPORT_TYPE, concept_code='patient-report-type')
    return PatientRecordFactory()


def created_field(name, field_type='text', table='Observation', *, mode='editable',
                  status='approved', unit='', tab='labs'):
    concept = ConceptFactory(concept_code=f'code-{name}'[:50],
                             vocabulary=VocabularyFactory(vocabulary_id='SNOMED'))
    mapping = FieldConceptMapping.objects.create(
        field_name=name, concept=concept, vocabulary_id='SNOMED', concept_code=concept.concept_code,
        omop_table=table, source_value=f'custom:{name}', unit=unit, status=status,
        value_kind={'number': 'number', 'boolean': 'boolean', 'date': 'date'}.get(field_type, 'string'),
    )
    return CustomPatientField.objects.create(
        field_name=name, display_name=name.replace('_', ' ').title(), tab=tab,
        field_type=field_type, mode=mode, mapping=mapping)


@pytest.fixture
def staff():
    client = APIClient()
    client.force_authenticate(Identity.objects.create_user(email='staff@example.org', is_staff=True))
    return client


def url(record):
    return f'/api/patient-info/{record.person_id}/'


def patch(client, record, values, expect=200):
    response = client.patch(url(record), {'custom_fields': values}, format='json')
    assert response.status_code == expect, response.data
    return response


def stored(record):
    return PatientRecord.objects.get(pk=record.pk)


def pending(record):
    return custom_field_values.pending_names(stored(record).user_edited_fields)


def facts(record, field, model=Observation):
    source = 'measurement_source_value' if model is Measurement else 'observation_source_value'
    return model.objects.filter(person=record.person, **{source: f'custom:{field.field_name}'})


# ------------------------------------------------------------- the value itself


@pytest.mark.parametrize('field_type, raw, expected', [
    ('text', '  stable  ', 'stable'),
    ('text', 7, '7'),
    ('number', '4.5', 4.5),
    ('number', 4, 4),
    ('number', '12', 12),
    ('date', '2026-03-01', '2026-03-01'),
    ('boolean', True, True),
    ('boolean', False, False),
    ('boolean', 'false', False),
    ('text', '', None),
    ('number', None, None),
    ('date', '   ', None),
])
def test_coerce_stores_a_value_as_its_type(field_type, raw, expected):
    value = custom_field_values.coerce(field_type, raw)
    assert value == expected and type(value) is type(expected)


@pytest.mark.parametrize('field_type, raw', [
    ('number', 'high'), ('number', True), ('number', 'nan'), ('number', [1]),
    ('date', '03/01/2026'), ('date', 20260301), ('date', '2026-02-30'),
    ('boolean', 'yes'), ('boolean', 1),
    ('text', {'a': 1}), ('text', True),
])
def test_coerce_refuses_a_value_of_another_type(field_type, raw):
    with pytest.raises(ValueError):
        custom_field_values.coerce(field_type, raw)


# ------------------------------------------------------ PatientRecord first, then OMOP


def test_a_number_lands_on_the_record_and_as_one_dated_measurement(record, staff):
    field = created_field('frailty_index', 'number', 'Measurement', unit='points')
    response = patch(staff, record, {'frailty_index': 4.5})

    assert response.data['custom_fields'] == {'frailty_index': 4.5}
    assert response.data['previous_values']['custom_fields'] == {'frailty_index': None}
    assert stored(record).custom_fields == {'frailty_index': 4.5}
    row = facts(record, field, Measurement).get()
    assert row.value_as_number == Decimal('4.5')
    assert row.measurement_date == timezone.localdate()
    assert row.measurement_concept_id == field.mapping.concept_id
    assert row.measurement_type_concept_id == EHR
    assert row.unit_source_value == 'points'
    # OMOP represents it, so nothing is left pending.
    assert pending(record) == set()


def test_editing_again_the_same_day_updates_that_days_fact(record, staff):
    field = created_field('frailty_index', 'number', 'Measurement')
    patch(staff, record, {'frailty_index': 4.5})
    patch(staff, record, {'frailty_index': 6})
    assert stored(record).custom_fields == {'frailty_index': 6}
    assert [row.value_as_number for row in facts(record, field, Measurement)] == [Decimal('6')]


@pytest.mark.parametrize('field_type, value, column, expected', [
    ('text', 'IgG kappa', 'value_as_string', 'IgG kappa'),
    ('date', '2026-03-01', 'value_as_string', '2026-03-01'),
    ('boolean', False, 'value_as_number', Decimal('0')),
    ('boolean', True, 'value_as_number', Decimal('1')),
])
def test_each_type_is_written_and_read_back_as_entered(record, staff, field_type, value, column, expected):
    field = created_field('note', field_type, 'Observation')
    patch(staff, record, {'note': value})
    assert getattr(facts(record, field).get(), column) == expected
    assert pending(record) == set()

    # A full derivation reads the entered value back, not the row's own date
    # for a date nor "a row exists" for a boolean.
    refresh_patient_record(record.person)
    assert stored(record).custom_fields == {'note': value}
    assert pending(record) == set()


def test_the_edit_is_in_the_revision_history(record, staff):
    created_field('note')
    patch(staff, record, {'note': 'first'})
    patch(staff, record, {'note': 'second'})
    history = list(RecordRevision.objects.filter(patient_record=record, field='custom_fields.note')
                   .order_by('id').values_list('old_value', 'new_value'))
    assert history == [(None, 'first'), ('first', 'second')]


def test_only_a_key_that_moved_is_an_edit(record, staff):
    """The editor sends the whole object back on every autosave."""
    note = created_field('note')
    created_field('other')
    patch(staff, record, {'note': 'kept', 'other': 'also kept'})
    revisions = RecordRevision.objects.count()
    first = facts(record, note).get()

    patch(staff, record, {'note': 'kept', 'other': 'changed'})
    assert stored(record).custom_fields == {'note': 'kept', 'other': 'changed'}
    assert RecordRevision.objects.count() == revisions + 1
    assert facts(record, note).get().pk == first.pk

    # A PATCH that names no created field leaves them all as they were.
    response = staff.patch(url(record), {'city': 'Rome'}, format='json')
    assert response.status_code == 200, response.data
    assert stored(record).custom_fields == {'note': 'kept', 'other': 'changed'}


def test_a_column_and_a_created_field_save_in_one_patch(record, staff):
    created_field('note')
    response = staff.patch(url(record), {'city': 'Rome', 'custom_fields': {'note': 'both'}}, format='json')
    assert response.status_code == 200, response.data
    saved = stored(record)
    assert saved.city == 'Rome' and saved.custom_fields == {'note': 'both'}


# ----------------------------------------------------------------------- clearing


def test_a_cleared_value_stays_cleared_through_derivation(record, staff):
    field = created_field('frailty_index', 'number', 'Measurement')
    # An older imported result for the same concept, under the import's own key.
    with suppress_patient_record_refresh():
        MeasurementFactory(person=record.person, measurement_concept=field.mapping.concept,
                           measurement_source_value='LAB-77', value_as_number=Decimal('3'),
                           measurement_date=timezone.localdate() - timedelta(days=30))
    refresh_patient_record(record.person)
    assert stored(record).custom_fields == {'frailty_index': 3}

    patch(staff, record, {'frailty_index': 5})
    patch(staff, record, {'frailty_index': None})
    assert stored(record).custom_fields == {}
    assert facts(record, field, Measurement).get().value_source_value == CLEAR_VALUE

    refresh_patient_record(record.person)
    assert stored(record).custom_fields == {}
    assert pending(record) == set()

    # A result imported after the clear becomes current.
    with suppress_patient_record_refresh():
        MeasurementFactory(person=record.person, measurement_concept=field.mapping.concept,
                           measurement_source_value='LAB-77', value_as_number=Decimal('8'),
                           measurement_date=timezone.localdate() + timedelta(days=1))
    refresh_patient_record(record.person)
    assert stored(record).custom_fields == {'frailty_index': 8}


def test_an_empty_string_clears_like_null(record, staff):
    created_field('note')
    patch(staff, record, {'note': 'something'})
    patch(staff, record, {'note': ''})
    assert stored(record).custom_fields == {}


# ------------------------------------------------- derivation and imported facts


def test_a_newer_import_replaces_an_entered_value_and_a_same_day_one_does_not(record, staff):
    field = created_field('frailty_index', 'number', 'Measurement')
    patch(staff, record, {'frailty_index': 4})
    with suppress_patient_record_refresh():
        same_day = MeasurementFactory(
            person=record.person, measurement_concept=field.mapping.concept,
            measurement_source_value='LAB-77', value_as_number=Decimal('9'),
            measurement_date=timezone.localdate())
    refresh_patient_record(record.person)
    assert stored(record).custom_fields == {'frailty_index': 4}

    with suppress_patient_record_refresh():
        Measurement.objects.filter(pk=same_day.pk).update(
            measurement_date=timezone.localdate() + timedelta(days=2))
    refresh_patient_record(record.person)
    assert stored(record).custom_fields == {'frailty_index': 9}


def test_derived_values_are_json_ready(record):
    """A Decimal or a date in the JSON column would fail the save."""
    number = created_field('frailty_index', 'number', 'Measurement')
    when = created_field('first_seen', 'date', 'Observation')
    day = timezone.localdate() - timedelta(days=3)
    with suppress_patient_record_refresh():
        MeasurementFactory(person=record.person, measurement_concept=number.mapping.concept,
                           measurement_source_value='LAB-1', value_as_number=Decimal('2.50000'),
                           measurement_date=day)
        ObservationFactory(person=record.person, observation_concept=when.mapping.concept,
                           observation_source_value='OBS-1', observation_date=day)
    refresh_patient_record(record.person)
    assert stored(record).custom_fields == {'frailty_index': 2.5, 'first_seen': day.isoformat()}


def test_a_value_omop_cannot_hold_stays_pending_and_survives_derivation(record, staff):
    """Text longer than value_as_string: the write fails, the record keeps it."""
    field = created_field('note')
    long_text = 'progressive disease by imaging, ' * 4
    patch(staff, record, {'note': long_text})
    assert not facts(record, field).exists()
    assert pending(record) == {'note'}

    refresh_patient_record(record.person)
    assert stored(record).custom_fields == {'note': long_text.strip()}
    assert pending(record) == {'note'}

    # A value OMOP can hold replaces it and nothing is left pending.
    patch(staff, record, {'note': 'stable'})
    assert pending(record) == set()


def test_a_field_whose_table_has_no_writer_keeps_its_value_on_the_record(record, staff):
    created_field('registry_id', 'text', 'Person')
    patch(staff, record, {'registry_id': 'R-100'})
    assert pending(record) == {'registry_id'}
    refresh_patient_record(record.person)
    assert stored(record).custom_fields == {'registry_id': 'R-100'}

    # A pending clear holds too.
    patch(staff, record, {'registry_id': None})
    refresh_patient_record(record.person)
    assert stored(record).custom_fields == {}


def test_an_occurrence_records_a_yes_and_nothing_else(record, staff):
    flag = created_field('had_fracture', 'boolean', 'ConditionOccurrence')
    text = created_field('fracture_site', 'text', 'ConditionOccurrence')
    patch(staff, record, {'had_fracture': True, 'fracture_site': 'left femur'})

    occurrences = ConditionOccurrence.objects.filter(person=record.person)
    assert [row.condition_concept_id for row in occurrences] == [flag.mapping.concept_id]
    assert not occurrences.filter(condition_concept_id=text.mapping.concept_id).exists()
    assert stored(record).custom_fields == {'had_fracture': True, 'fracture_site': 'left femur'}

    # An occurrence cannot say "no": the answer stays on the record.
    patch(staff, record, {'had_fracture': False})
    refresh_patient_record(record.person)
    assert stored(record).custom_fields['had_fracture'] is False
    assert 'had_fracture' in pending(record)


def test_an_edit_to_a_field_since_removed_is_dropped_by_derivation(record, staff):
    field = created_field('registry_id', 'text', 'Person')
    patch(staff, record, {'registry_id': 'R-100'})
    mapping = field.mapping
    field.delete()
    mapping.delete()
    refresh_patient_record(record.person)
    saved = stored(record)
    assert saved.custom_fields == {} and pending(record) == set()


def test_a_computed_created_field_keeps_its_result_beside_an_edit(record, staff):
    created_field('note')
    created_field('double_weight', 'number', 'Measurement', mode='computed')
    FieldFormula.objects.create(field_name='double_weight', formula='weight * 2', is_active=True)
    PatientRecord.objects.filter(pk=record.pk).update(weight=70)
    patch(staff, record, {'note': 'stable'})
    assert stored(record).custom_fields == {'note': 'stable', 'double_weight': 140}
    # The editor echoes the computed value back with its own next edit.
    patch(staff, record, {'note': 'better', 'double_weight': 140})
    assert stored(record).custom_fields == {'note': 'better', 'double_weight': 140}


# ---------------------------------------------------------------------- refusals


def test_a_computed_field_and_an_unknown_key_are_refused(record, staff):
    created_field('note')
    created_field('double_frailty', 'number', mode='computed')
    PatientRecord.objects.filter(pk=record.pk).update(custom_fields={'double_frailty': 8})

    response = patch(staff, record, {'double_frailty': 9}, expect=400)
    assert 'formula' in str(response.data['custom_fields']['double_frailty'])
    response = patch(staff, record, {'not_a_field': 'x'}, expect=400)
    assert 'not a field' in str(response.data['custom_fields']['not_a_field'])
    response = staff.patch(url(record), {'custom_fields': ['note']}, format='json')
    assert response.status_code == 400

    # The whole object echoed back, computed value included, is no edit.
    patch(staff, record, {'double_frailty': 8, 'note': 'ok'})
    assert stored(record).custom_fields == {'double_frailty': 8, 'note': 'ok'}


def test_a_refused_value_saves_nothing_from_the_request(record, staff):
    created_field('frailty_index', 'number', 'Measurement')
    response = staff.patch(
        url(record), {'city': 'Rome', 'custom_fields': {'frailty_index': 'high'}}, format='json')
    assert response.status_code == 400
    assert response.data['custom_fields']['frailty_index'] == ['Enter a number.']
    saved = stored(record)
    assert saved.city != 'Rome' and saved.custom_fields == {}
    assert not Measurement.objects.filter(person=record.person).exists()


def test_a_field_with_no_approved_mapping_is_not_editable(record, staff):
    created_field('note', status='proposed')
    response = patch(staff, record, {'note': 'x'}, expect=400)
    assert 'approved' in str(response.data['custom_fields']['note'])


def test_a_caller_who_cannot_write_the_patient_changes_nothing(record):
    created_field('note')
    analyst = Identity.objects.create_user(email='analyst@example.org')
    GroupAccess.objects.create(identity=analyst, org=record.organization, role='analyst')
    client = APIClient()
    client.force_authenticate(analyst)
    response = client.patch(url(record), {'custom_fields': {'note': 'x'}}, format='json')
    assert response.status_code in (403, 404)
    assert stored(record).custom_fields == {}


# -------------------------------------------------------------------- authorship


def test_the_patients_own_edit_is_self_report_with_provenance(record):
    field = created_field('note')
    identity = Identity.objects.create_user(email='patient@example.org')
    PatientUser.objects.create(identity=identity, person=record.person)
    client = APIClient()
    client.force_authenticate(identity)
    response = client.patch('/api/patient-info/me/', {'custom_fields': {'note': 'feeling well'}},
                            format='json')
    assert response.status_code == 200, response.data
    assert response.data['patient_info']['custom_fields'] == {'note': 'feeling well'}

    row = facts(record, field).get()
    assert row.observation_type_concept_id == PATIENT_SELF_REPORT_TYPE
    assert ProvenanceRecord.objects.filter(
        content_type=ContentType.objects.get_for_model(Observation), object_id=row.pk,
        source='PATIENT_SELF').exists()


# -------------------------------------------------------------------- descriptor


def test_writable_fields_lists_created_fields(record, staff):
    created_field('frailty_index', 'number', 'Measurement', unit='points')
    created_field('double_frailty', 'number', mode='computed')
    created_field('registry_id', 'text', 'Person')
    created_field('draft', status='proposed')
    data = staff.get(WRITABLE, {'person_id': record.person_id}).data

    entry = data['frailty_index']
    assert entry['writable'] is True and entry['custom'] is True
    assert entry['target'] == 'patient_record' and entry['container'] == 'custom_fields'
    assert entry['value_kind'] == 'number' and entry['unit'] == 'points'
    assert entry['projection']['omop_table'] == 'measurement'
    assert entry['projection']['source_value'] == 'custom:frailty_index'
    assert entry['scope'] == 'base'

    assert data['double_frailty']['writable'] is False
    assert 'formula' in data['double_frailty']['reason']
    assert data['registry_id']['writable'] is True and 'projection' not in data['registry_id']
    assert data['draft']['writable'] is False
    # A column is still described as before, and the container itself is not
    # an entry: the editor sends a key the descriptor does not name.
    assert data['city']['writable'] is True and 'custom' not in data['city']
    assert 'custom_fields' not in data


def test_writable_fields_is_read_only_for_a_caller_who_cannot_write(record):
    created_field('note')
    doctor = Identity.objects.create_user(email='elsewhere@example.org')
    GroupAccess.objects.create(identity=doctor, org=OrganizationFactory(), role='doctor')
    client = APIClient()
    client.force_authenticate(doctor)
    entry = client.get(WRITABLE, {'person_id': record.person_id}).data['note']
    assert entry['writable'] is False and entry['read_only_for_caller'] is True


@pytest.mark.usefixtures('disease_extensions')
def test_writable_fields_scopes_a_created_field_to_its_disease(record, staff):
    created_field('m_protein_note')
    FieldDiseaseMembership.objects.create(field_name='m_protein_note', disease='MM')
    created_field('base_note')
    assert staff.get(WRITABLE).data['m_protein_note']['diseases'] == ['MM']
    assert 'm_protein_note' in staff.get(WRITABLE, {'disease': 'MM'}).data
    breast = staff.get(WRITABLE, {'disease': 'BC'}).data
    assert 'm_protein_note' not in breast and 'base_note' in breast


def test_a_source_key_too_long_for_the_column_is_not_projected(record, staff):
    name = 'a_very_long_created_field_name_that_runs_past_the_source_column'
    field = created_field(name)
    assert custom_field_values.projection_for(field) is None
    patch(staff, record, {name: 'kept'})
    assert stored(record).custom_fields == {name: 'kept'} and pending(record) == {name}
    assert 'projection' not in staff.get(WRITABLE).data[name]
