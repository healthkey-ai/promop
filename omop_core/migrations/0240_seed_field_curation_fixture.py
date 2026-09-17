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
and will resolve correctly once Athena is loaded and the mapping is re-saved or
``copy_curation`` is run.
"""
import json
import os

from django.db import migrations


def load_fixture(apps, schema_editor):
    from django.db import connection

    # Skip in test databases: Django's test runner creates test DBs named
    # test_<original>, and seeding 314 mappings collides with tests that
    # create their own.  pytest uses --no-migrations so this migration never
    # fires there.
    db_name = str(connection.settings_dict.get('NAME', ''))
    if db_name.startswith('test_'):
        return

    from omop_core.models import (
        FieldChoice,
        FieldConceptMapping,
        FieldFormula,
        FieldSynonym,
    )

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

    # Filter out rows that already exist so we never overwrite curator work.
    # Only insert rows whose natural key is absent from the database.
    if 'mappings' in tables and 'mappings' in data:
        existing = set(
            FieldConceptMapping.objects.values_list('field_name', flat=True),
        )
        data['mappings'] = [
            r for r in data['mappings'] if r['field_name'] not in existing
        ]

    if 'choices' in tables and 'choices' in data:
        existing = set(
            FieldChoice.objects.values_list('field_name', 'display'),
        )
        data['choices'] = [
            r for r in data['choices']
            if (r['field_name'], r['display']) not in existing
        ]

    if 'formulas' in tables and 'formulas' in data:
        existing = set(
            FieldFormula.objects.values_list('field_name', flat=True),
        )
        data['formulas'] = [
            r for r in data['formulas'] if r['field_name'] not in existing
        ]

    if 'synonyms' in tables and 'synonyms' in data:
        existing = set(
            FieldSynonym.objects.values_list('field_name', 'synonym_text'),
        )
        data['synonyms'] = [
            r for r in data['synonyms']
            if (r['field_name'], r['synonym_text']) not in existing
        ]

    # Any rows left are genuinely new — apply_payload creates them.
    remaining = sum(len(data.get(t, [])) for t in tables)
    if remaining == 0:
        print('  Field curation already up to date.')
        return

    from omop_core.mapping.field import apply_payload

    stats = apply_payload(data, tables=tables, prune=False, dry_run=False)

    total_created = stats.total(stats.created)
    if total_created:
        print(f'  Seeded field curation: {total_created} created.')
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
