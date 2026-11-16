"""Disease profiles: a field's scope as membership reference data (#2331).

A patient's record is the base record plus the extension of each disease the
patient has. A field with no ``FieldDiseaseMembership`` rows is base; a field
with rows belongs to the diseases they name. This stage is read-only: the
rows are stored, exposed and exported, and no tab or save consults them.
"""
import csv
import importlib
import io
import pkgutil
from collections import Counter, defaultdict
from unittest import mock

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from rest_framework.test import APIClient

from omop_core.models import (
    CustomPatientField, DiseaseCategoryChoice, DiseaseCategoryMapping, FieldConceptMapping,
    FieldDiseaseMembership, GroupAccess, Organization, RelevanceSignal,
)
from omop_core.services import disease_field_sets, disease_profiles, disease_relevance
from omop_core.services.field_curation_transfer import apply_payload, read_payload
from omop_core.services.field_descriptor import get_all_field_descriptors
from patient_portal.models import Identity
from tests.factories import ConceptFactory, OrganizationFactory, PatientRecordFactory
from tests.migration_utils import run_data_migration

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


def sizes():
    return dict(Counter(FieldDiseaseMembership.objects.values_list('disease', flat=True)))


def scope_url(field_name):
    return f'{MAPPINGS}{field_name}/diseases/'


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
    assert sizes() == {'BC': 27, 'CLL': 21, 'MM': 19, 'FL': 13, 'MCL': 8}
    assert disease_profiles.extension_fields('MCL') == set(disease_profiles.SEED_DISEASE_FIELDS['MCL'])
    assert set(FieldDiseaseMembership.objects.values_list('provenance', flat=True)) == {
        disease_profiles.SEED_PROVENANCE}
    assert disease_profiles.seed() == 0


def test_the_seed_gives_no_members_to_a_category_the_instance_deleted():
    FieldDiseaseMembership.objects.all().delete()
    for code in ('BC', 'MM', 'Other'):
        DiseaseCategoryChoice.objects.create(code=code, label=code)
    disease_profiles.seed()
    assert set(sizes()) == {'BC', 'MM'}


def test_every_seeded_field_is_written_by_a_migration():
    # A deployed instance gets its rows from the migrations' own frozen
    # copies, never from the constant. Adding a field to the constant (the
    # tab drift test forces that) without a data migration fails here.
    import omop_core.migrations as package

    written = defaultdict(set)
    for info in pkgutil.iter_modules(package.__path__):
        module = importlib.import_module(f'omop_core.migrations.{info.name}')
        for code, fields in getattr(module, 'FIELD_DISEASE_SEED', {}).items():
            written[code] |= set(fields)
    # Contained in, not equal to: a field later taken off the tab leaves the
    # constant, and the frozen migration that once seeded it stays as it is.
    for code, fields in disease_profiles.SEED_DISEASE_FIELDS.items():
        assert set(fields) <= written[code], (code, set(fields) - written[code])
    assert set().union(*disease_profiles.SEED_DISEASE_FIELDS.values()) <= disease_profiles.field_names()


def test_the_seed_migration_writes_the_rows_once_and_skips_a_missing_category():
    # The function 0344 runs on a deployed instance, not the service's copy.
    FieldDiseaseMembership.objects.all().delete()
    for code in ('BC', 'CLL', 'FL', 'MM', 'Other'):
        DiseaseCategoryChoice.objects.create(code=code, label=code)
    FieldDiseaseMembership.objects.create(field_name='stage', disease='MM', provenance='curator')

    run_data_migration('0344_seed_field_disease_membership', 'seed')
    assert sizes() == {'BC': 27, 'CLL': 21, 'MM': 19, 'FL': 13}
    assert FieldDiseaseMembership.objects.get(field_name='stage', disease='MM').provenance == 'curator'
    assert FieldDiseaseMembership.objects.filter(provenance=disease_profiles.SEED_PROVENANCE).count() == 79

    run_data_migration('0344_seed_field_disease_membership', 'seed')
    assert FieldDiseaseMembership.objects.count() == 80


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

    # The same scope again writes nothing.
    with CaptureQueriesContext(connection) as queries:
        disease_profiles.set_field_diseases('stage', ['MM', 'CLL'])
    assert not [q for q in queries if q['sql'].startswith(('INSERT', 'DELETE', 'UPDATE'))]

    disease_profiles.set_field_diseases('stage', [])
    assert descriptor('stage')['scope'] == 'base'


