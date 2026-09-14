"""Component certification alone cannot establish writer prerequisites."""
from io import StringIO

import pytest
from django.core.management import CommandError, call_command
from django.db import connection
from django.test.utils import CaptureQueriesContext

from omop_core.models import Concept, FieldConceptMapping
from omop_core.services.genomics import _event_concept
from tests.factories import ConceptFactory
from tests.test_genomics_recipes import complete_vocabulary

pytestmark = pytest.mark.django_db


@pytest.fixture
def ready():
    complete_vocabulary()
    for concept_id in (0, 32817, 32865):
        ConceptFactory(concept_id=concept_id)
    return ConceptFactory(
        vocabulary__vocabulary_id='CDM', concept_code='measurement.measurement_id',
        concept_name='measurement.measurement_id',
    )


def audit(**kwargs):
    output = StringIO()
    call_command('audit_genomics_domains', stdout=output, **kwargs)
    return output.getvalue()


def failed_audit():
    output = StringIO()
    with pytest.raises(CommandError, match='not verified'):
        call_command('audit_genomics_domains', include_writer_prerequisites=True, stdout=output)
    assert 'Writer parent mappings and required concepts verified' not in output.getvalue()
    return output.getvalue()


def test_component_only_mode_does_not_claim_writer_readiness():
    complete_vocabulary()
    FieldConceptMapping.objects.filter(field_name='genomics_palb2').delete()
    assert 'All component domain assignments verified' in audit()
    assert 'Writer parent mappings' not in audit()
    output = failed_audit()
    assert 'genomics_palb2: missing or incomplete' in output
    assert 'CDM event:' in output


@pytest.mark.parametrize('identity', ['legacy', 'athena'])
def test_complete_check_uses_writer_event_identity_and_never_writes(ready, identity):
    if identity == 'athena':
        ready.concept_code = 'CDM126'
        ready.save(update_fields=['concept_code'])
    before = list(FieldConceptMapping.objects.order_by('pk').values())
    with CaptureQueriesContext(connection) as queries:
        output = audit(include_writer_prerequisites=True)
    assert all(query['sql'].lstrip().upper().startswith('SELECT') for query in queries)
    assert '42 priority parent recipes usable' in output
    assert f'measurement.measurement_id resolves to {_event_concept()}' in output
    assert 'Writer parent mappings and required concepts verified' in output
    assert list(FieldConceptMapping.objects.order_by('pk').values()) == before


@pytest.mark.parametrize('changes', [
    {'status': 'rejected'}, {'status': 'proposed'}, {'source_value': ''},
    {'source_value': 'x' * 51}, {'omop_table': 'observation'},
    {'omop_table': 'condition_occurrence'},
])
def test_invalid_parent_recipe_fails_even_with_complete_components(ready, changes):
    FieldConceptMapping.objects.filter(field_name='genomics_palb2').update(**changes)
    assert 'All component domain assignments verified' in audit()
    assert 'genomics_palb2:' in failed_audit()


@pytest.mark.parametrize('concept_id', [0, 32817, 32865])
@pytest.mark.parametrize('unavailable', ['missing', 'retired'])
def test_required_unmapped_and_actor_concepts_fail_when_unavailable(ready, concept_id, unavailable):
    if unavailable == 'missing':
        FieldConceptMapping.objects.filter(concept_id=concept_id).update(concept=None)
        Concept.objects.filter(pk=concept_id).delete()
    else:
        Concept.objects.filter(pk=concept_id).update(invalid_reason='D')
    assert f'required active concept {concept_id} unavailable' in failed_audit()


@pytest.mark.parametrize('changes', [
    {'invalid_reason': 'D'}, {'concept_code': 'CDM126', 'standard_concept': None},
    {'concept_code': 'CDM126', 'concept_name': 'observation.observation_id'},
])
def test_event_candidates_must_satisfy_writer_identity(ready, changes):
    Concept.objects.filter(pk=ready.pk).update(**changes)
    assert 'CDM event: active measurement.measurement_id identity unavailable' in failed_audit()


def test_wrong_vocabulary_does_not_supply_event_identity(ready):
    ready.delete()
    ConceptFactory(concept_code='measurement.measurement_id', concept_name='measurement.measurement_id')
    assert 'CDM event: active measurement.measurement_id identity unavailable' in failed_audit()


def test_observation_domain_parent_keeps_supported_source_only_storage(ready):
    parent = ConceptFactory(concept_code='81252-9', domain__domain_id='Observation')
    FieldConceptMapping.objects.filter(field_name__startswith='genomics_').update(concept=parent)
    output = audit(include_writer_prerequisites=True)
    assert '42 use concept 0 with source identity' in output
    assert 'Writer parent mappings and required concepts verified' in output


def test_component_failure_still_blocks_combined_check(ready):
    FieldConceptMapping.objects.filter(field_name='genetic_mutations.status').update(status='rejected')
    assert 'genetic_mutations.status: missing approved recipe' in failed_audit()
