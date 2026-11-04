"""Read-only audit of explicit negative answers that now contradict their source.

`no_active_infection_status` and `no_other_active_malignancies` can be confirmed
explicitly (#5666). While the attestation exists it wins over the source-fact
derivation, so a later recorded active infection / second active malignancy must
be surfaced for review rather than silently overwriting the answer.

This command reports; it never writes. Repair is an explicit, reviewed action.
"""
from django.core.management.base import BaseCommand

from omop_core.models import ExplicitNegativeAnswer, PatientRecord


class Command(BaseCommand):
    help = 'Report explicit negative confirmations whose source facts now contradict them.'

    def add_arguments(self, parser):
        parser.add_argument('--limit', type=int, default=20, help='Sample rows to print.')

    def handle(self, *args, **options):
        rows = (
            ExplicitNegativeAnswer.objects
            .filter(answer=True)
            .values_list('person_id', 'field')
            .order_by('person_id', 'field')
        )
        conflicts = []
        checked = 0
        for person_id, field in rows.iterator():
            checked += 1
            record = PatientRecord.objects.filter(person_id=person_id).only(
                'active_infection_status', 'active_malignancies',
            ).first()
            if record is None:
                conflicts.append((person_id, field, 'missing patient record'))
            elif field == 'no_active_infection_status' and record.active_infection_status is True:
                conflicts.append((person_id, field, 'active infection is recorded'))
            elif (field == 'no_other_active_malignancies'
                  and len(record.active_malignancies or []) > 1):
                conflicts.append((person_id, field, 'more than one active malignancy is recorded'))

        self.stdout.write('explicit negative answers (read-only audit)')
        if not conflicts:
            self.stdout.write(f'  none ({checked} confirmation(s) checked)')
            return
        self.stdout.write(f'  {len(conflicts)} conflict(s) out of {checked} confirmation(s):')
        for person_id, field, reason in conflicts[:options['limit']]:
            self.stdout.write(f'  person={person_id} {field}: {reason}')
        self.stdout.write('No rows were changed. Repair requires separate authorization.')
