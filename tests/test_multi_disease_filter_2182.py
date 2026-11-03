"""A patient with two cancers is listed and counted under both (#2182)."""
import pytest
from rest_framework.test import APIClient

from omop_core.models import DiseaseCategoryChoice, DiseaseCategoryMapping, PatientRecord
from omop_core.services.patient_record_service import invalidate_category_titles_cache
from omop_core.signals import suppress_patient_record_refresh
from patient_portal.models import Identity
from tests.factories import OrganizationFactory, PatientRecordFactory
from tests.test_primary_cancer import _condition, _refreshed, ago

pytestmark = pytest.mark.django_db

MYELOMA = 'Multiple myeloma (disorder)'
PROSTATE = 'Malignant neoplasm of prostate'


@pytest.fixture(autouse=True)
def prostate_category():
    """Prostate cancer as a curator adds it: a category and a name mapped to it."""
    DiseaseCategoryChoice.objects.get_or_create(code='PC', defaults={'label': 'Prostate Cancer'})
    DiseaseCategoryMapping.objects.get_or_create(disease_name=PROSTATE, defaults={'category': 'PC'})
    invalidate_category_titles_cache()
    yield
    invalidate_category_titles_cache()


@pytest.fixture
def staff_client():
    client = APIClient()
    client.force_authenticate(Identity.objects.create_user(email='staff-2182@t.com', password='x', is_staff=True))
    return client


def _patient(org, *cancers):
    """A person whose record is derived from these (days ago, name) conditions.

    With no lines of therapy, the newest cancer is the primary (#1738).
    """
    person = PatientRecordFactory(organization=org, disease=None).person
    for days, name in cancers:
        _condition(person, name, ago(days))
    return _refreshed(person)


def _listed(client, disease):
    resp = client.get('/api/patient-info/', {'page': 1, 'page_size': 50, 'disease': disease})
    assert resp.status_code == 200
    return {row['id'] for row in resp.data['results']}


# --- derivation -------------------------------------------------------------

def test_every_cancer_is_derived_primary_first():
    record = _patient(OrganizationFactory(), (900, PROSTATE), (100, MYELOMA))

    assert record.disease == 'Multiple Myeloma'
    assert record.disease_slugs[0] == record.disease_slug == 'multiple-myeloma'
    assert record.disease_slugs == ['multiple-myeloma', 'prostate-cancer']


def test_a_condition_that_is_not_a_cancer_is_left_out():
    record = _patient(OrganizationFactory(), (900, MYELOMA), (100, 'Essential hypertension'))

    assert record.disease_slugs == ['multiple-myeloma']


def test_two_rows_of_one_cancer_are_one_entry():
    record = _patient(OrganizationFactory(), (900, PROSTATE), (300, PROSTATE))

    assert record.disease_slugs == ['prostate-cancer']


def test_a_removed_cancer_leaves_the_list():
    org = OrganizationFactory()
    record = _patient(org, (100, MYELOMA))
    prostate = _condition(record.person, PROSTATE, ago(900))
    assert _refreshed(record.person).disease_slugs == ['multiple-myeloma', 'prostate-cancer']

    with suppress_patient_record_refresh():
        prostate.delete()

    assert _refreshed(record.person).disease_slugs == ['multiple-myeloma']


def test_no_cancer_derives_an_empty_list():
    assert _patient(OrganizationFactory(), (100, 'Essential hypertension')).disease_slugs == []


# --- the patient list filter --------------------------------------------------

def test_listed_under_each_cancer(staff_client):
    """The issue's case: myeloma is the primary, prostate cancer is also on file."""
    record = _patient(OrganizationFactory(), (900, PROSTATE), (100, MYELOMA))

    assert record.id in _listed(staff_client, 'Prostate Cancer')
    assert record.id in _listed(staff_client, 'Multiple Myeloma')


def test_not_listed_under_a_cancer_it_does_not_have(staff_client):
    record = _patient(OrganizationFactory(), (900, PROSTATE), (100, MYELOMA))

    assert record.id not in _listed(staff_client, 'Breast Cancer')


def test_filter_is_case_insensitive(staff_client):
    record = _patient(OrganizationFactory(), (900, PROSTATE), (100, MYELOMA))

    assert record.id in _listed(staff_client, 'prostate cancer')


def test_a_record_not_yet_rederived_still_matches_on_its_disease(staff_client):
    """Before a backfill the list is empty; the old single-disease match still applies."""
    record = PatientRecordFactory(disease='Multiple Myeloma', disease_slug='multiple-myeloma')
    PatientRecord.objects.filter(pk=record.pk).update(disease_slugs=[])

    assert record.id in _listed(staff_client, 'Multiple Myeloma')
    assert record.id not in _listed(staff_client, 'Prostate Cancer')


# --- org disease stats ----------------------------------------------------------

def _org_stats(client, org):
    resp = client.get('/api/stats/org-disease/')
    assert resp.status_code == 200
    entry = next(o for o in resp.data if o['org_slug'] == org.slug)
    return entry, {d['disease_slug']: d['count'] for d in entry['disease_counts']}


def test_stats_count_a_patient_under_each_cancer_and_once_in_the_total(staff_client):
    org = OrganizationFactory()
    _patient(org, (900, PROSTATE), (100, MYELOMA))
    _patient(org, (900, PROSTATE))

    entry, counts = _org_stats(staff_client, org)

    assert counts == {'prostate-cancer': 2, 'multiple-myeloma': 1}
    assert entry['owned_count'] == entry['total'] == 2
    labels = {d['disease_slug']: d['label'] for d in entry['disease_counts']}
    assert labels['prostate-cancer'] == 'Prostate Cancer'


def test_stats_read_the_primary_slug_when_the_list_is_empty(staff_client):
    org = OrganizationFactory()
    record = PatientRecordFactory(organization=org, disease_slug='multiple-myeloma')
    PatientRecord.objects.filter(pk=record.pk).update(disease_slugs=[])
    PatientRecordFactory(organization=org, disease_slug=None)

    entry, counts = _org_stats(staff_client, org)

    assert counts == {'multiple-myeloma': 1, '': 1}
    assert entry['owned_count'] == 2


def test_stats_count_an_edited_primary_not_in_the_list(staff_client):
    """A direct edit changes disease_slug alone; it is counted once, with the rest."""
    org = OrganizationFactory()
    record = PatientRecordFactory(organization=org)
    PatientRecord.objects.filter(pk=record.pk).update(
        disease_slug='follicular-lymphoma', disease_slugs=['multiple-myeloma', 'follicular-lymphoma'],
    )

    _, counts = _org_stats(staff_client, org)

    assert counts == {'multiple-myeloma': 1, 'follicular-lymphoma': 1}
