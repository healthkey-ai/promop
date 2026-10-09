from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('omop_core', '0273_async_athena_vocabulary_sync'),
    ]

    operations = [
        migrations.AddField(
            model_name='sourcecodeconceptmapping',
            name='source_unit_evidence',
            field=models.JSONField(
                blank=True,
                db_default=[],
                default=list,
                help_text=(
                    'Raw unit evidence imported with a source-code inventory. '
                    'Each entry preserves the sender display/code and occurrence '
                    'count; approval checks normalize it at read time. Live '
                    'Measurement evidence remains authoritative when it is more '
                    'recent or larger.'
                ),
            ),
        ),
    ]
