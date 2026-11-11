"""
Show, value by value, what `backfill_patient_records` would change.

Selects the same records as the backfill and runs the same refresh for each,
then rolls it back: nothing is written, no task is queued, nothing is
projected. See omop_core/services/rederivation_preview.py for how that is
enforced.

Usage:
    # Records the backfill would re-derive (derivation_version < current)
    python manage.py preview_rederivation

    # Same cohort options as the backfill
    python manage.py preview_rederivation --target-version 27 --organization acme
    python manage.py preview_rederivation --all

    # Narrow it down
    python manage.py preview_rederivation --person-id 101 --person-id 102
    python manage.py preview_rederivation --limit 500 --field disease --field stage

    # Machine-readable; --max-rows caps the change rows, not the counts
    python manage.py preview_rederivation --format json --max-rows 1000
    python manage.py preview_rederivation --format csv > changes.csv

Values of direct identifiers (email, phone, date of birth, address-level
location, facility, validator) are printed as [redacted]; that they change is
still reported. No names are printed: rows carry person_id only.
"""

import csv
import json
import logging
from collections import Counter

from django.core.management.base import BaseCommand, CommandError

from omop_core.models import PatientRecord
from omop_core.services.patient_record_service import DERIVATION_VERSION
from omop_core.services.rederivation_preview import preview_record

logger = logging.getLogger(__name__)


def _jsonable(value):
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    # Decimal, date, datetime: their string form is exact.
    return str(value)


def _text(value):
    """One unambiguous token: null, "" and "None" stay distinct."""
    return json.dumps(_jsonable(value), ensure_ascii=False, sort_keys=True)