def test_a_scope_change_is_logged_once_it_commits_and_not_when_it_rolls_back(
        django_capture_on_commit_callbacks):
    user = Identity.objects.create_user(email='admin@example.org')
    with mock.patch.object(disease_profiles.logger, 'info') as info:
        with django_capture_on_commit_callbacks(execute=True):
            disease_profiles.set_field_diseases('stage', ['CLL'], user=user)
        assert info.call_args.args[1:] == ('stage', ['FL', 'MCL', 'MM'], ['CLL'], user.pk)

        info.reset_mock()
        with django_capture_on_commit_callbacks(execute=True) as callbacks:
            # The same scope again changes nothing, so reports nothing.
            disease_profiles.set_field_diseases('stage', ['CLL'], user=user)
        assert not callbacks and not info.called
    # The line goes to a logger the deployment configures.
    assert disease_profiles.logger.name == 'omop.ops'


@pytest.mark.parametrize('name', ('person', 'user_edited_fields', 'disease', 'no_such_field'))
def test_only_a_field_the_descriptors_list_can_be_given_a_scope(name):
    with pytest.raises(disease_profiles.ScopeError):
        disease_profiles.set_field_diseases(name, ['MM'])
    assert diseases_of(name) == []
    # Any name can be made base: that is how a removed field's rows are cleared.
    assert disease_profiles.set_field_diseases(name, []) == []


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
    assert response.status_code == 409
    assert '1 field(s)' in response.data['detail'] and 'bulky_disease' in response.data['detail']
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


def test_a_code_resolves_in_any_case_and_a_name_mapped_to_other_is_the_base_record(staff):
    for code, label in (('MM', 'Myeloma'), ('Lung', 'Lung cancer'), ('Other', 'Other')):
        DiseaseCategoryChoice.objects.create(code=code, label=label)
    DiseaseCategoryMapping.objects.create(disease_name='Glioblastoma', category='Other')
    disease_profiles.set_field_diseases('bulky_disease', ['Lung'])

    # A curator typed "Lung"; relevance used to upper-case it and refuse it.
    assert staff.get('/api/v1/relevance/', {'disease': 'lung'}).data['diseases'] == ['Lung']
    assert staff.get('/api/v1/relevance/', {'disease': 'Other'}).status_code == 400
    assert 'bulky_disease' in staff.get(WRITABLE, {'disease': 'LUNG'}).data

    base = staff.get(WRITABLE, {'disease': 'Glioblastoma'})
    assert base.status_code == 200 and all(entry['scope'] == 'base' for entry in base.data.values())
    assert staff.get(WRITABLE, {'disease': 'no such disease'}).status_code == 400


def test_receiving_signals_reads_the_categories_once_not_once_per_signal(staff):
    concepts = [ConceptFactory().concept_id for _ in range(6)]

    def post(ids):
        with CaptureQueriesContext(connection) as queries:
            response = staff.post('/api/v1/relevance/signals/', [
                {'disease': 'MM', 'concept_id': concept_id, 'document_count': 1, 'period': '2026-11'}
                for concept_id in ids], format='json')
        assert response.status_code in (200, 201), response.data
        return sum('disease_category_choice' in q['sql'] for q in queries)

    assert post(concepts[:1]) == post(concepts[1:]) == 1
    assert RelevanceSignal.objects.count() == 6

    # A disease name is looked up once however many signals carry it.
    DiseaseCategoryMapping.objects.create(disease_name='Smoldering myeloma', category='MM')
    with CaptureQueriesContext(connection) as queries:
        response = staff.post('/api/v1/relevance/signals/', [
            {'disease': 'Smoldering myeloma', 'concept_id': concept_id, 'document_count': 1, 'period': '2026-12'}
            for concept_id in concepts], format='json')
    assert response.status_code in (200, 201), response.data
    assert sum('FROM "disease_category_mapping"' in q['sql'] for q in queries) == 1


# ── Scope by field name ───────────────────────────────────────────────────

