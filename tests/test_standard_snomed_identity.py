import json
from importlib import import_module
from io import StringIO

import pytest
from django.apps import apps
from django.core.management import call_command
from django.db import connection
from django.db.migrations.loader import MigrationLoader
from django.test.utils import CaptureQueriesContext, override_settings
from rest_framework.test import APIClient

from omop_core.data_migrations.snomed_crossmap_v1 import IDENTITY_ORIGIN, reconcile, summarize
from omop_core.mapping.code_resolution import resolve_source_code
from omop_core.models import SourceCodeConceptMapping as Mapping, MappingDestinationCandidate as Candidate
from tests.factories import ConceptFactory, DomainFactory, VocabularyFactory

pytestmark = pytest.mark.django_db


@pytest.fixture
def historical_apps():
    with override_settings(MIGRATION_MODULES={}):
        return MigrationLoader(connection).project_state([
            ('omop_core', '0259_normalize_snomed_oid_mappings'),
        ]).apps


def concept(vocab='SNOMED', code='source', domain='Observation', **changes):
    return ConceptFactory(vocabulary=VocabularyFactory(vocabulary_id=vocab), concept_code=code,
                          domain=DomainFactory(domain_id=domain), **changes)


def proposal(source, target=None, **changes):
    row = Mapping.objects.create(**(dict(
        source_vocabulary_id='SNOMED', source_code=source.concept_code,
        target_concept=target, domain_id='Drug', omop_table='drug_exposure',
        origin='import', origin_system='HT-One', source='HT-One', status='proposed',
        occurrence_count=19, notes='Imported crossmap evidence',
    ) | changes))
    Candidate.objects.create(mapping=row, target_vocabulary_id='RxNorm', target_concept_code='rx',
                             target_concept=target, origins=['HT-One'])
    return row


@pytest.mark.parametrize('domain,table', [('Observation','observation'), ('Device',''), ('Drug','drug_exposure')])
@pytest.mark.parametrize('rx_kind', ['missing','nonstandard','classification','retired'])
def test_migration_prefers_valid_standard_source_and_preserves_evidence(historical_apps, domain, table, rx_kind):
    source = concept(domain=domain)
    target = None if rx_kind == 'missing' else concept('RxNorm','rx','Drug',
        standard_concept='C' if rx_kind == 'classification' else None,
        invalid_reason='D' if rx_kind == 'retired' else None)
    row = proposal(source,target)
    candidate_before = Candidate.objects.values().get(mapping=row)
    migration = import_module('omop_core.migrations.0260_prefer_standard_snomed_identities')
    with connection.schema_editor(atomic=False) as editor:
        migration.prefer_standard_snomed_identities(historical_apps, editor)
    row.refresh_from_db()
    assert (row.status,row.target_concept_id,row.source_concept_id)==('approved',source.pk,source.pk)
    assert (row.domain_id,row.omop_table,row.destination_vocabulary_id)==(domain,table,'SNOMED')
    assert row.origin_system==IDENTITY_ORIGIN and row.source=='HT-One'
    assert row.occurrence_count==19 and row.reviewer_id is None and row.reviewed_at is None
    assert 'Imported crossmap evidence' in row.notes and 'RxNorm crossmap' in row.notes
    assert Candidate.objects.values().get(mapping=row)==candidate_before
    assert reconcile(historical_apps,connection)==[]


@pytest.mark.parametrize('source_changes', [
    {'standard_concept':None}, {'standard_concept':'C'}, {'invalid_reason':'D'},
    {'valid_start_date':'2099-01-01'}, {'valid_end_date':'2000-01-01'},
])
def test_nonstandard_or_inactive_source_is_not_self_mapped(historical_apps,source_changes):
    row=proposal(concept(**source_changes))
    before=Mapping.objects.values().get(pk=row.pk)
    assert reconcile(historical_apps,connection)==[]
    assert Mapping.objects.values().get(pk=row.pk)==before


@pytest.mark.parametrize('changes', [
    {'status':'approved'}, {'status':'rejected'}, {'origin':'curator'}, {'origin_system':'curator'},
    {'suggestion_model_version':'v1'}, {'suggestion_outcome':'rejected'},
])
def test_curator_and_suggestion_decisions_are_protected(historical_apps,changes):
    row=proposal(concept(),**changes)
    before=Mapping.objects.values().get(pk=row.pk)
    assert reconcile(historical_apps,connection)==[]
    assert Mapping.objects.values().get(pk=row.pk)==before


def test_human_attribution_and_locked_rows_are_protected(historical_apps):
    from patient_portal.models import Identity
    user=Identity.objects.create_user(email='standard-reviewer@example.test')
    for i,field in enumerate(['created_by','updated_by','reviewer','locked_by']):
        proposal(concept(code=f'source{i}'),**{field:user})
    assert reconcile(historical_apps,connection)==[]


