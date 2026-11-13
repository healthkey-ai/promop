"""Seed the field-curation tables from the checked-in fixture.

On a fresh database this populates the full curated inventory so new instances
start with approved mappings, choices, formulas, and synonyms.  On an existing
database only **missing** rows are inserted — rows that already exist (matched
by natural key) are left untouched so curator-approved or human-edited mappings
are never overwritten.

**Athena prerequisite:** the fixture references LOINC, SNOMED, NAACCR and other
standard concepts by ``(vocabulary_id, concept_code)``.  On a database that has
not yet loaded Athena vocabularies (``load_athena_vocabularies``), concept
resolution will fail and the seeded mappings will have ``concept=None``.  They
are still usable — the vocabulary_id/concept_code columns carry the intent —
and ``load_athena_vocabularies`` points them at their Concepts at the end of a
successful load (``manage.py resolve_field_concept_mappings`` does it for an
instance that loaded Athena earlier).

**Historical models, not ``omop_core.models``.** This migration used to seed
through ``apply_payload``, which applies through the *current* models. Anything
added to those tables by a later migration is then addressed before it exists:
0333 adds ``FieldConceptMapping.tab`` after this step, so a fresh ``migrate``
emitted ``INSERT ... tab`` against a table without the column and stopped at
0240 (cancerbot-org/cancerbot#5847). A data migration describes one point in
schema history, so it writes that point's state — the fixture predates ``tab``
and never carries one, and rows are created here rather than updated, which is
all this migration ever needs (it filters out the natural keys that exist).
"""
import json
import os

from django.db import migrations

#: Columns the fixture supplies for a mapping, mapped to the model field that
#: receives them. Everything else (id, concept, reviewer, timestamps) is set
#: here or left to the model.
_MAPPING_COLUMNS = (
    'vocabulary_id', 'concept_code', 'unit', 'omop_table', 'status',
    'reviewed_at', 'notes', 'multiple', 'source_value', 'type_concept_id',
    'value_kind', 'value_vocabulary', 'provenance',
)


def _concept_for(apps, row):
    """The packaged Concept a mapping names, or None when it is not loaded.

    Same resolution order the writer uses: the resolved code first (curators
    re-point a mapping without rewriting its source code), then the denormalized
    pair, then the numeric id. None is the expected answer on a fresh install,
    where Athena has not been loaded yet.
    """
    Concept = apps.get_model('omop_core', 'Concept')
    vocabulary_id = row.get('concept_vocabulary_id') or row.get('vocabulary_id') or ''
    concept_code = row.get('concept_code_resolved') or row.get('concept_code') or ''
    if vocabulary_id and concept_code:
        found = Concept.objects.filter(
            vocabulary_id=vocabulary_id, concept_code=concept_code,
        ).first()
        if found is not None:
            return found
    concept_id = row.get('concept_id')
    if concept_id is not None:
        return Concept.objects.filter(concept_id=concept_id).first()
    return None


def _seed_mappings(apps, rows):
    FieldConceptMapping = apps.get_model('omop_core', 'FieldConceptMapping')
    existing = set(FieldConceptMapping.objects.values_list('field_name', flat=True))
    created = 0
    for row in rows:
        if row['field_name'] in existing:
            continue
        FieldConceptMapping.objects.create(
            field_name=row['field_name'],
            **{column: row.get(column) for column in _MAPPING_COLUMNS},
            concept=_concept_for(apps, row),
            # Attribution does not cross instances, as in the writer.
            reviewer=None,
        )
        created += 1
    return created


def _seed_custom_fields(apps, rows):
    CustomPatientField = apps.get_model('omop_core', 'CustomPatientField')
    FieldConceptMapping = apps.get_model('omop_core', 'FieldConceptMapping')
    created = 0
    for row in rows:
        mapping = FieldConceptMapping.objects.filter(
            field_name=row['mapping_field_name'],
        ).first()
        if mapping is None:
            print(f"  custom field {row['field_name']}: mapping "
                  f"{row['mapping_field_name']} is not present — skipped.")
            continue
        if CustomPatientField.objects.filter(field_name=row['field_name']).exists():
            continue
        CustomPatientField.objects.create(
            field_name=row['field_name'],
            display_name=row['display_name'],
            tab=row['tab'],
            field_type=row['field_type'],
            mode=row.get('mode') or 'editable',
            mapping=mapping,
            created_by=None,
        )
        created += 1
    return created


