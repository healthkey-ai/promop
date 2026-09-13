"""Genomics UI/API → OMOP → projection; isolation and lossless round trips."""
import pytest
from rest_framework.test import APIRequestFactory, force_authenticate

from omop_core.models import Concept, Measurement, Note, Observation
from omop_core.services.genomics import FIELDS, list_variants, replace_variants, save_variant
from omop_core.services.patient_record_service import refresh_patient_record
from patient_portal.api.views import PatientRecordViewSet
from patient_portal.models import Identity, PatientUser
from tests.factories import ConceptFactory, MeasurementFactory, ObservationFactory, PatientRecordFactory, PersonFactory

pytestmark = pytest.mark.django_db


@pytest.mark.parametrize('note_id', [1, 10**15])
def test_genomic_overflow_reference_fits_cdm_width_with_large_note_ids(monkeypatch, note_id):
    from datetime import date
    from omop_core.services.genomics import _read_note_text, _store_text

    person = PersonFactory()
    monkeypatch.setattr('omop_core.services.genomics.next_pk', lambda model, field: note_id)
    value = 'Original laboratory narrative. ' * 20
    stored, created_id = _store_text(value, person, date(2026, 9, 1), 0, 7)
    assert created_id == note_id
    assert len(stored) == 60
    assert stored.endswith(f'[note:{note_id}]')
    assert _read_note_text(stored) == value


@pytest.fixture(params=['legacy', 'athena'])
def setup(request):
    person = PersonFactory()
    record = PatientRecordFactory(person=person)
    ConceptFactory(
        vocabulary__vocabulary_id='CDM',
        concept_code='measurement.measurement_id' if request.param == 'legacy' else 'CDM126',
        concept_name='measurement.measurement_id',
    )
    for field, (code, domain) in FIELDS.items():
        if not code.startswith('genomics:'):
            ConceptFactory(concept_code=code, domain__domain_id=domain)
    ConceptFactory(concept_code='81252-9')
    from django.core.management import call_command
    call_command('seed_genomics_catalog')
    staff = Identity.objects.create_user(email='genomics@example.test', password='pw', is_staff=True)
    return person, record, staff


def call(person, user, method, payload=None, variant_id=None):
    name = 'genomics' if variant_id is None else 'genomic_variant'
    request = getattr(APIRequestFactory(), method)(f'/api/v1/patient-records/{person.pk}/genomics/', payload, format='json')
    force_authenticate(request, user=user)
    kwargs = {'pk': str(person.pk)}
    if variant_id is not None:
        kwargs['variant_id'] = str(variant_id)
    return PatientRecordViewSet.as_view({method: name}, **getattr(PatientRecordViewSet, name).kwargs)(request, **kwargs)


