"""Cell-count source pairs: cells/µL in, thousands/µL out (#5700 follow-up).

EXACT's trial rows edit the legacy count column in cells/µL. Before this, the
descriptor resolved that row to the canonical thousands/µL field, so typing
1550 stored 1550 thousand/µL and the trial showed 1,550,000 cells/µL.
"""
from decimal import Decimal

import pytest
from rest_framework.test import APIClient

from omop_core.models import Measurement, PatientRecord, Person
from omop_core.services.patient_record_service import refresh_patient_record
from omop_core.services.write_descriptor import build_writable_field_descriptor
from patient_portal.models import Identity
from tests.factories import ConceptFactory, VocabularyFactory

pytestmark = pytest.mark.django_db

PERSON_ID = 99002


def _record():
    person = Person.objects.create(person_id=PERSON_ID)
    record, _ = PatientRecord.objects.get_or_create(person=person)
    return record


@pytest.fixture
def api(settings):
    settings.CELERY_BROKER_URL = ''
    type_vocab = VocabularyFactory(
        vocabulary_id='Type Concept', vocabulary_name='Type Concept',
    )
    ConceptFactory(
        concept_id=32865, concept_code='PT-SELF-REPORT',
        concept_name='Patient self-report', vocabulary=type_vocab,
    )
    loinc = VocabularyFactory(vocabulary_id='LOINC', vocabulary_name='LOINC')
    for code, name in (
        ('751-8', 'Neutrophils [#/volume] in Blood'),
        ('731-0', 'Lymphocytes [#/volume] in Blood'),
        ('777-3', 'Platelets [#/volume] in Blood'),
    ):
        ConceptFactory(concept_code=code, concept_name=name, vocabulary=loinc)
    ConceptFactory(
        concept_code='10*3/uL',
        vocabulary=VocabularyFactory(vocabulary_id='UCUM', vocabulary_name='UCUM'),
    )
    client = APIClient()
    client.force_authenticate(
        Identity.objects.create_user(email='count-pair@example.test', is_staff=True),
    )
    return client


def _edit(api, record, data):
    response = api.patch(
        f'/api/patient-info/{record.person.person_id}/', data, format='json',
    )
    assert response.status_code == 200, response.data
    record.refresh_from_db()
    return response


def _projected(record, source_value):
    return Measurement.objects.get(
        person=record.person, measurement_source_value=source_value,
    )


def test_the_descriptor_points_the_editor_at_the_cells_column(api):
    descriptor = build_writable_field_descriptor()

    for source, canonical, unit_field in (
        ('absolute_neutrophile_count', 'anc_thousand_per_ul',
         'absolute_neutrophile_count_units'),
        ('absolute_lymphocyte_count', 'alc_thousand_per_ul', None),
        ('platelet_count', 'platelet_count_thousand_per_ul',
         'platelet_count_units'),
    ):
        assert descriptor[source]['kind'] == 'direct'
        assert descriptor[source]['writable'] is True
        assert descriptor[source]['target'] == 'patient_record'
        assert descriptor[source]['unit'] == 'CELLS/UL'
        assert descriptor[canonical]['kind'] == 'computed'
        assert descriptor[canonical]['writable'] is False
        assert descriptor[canonical]['inputs'] == [source]
        assert descriptor[canonical]['normalised_read_model'] is True
        assert 'projection' in descriptor[canonical]
        if unit_field:
            assert descriptor[unit_field]['kind'] == 'selectable'
            assert descriptor[unit_field]['qualifies'] == source


def test_the_users_1550_stays_cells_per_microliter(api):
    record = _record()
    response = _edit(api, record, {'absolute_neutrophile_count': 1550})

    # The exact trial-18606 reproduction: 1550 cells/µL, not 1,550,000.
    assert float(record.absolute_neutrophile_count) == 1550
    assert record.absolute_neutrophile_count_units == 'CELLS/UL'
    assert float(record.anc_thousand_per_ul) == 1.55
    assert Decimal(str(response.data['anc_thousand_per_ul'])) == Decimal('1.55')

    row = _projected(record, '751-8')
    assert row.value_as_number == Decimal('1.55')
    assert row.unit_source_value == '10*3/uL'


