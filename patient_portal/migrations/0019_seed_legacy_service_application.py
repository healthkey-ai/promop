from django.db import migrations

# The legacy SERVICE_AUTH_TOKEN credential authenticates as service_id
# 'hk-labs-sync' (patient_portal/service_tokens.py), but 0017 seeded only
# 'hk-labs'. Disabling "HK-Labs" in Org Admin therefore left the legacy
# PHI-writing token working: check_environment_fallback() looks the application
# up by the credential's own service_id and found nothing.
LEGACY_SERVICE_ID = 'hk-labs-sync'


def seed_legacy_application(apps, schema_editor):
    Application = apps.get_model('patient_portal', 'ServiceApplication')
    rows = Application.objects.using(schema_editor.connection.alias)
    # 0017 seeded 'hk-labs' on the assumption that it was the legacy credential's
    # principal. It is not, and leaving both rows unexplained puts two plausible
    # HK-Labs entries in Org Admin, one of which silently does nothing. Say so on
    # the row itself — but only while nobody has edited or used it.
    rows.filter(service_id='hk-labs', description='', tokens__isnull=True).update(
        description=(
            'Managed tokens for HK-Labs. The legacy SERVICE_AUTH_TOKEN credential '
            'is NOT governed by this record — disable "hk-labs-sync" to stop it.'
        ),
    )
    rows.get_or_create(
        service_id=LEGACY_SERVICE_ID,
        defaults={
            'name': 'HK-Labs (legacy environment grant)',
            # Deliberately no scopes: while the environment credential is live
            # its scopes come from SERVICE_AUTH_SCOPES, and seeding a guess here
            # would either understate the live grant or silently narrow it at
            # cutover. Whoever issues the first managed token sets them.
            'scopes': '',
            'description': (
                'Kill switch for the SERVICE_AUTH_TOKEN environment credential. '
                'Its scopes come from SERVICE_AUTH_SCOPES, not from this record, '
                'until it is replaced by a managed token issued here — set them '
                'before issuing that token.'
            ),
        },
    )


def drop_legacy_application(apps, schema_editor):
    """Remove the row only while it is still exactly what this migration seeded.

    A row an operator has disabled is the kill switch doing its job: deleting it
    on a rollback, and recreating it enabled on the next roll-forward, would
    re-arm the legacy credential with nothing in the UI or the logs to say so.
    """
    Application = apps.get_model('patient_portal', 'ServiceApplication')
    rows = Application.objects.using(schema_editor.connection.alias)
    rows.filter(service_id=LEGACY_SERVICE_ID, is_active=True, tokens__isnull=True).delete()
    rows.filter(service_id='hk-labs', description__startswith='Managed tokens for HK-Labs.').update(
        description='',
    )


class Migration(migrations.Migration):
    dependencies = [('patient_portal', '0018_alter_serviceapplication_scopes')]
    operations = [migrations.RunPython(seed_legacy_application, drop_legacy_application)]
