from django.db import migrations, models
from django.db.models.functions import Lower
from django.utils import timezone


def backfill(apps, schema_editor):
    """Mark as verified the local accounts that have already proved their address.

    The proof that exists today: the account is staff (made by an operator), or
    an emailed invitation to that address was accepted -- the token only ever
    went to the mailbox. Everything else is a self-signup, or a trusted-app
    signup, whose address nobody checked; those stay unverified and get their
    email-derived access back by following a verification or password-reset link.

    Federated identities establish verification on their next verified login;
    issuer names and stored email text are not proof by themselves.
    """
    Identity = apps.get_model('patient_portal', 'Identity')
    OrgInvitation = apps.get_model('omop_core', 'OrgInvitation')
    PatientInvitation = apps.get_model('patient_portal', 'PatientInvitation')

    proved = set(
        OrgInvitation.objects.filter(confirmed_at__isnull=False)
        .annotate(e=Lower('email')).values_list('e', flat=True)
    ) | set(
        PatientInvitation.objects.filter(accepted_at__isnull=False)
        .annotate(e=Lower('email')).values_list('e', flat=True)
    )
    now = timezone.now()
    local = Identity.objects.filter(issuer='urn:local', email_verified_at__isnull=True).exclude(email='')
    local.filter(is_staff=True).update(email_verified_at=now)
    ids = [
        pk for pk, email in local.values_list('pk', 'email')
        if email.lower() in proved
    ]
    Identity.objects.filter(pk__in=ids).update(email_verified_at=now)

    # Earlier private-domain signups persisted analyst grants. Requiring a
    # verified domain in the access query alone would leave these grants live.
    GroupAccess = apps.get_model('omop_core', 'GroupAccess')
    OrgTrust = apps.get_model('omop_core', 'OrgTrust')
    for identity in local.filter(email_verified_at__isnull=True).iterator():
        domain = identity.email.rpartition('@')[2]
        if domain:
            trusted_orgs = OrgTrust.objects.filter(
                trusted_domain__iexact=domain,
                granting_org__allows_patient_signup=False,
            ).values_list('granting_org_id', flat=True)
            GroupAccess.objects.filter(
                identity_id=identity.pk, role='analyst', granted_by__isnull=True,
                org_id__in=trusted_orgs,
            ).update(role='patient', pending_email_verification=True)
        # Revoke email-addressed grants issued before ownership was proved.
        # Passwordless placeholders stay available to the verified SSO flow.
        if identity.password and not identity.password.startswith('!'):
            for invitation in OrgInvitation.objects.filter(
                email__iexact=identity.email, confirmed_at__isnull=True,
            ).iterator():
                GroupAccess.objects.filter(
                    identity_id=identity.pk, org_id=invitation.org_id,
                    role=invitation.role, granted_by_id=invitation.invited_by_id,
                ).delete()


class Migration(migrations.Migration):

    dependencies = [
        ('patient_portal', '0019_seed_legacy_service_application'),
        # The backfill reads omop_core.OrgInvitation.
        ('omop_core', '0247_groupaccess_pending_email_verification'),
    ]

    operations = [
        migrations.AddField(
            model_name='identity',
            name='email_verified_at',
            field=models.DateTimeField(blank=True, help_text='When this account proved it receives mail at `email`: by following an emailed verification, invitation or password-reset link. Null for a local account that only typed the address in. Anything that grants access because of the address (a domain trust, a pending invitation, matching a person by email) must go through `has_verified_email`.', null=True),
        ),
        migrations.RunPython(backfill, migrations.RunPython.noop),
    ]