def test_a_fields_scope_is_read_and_set_by_name_without_a_mapping_row(staff):
    assert staff.get(scope_url('myeloma_type')).data == {
        'field_name': 'myeloma_type', 'scope': 'disease', 'diseases': ['MM']}

    response = staff.put(scope_url('myeloma_type'), {'diseases': ['MM', 'CLL']}, format='json')
    assert response.status_code == 200 and response.data['diseases'] == ['CLL', 'MM']
    assert staff.put(scope_url('myeloma_type'), {'diseases': []}, format='json').data['scope'] == 'base'
    # No concept was asserted, so no mapping row appears for the field.
    assert not FieldConceptMapping.objects.filter(field_name='myeloma_type').exists()

    assert staff.put(scope_url('myeloma_type'), {'diseases': ['GLIOMA']}, format='json').status_code == 400
    assert staff.put(scope_url('myeloma_type'), {'diseases': 'MM'}, format='json').status_code == 400
    assert staff.put(scope_url('myeloma_type'), {}, format='json').status_code == 400
    # A blank code is refused, not read as "no diseases".
    disease_profiles.set_field_diseases('myeloma_type', ['MM'])
    assert staff.put(scope_url('myeloma_type'), {'diseases': ['']}, format='json').status_code == 400
    assert diseases_of('myeloma_type') == ['MM']
    # Not a field the descriptors list: an unknown name, an internal column,
    # and the field that says which disease the record has.
    for name in ('no_such_field', 'person', 'disease'):
        assert staff.get(scope_url(name)).status_code == 404
        assert staff.put(scope_url(name), {'diseases': ['MM']}, format='json').status_code == 404
        assert diseases_of(name) == []


def test_rows_left_by_a_removed_field_can_be_cleared_and_do_not_block_a_category(staff):
    for code in ('MM', 'Lung', 'Other'):
        DiseaseCategoryChoice.objects.create(code=code, label=code)
    for disease in ('MM', 'Lung'):
        FieldDiseaseMembership.objects.create(field_name='removed_field', disease=disease)

    # Readable and clearable by name, but not given a new scope.
    assert staff.get(scope_url('removed_field')).data['diseases'] == ['Lung', 'MM']
    assert staff.put(scope_url('removed_field'), {'diseases': ['MM']}, format='json').status_code == 400
    # Only real fields hold a category in place; the orphan goes with it.
    assert staff.delete('/api/v1/disease-categories/Lung/').status_code == 204
    assert diseases_of('removed_field') == ['MM']
    assert staff.put(scope_url('removed_field'), {'diseases': []}, format='json').status_code == 200
    assert staff.get(scope_url('removed_field')).status_code == 404


@pytest.mark.parametrize('role,read,write', (
    ('org_admin', 200, 200), ('analyst', 200, 200), ('doctor', 200, 403), ('patient', 403, 403)))
def test_scope_by_name_follows_the_mapping_page_roles(role, read, write):
    client = as_role(role)
    assert client.get(scope_url('stage')).status_code == read
    assert client.put(scope_url('stage'), {'diseases': ['MM']}, format='json').status_code == write
    assert diseases_of('stage') == (['MM'] if write == 200 else ['FL', 'MCL', 'MM'])


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


def test_a_scope_only_save_does_not_resave_or_reproject_an_approved_mapping(staff):
    concept = ConceptFactory(vocabulary_id='LOINC', concept_code='718-7')
    row = FieldConceptMapping.objects.create(
        field_name='hemoglobin_g_dl', concept=concept, omop_table='measurement',
        source_value='hemoglobin_g_dl', status='approved')
    saved_at = row.updated_at
    with mock.patch('omop_core.signals._project_field_sync') as project, \
            mock.patch('omop_core.signals._dispatch_projection') as dispatch:
        response = staff.patch(f'{MAPPINGS}{row.pk}/', {'diseases': ['MM']}, format='json')
        assert response.status_code == 200 and response.data['diseases'] == ['MM']
        assert not project.called and not dispatch.called
        row.refresh_from_db()
        assert row.updated_at == saved_at

        # A save that changes the mapping as well still saves it.
        staff.patch(f'{MAPPINGS}{row.pk}/', {'diseases': [], 'notes': 'x'}, format='json')
        row.refresh_from_db()
        assert row.notes == 'x' and row.updated_at > saved_at and diseases_of('hemoglobin_g_dl') == []


