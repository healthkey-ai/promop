"""Asserted projection edits, disabled derivations and truthful OMOP sources."""
import pytest
from django.utils.dateparse import parse_datetime
from rest_framework.test import APIClient

from omop_core.models import Measurement, Observation, PatientRecord, PersonalRepresentative
from omop_core.services.genomics import list_variants, save_variant
from omop_core.services.genomics_catalog import DerivedFinding, project_priority_variants
from omop_core.services.patient_record_service import refresh_patient_record
from omop_core.services.provenance_service import get_field_provenance
from patient_portal.models import Identity, PatientUser
from tests.factories import ConceptFactory, MeasurementFactory, ObservationFactory, PersonFactory
from tests.test_genomics_crud import setup  # noqa: F401

pytestmark = pytest.mark.django_db


def client_for(user):
    client = APIClient()
    client.force_authenticate(user)
    return client


@pytest.mark.parametrize('marker', ['complex_karyotype', 'complex_karyotype_excl_t1114'])
def test_asserted_projection_can_be_edited_through_named_and_dedicated_routes(setup, marker):
    person, record, staff = setup
    client = client_for(staff)
    url = f'/api/v1/patient-records/{person.pk}/'
    field = 'genomics_' + marker
    created = client.patch(url, {field: [{'variant': 'Reported abnormalities'}]}, format='json')
    assert created.status_code == 200, created.data
    entry = created.data[field][0]
    assert entry['provenance'] == 'asserted'
    assert created.data['genetic_mutations'][0] == entry
    edited = client.patch(url, {field: [{**entry, 'interpretation': 'Original report reviewed'}]}, format='json')
    assert edited.status_code == 200, edited.data
    assert edited.data[field][0]['id'] == entry['id']
    result = client.patch(f"{url}genomics/{entry['id']}/", {
        **edited.data[field][0], 'laboratory': 'Reviewed laboratory',
    }, format='json')
    assert result.status_code == 200, result.data
    assert result.data['provenance'] == 'asserted'
    assert result.data['laboratory'] == 'Reviewed laboratory'
    record.refresh_from_db()
    assert getattr(record, field) == record.genetic_mutations == [result.data]


@pytest.mark.parametrize('route', ['dedicated', 'general', 'named'])
@pytest.mark.parametrize('metadata', [
    {'provenance': 'derived'}, {'derivation_version': 'client-rule'},
    {'derived_at': '2026-09-13T10:00:00Z'},
])
def test_client_derived_metadata_cannot_create_or_modify_facts(setup, route, metadata):
    person, _, staff = setup
    client = client_for(staff)
    url = f'/api/v1/patient-records/{person.pk}/'
    original = save_variant(person, {'gene': 'TP53', 'variant': 'Original finding'})
    before = Measurement.objects.filter(person=person).count(), Observation.objects.filter(person=person).count()
    for payload in ({'gene': 'TP53', **metadata}, {**original, **metadata, 'variant': 'Forged finding'}):
        if route == 'dedicated':
            endpoint = f"{url}genomics/{original['id']}/" if 'id' in payload else f'{url}genomics/'
            response = (client.patch if 'id' in payload else client.post)(endpoint, payload, format='json')
        else:
            field = 'genetic_mutations' if route == 'general' else 'genomics_tp53'
            response = client.patch(url, {field: [payload]}, format='json')
        assert response.status_code == 400, response.data
        assert list_variants(person) == [original]
        assert (Measurement.objects.filter(person=person).count(), Observation.objects.filter(person=person).count()) == before


def test_new_assertion_cannot_supply_server_metadata(setup):
    person, _, staff = setup
    response = client_for(staff).post(f'/api/v1/patient-records/{person.pk}/genomics/', {
        'gene': 'TP53', 'provenance': 'asserted',
    }, format='json')
    assert response.status_code == 400
    assert list_variants(person) == []


@pytest.mark.parametrize('marker', ['complex_karyotype', 'complex_karyotype_excl_t1114'])
def test_synthetic_derivation_is_versioned_and_cannot_feed_back_into_inputs(setup, monkeypatch, marker):
    person, record, _ = setup
    saved = save_variant(person, {'gene': 'TP53', 'variant': 'Source finding'})
    field = 'genomics_' + marker
    prior = {'gene': 'Prior projection', 'marker_key': marker, 'provenance': 'derived'}
    PatientRecord.objects.filter(pk=record.pk).update(genetic_mutations=[prior], **{field: [prior]})
    observed = []

    def synthetic(findings):
        observed.append(findings)
        return DerivedFinding({'gene': 'KARYOTYPE', 'status': 'indeterminate'}, 'synthetic-test-v1')

    monkeypatch.setitem(__import__('omop_core.services.genomics_catalog', fromlist=['_DERIVATION_FUNCTIONS'])._DERIVATION_FUNCTIONS, marker, synthetic)
    before = Measurement.objects.filter(person=person).count(), Observation.objects.filter(person=person).count()
    for _ in range(2):
        refreshed = refresh_patient_record(person)
        derived = getattr(refreshed, field)[0]
        assert derived['provenance'] == 'derived'
        assert derived['derivation_version'] == 'synthetic-test-v1'
        assert parse_datetime(derived['derived_at']) is not None
        assert 'id' not in derived
        assert refreshed.genetic_mutations == [saved]
    assert all(rows == [saved] for rows in observed)
    assert (Measurement.objects.filter(person=person).count(), Observation.objects.filter(person=person).count()) == before
    assert get_field_provenance(person, field)['source_rows'] == []
    # Direct boundary callers also cannot supply previous derived rows as inputs.
    projected = project_priority_variants([saved, prior])
    assert projected[field][0]['derivation_version'] == 'synthetic-test-v1'
    assert observed[-1] == [saved]


