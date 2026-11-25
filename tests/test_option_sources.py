"""The lookup a field's options come from, shown read only in the choice editor (#1225)."""
import re
from pathlib import Path

import pytest
from rest_framework.test import APIClient

from omop_core.models import FieldChoice, FieldConceptMapping, GroupAccess, Her2Status, PatientRecord, TumorStage
from omop_core.services.option_sources import LOOKUP_FIELDS
from patient_portal.api.views import _VOCABULARY_REGISTRY
from patient_portal.models import Identity
from tests.factories import ConceptFactory, OrganizationFactory

pytestmark = pytest.mark.django_db

URL = '/api/v1/field-choices/lookup/'
TABS = Path(__file__).resolve().parent.parent / 'frontend/src/components/PatientInfo/tabs'


def as_role(role):
    user = Identity.objects.create_user(email=f'lookup-{role}@example.org', is_staff=role == 'staff')
    if role != 'staff':
        GroupAccess.objects.create(identity=user, org=OrganizationFactory(), role=role)
    client = APIClient()
    client.force_authenticate(user)
    return client


# ── The registry ───────────────────────────────────────────────────────────

def test_every_field_is_a_patient_record_column_and_every_lookup_is_served():
    columns = {f.name for f in PatientRecord._meta.get_fields() if getattr(f, 'concrete', False)}
    assert set(LOOKUP_FIELDS) <= columns
    assert set(LOOKUP_FIELDS.values()) <= set(_VOCABULARY_REGISTRY)
    assert len(set(LOOKUP_FIELDS.values())) == len(LOOKUP_FIELDS), 'one lookup per field'


def test_every_lookup_a_tab_draws_a_dropdown_from_is_bound_to_a_field():
    used = set()
    for tab in TABS.glob('*.tsx'):
        if tab.name.endswith('.test.tsx'):
            continue
        used |= set(re.findall(r"useVocabulary\(\s*['\"]([a-z0-9-]+)['\"]", tab.read_text()))
    assert used, 'the tabs no longer call useVocabulary; update this test'
    assert used <= set(LOOKUP_FIELDS.values()), sorted(used - set(LOOKUP_FIELDS.values()))
    # And the other way: a binding no tab draws from is stale.
    assert set(LOOKUP_FIELDS.values()) <= used, sorted(set(LOOKUP_FIELDS.values()) - used)


def test_the_seeded_enforced_bindings_agree_with_the_registry():
    """A mapping's `value_vocabulary` names a lookup class this endpoint can resolve."""
    import json
    seed = json.loads((Path(__file__).resolve().parent.parent / 'omop_core/data/field_curation_v1.json').read_text())
    rows = seed['field_mappings'] if isinstance(seed, dict) and 'field_mappings' in seed else seed
    classes = {m.__name__: slug for slug, m in _VOCABULARY_REGISTRY.items()}
    bound = {r['field_name']: r['value_vocabulary'] for r in _walk(rows) if r.get('value_vocabulary')}
    assert bound
    for field, cls in bound.items():
        assert cls in classes, (field, cls)
        assert LOOKUP_FIELDS.get(field) == classes[cls], (field, cls)


def _walk(node):
    if isinstance(node, dict):
        if 'field_name' in node:
            yield node
        for value in node.values():
            yield from _walk(value)
    elif isinstance(node, list):
        for item in node:
            yield from _walk(item)


# ── The endpoint ───────────────────────────────────────────────────────────

def test_a_field_the_tab_draws_from_a_lookup_lists_its_entries_and_copies_nothing():
    TumorStage.objects.create(code='T2', title='T2')
    TumorStage.objects.create(code='T1', title='T1')
    response = as_role('analyst').get(URL, {'field_name': 'tumor_stage'})
    assert response.status_code == 200
    body = response.json()
    assert body['source']['name'] == 'tumor-stage' and body['source']['label'] == 'TumorStage'
    assert body['source']['enforced'] is False
    # The entries and their order are the tab's own, from the same lookup.
    tab = as_role('analyst').get('/api/v1/vocabularies/tumor-stage/').json()
    assert [o['title'] for o in body['options']] == [o['title'] for o in tab]
    assert {o['title'] for o in body['options']} == {'T1', 'T2'}
    assert FieldChoice.objects.count() == 0


def test_an_approved_mapping_that_names_the_lookup_makes_it_enforced():
    Her2Status.objects.create(code='pos', title='Positive')
    FieldConceptMapping.objects.update_or_create(
        field_name='her2_status',
        defaults={'status': 'approved', 'value_vocabulary': 'Her2Status',
                  'concept': ConceptFactory(), 'omop_table': 'Observation'})
    source = as_role('staff').get(URL, {'field_name': 'her2_status'}).json()['source']
    assert source['name'] == 'her2-status' and source['enforced'] is True


def test_an_approved_mapping_too_incomplete_to_write_does_not_enforce_it():
    """No concept, or an empty lookup: the descriptor offers nothing, so the editor must not say it does."""
    Her2Status.objects.create(code='pos', title='Positive')
    FieldConceptMapping.objects.update_or_create(
        field_name='her2_status', defaults={'status': 'approved', 'value_vocabulary': 'Her2Status'})
    assert as_role('staff').get(URL, {'field_name': 'her2_status'}).json()['source']['enforced'] is False
    FieldConceptMapping.objects.filter(field_name='her2_status').update(
        concept=ConceptFactory(), omop_table='Observation')
    Her2Status.objects.all().delete()
    assert as_role('staff').get(URL, {'field_name': 'her2_status'}).json()['source']['enforced'] is False


def test_the_lookup_a_mapping_names_wins_over_the_tabs_hint():
    from omop_core.models import Language
    FieldConceptMapping.objects.update_or_create(
        field_name='ethnicity',
        defaults={'status': 'approved', 'value_vocabulary': 'Language',
                  'concept': ConceptFactory(), 'omop_table': 'Observation'})
    Language.objects.create(code='en', title='English')
    source = as_role('staff').get(URL, {'field_name': 'ethnicity'}).json()['source']
    assert source['label'] == 'Language' and source['enforced'] is True


def test_a_proposed_mapping_does_not_enforce_it():
    FieldConceptMapping.objects.update_or_create(
        field_name='her2_status', defaults={'status': 'proposed', 'value_vocabulary': 'Her2Status'})
    assert as_role('staff').get(URL, {'field_name': 'her2_status'}).json()['source']['enforced'] is False


def test_a_field_with_no_lookup_has_no_source():
    body = as_role('doctor').get(URL, {'field_name': 'hemoglobin_g_dl'}).json()
    assert body == {'field_name': 'hemoglobin_g_dl', 'source': None, 'options': [], 'orphaned': []}


def test_a_field_name_is_required():
    assert as_role('doctor').get(URL).status_code == 400


@pytest.mark.parametrize('role,expected', (('org_admin', 200), ('doctor', 200), ('analyst', 200), ('patient', 403)))
def test_whoever_reads_the_mapping_pages_reads_it(role, expected):
    assert as_role(role).get(URL, {'field_name': 'tumor_stage'}).status_code == expected
