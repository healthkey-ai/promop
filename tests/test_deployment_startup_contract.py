from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_production_uses_bounded_database_preparation_before_gunicorn():
    script = (ROOT / 'start.sh').read_text()

    preparation = 'python manage.py prepare_production_database --gdrive'
    gunicorn = 'exec gunicorn promop.wsgi:application'

    assert 'python manage.py seed_omop_concepts' not in script
    assert 'python manage.py load_athena_vocabularies' not in script
    assert preparation in script
    assert script.index(preparation) < script.index(gunicorn)
    assert 'ATHENA_VOCABULARY_GDRIVE_URL' in script


def test_render_requires_the_athena_source_for_the_web_service():
    blueprint = (ROOT / 'render.yaml').read_text()

    web_service = blueprint.split('  - type: worker', 1)[0]
    assert '- key: ATHENA_VOCABULARY_GDRIVE_URL' in web_service
