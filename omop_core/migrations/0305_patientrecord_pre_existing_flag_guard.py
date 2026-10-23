"""Keep "no pre-existing conditions" and its categories from coexisting (#1988).

The flag is an answer to its own question, not a derivation of an empty list. The
serializer refuses a write that states both and the service invalidates a stale flag
when categories exist; this migration resolves rows that already contradict, keeps the
old values in ``record_revision`` (the durable history used for every field change),
and adds the database constraint as the last line of defence for direct SQL.

Runs in a data step before the constraint so existing rows cannot block it. The
constraint is Postgres-specific JSONB comparison, which is why it is added with SQL
rather than a model ``CheckConstraint``.
"""

from django.db import migrations

CONSTRAINT = 'patientrecord_no_pre_existing_flag_needs_empty_list'
ACTOR = 'migration:1988'


def _has_categories(value):
    return isinstance(value, list) and any(
        isinstance(item, str) and item.strip() for item in value
    )


def resolve_conflicts(apps, schema_editor):
    PatientRecord = apps.get_model('omop_core', 'PatientRecord')
    RecordRevision = apps.get_model('omop_core', 'RecordRevision')
    alias = schema_editor.connection.alias

    candidates = PatientRecord.objects.using(alias).filter(
        no_pre_existing_conditions=True,
    ).only('id', 'preexisting_conditions', 'no_pre_existing_conditions')

    revisions = []
    resolved = []
    for record in candidates.iterator():
        if not _has_categories(record.preexisting_conditions):
            continue
        resolved.append(record.pk)
        revisions.append(RecordRevision(
            patient_record_id=record.pk,
            changed_by=ACTOR,
            field='no_pre_existing_conditions',
            old_value='True',
            new_value='',
        ))
    if revisions:
        RecordRevision.objects.using(alias).bulk_create(revisions)
    if resolved:
        PatientRecord.objects.using(alias).filter(pk__in=resolved).update(
            no_pre_existing_conditions=None,
        )


class Migration(migrations.Migration):

    dependencies = [('omop_core', '0304_deactivate_pre_existing_condition_formula')]

    operations = [
        migrations.RunPython(resolve_conflicts, migrations.RunPython.noop),
        migrations.RunSQL(
            sql=(
                f'ALTER TABLE patient_record ADD CONSTRAINT {CONSTRAINT} '
                "CHECK (no_pre_existing_conditions IS NOT TRUE"
                ' OR preexisting_conditions IS NULL'
                " OR preexisting_conditions IN ('[]'::jsonb, 'null'::jsonb))"
            ),
            reverse_sql=f'ALTER TABLE patient_record DROP CONSTRAINT {CONSTRAINT}',
        ),
    ]