def _seed_choices(apps, rows):
    FieldChoice = apps.get_model('omop_core', 'FieldChoice')
    FieldChoiceCode = apps.get_model('omop_core', 'FieldChoiceCode')
    existing = set(FieldChoice.objects.values_list('field_name', 'display'))
    created = 0
    for row in rows:
        if (row['field_name'], row['display']) in existing:
            continue
        choice = FieldChoice.objects.create(
            field_name=row['field_name'],
            display=row['display'],
            sort_order=row['sort_order'],
            created_by=None,
        )
        FieldChoiceCode.objects.bulk_create([
            FieldChoiceCode(choice=choice, **code) for code in row['codes']
        ])
        created += 1
    return created


def _seed_formulas(apps, rows):
    FieldFormula = apps.get_model('omop_core', 'FieldFormula')
    existing = set(FieldFormula.objects.values_list('field_name', flat=True))
    created = 0
    for row in rows:
        if row['field_name'] in existing:
            continue
        FieldFormula.objects.create(
            field_name=row['field_name'],
            formula=row['formula'],
            # The fixture's active state is what the reviewed formulas shipped
            # with. A later migration deactivates an authored-answer formula,
            # which is where that rule belongs: it did not exist at this point.
            is_active=row['is_active'],
            created_by=None,
        )
        created += 1
    return created


def _seed_synonyms(apps, rows):
    FieldSynonym = apps.get_model('omop_core', 'FieldSynonym')
    existing = set(
        FieldSynonym.objects.values_list('field_name', 'synonym_text'),
    )
    created = 0
    for row in rows:
        if (row['field_name'], row['synonym_text']) in existing:
            continue
        FieldSynonym.objects.create(
            field_name=row['field_name'],
            synonym_text=row['synonym_text'],
            source=row['source'],
            created_by=None,
        )
        created += 1
    return created


_SEEDERS = {
    'mappings': _seed_mappings,
    'custom_fields': _seed_custom_fields,
    'choices': _seed_choices,
    'formulas': _seed_formulas,
    'synonyms': _seed_synonyms,
}


def load_fixture(apps, schema_editor):
    from django.db import connection

    # Skip in test databases: Django's test runner creates test DBs named
    # test_<original>, and seeding 314 mappings collides with tests that
    # create their own.  pytest uses --no-migrations so this migration never
    # fires there.
    db_name = str(connection.settings_dict.get('NAME', ''))
    if db_name.startswith('test_'):
        return

    fixture_path = os.path.join(
        os.path.dirname(os.path.dirname(__file__)),
        'data',
        'field_curation_v1.json',
    )
    if not os.path.exists(fixture_path):
        print('  Fixture not found, skipping field curation seed.')
        return

    with open(fixture_path, encoding='utf-8') as f:
        data = json.load(f)

    tables = tuple(data.get('tables', []))
    if not tables:
        print('  No tables in fixture, skipping.')
        return

    created = 0
    for table in tables:
        seeder = _SEEDERS.get(table)
        if seeder is None:
            # A table this point in schema history does not have yet.
            print(f'  Unknown fixture table {table!r}, skipping.')
            continue
        created += seeder(apps, data.get(table, []))

    if created:
        print(f'  Seeded field curation: {created} created.')
    else:
        print('  Field curation already up to date.')


def reverse(apps, schema_editor):
    # Cannot distinguish fixture-seeded rows from curator-created ones.
    # Reverse is a no-op.
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('omop_core', '0239_backfill_field_mapping_provenance'),
    ]

    operations = [
        migrations.RunPython(load_fixture, reverse),
    ]
