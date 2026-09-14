import hashlib
import ast

from omop_core.services import field_inventory_history as history


def test_history_reads_schema_without_executing_migrations_or_inventing_value_aliases(tmp_path):
    migrations = tmp_path / 'trials/migrations'
    migrations.mkdir(parents=True)
    (migrations / '0001_initial.py').write_text('''
raise RuntimeError('Never execute migration code')
class Migration(migrations.Migration):
    operations = [
        migrations.CreateModel(name='PatientInfo', fields=[('gender', models.CharField(choices=[('F', 'Female'), ('U', 'Unknown')]))]),
        migrations.RenameField(model_name='patientinfo', old_name='measurable_disease', new_name='measurable_disease_imwg'),
        migrations.RemoveField(model_name='patientinfo', name='legacy'),
        migrations.RenameModel(old_name='Therapy', new_name='OtherTherapy'),
        migrations.AlterField(model_name='patientinfo', name='grade', field=models.CharField(choices=[(0, '0'), ('', 'Unknown')])),
        migrations.AlterField(model_name='patientinfo', name='status', field=models.CharField(choices=dynamic_choices())),
        migrations.RemoveField(model_name='trial', name='not_a_patient_field'),
        migrations.RunPython(destructive_function),
    ]
''')
    result = history.collect_cancerbot_history(tmp_path)
    assert len(result['schema_events']) == 6
    assert len(result['data_migrations']) == 1
    assert result['schema_events'][0]['choices'] == [('F', 'Female'), ('U', 'Unknown')]
    assert result['schema_events'][4]['choices'] == [(0, '0'), ('', 'Unknown')]
    assert result['schema_events'][5]['choices'] == {'unresolved_expression': 'dynamic_choices()'}
    assert result['catalog_events'] == []
    assert result['complete'] is False


def test_reviewed_removal_is_catalog_scoped_and_source_drift_invalidates_it(tmp_path, monkeypatch):
    migrations = tmp_path / 'trials/migrations'
    migrations.mkdir(parents=True)
    name = '0406_remove_nsaids_therapy.py'
    path = migrations / name
    path.write_text('class Migration(migrations.Migration):\n    operations = [migrations.RunPython(remove_nsaids)]\n')
    monkeypatch.setattr(history, 'REVIEWED', {name: hashlib.sha256(path.read_bytes()).hexdigest()})
    result = history.collect_cancerbot_history(tmp_path)
    assert result['catalog_events'][0]['models'] == ['Therapy']
    assert result['catalog_events'][0]['replacement_code'] is None
    assert result['data_migrations'][0]['status'] == 'reviewed_catalog_rule'
    path.write_text(path.read_text() + '# changed implementation\n')
    result = history.collect_cancerbot_history(tmp_path)
    assert result['catalog_events'] == []
    assert result['data_migrations'][0]['status'] == 'data_operation_requires_review'
    assert result['missing_or_changed_reviewed_files'] == [name]


def test_conditional_aliases_do_not_claim_membership_in_every_catalog():
    events = history._reviewed_events('0403_remap_junk_therapy_codes.py')
    assert {e['old_code']: e['replacement_code'] for e in events} == {'i': 'ixazomib', 's': 'selinexor', 't': 'tazemetostat'}
    assert all(e['kind'] == 'conditional_replacement' and e['historical_membership'] for e in events)
    wort = history._reviewed_events('0404_dedup_st_john_s_wort.py')[0]
    assert wort['models'] == ['TherapyComponent']
    assert wort['replacement_code'] == "st._john's_wort"


def test_dynamic_operations_and_missing_source_remain_open(tmp_path):
    assert history.collect_cancerbot_history(tmp_path)['status'] == 'source_unavailable'
    migrations = tmp_path / 'trials/migrations'
    migrations.mkdir(parents=True)
    (migrations / '0001_dynamic.py').write_text('class Migration(migrations.Migration):\n    operations = build_operations()\n')
    result = history.collect_cancerbot_history(tmp_path)
    assert result['data_migrations'][0]['status'] == 'dynamic_operations_require_review'
    assert result['complete'] is False


def test_commented_repairs_are_noops_and_schema_extensions_do_not_retire_values(tmp_path):
    migrations = tmp_path / 'trials/migrations'
    migrations.mkdir(parents=True)
    path = migrations / '0001_token_schema.py'
    path.write_text('''
from django.contrib.postgres.operations import CreateExtension
def no_change(apps, editor):
    pass
    # PatientInfo.objects.filter(ethnicity='Old').update(ethnicity='New')
class Migration(migrations.Migration):
    operations = [CreateExtension('postgis'), migrations.RunPython(no_change)]
''')
    result = history.collect_cancerbot_history(tmp_path)
    assert result['schema_events'][0]['status'] == 'schema_operation_not_value_change'
    assert result['data_migrations'][0]['status'] == 'source_noop'
    assert result['catalog_events'] == []
    assert result['files'] == [{'path': path.name, 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}]