def test_the_mapping_api_scopes_only_a_field_the_descriptors_list(staff):
    row = FieldConceptMapping.objects.create(field_name='disease')
    response = staff.patch(f'{MAPPINGS}{row.pk}/', {'diseases': ['MM']}, format='json')
    assert response.status_code == 400 and 'diseases' in response.data
    assert staff.patch(f'{MAPPINGS}{row.pk}/', {'diseases': []}, format='json').status_code == 200


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


def test_a_created_field_does_not_inherit_rows_left_under_its_name(staff):
    # A field of this name existed, was scoped to MM and was removed.
    FieldDiseaseMembership.objects.create(field_name='frailty_index', disease='MM')
    concept = ConceptFactory(vocabulary_id='LOINC', concept_code='2160-0')
    response = staff.post(CUSTOM, {
        'confirm_patient_record': True, 'field_name': 'frailty_index', 'display_name': 'Frailty Index',
        'field_type': 'number', 'concept': concept.concept_id, 'omop_table': 'observation',
    }, format='json')
    assert response.status_code == 201, response.data
    assert response.data['scope'] == 'base' and diseases_of('frailty_index') == []


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
    assert response.status_code == 400 and 'glioma' in response.data['detail']
    # A blank value is no filter, as it is on the field list.
    assert staff.get(WRITABLE, {'disease': ''}).data == everything


def test_both_descriptors_give_every_field_the_same_scope(staff):
    disease_profiles.set_field_diseases('hemoglobin_g_dl', ['MM'])
    writable = staff.get(WRITABLE).data
    listed = {d['field_name']: d for d in staff.get(MAPPINGS).data}
    shared = set(writable) & set(listed)
    assert 'hemoglobin_level' in shared and 'hemoglobin_g_dl' in shared
    assert {f: writable[f]['diseases'] for f in shared} == {f: listed[f]['diseases'] for f in shared}


# ── A disease's field list and its CSV ────────────────────────────────────

def test_the_field_list_filters_to_a_diseases_extension_or_its_full_record(staff):
    everything = {d['field_name'] for d in staff.get(MAPPINGS).data}
    extension = {d['field_name'] for d in staff.get(MAPPINGS, {'disease': 'MM', 'set': 'extension'}).data}
    profile = {d['field_name'] for d in staff.get(MAPPINGS, {'disease': 'MM'}).data}

    assert extension == disease_profiles.extension_fields('MM')
    assert extension < profile < everything
    assert 'hemoglobin_g_dl' in profile and 'estrogen_receptor_status' not in profile
    assert staff.get(MAPPINGS, {'disease': 'Other', 'set': 'extension'}).data == []
    assert {d['field_name'] for d in staff.get(MAPPINGS, {'disease': ''}).data} == everything
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


def test_the_csv_filename_takes_nothing_from_a_category_code_but_safe_characters(staff):
    for code in ('MM', 'Other', 'Lung "A"\nx'):
        DiseaseCategoryChoice.objects.create(code=code, label=code)
    response = staff.get(EXPORT, {'disease': 'Lung "A"\nx', 'set': 'extension'})
    assert response.status_code == 200
    assert 'filename="field-mappings-Lung_A_x-extension-' in response['Content-Disposition']


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
    again = apply_payload(payload)
    assert again.updated == {'memberships': 89} and not again.deleted


def test_a_narrowed_or_cleared_scope_reaches_the_target():
    # Both instances start from the same seed, and narrowing a scope removes
    # rows: adding the source's rows to the target's would change nothing.
    disease_profiles.set_field_diseases('stage', ['CLL'])
    disease_profiles.set_field_diseases('myeloma_type', [])
    payload = read_payload('default', tables=('memberships',))

    FieldDiseaseMembership.objects.all().delete()
    disease_profiles.seed()
    assert diseases_of('stage') == ['FL', 'MCL', 'MM'] and diseases_of('myeloma_type') == ['MM']

    stats = apply_payload(payload)
    assert diseases_of('stage') == ['CLL'] and diseases_of('myeloma_type') == []
    assert stats.created == {'memberships': 1} and stats.deleted == {'memberships': 4}


