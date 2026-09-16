"""Seed the field-curation tables from the checked-in fixture.

On a fresh database this populates the full curated inventory so new instances
start with approved mappings, choices, formulas, and synonyms. On an existing
database it is a no-op for rows that already match (natural-key upsert).
"""
import json
import os

from django.db import migrations


def load_fixture(apps, schema_editor):
    # Import apply_payload at runtime so the migration does not break if the
    # module is refactored later — elidable=True lets Django skip it entirely
    # once the migration is recorded.
    from django.db import connection

    # Skip in test databases: Django creates test DBs named test_<original>,
    # and seeding 314 mappings collides with tests that create their own.
    db_name = connection.settings_dict.get('NAME', '')
    if db_name.startswith('test_'):
        return

    from omop_core.mapping.field import apply_payload

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

    stats = apply_payload(data, tables=tables, prune=False, dry_run=False)

    total_created = stats.total(stats.created)
    total_updated = stats.total(stats.updated)
    if total_created or total_updated:
        print(f'  Seeded field curation: {total_created} created, {total_updated} updated.')
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
        migrations.RunPython(load_fixture, reverse, elidable=True),
    ]
