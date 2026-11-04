from datetime import date
from decimal import Decimal
import io
import json

import pytest
from rest_framework.test import APIClient

from omop_core.models import FieldConceptMapping, Measurement, PatientRecord
from omop_core.services.clinical_units import blood_count_to_canonical, count_unit
from omop_core.services.patient_record_service import refresh_patient_record
from tests.factories import ConceptFactory, MeasurementFactory, PatientRecordFactory, PersonFactory


@pytest.mark.parametrize('unit,value', [
    ('cells/uL', 1500), ('cells/µL', 1500), ('CELLS/UL', 1500),
    ('/uL', 1500), ('{cells}/uL', 1500), ('#/uL', 1500),
    ('cells/L', 1_500_000_000), ('/L', 1_500_000_000),
    ('10*3/uL', 1.5), ('10^3/μL', 1.5), ('10³/µL', 1.5),
    ('K/uL', 1.5), ('10*9/L', 1.5), ('10^9/L', 1.5), ('G/L', 1.5),
])
def test_equivalent_count_scales(unit, value):
    assert blood_count_to_canonical(value, unit) == Decimal('1.5')


@pytest.mark.parametrize('value,unit', [
    (1500, None), (1500, ''), (1500, 'mg/dL'), (1500, 'g/L'),
    (1500, '%'), (-1, 'cells/uL'), (float('nan'), 'cells/uL'),
    (float('inf'), 'cells/uL'), (None, 'cells/uL'), (True, 'cells/uL'),
])
def test_unknown_scale_or_invalid_count_is_not_zero(value, unit):
    assert blood_count_to_canonical(value, unit) is None


@pytest.mark.django_db
@pytest.mark.parametrize('code,field,alias,alias_unit,source,value,expected', [
    ('751-8', 'anc_thousand_per_ul', 'absolute_neutrophile_count', 'CELLS/UL', 'cells/uL', 1500, 1.5),
    ('751-8', 'anc_thousand_per_ul', 'absolute_neutrophile_count', 'CELLS/UL', '10*9/L', 1.5, 1.5),
    ('777-3', 'platelet_count_thousand_per_ul', 'platelet_count', 'CELLS/UL', 'cells/uL', 150500, 150.5),
    ('777-3', 'platelet_count_thousand_per_ul', 'platelet_count', 'CELLS/UL', '10*3/uL', 150.5, 150.5),
])
@pytest.mark.parametrize('path', ['loinc', 'source-code', 'source-display', 'legacy-name', 'curated'])
def test_projection_paths_preserve_scale_and_source(code, field, alias, alias_unit, source, value, expected, path):
    person = PersonFactory()
    kwargs = {'person': person, 'value_as_number': value, 'unit_source_value': source}
    if path in ('loinc', 'curated'):
        concept = ConceptFactory(concept_code=code)
        kwargs.update(measurement_concept=concept, measurement_source_value=code)
        if path == 'curated':
            FieldConceptMapping.objects.create(
                field_name=field, concept=concept, vocabulary_id='LOINC', concept_code=code,
                status='approved', omop_table='measurement', source_value=code, value_kind='number',
            )
    elif path == 'source-code':
        kwargs.update(measurement_concept_id=0, measurement_source_value=code)
    elif path == 'source-display':
        kwargs.update(measurement_concept_id=0, measurement_source_value=(
            'Platelets' if code == '777-3' else 'Absolute Neutrophil Count'))
    else:
        kwargs['measurement_concept'] = ConceptFactory(concept_name=(
            'Platelet count' if code == '777-3' else 'Absolute neutrophil count'))
    measurement = MeasurementFactory(**kwargs)
    for _ in range(2):
        record = refresh_patient_record(person)
        record.refresh_from_db()
        assert float(getattr(record, field)) == expected
        assert float(getattr(record, alias)) == expected * 1000
        assert getattr(record, alias + '_units') == alias_unit
    measurement.refresh_from_db()
    assert float(measurement.value_as_number) == value
    assert measurement.unit_source_value == source


