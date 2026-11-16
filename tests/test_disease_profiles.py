"""Disease profiles: a field's scope as membership reference data (#2331).

A patient's record is the base record plus the extension of each disease the
patient has. A field with no ``FieldDiseaseMembership`` rows is base; a field
with rows belongs to the diseases they name. This stage is read-only: the
rows are stored, exposed and exported, and no tab or save consults them.
"""
import csv
import io

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from rest_framework.test import APIClient

from omop_core.models import (
    CustomPatientField, DiseaseCategoryChoice, DiseaseCategoryMapping, FieldConceptMapping,
    FieldDiseaseMembership, GroupAccess, Organization,
)
from omop_core.services import disease_field_sets, disease_profiles, disease_relevance
from omop_core.services.field_curation_transfer import apply_payload, read_payload
from omop_core.services.field_descriptor import get_all_field_descriptors
from patient_portal.models import Identity
from tests.factories import ConceptFactory, OrganizationFactory, PatientRecordFactory

pytestmark = [pytest.mark.django_db, pytest.mark.usefixtures('disease_extensions')]

MAPPINGS = '/api/v1/field-mappings/'
EXPORT = '/api/v1/field-mappings/export/'
WRITABLE = '/api/v1/patient-records/writable-fields/'
CUSTOM = '/api/v1/custom-patient-fields/'


@pytest.fixture
def staff():
    # has_org_admin_access requires at least one Organization row for staff.
    Organization.objects.get_or_create(slug='test-org', defaults={'name': 'Test Org'})
    client = APIClient()
    client.force_authenticate(Identity.objects.create_user(email='curator@example.org', is_staff=True))
    return client


def as_role(role):
    user = Identity.objects.create_user(email=f'{role}@example.org')
    GroupAccess.objects.create(identity=user, org=OrganizationFactory(), role=role)
    client = APIClient()
    client.force_authenticate(user)
    return client


def diseases_of(field_name):
    return sorted(FieldDiseaseMembership.objects.filter(field_name=field_name)
                  .values_list('disease', flat=True))


def descriptor(name):
    return next(d for d in get_all_field_descriptors() if d['field_name'] == name)


def custom_field(name='frailty_index', **kwargs):
    concept = ConceptFactory(vocabulary_id='LOINC', concept_code=f'code-{name}')
    mapping = FieldConceptMapping.objects.create(
        field_name=name, concept=concept, vocabulary_id='LOINC', concept_code=concept.concept_code,
        omop_table='observation', source_value=f'custom:{name}', value_kind='number', status='approved',
    )
    return CustomPatientField.objects.create(
        field_name=name, display_name='Frailty Index', field_type='number', mapping=mapping, **kwargs)


# ── The seed and the model ────────────────────────────────────────────────

def test_the_seed_writes_each_diseases_tab_fields_and_a_replay_adds_nothing():
    assert disease_profiles.extension_sizes() == {'BC': 27, 'CLL': 21, 'MM': 19, 'FL': 13, 'MCL': 8}
    assert disease_profiles.extension_fields('MCL') == set(disease_profiles.SEED_DISEASE_FIELDS['MCL'])
    assert set(FieldDiseaseMembership.objects.values_list('provenance', flat=True)) == {
        disease_profiles.SEED_PROVENANCE}
    assert disease_profiles.seed() == 0


def test_the_seed_gives_no_members_to_a_category_the_instance_deleted():
    FieldDiseaseMembership.objects.all().delete()
    for code in ('BC', 'MM', 'Other'):
        DiseaseCategoryChoice.objects.create(code=code, label=code)
    disease_profiles.seed()
    assert set(disease_profiles.extension_sizes()) == {'BC', 'MM'}


def test_a_field_with_no_rows_is_base_and_one_with_rows_names_its_diseases():
    assert descriptor('hemoglobin_g_dl')['scope'] == 'base'
    assert descriptor('hemoglobin_g_dl')['diseases'] == []
    assert descriptor('myeloma_type')['scope'] == 'disease'
    assert descriptor('myeloma_type')['diseases'] == ['MM']
    # One field may be in several extensions, which is why scope is a set.
    assert descriptor('stage')['diseases'] == ['FL', 'MCL', 'MM']


def test_setting_a_scope_replaces_the_set_and_keeps_who_added_the_rows_it_keeps():
    user = Identity.objects.create_user(email='admin@example.org')
    assert disease_profiles.set_field_diseases('stage', ['MM', 'CLL', 'MM'], user=user) == ['CLL', 'MM']
    assert diseases_of('stage') == ['CLL', 'MM']
    rows = {m.disease: m for m in FieldDiseaseMembership.objects.filter(field_name='stage')}
    assert rows['MM'].provenance == disease_profiles.SEED_PROVENANCE
    assert rows['CLL'].provenance == 'admin@example.org' and rows['CLL'].created_by == user

    disease_profiles.set_field_diseases('stage', [])
    assert descriptor('stage')['scope'] == 'base'


