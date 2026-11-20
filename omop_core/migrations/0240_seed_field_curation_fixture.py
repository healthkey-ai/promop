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

**Historical models only (#2440).**  This used to call
``field_curation_transfer.apply_payload`` with the live models, so ``migrate``
on an empty database broke as soon as a later migration added a column to one
of them (0333 added ``field_concept_mapping.tab``).  The seed is written here
against ``apps.get_model`` and the columns these tables had at this point in the
graph.  It writes the rows ``apply_payload(prune=False)`` wrote for the rows
left after the natural-key filter, each of which was a create.  Custom fields
were not filtered before; an existing one is now left alone rather than
overwritten (the fixture carries none).  The historical model does not send the
live ``post_save`` that projects an approved mapping into OMOP. A fresh
database has no patient data for it to project; a restored dump older than
this migration that does hold patients is not re-projected here.  A column added to the
fixture after this migration is not seeded here; a new migration has to carry
it.
"""
import json
import os

from django.db import migrations

# FieldConceptMapping columns copied verbatim, as of this migration.  Excludes
# id, concept (resolved below), reviewer (cleared), and the auto timestamps.
_MAPPING_FIELDS = (
    'provenance',
    'vocabulary_id', 'concept_code', 'unit', 'omop_table', 'source_value',
    'value_kind', 'type_concept_id', 'value_vocabulary', 'multiple',
    'status', 'reviewed_at', 'notes',
)
# A payload written before the column existed may omit it; it reads as blank.
_OPTIONAL_MAPPING_FIELDS = frozenset({'provenance'})
_CUSTOM_FIELD_FIELDS = ('display_name', 'tab', 'field_type', 'mode')


def _resolve_concept(Concept, alias, row):
    """The local Concept a fixture mapping names, or None.

    ``(vocabulary_id, concept_code)`` wins; the id is only a fallback for a row
    that carries no code, since locally minted concept ids differ per instance.
    """
    vocab = row.get('concept_vocabulary_id') or row.get('vocabulary_id') or ''
    code = row.get('concept_code_resolved') or row.get('concept_code') or ''
    concepts = Concept.objects.using(alias)
    if vocab and code:
        return concepts.filter(vocabulary_id=vocab, concept_code=code).first()
    if row.get('concept_id') is not None:
        return concepts.filter(concept_id=row['concept_id']).first()
    return None


def seed(apps, alias, data):
    """Create the fixture rows whose natural key is absent. Returns the count."""
    Concept = apps.get_model('omop_core', 'Concept')
    FieldConceptMapping = apps.get_model('omop_core', 'FieldConceptMapping')
    CustomPatientField = apps.get_model('omop_core', 'CustomPatientField')
    FieldChoice = apps.get_model('omop_core', 'FieldChoice')
    FieldChoiceCode = apps.get_model('omop_core', 'FieldChoiceCode')
    FieldFormula = apps.get_model('omop_core', 'FieldFormula')
    FieldSynonym = apps.get_model('omop_core', 'FieldSynonym')

    tables = tuple(data.get('tables', []))
    created = 0

    # Mappings first: a custom field requires its mapping.
    if 'mappings' in tables:
        existing = set(
            FieldConceptMapping.objects.using(alias).values_list('field_name', flat=True),
        )
        for row in data.get('mappings', []):
            if row['field_name'] in existing:
                continue
            values = {
                name: row.get(name, '') if name in _OPTIONAL_MAPPING_FIELDS else row[name]
                for name in _MAPPING_FIELDS
            }
            FieldConceptMapping.objects.using(alias).create(
                field_name=row['field_name'],
                concept=_resolve_concept(Concept, alias, row),
                reviewer=None,
                **values,
            )
            existing.add(row['field_name'])
            created += 1

    if 'custom_fields' in tables:
        existing = set(
            CustomPatientField.objects.using(alias).values_list('field_name', flat=True),
        )
        mappings = {m.field_name: m for m in FieldConceptMapping.objects.using(alias)}
        for row in data.get('custom_fields', []):
            mapping = mappings.get(row['mapping_field_name'])
            if row['field_name'] in existing or mapping is None:
                continue
            CustomPatientField.objects.using(alias).create(
                field_name=row['field_name'],
                mapping=mapping,
                created_by=None,
                **{name: row[name] for name in _CUSTOM_FIELD_FIELDS},
            )
            existing.add(row['field_name'])
            created += 1

    if 'choices' in tables:
        existing = set(
            FieldChoice.objects.using(alias).values_list('field_name', 'display'),
        )
        for row in data.get('choices', []):
            key = (row['field_name'], row['display'])
            if key in existing:
                continue
            choice = FieldChoice.objects.using(alias).create(
                field_name=row['field_name'],
                display=row['display'],
                sort_order=row['sort_order'],
                created_by=None,
            )
            FieldChoiceCode.objects.using(alias).bulk_create([
                FieldChoiceCode(
                    choice=choice,
                    code=code['code'],
                    vocabulary_id=code['vocabulary_id'],
                    display=code.get('display', ''),
                    is_primary=code.get('is_primary', False),
                )
                for code in row['codes']
            ])
            existing.add(key)
            created += 1

    if 'formulas' in tables:
        existing = set(
            FieldFormula.objects.using(alias).values_list('field_name', flat=True),
        )
        for row in data.get('formulas', []):
            if row['field_name'] in existing:
                continue
            FieldFormula.objects.using(alias).create(
                field_name=row['field_name'],
                formula=row['formula'],
                is_active=row['is_active'],
                created_by=None,
            )
            existing.add(row['field_name'])
            created += 1

    if 'synonyms' in tables:
        existing = set(
            FieldSynonym.objects.using(alias).values_list('field_name', 'synonym_text'),
        )
        for row in data.get('synonyms', []):
            key = (row['field_name'], row['synonym_text'])
            if key in existing:
                continue
            FieldSynonym.objects.using(alias).create(
                field_name=row['field_name'],
                synonym_text=row['synonym_text'],
                source=row['source'],
                created_by=None,
            )
            existing.add(key)
            created += 1

    return created


def load_fixture(apps, schema_editor):
    # Skip in test databases: Django's test runner creates test DBs named
    # test_<original>, and seeding 314 mappings collides with tests that
    # create their own.  pytest uses --no-migrations so this migration never
    # fires there.
    db_name = str(schema_editor.connection.settings_dict.get('NAME', ''))
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

    if not data.get('tables'):
        print('  No tables in fixture, skipping.')
        return

    created = seed(apps, schema_editor.connection.alias, data)
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
