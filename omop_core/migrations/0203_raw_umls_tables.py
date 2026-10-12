from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('omop_core', '0202_vocabularyrelease_umls_release')]

    operations = [
        migrations.CreateModel(
            name='UmlsRelease',
            fields=[
                ('release_version', models.CharField(max_length=20, primary_key=True, serialize=False)),
                ('release_url', models.URLField(max_length=500)),
                ('archive_sha256', models.CharField(blank=True, max_length=64)),
                ('loaded_at', models.DateTimeField(auto_now=True)),
            ], options={'db_table': 'umls_release'},
        ),
        migrations.CreateModel(
            name='UmlsConcept',
            fields=[
                ('cui', models.CharField(max_length=8, primary_key=True, serialize=False)),
                ('preferred_name', models.TextField(blank=True)),
                ('release', models.ForeignKey(on_delete=models.deletion.PROTECT, to='omop_core.umlsrelease')),
            ], options={'db_table': 'umls_concept'},
        ),
        migrations.CreateModel(
            name='UmlsSourceCode',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('root_source', models.CharField(max_length=50)),
                ('code', models.CharField(max_length=255)),
                ('term_type', models.CharField(max_length=20)),
                ('name', models.TextField()),
                ('is_preferred', models.BooleanField(default=False)),
                ('concept', models.ForeignKey(on_delete=models.deletion.CASCADE, related_name='source_codes', to='omop_core.umlsconcept')),
            ], options={'db_table': 'umls_source_code'},
        ),
        migrations.AddConstraint(model_name='umlssourcecode', constraint=models.UniqueConstraint(fields=('root_source', 'code', 'concept', 'term_type', 'name'), name='uq_umls_source_code_term')),
        migrations.AddIndex(model_name='umlssourcecode', index=models.Index(fields=['root_source', 'code'], name='umls_source_root_so_9d0e39_idx')),
    ]
