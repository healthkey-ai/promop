from django.db import migrations, models


def forwards(apps, schema_editor):
    SourceCodeConceptMapping = apps.get_model('omop_core', 'SourceCodeConceptMapping')
    SourceCodeConceptMapping.objects.filter(status='active').update(status='approved')
    SourceCodeConceptMapping.objects.filter(status='retired').update(status='rejected')


def backwards(apps, schema_editor):
    SourceCodeConceptMapping = apps.get_model('omop_core', 'SourceCodeConceptMapping')
    SourceCodeConceptMapping.objects.filter(status='approved').update(status='active')


class Migration(migrations.Migration):

    dependencies = [
        ('omop_core', '0188_language_skill_concept_and_flat_columns'),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
        migrations.AlterField(
            model_name='sourcecodeconceptmapping',
            name='status',
            field=models.CharField(
                choices=[
                    ('proposed', 'Proposed'),
                    ('approved', 'Approved'),
                    ('rejected', 'Rejected'),
                ],
                default='proposed',
                max_length=20,
            ),
        ),
    ]
