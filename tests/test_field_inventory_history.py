import hashlib

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
