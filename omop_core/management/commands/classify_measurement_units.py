"""Decide who owns the unit of measurements written before #2104.

``Measurement.units_as_sent`` says whether unit normalization may rewrite a
row: ``False`` lets repoint and resolve convert it to the canonical unit,
``True`` keeps it as its writer sent it. Rows written before the column existed
hold ``None``: nothing recorded who wrote them, and provenance cannot tell
(``EHR_SYNC`` is used by both the ETL and promop's FHIR sync, and the ETL's
headers are optional). Normalization leaves ``None`` rows as stored, the
reversible choice: an unconverted value still carries its source unit, while a
wrongly converted one has lost the value the writer matches on.

This command makes the decision explicit for a chosen set of those rows. It
only touches ``None`` rows, needs at least one filter, and is a dry run unless
``--apply`` is given.

    # Lab-results uploads are promop's own ingest: let it convert them.
    manage.py classify_measurement_units --as convertible --lab-uploads --apply

    # One patient known to be loaded by the ETL.
    manage.py classify_measurement_units --as as-sent --person 16813 --apply
"""

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.db.models import Exists, OuterRef

from omop_core.models import Measurement, MeasurementOwnership

BATCH = 10_000


def unknown_rows(*, lab_uploads=False, person_ids=None):
    """``None`` rows matching every given filter, by primary key."""
    qs = Measurement.objects.filter(units_as_sent__isnull=True)
    if lab_uploads:
        qs = qs.filter(Exists(MeasurementOwnership.objects.filter(
            measurement_id=OuterRef('measurement_id'))))
    if person_ids:
        qs = qs.filter(person_id__in=person_ids)
    return qs.order_by('measurement_id')


def classify(qs, value):
    """Set ``units_as_sent`` on *qs* in batches; returns the rows written."""
    written = 0
    last_pk = None
    while True:
        batch = qs if last_pk is None else qs.filter(measurement_id__gt=last_pk)
        pks = list(batch.values_list('measurement_id', flat=True)[:BATCH])
        if not pks:
            return written
        with transaction.atomic():
            written += Measurement.objects.filter(
                measurement_id__in=pks, units_as_sent__isnull=True,
            ).update(units_as_sent=value)
        last_pk = pks[-1]


class Command(BaseCommand):
    help = ('Set units_as_sent on measurements written before it was recorded '
            '(dry run unless --apply)')

    def add_arguments(self, parser):
        parser.add_argument(
            '--as', dest='as_', required=True, choices=['convertible', 'as-sent'],
            help='convertible: normalization may convert them; as-sent: never')
        parser.add_argument(
            '--lab-uploads', action='store_true',
            help='only rows a lab-results upload created (measurement_ownership)')
        parser.add_argument(
            '--person', type=int, action='append', dest='person_ids',
            help='only this person; repeatable')
        parser.add_argument('--apply', action='store_true', help='write; default is a dry run')

    def handle(self, *args, **opts):
        if not (opts['lab_uploads'] or opts['person_ids']):
            raise CommandError('Give at least one filter: --lab-uploads or --person.')
        qs = unknown_rows(lab_uploads=opts['lab_uploads'], person_ids=opts['person_ids'])
        value = opts['as_'] == 'as-sent'
        if not opts['apply']:
            self.stdout.write(f'Would set units_as_sent={value} on {qs.count()} row(s). '
                              'Re-run with --apply to write.')
            return
        written = classify(qs, value)
        self.stdout.write(self.style.SUCCESS(f'Set units_as_sent={value} on {written} row(s).'))
