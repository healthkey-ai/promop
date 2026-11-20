from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('omop_core', '0303_axis_variant_refresh'),
    ]

    operations = [
        migrations.AddField(
            model_name='axisvariantrefresh',
            name='progress_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