def test_full_crud_preserves_all_fields_and_unrelated_facts(setup):
    person, record, staff = setup
    payload = {
        'gene': 'BRCA1', 'variant': 'NM_007294.4:c.68_69delAG',
        'variant_name': '185delAG', 'variant_description': 'Original laboratory narrative. ' * 20,
        'origin': 'germline', 'interpretation': 'Likely pathogenic',
        'test_date': '2026-09-01', 'genome_assembly': 'GRCh38',
        'transcript_reference_sequence_id': 'NM_007294.4',
        'amino_acid_change': 'p.Glu23ValfsTer17', 'variant_category': 'Simple variant',
        'variant_analysis_method_type': 'NGS', 'genomic_source_class': 'Germline',
        'chromosome': '17', 'cytogenetic_location': '17q21.31',
        'genomic_dna_change': 'NC_000017.11:g.43124027_43124028del',
        'allelic_frequency': 43.25, 'allelic_frequency_unit': '%',
    }
    unrelated = MeasurementFactory(person=person, value_as_string='Unrelated lab')
    response = call(person, staff, 'post', payload)
    assert response.status_code == 201, response.data
    variant_id = response.data['id']
    assert {k: response.data[k] for k in payload} == payload
    parent = Measurement.objects.get(pk=variant_id)
    assert parent.measurement_source_value == 'genomics:brca1'
    obs = Observation.objects.get(person=person, observation_source_value='genomics:variant_description')
    # Long text overflows to a linked NOTE; the component holds a truncation marker.
    if len(payload['variant_description']) > 60:
        assert '[note:' in obs.value_as_string
        note = Note.objects.get(pk=int(obs.value_as_string.split('[note:')[1].rstrip(']')))
        assert note.note_text == payload['variant_description']
    else:
        assert obs.value_as_string == payload['variant_description']
    frequency = Measurement.objects.get(person=person, measurement_source_value='81258-6')
    assert float(frequency.value_as_number) == 43.25
    assert frequency.unit_source_value == '%'
    assert frequency.measurement_event_id == variant_id
    refresh_patient_record(person)
    record.refresh_from_db()
    assert record.genetic_mutations == [response.data]
    assert call(person, staff, 'get').data == [response.data]
    assert call(person, staff, 'get', variant_id=variant_id).data == response.data

    second = call(person, staff, 'post', {**payload, 'allelic_frequency': 0})
    assert second.status_code == 201
    assert second.data['id'] != variant_id  # repeat gene / variant is a distinct test
    edited = call(person, staff, 'patch', {'amino_acid_change': '', 'allelic_frequency': 0.5, 'allelic_frequency_unit': '1'}, variant_id)
    assert edited.status_code == 200, edited.data
    assert edited.data['id'] == variant_id
    assert edited.data.get('amino_acid_change') in ('', None)
    assert edited.data['variant_description'] == payload['variant_description']
    assert edited.data['allelic_frequency'] == 0.5
    assert call(person, staff, 'delete', variant_id=variant_id).status_code == 204
    assert [v['id'] for v in list_variants(person)] == [second.data['id']]
    assert Measurement.objects.get(pk=variant_id).is_erroneous
    assert not Measurement.objects.get(pk=unrelated.pk).is_erroneous
    assert not Measurement.objects.filter(person=person, measurement_event_id=variant_id, is_erroneous=False).exists()
    assert not Observation.objects.filter(person=person, observation_event_id=variant_id, is_erroneous=False).exists()
    assert call(person, staff, 'get', variant_id=variant_id).status_code == 404


@pytest.mark.parametrize('bad', [
    {'gene': ''}, {'gene': {}}, {'test_date': '2026-02-30'},
    {'allelic_frequency': -1}, {'allelic_frequency': 101},
    {'allelic_frequency': 'NaN'}, {'allelic_frequency': True},
    {'allelic_frequency': 0.123456}, {'allelic_frequency_unit': 'ppm'},
    {'allelic_frequency': 10, 'allelic_frequency_unit': '1'},
    {'variant_name': ['invalid']}, {'unknown_field': 'lost data'},
])
def test_invalid_data_cannot_partially_write(setup, bad):
    person, _, staff = setup
    response = call(person, staff, 'post', {'gene': 'TP53', **bad})
    assert response.status_code == 400, response.data
    assert not Measurement.objects.filter(person=person).exists()
    assert not Observation.objects.filter(person=person).exists()


def test_imported_variant_can_be_updated_without_duplicate(setup):
    person, _, staff = setup
    old = MeasurementFactory(person=person, measurement_source_value='21636-6', value_as_string='c.123A>G')
    response = call(person, staff, 'patch', {'gene': 'TP53', 'genome_assembly': 'GRCh38'}, old.pk)
    assert response.status_code == 200, response.data
    assert response.data['gene'] == 'TP53'
    assert response.data['variant'] == 'c.123A>G'
    assert [v['id'] for v in list_variants(person)] == [old.pk]


