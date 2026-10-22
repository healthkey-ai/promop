from django.db import migrations


def seed_names(apps, schema_editor):
    Application = apps.get_model('patient_portal', 'ServiceApplication')
    for service_id, name, owner in (
        ('etl', 'ETL', 'Nikita Shpilevoy'),
        ('ht-phr', 'HT-PHR', 'Vlad Tarasov'),
        ('hk-labs', 'HK-Labs', 'Vlad Tarasov'),
        ('exact', 'EXACT', 'Leonid'),
    ):
        Application.objects.using(schema_editor.connection.alias).get_or_create(
            service_id=service_id,
            defaults={'name': name, 'owner_contact': owner},
        )


class Migration(migrations.Migration):
    dependencies = [('patient_portal', '0016_service_applications')]
    operations = [migrations.RunPython(seed_names, migrations.RunPython.noop)]
