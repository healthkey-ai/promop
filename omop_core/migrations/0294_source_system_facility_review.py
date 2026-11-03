"""Record curator decisions without replacing imported facility evidence."""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('omop_core', '0293_fix_phr_concepts_and_custom_field')]

    operations = [
        migrations.AddField(
            model_name='sourcesystemfacility', name='review_status',
            field=models.CharField(max_length=16, default='unreviewed', choices=[
                ('unreviewed', 'Unreviewed'), ('confirmed', 'Confirmed'),
                ('rejected', 'Rejected'),
            ]),
        ),
        migrations.AddField(
            model_name='sourcesystemfacility', name='reviewed_by',
            field=models.CharField(max_length=255, blank=True, default=''),
        ),
        migrations.AddField(
            model_name='sourcesystemfacility', name='reviewed_at',
            field=models.DateTimeField(null=True, blank=True),
        ),
    ]
