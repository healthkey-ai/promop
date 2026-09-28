"""Preview/apply only the approved TP53 aggregate from current OMOP findings."""
import json

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from omop_core.models import PatientRecord
from omop_core.services.genomics import list_variants
from omop_core.services.patient_record_service import tp53_disruption_from_findings


def state(value):
    return 'true' if value is True else 'false' if value is False else 'null'


class Command(BaseCommand):
    help = 'Reconcile only TP53 from source findings; preview by default, pending edits are held.'

    def add_arguments(self, parser):
        parser.add_argument('--apply', action='store_true')
        scope = parser.add_mutually_exclusive_group(required=True)
        scope.add_argument('--organization')
        scope.add_argument('--person-id', type=int)
        scope.add_argument('--all', action='store_true', dest='all_records')

    def handle(self, *args, **options):
        qs = PatientRecord.objects.order_by('pk')
        if options['organization']:
            qs = qs.filter(organization__slug=options['organization'])
        elif options['person_id'] is not None:
            qs = qs.filter(person_id=options['person_id'])
        report = {'report_version': 1, 'rule_version': 8,
                  'captured_at': timezone.now().isoformat(),
                  'mode': 'apply' if options['apply'] else 'preview',
                  'processed': 0, 'held_pending_edits': 0, 'changed': 0,
                  'before': dict.fromkeys(('true', 'false', 'null'), 0),
                  'after': dict.fromkeys(('true', 'false', 'null'), 0),
                  'transitions': {}, 'complete': False}
        try:
            for pk in qs.values_list('pk', flat=True).iterator(chunk_size=100):
                with transaction.atomic():
                    current = PatientRecord.objects.select_related('person')
                    if options['apply']:
                        # The writer and full refresh take this same row lock.
                        current = current.select_for_update(of=('self',))
                    record = current.get(pk=pk)
                    before = state(record.tp53_disruption)
                    pending = set(record.user_edited_fields or [])
                    held = bool(pending & {'tp53_disruption', 'genetic_mutations'}
                                or any(field.startswith('genomics_') for field in pending))
                    value = record.tp53_disruption if held else tp53_disruption_from_findings(list_variants(record.person))
                    after = state(value)
                    changed = before != after
                    if changed and options['apply']:
                        # Do not claim a full version-8 refresh or trigger unrelated
                        # derivations, dates, source writes or pending-edit changes.
                        PatientRecord.objects.filter(pk=pk).update(tp53_disruption=value)
                        # That deliberate `update()` is also invisible to post_save,
                        # so the webhook receiver never sees this write and a
                        # subscriber would keep a stale tp53_disruption until some
                        # unrelated save happened to touch the row. Publish the same
                        # event the receiver would have, inside this transaction, so
                        # the outbox row commits with the value it describes. Held
                        # and preview rows change nothing and stay silent.
                        from patient_portal.webhooks import publish_event
                        if record.organization_id is not None:
                            publish_event(record.organization_id, 'patient.changed', {
                                'person_id': record.person_id,
                                'resource_id': str(pk),
                                'resource_type': 'omop_core.patientrecord',
                                'operation': 'saved',
                            })
                report['processed'] += 1
                report['held_pending_edits'] += int(held)
                report['changed'] += int(changed)
                report['before'][before] += 1
                report['after'][after] += 1
                transition = before + '->' + after
                report['transitions'][transition] = report['transitions'].get(transition, 0) + 1
            report['complete'] = True
        except Exception as exc:
            # Earlier rows may have committed; output their counts so retries
            # and partial execution are explicit without exposing patient data.
            report['error_type'] = type(exc).__name__
            self.stdout.write(json.dumps(report, sort_keys=True))
            raise CommandError('TP53 reconciliation stopped; review the aggregate receipt before retrying.') from None
        self.stdout.write(json.dumps(report, sort_keys=True))
