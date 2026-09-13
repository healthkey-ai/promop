"""Portable recipes retain source facts and never certify incomplete vocabulary."""
from importlib import import_module
from io import StringIO

import pytest
from django.apps import apps
from django.core.management import call_command, CommandError
from django.db import connection

from omop_core.models import Concept, FieldConceptMapping, Observation
from omop_core.services.genomics import list_variants, mapped_concept, save_variant
from omop_core.services.genomics_components import components
from omop_core.services.genomics_vocabulary import resolve_loinc
from tests.factories import ConceptFactory
from tests.test_genomics_crud import setup  # noqa: F401
from tests.test_suggestion_context import relation

pytestmark = pytest.mark.django_db
migration = import_module('omop_core.migrations.0231_genomics_variant_name_recipe')


def promote():
    with connection.schema_editor() as editor:
        migration.promote_variant_name(apps, editor)


def original_seed():
    call_command('seed_genomics_catalog')
    row = FieldConceptMapping.objects.get(field_name='genetic_mutations.variant_name')
    FieldConceptMapping.objects.filter(pk=row.pk).update(
        vocabulary_id='', concept_code='', source_value='genomics:variant_name',
        concept=None, omop_table='observation', value_kind='string', unit='',
        value_vocabulary='', type_concept_id=32817, multiple=False, status='approved',
        reviewer=None, provenance='', notes=migration._ORIGINAL_NOTES,
    )
    row.refresh_from_db()
    return row


@pytest.mark.parametrize('load_first', [False, True])
def test_portable_recipe_resolves_before_or_after_vocab_load(load_first):
    row = original_seed()
    if load_first:
        concept = ConceptFactory(concept_code='81253-7', domain__domain_id='Observation')
    promote()
    row.refresh_from_db()
    assert row.concept_code == '81253-7'
    assert row.source_value == 'genomics:variant_name'
    assert row.provenance == 'system_generated'
    if not load_first:
        assert mapped_concept(row) == (0, None)
        concept = ConceptFactory(concept_code='81253-7', domain__domain_id='Observation')
    assert mapped_concept(row) == (concept.pk, concept.pk)
    before = FieldConceptMapping.objects.filter(pk=row.pk).values().get()
    call_command('seed_genomics_catalog')
    assert FieldConceptMapping.objects.filter(pk=row.pk).values().get() == before


@pytest.mark.parametrize('change', [
    {'status': 'rejected'}, {'status': 'proposed'}, {'provenance': 'curator'},
    {'notes': 'Reviewed name recipe'}, {'source_value': 'reviewed:variant_name'},
    {'unit': 'reviewed'}, {'omop_table': 'measurement'}, {'value_vocabulary': 'Reviewed'},
])
def test_promotion_preserves_curator_and_nonmatching_legacy_recipes(change):
    row = original_seed()
    FieldConceptMapping.objects.filter(pk=row.pk).update(**change)
    before = FieldConceptMapping.objects.filter(pk=row.pk).values().get()
    promote()
    call_command('seed_genomics_catalog')
    assert FieldConceptMapping.objects.filter(pk=row.pk).values().get() == before


@pytest.mark.parametrize('source', ['genomics:variant_name', '81253-7', 'reviewed:variant_name'])
def test_variant_name_aliases_survive_read_and_edit(setup, source):
    person, _, _ = setup
    saved = save_variant(person, {'gene': 'BRCA1', 'variant_name': 'legacy name'})
    row = Observation.objects.get(person=person, observation_source_value='genomics:variant_name', is_erroneous=False)
    Observation.objects.filter(pk=row.pk).update(observation_source_value=source)
    if source.startswith('reviewed:'):
        FieldConceptMapping.objects.filter(field_name='genetic_mutations.variant_name').update(source_value=source)
    assert list_variants(person)[0]['variant_name'] == 'legacy name'
    changed = save_variant(person, {'gene': 'BRCA1', 'variant_name': 'new name'}, variant_id=saved['id'])
    assert changed['variant_name'] == 'new name'
    row.refresh_from_db()
    assert row.is_erroneous
    assert Observation.objects.filter(person=person, observation_event_id=saved['id'], is_erroneous=False).count() == 1


def complete_vocabulary():
    call_command('seed_genomics_catalog')
    for attribute in components():
        code = attribute.get('concept_code') or attribute['code']
        if code.startswith('genomics:') or not code:
            continue
        concept = ConceptFactory(concept_code=code, domain__domain_id=attribute['table'].title())
        # Simulate reviewed repair of recipes seeded before vocabulary loading.
        FieldConceptMapping.objects.filter(field_name='genetic_mutations.' + attribute['key']).update(
            vocabulary_id='LOINC', concept_code=code, concept=concept,
            omop_table=concept.domain_id.lower(), provenance='curator',
        )


def audit():
    output = StringIO()
    call_command('audit_genomics_domains', stdout=output)
    return output.getvalue()


def test_no_vocabulary_and_empty_recipes_cannot_pass_audit():
    FieldConceptMapping.objects.filter(field_name__startswith='genetic_mutations.').delete()
    with pytest.raises(CommandError, match='not verified'):
        audit()
    call_command('seed_genomics_catalog')
    with pytest.raises(CommandError, match='not verified'):
        audit()