def test_a_count_sent_in_thousands_is_not_divided_again(api):
    record = _record()
    _edit(api, record, {
        'absolute_neutrophile_count': 1.5,
        'absolute_neutrophile_count_units': '10*3/uL',
    })

    assert float(record.absolute_neutrophile_count) == 1.5
    assert record.absolute_neutrophile_count_units == '10*3/uL'
    assert float(record.anc_thousand_per_ul) == 1.5
    assert _projected(record, '751-8').value_as_number == Decimal('1.5')


def test_lymphocytes_use_the_same_factor(api):
    record = _record()
    _edit(api, record, {'absolute_lymphocyte_count': 1500})

    assert float(record.absolute_lymphocyte_count) == 1500
    assert float(record.alc_thousand_per_ul) == 1.5
    assert _projected(record, '731-0').value_as_number == Decimal('1.5')


def test_platelets_keep_the_integer_cells_column(api):
    record = _record()
    _edit(api, record, {'platelet_count': 150000})

    assert record.platelet_count == 150000
    assert record.platelet_count_units == 'CELLS/UL'
    assert float(record.platelet_count_thousand_per_ul) == 150.0
    assert _projected(record, '777-3').value_as_number == Decimal('150.0')


def test_a_refresh_keeps_the_cells_value_and_one_fact(api):
    record = _record()
    _edit(api, record, {'absolute_neutrophile_count': 1550})

    for _ in range(2):
        record = refresh_patient_record(record.person)
        record.refresh_from_db()
        assert float(record.absolute_neutrophile_count) == 1550
        assert record.absolute_neutrophile_count_units == 'CELLS/UL'
        assert float(record.anc_thousand_per_ul) == 1.55
        assert {'absolute_neutrophile_count', 'absolute_neutrophile_count_units'} <= set(
            record.user_edited_fields or [])

    assert Measurement.objects.filter(
        person=record.person, measurement_source_value='751-8',
    ).count() == 1


def test_a_canonical_import_retires_the_source_pair(api):
    record = _record()
    _edit(api, record, {'absolute_neutrophile_count': 1550})

    _edit(api, record, {'anc_thousand_per_ul': 2.5})

    assert float(record.anc_thousand_per_ul) == 2.5
    assert float(record.absolute_neutrophile_count) == 2500
    assert not ({'absolute_neutrophile_count', 'absolute_neutrophile_count_units'}
                & set(record.user_edited_fields or []))


def test_the_shared_form_normalises_the_count_pair(api):
    from omop_core.services.patient_form import apply_patient_form_patch

    record = _record()
    saved, result = apply_patient_form_patch(
        record.person, {'absolute_neutrophile_count': 1550},
    )

    assert 'absolute_neutrophile_count' in result.applied
    assert 'anc_thousand_per_ul' in result.applied
    assert float(saved.absolute_neutrophile_count) == 1550
    assert float(saved.anc_thousand_per_ul) == 1.55
    assert _projected(saved, '751-8').value_as_number == Decimal('1.55')


def test_a_unit_without_its_value_is_refused(api):
    record = _record()
    response = api.patch(
        f'/api/patient-info/{record.person.person_id}/',
        {'absolute_neutrophile_count_units': 'CELLS/UL'}, format='json',
    )

    assert response.status_code == 400
    assert 'absolute_neutrophile_count_units' in response.data


def test_an_unknown_count_unit_is_refused(api):
    record = _record()
    response = api.patch(
        f'/api/patient-info/{record.person.person_id}/',
        {'absolute_neutrophile_count': 1550,
         'absolute_neutrophile_count_units': 'bananas'},
        format='json',
    )

    assert response.status_code == 400
    assert 'absolute_neutrophile_count_units' in response.data


def test_al_lymphocyte_count_offers_only_the_unit_it_stores(api):
    descriptor = build_writable_field_descriptor()

    assert descriptor['absolute_lymphocyte_count']['unit_options'] == [
        {'value': 'CELLS/UL', 'label': 'cells/µL'},
    ]


def test_legacy_upper_g_per_l_is_a_count_but_mass_g_per_l_is_refused(api):
    record = _record()
    _edit(api, record, {
        'absolute_neutrophile_count': 1.5,
        'absolute_neutrophile_count_units': 'G/L',
    })

    assert record.absolute_neutrophile_count_units == '10*9/L'
    assert float(record.anc_thousand_per_ul) == 1.5

    response = api.patch(
        f'/api/patient-info/{record.person.person_id}/',
        {'absolute_neutrophile_count': 1.5,
         'absolute_neutrophile_count_units': 'g/L'},
        format='json',
    )
    assert response.status_code == 400
    assert 'absolute_neutrophile_count_units' in response.data


