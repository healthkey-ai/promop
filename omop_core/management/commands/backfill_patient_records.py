"""
Re-derive PatientRecord rows whose derivation_version is stale.

Usage:
    # Re-derive only records older than the current DERIVATION_VERSION
    python manage.py backfill_patient_records

    # Re-derive records older than a specific version
    python manage.py backfill_patient_records --target-version 3

    # Re-derive every record regardless of version
    python manage.py backfill_patient_records --all

    # Preview without modifying
    python manage.py backfill_patient_records --dry-run

    # Control batch size (default 100)
    python manage.py backfill_patient_records --batch-size 50
"""

import logging
import json
import os

from django.core.management.base import BaseCommand, CommandError

from omop_core.models import PatientRecord
from omop_core.services.patient_record_service import (
    DERIVATION_VERSION,
    refresh_patient_record,
)

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Re-derive PatientRecord rows whose derivation_version is below the current version."

    def add_arguments(self, parser):
        plans = parser.add_mutually_exclusive_group()
        plans.add_argument('--plan', metavar='PRIVATE_FILE', help='Preview an explicit, bounded person scope into a new private signed file.')
        plans.add_argument('--apply-plan', metavar='PRIVATE_FILE', help='Apply a reviewed plan, resuming previously completed records.')
        plans.add_argument('--rollback-plan', metavar='PRIVATE_FILE', help='Restore applied read-model changes if no later edits occurred.')
        parser.add_argument('--person-id', type=int, action='append', default=[],
                            help='Select a person for --plan; repeat for up to 100 people.')
        parser.add_argument(
            "--target-version",
            type=int,
            default=None,
            help=(
                "Target version — re-derive records with derivation_version < this value. "
                f"Defaults to current DERIVATION_VERSION ({DERIVATION_VERSION})."
            ),
        )
        parser.add_argument(
            "--all",
            action="store_true",
            dest="backfill_all",
            help="Re-derive every record regardless of its current version.",
        )
        parser.add_argument(
            "--batch-size",
            type=int,
            default=100,
            help="Number of records to process per batch (default 100).",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report how many records would be re-derived without modifying any.",
        )
        parser.add_argument(
            "--organization",
            help="Limit the backfill to one organization slug.",
        )

    def handle(self, **options):
        if options['plan'] or options['apply_plan'] or options['rollback_plan']:
            return self._handle_plan(options)
        if options['person_id']:
            raise CommandError('--person-id requires --plan.')
        target = options["target_version"] if options["target_version"] is not None else DERIVATION_VERSION
        backfill_all = options["backfill_all"]
        batch_size = options["batch_size"]
        dry_run = options["dry_run"]
        organization = options["organization"]

        if backfill_all:
            qs = PatientRecord.objects.select_related("person").all()
            label = "all"
        else:
            qs = PatientRecord.objects.select_related("person").filter(
                derivation_version__lt=target,
            )
            label = f"derivation_version < {target}"

        if organization:
            qs = qs.filter(organization__slug=organization)
            label += f" in organization {organization}"

        total = qs.count()
        if total == 0:
            self.stdout.write(self.style.SUCCESS("No stale records found."))
            return

        if dry_run:
            self.stdout.write(
                self.style.WARNING(
                    f"[dry-run] {total} record(s) matching {label} would be re-derived."
                )
            )
            return

        self.stdout.write(f"Re-deriving {total} record(s) matching {label} ...")

        success = 0
        errors = 0
        # Use iterator to avoid loading all records into memory at once.
        for record in qs.iterator(chunk_size=batch_size):
            try:
                refresh_patient_record(record.person)
                success += 1
                if success % batch_size == 0:
                    self.stdout.write(f"  ... {success}/{total}")
            except Exception:
                errors += 1
                logger.exception(
                    "Failed to refresh PatientRecord for person_id=%s",
                    record.person_id,
                )

        self.stdout.write(
            self.style.SUCCESS(
                f"Done. {success} re-derived, {errors} error(s)."
            )
        )

    def _handle_plan(self, options):
        from omop_core.services.projection_refresh_plan import (
            PlanConflict, apply_plan, create_plan, summarize,
        )
        if (options['backfill_all'] or options['organization'] or options['dry_run']
                or options['target_version'] is not None):
            raise CommandError('Plan operations use their explicit person scope; do not combine legacy backfill filters.')
        if not options['plan'] and options['person_id']:
            raise CommandError('Apply and rollback use the scope in the signed plan.')
        try:
            if options['plan']:
                plan = create_plan(options['person_id'])
                # Exclusive creation prevents overwriting another plan; mode
                # 0600 prevents exposing exact before/after clinical values.
                fd = os.open(options['plan'], os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, 'w') as output:
                    json.dump(plan, output, indent=2, sort_keys=True)
                    output.write('\n')
                self.stdout.write(json.dumps({'preview': summarize(plan)}, sort_keys=True))
            else:
                path = options['apply_plan'] or options['rollback_plan']
                with open(path) as source:
                    plan = json.load(source)
                result = apply_plan(plan, rollback=bool(options['rollback_plan']))
                self.stdout.write(json.dumps(result, sort_keys=True))
        except PlanConflict as error:
            raise CommandError(str(error)) from None
        except (OSError, ValueError, KeyError, TypeError):
            raise CommandError('The private plan could not be read or written. Check its path and format; existing files are never overwritten.') from None
        except Exception:
            # Driver errors can contain clinical values; preserve the original
            # transaction rollback without copying exception text to logs.
            raise CommandError('Refresh plan failed. Completed records remain audited; the current record was rolled back.') from None