def test_the_source_replaces_only_what_it_can_vouch_for():
    custom_field('shared')
    payload = read_payload('default', tables=('custom_fields', 'memberships'))
    assert 'shared' in payload['membership_source']['fields']
    assert payload['membership_source']['categories'] == ['BC', 'CLL', 'FL', 'MCL', 'MM', 'Other']

    # Since the source was read, the target gained a field, a category and
    # scopes of its own.
    for code in ('BC', 'CLL', 'FL', 'MCL', 'MM', 'Other', 'AML'):
        DiseaseCategoryChoice.objects.create(code=code, label=code)
    custom_field('local_only')
    disease_profiles.set_field_diseases('local_only', ['BC'])
    disease_profiles.set_field_diseases('shared', ['BC'])
    disease_profiles.set_field_diseases('hemoglobin_g_dl', ['AML', 'MM'])

    stats = apply_payload(payload, tables=('memberships',))
    # A field only here, and a category only here: the source says nothing.
    assert diseases_of('local_only') == ['BC']
    assert diseases_of('hemoglobin_g_dl') == ['AML']
    # A field both have, base at the source, becomes base here.
    assert diseases_of('shared') == []
    assert stats.deleted == {'memberships': 2}


def test_a_handwritten_payload_adds_rows_and_removes_none():
    payload = {'memberships': [
        {'field_name': 'hemoglobin_g_dl', 'disease': 'CLL', 'required': True, 'section': '', 'sort_order': 0},
        {'field_name': 'no_such_field_here', 'disease': 'MM', 'required': False, 'section': '', 'sort_order': 0},
    ]}
    stats = apply_payload(payload, tables=('memberships',))
    assert stats.created == {'memberships': 1} and not stats.deleted
    assert stats.skipped == {'memberships': 1} and 'no such field' in stats.warnings[0]
    assert diseases_of('no_such_field_here') == [] and FieldDiseaseMembership.objects.count() == 89
    assert FieldDiseaseMembership.objects.get(field_name='hemoglobin_g_dl').required is True


def test_a_field_whose_source_category_is_missing_here_keeps_the_scope_it_has():
    disease_profiles.set_field_diseases('stage', ['CLL', 'MM'])
    payload = read_payload('default', tables=('memberships',))
    # At the source myeloma_type belongs to DLBCL alone and stage to CLL and
    # DLBCL; this instance has no DLBCL category.
    payload['membership_source']['categories'].append('DLBCL')
    payload['memberships'] = [
        {**row, 'disease': 'DLBCL'} if (row['field_name'], row['disease']) in (
            ('myeloma_type', 'MM'), ('stage', 'MM')) else row
        for row in payload['memberships']]
    disease_profiles.set_field_diseases('stage', ['FL'])

    stats = apply_payload(payload)
    assert stats.skipped == {'memberships': 2} and 'left as it is' in stats.warnings[0]
    # Skipping the row and replacing the rest would have made these base, or
    # a scope the source never had.
    assert diseases_of('myeloma_type') == ['MM'] and diseases_of('stage') == ['FL']


def test_a_payload_written_before_memberships_changes_no_scope_even_with_prune():
    disease_profiles.set_field_diseases('hemoglobin_g_dl', ['MM'])
    # field_curation_v1.json predates the table: it cannot say what a scope is.
    stats = apply_payload({'mappings': []}, tables=('mappings', 'memberships'), prune=True)
    assert diseases_of('hemoglobin_g_dl') == ['MM'] and FieldDiseaseMembership.objects.count() == 89
    assert 'memberships' not in stats.deleted


def test_scope_travels_with_copy_reference_data_after_the_categories():
    from omop_core.services.reference_transfer import apply_reference, read_reference

    disease_profiles.set_field_diseases('stage', ['CLL'])
    payload = read_reference('default')
    disease_profiles.set_field_diseases('stage', ['FL', 'MM'])
    apply_reference(payload)
    assert diseases_of('stage') == ['CLL']


def test_pruning_a_created_field_takes_its_memberships_with_it():
    custom_field('frailty_index')
    disease_profiles.set_field_diseases('frailty_index', ['MM'])
    apply_payload({'mappings': [], 'custom_fields': []}, tables=('custom_fields',), prune=True)
    assert not CustomPatientField.objects.exists() and diseases_of('frailty_index') == []
