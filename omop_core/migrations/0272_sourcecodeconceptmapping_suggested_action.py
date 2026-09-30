from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('omop_core', '0271_source_code_mapping_organization')]

    operations = [
        migrations.AddField(
            model_name='sourcecodeconceptmapping',
            name='suggested_action',
            field=models.CharField(
                blank=True,
                choices=[('reject', 'Reject')],
                default='',
                db_default='',
                help_text=(
                    'A machine-proposed curator action that has not happened yet. '
                    'Currently used for narrative/noise labels that should be rejected; '
                    'the row remains proposed until a curator confirms the group action.'
                ),
                max_length=12,
            ),
        ),
    ]