def test_cross_patient_variant_ids_are_rejected(setup):
    person, _, staff = setup
    other = PersonFactory()
    PatientRecordFactory(person=other)
    variant = save_variant(other, {'gene': 'TP53'})
    for method in ('get', 'patch', 'delete'):
        response = call(person, staff, method, {'gene': 'BRCA1'}, variant['id'])
        assert response.status_code == 404
    assert len(list_variants(other)) == 1


def test_unrelated_user_cannot_read_or_write(setup):
    person, _, _ = setup
    outsider = Identity.objects.create_user(email='outsider@example.test', password='pw')
    for method in ('get', 'post'):
        assert call(person, outsider, method, {'gene': 'TP53'}).status_code == 404


def test_patient_can_crud_own_record(setup):
    person, _, _ = setup
    patient = Identity.objects.create_user(email='self@example.test', password='pw')
    PatientUser.objects.create(identity=patient, person=person)
    ConceptFactory(concept_id=32865)
    response = call(person, patient, 'post', {'gene': 'TP53'})
    assert response.status_code == 201, response.data
    assert Measurement.objects.get(pk=response.data['id']).measurement_type_concept_id == 32865
    assert call(person, patient, 'delete', variant_id=response.data['id']).status_code == 204


def test_standard_observation_domain_is_respected(setup):
    person, _, _ = setup
    from tests.factories import DomainFactory
    domain = DomainFactory(domain_id='Observation')
    Concept.objects.filter(concept_code='53037-8').update(domain=domain)
    from omop_core.models import FieldConceptMapping
    FieldConceptMapping.objects.filter(field_name='genetic_mutations.interpretation').update(omop_table='observation')
    result = save_variant(person, {'gene': 'TP53', 'interpretation': 'VUS'})
    row = Observation.objects.get(person=person, observation_source_value='53037-8')
    assert row.observation_concept.domain_id == 'Observation'
    assert row.observation_event_id == result['id']
    assert result['interpretation'] == 'VUS'


def test_missing_loinc_keeps_source_code_and_unmapped_concept(setup):
    person, _, _ = setup
    from omop_core.models import FieldConceptMapping
    FieldConceptMapping.objects.filter(field_name='genetic_mutations.genome_assembly').update(concept_id=0)
    Concept.objects.filter(concept_code='62374-4').delete()
    save_variant(person, {'gene': 'TP53', 'genome_assembly': 'GRCh38'})
    row = Measurement.objects.get(person=person, measurement_source_value='62374-4')
    assert row.measurement_concept_id == 0
    assert row.value_as_string == 'GRCh38'


def test_linked_imported_gene_and_component_without_source_code(setup):
    person, _, staff = setup
    parent = MeasurementFactory(person=person, measurement_source_value='81252-9', value_as_string='c.123A>G')
    link = Concept.objects.get(vocabulary_id='CDM', concept_name='measurement.measurement_id')
    ObservationFactory(person=person, observation_concept=Concept.objects.get(concept_code='48018-6'),
        observation_event_id=parent.pk, obs_event_field_concept=link, value_as_string='BRCA2')
    amino = MeasurementFactory(person=person, measurement_concept=Concept.objects.get(concept_code='48005-3'),
        measurement_source_value=None, measurement_event_id=parent.pk, meas_event_field_concept=link,
        value_as_string='p.Gly12Val')
    assert list_variants(person)[0]['gene'] == 'BRCA2'
    result = call(person, staff, 'patch', {'amino_acid_change': ''}, parent.pk)
    assert result.status_code == 200, result.data
    assert result.data.get('amino_acid_change') in (None, '')
    amino.refresh_from_db()
    assert amino.is_erroneous


