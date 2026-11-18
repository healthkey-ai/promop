"""CDISC Biomedical Concepts load, and wearable codes reach them through their OMOP concept."""
import importlib
from types import SimpleNamespace

import pytest
from django.db import connection

from omop_core.models import (
    BiomedicalConcept, ConceptBiomedicalConceptMapping as Link, SourceCodeConceptMapping,
)
from omop_core.services.biomedical_concepts import biomedical_concept_links
from tests.factories import ConceptFactory

migration = importlib.import_module('omop_core.migrations.0354_load_cdisc_biomedical_concepts')


def run():
    from django.apps import apps
    migration.load_concepts_and_proposals(apps, SimpleNamespace(connection=connection))


@pytest.mark.django_db
def test_loads_the_whole_package_and_names_ncit_codes():
    run()
    assert BiomedicalConcept.objects.count() == 1588
    assert BiomedicalConcept.objects.exclude(ncit_code='').count() == 1582
    heart_rate = BiomedicalConcept.objects.get(bc_id='C49677')
    assert heart_rate.short_name == 'Heart Rate' and heart_rate.ncit_code == 'C49677'
    assert 'Vital Signs' in heart_rate.categories


@pytest.mark.django_db
def test_every_proposal_names_a_loaded_bc_and_is_only_proposed():
    run()
    assert Link.objects.count() == len(migration.CROSSWALK)
    assert set(Link.objects.values_list('status', flat=True)) == {'proposed'}
    assert set(Link.objects.values_list('bc_id', flat=True)) <= set(
        BiomedicalConcept.objects.values_list('bc_id', flat=True))


@pytest.mark.django_db
def test_rerun_keeps_a_curators_decision_and_adds_nothing():
    run()
    Link.objects.filter(target_concept_code='8867-4').update(status='approved', notes='checked')
    Link.objects.filter(target_concept_code='9279-1').delete()
    run()
    assert Link.objects.get(target_concept_code='8867-4').status == 'approved'
    assert Link.objects.get(target_concept_code='8867-4').notes == 'checked'
    assert Link.objects.count() == len(migration.CROSSWALK)
    assert BiomedicalConcept.objects.count() == 1588


@pytest.mark.django_db
def test_migration_writes_no_code_mapping():
    before = SourceCodeConceptMapping.objects.count()
    run()
    assert SourceCodeConceptMapping.objects.count() == before


@pytest.mark.django_db
def test_wearable_code_reaches_bc_through_its_destination_concept():
    run()
    concept = ConceptFactory(vocabulary__vocabulary_id='LOINC', concept_code='8867-4')
    mapping = SourceCodeConceptMapping.objects.create(
        source_vocabulary_id='OpenWearables', source_code='heart_rate', target_concept=concept,
        status='approved', omop_table='measurement')
    assert [l.bc_id for l in biomedical_concept_links(mapping)] == ['C49677']
    Link.objects.filter(bc_id='C49677', target_concept_code='8867-4').update(status='rejected')
    assert biomedical_concept_links(mapping) == []
    assert [l.bc_id for l in biomedical_concept_links(mapping, statuses=('rejected',))] == ['C49677']


@pytest.mark.django_db
def test_code_without_destination_has_no_bc():
    mapping = SourceCodeConceptMapping.objects.create(
        source_vocabulary_id='OpenWearables', source_code='time_in_daylight', status='proposed')
    assert biomedical_concept_links(mapping) == []