@pytest.mark.django_db
@pytest.mark.parametrize('code,field,alias', [
    ('751-8', 'anc_thousand_per_ul', 'absolute_neutrophile_count'),
    ('777-3', 'platelet_count_thousand_per_ul', 'platelet_count'),
])
@pytest.mark.parametrize('unit,value', [(None, 1500), ('mg/dL', 1500), ('cells/uL', None)])
def test_latest_unknown_blocks_older_code_and_display_fallbacks(code, field, alias, unit, value):
    person = PersonFactory()
    concept = ConceptFactory(concept_code=code)
    FieldConceptMapping.objects.create(
        field_name=field, concept=concept, vocabulary_id='LOINC', concept_code=code,
        status='approved', omop_table='measurement', source_value=code, value_kind='number',
    )
    MeasurementFactory(
        person=person, measurement_concept=concept, measurement_source_value=code,
        measurement_date=date(2024, 1, 1), value_as_number=1500, unit_source_value='cells/uL',
    )
    MeasurementFactory(
        person=person, measurement_source_value='Platelets' if code == '777-3' else 'Absolute Neutrophil Count',
        measurement_date=date(2024, 1, 2), value_as_number=value, unit_source_value=unit,
    )
    record = refresh_patient_record(person)
    assert getattr(record, field) is None
    assert getattr(record, alias) is None
    assert getattr(record, alias + '_units') is None


@pytest.mark.parametrize('text,ucum,expected', [
    ('x10E3/uL', '10*3/uL', '10*3/uL'),     # the ETL names what the lab spelling means
    ('K/cu mm', None, 'K/cu mm'),            # no concept: unreadable, as before
    ('', '/uL', '/uL'), (None, '/uL', '/uL'), (None, None, None),
    ('K/uL', '/L', 'K/uL'),                  # readable text wins over a converted concept
    ('mg/dL', '10*3/uL', 'mg/dL'),           # readable, just not a count
])
def test_count_unit_reads_the_ucum_concept_only_when_the_text_is_unreadable(text, ucum, expected):
    assert count_unit(text, ucum) == expected


@pytest.mark.django_db
def test_ucum_unit_concept_stands_in_for_unreadable_or_absent_text():
    person = PersonFactory()
    measurement = MeasurementFactory(
        person=person, measurement_source_value='751-8', value_as_number=1500,
        unit_concept=ConceptFactory(vocabulary__vocabulary_id='UCUM', concept_code='/uL'),
    )
    assert refresh_patient_record(person).anc_thousand_per_ul == Decimal('1.5')
    Measurement.objects.filter(pk=measurement.pk).update(unit_source_value='/MM3')
    assert refresh_patient_record(person).anc_thousand_per_ul == Decimal('1.5')
    # Readable text with a different concept is #2249's unproven conversion.
    Measurement.objects.filter(pk=measurement.pk).update(unit_source_value='10*3/uL')
    assert refresh_patient_record(person).anc_thousand_per_ul is None
    Measurement.objects.filter(pk=measurement.pk).update(unit_source_value='x1000', unit_concept=None)
    assert refresh_patient_record(person).anc_thousand_per_ul is None


@pytest.mark.django_db
def test_lab_spellings_with_a_ucum_concept_fill_wbc_anc_and_platelets():
    """The issue's repro: x10E3/uL and friends, with the unit the ETL read them as."""
    person = PersonFactory()
    ucum = {code: ConceptFactory(vocabulary__vocabulary_id='UCUM', concept_code=code)
            for code in ('10*3/uL', '10*9/L')}
    for code, value, text, unit in [
        ('6690-2', 5.2, 'x10E3/uL', '10*3/uL'),
        ('751-8', 2.4, 'x10(9)/L', '10*9/L'),
        ('777-3', 213, 'K/cu mm', '10*3/uL'),
    ]:
        MeasurementFactory(
            person=person, measurement_concept=ConceptFactory(concept_code=code), measurement_source_value=code,
            value_as_number=value, unit_source_value=text, unit_concept=ucum[unit],
        )
    record = refresh_patient_record(person)
    record.refresh_from_db()
    assert record.wbc_count_thousand_per_ul == Decimal('5.2')
    assert record.anc_thousand_per_ul == Decimal('2.4')
    assert record.platelet_count_thousand_per_ul == Decimal('213')


@pytest.mark.django_db
def test_zero_and_pending_edit_survive_refresh():
    person = PersonFactory()
    measurement = MeasurementFactory(
        person=person, measurement_source_value='751-8', value_as_number=0, unit_source_value='cells/uL',
    )
    record = refresh_patient_record(person)
    assert record.anc_thousand_per_ul == 0
    assert record.absolute_neutrophile_count == 0
    PatientRecord.objects.filter(pk=record.pk).update(
        anc_thousand_per_ul=2.5, user_edited_fields=['anc_thousand_per_ul'],
    )
    record = refresh_patient_record(person)
    assert record.anc_thousand_per_ul == Decimal('2.5')
    assert record.absolute_neutrophile_count == Decimal('2500')
    assert record.absolute_neutrophile_count_units == 'CELLS/UL'
    measurement.refresh_from_db()
    assert measurement.value_as_number == 0


