"""
Verify audit-trail tamper-evidence (PHR-S FM TI.2.2.1).

Recomputes each AuditEvent's HMAC signature and reports any row whose stored
signature no longer matches its content — i.e. the row was altered after it was
written. Exits non-zero if any tampering (or an unsigned row) is found, so it can
gate CI / scheduled integrity checks.
"""
from django.core.management.base import BaseCommand

from patient_portal.models import AuditEvent


class Command(BaseCommand):
    help = "Verify the integrity (tamper-evidence) of the audit trail."

    def add_arguments(self, parser):
        parser.add_argument('--batch-size', type=int, default=2000,
                            help='Rows to scan per query (default 2000).')

    def handle(self, *args, **options):
        batch_size = options['batch_size']
        if batch_size < 1:
            batch_size = 2000

        total = 0
        tampered = []
        unsigned = 0
        qs = AuditEvent.objects.order_by('pk').iterator(chunk_size=batch_size)
        for row in qs:
            total += 1
            if not row.signature:
                unsigned += 1
                tampered.append(row.pk)
                continue
            if not row.signature_valid():
                tampered.append(row.pk)

        self.stdout.write(f"Scanned: {total} audit event(s)")
        self.stdout.write(f"Unsigned: {unsigned}")
        if tampered:
            self.stdout.write(self.style.ERROR(
                f"TAMPERING DETECTED: {len(tampered)} row(s) failed integrity check."
            ))
            preview = ', '.join(str(pk) for pk in tampered[:20])
            self.stdout.write(self.style.ERROR(f"Affected ids (first 20): {preview}"))
            raise SystemExit(1)
        self.stdout.write(self.style.SUCCESS(f"Integrity OK — all {total} row(s) verified."))
