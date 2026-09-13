import pytest
from django.core.management import call_command
from rest_framework.test import APIClient

from omop_core.models import FieldConceptMapping, Measurement, Observation
from omop_core.services.genomics import list_variants, save_variant
from omop_core.services.genomics_catalog import markers, patient_fields
from omop_core.services.patient_record_service import refresh_patient_record
from tests.test_genomics_crud import setup  # noqa: F401 — shared vocab/patient fixture

pytestmark = pytest.mark.django_db


def client_for(staff):
    client = APIClient()
    client.force_authenticate(staff)
    return client


@pytest.mark.parametrize('disease,expected', [
    ('BC', {'brca1', 'brca2', 'pik3ca', 'tp53', 'esr1', 'palb1'}),
    ('FL', {'bcl2', 'ezh2', 'kmt2d', 'crebbp', 'bcl6'}),
    ('MM', {'tp53', 'kras', 'nras', 'braf', 'del17p', 't414', 't1114', 'gain1q'}),
    ('MCL', {'tp53', 'notch1', 'notch2', 'nsd2', 'ccnd1', 'bcl2_amplification'}),
    ('CLL', {'tp53', 'notch1', 'sf3b1', 'atm', 'del11q', 'del13q', 'trisomy12'}),
])
def test_catalog_is_disease_specific_without_patient_facts(setup, disease, expected):
    person, _, staff = setup
    response = client_for(staff).get(f'/api/v1/patient-records/{person.pk}/genomics-catalog/', {'disease': disease})
    assert response.status_code == 200, response.data
    assert expected <= {m['key'] for m in response.data['markers']}
    assert all(m['writable'] for m in response.data['markers'])
    assert not Measurement.objects.filter(person=person).exists()
    assert not Observation.objects.filter(person=person).exists()


def test_named_patientrecord_edits_project_both_directions(setup):
    person, record, staff = setup
    client = client_for(staff)
    response = client.patch(f'/api/v1/patient-records/{person.pk}/', {
        'genomics_brca1': [{'variant': 'c.68_69delAG', 'origin': 'germline', 'interpretation': 'Pathogenic',
                           'assessment': 'present', 'report_id': 'report-A', 'specimen_id': 'sample-A'}],
        'genomics_tp53': [{'variant': 'p.R175H', 'origin': 'somatic'}],
    }, format='json')
    assert response.status_code == 200, response.data
    assert response.data['genomics_brca1'][0]['origin'] == 'germline'
    brca = Measurement.objects.get(person=person, measurement_source_value='genomics:brca1')
    origin = Observation.objects.get(person=person, observation_event_id=brca.pk, observation_source_value='genomics:origin', is_erroneous=False)
    assert origin.value_as_string == 'germline'
    refresh_patient_record(person)
    record.refresh_from_db()
    assert record.genomics_brca1 == response.data['genomics_brca1']
    assert record.genomics_tp53[0]['variant'] == 'p.R175H'
    changed = {**record.genomics_brca1[0], 'interpretation': 'VUS'}
    response = client.patch(f'/api/v1/patient-records/{person.pk}/', {'genomics_brca1': [changed]}, format='json')
    assert response.status_code == 200, response.data
    assert response.data['genomics_brca1'][0]['id'] == brca.pk
    assert response.data['genomics_brca1'][0]['interpretation'] == 'VUS'
    assert len(response.data['genomics_tp53']) == 1
    response = client.patch(f'/api/v1/patient-records/{person.pk}/', {'genomics_brca1': []}, format='json')
    assert response.status_code == 200
    assert response.data['genomics_brca1'] == []
    assert len(response.data['genomics_tp53']) == 1


def test_repeated_findings_and_disease_changes_preserve_data(setup):
    person, record, staff = setup
    one = save_variant(person, {'gene': 'BRCA1', 'variant': 'c.123A>G', 'report_id': 'one'})
    two = save_variant(person, {'gene': 'BRCA1', 'variant': 'c.123A>G', 'report_id': 'two'})
    record.refresh_from_db()
    assert {v['id'] for v in record.genomics_brca1} == {one['id'], two['id']}
    # A new disease catalog is presentation only.
    client_for(staff).get(f'/api/v1/patient-records/{person.pk}/genomics-catalog/', {'disease': 'CLL'})
    assert len(list_variants(person)) == 2


@pytest.mark.parametrize('field', ['genomics_brca1', 'genetic_mutations.origin'])
def test_unapproved_mappings_block_writes_atomically(setup, field):
    person, _, staff = setup
    FieldConceptMapping.objects.filter(field_name=field).update(status='rejected')
    response = client_for(staff).patch(f'/api/v1/patient-records/{person.pk}/', {
        'genomics_brca1': [{'variant': 'c.123A>G', 'origin': 'germline'}],
    }, format='json')
    assert response.status_code == 400, response.data
    assert list_variants(person) == []


def test_seed_is_complete_idempotent_and_preserves_reviewer_decisions(setup):
    expected = set(patient_fields())
    assert set(FieldConceptMapping.objects.filter(field_name__in=expected, status='approved').values_list('field_name', flat=True)) == expected
    count = FieldConceptMapping.objects.count()
    FieldConceptMapping.objects.filter(field_name='genomics_brca1').update(status='rejected', notes='Expert correction')
    call_command('seed_genomics_catalog')
    assert FieldConceptMapping.objects.count() == count
    assert FieldConceptMapping.objects.get(field_name='genomics_brca1').notes == 'Expert correction'
    assert FieldConceptMapping.objects.get(field_name='genomics_brca1').status == 'rejected'


def test_every_catalog_marker_has_a_real_patientrecord_projection(setup):
    _, record, _ = setup
    for marker in markers():
        assert getattr(record, marker['field_name']) == []


