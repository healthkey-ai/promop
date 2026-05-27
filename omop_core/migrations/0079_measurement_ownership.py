from django.db import migrations, models


def backfill_ownership(apps, schema_editor):
    """Create ownership records for all existing measurements."""
    MeasurementOwnership = apps.get_model('omop_core', 'MeasurementOwnership')
    Measurement = apps.get_model('omop_core', 'Measurement')

    rows = Measurement.objects.filter(
        visit_occurrence_id__isnull=False,
    ).values_list('measurement_id', 'visit_occurrence_id')

    batch = []
    for m_id, v_id in rows.iterator(chunk_size=2000):
        batch.append(MeasurementOwnership(
            measurement_id=m_id,
            visit_occurrence_id=v_id,
        ))
        if len(batch) >= 2000:
            MeasurementOwnership.objects.bulk_create(batch, ignore_conflicts=True)
            batch = []
    if batch:
        MeasurementOwnership.objects.bulk_create(batch, ignore_conflicts=True)


class Migration(migrations.Migration):

    dependencies = [
        ('omop_core', '0078_backfill_measurement_source_value'),
    ]

    operations = [
        migrations.CreateModel(
            name='MeasurementOwnership',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('measurement_id', models.BigIntegerField()),
                ('visit_occurrence_id', models.BigIntegerField()),
            ],
            options={
                'db_table': 'measurement_ownership',
                'unique_together': {('measurement_id', 'visit_occurrence_id')},
            },
        ),
        migrations.AddIndex(
            model_name='measurementownership',
            index=models.Index(fields=['visit_occurrence_id'], name='ix_measown_visit'),
        ),
        migrations.RunPython(backfill_ownership, migrations.RunPython.noop),
    ]
