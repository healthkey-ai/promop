from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('omop_core', '0204_merge_20260903_1830')]

    operations = [
        migrations.AddField(
            model_name='sourcecodeconceptmapping',
            name='suggested_target_concept',
            field=models.ForeignKey(blank=True, db_constraint=False, null=True,
                on_delete=models.deletion.DO_NOTHING,
                related_name='source_code_mapping_suggestions', to='omop_core.concept'),
        ),
        migrations.AddField(
            model_name='sourcecodeconceptmapping',
            name='suggestion_outcome',
            field=models.CharField(blank=True, choices=[('accepted', 'Accepted'), ('overridden', 'Overridden'), ('rejected', 'Rejected')], db_index=True, default='', max_length=12),
        ),
    ]
