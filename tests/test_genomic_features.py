"""Feature-level findings remain distinct from source gene/variant annotations."""
from importlib import import_module

import pytest
from django.apps import apps
from django.db import connection
from rest_framework.test import APIClient

from omop_core.models import FieldConceptMapping, Measurement, Observation
from omop_core.services.genomics import list_variants, save_variant
from omop_core.services.genomics_features import describe_finding
from omop_core.services.patient_record_service import refresh_patient_record
from tests.factories import MeasurementFactory
from tests.test_genomics_crud import setup, call  # noqa: F401

pytestmark = pytest.mark.django_db


@pytest.mark.parametrize('gene,name,feature,kind,category', [
    ('TP53', 'p.R175H', 'TP53', 'Gene', None),
    ('TP53', 'del17p', '17p', 'Chromosome arm/region', 'Deletion'),
    ('11q', 'del11q', '11q', 'Chromosome arm/region', 'Deletion'),
    ('13q', 'del13q', '13q', 'Chromosome arm/region', 'Deletion'),
    ('12', 'Trisomy 12', 'Chromosome 12', 'Chromosome(s)', 'Aneuploidy'),
])
def test_legacy_findings_get_precise_features_without_rewriting_source(setup, gene, name, feature, kind, category):
    person, _, staff = setup
    parent = MeasurementFactory(person=person, measurement_source_value='81252-9',
                                qualifier_source_value=gene, value_as_string=name)
    before = Measurement.objects.filter(pk=parent.pk).values().get()
    finding = call(person, staff, 'get').data[0]
    assert finding['genomic_feature'] == feature
    assert finding['feature_type'] == kind
    assert finding.get('finding_category') == category
    assert finding['gene'] == gene.lower()
    assert finding['variant'] == name
    assert Measurement.objects.filter(pk=parent.pk).values().get() == before
    assert not Observation.objects.filter(person=person).exists()
    # An ordinary edit preserves the original source text alongside classification.
    edited = call(person, staff, 'patch', {**finding, 'laboratory': 'Reviewed lab'}, parent.pk)
    assert edited.status_code == 200, edited.data
    assert edited.data['genomic_feature'] == feature
    assert edited.data['variant'] == name


@pytest.mark.parametrize('feature,kind,category,name', [
    ('TP53', 'Gene', 'Sequence variant', 'p.R175H'),
    ('17p', 'Chromosome arm/region', 'Deletion', 'del17p'),
    ('Chromosome 12', 'Chromosome(s)', 'Aneuploidy', 'Trisomy 12'),
    ('ETV6/NTRK3', 'Rearrangement partners', 'Translocation', 'ETV6-NTRK3 fusion'),
    ('5q', 'Chromosome arm/region', 'Deletion', 'del(5q)'),
    ('Chromosomes', 'Chromosome(s)', 'Ploidy abnormality', 'Hypodiploidy'),
])
def test_new_feature_round_trips_and_nongenes_do_not_create_gene_components(setup, feature, kind, category, name):
    person, record, staff = setup
    response = call(person, staff, 'post', {
        'genomic_feature': feature, 'feature_type': kind, 'finding_category': category,
        'variant_name': name, 'variant_description': 'Original lab report',
        'variant_analysis_method_type': 'FISH', 'report_id': 'report-1459',
    })
    assert response.status_code == 201, response.data
    saved = response.data
    assert saved['genomic_feature'] == feature
    assert saved['feature_type'] == kind
    assert saved['finding_category'] == category
    if kind != 'Gene':
        assert not Observation.objects.filter(person=person, observation_source_value='48018-6').exists()
        assert not Measurement.objects.filter(person=person, measurement_source_value='48018-6').exists()
    refresh_patient_record(person)
    record.refresh_from_db()
    assert record.genetic_mutations == [saved]
    edited = call(person, staff, 'patch', {**saved, 'laboratory': 'Updated lab'}, saved['id'])
    assert edited.status_code == 200, edited.data
    assert edited.data['variant_description'] == 'Original lab report'
    assert edited.data['finding_category'] == category
    assert call(person, staff, 'delete', variant_id=saved['id']).status_code == 204
    assert list_variants(person) == []


def test_priority_placeholders_and_named_patch_use_chromosome_feature(setup):
    person, _, staff = setup
    client = APIClient()
    client.force_authenticate(staff)
    catalog = client.get(f'/api/v1/patient-records/{person.pk}/genomics-catalog/', {'disease': 'CLL'}).data
    deletion = next(m for m in catalog['markers'] if m['key'] == 'del17p')
    assert deletion['genomic_feature'] == '17p'
    assert deletion['feature_type'] == 'Chromosome arm/region'
    result = client.patch(f'/api/v1/patient-records/{person.pk}/', {'genomics_del17p': [{
        'genomic_feature': '17p', 'feature_type': 'Chromosome arm/region',
        'finding_category': 'Deletion', 'status': 'absent', 'variant_analysis_method_type': 'FISH',
    }]}, format='json')
    assert result.status_code == 200, result.data
    assert result.data['genomics_del17p'][0]['genomic_feature'] == '17p'
    assert result.data['genomics_tp53'] == []
    assert result.data['genomics_del17p'][0]['status'] == 'absent'


@pytest.mark.parametrize('change', [
    {'feature_type': 'invalid'}, {'feature_type': ''}, {'finding_category': 'invalid'},
    {'genomic_feature': 'TP53', 'feature_type': 'Gene', 'marker_key': 'del17p'},
])
def test_invalid_feature_payload_is_atomic(setup, change):
    person, _, staff = setup
    result = call(person, staff, 'post', {
        'genomic_feature': '17p', 'feature_type': 'Chromosome arm/region', **change,
    })
    assert result.status_code == 400, result.data
    assert not Measurement.objects.filter(person=person).exists()
    assert not Observation.objects.filter(person=person).exists()


def test_unknown_and_legacy_categories_are_preserved_without_guessing():
    from omop_core.services.genomics_catalog import marker_for_variant
    assert marker_for_variant({'gene': 'MYC', 'variant_name': 'MYC'}) is None
    assert describe_finding({'gene': 'TP53'}).get('finding_category') is None
    assert describe_finding({'gene': 'TP53', 'variant_category': 'Structural variant'}).get('finding_category') is None
    finding = describe_finding({'gene': 'TP53', 'variant_category': 'Simple variant'})
    assert finding['finding_category'] == 'Sequence variant'
    assert finding['variant_category'] == 'Simple variant'
    assert describe_finding({'gene': 'TP53', 'variant': 'unknown loss near TP53'}).get('finding_category') is None


def test_feature_recipe_migration_is_additive_idempotent_and_preserves_curation(setup):
    person, _, _ = setup
    saved = save_variant(person, {'gene': 'TP53', 'variant': 'p.R175H'})
    mapping = FieldConceptMapping.objects.get(field_name='genetic_mutations.feature_type')
    mapping.status = 'rejected'
    mapping.source_value = 'reviewed:feature_type'
    mapping.save()
    before = FieldConceptMapping.objects.filter(pk=mapping.pk).values().get()
    seed = import_module('omop_core.migrations.0252_genomic_feature_recipes').seed_feature_recipes
    with connection.schema_editor() as editor:
        seed(apps, editor)
        seed(apps, editor)
    assert FieldConceptMapping.objects.filter(pk=mapping.pk).values().get() == before
    assert list_variants(person) == [saved]