@pytest.mark.django_db
def test_raw_eligibility_benchmark_uses_canonical_counts():
    from omop_core.management.commands.benchmark_trial_eligibility import _fetch_omop_trial_row
    person = PersonFactory()
    MeasurementFactory(person=person, measurement_source_value='751-8', value_as_number=1500, unit_source_value='cells/uL')
    MeasurementFactory(person=person, measurement_source_value='777-3', value_as_number=150.5, unit_source_value='THOU/uL',
                       unit_concept=ConceptFactory(vocabulary__vocabulary_id='UCUM', concept_code='10*3/uL'))
    result = _fetch_omop_trial_row(person.pk)
    assert result['anc_thousand_per_ul'] == Decimal('1.5')
    assert result['platelet_count_thousand_per_ul'] == Decimal('150.5')


@pytest.mark.django_db
@pytest.mark.parametrize('code_only', [False, True])
def test_fhir_upload_retains_source_scale_and_projects_counts(code_only, request):
    from patient_portal.models import Identity
    from patient_portal.tests import _make_vocab_fixtures
    from omop_core.services.concept_cache import concept_cache_clear
    concept_cache_clear()
    request.addfinalizer(concept_cache_clear)
    _make_vocab_fixtures()
    for code in ('751-8', '777-3'):
        ConceptFactory(concept_code=code)
    client = APIClient()
    client.force_authenticate(Identity.objects.create_superuser(email='counts@example.test', password='test'))
    patient_id = 'unit-test-patient'
    resources = [{
        'resourceType': 'Patient', 'id': patient_id,
        'name': [{'family': 'CountScale', 'given': ['Test']}], 'gender': 'female', 'birthDate': '1975-01-01',
    }]
    for code, value in [('751-8', 1500), ('777-3', 150500)]:
        quantity = {'value': value, 'system': 'http://unitsofmeasure.org', 'code': '/uL'}
        if not code_only:
            quantity['unit'] = 'cells/uL'
        resources.append({
            'resourceType': 'Observation', 'id': 'lab-' + code, 'status': 'final',
            'subject': {'reference': 'Patient/' + patient_id}, 'effectiveDateTime': '2024-01-15',
            'category': [{'coding': [{'system': 'http://terminology.hl7.org/CodeSystem/observation-category', 'code': 'laboratory'}]}],
            'code': {'coding': [{'system': 'http://loinc.org', 'code': code}]}, 'valueQuantity': quantity,
        })
    payload = io.BytesIO(json.dumps({'resourceType': 'Bundle', 'type': 'collection', 'entry': [{'resource': r} for r in resources]}).encode())
    payload.name = 'blood-counts.json'
    response = client.post('/api/patient-info/upload_fhir/', {'file': payload}, format='multipart')
    assert response.status_code in (200, 201), response.data
    record = PatientRecord.objects.get(person__family_name='CountScale')
    assert record.anc_thousand_per_ul == Decimal('1.5')
    assert record.platelet_count_thousand_per_ul == Decimal('150.5')
    assert record.platelet_count == 150500
    assert list(Measurement.objects.filter(person=record.person).order_by('measurement_source_value').values_list('value_as_number', flat=True)) == [Decimal('1500'), Decimal('150500')]
    # A corrected source scale must be saved even when its numeric payload
    # is unchanged, otherwise the next refresh keeps the old interpretation.
    resources[1]['valueQuantity'] = {'value': 1500, 'unit': '10*3/uL'}
    payload = io.BytesIO(json.dumps({'resourceType': 'Bundle', 'type': 'collection', 'entry': [{'resource': r} for r in resources]}).encode())
    payload.name = 'blood-counts-corrected.json'
    response = client.post('/api/patient-info/upload_fhir/', {'file': payload}, format='multipart')
    assert response.status_code in (200, 201), response.data
    record.refresh_from_db()
    assert record.anc_thousand_per_ul == Decimal('1500')


# --- Lymphocyte and monocyte counts (#2282) -------------------------------

@pytest.mark.django_db
def test_alc_and_amc_in_cells_per_ul_are_scaled_to_thousands():
    """The issue's repro: ALC 900 /MM3 (with the UCUM unit the ETL read it as) is 0.9, not 900."""
    person = PersonFactory()
    per_ul = ConceptFactory(vocabulary__vocabulary_id='UCUM', concept_code='/uL')
    MeasurementFactory(
        person=person, measurement_concept=ConceptFactory(concept_code='731-0'), measurement_source_value='731-0',
        value_as_number=900, unit_source_value='/MM3', unit_concept=per_ul,
    )
    MeasurementFactory(
        person=person, measurement_concept=ConceptFactory(concept_code='742-7'), measurement_source_value='742-7',
        value_as_number=450, unit_source_value='cells/uL',
    )
    record = refresh_patient_record(person)
    record.refresh_from_db()
    assert record.alc_thousand_per_ul == Decimal('0.9')
    assert record.absolute_lymphocyte_count == 900
    assert record.amc_thousand_per_ul == Decimal('0.5')