def test_component_links_preserve_patient_vocabulary_and_event_type_isolation(setup):
    from omop_core.services.genomics import delete_variant

    person, _, _ = setup
    saved = save_variant(person, {'gene': 'TP53', 'laboratory': 'Correct laboratory'})
    link = Concept.objects.get(vocabulary_id='CDM', concept_name='measurement.measurement_id')
    wrong_table = ConceptFactory(vocabulary__vocabulary_id='CDM', concept_code='CDM999',
                                 concept_name='observation.observation_id')
    wrong_vocabulary = ConceptFactory(vocabulary__vocabulary_id='Local',
                                      concept_code='measurement.measurement_id',
                                      concept_name='measurement.measurement_id')
    nonstandard = ConceptFactory(vocabulary__vocabulary_id='CDM', concept_code='LOCAL-MEAS',
                                 concept_name='measurement.measurement_id', standard_concept=None)
    unrelated = []
    for event, owner in [(wrong_table, person), (wrong_vocabulary, person),
                         (nonstandard, person), (link, PersonFactory())]:
        unrelated.append(ObservationFactory(
            person=owner, observation_event_id=saved['id'], obs_event_field_concept=event,
            observation_source_value=FIELDS['laboratory'][0], value_as_string='Unrelated laboratory',
        ))
        unrelated.append(MeasurementFactory(
            person=owner, measurement_event_id=saved['id'], meas_event_field_concept=event,
            measurement_source_value=FIELDS['laboratory'][0], value_as_string='Unrelated laboratory',
        ))
    assert list_variants(person)[0]['laboratory'] == 'Correct laboratory'
    edited = save_variant(person, {'laboratory': 'Updated laboratory'}, saved['id'])
    assert edited['laboratory'] == 'Updated laboratory'
    delete_variant(person, saved['id'])
    for row in unrelated:
        row.refresh_from_db()
        assert not row.is_erroneous


def test_legacy_list_patch_rolls_back_and_handles_empty_list(setup):
    person, _, _ = setup
    ConceptFactory(concept_id=32865)
    initial = save_variant(person, {'gene': 'TP53', 'variant': 'c.123A>G'})
    from rest_framework.exceptions import ValidationError
    with pytest.raises(ValidationError):
        replace_variants(person, [{**initial, 'gene': 'BRCA1'}, {'gene': ''}])
    assert list_variants(person) == [initial]
    replace_variants(person, [])
    assert list_variants(person) == []


def test_analyst_cannot_mutate_visible_patient(setup, monkeypatch):
    person, _, _ = setup
    analyst = Identity.objects.create_user(email='analyst@example.test', password='pw')
    monkeypatch.setattr('omop_core.authorization.can_access_patient', lambda *_: True)
    monkeypatch.setattr('omop_core.authorization.can_write_patient', lambda *_: False)
    variant = save_variant(person, {'gene': 'TP53'})
    assert call(person, analyst, 'get').status_code == 200
    for method in ('post', 'patch', 'delete'):
        response = call(person, analyst, method, {'gene': 'BRCA1'}, None if method == 'post' else variant['id'])
        assert response.status_code == 403
    assert list_variants(person) == [variant]


# --- Finding status (§2) ---


def test_status_defaults_to_present_on_new_write(setup):
    """A finding written without explicit status defaults to present."""
    person, _, _ = setup
    variant = save_variant(person, {'gene': 'TP53'})
    assert variant['status'] == 'present'
    # Status component is stored in OMOP (table depends on LOINC domain).
    assert (
        Measurement.objects.filter(
            person=person, measurement_event_id=variant['id'],
            measurement_source_value='genomics:status',
        ).exists()
        or Observation.objects.filter(
            person=person, observation_event_id=variant['id'],
            observation_source_value='genomics:status',
        ).exists()
    )


def test_status_defaults_to_present_for_legacy_rows(setup):
    """Legacy Measurement rows with no status component project status=present."""
    person, _, _ = setup
    parent = MeasurementFactory(person=person, measurement_source_value='81252-9',
        value_as_string='c.123A>G', qualifier_source_value='TP53')
    variants = list_variants(person)
    assert len(variants) == 1
    assert variants[0]['status'] == 'present'


