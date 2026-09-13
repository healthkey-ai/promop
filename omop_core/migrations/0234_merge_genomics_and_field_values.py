"""Join independent genomic recipe and field-answer mapping migrations."""
from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ('omop_core', '0231_genomics_variant_name_recipe'),
        ('omop_core', '0233_withdraw_invalid_bc_field_recipes'),
    ]
    operations = []