def test_valid_selected_rxnorm_and_conflicting_candidates_are_preserved(historical_apps):
    proposal(concept(),concept('RxNorm','rx','Drug'))
    row=proposal(concept(code='other'))
    Candidate.objects.create(mapping=row,target_vocabulary_id='LOINC',target_concept_code='unrelated')
    assert reconcile(historical_apps,connection)==[]


def test_preview_has_no_writes_or_locks_and_matches_apply(historical_apps):
    proposal(concept())
    with CaptureQueriesContext(connection) as queries:
        preview=reconcile(historical_apps,connection,dry_run=True)
    assert summarize(preview)=={'self_mapped':1}
    assert not any(q['sql'].lstrip().upper().startswith(('INSERT','UPDATE','DELETE')) or 'FOR UPDATE' in q['sql'] for q in queries)
    assert reconcile(historical_apps,connection)==preview


def test_runtime_promotes_imported_crossmap_but_never_returns_observation_as_drug():
    source=concept()
    row=proposal(source)
    found,mapping=resolve_source_code(source_vocabulary_id='SNOMED',source_code=source.concept_code,omop_table='drug_exposure')
    assert found is None and mapping.status=='approved' and mapping.target_concept_id==source.pk
    assert mapping.domain_id=='Observation' and mapping.omop_table=='observation'
    found,mapping=resolve_source_code(source_vocabulary_id='SNOMED',source_code=source.concept_code,omop_table='observation')
    assert found==source and mapping.pk==row.pk


def test_fresh_standard_snomed_uses_its_actual_domain():
    source=concept(domain='Device')
    found,row=resolve_source_code(source_vocabulary_id='SNOMED',source_code=source.concept_code,omop_table='drug_exposure')
    assert found is None
    assert row.target_concept_id==source.pk and row.status=='approved'
    assert row.domain_id=='Device' and row.omop_table==''


def test_fresh_nonstandard_snomed_stays_unresolved():
    source=concept(standard_concept=None)
    found,row=resolve_source_code(source_vocabulary_id='SNOMED',source_code=source.concept_code,omop_table='drug_exposure')
    assert found is None and row.status=='proposed'
    assert row.target_concept_id is None


def test_api_explains_domain_mismatch():
    from patient_portal.models import Identity
    source=concept();proposal(source)
    client=APIClient();client.force_authenticate(Identity.objects.create_user(email='standard-admin@example.test',is_staff=True))
    response=client.post('/api/v1/code-mappings/lookup/',{'codes':[dict(source_vocabulary_id='SNOMED',source_code=source.concept_code,omop_table='drug_exposure')]},format='json')
    assert response.status_code==200,response.data
    row=response.data['mappings']['SNOMED|source']
    assert row['resolved'] is False and row['domain_id']=='Observation'
    assert row['unresolved_reason']=='destination_domain_or_validity_mismatch'


@pytest.mark.parametrize('command',['import_healthtree_crossmaps','load_mappings','import_fhir_crossmaps','import_etl_cross_maps'])
@pytest.mark.parametrize('rx_standard',[None,'S','missing'])
def test_importers_prefer_standard_snomed_to_rxnorm_and_preserve_existing_approval(tmp_path,command,rx_standard):
    source=concept()
    if rx_standard!='missing': concept('RxNorm','rx','Drug',standard_concept=rx_standard)
    path=tmp_path/'crossmap.json'
    if command in ('import_fhir_crossmaps','import_etl_cross_maps'):
        path.write_text(json.dumps({'source':'rx'}))
        options={'type':'snomed-to-rxnorm','file':str(path),'all':True} if command=='import_fhir_crossmaps' else {'snomed_rxnorm_file':str(path),'skip_cpt':True}
    else:
        path.write_text(json.dumps({'mappings':[dict(source_vocabulary_id='SNOMED',source_code='source',target_vocabulary_id='RxNorm',target_concept_code='rx',domain_id='Drug',status='approved',origins=['HT-One'])]}))
        options={'artifact':str(path)}
    call_command(command,stdout=StringIO(),**options)
    row=Mapping.objects.get(source_code='source')
    assert row.status=='approved' and row.target_concept_id==source.pk
    assert row.destination_vocabulary_id=='SNOMED' and row.domain_id=='Observation' and row.omop_table=='observation'
    assert row.origin_system==IDENTITY_ORIGIN and 'RxNorm:rx' in row.notes
    replacement=concept(code='curator-selection')
    row.target_concept=replacement;row.origin_system='curator';row.save()
    call_command(command,stdout=StringIO(),**options)
    row.refresh_from_db()
    assert row.target_concept_id==replacement.pk and row.origin_system=='curator'
