"""Create DiseaseCategoryChoice table and seed from the DiseaseCategory enum.

Also widens DiseaseCategoryMapping.category to max_length=30 and drops the
choices= constraint so custom categories are accepted.
"""

from django.db import migrations, models


_SEED = [
    ('BC', 'Breast Cancer'),
    ('MM', 'Multiple Myeloma'),
    ('FL', 'Follicular Lymphoma'),
    ('CLL', 'Chronic Lymphocytic Leukemia'),
    ('MCL', 'Mantle Cell Lymphoma'),
    ('Other', 'Other'),
]


def seed_forward(apps, schema_editor):
    Choice = apps.get_model('omop_core', 'DiseaseCategoryChoice')
    for code, label in _SEED:
        Choice.objects.get_or_create(code=code, defaults={'label': label})


def seed_reverse(apps, schema_editor):
    Choice = apps.get_model('omop_core', 'DiseaseCategoryChoice')
    Choice.objects.filter(code__in=[c for c, _ in _SEED]).delete()


class Migration(migrations.Migration):
    dependencies = [
        ('omop_core', '0320_merge_20261029_0812'),
    ]

    operations = [
        migrations.CreateModel(
            name='DiseaseCategoryChoice',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('code', models.CharField(max_length=30, unique=True)),
                ('label', models.CharField(max_length=120)),
            ],
            options={
                'db_table': 'disease_category_choice',
                'ordering': ['label'],
            },
        ),
        migrations.AlterField(
            model_name='diseasecategorymapping',
            name='category',
            field=models.CharField(max_length=30),
        ),
        migrations.RunPython(seed_forward, seed_reverse),
    ]
