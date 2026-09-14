"""Join the independently merged 0236 repairs without rewriting history."""
from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ('omop_core', '0236_merge_all_deployment_repairs'),
        ('omop_core', '0236_merge_concurrent_0235_branches'),
    ]
    operations = []
