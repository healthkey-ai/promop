"""
Django management command: load_from_healthtree_bq
====================================================
Reads FHIR patient data from the HealthTree BigQuery warehouse
(the same source as the ht-analytics-dbt project) and populates:

  * omop_core.Person        — core OMOP demographics
  * omop_core.PatientInfo   — extended denormalised patient info including:
      - ai_lines_of_therapy   (from core__ai_lines_of_therapy)
      - survey_responses      (from core__survey_responses)

BigQuery source
---------------
GCP project  : set via env var HT_BQ_PROJECT   (default: ht-analytics-486920)
Core dataset : set via env var HT_BQ_DATASET    (default: core)
Auth         : Application Default Credentials or a service-account key file
               set via env var GOOGLE_APPLICATION_CREDENTIALS

Tables read
-----------
  <project>.<dataset>.core__patients
  <project>.<dataset>.core__ai_lines_of_therapy
  <project>.<dataset>.core__survey_responses

Usage
-----
  # All patients (default batch size 500)
  python manage.py load_from_healthtree_bq

  # Single patient by HealthTree user_id
  python manage.py load_from_healthtree_bq --user-id abc123

  # Custom project / dataset
  python manage.py load_from_healthtree_bq \\
      --bq-project ht-analytics-486920 \\
      --bq-dataset core

  # Use a service-account key file
  python manage.py load_from_healthtree_bq \\
      --keyfile /path/to/service-account.json

  # Dry-run (query BQ but don't write to Django DB)
  python manage.py load_from_healthtree_bq --dry-run

  # Force-update existing PatientInfo records
  python manage.py load_from_healthtree_bq --force-update

Environment variables
---------------------
  HT_BQ_PROJECT              BigQuery GCP project id
  HT_BQ_DATASET              BigQuery dataset containing the core__ tables
  GOOGLE_APPLICATION_CREDENTIALS  Path to service-account JSON key
"""

import json
import os
import hashlib
from datetime import date, datetime, timezone as dt_timezone
from decimal import Decimal, InvalidOperation
from typing import Any

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction, IntegrityError
from django.utils import timezone

from omop_core.models import Concept, Location, PatientInfo, Person

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_BQ_PROJECT = "ht-analytics-486920"
DEFAULT_BQ_DATASET = "core"

# OMOP concept IDs for gender (standard OMOP vocabulary)
GENDER_CONCEPT_MAP = {
    "male": 8507,
    "m": 8507,
    "female": 8532,
    "f": 8532,
}

# OMOP concept IDs for race (standard OMOP vocabulary)
RACE_CONCEPT_MAP = {
    "white": 8527,
    "black or african american": 8516,
    "asian": 8515,
    "american indian or alaska native": 8657,
    "native hawaiian or other pacific islander": 8557,
    "other": 8522,
}

# OMOP concept ID for unknown (0 means no concept)
UNKNOWN_CONCEPT_ID = 0

# Batch size for Django bulk_create / update
BATCH_SIZE = 500


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _stable_person_id(user_id: str) -> int:
    """
    Generate a stable integer person_id from a HealthTree user_id string.
    Uses the first 9 decimal digits of the MD5 hash so it fits in a 32-bit int.
    """
    digest = hashlib.md5(user_id.encode()).hexdigest()
    return int(digest[:9], 16) % 2_000_000_000  # keep under 2^31


