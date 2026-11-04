"""Explicit confirmations of the two derived negative states (#5666).

`no_active_infection_status` and `no_other_active_malignancies` are formulas
over recorded facts. A formula cannot tell "never assessed" from "confirmed
negative", and a generic PATCH is replaced by the next derivation. The answer
therefore gets its own attestation row, endpoint and snapshot.

The seeded formulas are deactivated rather than rewritten: the value belongs to
whoever answers the question, and the source-fact derivation in
`patient_record_service._compute_derived_fields` keeps running only while no
attestation exists.
"""
from django.db import migrations, models
import django.db.models.deletion


FIELDS = ('no_active_infection_status', 'no_other_active_malignancies')


def deactivate_formulas(apps, schema_editor):
    FieldFormula = apps.get_model('omop_core', 'FieldFormula')
    alias = schema_editor.connection.alias if schema_editor is not None else 'default'
    FieldFormula.objects.using(alias).filter(field_name__in=FIELDS).update(is_active=False)


class Migration(migrations.Migration):

    dependencies = [
        ('omop_core', '0325_gene_concept_ids'),
    ]

    operations = [
        migrations.CreateModel(
            name='ExplicitNegativeAnswer',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('field', models.CharField(max_length=64)),
                ('answer', models.BooleanField(default=True)),
                ('confirmed_at', models.DateTimeField()),
                ('source', models.CharField(default='patient', max_length=20)),
                ('person', models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='explicit_negative_answers',
                    to='omop_core.person',
                )),
            ],
            options={
                'db_table': 'explicit_negative_answer',
                'constraints': [
                    models.UniqueConstraint(
                        fields=('person', 'field'),
                        name='uq_explicit_negative_answer_person_field',
                    ),
                ],
            },
        ),
        migrations.RunPython(deactivate_formulas, migrations.RunPython.noop),
        migrations.AddConstraint(
            model_name='fieldformula',
            constraint=models.CheckConstraint(
                condition=(
                    ~models.Q(field_name__in=FIELDS)
                    | models.Q(is_active=False)
                ),
                name='field_formula_explicit_answer_inactive',
            ),
        ),
    ]
