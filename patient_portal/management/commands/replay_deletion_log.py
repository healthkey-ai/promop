"""Re-apply the deletion log against a restored database.

A backup taken before an account was deleted restores that account's rows.
Every deletion wrote a ``DeletionLogEntry`` naming the Person and Identity
ids it removed, so this command deletes them again: the PROlog rows, the
Person with everything that cascades from it, and the Identity. It writes
no new log entries, since the existing ones already record the deletions.

The chain is verified first and a log that does not verify is not replayed,
unless ``--ignore-chain`` says the breakage is understood (a restore from
before the chain began, say). Run it immediately after a restore, before any
new account could be given an id the log names. ``--dry-run`` only reports.

Exits 1 when the chain does not verify, so it can gate a restore procedure;
``verify_deletion_log`` alone is ``--verify-only``.
"""
from django.core.management.base import BaseCommand

from patient_portal.account_deletion import replay, verify_deletion_log


class Command(BaseCommand):
    help = 'Delete again whatever a backup restore brought back, from the deletion log.'

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true', help='Report what would be deleted; delete nothing.')
        parser.add_argument('--verify-only', action='store_true', help='Only verify the log chain.')
        parser.add_argument('--ignore-chain', action='store_true',
                            help='Replay even when the chain does not verify.')

    def handle(self, *args, **options):
        report = verify_deletion_log()
        self.stdout.write(f'Deletion log: {report.scanned} entr{"y" if report.scanned == 1 else "ies"}')
        if report.altered:
            self.stdout.write(self.style.ERROR(
                f'ALTERED entries ({len(report.altered)}): {", ".join(str(p) for p in report.altered[:20])}'))
        if report.broken:
            self.stdout.write(self.style.ERROR(
                f'CHAIN BREAKS ({len(report.broken)}) before ids: {", ".join(str(p) for p in report.broken[:20])}'))
        if report.ok:
            self.stdout.write(self.style.SUCCESS('Chain verified.'))
        if options['verify_only']:
            raise SystemExit(0 if report.ok else 1)
        if not report.ok and not options['ignore_chain']:
            self.stdout.write(self.style.ERROR('Not replaying a log that does not verify (--ignore-chain overrides).'))
            raise SystemExit(1)

        result = replay(dry_run=options['dry_run'])
        verb = 'would delete' if options['dry_run'] else 'deleted'
        self.stdout.write(
            f'Replayed {result.entries} entr{"y" if result.entries == 1 else "ies"}: '
            f'{verb} {len(result.persons_deleted)} person(s) and {len(result.identities_deleted)} identit(y/ies).'
        )
        for pid in result.persons_deleted:
            self.stdout.write(f'  person {pid}')
        for iid in result.identities_deleted:
            self.stdout.write(f'  identity {iid}')
        if not report.ok:
            raise SystemExit(1)