@pytest.mark.django_db
@pytest.mark.parametrize('code,field,source,value,expected', [
    ('731-0', 'alc_thousand_per_ul', 'cells/uL', 1500, 1.5),
    ('731-0', 'alc_thousand_per_ul', '10*9/L', 1.5, 1.5),
    ('742-7', 'amc_thousand_per_ul', '/uL', 600, 0.6),
    ('742-7', 'amc_thousand_per_ul', 'K/uL', 0.6, 0.6),
])
@pytest.mark.parametrize('path', ['loinc', 'source-code', 'source-display', 'legacy-name', 'curated'])
def test_alc_amc_projection_paths_preserve_scale_and_source(code, field, source, value, expected, path):
    person = PersonFactory()
    display = 'Absolute Lymphocyte Count' if code == '731-0' else 'Absolute Monocyte Count'
    kwargs = {'person': person, 'value_as_number': value, 'unit_source_value': source}
    if path in ('loinc', 'curated'):
        concept = ConceptFactory(concept_code=code)
        kwargs.update(measurement_concept=concept, measurement_source_value=code)
        if path == 'curated':
            FieldConceptMapping.objects.create(
                field_name=field, concept=concept, vocabulary_id='LOINC', concept_code=code,
                status='approved', omop_table='measurement', source_value=code, value_kind='number',
            )
    elif path == 'source-code':
        kwargs.update(measurement_concept_id=0, measurement_source_value=code)
    elif path == 'source-display':
        kwargs.update(measurement_concept_id=0, measurement_source_value=display)
    else:
        kwargs['measurement_concept'] = ConceptFactory(concept_name=display.lower())
    measurement = MeasurementFactory(**kwargs)
    for _ in range(2):
        record = refresh_patient_record(person)
        record.refresh_from_db()
        assert float(getattr(record, field)) == expected
        if field == 'alc_thousand_per_ul':
            assert record.absolute_lymphocyte_count == expected * 1000
    measurement.refresh_from_db()
    assert float(measurement.value_as_number) == value
    assert measurement.unit_source_value == source


@pytest.mark.django_db
@pytest.mark.parametrize('code,field', [('731-0', 'alc_thousand_per_ul'), ('742-7', 'amc_thousand_per_ul')])
@pytest.mark.parametrize('unit,value', [(None, 900), ('/MM3', 900), ('mg/dL', 900), ('cells/uL', None)])
def test_latest_unknown_alc_amc_leaves_field_empty_and_blocks_older(code, field, unit, value):
    """An unreadable unit without a UCUM concept is unknown, never taken as thousands/µL."""
    person = PersonFactory()
    concept = ConceptFactory(concept_code=code)
    MeasurementFactory(
        person=person, measurement_concept=concept, measurement_source_value=code,
        measurement_date=date(2024, 1, 1), value_as_number=1500, unit_source_value='cells/uL',
    )
    MeasurementFactory(
        person=person, measurement_concept=concept, measurement_source_value=code,
        measurement_date=date(2024, 1, 2), value_as_number=value, unit_source_value=unit,
    )
    record = refresh_patient_record(person)
    assert getattr(record, field) is None
    if field == 'alc_thousand_per_ul':
        assert record.absolute_lymphocyte_count is None


@pytest.mark.django_db
def test_curator_alias_code_is_converted_like_the_canonical_code(lab_field_mappings):
    """26474-7 approved as an alias of alc_thousand_per_ul: a 1500 cells/uL row
    goes through the blood-count selection and derives 1.5 (10^3/uL)."""
    from omop_core.models import SourceCodeConceptMapping
    person = PersonFactory()
    SourceCodeConceptMapping.objects.create(
        source_vocabulary_id='LOINC', source_code='26474-7', status='approved',
        omop_table='measurement', origin='curator',
        target_concept_id=FieldConceptMapping.objects.get(field_name='alc_thousand_per_ul').concept_id,
    )
    MeasurementFactory(
        person=person, measurement_concept=ConceptFactory(concept_code='26474-7'),
        measurement_source_value='26474-7', value_as_number=1500, unit_source_value='cells/uL',
    )
    record = refresh_patient_record(person)
    record.refresh_from_db()
    assert record.alc_thousand_per_ul == Decimal('1.5')
    assert record.absolute_lymphocyte_count == Decimal('1500')
