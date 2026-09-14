import copy
import hashlib
import json
import subprocess
from pathlib import Path

import pytest

from omop_core.services.field_inventory_frontend import reconcile_frontend_providers

ROOT=Path(__file__).resolve().parents[1]


def parse(tmp_path, source, filename='tabs/GeneralTab.tsx'):
    deps=ROOT/'frontend/node_modules'
    if not (deps/'typescript').exists(): pytest.skip('TypeScript parser dependency required')
    target=tmp_path/'frontend/src/components/PatientInfo'/filename
    target.parent.mkdir(parents=True)
    (tmp_path/'frontend/node_modules').symlink_to(deps)
    target.write_text(source)
    return json.loads(subprocess.check_output(['node',str(ROOT/'scripts/inventory-frontend-options.cjs'),str(tmp_path)],text=True))


def test_nested_constants_keep_gene_identity_and_reject_computed_keys(tmp_path):
    result=parse(tmp_path, '''
throw new Error('source must never execute');
const MUTATION_OPTIONS = {G1: ['unknown', 'v1'], G2: ['unknown'], G3: []};
const DYNAMIC_OPTIONS = {[readGeneFromPatient()]: ['v2']};
''')
    groups=result['constants']
    assert [(g['parent_keys'],g['values']) for g in groups]==[(['G1'],['unknown','v1']),(['G2'],['unknown']),(['G3'],[])]
    assert len({g['name'] for g in groups})==3
    assert result['unresolved'][0]['name']=='DYNAMIC_OPTIONS'


def test_structured_criteria_keep_storage_keys_and_labels_separate(tmp_path):
    result=parse(tmp_path,"const FACTORS = [['large_mass', 'A mass > 7 cm'], ['effusion', 'Pleural effusion']] as const;",'GelfAssessment.tsx')
    group=result['constants'][0]
    assert group['destination_field']=='gelf_criteria_options'
    assert group['disease']=='FL'
    assert group['values']==[{'value':'large_mass','label':'A mass > 7 cm'},{'value':'effusion','label':'Pleural effusion'}]


@pytest.mark.parametrize('helper', [True,False])
def test_general_field_calls_require_the_known_forwarding_helper(tmp_path, helper):
    declaration=('const field=(label,name,type,extra={}) => { return <ClinicalField name={name} type={type} options={extra.options} />; };'
                 if helper else 'const field = unrelatedFunction;')
    result=parse(tmp_path, declaration+'''
function GeneralTab(){return field('Country', 'country', 'select', {options: ['US', 'CA']});}
''')
    calls=[c for c in result['controls'] if c.get('helper')]
    assert len(calls)==int(helper)
    if helper:
        assert calls[0]['field']=='country'
        assert calls[0]['options']==['US','CA']


def fixture(tmp_path, kind='lookup_titles'):
    source=tmp_path/'frontend/source.tsx';source.parent.mkdir();source.write_text('reviewed source')
    rule={'file':'frontend/source.tsx','expression':'lookupOptions','field':'histologic_type',
          'kind':kind,'table':'vocabulary_histologic_type','fallback_constant':'HISTOLOGIC_TYPE_OPTIONS',
          'reason':'Explicit source routing; descriptor has precedence.',
          'sources':{'frontend/source.tsx':hashlib.sha256(source.read_bytes()).hexdigest()}}
    data=tmp_path/'omop_core/data';data.mkdir(parents=True)
    (data/'field_inventory_frontend_providers.json').write_text(json.dumps({'rules':[rule]}))
    frontend={'controls':[{'file':rule['file'],'expression':rule['expression'],'field':rule['field']}], 'unresolved':[]}
    tables={'vocabulary_histologic_type':[{'id':1,'code':'ductal','title':'Invasive ductal carcinoma'}]}
    return frontend,tables,source


def test_lookup_provider_preserves_ui_title_and_underlying_source_code(tmp_path):
    frontend,tables,_=fixture(tmp_path)
    rows=reconcile_frontend_providers(tmp_path,frontend,tables)
    assert rows[0]['canonical_value']=={'type':'string','value':'Invasive ductal carcinoma'}
    assert rows[0]['destination_path']=='histologic_type'
    assert rows[0]['search_evidence'][0]['source_record']['reference_code']=='ductal'
    assert rows[0]['disposition']=='needs_review'
    binding=frontend['controls'][0]['provider_resolution']
    assert binding['descriptor_precedence'] is True
    assert binding['fallback_constant']=='HISTOLOGIC_TYPE_OPTIONS'


@pytest.mark.parametrize('failure',['source_drift','stale_export','missing_source','missing_table'])
def test_reviewed_provider_changes_remain_unresolved(tmp_path,failure):
    frontend,tables,source=fixture(tmp_path)
    reconcile_frontend_providers(tmp_path,frontend,tables)
    assert 'provider_resolution' in frontend['controls'][0]
    if failure=='source_drift':source.write_text('different provider')
    elif failure=='stale_export':frontend['files']={'frontend/source.tsx':'stale-hash'}
    elif failure=='missing_source':source.unlink()
    else:tables={}
    assert reconcile_frontend_providers(tmp_path,frontend,tables)==[]
    assert 'provider_resolution' not in frontend['controls'][0]
    assert frontend['provider_reconciliation'][0]['status']!='source_provider_accounted_for'


def test_checked_in_provider_rules_match_current_sources_and_preserve_catalogs():
    manifest=json.loads((ROOT/'docs/field-mapping-inventory/manifest.json').read_text())
    frontend=copy.deepcopy(manifest['frontend'])
    tables=copy.deepcopy(manifest['reference_tables'])
    rows=reconcile_frontend_providers(ROOT,frontend,tables)
    assert len(rows)==41
    assert all(e['status']=='source_provider_accounted_for' for e in frontend['provider_reconciliation'])
    assert tables==manifest['reference_tables']
    language=[r for r in rows if r['destination_path']=='PersonLanguageSkill.skill_level']
    assert {r['canonical_value']['value'] for r in language}=={'speak','read','write','understand'}
    assert all(r['disposition']=='requires_structured_representation' for r in language)


def test_descriptor_capture_keeps_source_code_separate_from_clinical_approval(monkeypatch):
    from omop_core.management.commands.export_field_mapping_inventory import collect_descriptor_options
    from omop_core.services import write_descriptor
    monkeypatch.setattr(write_descriptor, 'build_writable_field_descriptor', lambda: {
        'gender': {'projection_target':'person', 'options':[{'value':'Female','code':'F'}]},
        'answer': {'projection':{'concept_id':123},'options':[{'value':0},{'value':'0'}]},
    })
    rows, source=collect_descriptor_options({})
    assert source['status']=='base_descriptors_captured'
    assert len({r['id'] for r in rows})==3
    assert rows[0]['search_evidence'][0]['option_code']=='F'
    assert rows[0]['mapping_role']=='reference'
    assert all(r['candidate_ids']==[] and r['disposition']=='needs_review' for r in rows)


def test_descriptor_lookup_cannot_read_patient_models(monkeypatch):
    from omop_core.management.commands.export_field_mapping_inventory import collect_descriptor_options
    from omop_core.services import write_descriptor
    def forbidden():
        raise AssertionError('Must refuse before invoking runtime lookup')
    monkeypatch.setattr(write_descriptor, 'build_writable_field_descriptor', forbidden)
    rows, source=collect_descriptor_options({'field_concept_mapping':[{'status':'approved','value_vocabulary':'PatientRecord'}]})
    assert rows==[]
    assert source['status']=='unresolved_lookup_model'
