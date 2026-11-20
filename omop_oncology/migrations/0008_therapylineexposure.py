"""Record canonical writer ownership; do not adopt historical clinical facts."""
import django.db.models.deletion
from django.db import migrations, models


def install_link_lock(apps, schema_editor):
    if schema_editor.connection.vendor != 'postgresql':
        return
    schema_editor.execute("""
        CREATE FUNCTION lock_therapy_exposure_link() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF NEW.episode_event_field_concept_id = 1147094 THEN
                PERFORM drug_exposure_id FROM drug_exposure
                WHERE drug_exposure_id = NEW.event_id FOR UPDATE;
            END IF;
            RETURN NEW;
        END;
        $$;
        CREATE TRIGGER lock_therapy_exposure_link
        BEFORE INSERT OR UPDATE OF event_id, episode_event_field_concept_id, episode_id
        ON episode_event FOR EACH ROW EXECUTE FUNCTION lock_therapy_exposure_link();
    """)


def uninstall_link_lock(apps, schema_editor):
    if schema_editor.connection.vendor != 'postgresql':
        return
    schema_editor.execute("""
        DROP TRIGGER IF EXISTS lock_therapy_exposure_link ON episode_event;
        DROP FUNCTION IF EXISTS lock_therapy_exposure_link();
    """)


class Migration(migrations.Migration):
    dependencies = [
        ('omop_core', '0298_concept_mapping_priority'),
        ('omop_oncology', '0007_lines_per_cancer'),
    ]

    operations = [
        migrations.CreateModel(
            name='TherapyLineExposure',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('source_value', models.CharField(max_length=50)),
                ('drug_exposure', models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, to='omop_core.drugexposure')),
                ('episode', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='authored_exposures', to='omop_oncology.episode')),
            ],
            options={
                'db_table': 'therapy_line_exposure',
                'indexes': [models.Index(fields=['episode', 'source_value'], name='therapy_exposure_source_idx')],
            },
        ),
        migrations.RunPython(install_link_lock, uninstall_link_lock),
    ]
