"""
Show, field by field, what `backfill_patient_records` would change.

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

No values are printed, stored or re-derived: a row says which field of which
person_id would change and how -- set (empty -> value), cleared (value ->
empty; empty is null, "", [] or {}) or changed, plus up/down for a number (not
for a flag: its direction would be its value) -- and whether it held a pending
user edit. A custom_fields key with no CustomPatientField definition prints as
custom_fields.<undefined> (and --field takes that name). The refresh's own log lines are dropped while it
runs. No names either: rows carry person_id only. Needs the writable primary: see
"Before a backfill" in docs/DERIVATION_CHANGELOG.md.
"""

import csv
import json
import logging
from collections import Counter

from django.core.management.base import BaseCommand, CommandError
from django.db import DatabaseError

from omop_core.models import PatientRecord
from omop_core.services.patient_record_service import DERIVATION_VERSION
from omop_core.services.rederivation_preview import (
    UNDEFINED_CUSTOM_FIELD, custom_field_names, preview_record,
)

logger = logging.getLogger(__name__)


def _failure(person_id, exc):
    """What can be said about a failed preview without quoting the record.

    Not the message or the traceback: a constraint violation's message carries
    PostgreSQL's "Failing row contains (...)", which is every column of the
    patient's record. A database error is described by its SQLSTATE and
    constraint name only.
    """
    failure = {"person_id": person_id, "error": type(exc).__name__}
    cause = exc if isinstance(exc, DatabaseError) else None
    while cause is not None and not hasattr(cause, "sqlstate"):
        cause = cause.__cause__
    if cause is not None:
        if cause.sqlstate:
            failure["sqlstate"] = cause.sqlstate
        constraint = getattr(getattr(cause, "diag", None), "constraint_name", None)
        if constraint:
            failure["constraint"] = constraint
    return failure


def _known_fields():
    from omop_core.services.rederivation_preview import IGNORED_FIELDS

    return {field.name for field in PatientRecord._meta.concrete_fields} - IGNORED_FIELDS


def _describe(row):
    """change, e.g. "changed (up)", "cleared"."""
    return f"{row.change} ({row.direction})" if row.direction else row.change


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
                "defined custom field, custom_fields.<undefined> every key without a "
                "definition, custom_fields all of them."
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
        known = _known_fields()
        defined = custom_field_names()
        unknown = sorted(
            field for field in wanted
            if field not in known and field not in defined and field != UNDEFINED_CUSTOM_FIELD
        )
        if unknown:
            raise CommandError(
                f"Unknown field(s): {', '.join(unknown)}. Use a PatientRecord field name, "
                f"custom_fields.<key> of a defined custom field, or {UNDEFINED_CUSTOM_FIELD}."
            )
        max_rows = options["max_rows"]

        scanned = 0
        changed_records = 0
        per_field = Counter()
        per_kind = Counter()
        total_rows = 0
        rows = []
        errors = []
        for record in qs.only("pk", "person_id").iterator(chunk_size=options["batch_size"]):
            scanned += 1
            try:
                changes = preview_record(record, defined)
            except Exception as exc:
                failure = _failure(record.person_id, exc)
                errors.append(failure)
                # No exc_info: see _failure.
                logger.error(
                    "Preview failed for person_id=%s: %s",
                    record.person_id,
                    " ".join(f"{key}={value}" for key, value in failure.items() if key != "person_id"),
                )
                continue
            changes = [c for c in changes if self._selected(c.field, wanted)]
            if changes:
                changed_records += 1
            # Records, not rows: undefined custom keys share one name.
            per_field.update({change.field for change in changes})
            for change in changes:
                per_kind[change.change] += 1
                total_rows += 1
                if max_rows is None or len(rows) < max_rows:
                    rows.append(change)

        summary = {
            "cohort": label,
            "derivation_version": DERIVATION_VERSION,
            "records_scanned": scanned,
            "records_changed": changed_records,
            "changes": total_rows,
            "changes_shown": len(rows),
            "errors": len(errors),
            "field_filter": sorted(wanted),
            "fields": dict(sorted(per_field.items(), key=lambda item: (-item[1], item[0]))),
            "changes_by_kind": dict(sorted(per_kind.items())),
        }

        writer = {"text": self._write_text, "json": self._write_json, "csv": self._write_csv}
        writer[options["format"]](summary, rows, errors)

        if errors:
            raise CommandError(
                f"{len(errors)} record(s) could not be previewed; see the log for person_ids."
            )

    def _write_text(self, summary, rows, errors):
        scope = " in the selected fields" if summary["field_filter"] else ""
        out = self.stdout
        out.write(f"Preview of re-derivation to version {summary['derivation_version']} "
                  f"({summary['cohort']}). Nothing was written.")
        out.write(f"Records scanned: {summary['records_scanned']}")
        out.write(f"Records that would change{scope}: {summary['records_changed']}")
        out.write(f"Field changes{scope}: {summary['changes']}"
                  + "".join(f", {kind} {count}" for kind, count in summary["changes_by_kind"].items()))
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
                out.write(f"person_id={row.person_id} {row.field}: {_describe(row)}{mark}")
        if summary["changes_shown"] < summary["changes"]:
            # Outside the `if rows` block: --max-rows 0 and a filter that matches
            # nothing both suppress rows, and the count they suppressed must
            # still be visible.
            out.write(f"... {summary['changes'] - summary['changes_shown']} more row(s) not shown (--max-rows)")
        for error in errors:
            detail = " ".join(f"{key}={value}" for key, value in error.items() if key != "person_id")
            out.write(self.style.ERROR(f"person_id={error['person_id']} not previewed: {detail}"))

    def _write_json(self, summary, rows, errors):
        document = {
            "summary": summary,
            "changes": [
                {
                    "person_id": row.person_id,
                    "field": row.field,
                    "change": row.change,
                    "direction": row.direction,
                    "user_edited": row.user_edited,
                }
                for row in rows
            ],
            "errors": errors,
        }
        self.stdout.write(json.dumps(document, ensure_ascii=False, indent=2))

    def _write_csv(self, summary, rows, errors):
        writer = csv.writer(self.stdout, lineterminator="\n")
        writer.writerow(["person_id", "field", "change", "direction", "user_edited"])
        for row in rows:
            writer.writerow([
                row.person_id, row.field, row.change, row.direction or "",
                "true" if row.user_edited else "false",
            ])
        self.stderr.write(json.dumps({"summary": summary, "errors": errors}, ensure_ascii=False))
