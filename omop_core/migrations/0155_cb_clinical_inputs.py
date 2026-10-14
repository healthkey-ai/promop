from decimal import Decimal

from django.core.validators import MinValueValidator
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('omop_core', '0154_merge_field_synonym_and_refresh_indexes')]
    operations = [
        migrations.AddField(
            model_name='patientrecord', name='prothrombin_time_uln',
            field=models.DecimalField(max_digits=10, decimal_places=5, blank=True, null=True,
                                      validators=[MinValueValidator(Decimal('0.00001'))],
                                      help_text='Prothrombin time ratio to laboratory ULN, not INR or seconds'),
        ),
        migrations.AddField(
            model_name='patientrecord', name='activated_partial_thromboplastin_time_uln',
            field=models.DecimalField(max_digits=10, decimal_places=5, blank=True, null=True,
                                      validators=[MinValueValidator(Decimal('0.00001'))],
                                      help_text='aPTT ratio to laboratory ULN, not seconds'),
        ),
        migrations.AddField(
            model_name='patientrecord', name='able_to_swallow_tablets_capsules',
            field=models.BooleanField(blank=True, null=True),
        ),
    ]
