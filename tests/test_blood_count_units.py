from datetime import date
from decimal import Decimal
import io
import json

import pytest
from rest_framework.test import APIClient

from omop_core.models import FieldConceptMapping, Measurement, PatientRecord
from omop_core.services.clinical_units import blood_count_to_canonical
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
    ('751-8', 'anc_thousand_per_ul', 'absolute_neutrophile_count', '10*3/uL', 'cells/uL', 1500, 1.5),
    ('751-8', 'anc_thousand_per_ul', 'absolute_neutrophile_count', '10*3/uL', '10*9/L', 1.5, 1.5),
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
        assert float(getattr(record, alias)) == (expected * 1000 if code == '777-3' else expected)
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


@pytest.mark.django_db
def test_ucum_unit_concept_is_used_only_when_source_unit_absent():
    person = PersonFactory()
    measurement = MeasurementFactory(
        person=person, measurement_source_value='751-8', value_as_number=1500,
        unit_concept=ConceptFactory(vocabulary__vocabulary_id='UCUM', concept_code='/uL'),
    )
    assert refresh_patient_record(person).anc_thousand_per_ul == Decimal('1.5')
    Measurement.objects.filter(pk=measurement.pk).update(unit_source_value='unknown')
    assert refresh_patient_record(person).anc_thousand_per_ul is None


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
    measurement.refresh_from_db()
    assert measurement.value_as_number == 0


@pytest.mark.django_db
def test_raw_eligibility_benchmark_uses_canonical_counts():
    from omop_core.management.commands.benchmark_trial_eligibility import _fetch_omop_trial_row
    person = PersonFactory()
    MeasurementFactory(person=person, measurement_source_value='751-8', value_as_number=1500, unit_source_value='cells/uL')
    MeasurementFactory(person=person, measurement_source_value='777-3', value_as_number=150500, unit_source_value='cells/uL')
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