@pytest.mark.parametrize('status', ['present', 'absent', 'indeterminate'])
def test_status_round_trips_through_omop(setup, status):
    """Each status value stores in OMOP and reads back correctly."""
    person, record, _ = setup
    payload = {'gene': 'TP53', 'status': status}
    if status != 'absent':
        payload['variant'] = 'c.123A>G'
    variant = save_variant(person, payload)
    assert variant['status'] == status
    refresh_patient_record(person)
    record.refresh_from_db()
    projected = record.genetic_mutations
    assert len(projected) == 1
    assert projected[0]['status'] == status


def test_status_rejects_invalid_values(setup):
    """Only present, absent and indeterminate are accepted."""
    person, _, _ = setup
    from rest_framework.exceptions import ValidationError
    with pytest.raises(ValidationError, match='status'):
        save_variant(person, {'gene': 'TP53', 'status': 'unknown'})
    with pytest.raises(ValidationError, match='status'):
        save_variant(person, {'gene': 'TP53', 'status': 'not_tested'})
    assert not Measurement.objects.filter(person=person).exists()


def test_absent_finding_writes_parent_and_contextual_components(setup):
    """An absent finding writes the parent and contextual components."""
    person, _, _ = setup
    payload = {
        'gene': 'TP53', 'status': 'absent',
        'variant_analysis_method_type': 'FISH',
        'specimen_type': 'blood', 'laboratory': 'MGH',
    }
    variant = save_variant(person, payload)
    assert variant['status'] == 'absent'
    assert variant['variant_analysis_method_type'] == 'FISH'
    assert variant['specimen_type'] == 'blood'
    assert variant['laboratory'] == 'MGH'
    parent = Measurement.objects.get(pk=variant['id'])
    assert not parent.is_erroneous


def test_absent_finding_has_no_variant_level_components(setup):
    """Variant-level components are not stored on absent findings."""
    person, _, _ = setup
    variant = save_variant(person, {'gene': 'TP53', 'status': 'absent'})
    assert variant.get('amino_acid_change') in ('', None)
    assert variant.get('allelic_frequency') is None
    assert variant.get('genomic_dna_change') in ('', None)
    assert variant.get('transcript_reference_sequence_id') in ('', None)


def test_variant_level_components_rejected_on_absent(setup):
    """Supplying variant-level components on an absent finding is an error."""
    person, _, _ = setup
    from rest_framework.exceptions import ValidationError
    with pytest.raises(ValidationError, match='amino_acid_change'):
        save_variant(person, {'gene': 'TP53', 'status': 'absent', 'amino_acid_change': 'p.R175H'})
    with pytest.raises(ValidationError, match='allelic_frequency'):
        save_variant(person, {'gene': 'TP53', 'status': 'absent', 'allelic_frequency': 50.0})
    with pytest.raises(ValidationError, match='genomic_dna_change'):
        save_variant(person, {'gene': 'TP53', 'status': 'absent', 'genomic_dna_change': 'g.123A>G'})
    assert not Measurement.objects.filter(person=person).exists()


def test_empty_list_writes_no_omop_facts(setup):
    """An empty genomics list leaves the marker unknown; no OMOP facts written."""
    person, record, staff = setup
    request = APIRequestFactory().patch(
        f'/api/v1/patient-records/{person.pk}/',
        {'genomics_tp53': []}, format='json',
    )
    force_authenticate(request, user=staff)
    response = PatientRecordViewSet.as_view({'patch': 'partial_update'})(request, pk=person.person_id)
    assert response.status_code == 200
    assert response.data['genomics_tp53'] == []
    assert not Measurement.objects.filter(person=person).exists()


def test_absent_finding_via_named_list_patch(setup):
    """Recording absence requires an explicit entry with status=absent."""
    person, record, staff = setup
    request = APIRequestFactory().patch(
        f'/api/v1/patient-records/{person.pk}/',
        {'genomics_del17p': [{'status': 'absent', 'variant_analysis_method_type': 'FISH'}]},
        format='json',
    )
    force_authenticate(request, user=staff)
    response = PatientRecordViewSet.as_view({'patch': 'partial_update'})(request, pk=person.person_id)
    assert response.status_code == 200
    entries = response.data['genomics_del17p']
    assert len(entries) == 1
    assert entries[0]['status'] == 'absent'