def test_complete_audit_then_missing_code_or_recipe_fail():
    complete_vocabulary()
    assert 'All component domain assignments verified' in audit()
    Concept.objects.filter(concept_code='81253-7').update(invalid_reason='D')
    with pytest.raises(CommandError):
        audit()
    Concept.objects.filter(concept_code='81253-7').update(invalid_reason=None)
    FieldConceptMapping.objects.filter(field_name='genetic_mutations.origin').update(status='rejected')
    with pytest.raises(CommandError):
        audit()


@pytest.mark.parametrize('code', ['81290-9', '81304-8', '83005-9', '82121-5'])
def test_domain_drift_requires_reviewed_repair_and_preserves_raw_source(code):
    complete_vocabulary()
    row = FieldConceptMapping.objects.get(concept_code=code, field_name__startswith='genetic_mutations.')
    FieldConceptMapping.objects.filter(pk=row.pk).update(omop_table='measurement', concept=None)
    row.refresh_from_db()
    assert mapped_concept(row)[0] == 0
    assert mapped_concept(row)[1] == Concept.objects.get(concept_code=code).pk
    before = FieldConceptMapping.objects.filter(pk=row.pk).values().get()
    output = StringIO()
    with pytest.raises(CommandError):
        call_command('audit_genomics_domains', stdout=output)
    assert 'REVIEW REPAIR: omop_table=observation' in output.getvalue()
    assert FieldConceptMapping.objects.filter(pk=row.pk).values().get() == before
    FieldConceptMapping.objects.filter(pk=row.pk).update(omop_table='observation')
    assert 'All component domain assignments verified' in audit()


def test_maps_to_resolution_and_ambiguity_are_shared_with_writer_and_audit():
    complete_vocabulary()
    source = Concept.objects.get(concept_code='81253-7')
    source.standard_concept = None
    source.save(update_fields=['standard_concept'])
    target = ConceptFactory(concept_code='mapped-variant-name', domain__domain_id='Observation')
    relation(source, target)
    row = FieldConceptMapping.objects.get(field_name='genetic_mutations.variant_name')
    row.concept = None
    row.save(update_fields=['concept'])
    assert mapped_concept(row) == (target.pk, source.pk)
    assert resolve_loinc('81253-7').standard == target
    assert 'All component domain assignments verified' in audit()
    other = ConceptFactory(concept_code='other-target')
    relation(source, other)
    assert mapped_concept(row) == (0, source.pk)
    assert resolve_loinc('81253-7').problem == 'ambiguous Maps to'
    with pytest.raises(CommandError):
        audit()


@pytest.mark.parametrize('key,code,value', [
    ('status', '69548-6', 'present'),
    ('transcript_dna_change', '48004-6', 'c.123A>G'),
    ('coverage_depth', '82121-5', 120),
    ('amino_acid_change_type', '48006-1', 'missense'),
])
def test_later_portable_component_aliases_are_read_and_superseded(setup, key, code, value):
    from omop_core.models import Measurement
    person, _, _ = setup
    saved = save_variant(person, {'gene': 'BRCA1', 'variant': 'example', key: value})
    mapping = FieldConceptMapping.objects.get(field_name='genetic_mutations.' + key)
    model = Measurement if mapping.omop_table == 'measurement' else Observation
    prefix = mapping.omop_table
    row = model.objects.get(person=person, **{prefix + '_source_value': mapping.source_value}, is_erroneous=False)
    model.objects.filter(pk=row.pk).update(**{prefix + '_source_value': code})
    assert list_variants(person)[0][key] == value
    save_variant(person, {'gene': 'BRCA1', 'variant': 'edited', key: value}, variant_id=saved['id'])
    row.refresh_from_db()
    assert row.is_erroneous
    assert list_variants(person)[0][key] == value


def test_invalid_maps_to_and_unsupported_target_cannot_resolve():
    source = ConceptFactory(concept_code='81253-7', standard_concept=None)
    target = ConceptFactory(concept_code='obsolete-target', domain__domain_id='Observation')
    relationship = relation(source, target, invalid_reason='D')
    assert resolve_loinc('81253-7').problem == 'missing standard target'
    relationship.invalid_reason = None
    relationship.save(update_fields=['invalid_reason'])
    target.invalid_reason = 'D'
    target.save(update_fields=['invalid_reason'])
    assert resolve_loinc('81253-7').problem == 'missing standard target'
    unsupported = ConceptFactory(concept_code='condition-target', domain__domain_id='Condition')
    relation(source, unsupported)
    assert resolve_loinc('81253-7').problem == 'unsupported standard domain Condition'


def test_reviewed_maps_to_recipe_keeps_source_and_standard_ids_distinct():
    source = ConceptFactory(concept_code='81253-7', standard_concept=None)
    target = ConceptFactory(concept_code='reviewed-standard', domain__domain_id='Observation')
    relation(source, target)
    row = original_seed()
    promote()
    row.refresh_from_db()
    row.concept = target
    row.provenance = 'curator'
    row.save(update_fields=['concept', 'provenance'])
    assert mapped_concept(row) == (target.pk, source.pk)


@pytest.mark.parametrize('changes', [
    {'source_value': ''}, {'omop_table': 'condition_occurrence'}, {'value_kind': 'number'},
])
def test_incomplete_local_storage_recipe_cannot_pass_audit(changes):
    complete_vocabulary()
    FieldConceptMapping.objects.filter(field_name='genetic_mutations.origin').update(**changes)
    with pytest.raises(CommandError):
        audit()