def test_cannot_move_an_id_to_a_different_priority_field(setup):
    person, _, staff = setup
    other = save_variant(person, {'gene': 'TP53', 'variant': 'p.R175H'})
    response = client_for(staff).patch(f'/api/v1/patient-records/{person.pk}/', {'genomics_brca1': [other]}, format='json')
    assert response.status_code == 400


def test_curated_source_keys_control_parent_and_component_writes(setup):
    person, _, _ = setup
    FieldConceptMapping.objects.filter(field_name='genomics_brca1').update(source_value='reviewed:brca1')
    FieldConceptMapping.objects.filter(field_name='genetic_mutations.origin').update(source_value='reviewed:origin')
    result = save_variant(person, {'gene': 'BRCA1', 'variant': 'c.123A>G', 'origin': 'germline'})
    assert Measurement.objects.get(pk=result['id']).measurement_source_value == 'reviewed:brca1'
    assert Observation.objects.get(person=person, observation_source_value='reviewed:origin').value_as_string == 'germline'
    assert result['origin'] == 'germline'


def test_withdrawn_mapping_preserves_reads_but_blocks_clear_and_delete(setup):
    person, _, staff = setup
    FieldConceptMapping.objects.filter(field_name='genetic_mutations.origin').update(source_value='reviewed:origin')
    result = save_variant(person, {'gene': 'BRCA1', 'variant': 'c.123A>G', 'origin': 'germline'})
    FieldConceptMapping.objects.filter(field_name='genetic_mutations.origin').update(status='rejected')
    client = client_for(staff)
    url = f'/api/v1/patient-records/{person.pk}/genomics/{result["id"]}/'
    assert client.get(url).data['origin'] == 'germline'
    assert client.patch(url, {'origin': ''}, format='json').status_code == 400
    assert client.get(url).data['origin'] == 'germline'
    FieldConceptMapping.objects.filter(field_name='genomics_brca1').update(status='rejected')
    assert client.delete(url).status_code == 400
    assert client.get(url).status_code == 200


def test_component_mappings_are_discoverable_and_curatable(setup):
    from omop_core.services.field_descriptor import get_all_field_descriptors
    from patient_portal.api.serializers import FieldConceptMappingSerializer
    descriptor = next(d for d in get_all_field_descriptors() if d['field_name'] == 'genetic_mutations.origin')
    assert descriptor['tab'] == 'genomics'
    assert descriptor['mapping']['status'] == 'approved'
    mapping = FieldConceptMapping.objects.get(field_name='genetic_mutations.origin')
    serializer = FieldConceptMappingSerializer(mapping, data={'status': 'rejected'}, partial=True)
    assert serializer.is_valid(), serializer.errors


@pytest.mark.parametrize('assessment', ['absent', 'not_tested', 'no_call', 'indeterminate'])
def test_nonpositive_assessments_do_not_become_detected_markers(setup, assessment):
    person, record, _ = setup
    saved = save_variant(person, {'gene': 'TP53', 'variant': 'p.R175H',
        'interpretation': 'Pathogenic', 'assessment': assessment})
    record.refresh_from_db()
    assert record.genomics_tp53[0]['assessment'] == assessment
    assert record.tp53_disruption is not True
    assert not record.molecular_markers
    save_variant(person, {'assessment': 'present'}, saved['id'])
    record.refresh_from_db()
    assert record.tp53_disruption is True
    assert 'TP53' in record.molecular_markers


@pytest.mark.parametrize('key,value_kind', [
    ('status', 'string'), ('clone_fraction', 'number'),
    ('transcript_dna_change', 'string'), ('coverage_depth', 'number'),
    ('amino_acid_change_type', 'string'),
])
def test_later_components_are_discoverable_editable_and_rejectable(setup, key, value_kind):
    from omop_core.services.field_descriptor import get_all_field_descriptors
    from patient_portal.api.serializers import FieldConceptMappingSerializer

    field = 'genetic_mutations.' + key
    descriptor = next(d for d in get_all_field_descriptors() if d['field_name'] == field)
    assert descriptor['tab'] == 'genomics'
    assert descriptor['field_type'] == value_kind
    mapping = FieldConceptMapping.objects.get(field_name=field)
    serializer = FieldConceptMappingSerializer(mapping, data={
        'status': 'rejected', 'source_value': 'reviewed:' + key, 'notes': 'Curator decision',
    }, partial=True)
    assert serializer.is_valid(), serializer.errors
    serializer.save()
    call_command('seed_genomics_catalog')
    mapping.refresh_from_db()
    assert mapping.status == 'rejected'
    assert mapping.source_value == 'reviewed:' + key
    assert mapping.notes == 'Curator decision'


def test_effective_registry_is_complete_and_does_not_mutate_frozen_catalog(setup):
    from copy import deepcopy
    from omop_core.services.genomics import FIELDS
    from omop_core.services.genomics_catalog import catalog
    from omop_core.services.genomics_components import components
    from omop_core.services.field_descriptor import get_all_field_descriptors
    from patient_portal.api.serializers import FieldConceptMappingSerializer

    frozen = deepcopy(catalog())
    registry = components()
    fields = {'genetic_mutations.' + row['key'] for row in registry}
    assert len(registry) == len(fields) == 31
    assert fields == {'genetic_mutations.' + key for key in FIELDS}
    assert fields <= {row['field_name'] for row in get_all_field_descriptors()}
    assert fields <= set(FieldConceptMapping.objects.values_list('field_name', flat=True))
    registry[0]['table'] = 'modified by caller'
    assert catalog() == frozen
    assert components()[0]['table'] != 'modified by caller'
    serializer = FieldConceptMappingSerializer(data={'field_name': 'genetic_mutations.unknown'})
    assert not serializer.is_valid()
    assert 'field_name' in serializer.errors