# --- Clone fraction (§5) ---


@pytest.mark.parametrize('value,unit,expected', [
    (50.0, '%', 50.0),
    (0.5, '1', 0.5),
    (0, '%', 0.0),
    (0.0, '1', 0.0),
])
def test_clone_fraction_round_trips(setup, value, unit, expected):
    """Clone fraction stores and reads back with both unit conventions."""
    person, _, _ = setup
    variant = save_variant(person, {
        'gene': 'TP53', 'variant_analysis_method_type': 'FISH',
        'clone_fraction': value, 'clone_fraction_unit': unit,
    })
    assert variant['clone_fraction'] == expected
    assert variant['clone_fraction_unit'] == unit


def test_clone_fraction_preserves_zero(setup):
    """Zero clone fraction is meaningful and not treated as missing."""
    person, _, _ = setup
    variant = save_variant(person, {
        'gene': 'TP53', 'clone_fraction': 0, 'clone_fraction_unit': '%',
    })
    assert variant['clone_fraction'] == 0.0
    row = Measurement.objects.get(person=person, measurement_source_value='genomics:clone_fraction')
    assert float(row.value_as_number) == 0.0


def test_clone_fraction_rejects_six_decimal_places(setup):
    """Clone fraction has a maximum of five decimal places."""
    person, _, _ = setup
    from rest_framework.exceptions import ValidationError
    with pytest.raises(ValidationError, match='clone_fraction'):
        save_variant(person, {'gene': 'TP53', 'clone_fraction': 0.123456, 'clone_fraction_unit': '1'})


def test_clone_fraction_not_interchangeable_with_allelic_frequency(setup):
    """Clone fraction and allelic frequency are distinct components."""
    person, _, _ = setup
    variant = save_variant(person, {
        'gene': 'TP53', 'allelic_frequency': 45.0, 'allelic_frequency_unit': '%',
        'clone_fraction': 80.0, 'clone_fraction_unit': '%',
    })
    assert variant['allelic_frequency'] == 45.0
    assert variant['clone_fraction'] == 80.0
    af_row = Measurement.objects.get(person=person, measurement_source_value='81258-6')
    cf_row = Measurement.objects.get(person=person, measurement_source_value='genomics:clone_fraction')
    assert float(af_row.value_as_number) == 45.0
    assert float(cf_row.value_as_number) == 80.0


# --- BIDMC component alignment (§7) ---


def test_transcript_dna_change_round_trips(setup):
    """Transcript DNA change (48004-6) stores and reads back separately from genomic_dna_change."""
    person, _, _ = setup
    variant = save_variant(person, {
        'gene': 'TP53', 'variant': 'c.215C>G',
        'transcript_dna_change': 'c.215C>G',
        'genomic_dna_change': 'NC_000017.11:g.7674220C>G',
    })
    assert variant['transcript_dna_change'] == 'c.215C>G'
    assert variant['genomic_dna_change'] == 'NC_000017.11:g.7674220C>G'
    # The raw variant string is preserved alongside the coded value.
    assert variant['variant'] == 'c.215C>G'


def test_transcript_and_genomic_dna_changes_are_separate(setup):
    """Writing one DNA change does not populate the other."""
    person, _, _ = setup
    variant = save_variant(person, {
        'gene': 'TP53', 'transcript_dna_change': 'c.215C>G',
    })
    assert variant['transcript_dna_change'] == 'c.215C>G'
    assert variant.get('genomic_dna_change') in ('', None)


