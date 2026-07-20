from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('omop_core', '0108_add_location_visit_detail_sequences'),
    ]

    operations = [
        migrations.RemoveField(
            model_name='patientrecord',
            name='best_response',
        ),
    ]