@pytest.mark.parametrize('codes', (['GLIOMA'], ['MM', 'Other'], ['mm']))
def test_a_scope_names_only_disease_categories_and_never_other(codes):
    with pytest.raises(disease_profiles.ScopeError):
        disease_profiles.set_field_diseases('stage', codes)
    assert diseases_of('stage') == ['FL', 'MCL', 'MM']


def test_a_category_a_curator_adds_gets_a_profile_and_cannot_be_deleted_with_members(staff):
    for code, label in (('BC', 'Breast'), ('MM', 'Myeloma'), ('Other', 'Other')):
        DiseaseCategoryChoice.objects.create(code=code, label=label)
    assert staff.post('/api/v1/disease-categories/', {'code': 'DLBCL', 'label': 'DLBCL'},
                      format='json').status_code == 201
    assert 'DLBCL' in disease_relevance.diseases()
    disease_profiles.set_field_diseases('bulky_disease', ['DLBCL'])

    response = staff.delete('/api/v1/disease-categories/DLBCL/')
    assert response.status_code == 409 and '1 field(s)' in response.data['detail']
    disease_profiles.set_field_diseases('bulky_disease', [])
    assert staff.delete('/api/v1/disease-categories/DLBCL/').status_code == 204


def test_a_membership_edit_changes_no_stored_value():
    custom_field()
    record = PatientRecordFactory(disease='Multiple myeloma', custom_fields={'frailty_index': 4})
    disease_profiles.set_field_diseases('frailty_index', ['BC'])
    disease_profiles.set_field_diseases('frailty_index', [])
    record.refresh_from_db()
    assert record.custom_fields == {'frailty_index': 4}


# ── A record's profile ────────────────────────────────────────────────────

def test_a_disease_name_a_curator_maps_to_a_category_gets_that_profile():
    record = PatientRecordFactory(disease='Smoldering myeloma')
    assert disease_relevance.patient_diseases(record.person) == []
    DiseaseCategoryMapping.objects.create(disease_name='Smoldering myeloma', category='MM')
    assert disease_relevance.patient_diseases(record.person) == ['MM']


def test_relevance_reads_the_rows_so_moving_a_field_moves_its_tier():
    concept = ConceptFactory(concept_name='hemoglobin')
    FieldConceptMapping.objects.create(field_name='hemoglobin_g_dl', concept=concept,
                                       status='approved', omop_table='measurement')

    def tier(code):
        return {e['field_name']: e['tier'] for e in disease_relevance.relevance([code])['concepts']
                }.get('hemoglobin_g_dl')

    assert tier('MM') == 'common' and tier('BC') == 'common'
    disease_profiles.set_field_diseases('hemoglobin_g_dl', ['MM'])
    assert tier('MM') == 'disease' and tier('BC') is None


# ── The mapping API ───────────────────────────────────────────────────────

def test_the_mapping_api_sets_reports_and_clears_a_fields_diseases(staff):
    row = FieldConceptMapping.objects.create(field_name='hemoglobin_g_dl', provenance='curator')
    response = staff.patch(f'{MAPPINGS}{row.pk}/', {'diseases': ['MM', 'BC']}, format='json')
    assert response.status_code == 200, response.data
    assert response.data['diseases'] == ['BC', 'MM'] and response.data['scope'] == 'disease'
    listed = {d['field_name']: d for d in staff.get(MAPPINGS).data}
    assert listed['hemoglobin_g_dl']['diseases'] == ['BC', 'MM']
    # Scope is not part of the recipe: nobody signs the mapping off by setting it.
    row.refresh_from_db()
    assert row.provenance == 'curator' and row.reviewer is None

    # A save that does not mention the scope leaves it alone.
    assert staff.patch(f'{MAPPINGS}{row.pk}/', {'notes': 'x'}, format='json').data['diseases'] == ['BC', 'MM']
    assert staff.patch(f'{MAPPINGS}{row.pk}/', {'diseases': []}, format='json').data['scope'] == 'base'
    assert diseases_of('hemoglobin_g_dl') == []


