import json
from importlib import import_module
from io import StringIO

import pytest
from django.apps import apps
from django.core.management import call_command
from django.db import connection
from django.db.migrations.loader import MigrationLoader
from django.test.utils import CaptureQueriesContext, override_settings

from omop_core.data_migrations.snomed_relationships_v1 import (
    reconcile, summarize, SINGLE_ORIGIN, MULTIPLE_ORIGIN,
)
from omop_core.mapping.code_resolution import resolve_source_code
from omop_core.models import ConceptRelationship, Relationship, SourceCodeConceptMapping as Mapping, MappingDestinationCandidate as Candidate
from tests.test_standard_snomed_identity import concept, proposal

pytestmark = pytest.mark.django_db


@pytest.fixture
def historical_apps():
    with override_settings(MIGRATION_MODULES={}):
        return MigrationLoader(connection).project_state([
            ('omop_core', '0261_prefer_standard_snomed_identities'),
        ]).apps


def edge(source, target, **changes):
    rel, _ = Relationship.objects.get_or_create(relationship_id='Maps to', defaults=dict(
        relationship_name='Maps to', is_hierarchical=0, defines_ancestry=0,
        reverse_relationship_id='Mapped from', relationship_concept_id=0))
    return ConceptRelationship.objects.create(concept_1=source, concept_2=target, relationship=rel,
        **(dict(valid_start_date='1970-01-01',valid_end_date='2099-12-31',invalid_reason=None) | changes))


def setup_mapping(**source_changes):
    source=concept(standard_concept=None,**source_changes)
    row=proposal(source)
    target=concept('RxNorm','standard','Drug')
    edge(source,target)
    return source,row,target


def test_actual_migration_uses_local_tables_and_retains_audit(historical_apps):
    source,row,target=setup_mapping()
    before=Mapping.objects.values().get(pk=row.pk)
    candidate=Candidate.objects.values().get(mapping=row)
    migration=import_module('omop_core.migrations.0262_reconcile_snomed_local_relationships')
    with connection.schema_editor(atomic=False) as editor:
        migration.reconcile_snomed_local_relationships(historical_apps,editor)
    row.refresh_from_db()
    assert (row.status,row.target_concept_id,row.source_concept_id)==('approved',target.pk,source.pk)
    assert (row.destination_vocabulary_id,row.domain_id,row.omop_table)==('RxNorm','Drug','drug_exposure')
    assert row.origin_system==SINGLE_ORIGIN
    for field in ['source','occurrence_count','first_seen','last_seen','reviewer_id','reviewed_at','source_code_description']:
        assert getattr(row,field)==before[field]
    audit=json.loads(row.notes.split('Local SNOMED Maps to reconciliation (#1584): ')[1])
    assert audit['candidates']==[candidate] and audit['relationships'][0]['concept_2_id']==target.pk
    assert list(Candidate.objects.filter(mapping=row).values_list('target_concept_id',flat=True))==[target.pk]
    assert reconcile(historical_apps,connection)==[]
    found,_=resolve_source_code(source_vocabulary_id='SNOMED',source_code=source.concept_code,omop_table='drug_exposure')
    assert found.pk==target.pk


def test_multiple_targets_are_proposed_and_not_auto_promoted(historical_apps):
    source,row,target=setup_mapping()
    other=concept('RxNorm','other','Drug');edge(source,other)
    assert summarize(reconcile(historical_apps,connection))=={'proposed_multiple':1}
    row.refresh_from_db()
    assert row.status=='proposed' and row.target_concept_id is None and row.origin_system==MULTIPLE_ORIGIN
    assert set(Candidate.objects.filter(mapping=row).values_list('target_concept_id',flat=True))=={target.pk,other.pk}
    # Even if a later vocabulary load makes the source standard, the choice
    # remains a curator decision, rather than silently discarding alternatives.
    source.standard_concept='S';source.domain=target.domain;source.save()
    found,_=resolve_source_code(source_vocabulary_id='SNOMED',source_code=source.concept_code,omop_table='drug_exposure')
    assert found is None
    assert reconcile(historical_apps,connection)==[]


@pytest.mark.parametrize('domain,table',[('Observation','observation'),('Device','')])
def test_actual_domain_is_preserved_and_wrong_table_blocked(historical_apps,domain,table):
    source,row,target=setup_mapping()
    target.domain=concept(code='domain',domain=domain).domain;target.save()
    reconcile(historical_apps,connection)
    row.refresh_from_db()
    assert (row.domain_id,row.omop_table)==(domain,table)
    assert resolve_source_code(source_vocabulary_id='SNOMED',source_code=source.concept_code,omop_table='drug_exposure')[0] is None


@pytest.mark.parametrize('changes',[{'invalid_reason':'D'},{'valid_end_date':'2000-01-01'},{'valid_start_date':'2099-01-01'}])
def test_inactive_relationship_is_not_used(historical_apps,changes):
    source,row,target=setup_mapping()
    ConceptRelationship.objects.filter(concept_1=source).update(**changes)
    before=Mapping.objects.values().get(pk=row.pk)
    assert summarize(reconcile(historical_apps,connection))=={'no_standard_destination':1}
    assert Mapping.objects.values().get(pk=row.pk)==before


@pytest.mark.parametrize('changes',[{'standard_concept':None},{'standard_concept':'C'},{'invalid_reason':'D'},
                                    {'valid_end_date':'2000-01-01'},{'valid_start_date':'2099-01-01'},{'source':'HealthKey'}])
