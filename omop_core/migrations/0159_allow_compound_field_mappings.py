from django.db import migrations, models
from django.db.models import Q


class Migration(migrations.Migration):

    dependencies = [
        ('omop_core', '0158_seed_suggested_field_mappings'),
    ]

    operations = [
        migrations.RemoveConstraint(
            model_name='fieldconceptmapping',
            name='uq_field_concept_mapping_vocab_code',
        ),
        migrations.AddConstraint(
            model_name='fieldconceptmapping',
            constraint=models.UniqueConstraint(
                fields=('omop_table', 'source_value'),
                condition=(
                    Q(status='approved')
                    & ~Q(omop_table='')
                    & ~Q(source_value='')
                ),
                name='uq_field_concept_mapping_approved_source',
            ),
        ),
    ]