def test_the_mapping_api_refuses_an_unknown_disease_and_writes_nothing(staff):
    row = FieldConceptMapping.objects.create(field_name='hemoglobin_g_dl')
    response = staff.patch(f'{MAPPINGS}{row.pk}/', {'diseases': ['MM', 'GLIOMA'], 'notes': 'kept out'},
                           format='json')
    assert response.status_code == 400 and 'diseases' in response.data
    row.refresh_from_db()
    assert row.notes == '' and diseases_of('hemoglobin_g_dl') == []


def test_a_new_mapping_may_carry_its_scope(staff):
    response = staff.post(MAPPINGS, {'field_name': 'hemoglobin_g_dl', 'diseases': ['CLL']}, format='json')
    assert response.status_code == 201, response.data
    assert response.data['diseases'] == ['CLL'] and diseases_of('hemoglobin_g_dl') == ['CLL']


# A scope edit follows mapping write access: the analyst's, not the doctor's (#2416).
@pytest.mark.parametrize('role,status', (('org_admin', 200), ('analyst', 200), ('doctor', 403)))
def test_setting_a_scope_follows_mapping_write_access(role, status):
    row = FieldConceptMapping.objects.create(field_name='hemoglobin_g_dl')
    response = as_role(role).patch(f'{MAPPINGS}{row.pk}/', {'diseases': ['MM']}, format='json')
    assert response.status_code == status
    assert diseases_of('hemoglobin_g_dl') == (['MM'] if status == 200 else [])


# ── Creating a field with a scope ─────────────────────────────────────────

def test_a_created_field_takes_its_diseases_and_is_base_without_any(staff):
    concept = ConceptFactory(vocabulary_id='LOINC', concept_code='2160-0')
    payload = {
        'confirm_patient_record': True, 'field_name': 'frailty_index', 'display_name': 'Frailty Index',
        'field_type': 'number', 'concept': concept.concept_id, 'omop_table': 'observation', 'tab': 'disease',
    }
    response = staff.post(CUSTOM, {**payload, 'diseases': ['MM']}, format='json')
    assert response.status_code == 201, response.data
    assert response.data['scope'] == 'disease' and response.data['diseases'] == ['MM']
    assert descriptor('frailty_index')['diseases'] == ['MM']

    response = staff.post(CUSTOM, {**payload, 'field_name': 'frailty_note'}, format='json')
    assert response.status_code == 201, response.data
    assert response.data['scope'] == 'base' and response.data['diseases'] == []
    listed = {f['field_name']: f for f in staff.get(CUSTOM).data}
    assert listed['frailty_index']['diseases'] == ['MM'] and listed['frailty_note']['scope'] == 'base'


def test_a_created_field_with_an_unknown_disease_is_not_created(staff):
    concept = ConceptFactory(vocabulary_id='LOINC', concept_code='2160-0')
    response = staff.post(CUSTOM, {
        'confirm_patient_record': True, 'field_name': 'frailty_index', 'display_name': 'Frailty Index',
        'field_type': 'number', 'concept': concept.concept_id, 'omop_table': 'observation',
        'diseases': ['GLIOMA'],
    }, format='json')
    assert response.status_code == 400 and 'diseases' in response.data
    assert not CustomPatientField.objects.exists() and not FieldConceptMapping.objects.exists()


def test_listing_created_fields_does_not_query_per_field(staff):
    custom_field('frailty_index')
    with CaptureQueriesContext(connection) as one:
        staff.get(CUSTOM)
    for n in range(5):
        custom_field(f'extra_{n}')
        disease_profiles.set_field_diseases(f'extra_{n}', ['MM', 'BC'])
    with CaptureQueriesContext(connection) as six:
        assert len(staff.get(CUSTOM).data) == 6
    assert len(six) == len(one)


# ── The writable-fields descriptor ────────────────────────────────────────

def test_writable_fields_say_whose_record_each_field_is_part_of(staff):
    fields = staff.get(WRITABLE).data
    assert fields['hemoglobin_g_dl']['scope'] == 'base' and fields['hemoglobin_g_dl']['diseases'] == []
    assert fields['myeloma_type']['diseases'] == ['MM']
    assert all('scope' in entry and 'diseases' in entry for entry in fields.values())


def test_writable_fields_for_a_disease_are_the_base_record_plus_its_extension(staff):
    everything = staff.get(WRITABLE).data
    mm = staff.get(WRITABLE, {'disease': 'MM'}).data
    assert 'hemoglobin_g_dl' in mm and 'myeloma_type' in mm and 'stage' in mm
    assert 'estrogen_receptor_status' not in mm and 'binet_stage' not in mm
    assert set(mm) == {f for f, e in everything.items() if not e['diseases'] or 'MM' in e['diseases']}

    # A patient with two cancers gets the union; a name resolves like a code.
    both = staff.get(WRITABLE + '?disease=multiple%20myeloma&disease=bc').data
    assert 'myeloma_type' in both and 'estrogen_receptor_status' in both and 'binet_stage' not in both

    # Other has no extension: its profile is the base record.
    other = staff.get(WRITABLE, {'disease': 'Other'}).data
    assert other and all(entry['scope'] == 'base' for entry in other.values())

    response = staff.get(WRITABLE, {'disease': 'glioma'})
    assert response.status_code == 400 and 'disease' in response.data


