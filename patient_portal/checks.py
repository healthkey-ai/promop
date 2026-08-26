from django.conf import settings
from django.core.checks import Error, Tags, register


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
    return errors
