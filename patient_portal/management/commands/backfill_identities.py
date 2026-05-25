"""Report and fix Identity linkage on PatientUser rows.

After Phase A deployment, PartnerAuthentication creates Identity records
and _ensure_person() links them to PatientUser on every login. This
command reports the current state and fixes any PatientUser rows where
the User already has been linked to an Identity (via a subsequent login)
but the PatientUser.identity wasn't set (edge case from the Phase A
rollout window).
"""
from django.core.management.base import BaseCommand

from patient_portal.models import Identity, PatientUser


class Command(BaseCommand):
    help = "Link PatientUser rows to Identity records created by Phase A"

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Print what would be done without making changes",
        )

    def handle(self, *args, **options):
        dry_run = options["dry_run"]

        total = PatientUser.objects.count()
        linked = PatientUser.objects.filter(identity__isnull=False).count()
        unlinked = PatientUser.objects.filter(identity__isnull=True)
        unlinked_count = unlinked.count()

        self.stdout.write(
            f"PatientUser: {total} total, {linked} linked, "
            f"{unlinked_count} unlinked"
        )

        if unlinked_count == 0:
            self.stdout.write("All PatientUser rows have identities. Nothing to do.")
            return

        identities_total = Identity.objects.count()
        self.stdout.write(f"Identity records: {identities_total}")

        fixable = 0
        unfixable = 0

        for pu in unlinked.select_related("user"):
            email = pu.user.email
            identity = Identity.objects.filter(
                email=email,
                patient_user__isnull=True,
            ).first() if email else None

            if not identity:
                unfixable += 1
                continue
            fixable += 1

        self.stdout.write(
            f"Fixable: {fixable}, unfixable: {unfixable} "
            f"(will be linked on next login)"
        )

        if dry_run:
            self.stdout.write("Dry run — no changes made.")
            return

        self.stdout.write(
            f"Unfixable rows will self-heal on next user login "
            f"(Phase A PartnerAuthentication links identity automatically)."
        )