# ── A disease's field list and its CSV ────────────────────────────────────

def test_the_field_list_filters_to_a_diseases_extension_or_its_full_record(staff):
    everything = {d['field_name'] for d in staff.get(MAPPINGS).data}
    extension = {d['field_name'] for d in staff.get(MAPPINGS, {'disease': 'MM', 'set': 'extension'}).data}
    profile = {d['field_name'] for d in staff.get(MAPPINGS, {'disease': 'MM'}).data}

    assert extension == disease_profiles.extension_fields('MM')
    assert extension < profile < everything
    assert 'hemoglobin_g_dl' in profile and 'estrogen_receptor_status' not in profile
    assert staff.get(MAPPINGS, {'disease': 'Other', 'set': 'extension'}).data == []
    assert staff.get(MAPPINGS, {'disease': 'MM', 'set': 'tab'}).status_code == 400
    assert staff.get(MAPPINGS, {'disease': 'glioma'}).status_code == 400


def read_csv(response):
    assert response.status_code == 200, getattr(response, 'data', None)
    assert response['Content-Type'].startswith('text/csv')
    return list(csv.DictReader(io.StringIO(response.content.decode())))


def test_the_csv_lists_each_field_with_its_concept_or_blank_concept_columns(staff):
    concept = ConceptFactory(vocabulary_id='LOINC', concept_code='33358-3', concept_name='Kappa light chains')
    FieldConceptMapping.objects.create(
        field_name='kappa_flc', concept=concept, vocabulary_id='LOINC', concept_code='33358-3',
        omop_table='measurement', unit='mg/L', status='approved', provenance='admin@example.org', tab='labs',
    )
    response = staff.get(EXPORT, {'disease': 'MM', 'set': 'extension'})
    rows = {row['field_name']: row for row in read_csv(response)}

    assert 'field-mappings-MM-extension-' in response['Content-Disposition']
    assert set(rows) == disease_profiles.extension_fields('MM')
    assert list(next(iter(rows.values()))) == list(disease_field_sets.COLUMNS)
    assert rows['kappa_flc'] == {
        'field_name': 'kappa_flc', 'label': 'Kappa flc', 'scope': 'disease', 'diseases': 'MM',
        'tab': 'labs', 'field_type': rows['kappa_flc']['field_type'], 'omop_table': 'measurement',
        'concept_id': str(concept.concept_id), 'concept_name': 'Kappa light chains',
        'vocabulary_id': 'LOINC', 'concept_code': '33358-3', 'unit': 'mg/L',
        # A reviewer's name does not leave the instance in a file.
        'mapping_status': 'approved', 'provenance': 'curator',
    }
    # Not curated yet: the row is there, with nothing in the concept columns.
    assert rows['myeloma_type']['concept_id'] == '' and rows['myeloma_type']['mapping_status'] == ''
    assert rows['stage']['diseases'] == 'FL MCL MM'


def test_the_full_record_csv_is_the_base_record_plus_the_extension(staff):
    profile = {row['field_name']: row for row in read_csv(staff.get(EXPORT, {'disease': 'MM'}))}
    listed = {d['field_name'] for d in staff.get(MAPPINGS, {'disease': 'MM'}).data}
    assert set(profile) == listed
    assert profile['hemoglobin_g_dl']['scope'] == 'base' and 'estrogen_receptor_status' not in profile

    # No disease, like Other, is the base record.
    response = staff.get(EXPORT)
    assert 'field-mappings-base-profile-' in response['Content-Disposition']
    base = read_csv(response)
    assert base and all(row['scope'] == 'base' for row in base)
    assert read_csv(staff.get(EXPORT, {'disease': 'Other'})) == base
    assert staff.get(EXPORT, {'disease': 'glioma'}).status_code == 400
    assert staff.get(EXPORT, {'disease': 'MM', 'set': 'tab'}).status_code == 400


def test_a_created_field_is_in_its_diseases_csv_under_its_display_name(staff):
    custom_field(tab='disease')
    disease_profiles.set_field_diseases('frailty_index', ['BC'])
    rows = {row['field_name']: row for row in read_csv(staff.get(EXPORT, {'disease': 'BC', 'set': 'extension'}))}
    assert rows['frailty_index']['label'] == 'Frailty Index' and rows['frailty_index']['tab'] == 'disease'
    assert 'frailty_index' not in {
        row['field_name'] for row in read_csv(staff.get(EXPORT, {'disease': 'MM'}))}


