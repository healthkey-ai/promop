"""
Management command: audit_sct_history

Audits all stem_cell_transplant_history values in the database against the
mapping used by migration 0086. Run this against the target database BEFORE
applying the migration to confirm there are no unrecognized values that would
be preserved as-is (or non-string items from the old BQ loader).

Usage:
    # Staging DB
    DATABASE_URL="postgresql://..." python manage.py audit_sct_history

    # Production DB (read-only check — no writes)
    DATABASE_URL="postgresql://..." python manage.py audit_sct_history
"""
from collections import Counter

from django.core.management.base import BaseCommand

from omop_core.models import PatientInfo

# Must stay in sync with _OLD_TO_NEW_SCT in migration 0086.
_OLD_TO_NEW_SCT = {
    'prior SCT':                    'autologous SCT',
    'prior autologous SCT':         'autologous SCT',
    'prior allogeneic SCT':         'allogeneic SCT',
    'recent SCT':                   'autologous SCT',
    'recent autologous SCT':        'autologous SCT',
    'recent allogeneic SCT':        'allogeneic SCT',
    'relapsed post-SCT':            'autologous SCT',
    'relapsed post-autologous SCT': 'autologous SCT',
    'relapsed post-allogeneic SCT': 'allogeneic SCT',
    'completed tandem SCT':         'tandem SCT',
    'never received SCT':           None,   # intentionally cleared
    'pre-autologous SCT':           'autologous SCT',
    'pre-allogeneic SCT':           'allogeneic SCT',
}

_NEW_VOCAB = {'autologous SCT', 'allogeneic SCT', 'tandem SCT'}


class Command(BaseCommand):
    help = (
        'Read-only audit of stem_cell_transplant_history values against the '
        'migration 0086 mapping. Run before applying the migration to production.'
    )

    def handle(self, *args, **options):
        qs = PatientInfo.objects.exclude(
            stem_cell_transplant_history=[]
        ).exclude(
            stem_cell_transplant_history__isnull=True
        )

        total_rows = qs.count()
        if total_rows == 0:
            self.stdout.write('No PatientInfo rows with non-empty SCT history found.')
            return

        self.stdout.write(f'Scanning {total_rows} PatientInfo rows...\n')

        value_counter: Counter = Counter()
        for pi in qs.iterator():
            for v in (pi.stem_cell_transplant_history or []):
                if isinstance(v, str):
                    value_counter[v] += 1
                else:
                    value_counter[f'<non-string:{type(v).__name__}>'] += 1

        will_remap: dict = {}
        will_clear: dict = {}
        already_new: dict = {}
        unrecognized: dict = {}

        for v, count in value_counter.items():
            if v in _NEW_VOCAB:
                already_new[v] = count
            elif v in _OLD_TO_NEW_SCT:
                target = _OLD_TO_NEW_SCT[v]
                if target is None:
                    will_clear[v] = count
                else:
                    will_remap[v] = (target, count)
            else:
                unrecognized[v] = count

        W = 52  # column width for value display

        self.stdout.write('=== WILL BE REMAPPED ===')
        if will_remap:
            for v, (target, count) in sorted(will_remap.items()):
                self.stdout.write(f'  {v!r:{W}s} → {target!r}  ({count}x)')
        else:
            self.stdout.write('  (none)')

        self.stdout.write('\n=== WILL BE CLEARED (maps to None) ===')
        if will_clear:
            for v, count in sorted(will_clear.items()):
                self.stdout.write(f'  {v!r:{W}s} → cleared  ({count}x)')
        else:
            self.stdout.write('  (none)')

        self.stdout.write('\n=== ALREADY IN NEW VOCABULARY (pass through unchanged) ===')
        if already_new:
            for v, count in sorted(already_new.items()):
                self.stdout.write(f'  {v!r:{W}s}  ({count}x)')
        else:
            self.stdout.write('  (none)')

        self.stdout.write('\n=== UNRECOGNIZED (will be PRESERVED as-is by migration) ===')
        if unrecognized:
            self.stdout.write(self.style.WARNING(
                f'  {len(unrecognized)} unrecognized value(s) — '
                'migration will keep these unchanged rather than drop them.\n'
                '  Add each to _OLD_TO_NEW_SCT in migration 0086 if a mapping is known,'
                ' or accept that they will remain as-is.'
            ))
            for v, count in sorted(unrecognized.items()):
                self.stdout.write(self.style.WARNING(f'  {v!r:{W}s}  ({count}x)'))
        else:
            self.stdout.write(self.style.SUCCESS('  (none)'))

        self.stdout.write(
            f'\nSummary: {total_rows} rows | '
            f'{len(will_remap)} remap | {len(will_clear)} clear | '
            f'{len(already_new)} pass-through | {len(unrecognized)} unrecognized'
        )

        if unrecognized:
            self.stdout.write(self.style.WARNING(
                '\nACTION REQUIRED before running migration 0086 on this database.'
            ))
            raise SystemExit(1)
        else:
            self.stdout.write(self.style.SUCCESS(
                '\nAll values recognized. Safe to apply migration 0086.'
            ))
