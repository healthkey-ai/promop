from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ('omop_core', '0213_suggest_embedding_snapshot'),
    ]

    operations = [
        migrations.RenameField(
            model_name='fhirconnection',
            old_name='access_token_encrypted',
            new_name='access_token',
        ),
        migrations.RenameField(
            model_name='fhirconnection',
            old_name='refresh_token_encrypted',
            new_name='refresh_token',
        ),
    ]
