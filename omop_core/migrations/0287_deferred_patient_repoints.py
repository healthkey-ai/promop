from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ('omop_core', '0286_add_resolve_run_detail_fields'),
    ]

    operations = [
        migrations.AddField(
            model_name='sourcecodeconceptmapping',
            name='deferred_repoint_concept_ids',
            field=models.JSONField(blank=True, db_default=[], default=list),
        ),
    ]
