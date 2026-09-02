from django.conf import settings
from django.core.checks import Error, Tags, Warning, register


@register(Tags.security, deploy=True)
def production_key_separation_check(app_configs, **kwargs):
    """Require independent audit/export signing keys outside DEBUG.

    Audit-event tamper evidence and FHIR-export signatures are independent
    controls. Falling back to SECRET_KEY is acceptable for local development, but
    production needs key separation so rotating the Django signing key does not
    also rewrite the trust basis for audit and interchange evidence.
    """
    if getattr(settings, 'DEBUG', False):
        return []

    errors = []
    secret_key = getattr(settings, 'SECRET_KEY', '')
    configured_keys = {}
    for setting_name, check_id in (
        ('AUDIT_HMAC_KEY', 'patient_portal.E001'),
        ('EXPORT_SIGNING_KEY', 'patient_portal.E002'),
    ):
        value = getattr(settings, setting_name, '')
        if not value:
            errors.append(Error(
                f'{setting_name} must be set in production.',
                hint=(
                    f'Set {setting_name} to a managed secret independent from '
                    'SECRET_KEY.'
                ),
                id=check_id,
            ))
        elif value == secret_key:
            errors.append(Error(
                f'{setting_name} must not equal SECRET_KEY in production.',
                hint=f'Rotate {setting_name} to a separate managed secret.',
                id=check_id,
            ))
        else:
            configured_keys[setting_name] = value

    if (
        configured_keys.get('AUDIT_HMAC_KEY')
        and configured_keys.get('AUDIT_HMAC_KEY') == configured_keys.get('EXPORT_SIGNING_KEY')
    ):
        errors.append(Error(
            'AUDIT_HMAC_KEY and EXPORT_SIGNING_KEY must be different in production.',
            hint='Provision one managed secret for each signing purpose.',
            id='patient_portal.E003',
        ))
    return errors


@register(Tags.compatibility)
def prolog_runner_mount_check(app_configs, **kwargs):
    """The survey runner is either served or it is not; half-mounted is the bug.

    `PROLOG_RUNNER_DIST` is read at startup and the routes are added only when
    the directory is there. The Surveys tab is independent of that: it lists
    what the runner serves and links to `/s/<slug>` regardless. So a typo in
    the path, or a build that did not land in the image, produces a patient
    clicking Start and arriving at the portal dashboard — a dead end with no
    error anywhere.
    """
    issues = []
    dist = getattr(settings, 'RUNNER_DIST', None)
    if dist is not None:
        if not dist.exists():
            issues.append(Error(
                f'PROLOG_RUNNER_DIST is set to {dist}, which does not exist.',
                hint=(
                    'Point it at the runner\'s build output, or unset it. As it '
                    'stands the API is mounted but no survey page is served, and '
                    'the Surveys tab still links to /s/<slug>.'
                ),
                id='patient_portal.E004',
            ))
        elif not (dist / 'index.html').exists():
            issues.append(Error(
                f'PROLOG_RUNNER_DIST ({dist}) has no index.html.',
                hint=(
                    'It should be the runner\'s built dist directory — the one '
                    'containing index.html and assets/ — not its parent.'
                ),
                id='patient_portal.E004',
            ))
    elif getattr(settings, 'PROLOG_DEFINITION_DIRS', None):
        issues.append(Warning(
            'Surveys are configured but no runner front end is mounted.',
            hint=(
                'PROLOG_DEFINITION_DIRS is set and PROLOG_RUNNER_DIST is not, so '
                'the API answers but /s/<slug> returns the portal shell. Set '
                'PROLOG_RUNNER_DIST, or leave the definitions unmounted too.'
            ),
            id='patient_portal.W004',
        ))
    return issues


@register(Tags.caches, deploy=True)
def throttle_cache_is_shared_check(app_configs, **kwargs):
    """A per-process throttle counter is not a rate limit.

    DRF counts throttled requests in caches['default']. LocMemCache is private
    to each worker, so N gunicorn workers allow N times every published rate
    and a restart forgets them all. The one that matters is `run.create`: it is
    the only bound on an unauthenticated caller minting Person rows through the
    survey runner.
    """
    if getattr(settings, 'DEBUG', False):
        return []
    backend = settings.CACHES.get('default', {}).get('BACKEND', '')
    if 'locmem' not in backend.lower():
        return []
    return [Warning(
        'Throttle counters are stored in per-process memory.',
        hint=(
            'Set CACHE_URL (or a Redis CELERY_BROKER_URL) so DEFAULT_THROTTLE_RATES '
            'are enforced across workers. As configured, every rate is multiplied '
            'by the worker count — including run.create, the bound on anonymous '
            'Person creation.'
        ),
        id='patient_portal.W005',
    )]
