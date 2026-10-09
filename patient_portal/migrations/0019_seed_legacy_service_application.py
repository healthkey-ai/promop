from django.db import migrations

# The legacy SERVICE_AUTH_TOKEN credential authenticates as service_id
# 'hk-labs-sync' (patient_portal/service_tokens.py), but 0017 seeded only
# 'hk-labs'. Disabling "HK-Labs" in Org Admin therefore left the legacy
# PHI-writing token working: check_environment_fallback() looks the application
# up by the credential's own service_id and found nothing.
LEGACY_SERVICE_ID = 'hk-labs-sync'
MANAGED_SERVICE_ID = 'hk-labs'
# One literal, referenced by the forward and the reverse: two copies would drift
# and the reverse would stop recognising what the forward wrote.
LEGACY_NAME = 'HK-Labs (legacy environment grant)'
LEGACY_NOTE = (
    'Kill switch for the SERVICE_AUTH_TOKEN environment credential. '
    'Its scopes come from SERVICE_AUTH_SCOPES, not from this record, '
    'until it is replaced by a managed token issued here — set them '
    'before issuing that token.'
)
MANAGED_NOTE = (
    'Managed tokens for HK-Labs. The legacy SERVICE_AUTH_TOKEN credential '
    'is NOT governed by this record — disable "hk-labs-sync" to stop it.'
)


def seed_legacy_application(apps, schema_editor):
    Application = apps.get_model('patient_portal', 'ServiceApplication')
    rows = Application.objects.using(schema_editor.connection.alias)
    # 0017 seeded 'hk-labs' on the assumption that it was the legacy credential's
    # principal. It is not, and leaving both rows unexplained puts two plausible
    # HK-Labs entries in Org Admin, one of which silently does nothing. Say so on
    # the row itself.
    #
    # Guarded on the description alone, not on tokens: the documented setup runs
    # import_service_tokens, whose distributed file includes 'hk-labs', so any
    # deployment that followed the runbook has a token on this row — and that is
    # exactly the deployment that needs the label. An operator's own description
    # is still never overwritten.
    rows.filter(service_id=MANAGED_SERVICE_ID, description='').update(description=MANAGED_NOTE)
    rows.get_or_create(
        service_id=LEGACY_SERVICE_ID,
        defaults={
            'name': LEGACY_NAME,
            # Deliberately no scopes: while the environment credential is live
            # its scopes come from SERVICE_AUTH_SCOPES, and seeding a guess here
            # would either understate the live grant or silently narrow it at
            # cutover. Whoever issues the first managed token sets them.
            'scopes': '',
            'description': LEGACY_NOTE,
        },
    )


def drop_legacy_application(apps, schema_editor):
    """Remove the row only while it is still exactly what this migration seeded.

    Matched on every seeded field, not just "active and tokenless": an operator
    who created an hk-labs-sync record by hand before this ran wrote something
    this migration did not, and a rollback must not take it with them.

    A row an operator has disabled is the kill switch doing its job: deleting it
    on a rollback, and recreating it enabled on the next roll-forward, would
    re-arm the legacy credential with nothing in the UI or the logs to say so.
    """
    Application = apps.get_model('patient_portal', 'ServiceApplication')
    rows = Application.objects.using(schema_editor.connection.alias)
    rows.filter(
        service_id=LEGACY_SERVICE_ID, name=LEGACY_NAME, description=LEGACY_NOTE,
        scopes='', is_active=True, tokens__isnull=True,
    ).delete()
    # Exact match, not a prefix: an operator who kept the note and appended their
    # own text has written something this migration did not, and a rollback must
    # not eat it.
    rows.filter(service_id=MANAGED_SERVICE_ID, description=MANAGED_NOTE).update(description='')


class Migration(migrations.Migration):
    dependencies = [('patient_portal', '0018_alter_serviceapplication_scopes')]
    operations = [migrations.RunPython(seed_legacy_application, drop_legacy_application)]
