"""
Verify audit-trail tamper-evidence (PHR-S FM TI.2.2.1).

Two checks per run:
  1. **Signature** — recompute each AuditEvent's content HMAC; a mismatch means the
     row was ALTERED after it was written.
  2. **Hash chain** — recompute each row's chain_hash from its predecessor's
     chain_hash + its own signature; a mismatch means a row was DELETED or INSERTED
     between two survivors. The earliest chained row is treated as the anchor (rows
     older than it may have been legitimately pruned by the retention job), and rows
     with an empty chain_hash (pre-chain / chaining disabled) are skipped for the
     chain check.

Exits non-zero if any signature or chain anomaly is found, so it can gate a
scheduled / CI integrity check.
"""
from django.core.management.base import BaseCommand

from patient_portal.models import AuditEvent


class Command(BaseCommand):
    help = "Verify the integrity (tamper-evidence + hash chain) of the audit trail."

    def add_arguments(self, parser):
        parser.add_argument('--batch-size', type=int, default=2000,
                            help='Rows to scan per query (default 2000).')

    def handle(self, *args, **options):
        batch_size = options['batch_size']
        if batch_size < 1:
            batch_size = 2000

        total = 0
        unsigned = 0
        signature_failures = []
        chain_breaks = []
        prev_chain = None  # chain_hash of the previous chained row (None = not yet anchored)

        qs = AuditEvent.objects.order_by('pk').iterator(chunk_size=batch_size)
        for row in qs:
            total += 1

            # 1. Content signature. An empty signature is a legacy/pre-signature row
            # (predates the feature) — reported, not a hard failure. A row that HAS a
            # signature which no longer validates is an alteration → failure.
            if not row.signature:
                unsigned += 1
            elif not row.signature_valid():
                signature_failures.append(row.pk)

            # 2. Hash chain (only for chained rows).
            if not row.chain_hash:
                continue
            if prev_chain is None:
                # First chained row = anchor; its predecessor may be legitimately pruned.
                prev_chain = row.chain_hash
                continue
            if row.chain_hash != row.compute_chain_hash(prev_chain):
                chain_breaks.append(row.pk)  # a row was deleted or inserted before this one
            prev_chain = row.chain_hash

        self.stdout.write(f"Scanned: {total} audit event(s)")
        self.stdout.write(f"Unsigned: {unsigned}")
        self.stdout.write(f"Signature failures: {len(signature_failures)}")
        self.stdout.write(f"Chain breaks: {len(chain_breaks)}")

        if signature_failures or chain_breaks:
            if signature_failures:
                self.stdout.write(self.style.ERROR(
                    f"ALTERED rows ({len(signature_failures)}): "
                    f"{', '.join(str(p) for p in signature_failures[:20])}"))
            if chain_breaks:
                self.stdout.write(self.style.ERROR(
                    f"CHAIN BREAKS ({len(chain_breaks)}) — deletion/insertion before ids: "
                    f"{', '.join(str(p) for p in chain_breaks[:20])}"))
            raise SystemExit(1)
        self.stdout.write(self.style.SUCCESS(f"Integrity OK — all {total} row(s) verified."))
