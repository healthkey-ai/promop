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


def test_preparation_never_invokes_an_unconditional_full_loader():
    script = commands(PREPARE)

    # prepare_production_database loads only what migration 0201 needs. The two
    # commands below load whole vocabularies and must not run on a web boot.
    assert 'python manage.py seed_omop_concepts' not in script
    assert 'python manage.py load_athena_vocabularies' not in script
    assert 'python manage.py prepare_production_database --gdrive' in script
    assert 'python manage.py queue_athena_vocabulary_sync --gdrive' in script
    assert 'python manage.py sync_athena_vocabulary --gdrive' not in script


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


def web_services():
    blueprint = yaml.safe_load((ROOT / 'render.yaml').read_text())
    return [s for s in blueprint['services'] if s.get('type') == 'web']


def test_staging_prepares_once_per_deploy():
    """Staging gains the release phase Cloud Run already has.

    A failure in a pre-deploy stops the deploy and leaves the running version
    serving; the same failure during web boot gives a crash-looping service.
    """
    staging = [s for s in web_services() if s.get('branch') == 'dev']

    assert staging, 'no web service tracking dev'
    for service in staging:
        assert service.get('preDeployCommand') == 'bash scripts/prepare-deployment.sh', (
            f"{service['name']} does not prepare in its release phase"
        )


def test_a_pre_deploy_only_names_a_script_this_branch_actually_has():
    """A Blueprint sync applies settings to every service it declares, whatever
    branch each one deploys code from -- and changing a service's settings
    redeploys it. Pointing production at a script that exists on dev but not on
    main failed every production deploy on "No such file or directory", from a
    sync alone, with no release to main involved.

    A service can therefore only carry a pre-deploy naming a file present on the
    branch it deploys. This checks the weaker thing a test can see: the file
    exists here at all. The branch-relative half is why the production service
    carries a comment instead of a hook.
    """
    for service in web_services():
        command = service.get('preDeployCommand')
        if not command:
            continue
        script = command.split()[-1]
        assert (ROOT / script).exists(), (
            f"{service['name']} pre-deploys {script}, which is not in the repo"
        )


def test_production_waits_for_the_script_to_reach_its_branch():
    production = [s for s in web_services() if s.get('branch') == 'main']

    assert production, 'no web service tracking main'
    for service in production:
        # Remove this only together with dev reaching main, or the next
        # Blueprint sync breaks production deploys again.
        assert service.get('preDeployCommand') is None, (
            f"{service['name']} pre-deploys a script main may not have"
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


def test_workers_inherit_the_loinc_credentials_rather_than_repeating_them():
    """The load runs on Celery, so the workers need the credentials too -- but
    a secret entered in four dashboards is a secret that ends up different in
    one of them. They inherit from their web service, as ANTHROPIC_API_KEY
    already does (#1624).
    """
    blueprint = yaml.safe_load((ROOT / 'render.yaml').read_text())
    services = {s['name']: s for s in blueprint['services']}

    for worker, web in (('promop-worker', 'promop'),
                        ('promop-staging-worker', 'promop-staging')):
        env = {e['key']: e for e in services[worker].get('envVars', [])}
        for key in ('LOINC_USER', 'LOINC_PASSWORD'):
            assert key in env, f'{worker} has no {key}; the release load would fail there'
            source = env[key].get('fromService') or {}
            assert source.get('name') == web and source.get('envVarKey') == key, (
                f'{worker}.{key} should inherit from {web}, not be set separately'
            )


def test_every_render_worker_consumes_the_dedicated_athena_queue():
    blueprint = yaml.safe_load((ROOT / 'render.yaml').read_text())
    services = {service['name']: service for service in blueprint['services']}

    assert '--queues=celery,athena' in services['promop-worker']['startCommand']
    assert services['promop-staging-worker']['startCommand'] == 'bash start-worker.sh'
    assert '--queues=celery,athena' in commands(ROOT / 'start-worker.sh')


def test_the_web_services_are_where_the_loinc_credentials_are_entered():
    blueprint = yaml.safe_load((ROOT / 'render.yaml').read_text())

    for service in blueprint['services']:
        if service.get('type') != 'web':
            continue
        env = {e['key']: e for e in service.get('envVars', [])}
        for key in ('LOINC_USER', 'LOINC_PASSWORD'):
            assert env.get(key, {}).get('sync') is False, (
                f"{service['name']}.{key} must be dashboard-managed, never committed"
            )


def test_render_requires_the_athena_source_for_the_web_service():
    blueprint = (ROOT / 'render.yaml').read_text()

    web_service = blueprint.split('  - type: worker', 1)[0]
    assert '- key: ATHENA_VOCABULARY_GDRIVE_URL' in web_service
