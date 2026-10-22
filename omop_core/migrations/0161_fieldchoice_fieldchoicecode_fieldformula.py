"""Add FieldChoice, FieldChoiceCode, and FieldFormula models."""

from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ('omop_core', '0160_seed_behavior_field_mappings'),
    ]

    operations = [
        migrations.CreateModel(
            name='FieldChoice',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('field_name', models.CharField(db_index=True, max_length=100)),
                ('display', models.CharField(max_length=200)),
                ('sort_order', models.IntegerField(default=0)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('created_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, to=settings.AUTH_USER_MODEL)),
            ],
            options={
                'db_table': 'field_choice',
                'ordering': ['field_name', 'sort_order', 'display'],
                'unique_together': {('field_name', 'display')},
            },
        ),
        migrations.CreateModel(
            name='FieldChoiceCode',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('code', models.CharField(max_length=50)),
                ('vocabulary_id', models.CharField(max_length=20)),
                ('display', models.CharField(blank=True, default='', max_length=200)),
                ('is_primary', models.BooleanField(default=False)),
                ('choice', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='codes', to='omop_core.fieldchoice')),
            ],
            options={
                'db_table': 'field_choice_code',
                'unique_together': {('choice', 'vocabulary_id')},
            },
        ),
        migrations.CreateModel(
            name='FieldFormula',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('field_name', models.CharField(db_index=True, max_length=100, unique=True)),
                ('formula', models.TextField(help_text='e.g. "@not(active_infection_status)" or "weight / (height/100)^2"')),
                ('is_active', models.BooleanField(default=False)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('created_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, to=settings.AUTH_USER_MODEL)),
            ],
            options={
                'db_table': 'field_formula',
            },
        ),
    ]