def test_a_csv_cell_a_spreadsheet_would_run_is_quoted(staff):
    custom = custom_field()
    custom.display_name = '=HYPERLINK("http://x")'
    custom.save()
    rows = {row['field_name']: row for row in read_csv(staff.get(EXPORT))}
    assert rows['frailty_index']['label'] == '\'=HYPERLINK("http://x")'


# Whoever may read the mapping pages may download; a patient may not (#2416).
@pytest.mark.parametrize('role,status', (('org_admin', 200), ('analyst', 200), ('doctor', 200), ('patient', 403)))
def test_the_csv_follows_mapping_read_access(role, status):
    assert as_role(role).get(EXPORT, {'disease': 'MM'}).status_code == status
    assert APIClient().get(EXPORT, {'disease': 'MM'}).status_code in (401, 403)


def test_the_csv_costs_the_same_queries_however_many_fields_and_diseases(staff):
    with CaptureQueriesContext(connection) as before:
        staff.get(EXPORT, {'disease': 'MM'})
    DiseaseCategoryChoice.objects.bulk_create(
        [DiseaseCategoryChoice(code=code, label=code) for code in ('BC', 'CLL', 'FL', 'MCL', 'MM', 'Other', 'DLBCL')])
    for field in ('hemoglobin_g_dl', 'platelet_count', 'serum_creatinine_level', 'weight', 'height'):
        disease_profiles.set_field_diseases(field, ['MM', 'DLBCL', 'CLL'])
    with CaptureQueriesContext(connection) as after:
        staff.get(EXPORT, {'disease': 'MM'})
    assert len(after) == len(before)


# ── Moving between instances ──────────────────────────────────────────────

def test_scope_round_trips_through_the_curation_payload_without_naming_a_person():
    user = Identity.objects.create_user(email='admin@example.org')
    disease_profiles.set_field_diseases('hemoglobin_g_dl', ['MM'], user=user)
    payload = read_payload('default', tables=('memberships',))
    row = next(r for r in payload['memberships'] if r['field_name'] == 'hemoglobin_g_dl')
    assert row == {'field_name': 'hemoglobin_g_dl', 'disease': 'MM', 'required': False,
                   'section': '', 'sort_order': 0, 'provenance': 'curator'}
    assert len(payload['memberships']) == 89

    FieldDiseaseMembership.objects.all().delete()
    stats = apply_payload(payload)
    assert stats.created == {'memberships': 89}
    assert diseases_of('hemoglobin_g_dl') == ['MM'] and diseases_of('stage') == ['FL', 'MCL', 'MM']
    copied = FieldDiseaseMembership.objects.get(field_name='hemoglobin_g_dl')
    assert copied.created_by is None and copied.provenance == 'curator'
    assert FieldDiseaseMembership.objects.get(field_name='stage', disease='MM').provenance == (
        disease_profiles.SEED_PROVENANCE)
    assert apply_payload(payload).updated == {'memberships': 89}


def test_a_payload_naming_a_category_this_instance_lacks_skips_that_row():
    payload = {'memberships': [
        {'field_name': 'hemoglobin_g_dl', 'disease': 'DLBCL', 'required': False, 'section': '', 'sort_order': 0},
        {'field_name': 'hemoglobin_g_dl', 'disease': 'CLL', 'required': True, 'section': '', 'sort_order': 0},
    ]}
    stats = apply_payload(payload, tables=('memberships',))
    assert stats.skipped == {'memberships': 1} and 'DLBCL' in stats.warnings[0]
    assert diseases_of('hemoglobin_g_dl') == ['CLL']
    assert FieldDiseaseMembership.objects.get(field_name='hemoglobin_g_dl').required is True


def test_prune_mirrors_the_sources_scopes_but_an_older_payload_removes_none():
    payload = read_payload('default', tables=('memberships',))
    disease_profiles.set_field_diseases('hemoglobin_g_dl', ['MM'])

    # field_curation_v1.json predates the table: it cannot say what it lacks.
    apply_payload({'mappings': []}, tables=('mappings', 'memberships'), prune=True)
    assert diseases_of('hemoglobin_g_dl') == ['MM'] and FieldDiseaseMembership.objects.count() == 89

    stats = apply_payload(payload, tables=('memberships',), prune=True)
    assert stats.deleted == {'memberships': 1} and diseases_of('hemoglobin_g_dl') == []