def test_coverage_depth_round_trips(setup):
    """Coverage depth stores as a numeric value."""
    person, _, _ = setup
    variant = save_variant(person, {
        'gene': 'TP53', 'variant': 'c.215C>G',
        'coverage_depth': 250,
    })
    assert variant['coverage_depth'] == 250.0


def test_amino_acid_change_and_type_are_separate(setup):
    """amino_acid_change and amino_acid_change_type are distinct components."""
    person, _, _ = setup
    variant = save_variant(person, {
        'gene': 'TP53', 'variant': 'c.215C>G',
        'amino_acid_change': 'p.Pro72Arg',
        'amino_acid_change_type': 'missense',
    })
    assert variant['amino_acid_change'] == 'p.Pro72Arg'
    assert variant['amino_acid_change_type'] == 'missense'


def test_long_text_lands_in_note_and_round_trips(setup):
    """Text exceeding CDM column width is stored in a NOTE row and round-trips."""
    person, _, _ = setup
    long_text = 'A' * 200  # Well beyond the 60-char CDM limit.
    variant = save_variant(person, {
        'gene': 'TP53', 'variant_description': long_text,
    })
    assert variant['variant_description'] == long_text
    obs = Observation.objects.get(person=person, observation_source_value='genomics:variant_description')
    assert '[note:' in obs.value_as_string
    note_pk = int(obs.value_as_string.split('[note:')[1].rstrip(']'))
    note = Note.objects.get(pk=note_pk)
    assert note.note_text == long_text
    assert note.person == person


def test_chromosome_resolves_at_48000_4(setup):
    """Chromosome stays at LOINC 48000-4, not 73822-9."""
    person, _, _ = setup
    variant = save_variant(person, {'gene': 'TP53', 'chromosome': '17'})
    row = Measurement.objects.filter(
        person=person, measurement_source_value='48000-4',
    ).first()
    assert row is not None
    assert row.value_as_string == '17'


# --- Observed vs derived markers (§4) ---


def test_asserted_complex_karyotype_has_provenance(setup):
    """Asserted complex karyotype stores as a finding with provenance=asserted."""
    person, record, staff = setup
    request = APIRequestFactory().patch(
        f'/api/v1/patient-records/{person.pk}/',
        {'genomics_complex_karyotype': [{'variant': '3 abnormalities'}]},
        format='json',
    )
    force_authenticate(request, user=staff)
    response = PatientRecordViewSet.as_view({'patch': 'partial_update'})(request, pk=person.person_id)
    assert response.status_code == 200
    entries = response.data['genomics_complex_karyotype']
    assert len(entries) == 1
    assert entries[0]['provenance'] == 'asserted'


def test_derived_path_stub_returns_no_value(setup):
    """The derived path is a stub and returns no value."""
    from omop_core.services.genomics_catalog import _derive_complex_karyotype, _derive_complex_karyotype_excl_t1114
    assert _derive_complex_karyotype([]) is None
    assert _derive_complex_karyotype_excl_t1114([]) is None


def test_asserted_finding_wins_over_derived(setup):
    """When an asserted finding exists, derived is not computed."""
    person, record, staff = setup
    request = APIRequestFactory().patch(
        f'/api/v1/patient-records/{person.pk}/',
        {'genomics_complex_karyotype': [{'variant': '5 abnormalities'}]},
        format='json',
    )
    force_authenticate(request, user=staff)
    response = PatientRecordViewSet.as_view({'patch': 'partial_update'})(request, pk=person.person_id)
    assert response.status_code == 200
    entries = response.data['genomics_complex_karyotype']
    assert len(entries) == 1
    assert entries[0]['provenance'] == 'asserted'
    # No derived entry alongside the asserted one.
    assert all(e['provenance'] == 'asserted' for e in entries)


def test_no_asserted_complex_karyotype_returns_empty(setup):
    """With no asserted finding and stub derivation, the list is empty."""
    person, record, staff = setup
    refresh_patient_record(person)
    record.refresh_from_db()
    assert record.genomics_complex_karyotype == []
