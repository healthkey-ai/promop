"""Populate missing sample institutions without changing recorded care teams."""
import json
from collections import Counter

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from omop_core.models import PatientRecord, Person
from omop_core.services.sample_patient_stage import SAMPLE_ORG_DISEASES
from omop_core.services.treating_institutions import sample_institution


class Command(BaseCommand):
    help = 'Preview sample treating institutions; use --confirm to write PatientRecord and Person.'

    def add_arguments(self, parser):
        parser.add_argument('--confirm', action='store_true')
        parser.add_argument('--dry-run', action='store_true')
        parser.add_argument('--batch-size', type=int, default=100)

    def handle(self, *args, **options):
        batch_size = options['batch_size']
        if batch_size < 1:
            raise CommandError('Batch size must be positive.')
        dry = options['dry_run'] or not options['confirm']
        ids = list(PatientRecord.objects.filter(
            organization__slug__in=SAMPLE_ORG_DISEASES,
        ).order_by('pk').values_list('pk', flat=True))
        counts = Counter()
        for offset in range(0, len(ids), batch_size):
            with transaction.atomic():
                records = PatientRecord.objects.filter(pk__in=ids[offset:offset + batch_size]).only(
                    'pk', 'person_id', 'facility_name', 'region', 'user_edited_fields',
                )
                if not dry:
                    records = records.select_for_update()
                records = list(records)
                people = Person.objects.filter(pk__in=[r.person_id for r in records]).only('pk', 'facility_name').order_by('pk')
                if not dry:
                    people = people.select_for_update()
                people = {p.pk: p for p in people}
                record_updates, person_updates = [], []
                for record in records:
                    counts['processed'] += 1
                    person = people[record.person_id]
                    if 'facility_name' in (record.user_edited_fields or []):
                        counts['protected'] += 1
                        continue
                    recorded = (record.facility_name or '').strip()
                    source = (person.facility_name or '').strip()
                    if recorded and source and recorded != source:
                        counts['preserved_conflict'] += 1
                        continue
                    value = recorded or source or sample_institution(record.person_id, record.region or '')
                    if not recorded and not source:
                        counts['assigned'] += 1
                    if not recorded:
                        record.facility_name = value
                        record_updates.append(record)
                    if not source:
                        person.facility_name = value
                        person_updates.append(person)
                if not dry:
                    # Direct batch updates intentionally avoid a reverse derivation.
                    PatientRecord.objects.bulk_update(record_updates, ['facility_name'], batch_size=batch_size)
                    Person.objects.bulk_update(person_updates, ['facility_name'], batch_size=batch_size)
                counts['patient_record_updates'] += len(record_updates)
                counts['person_updates'] += len(person_updates)
        self.stdout.write(json.dumps(dict(sorted(counts.items()))))
        self.stdout.write('Preview only; use --confirm to apply.' if dry else 'Sample institutions populated.')
