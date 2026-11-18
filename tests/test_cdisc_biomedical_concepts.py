"""CDISC Biomedical Concepts load, become source codes, and are reached through a shared OMOP concept."""
import importlib
from types import SimpleNamespace

import pytest
from django.apps import apps
from django.db import connection

from omop_core.models import BiomedicalConcept, SourceCodeConceptMapping as Mapping
from omop_core.services.biomedical_concepts import biomedical_concept_mappings
from omop_core.services.mapping_browse import canonical_source, tab_queryset
from tests.factories import ConceptFactory, DomainFactory, VocabularyFactory

pytestmark = pytest.mark.django_db

load = importlib.import_module('omop_core.migrations.0354_load_cdisc_biomedical_concepts')
seed = importlib.import_module('omop_core.migrations.0355_cdisc_bcs_as_source_codes')
EDITOR = SimpleNamespace(connection=connection)


def load_catalog():
    """0354's catalog load, without the link table 0356 dropped."""
    BiomedicalConcept.objects.bulk_create(
        [BiomedicalConcept(**row) for row in load._read_payload()], batch_size=500)


def loinc(code, domain='Measurement'):
    return ConceptFactory(
        concept_code=code, vocabulary=VocabularyFactory(vocabulary_id='LOINC'),
        domain=DomainFactory(domain_id=domain))


def run_seed():
    seed.seed_bc_source_codes(apps, EDITOR)


def bc_rows():
    return Mapping.objects.filter(source_vocabulary_id='CDISC-BC')


def test_the_package_holds_every_bc_with_its_ncit_code():
    load_catalog()
    assert BiomedicalConcept.objects.count() == 1588
    assert BiomedicalConcept.objects.exclude(ncit_code='').count() == 1582
    heart_rate = BiomedicalConcept.objects.get(bc_id='C49677')
    assert heart_rate.short_name == 'Heart Rate' and 'Vital Signs' in heart_rate.categories


def test_every_seeded_bc_is_in_the_package_and_has_one_destination():
    load_catalog()
    ids = [bc for bc, _, _ in seed.BC_DESTINATIONS]
    assert len(ids) == len(set(ids))
    assert BiomedicalConcept.objects.filter(bc_id__in=ids).count() == len(ids)


def test_a_bc_becomes_a_proposed_source_code_in_its_wearable_group():
    load_catalog()
    heart = loinc('8867-4')
    Mapping.objects.create(
        source_vocabulary_id='OpenWearables', source_code='heart_rate', target_concept=heart,
        status='approved', omop_table='measurement', domain_id='Measurement',
        category='Heart & Cardiovascular', metric_key='heart_rate')
    run_seed()
    row = bc_rows().get()  # only the BC whose concept is loaded here
    assert (row.source_code, row.source_code_description) == ('C49677', 'Heart Rate')
    assert row.target_concept_id == heart.pk and row.destination_vocabulary_id == 'LOINC'
    assert (row.status, row.omop_table, row.organization_id) == ('proposed', 'measurement', None)
    assert (row.category, row.metric_key) == ('Heart & Cardiovascular', 'heart_rate')
    row.full_clean()
    # It is a row of the Wearables tab, so it is browsed, voted on and exported there.
    assert row in tab_queryset(Mapping.objects.all(), canonical_source('OpenWearables'))
    assert canonical_source('CDISC-BC') == canonical_source('OpenWearables')


def test_rerun_keeps_a_curators_row_and_adds_bcs_whose_concept_arrived():
    load_catalog()
    loinc('8867-4')
    run_seed()
    bc_rows().update(status='approved', notes='checked')
    loinc('9279-1')
    run_seed()
    assert bc_rows().count() == 2
    kept = bc_rows().get(source_code='C49677')
    assert (kept.status, kept.notes) == ('approved', 'checked')


def test_a_bc_with_a_concept_outside_measurement_and_observation_is_skipped():
    load_catalog()
    loinc('8867-4', domain='Procedure')
    run_seed()
    assert not bc_rows().exists()


def test_wearable_codes_reach_the_bc_that_shares_their_destination():
    heart, resting = loinc('8867-4'), loinc('40443-4')
    bc = Mapping.objects.create(
        source_vocabulary_id='CDISC-BC', source_code='C49677', target_concept=heart, status='proposed')
    apple = Mapping.objects.create(
        source_vocabulary_id='Apple', source_code='HKQuantityTypeIdentifierHeartRate',
        target_concept=heart, status='approved')
    garmin = Mapping.objects.create(
        source_vocabulary_id='Garmin', source_code='heart_rate', target_concept=heart, status='approved')
    other = Mapping.objects.create(
        source_vocabulary_id='Apple', source_code='HKQuantityTypeIdentifierRestingHeartRate',
        target_concept=resting, status='approved')
    unmapped = Mapping.objects.create(source_vocabulary_id='OpenWearables', source_code='daylight')
    assert biomedical_concept_mappings(apple) == [bc]
    assert biomedical_concept_mappings(garmin) == [bc]
    assert biomedical_concept_mappings(other) == []     # one destination per BC
    assert biomedical_concept_mappings(unmapped) == []
    assert biomedical_concept_mappings(bc) == []        # a BC does not reach itself
    bc.status = 'rejected'
    bc.save()
    assert biomedical_concept_mappings(apple) == []
    assert biomedical_concept_mappings(apple, statuses=('rejected',)) == [bc]
