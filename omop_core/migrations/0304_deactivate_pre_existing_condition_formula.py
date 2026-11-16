"""Make `no_pre_existing_conditions` an authored answer, not a formula output (#1988).

The seeded formula (``0167``) derived the flag from
``@count(preexisting_conditions) == 0``. The evaluator cannot tell an empty list from
"never assessed", and it also ran as the final authority over a stored answer, so an
unassessed record read as an explicit "none" and CB cleared a safety-relevant exclusion.

The formula is deactivated rather than rewritten: the expression language has no way to
express "assessed", and the value belongs to whoever answers the question. The same row
in ``omop_core/data/field_curation_v1.json`` is shipped inactive, or a curation import
would re-enable it.
"""

from django.db import migrations

FIELD = 'no_pre_existing_conditions'


def deactivate(apps, schema_editor):
    FieldFormula = apps.get_model('omop_core', 'FieldFormula')
    alias = schema_editor.connection.alias if schema_editor is not None else 'default'
    FieldFormula.objects.using(alias).filter(field_name=FIELD).update(is_active=False)


class Migration(migrations.Migration):

    dependencies = [('omop_core', '0303_therapy_history_attestation')]

    operations = [
        migrations.RunPython(deactivate, migrations.RunPython.noop),
    ]