def _safe_date(value: Any) -> date | None:
    """Convert a BQ date/datetime/string to a Python date, or None."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except (ValueError, TypeError):
        return None


def _safe_decimal(value: Any) -> Decimal | None:
    """Convert a BQ numeric/float/string to Decimal, or None."""
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except InvalidOperation:
        return None


def _safe_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _infer_sct_type(procedures: Any) -> str:
    """Map a free-text BQ procedures value to one of the 3 SCT vocabulary strings.

    Matches keywords case-insensitively; defaults to 'autologous SCT' (the most
    common transplant type in MM) when the string is absent or unrecognized.
    """
    s = str(procedures).lower() if procedures else ''
    if 'allogeneic' in s or 'allo' in s:
        return 'allogeneic SCT'
    if 'tandem' in s:
        return 'tandem SCT'
    return 'autologous SCT'


def _serialize_row(row) -> dict:
    """
    Convert a BigQuery Row to a plain JSON-serialisable dict.
    Converts date/datetime objects to ISO strings so they can be stored
    in a JSONField.
    """
    result = {}
    for key in row.keys():
        val = row[key]
        if isinstance(val, datetime):
            result[key] = val.isoformat()
        elif isinstance(val, date):
            result[key] = val.isoformat()
        elif isinstance(val, Decimal):
            result[key] = float(val)
        else:
            result[key] = val
    return result


def _get_or_create_concept(concept_id: int) -> Concept | None:
    """Return a Concept ORM object, creating a placeholder if not present."""
    if not concept_id:
        return None
    obj, _ = Concept.objects.get_or_create(
        concept_id=concept_id,
        defaults={
            "concept_name": f"Concept {concept_id}",
            "concept_code": str(concept_id),
            "standard_concept": "S",
            "valid_start_date": date(1970, 1, 1),
            "valid_end_date": date(2099, 12, 31),
            # domain / vocabulary / concept_class are FK — use get_or_create stubs
            "domain_id": _get_or_create_domain("Unknown"),
            "vocabulary_id": _get_or_create_vocabulary("None"),
            "concept_class_id": _get_or_create_concept_class("Unknown"),
        },
    )
    return obj


def _get_or_create_domain(domain_id: str):
    from omop_core.models import Domain
    obj, _ = Domain.objects.get_or_create(
        domain_id=domain_id,
        defaults={"domain_name": domain_id, "domain_concept_id": 0},
    )
    return obj.domain_id


def _get_or_create_vocabulary(vocab_id: str):
    from omop_core.models import Vocabulary
    obj, _ = Vocabulary.objects.get_or_create(
        vocabulary_id=vocab_id,
        defaults={
            "vocabulary_name": vocab_id,
            "vocabulary_concept_id": 0,
        },
    )
    return obj.vocabulary_id


def _get_or_create_concept_class(cc_id: str):
    from omop_core.models import ConceptClass
    obj, _ = ConceptClass.objects.get_or_create(
        concept_class_id=cc_id,
        defaults={"concept_class_name": cc_id, "concept_class_concept_id": 0},
    )
    return obj.concept_class_id


# ---------------------------------------------------------------------------
# BigQuery query builders
# ---------------------------------------------------------------------------

def _q_patients(project: str, dataset: str, user_id: str | None) -> str:
    where = f"WHERE user_id = '{user_id}'" if user_id else ""
    return f"""
        SELECT
            user_id,
            person_id,
            first_name,
            middle_name,
            last_name,
            gender,
            date_of_birth,
            marital_status,
            race,
            ethnicity,
            email,
            phone,
            city,
            state,
            postal_code,
            median_income,
            created_at,
            updated_at
        FROM `{project}.{dataset}.core__patients`
        {where}
        QUALIFY ROW_NUMBER() OVER (
            PARTITION BY user_id
            ORDER BY updated_at DESC
        ) = 1
    """


def _q_lot(project: str, dataset: str, user_id: str | None) -> str:
    where = f"WHERE user_id = '{user_id}'" if user_id else ""
    return f"""
        SELECT
            ai_lines_of_therapy_summary_id,
            user_id,
            line_number,
            disease,
            outcome,
            start_date,
            end_date,
            is_ongoing_line_of_therapy,
            active_ingredients,
            active_ingredients_induction,
            active_ingredients_maintenance,
            has_bispecifics,
            line_has_procedures,
            procedures,
            has_transplant,
            has_cart,
            is_clinical_trial,
            clinical_trial_identifier,
            censoring_date,
            notes,
            is_validated,
            prompt_version,
            created_at,
            updated_at
        FROM `{project}.{dataset}.core__ai_lines_of_therapy`
        {where}
        ORDER BY user_id, line_number
    """


def _q_surveys(project: str, dataset: str, user_id: str | None) -> str:
    where = f"WHERE user_id = '{user_id}'" if user_id else ""
    return f"""
        SELECT
            response_id,
            user_id,
            survey_id,
            question_id,
            survey_name,
            survey_title,
            survey_type,
            survey_status,
            survey_frequency,
            survey_disease,
            irb_number,
            question_label,
            question_type,
            question_options,
            is_required,
            page_position,
            global_question_position,
            is_answered,
            answer_type,
            answer_scalar,
            answer_array,
            answer_object,
            answer_location,
            is_survey_started,
            is_survey_completed,
            survey_percentage_complete,
            age_at_answer,
            answered_at,
            survey_completed_at
        FROM `{project}.{dataset}.core__survey_responses`
        {where}
        ORDER BY user_id, survey_name, global_question_position
    """


# ---------------------------------------------------------------------------
# Management command
# ---------------------------------------------------------------------------

class Command(BaseCommand):
    help = (
        "Load HealthTree patient data from BigQuery into CTOMOP "
        "(Person + PatientInfo tables, including AI lines of therapy and "
        "survey responses)."
    )

    # ------------------------------------------------------------------
    # Argument parsing
    # ------------------------------------------------------------------

    def add_arguments(self, parser):
        parser.add_argument(
            "--bq-project",
            default=os.environ.get("HT_BQ_PROJECT", DEFAULT_BQ_PROJECT),
            help=(
                f"BigQuery GCP project id "
                f"(default: {DEFAULT_BQ_PROJECT} or HT_BQ_PROJECT env var)"
            ),
        )
        parser.add_argument(
            "--bq-dataset",
            default=os.environ.get("HT_BQ_DATASET", DEFAULT_BQ_DATASET),
            help=(
                f"BigQuery dataset that contains core__ tables "
                f"(default: {DEFAULT_BQ_DATASET} or HT_BQ_DATASET env var)"
            ),
        )
        parser.add_argument(
            "--keyfile",
            default=os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", ""),
            help=(
                "Path to a Google service-account JSON key file. "
                "Falls back to Application Default Credentials when omitted."
            ),
        )
        parser.add_argument(
            "--user-id",
            default=None,
            help="Process a single HealthTree user_id only.",
        )
        parser.add_argument(
            "--force-update",
            action="store_true",
            help="Re-populate PatientInfo even if it already exists.",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Query BigQuery but do NOT write anything to the Django DB.",
        )
        parser.add_argument(
            "--verbose",
            action="store_true",
            help="Print per-patient progress.",
        )

    # ------------------------------------------------------------------
    # Entry point
    # ------------------------------------------------------------------

    def handle(self, *args, **options):
        try:
            from google.cloud import bigquery
            from google.oauth2 import service_account
        except ImportError:
            raise CommandError(
                "google-cloud-bigquery is not installed. "
                "Run: pip install google-cloud-bigquery"
            )

        bq_project = options["bq_project"]
        bq_dataset = options["bq_dataset"]
        keyfile = options["keyfile"]
        user_id_filter = options["user_id"]
        force_update = options["force_update"]
        dry_run = options["dry_run"]
        verbose = options["verbose"]

        # Build BigQuery client
        if keyfile:
            credentials = service_account.Credentials.from_service_account_file(
                keyfile,
                scopes=["https://www.googleapis.com/auth/bigquery.readonly"],
            )
            bq_client = bigquery.Client(project=bq_project, credentials=credentials)
        else:
            bq_client = bigquery.Client(project=bq_project)

        self.stdout.write(
            self.style.MIGRATE_HEADING(
                f"HealthTree → CTOMOP loader\n"
                f"  BigQuery: {bq_project}.{bq_dataset}\n"
                f"  Auth:     {'keyfile: ' + keyfile if keyfile else 'Application Default Credentials'}\n"
                f"  Filter:   {'user_id=' + user_id_filter if user_id_filter else 'ALL patients'}\n"
                f"  Dry-run:  {dry_run}\n"
            )
        )

        # ------------------------------------------------------------------
        # 1. Fetch patients from BigQuery
        # ------------------------------------------------------------------
        self.stdout.write("Querying core__patients …")
        patients_query = _q_patients(bq_project, bq_dataset, user_id_filter)
        patients_rows = list(bq_client.query(patients_query).result())
        self.stdout.write(f"  → {len(patients_rows):,} patient row(s) retrieved.")

        if not patients_rows:
            self.stdout.write(self.style.WARNING("No patients found. Exiting."))
            return

        # ------------------------------------------------------------------
        # 2. Fetch lines of therapy (indexed by user_id)
        # ------------------------------------------------------------------
        self.stdout.write("Querying core__ai_lines_of_therapy …")
        lot_query = _q_lot(bq_project, bq_dataset, user_id_filter)
        lot_by_user: dict[str, list[dict]] = {}
        for row in bq_client.query(lot_query).result():
            uid = row["user_id"]
            lot_by_user.setdefault(uid, []).append(_serialize_row(row))
        self.stdout.write(
            f"  → {sum(len(v) for v in lot_by_user.values()):,} LOT row(s) "
            f"for {len(lot_by_user):,} user(s)."
        )

        # ------------------------------------------------------------------
        # 3. Fetch survey responses (indexed by user_id)
        # ------------------------------------------------------------------
        self.stdout.write("Querying core__survey_responses …")
        survey_query = _q_surveys(bq_project, bq_dataset, user_id_filter)
        surveys_by_user: dict[str, list[dict]] = {}
        for row in bq_client.query(survey_query).result():
            uid = row["user_id"]
            surveys_by_user.setdefault(uid, []).append(_serialize_row(row))
        self.stdout.write(
            f"  → {sum(len(v) for v in surveys_by_user.values()):,} survey row(s) "
            f"for {len(surveys_by_user):,} user(s)."
        )

        # ------------------------------------------------------------------
        # 4. Upsert Person + PatientInfo records
        # ------------------------------------------------------------------
        created_persons = 0
        updated_persons = 0
        created_pi = 0
        updated_pi = 0
        skipped_pi = 0
        errors = 0

        for patient_row in patients_rows:
            user_id = patient_row["user_id"]
            try:
                with transaction.atomic():
                    result = self._process_patient(
                        patient_row=patient_row,
                        lot_rows=lot_by_user.get(user_id, []),
                        survey_rows=surveys_by_user.get(user_id, []),
                        force_update=force_update,
                        dry_run=dry_run,
                        verbose=verbose,
                    )
                    created_persons += result["created_person"]
                    updated_persons += result["updated_person"]
                    created_pi += result["created_pi"]
                    updated_pi += result["updated_pi"]
                    skipped_pi += result["skipped_pi"]
            except Exception as exc:  # noqa: BLE001
                errors += 1
                self.stderr.write(
                    self.style.ERROR(f"  ERROR processing user {user_id}: {exc}")
                )

        # ------------------------------------------------------------------
        # 5. Summary
        # ------------------------------------------------------------------
        self.stdout.write(
            self.style.SUCCESS(
                f"\nDone{'  [DRY RUN — no DB writes]' if dry_run else ''}.\n"
                f"  Person   — created: {created_persons:,}  updated: {updated_persons:,}\n"
                f"  PatientInfo — created: {created_pi:,}  updated: {updated_pi:,}  "
                f"skipped: {skipped_pi:,}\n"
                f"  Errors: {errors:,}"
            )
        )

    # ------------------------------------------------------------------
    # Per-patient processing
    # ------------------------------------------------------------------

    def _process_patient(
        self,
        *,
        patient_row,
        lot_rows: list[dict],
        survey_rows: list[dict],
        force_update: bool,
        dry_run: bool,
        verbose: bool,
    ) -> dict:
        result = {
            "created_person": 0,
            "updated_person": 0,
            "created_pi": 0,
            "updated_pi": 0,
            "skipped_pi": 0,
        }

        user_id: str = patient_row["user_id"]
        # Use the BQ person_id if available, otherwise derive a stable one
        bq_person_id = _safe_int(patient_row.get("person_id"))
        person_id = bq_person_id if bq_person_id else _stable_person_id(user_id)

        gender_raw = (patient_row.get("gender") or "").lower().strip()
        gender_concept_id = GENDER_CONCEPT_MAP.get(gender_raw, UNKNOWN_CONCEPT_ID)

        race_raw = (patient_row.get("race") or "").lower().strip()
        race_concept_id = RACE_CONCEPT_MAP.get(race_raw, UNKNOWN_CONCEPT_ID)

        dob = _safe_date(patient_row.get("date_of_birth"))
        year_of_birth = dob.year if dob else None
        month_of_birth = dob.month if dob else None
        day_of_birth = dob.day if dob else None

        if verbose:
            self.stdout.write(
                f"  Processing user_id={user_id} → person_id={person_id}"
            )

        if dry_run:
            result["created_person"] = 1
            result["created_pi"] = 1
            return result

        # ----- Person -----
        person_defaults = {
            "gender_concept_id": gender_concept_id,
            "gender_source_value": patient_row.get("gender"),
            "race_concept_id": race_concept_id,
            "race_source_value": patient_row.get("race"),
            "ethnicity_concept_id": UNKNOWN_CONCEPT_ID,
            "ethnicity_source_value": patient_row.get("ethnicity"),
            "year_of_birth": year_of_birth,
            "month_of_birth": month_of_birth,
            "day_of_birth": day_of_birth,
            "birth_datetime": (
                datetime(dob.year, dob.month, dob.day, tzinfo=dt_timezone.utc)
                if dob
                else None
            ),
            "given_name": patient_row.get("first_name"),
            "family_name": patient_row.get("last_name"),
        }

        person, created_person = Person.objects.get_or_create(
            person_id=person_id,
            defaults=person_defaults,
        )

        if created_person:
            result["created_person"] = 1
        else:
            # Always refresh demographic fields in case they have changed
            for attr, val in person_defaults.items():
                setattr(person, attr, val)
            person.save(update_fields=list(person_defaults.keys()))
            result["updated_person"] = 1

        # ----- PatientInfo -----
        pi_exists = PatientInfo.objects.filter(person=person).exists()

        if pi_exists and not force_update:
            result["skipped_pi"] = 1
            return result

        # Compute age
        today = date.today()
        age = (
            today.year
            - dob.year
            - ((today.month, today.day) < (dob.month, dob.day))
            if dob
            else None
        )

        # Map gender to PatientInfo choices
        gender_pi = None
        if gender_raw in ("male", "m"):
            gender_pi = "M"
        elif gender_raw in ("female", "f"):
            gender_pi = "F"

        pi_fields = {
            "email": patient_row.get("email"),
            "date_of_birth": dob,
            "patient_age": age,
            "gender": gender_pi,
            "ethnicity": patient_row.get("ethnicity"),
            "country": None,  # not in core__patients directly
            "city": patient_row.get("city"),
            "region": patient_row.get("state"),
            "postal_code": patient_row.get("postal_code"),
            # HealthTree-specific denormalised arrays
            "ai_lines_of_therapy": lot_rows,
            "survey_responses": survey_rows,
        }

        # Derive therapy summary fields from LOT rows
        if lot_rows:
            pi_fields.update(self._derive_therapy_fields(lot_rows))

        if pi_exists:
            PatientInfo.objects.filter(person=person).update(**pi_fields)
            result["updated_pi"] = 1
        else:
            pi_fields["person"] = person
            PatientInfo.objects.create(**pi_fields)
            result["created_pi"] = 1

        return result

    # ------------------------------------------------------------------
    # Derive scalar therapy fields from the LOT JSON array
    # ------------------------------------------------------------------

    def _derive_therapy_fields(self, lot_rows: list[dict]) -> dict:
        """
        Populate legacy scalar therapy fields on PatientInfo from the
        ai_lines_of_therapy JSON array so the existing UI / matching logic
        still works without changes.
        """
        fields: dict = {}
        line1 = next((r for r in lot_rows if r.get("line_number") == 1), None)
        line2 = next((r for r in lot_rows if r.get("line_number") == 2), None)
        line3 = next((r for r in lot_rows if r.get("line_number") not in (1, 2)), None)

        fields["therapy_lines_count"] = len(lot_rows)

        if line1:
            fields["first_line_therapy"] = line1.get("active_ingredients")
            fields["first_line_start_date"] = _safe_date(line1.get("start_date"))
            fields["first_line_end_date"] = _safe_date(line1.get("end_date"))
            fields["first_line_outcome"] = line1.get("outcome")

        if line2:
            fields["second_line_therapy"] = line2.get("active_ingredients")
            fields["second_line_start_date"] = _safe_date(line2.get("start_date"))
            fields["second_line_end_date"] = _safe_date(line2.get("end_date"))
            fields["second_line_outcome"] = line2.get("outcome")

        if line3:
            fields["later_therapy"] = line3.get("active_ingredients")
            fields["later_start_date"] = _safe_date(line3.get("start_date"))
            fields["later_end_date"] = _safe_date(line3.get("end_date"))
            fields["later_outcome"] = line3.get("outcome")

        # Determine refractory status from the most recent completed line
        last_outcome = None
        for line in sorted(lot_rows, key=lambda r: r.get("line_number") or 0, reverse=True):
            if line.get("outcome"):
                last_outcome = line["outcome"]
                break

        if last_outcome:
            outcome_lower = last_outcome.lower()
            if "progressive" in outcome_lower:
                fields["treatment_refractory_status"] = "Refractory"
            elif any(w in outcome_lower for w in ("complete", "partial")):
                fields["treatment_refractory_status"] = "Responsive"
            elif "stable" in outcome_lower:
                fields["treatment_refractory_status"] = "Stable"
            else:
                fields["treatment_refractory_status"] = "Unknown"

        # Transplant / CAR-T flags — check any line.
        # Infer SCT vocabulary string from the free-text procedures field; deduplicate
        # while preserving order (a patient may have multiple transplant lines of the
        # same type).
        fields["stem_cell_transplant_history"] = list(dict.fromkeys(
            _infer_sct_type(r.get("procedures"))
            for r in lot_rows
            if r.get("has_transplant")
        ))

        return fields
