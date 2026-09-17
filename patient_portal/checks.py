import json

from django.conf import settings
from django.core.checks import Error, Info, Tags, Warning, register


@register(Tags.security, deploy=True)
def production_key_separation_check(app_configs, **kwargs):
    """Require independent audit/export signing keys on deployments.

    Audit-event tamper evidence and FHIR-export signatures are independent
    controls. Falling back to SECRET_KEY is acceptable for local development, but
    production needs key separation so rotating the Django signing key does not
    also rewrite the trust basis for audit and interchange evidence.
    """
    if settings.DEBUG and not settings.IS_DEPLOYED:
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
    if settings.DEBUG and not settings.IS_DEPLOYED:
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


@register(Tags.security, deploy=True)
def security_posture_check(app_configs, **kwargs):
    """Report an allowlist of effective controls, never credentials or URLs."""
    posture = {
        name: getattr(settings, name)
        for name in (
            'DEBUG', 'IS_DEPLOYED', 'CORS_ALLOW_ALL_ORIGINS',
            'CORS_ALLOW_CREDENTIALS', 'FIREBASE_SKIP_REVOCATION_CHECK',
            'SESSION_COOKIE_SECURE', 'CSRF_COOKIE_SECURE', 'SECURE_SSL_REDIRECT',
            'SECURE_HSTS_SECONDS', 'SECURE_HSTS_INCLUDE_SUBDOMAINS',
            'SECURE_HSTS_PRELOAD', 'SECURE_CONTENT_TYPE_NOSNIFF',
            'X_FRAME_OPTIONS', 'SECURE_PROXY_SSL_HEADER',
        )
    }
    posture['ALLOWED_HOSTS_CONFIGURED'] = bool(settings.ALLOWED_HOSTS)
    posture['WILDCARD_HOSTS'] = '*' in settings.ALLOWED_HOSTS
    posture['CORS_ALLOWED_ORIGINS_CONFIGURED'] = bool(settings.CORS_ALLOWED_ORIGINS)
    posture['BASIC_AUTH_ENABLED'] = (
        'rest_framework.authentication.BasicAuthentication'
        in settings.REST_FRAMEWORK['DEFAULT_AUTHENTICATION_CLASSES']
    )
    # Default to the toolkit's own, because an absent key is not an empty one:
    # it falls back to ["http", "https"], so reporting [] here would claim E006
    # "empty" for a configuration that in fact accepts http, and suppress E005.
    # Subscripting instead would raise inside a registered check and abort the
    # whole run, hiding E001-E004 behind a traceback.
    posture['ALLOWED_REDIRECT_URI_SCHEMES'] = settings.OAUTH2_PROVIDER.get(
        'ALLOWED_REDIRECT_URI_SCHEMES', ['http', 'https'])
    for name in ('PHR_AUDIENCE', 'PHR_BASE_URL', 'FIREBASE_PROJECT_ID'):
        posture[f'{name}_CONFIGURED'] = bool(getattr(settings, name))
    issues = [Info(
        'Effective security posture: ' + json.dumps(posture, sort_keys=True),
        id='patient_portal.I001',
    )]
    # Compare the way django-oauth-toolkit does: it lowercases the allowed
    # schemes (Application.clean), while _env_list only strips. Without this,
    # ALLOWED_REDIRECT_URI_SCHEMES=https,HTTP accepts plaintext redirects while
    # reporting nothing here. Posture keeps the raw value it was configured with.
    schemes = [scheme.lower() for scheme in posture['ALLOWED_REDIRECT_URI_SCHEMES']]
    http_redirects = 'http' in schemes
    for enabled, label in (
        (posture['WILDCARD_HOSTS'], 'Wildcard ALLOWED_HOSTS'),
        (settings.CORS_ALLOW_ALL_ORIGINS, 'All-origin CORS'),
        (posture['BASIC_AUTH_ENABLED'], 'HTTP Basic authentication'),
        (settings.FIREBASE_SKIP_REVOCATION_CHECK, 'Skipped Firebase revocation checks'),
        # Deployed hosts get E005 below instead: there the override is not a
        # reviewable choice, it is a plaintext authorization-code redirect.
        (http_redirects and not posture['IS_DEPLOYED'], 'HTTP OAuth redirects'),
    ):
        if enabled:
            issues.append(Warning(
                f'{label} is explicitly enabled.',
                hint='Review this security override before deploying.',
                id='patient_portal.W006',
            ))
    if http_redirects and posture['IS_DEPLOYED']:
        issues.append(Error(
            'HTTP OAuth redirect URIs are accepted on a deployed service.',
            hint='Drop http from ALLOWED_REDIRECT_URI_SCHEMES. It exists for local '
                 'callbacks; on a deployed host it lets an authorization code be '
                 'returned over plaintext.',
            id='patient_portal.E005',
        ))
    if not schemes:
        issues.append(Error(
            'ALLOWED_REDIRECT_URI_SCHEMES is empty.',
            hint='django-oauth-toolkit treats the setting as mandatory and raises '
                 'AttributeError from inside Application.clean() at first use, so an '
                 'empty value surfaces as a 500 rather than a configuration error. '
                 'Set https, or https,http for local HTTP callbacks.',
            id='patient_portal.E006',
        ))
    return issues


@register(Tags.security, deploy=True)
def service_token_scope_check(app_configs, **kwargs):
    """Report environment service grants asking for scopes the cap does not list.

    `ALLOWED_SCOPES` is enforced by the serializer, the token importer and now
    the model, but `SERVICE_AUTH_TOKENS` is parsed straight out of the
    environment. An unrecognised scope grants nothing — `ScopedTokenPermission`
    intersects against fixed sets — so this is a warning rather than an error:
    the deployment is not unsafe, its configuration is just not doing what it
    says.
    """
    from patient_portal.service_tokens import ALLOWED_SCOPES

    warnings = []
    grants = getattr(settings, 'SERVICE_AUTH_TOKENS', {})
    # settings.py validates this at import, but the check also runs before
    # migrate on every deploy: a traceback here fails the deploy with no message.
    configured = [
        (service_id, (grant or {}).get('scopes', ''))
        for service_id, grant in (grants.items() if isinstance(grants, dict) else [])
        if isinstance(grant, dict)
    ]
    if (getattr(settings, 'SERVICE_AUTH_TOKEN', '') or '').strip():
        configured.append(('SERVICE_AUTH_TOKEN', getattr(settings, 'SERVICE_AUTH_SCOPES', '')))
    for service_id, scopes in configured:
        unsupported = set((scopes or '').split()) - ALLOWED_SCOPES
        if unsupported:
            warnings.append(Warning(
                f'Service grant {service_id} requests unsupported scope(s): '
                f'{" ".join(sorted(unsupported))}.',
                hint='Unrecognised scopes grant nothing; use a scope from ALLOWED_SCOPES.',
                id='patient_portal.W007',
            ))
    return warnings
