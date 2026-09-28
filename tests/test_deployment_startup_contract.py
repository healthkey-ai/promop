"""What must be true before the application serves, and where that is written.

Both deployment targets need the same preparation, and each used to express it
separately: Render inline in start.sh, Cloud Run in a <service>-migrate job whose
command lives in Terraform in another repository. scripts/prepare-deployment.sh
is the single in-repo statement of the sequence, so these tests guard the
sequence itself rather than the file that happened to hold it.
"""
import stat
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
PREPARE = ROOT / 'scripts' / 'prepare-deployment.sh'
START = ROOT / 'start.sh'


def commands(script_path):
    """The executable lines, without comments.

    These scripts explain themselves at length, and a comment mentioning
    gunicorn or --bind is not the same as running it -- an assertion over the
    raw text would pass or fail on prose.
    """
    return '\n'.join(
        line for line in script_path.read_text().splitlines()
        if line.strip() and not line.lstrip().startswith('#')
    )


def test_preparation_runs_the_bounded_loader_and_nothing_unbounded():
    script = commands(PREPARE)

    # prepare_production_database loads only what migration 0201 needs. The two
    # commands below load whole vocabularies and must not run on a web boot.
    assert 'python manage.py seed_omop_concepts' not in script
    assert 'python manage.py load_athena_vocabularies' not in script
    assert 'python manage.py prepare_production_database --gdrive' in script


def test_preparation_checks_the_environment_before_touching_the_database():
    script = commands(PREPARE)

    check = 'python manage.py check --deploy --fail-level ERROR'
    prepare = 'python manage.py prepare_production_database'
    assert check in script
    # A misconfigured deploy must stop before it migrates, which is what makes
    # patient_portal.E001/E002/E003 a real control rather than a CI formality.
    assert script.index(check) < script.index(prepare)


def test_preparation_aborts_without_an_athena_source():
    script = commands(PREPARE)

    # ':?' expansion, not a default: a deployment with no vocabulary source is a
    # broken deployment, and every target pointed at this script must set it.
    assert ':"${ATHENA_VOCABULARY_GDRIVE_URL:?' in script.replace(': "', ':"')


def test_preparation_does_not_start_a_server():
    script = commands(PREPARE)

    # Render execs gunicorn after this; Cloud Run runs it as a release job. A
    # server started here would make the job never finish.
    assert 'gunicorn' not in script
    assert 'celery' not in script


def test_preparation_is_directly_executable_by_another_platform():
    # Cloud Run's job runs it as a command, with no shell to be invoked through,
    # so it needs both the bit and an interpreter line. commands() strips the
    # shebang out of every other assertion here, so nothing else would notice
    # its loss -- the job would die with "Exec format error" on green tests.
    assert stat.S_IMODE(PREPARE.stat().st_mode) & stat.S_IXUSR
    assert PREPARE.read_text().startswith('#!')


def test_preparation_runs_from_the_repository_root_whatever_the_caller_did():
    script = commands(PREPARE)

    # The steps are bare `python manage.py`, and the point of this script is to
    # be pointed at by another platform's job command. Without this, a job spec
    # with no working directory fails on "can't open file 'manage.py'".
    assert 'cd "$(dirname "$0")/.."' in script
    assert script.index('cd "$(dirname "$0")/.."') < script.index('python manage.py')


def test_render_prepares_before_serving_and_keeps_no_second_copy():
    script = commands(START)

    # Through bash, not ./scripts/... -- render.yaml chmods start.sh and only
    # start.sh, so a lost exec bit on a second file would stop the web service
    # booting with nothing in the log but "Permission denied".
    prepare = 'bash scripts/prepare-deployment.sh'
    gunicorn = 'exec gunicorn promop.wsgi:application'
    assert './scripts/prepare-deployment.sh' not in script
    assert prepare in script
    assert script.index(prepare) < script.index(gunicorn)
    # Duplicating a step here is how the two targets drift apart again.
    assert 'prepare_production_database' not in script
    assert 'check --deploy' not in script
    assert 'setup_admin' not in script
    # And the guard the pre-extraction test placed on this file has to stay on
    # it: a whole-vocabulary load added here would run on every instance boot,
    # and asserting it only against the prepare script would not notice.
    assert 'python manage.py seed_omop_concepts' not in script
    assert 'python manage.py load_athena_vocabularies' not in script


def test_render_lets_the_platform_choose_bind_and_workers():
    script = commands(START)

    # gunicorn defaults to 0.0.0.0:$PORT when PORT is set and to WEB_CONCURRENCY
    # workers; hardcoding either overrides what Render or Cloud Run chose.
    assert '--bind' not in script
    assert '--workers' not in script


def test_render_prepares_once_per_deploy():
    """Render gains the release phase Cloud Run already has.

    A failure in a pre-deploy stops the deploy and leaves the running version
    serving; the same failure during web boot gives a crash-looping service.
    """
    blueprint = yaml.safe_load((ROOT / 'render.yaml').read_text())
    web = [s for s in blueprint['services'] if s.get('type') == 'web']

    assert web, 'no web services in the blueprint'
    for service in web:
        assert service.get('preDeployCommand') == 'bash scripts/prepare-deployment.sh', (
            f"{service['name']} does not prepare in its release phase"
        )


def test_the_boot_path_still_prepares_until_the_blueprint_sync_is_verified():
    """Phase 1 of two, and the ordering is what makes it safe.

    A Blueprint sync applies preDeployCommand; a code-only redeploy does not.
    Removing the boot-time call in the same change would mean the next deploy
    runs a start.sh that no longer prepares while the pre-deploy hook is not yet
    active -- migrations silently stopping on production. The script is
    idempotent, so running in both places is a no-op the second time, and the
    boot call comes out only once a synced deploy has been seen to run it.
    """
    assert 'bash scripts/prepare-deployment.sh' in commands(START)


def test_render_requires_the_athena_source_for_the_web_service():
    blueprint = (ROOT / 'render.yaml').read_text()

    web_service = blueprint.split('  - type: worker', 1)[0]
    assert '- key: ATHENA_VOCABULARY_GDRIVE_URL' in web_service