class Command(BaseCommand):
    help = (
        "Report, per field, what a re-derivation would change in stored "
        "PatientRecords, without writing anything."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--target-version",
            type=int,
            default=None,
            help=(
                "Preview records with derivation_version < this value. "
                f"Defaults to current DERIVATION_VERSION ({DERIVATION_VERSION})."
            ),
        )
        parser.add_argument(
            "--all",
            action="store_true",
            dest="preview_all",
            help="Preview every record regardless of its current version.",
        )
        parser.add_argument(
            "--organization",
            help="Limit to one organization slug.",
        )
        parser.add_argument(
            "--person-id",
            type=int,
            action="append",
            dest="person_ids",
            default=None,
            help="Limit to this person_id (repeatable). Still subject to the version filter unless --all.",
        )
        parser.add_argument(
            "--limit",
            type=int,
            default=None,
            help="Scan at most this many records (lowest person_id first).",
        )
        parser.add_argument(
            "--batch-size",
            type=int,
            default=100,
            help="Records fetched per database round trip (default 100).",
        )
        parser.add_argument(
            "--field",
            action="append",
            dest="fields",
            default=None,
            help=(
                "Report only this field (repeatable). custom_fields.<key> names one "
                "custom field; custom_fields names all of them."
            ),
        )
        parser.add_argument(
            "--max-rows",
            type=int,
            default=None,
            help="Print at most this many change rows. The summary still counts all of them.",
        )
        parser.add_argument(
            "--format",
            choices=("text", "json", "csv"),
            default="text",
            help="text (default), json (summary and rows in one document) or csv (rows; summary on stderr).",
        )

    def _cohort(self, options):
        target = options["target_version"] if options["target_version"] is not None else DERIVATION_VERSION
        qs = PatientRecord.objects.all()
        if options["preview_all"]:
            label = "all"
        else:
            qs = qs.filter(derivation_version__lt=target)
            label = f"derivation_version < {target}"
        if options["organization"]:
            qs = qs.filter(organization__slug=options["organization"])
            label += f" in organization {options['organization']}"
        if options["person_ids"]:
            qs = qs.filter(person_id__in=options["person_ids"])
            label += f" for {len(set(options['person_ids']))} person_id(s)"
        qs = qs.order_by("person_id")
        if options["limit"] is not None:
            qs = qs[:options["limit"]]
            label += f", first {options['limit']}"
        return qs, label

    @staticmethod
    def _selected(field, wanted):
        if not wanted:
            return True
        return field in wanted or field.split(".", 1)[0] in wanted

    def handle(self, **options):
        for name in ("limit", "max_rows"):
            if options[name] is not None and options[name] < 0:
                raise CommandError(f"--{name.replace('_', '-')} must not be negative")
        if options["batch_size"] < 1:
            raise CommandError("--batch-size must be at least 1")

        qs, label = self._cohort(options)
        wanted = set(options["fields"] or ())

        scanned = 0
        changed_records = 0
        per_field = Counter()
        rows = []
        errors = []
        for record in qs.only("pk", "person_id").iterator(chunk_size=options["batch_size"]):
            scanned += 1
            try:
                changes = preview_record(record)
            except Exception as exc:
                errors.append({"person_id": record.person_id, "error": type(exc).__name__})
                logger.exception("Preview failed for person_id=%s", record.person_id)
                continue
            changes = [c for c in changes if self._selected(c.field, wanted)]
            if changes:
                changed_records += 1
            for change in changes:
                per_field[change.field] += 1
                rows.append(change)

        total_rows = len(rows)
        if options["max_rows"] is not None:
            rows = rows[:options["max_rows"]]

        summary = {
            "cohort": label,
            "derivation_version": DERIVATION_VERSION,
            "records_scanned": scanned,
            "records_changed": changed_records,
            "changes": total_rows,
            "changes_shown": len(rows),
            "errors": len(errors),
            "fields": dict(sorted(per_field.items(), key=lambda item: (-item[1], item[0]))),
        }

        writer = {"text": self._write_text, "json": self._write_json, "csv": self._write_csv}
        writer[options["format"]](summary, rows, errors)

        if errors:
            raise CommandError(
                f"{len(errors)} record(s) could not be previewed; see the log for person_ids."
            )

    def _write_text(self, summary, rows, errors):
        out = self.stdout
        out.write(f"Preview of re-derivation to version {summary['derivation_version']} "
                  f"({summary['cohort']}). Nothing was written.")
        out.write(f"Records scanned: {summary['records_scanned']}")
        out.write(f"Records that would change: {summary['records_changed']}")
        out.write(f"Field changes: {summary['changes']}")
        if summary["errors"]:
            out.write(self.style.ERROR(f"Errors: {summary['errors']}"))
        if summary["fields"]:
            out.write("")
            out.write("Records changed per field:")
            width = max(len(field) for field in summary["fields"])
            for field, count in summary["fields"].items():
                out.write(f"  {field.ljust(width)}  {count}")
        if rows:
            out.write("")
            for row in rows:
                mark = "  [user-edited]" if row.user_edited else ""
                out.write(f"person_id={row.person_id} {row.field}: "
                          f"{_text(row.stored)} -> {_text(row.rederived)}{mark}")
            if summary["changes_shown"] < summary["changes"]:
                out.write(f"... {summary['changes'] - summary['changes_shown']} more row(s) not shown (--max-rows)")
        for error in errors:
            out.write(self.style.ERROR(f"person_id={error['person_id']} not previewed: {error['error']}"))

    def _write_json(self, summary, rows, errors):
        document = {
            "summary": summary,
            "changes": [
                {
                    "person_id": row.person_id,
                    "field": row.field,
                    "stored": _jsonable(row.stored),
                    "rederived": _jsonable(row.rederived),
                    "user_edited": row.user_edited,
                    "redacted": row.redacted,
                }
                for row in rows
            ],
            "errors": errors,
        }
        self.stdout.write(json.dumps(document, ensure_ascii=False, indent=2))

    def _write_csv(self, summary, rows, errors):
        writer = csv.writer(self.stdout, lineterminator="\n")
        writer.writerow(["person_id", "field", "stored", "rederived", "user_edited", "redacted"])
        for row in rows:
            writer.writerow([
                row.person_id, row.field, _text(row.stored), _text(row.rederived),
                "true" if row.user_edited else "false",
                "true" if row.redacted else "false",
            ])
        self.stderr.write(json.dumps({"summary": summary, "errors": errors}, ensure_ascii=False))