def test_a_fractional_platelet_source_is_refused(api):
    from omop_core.services.patient_form import apply_patient_form_patch

    record = _record()
    saved, result = apply_patient_form_patch(
        record.person, {'platelet_count': 150.5},
    )

    assert 'platelet_count' in result.rejected
    assert not result.applied
    assert not Measurement.objects.filter(person=record.person).exists()

    response = api.patch(
        f'/api/patient-info/{record.person.person_id}/',
        {'platelet_count': 150.5}, format='json',
    )
    assert response.status_code == 400


def test_a_caller_read_only_count_unit_can_be_used_as_stored(api):
    from omop_core.services.patient_form import apply_patient_form_patch

    record = _record()
    record.absolute_neutrophile_count = 1550
    record.absolute_neutrophile_count_units = 'CELLS/UL'
    record.anc_thousand_per_ul = Decimal('1.55')
    record.user_edited_fields = []
    record.save()
    descriptor = build_writable_field_descriptor()
    descriptor['absolute_neutrophile_count_units'].update(
        writable=True, read_only_for_caller=True,
    )

    saved, result = apply_patient_form_patch(
        record.person, {'absolute_neutrophile_count': 1600},
        descriptor=descriptor,
    )

    assert 'absolute_neutrophile_count' in result.applied
    assert 'absolute_neutrophile_count_units' not in result.applied
    saved.refresh_from_db()
    assert float(saved.absolute_neutrophile_count) == 1600
    assert saved.absolute_neutrophile_count_units == 'CELLS/UL'
    assert float(saved.anc_thousand_per_ul) == 1.6

    refreshed = refresh_patient_record(saved.person)
    assert float(refreshed.absolute_neutrophile_count) == 1600
    assert refreshed.absolute_neutrophile_count_units == 'CELLS/UL'
    assert float(refreshed.anc_thousand_per_ul) == 1.6


def test_a_caller_read_only_canonical_refuses_the_whole_pair(api):
    from omop_core.services.patient_form import apply_patient_form_patch

    record = _record()
    record.absolute_neutrophile_count = 1550
    record.absolute_neutrophile_count_units = 'CELLS/UL'
    record.anc_thousand_per_ul = Decimal('1.55')
    record.user_edited_fields = []
    record.save()
    descriptor = build_writable_field_descriptor()
    descriptor['anc_thousand_per_ul'].update(
        read_only_for_caller=True, reason='Read-only for this caller.',
    )

    saved, result = apply_patient_form_patch(
        record.person, {'absolute_neutrophile_count': 1600},
        descriptor=descriptor,
    )

    assert 'absolute_neutrophile_count' in result.rejected
    assert 'absolute_neutrophile_count_units' in result.rejected
    assert not result.applied
    assert not Measurement.objects.filter(person=record.person).exists()
    saved.refresh_from_db()
    assert float(saved.absolute_neutrophile_count) == 1550
    assert float(saved.anc_thousand_per_ul) == 1.55


def test_the_precision_rollback_refuses_lossy_values():
    from importlib import import_module

    from django.db import connection as db_connection

    guard = import_module(
        'omop_core.migrations.0328_widen_count_precision',
    ).refuse_lossy_reverse

    class Editor:
        pass

    editor = Editor()
    editor.connection = db_connection

    record = _record()
    record.anc_thousand_per_ul = Decimal('1.550')
    record.save()

    with pytest.raises(RuntimeError, match='narrow'):
        guard(None, editor)

    record.anc_thousand_per_ul = Decimal('1.5')
    record.save()
    guard(None, editor)


def test_the_precision_rollback_guard_blocks_concurrent_writes():
    from importlib import import_module

    from django.conf import settings
    from django.db import connection as db_connection
    from psycopg import connect

    guard = import_module(
        'omop_core.migrations.0328_widen_count_precision',
    ).refuse_lossy_reverse

    class Editor:
        pass

    editor = Editor()
    editor.connection = db_connection
    record = _record()
    record.anc_thousand_per_ul = Decimal('1.5')
    record.save()

    # The guard takes an ACCESS EXCLUSIVE lock in the surrounding (test)
    # transaction. A second connection must not be able to slip a 1.550 write
    # between the check and the reverse ALTER.
    guard(None, editor)

    params = {
        key.lower(): value for key, value in settings.DATABASES['default'].items()
        if key in ('NAME', 'USER', 'PASSWORD', 'HOST', 'PORT')
    }
    params.setdefault('dbname', params.pop('name', None))
    params.setdefault('user', params.pop('user', None))
    params.setdefault('password', params.pop('password', None))
    params.setdefault('host', params.pop('host', None))
    params.setdefault('port', params.pop('port', None))
    params = {key: value for key, value in params.items() if value not in (None, '')}

    with pytest.raises(Exception, match='timeout|lock'):
        with connect(**params, options='-c statement_timeout=200') as other:
            with other.cursor() as cursor:
                cursor.execute(
                    'UPDATE patient_record SET anc_thousand_per_ul = 1.550 '
                    'WHERE person_id = %s',
                    [record.person_id],
                )