@pytest.mark.parametrize('marker', ['complex_karyotype', 'complex_karyotype_excl_t1114'])
def test_assertion_of_any_status_bypasses_derivation(setup, monkeypatch, marker):
    from omop_core.services import genomics_catalog
    person, record, _ = setup
    marker_info = genomics_catalog.patient_fields()['genomics_' + marker]
    saved = save_variant(person, {'gene': marker_info['gene'], 'marker_key': marker, 'status': 'absent'})

    def unexpected(_):
        pytest.fail('An asserted marker must bypass derivation')

    monkeypatch.setitem(genomics_catalog._DERIVATION_FUNCTIONS, marker, unexpected)
    record = refresh_patient_record(person)
    assert getattr(record, 'genomics_' + marker) == [saved]


@pytest.mark.parametrize('actor,expected', [('staff', 32817), ('self', 32865), ('representative', 32865)])
@pytest.mark.parametrize('route', ['dedicated', 'general', 'named'])
def test_actor_type_is_consistent_across_create_and_edit_paths(setup, actor, expected, route):
    person, _, staff = setup
    ConceptFactory(concept_id=32865)
    user = staff
    if actor != 'staff':
        user = Identity.objects.create_user(email=f'{actor}@example.test', password='pw')
        if actor == 'self':
            PatientUser.objects.create(identity=user, person=person)
        else:
            PersonalRepresentative.objects.create(representative=user, person_id=person.pk,
                                                   relationship='caregiver', verification_status='VERIFIED')
    client = client_for(user)
    url = f'/api/v1/patient-records/{person.pk}/'
    payload = {'gene': 'TP53', 'variant': 'Source finding'}
    if route == 'dedicated':
        response = client.post(f'{url}genomics/', payload, format='json')
        assert response.status_code == 201, response.data
        saved = response.data
    else:
        field = 'genetic_mutations' if route == 'general' else 'genomics_tp53'
        response = client.patch(url, {field: [payload]}, format='json')
        assert response.status_code == 200, response.data
        saved = response.data[field][0]
    assert set(Measurement.objects.filter(person=person).values_list('measurement_type_concept_id', flat=True)) == {expected}
    assert set(Observation.objects.filter(person=person).values_list('observation_type_concept_id', flat=True)) <= {expected}
    # A clinician's later edit must update the parent and current components.
    edited = client_for(staff).patch(url, {'genetic_mutations': [{**saved, 'variant': 'Clinician correction'}]}, format='json')
    assert edited.status_code == 200, edited.data
    assert set(Measurement.objects.filter(person=person, is_erroneous=False).values_list('measurement_type_concept_id', flat=True)) == {32817}
    assert set(Observation.objects.filter(person=person, is_erroneous=False).values_list('observation_type_concept_id', flat=True)) <= {32817}


def test_provenance_traces_only_current_parents_and_recognized_patient_components(setup):
    person, _, _ = setup
    one = save_variant(person, {'gene': 'TP53', 'variant': 'Source finding',
                                'variant_description': 'Original narrative ' * 30, 'allelic_frequency': 12.5})
    two = save_variant(person, {'gene': 'BRCA1'})
    old_component = Observation.objects.get(person=person, observation_source_value='genomics:variant_description')
    one = save_variant(person, {'interpretation': 'Reviewed'}, one['id'])
    parent = Measurement.objects.get(pk=one['id'])
    event_id = Measurement.objects.filter(person=person, measurement_event_id=parent.pk).first().meas_event_field_concept_id
    unrelated = ObservationFactory(person=person, observation_source_value='unrelated',
                                     observation_event_id=parent.pk, obs_event_field_concept_id=event_id)
    wrong_event = ConceptFactory(vocabulary__vocabulary_id='CDM', concept_code='observation.observation_id')
    wrong = ObservationFactory(person=person, observation_source_value='genomics:origin',
                                observation_event_id=parent.pk, obs_event_field_concept=wrong_event)
    foreign = MeasurementFactory(person=PersonFactory(), measurement_source_value='genomics:tp53')
    result = get_field_provenance(person, 'genomics_tp53')
    rows = result['source_rows']
    identities = {(row['table'], row['id']) for row in rows}
    assert ('Measurement', one['id']) in identities
    assert ('Measurement', two['id']) not in identities
    assert ('Measurement', foreign.pk) not in identities
    for row in (old_component, unrelated, wrong):
        assert ('Observation', row.pk) not in identities
    assert any(row['value'] == 'Original narrative ' * 30 for row in rows)
    assert any(row['value'] == 12.5 for row in rows)
    assert all(row['finding_id'] == one['id'] for row in rows)
    general = get_field_provenance(person, 'genetic_mutations')
    assert {row['finding_id'] for row in general['source_rows']} == {one['id'], two['id']}


def test_composite_provenance_includes_genomic_inputs(setup):
    person, _, _ = setup
    saved = save_variant(person, {'gene': 'TP53', 'interpretation': 'Pathogenic'})
    provenance = get_field_provenance(person, 'tp53_disruption')
    assert any(row['table'] == 'Measurement' and row['id'] == saved['id']
               and row['constituent_field'] == 'genetic_mutations'
               for row in provenance['source_rows'])