def test_loader_dependencies_are_fingerprinted_without_execution_or_retirement_inference(tmp_path):
    migrations = tmp_path / 'trials/migrations'
    services = tmp_path / 'trials/services'
    migrations.mkdir(parents=True)
    services.mkdir()
    loader = services / 'loader.py'
    loader.write_text("raise RuntimeError('Do not execute loader')\nfrom trials.services.mapper import OPTIONS\n")
    mapper = services / 'mapper.py'
    mapper.write_text("from trials.services.loader import Loader\nOPTIONS = {'old': 'Old'}\n")
    (migrations / '0001_loader.py').write_text('''
from trials.services.loader import Loader
def seed(apps, editor):
    Loader().run()
class Migration(migrations.Migration):
    operations = [migrations.RunPython(seed)]
''')
    result = history.collect_cancerbot_history(tmp_path)
    operation = result['data_migrations'][0]
    assert operation['status'] == 'delegated_source_requires_review'
    assert operation['loader_sources'] == [
        {'path': 'trials/services/loader.py', 'sha256': hashlib.sha256(loader.read_bytes()).hexdigest()},
        {'path': 'trials/services/mapper.py', 'sha256': hashlib.sha256(mapper.read_bytes()).hexdigest()},
    ]
    assert result['catalog_events'] == []


def test_retired_grade_and_trial_type_never_alias_to_unknown_or_a_therapy():
    grade = history._reviewed_events('0337_patch_remove_tumor_grade_4.py')[0]
    assert grade['field'] == 'tumor_grade'
    assert type(grade['old_code']) is int and grade['old_code'] == 40
    assert grade['replacement_code'] is None
    trial = history._reviewed_events('0371_delete_targeted_therapy_all.py')[0]
    assert trial['models'] == ['TrialType']
    aliases = history._reviewed_events('0350_merge_lowercase_disease_codes.py')
    assert {r['old_code']: r['replacement_code'] for r in aliases} == {'mm': 'MM', 'fl': 'FL', 'bc': 'BC'}
    assert all(r['models'] == ['Disease'] for r in aliases)


def test_historical_options_preserve_typed_values_and_never_infer_retirement():
    history_data = {'seed_options': [], 'catalog_events': [], 'schema_events': [
        {'file': '0001.py', 'operation_index': 0, 'model_name': 'PatientInfo', 'name': 'grade',
         'choices': [(False, 'False'), (0, 'Zero'), ('0', 'Text zero'), ('', 'Unknown'), (None, 'Missing')]},
        {'file': '0002.py', 'operation_index': 0, 'model_name': 'PatientInfo', 'name': 'removed',
         'choices': [('old', 'Old label')]},
    ]}
    rows = history.historical_option_rows(history_data, {'grade'})
    assert len({r['id'] for r in rows}) == 6
    assert {r['canonical_value']['type'] for r in rows[:5]} == {'boolean', 'integer', 'string', 'null'}
    assert all(r['retired'] is None and r['disposition'] == 'needs_review' for r in rows)
    assert rows[-1]['destination_path'] is None
    assert rows[-1]['destination_candidates'] == ['removed']


def test_seed_literals_preserve_gene_context_and_source_code_rules():
    tree = ast.parse('''
raise RuntimeError('Never execute')
VARIANTS = ['c.3113G>A (p.W1038*)']
def seed():
    codes = {'same', 'same', 'other'}
''')
    options = history._seed_options(tree, [
        ('seed', 'codes', 'ESR1Mutation', 'genetic_mutations.variant_name', {'gene': 'ESR1'}),
        (None, 'VARIANTS', 'MutationCode', 'genetic_mutations.variant_name',
         {'gene': 'PALB1', 'source_code_transform': 'palb1_variant'}),
    ])
    assert [r['code'] for r in options] == ['other', 'same', 'c.3113g_a_(p.w1038*)']
    assert options[-1]['scope']['gene'] == 'PALB1'
    assert options[-1]['label'] == 'c.3113G>A (p.W1038*)'


def test_seed_source_drift_removes_historical_declarations(tmp_path, monkeypatch):
    base = tmp_path / 'trials/migrations'
    base.mkdir(parents=True)
    file = base / '0001_seed.py'
    file.write_text("DATA = {'a': 'A'}\nclass Migration:\n    operations = []\n")
    monkeypatch.setattr(history, 'SEED_DECLARATIONS', {file.name: {
        'sha256': hashlib.sha256(file.read_bytes()).hexdigest(),
        'declarations': [(None, 'DATA', 'Example', 'example', {})],
    }})
    assert len(history.collect_cancerbot_history(tmp_path)['seed_options']) == 1
    file.write_text(file.read_text() + '# drift\n')
    result = history.collect_cancerbot_history(tmp_path)
    assert result['seed_options'] == []
    assert result['missing_or_changed_seed_files'] == [file.name]