def test_a_pending_canonical_clear_wins_over_a_stale_count_pair():
    """The same precedence holds for the cell-count source pairs."""
    from omop_core.services.patient_record_service import recompute_patient_record_fields

    record = _record()
    record.absolute_neutrophile_count = 1550
    record.absolute_neutrophile_count_units = 'CELLS/UL'
    record.anc_thousand_per_ul = None
    record.user_edited_fields = ['anc_thousand_per_ul']
    record.save()

    recompute_patient_record_fields(record)

    record.refresh_from_db()
    assert record.anc_thousand_per_ul is None


def test_a_count_whose_canonical_overflows_the_column_is_refused(api):
    """#2433: thousands/µL is DecimalField(8,3); a larger value raised on save."""
    record = _record()
    response = api.patch(
        f'/api/patient-info/{record.person.person_id}/',
        {'absolute_neutrophile_count': 150000,
         'absolute_neutrophile_count_units': '10*3/uL'},
        format='json',
    )

    assert response.status_code == 400, response.data
    record.refresh_from_db()
    assert record.anc_thousand_per_ul is None


@pytest.mark.parametrize('value, unit, recognised', [
    (99999.999, '10*3/uL', True),
    (99999.9995, '10*3/uL', False),
    (100000, '10*3/uL', False),
    (-1, 'cells/uL', False),
    # Past the decimal context: quantize raised InvalidOperation (#2489).
    ('1e40', 'cells/uL', False),
    ('1e40', '10*3/uL', False),
])
def test_canonical_from_source_refuses_what_the_column_cannot_hold(value, unit, recognised):
    from omop_core.services.blood_count_source_pairs import canonical_from_source
    assert canonical_from_source('absolute_neutrophile_count', value, unit)[0] is recognised


def test_a_newer_dated_import_retires_the_source_pair(api):
    from datetime import date

    from omop_core.signals import suppress_patient_record_refresh
    from tests.factories import MeasurementFactory

    ConceptFactory(concept_id=32817, concept_code='EHR')
    record = _record()
    draw = date(2024, 1, 1)
    _edit(api, record, {'absolute_neutrophile_count': {
        'value': 1550, 'date': draw.isoformat()}})
    assert record.pending_source_pair_dates == {
        'absolute_neutrophile_count': draw.isoformat()}

    with suppress_patient_record_refresh():
        MeasurementFactory(
            person=record.person,
            measurement_concept=ConceptFactory(
                concept_code='751-8', concept_name='Neutrophils [#/volume] in Blood'),
            measurement_source_value='751-8',
            measurement_date=date(2024, 2, 1),
            measurement_type_concept_id=32817,
            value_as_number=Decimal('2.5'),
            unit_source_value='10*3/uL',
        )
    record = refresh_patient_record(record.person)

    # The newer result wins; the cells column follows the canonical read-model.
    assert float(record.anc_thousand_per_ul) == 2.5
    assert float(record.absolute_neutrophile_count) == 2500
    assert not ({'absolute_neutrophile_count',
                 'absolute_neutrophile_count_units'} & set(
                     record.user_edited_fields or []))
    assert record.pending_source_pair_dates == {}


def test_a_count_past_the_decimal_context_is_a_400_on_the_shared_form(api):
    """`quantize` raised InvalidOperation for a huge count: a 500 (#2489)."""
    record = _record()
    response = api.patch(
        f'/api/v1/patient-records/{record.person.person_id}/',
        {'absolute_neutrophile_count': '1e40'},
        format='json', HTTP_X_PATIENT_FORM='1',
    )

    assert response.status_code == 400, response.data
    assert 'absolute_neutrophile_count' in response.data['rejected']
    record.refresh_from_db()
    assert record.anc_thousand_per_ul is None