def test_invalid_target_cannot_be_approved(historical_apps,changes):
    source,row,target=setup_mapping()
    type(target).objects.filter(pk=target.pk).update(**changes)
    assert summarize(reconcile(historical_apps,connection))=={'no_standard_destination':1}
    row.refresh_from_db();assert row.status=='proposed' and row.target_concept_id is None


def test_deprecated_vocabulary_is_not_used(historical_apps):
    _,_,target=setup_mapping()
    target.vocabulary.is_deprecated=True;target.vocabulary.save()
    assert summarize(reconcile(historical_apps,connection))=={'no_standard_destination':1}


def test_current_maps_to_can_resolve_retired_source(historical_apps):
    _,row,_=setup_mapping(invalid_reason='U')
    assert summarize(reconcile(historical_apps,connection))=={'approved_single':1}
    row.refresh_from_db();assert row.status=='approved'


@pytest.mark.parametrize('changes',[{'status':'approved'},{'status':'rejected'},{'origin':'curator'},
    {'origin_system':'curator'},{'suggestion_model_version':'v1'},{'last_suggest_attempt':'v1'},{'suggestion_outcome':'rejected'}])
def test_curator_decisions_are_protected(historical_apps,changes):
    _,row,_=setup_mapping();Mapping.objects.filter(pk=row.pk).update(**changes)
    assert reconcile(historical_apps,connection)==[]


def test_other_candidates_and_historical_reviews_are_protected(historical_apps):
    from omop_core.models import MappingSuggestionReview
    _,row,target=setup_mapping()
    candidate=Candidate.objects.create(mapping=row,target_vocabulary_id='LOINC',target_concept_code='other')
    assert reconcile(historical_apps,connection)==[]
    candidate.delete()
    MappingSuggestionReview.objects.create(mapping=row,source_code=row.source_code,source_vocabulary_id='SNOMED',
        suggestion_model_version='v1',suggestion_outcome='rejected')
    assert reconcile(historical_apps,connection)==[]


def test_preview_only_reads_and_matches_apply(historical_apps):
    setup_mapping()
    with CaptureQueriesContext(connection) as queries:
        preview=reconcile(historical_apps,connection,dry_run=True)
    assert not any(q['sql'].lstrip().upper().startswith(('INSERT','UPDATE','DELETE')) or 'FOR UPDATE' in q['sql'] for q in queries)
    assert reconcile(historical_apps,connection)==preview


def test_batch_rechecks_concurrent_curation(historical_apps):
    source,first,target=setup_mapping()
    second_source=concept(code='second',standard_concept=None);second=proposal(second_source);edge(second_source,target)
    def curate(_):Mapping.objects.filter(pk=second.pk).update(status='rejected')
    receipts=reconcile(historical_apps,connection,batch_size=1,on_batch=curate)
    assert summarize(receipts)=={'approved_single':1,'changed_or_protected':1}
    second.refresh_from_db();assert second.status=='rejected'


def test_reimport_cannot_restore_bad_candidates(tmp_path,historical_apps):
    source,row,target=setup_mapping();other=concept('RxNorm','other','Drug');edge(source,other)
    reconcile(historical_apps,connection)
    path=tmp_path/'crossmap.json'
    path.write_text(json.dumps({'mappings':[dict(source_vocabulary_id='SNOMED',source_code=source.concept_code,
        target_vocabulary_id='RxNorm',target_concept_code='rx',domain_id='Drug',status='approved',origins=['HT-One'])]}))
    call_command('import_healthtree_crossmaps',artifact=str(path),stdout=StringIO())
    row.refresh_from_db()
    assert row.status=='proposed' and row.origin_system==MULTIPLE_ORIGIN
    assert set(Candidate.objects.filter(mapping=row).values_list('target_concept_id',flat=True))=={target.pk,other.pk}


def test_audit_command_is_read_only():
    _,row,_=setup_mapping();out=StringIO()
    call_command('audit_snomed_relationship_mappings',stdout=out)
    assert json.loads(out.getvalue())['summary']=={'approved_single':1}
    row.refresh_from_db();assert row.status=='proposed'


def test_human_edits_and_locks_are_protected(historical_apps):
    from patient_portal.models import Identity
    user=Identity.objects.create_user(email='relationship-reviewer@example.test')
    _,row,_=setup_mapping()
    for field in ['created_by','updated_by','reviewer','locked_by']:
        Mapping.objects.filter(pk=row.pk).update(**{field:user})
        assert reconcile(historical_apps,connection)==[]
        Mapping.objects.filter(pk=row.pk).update(**{field:None})


def test_source_identity_and_existing_valid_destination_are_preserved(historical_apps):
    source,row,target=setup_mapping()
    source.standard_concept='S';source.save()
    assert summarize(reconcile(historical_apps,connection))=={'standard_identity':1}
    Mapping.objects.filter(pk=row.pk).update(target_concept=target)
    assert reconcile(historical_apps,connection)==[]


def test_valid_destination_survives_invalid_alternative(historical_apps):
    source,row,target=setup_mapping()
    other=concept('RxNorm','nonstandard','Drug',standard_concept=None);edge(source,other)
    assert summarize(reconcile(historical_apps,connection))=={'approved_single':1}
    row.refresh_from_db();assert row.target_concept_id==target.pk


def test_mixed_target_domains_do_not_invent_a_domain(historical_apps):
    source,row,target=setup_mapping()
    other=concept('SNOMED','observation','Observation');edge(source,other)
    reconcile(historical_apps,connection)
    row.refresh_from_db()
    assert row.status=='proposed' and row.domain_id==row.omop_table==row.destination_vocabulary_id==''
    assert Candidate.objects.filter(mapping=row).count()==2
