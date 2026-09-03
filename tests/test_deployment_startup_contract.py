from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_production_loads_athena_before_mappings_that_depend_on_it():
    """Keep migration 0201 from ever seeding null-target LOINC mappings."""
    script = (ROOT / 'start.sh').read_text()

    baseline_migration = 'python manage.py migrate omop_core 0200 --noinput'
    vocabulary_load = 'python manage.py load_athena_vocabularies --gdrive'
    remaining_migrations = 'python manage.py migrate --noinput'

    assert 'python manage.py seed_omop_concepts' not in script
    assert baseline_migration in script
    assert vocabulary_load in script
    assert remaining_migrations in script
    assert script.index(baseline_migration) < script.index(vocabulary_load)
    assert script.index(vocabulary_load) < script.index(remaining_migrations)
    assert 'ATHENA_VOCABULARY_GDRIVE_URL' in script


def test_render_requires_the_athena_source_for_the_web_service():
    blueprint = (ROOT / 'render.yaml').read_text()

    web_service = blueprint.split('  - type: worker', 1)[0]
    assert '- key: ATHENA_VOCABULARY_GDRIVE_URL' in web_service
