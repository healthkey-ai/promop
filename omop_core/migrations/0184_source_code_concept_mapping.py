from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


LOCAL_CONCEPT_ID_MIN = 2_000_000_000


def seed_existing_wearable_code_mappings(apps, schema_editor):
    Concept = apps.get_model('omop_core', 'Concept')
    SourceCodeConceptMapping = apps.get_model('omop_core', 'SourceCodeConceptMapping')

    wearable_concepts = Concept.objects.filter(
        concept_id__gte=LOCAL_CONCEPT_ID_MIN,
        standard_concept__isnull=True,
        vocabulary__vocabulary_id='HK-Wearable',
    ).filter(
        models.Q(source='HealthKey') | models.Q(vocabulary__vocabulary_id__startswith='HK-')
    )

    for concept in wearable_concepts.iterator():
        if not concept.concept_code:
            continue
        SourceCodeConceptMapping.objects.update_or_create(
            source_vocabulary_id=concept.vocabulary_id,
            source_code=concept.concept_code,
            defaults={
                'source_code_description': concept.concept_name,
                'target_concept_id': concept.concept_id,
                'source': concept.vocabulary_id or 'HealthKey',
                'status': 'active',
            },
        )


def unseed_existing_wearable_code_mappings(apps, schema_editor):
    SourceCodeConceptMapping = apps.get_model('omop_core', 'SourceCodeConceptMapping')
    SourceCodeConceptMapping.objects.filter(source_vocabulary_id='HK-Wearable').delete()


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ('omop_core', '0183_update_m_protein_type_values'),
    ]

    operations = [
        migrations.CreateModel(
            name='SourceCodeConceptMapping',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('source_vocabulary_id', models.CharField(db_index=True, max_length=50)),
                ('source_code', models.CharField(db_index=True, max_length=100)),
                ('source_code_description', models.CharField(blank=True, default='', max_length=255)),
                ('source', models.CharField(blank=True, db_index=True, default='HealthKey', max_length=50)),
                ('status', models.CharField(choices=[('active', 'Active'), ('retired', 'Retired'), ('rejected', 'Rejected')], default='active', max_length=20)),
                ('notes', models.TextField(blank=True, default='')),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('created_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to=settings.AUTH_USER_MODEL)),
                ('target_concept', models.ForeignKey(db_constraint=False, on_delete=django.db.models.deletion.DO_NOTHING, related_name='source_code_mappings', to='omop_core.concept')),
                ('updated_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to=settings.AUTH_USER_MODEL)),
            ],
            options={
                'db_table': 'source_code_concept_mapping',
            },
        ),
        migrations.AddIndex(
            model_name='sourcecodeconceptmapping',
            index=models.Index(fields=['source_vocabulary_id', 'source_code'], name='ix_sccm_source_code'),
        ),
        migrations.AddIndex(
            model_name='sourcecodeconceptmapping',
            index=models.Index(fields=['target_concept', 'status'], name='ix_sccm_target_status'),
        ),
        migrations.AddConstraint(
            model_name='sourcecodeconceptmapping',
            constraint=models.UniqueConstraint(fields=('source_vocabulary_id', 'source_code'), name='uq_sccm_source_vocabulary_code'),
        ),
        migrations.RunPython(seed_existing_wearable_code_mappings, unseed_existing_wearable_code_mappings),
    ]
