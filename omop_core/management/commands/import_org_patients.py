"""
Management command: import_org_patients

Imports patients from a JSON file produced by export_org_patients into a
target organization. Designed for reproducing published benchmark results
from a Zenodo data bundle.

Usage:
    DATABASE_URL="..." python manage.py import_org_patients synthea-bc.json --org synthea-bc
    DATABASE_URL="..." python manage.py import_org_patients synthea-bc.json --org synthea-bc --dry-run
    DATABASE_URL="..." python manage.py import_org_patients synthea-bc.json --org synthea-bc --replace
    DATABASE_URL="..." python manage.py import_org_patients synthea-bc.json --org synthea-bc --create-org

The importer:
  - Resolves (or creates) the target organization
  - For each patient in the export file:
      * Creates a Person record using the original person_id if it is free;
        skips if taken and --replace is not set; deletes and reimports if
        --replace is set
      * Creates all OMOP CDM rows with fresh sequence-backed PKs
      * Derives PatientRecord from those rows via refresh_patient_record,
        the same path every other write obeys. With --snapshot-patient-record
        the exported projection is written verbatim instead, preserving
        enriched values that have no OMOP row behind them and therefore
        cannot be re-derived — required to reproduce published benchmark
        numbers exactly.
      * Export keys that no longer exist on PatientRecord are dropped with a
        warning rather than raising, since an export outlives schema changes.
  - Concept FK values absent from the target Concept table are silently
    remapped to concept_id=0 ("No matching concept" per OMOP CDM v5.4).
    The source_value fields that benchmark queries use as fallback are
    always preserved intact.
  - Cross-table OMOP FKs (e.g. visit_occurrence_id inside ConditionOccurrence)
    are set to NULL to avoid FK violations caused by re-sequenced PKs.
  - PersonLanguageSkill rows whose language_concept is absent from the target
    Concept table are skipped (not critical for benchmarks).
  - Each patient is imported inside its own transaction; a failure for one
    patient does not abort the rest.

Typical run time: ~1–3 s per patient on a local PostgreSQL instance.
"""
import json

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from omop_core.models import (
    Concept,
    ConceptClass,
    ConditionEra,
    ConditionOccurrence,
    Death,
    Domain,
    DoseEra,
    DrugEra,
    DrugExposure,
    Measurement,
    Note,
    NoteNlp,
    Observation,
    Organization,
    PatientDocument,
    PatientRecord,
    PatientSurveyResponse,
    PatientTrialEnrollment,
    Person,
    PersonLanguageSkill,
    ProcedureOccurrence,
    Specimen,
    Survey,
    VisitDetail,
    VisitOccurrence,
    Vocabulary,
)
from omop_core.services.pk import next_pk_batch
from omop_core.services.patient_record_service import refresh_patient_record


# ---------------------------------------------------------------------------
# OMOP table registry
# (export_key, Model, pk_field, cross_table_fk_fields_to_null)
#
# cross_table_fk_fields_to_null: fields that reference other OMOP rows by the
# *original* PK values, which are no longer valid after re-sequencing.  Setting
# them to NULL is safe for benchmarking — the benchmark does not join through
# these links.
# ---------------------------------------------------------------------------
_OMOP_TABLES = [
    ('visit_occurrences',     VisitOccurrence,     'visit_occurrence_id',    []),
    ('condition_occurrences', ConditionOccurrence, 'condition_occurrence_id', ['visit_occurrence_id', 'visit_detail_id']),
    ('drug_exposures',        DrugExposure,        'drug_exposure_id',        ['visit_occurrence_id', 'visit_detail_id']),
    ('procedure_occurrences', ProcedureOccurrence, 'procedure_occurrence_id', ['visit_occurrence_id', 'visit_detail_id']),
    ('measurements',          Measurement,         'measurement_id',          ['visit_occurrence_id', 'visit_detail_id']),
    ('observations',          Observation,         'observation_id',          ['visit_occurrence_id', 'visit_detail_id']),
    ('visit_details',         VisitDetail,         'visit_detail_id',         ['visit_occurrence_id']),
    ('specimens',             Specimen,            'specimen_id',             ['visit_occurrence_id']),
    ('notes',                 Note,                'note_id',                 ['visit_occurrence_id', 'visit_detail_id']),
    ('note_nlp',              NoteNlp,             'note_nlp_id',             ['note_id']),
    ('condition_eras',        ConditionEra,        'condition_era_id',        []),
    ('drug_eras',             DrugEra,             'drug_era_id',             []),
    ('dose_eras',             DoseEra,             'dose_era_id',             []),
]

# PatientRecord fields that are auto-managed by Django and must not be set
# on import.  person_id / organization_id are overridden explicitly instead.
_PR_SKIP_FIELDS = frozenset(['id', 'created_at', 'updated_at', 'person_id', 'organization_id'])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ensure_zero_concept():
    """Ensure concept_id=0 ('No matching concept') exists.

    OMOP CDM v5.4 mandates concept_id=0 as the universal sentinel for
    unmapped codes.  A freshly migrated PROMOP DB may not have the full
    Athena vocabulary bundle loaded, so we create the minimal row here.
    The supporting lookup rows (Vocabulary, Domain, ConceptClass) use 0 as
    their own *_concept_id FK, which mirrors the convention used by the
    existing enrich_breast_cancer_omop_data command.
    """
    if Concept.objects.filter(concept_id=0).exists():
        return
    Vocabulary.objects.get_or_create(
        vocabulary_id='None',
        defaults={
            'vocabulary_name': 'OMOP Standardized Vocabularies',
            'vocabulary_reference': 'http://www.ohdsi.org',
            'vocabulary_version': '5.0',
            'vocabulary_concept_id': 0,
        },
    )
    Domain.objects.get_or_create(
        domain_id='Metadata',
        defaults={'domain_name': 'Metadata', 'domain_concept_id': 0},
    )
    ConceptClass.objects.get_or_create(
        concept_class_id='Undefined',
        defaults={'concept_class_name': 'Undefined', 'concept_class_concept_id': 0},
    )
    Concept.objects.create(
        concept_id=0,
        concept_name='No matching concept',
        domain_id='Metadata',
        vocabulary_id='None',
        concept_class_id='Undefined',
        standard_concept=None,
        concept_code='No matching concept',
        valid_start_date='1970-01-01',
        valid_end_date='2099-12-31',
    )


def _collect_concept_ids(patients):
    """Return the set of all non-zero concept_id integers referenced anywhere
    in the exported patient list (OMOP rows + Person fields + language_skills).
    Used to pre-check which concepts need to be remapped to 0.
    """
    ids = set()
    for patient in patients:
        person_data = patient.get('person') or {}
        for k, v in person_data.items():
            if k.endswith('_concept_id') and isinstance(v, int) and v > 0:
                ids.add(v)

        omop = patient.get('omop') or {}
        for rows in omop.values():
            if isinstance(rows, dict):
                rows = [rows]
            elif not isinstance(rows, list):
                continue
            for row in rows:
                if not isinstance(row, dict):
                    continue
                for k, v in row.items():
                    if k.endswith('_concept_id') and isinstance(v, int) and v > 0:
                        ids.add(v)

        for ls in patient.get('language_skills') or []:
            if isinstance(ls, dict):
                v = ls.get('language_concept_id')
                if isinstance(v, int) and v > 0:
                    ids.add(v)

    return ids


def _remap_concepts(row_dict, concept_map):
    """Return a copy of row_dict with missing concept_ids replaced by 0."""
    out = {}
    for k, v in row_dict.items():
        if k.endswith('_concept_id') and isinstance(v, int) and v in concept_map:
            out[k] = 0
        else:
            out[k] = v
    return out


def _build_omop_row(row, pk_field, new_pk, new_person_id, concept_map, null_fields):
    """Build a cleaned field dict for one OMOP row, ready for Model(**cleaned)."""
    cleaned = {}
    for k, v in row.items():
        if k == pk_field:
            cleaned[k] = new_pk
        elif k == 'person_id':
            cleaned[k] = new_person_id
        elif k in null_fields:
            cleaned[k] = None
        elif k.endswith('_concept_id') and isinstance(v, int) and v in concept_map:
            cleaned[k] = 0
        else:
            cleaned[k] = v
    return cleaned


# ---------------------------------------------------------------------------
# Command
# ---------------------------------------------------------------------------

class Command(BaseCommand):
    help = (
        'Import patients from an export_org_patients JSON file into a '
        'target organization. Designed for reproducing benchmark results '
        'from a Zenodo data bundle.'
    )

    # Set once per run by _filter_patient_record_fields so the warning about
    # retired columns prints once, not once per patient.
    _warned_dropped_fields = False

    def add_arguments(self, parser):
        parser.add_argument(
            'input',
            nargs='?',
            default=None,
            help='Path to JSON export file (e.g. synthea-bc.json)',
        )
        # The published Zenodo record for the benchmark cohort documents the
        # flag spelling, so accept both rather than invalidating that citation.
        parser.add_argument(
            '--input',
            dest='input_flag',
            default=None,
            help='Same as the positional argument.',
        )
        parser.add_argument(
            '--org',
            required=True,
            help=(
                'Target organization slug (e.g. synthea-bc). '
                'Pass --create-org to create it if it does not exist.'
            ),
        )
        parser.add_argument(
            '--create-org',
            action='store_true',
            help='Create the target organization if it does not already exist.',
        )
        parser.add_argument(
            '--replace',
            action='store_true',
            help=(
                'If a Person with the same person_id already exists, delete it '
                '(cascading to all OMOP rows and PatientRecord) and reimport. '
                'Without this flag, existing patients are skipped.'
            ),
        )
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='Parse and validate without writing anything to the database.',
        )
        parser.add_argument(
            '--snapshot-patient-record',
            action='store_true',
            help=(
                'Write the exported PatientRecord projection verbatim instead of '
                'deriving it from the imported OMOP rows. Needed to reproduce '
                'published benchmark numbers exactly, because an export may carry '
                'enriched values that have no OMOP row behind them and so cannot '
                'be re-derived. Off by default: deriving is the contract every '
                'other write path obeys.'
            ),
        )

    def handle(self, *args, **options):
        input_path = options['input'] or options['input_flag']
        if not input_path:
            raise CommandError(
                'Give the export file as a positional argument or with --input.'
            )
        org_slug = options['org']
        dry_run = options['dry_run']
        create_org = options['create_org']
        replace = options['replace']

        # ------------------------------------------------------------------
        # 1. Load the export file
        # ------------------------------------------------------------------
        self.stdout.write(f'Loading {input_path!r}...')
        try:
            with open(input_path) as fh:
                export = json.load(fh)
        except FileNotFoundError:
            raise CommandError(f'File not found: {input_path!r}')
        except json.JSONDecodeError as exc:
            raise CommandError(f'JSON parse error in {input_path!r}: {exc}')

        if 'patients' not in export:
            raise CommandError(
                "Invalid export file: missing 'patients' key. "
                "Expected a JSON file produced by export_org_patients."
            )

        patients = export['patients']
        total = len(patients)
        meta = export.get('export_metadata') or {}

        self.stdout.write(f'Found {total:,} patient record(s).')
        if meta:
            self.stdout.write(
                f'  Source org : {meta.get("org_slug")!r}\n'
                f'  Exported at: {meta.get("exported_at")!r}'
            )
        self.stdout.write('')

        # ------------------------------------------------------------------
        # 2. Resolve (or create) organization
        # ------------------------------------------------------------------
        org = None
        try:
            org = Organization.objects.get(slug=org_slug)
            self.stdout.write(f'Target org: {org.name!r}  (id={org.pk})')
        except Organization.DoesNotExist:
            if not create_org:
                raise CommandError(
                    f'Organization {org_slug!r} not found. '
                    'Pass --create-org to create it automatically.'
                )
            if not dry_run:
                org = Organization.objects.create(
                    slug=org_slug, name=org_slug, is_active=True,
                )
                self.stdout.write(
                    self.style.SUCCESS(f'Created organization {org_slug!r}  (id={org.pk})')
                )
            else:
                self.stdout.write(f'[dry-run] Would create organization {org_slug!r}')

        # ------------------------------------------------------------------
        # 3. Ensure concept_id=0 exists (OMOP unmapped-concept sentinel)
        # ------------------------------------------------------------------
        if not dry_run:
            _ensure_zero_concept()

        # ------------------------------------------------------------------
        # 4. Pre-check concept references
        # ------------------------------------------------------------------
        self.stdout.write('Scanning concept references...')
        all_concept_ids = _collect_concept_ids(patients)
        self.stdout.write(f'  {len(all_concept_ids):,} distinct concept_id(s) referenced')

        if all_concept_ids and not dry_run:
            existing_cids = set(
                Concept.objects
                .filter(concept_id__in=all_concept_ids)
                .values_list('concept_id', flat=True)
            )
            missing_cids = all_concept_ids - existing_cids
            if missing_cids:
                self.stdout.write(
                    self.style.WARNING(
                        f'  {len(missing_cids):,} concept(s) absent from target DB '
                        f'— will be remapped to concept_id=0'
                    )
                )
        else:
            missing_cids = set()

        # concept_map: {concept_id → 0} for every concept missing in target DB
        concept_map = {cid: 0 for cid in missing_cids}
        self.stdout.write('')

        # ------------------------------------------------------------------
        # 5. Import loop
        # ------------------------------------------------------------------
        counts = {'imported': 0, 'replaced': 0, 'skipped': 0, 'errors': 0}

        for i, patient in enumerate(patients, start=1):
            person_data = patient.get('person') or {}
            old_pid = person_data.get('person_id')

            label = f'[{i:>{len(str(total))}}/{total}] person_id={old_pid}'
            self.stdout.write(label, ending='')
            self.stdout.flush()

            if dry_run:
                already_exists = Person.objects.filter(person_id=old_pid).exists()
                if already_exists and not replace:
                    self.stdout.write(self.style.WARNING(' — would skip (person_id exists)'))
                    counts['skipped'] += 1
                elif already_exists:
                    self.stdout.write(f' — would replace')
                    counts['replaced'] += 1
                else:
                    self.stdout.write(' — would import')
                    counts['imported'] += 1
                continue

            try:
                new_pid, was_replaced = self._import_patient(
                    patient=patient,
                    org=org,
                    concept_map=concept_map,
                    replace=replace,
                    snapshot=options['snapshot_patient_record'],
                )
                if new_pid is None:
                    self.stdout.write(self.style.WARNING(' — skipped (person_id exists)'))
                    counts['skipped'] += 1
                else:
                    remap = f' → new person_id={new_pid}' if new_pid != old_pid else ''
                    if was_replaced:
                        self.stdout.write(self.style.SUCCESS(f' — replaced{remap}'))
                        counts['replaced'] += 1
                    else:
                        self.stdout.write(self.style.SUCCESS(f' — imported{remap}'))
                        counts['imported'] += 1
            except Exception as exc:
                self.stdout.write(self.style.ERROR(f' — ERROR: {exc}'))
                counts['errors'] += 1

        # ------------------------------------------------------------------
        # 6. Summary
        # ------------------------------------------------------------------
        self.stdout.write('')
        prefix = '[DRY RUN] ' if dry_run else ''
        self.stdout.write(self.style.SUCCESS(f'{prefix}Import complete'))
        self.stdout.write(f'  Patients in file  : {total:>8,}')
        self.stdout.write(f'  Imported          : {counts["imported"]:>8,}')
        self.stdout.write(f'  Replaced          : {counts["replaced"]:>8,}')
        self.stdout.write(f'  Skipped (exists)  : {counts["skipped"]:>8,}')
        self.stdout.write(f'  Errors            : {counts["errors"]:>8,}')

    # ------------------------------------------------------------------
    # Per-patient import (runs inside its own transaction)
    # ------------------------------------------------------------------

    @transaction.atomic
    def _import_patient(self, patient, org, concept_map, replace, snapshot=False):
        """Import one patient.  Returns (new_person_id, was_replaced) or
        (None, False) if the patient was skipped."""
        person_data = patient.get('person') or {}
        pr_data = patient.get('patient_record') or {}
        omop_data = patient.get('omop') or {}
        old_pid = person_data.get('person_id')

        # ---- Collision check ----
        was_replaced = False
        existing = Person.objects.filter(person_id=old_pid).first()
        if existing is not None:
            if not replace:
                return None, False
            existing.delete()   # cascade: OMOP rows + PatientRecord
            was_replaced = True

        new_pid = old_pid  # reuse original person_id (it's free now)

        # ---- Person ----
        person_fields = {
            k: v for k, v in person_data.items() if k != 'person_id'
        }
        person_fields = _remap_concepts(person_fields, concept_map)
        person = Person(person_id=new_pid, **person_fields)
        person.save()

        # ---- OMOP tables ----
        for export_key, Model, pk_field, null_fields in _OMOP_TABLES:
            rows = omop_data.get(export_key)
            if not rows:
                continue
            if isinstance(rows, dict):
                rows = [rows]

            new_pks = next_pk_batch(Model, pk_field, len(rows))
            instances = [
                Model(**_build_omop_row(row, pk_field, new_pk, new_pid, concept_map, null_fields))
                for row, new_pk in zip(rows, new_pks)
            ]
            Model.objects.bulk_create(instances, batch_size=500)

        # ---- Death (person_id is the PK — no sequence needed) ----
        death_data = omop_data.get('death')
        if isinstance(death_data, dict):
            cleaned = {
                k: (0 if k.endswith('_concept_id') and isinstance(v, int) and v in concept_map else v)
                for k, v in death_data.items()
                if k != 'person_id'
            }
            Death.objects.create(person=person, **cleaned)

        # ---- PersonLanguageSkill ----
        for ls in patient.get('language_skills') or []:
            if not isinstance(ls, dict):
                continue
            lang_cid = ls.get('language_concept_id')
            if lang_cid and not Concept.objects.filter(concept_id=lang_cid).exists():
                continue  # skip unknown language concepts
            try:
                PersonLanguageSkill.objects.get_or_create(
                    person=person,
                    language_concept_id=lang_cid,
                    defaults={
                        'skill_level': ls.get('skill_level', 'both'),
                        'is_primary': ls.get('is_primary', False),
                    },
                )
            except Exception:
                pass  # non-critical; skip silently

        # ---- PatientDocument ----
        for doc in patient.get('documents') or []:
            if not isinstance(doc, dict):
                continue
            fields = {
                k: v for k, v in doc.items()
                if k not in ('id', 'person_id', 'uploaded_at')
            }
            try:
                PatientDocument.objects.create(person=person, **fields)
            except Exception:
                pass

        # ---- PatientTrialEnrollment ----
        for enr in patient.get('trial_enrollments') or []:
            if not isinstance(enr, dict):
                continue
            fields = {
                k: v for k, v in enr.items()
                if k not in ('id', 'person_id')
            }
            try:
                PatientTrialEnrollment.objects.create(person=person, **fields)
            except Exception:
                pass

        # ---- PatientSurveyResponse ----
        for resp in patient.get('survey_responses') or []:
            if not isinstance(resp, dict):
                continue
            survey_id = resp.get('survey_id')
            if not survey_id or not Survey.objects.filter(id=survey_id).exists():
                continue  # skip if survey definition is absent
            fields = {
                k: v for k, v in resp.items()
                if k not in ('id', 'person_id')
            }
            try:
                PatientSurveyResponse.objects.create(person=person, **fields)
            except Exception:
                pass

        # ---- PatientRecord ----
        # Derived by default: the OMOP rows above are the facts, and every other
        # write path in the system rebuilds the projection from them. Importing
        # the exported projection verbatim is the exception, not the rule — see
        # --snapshot-patient-record.
        if snapshot:
            pr_fields = self._filter_patient_record_fields(pr_data)
            PatientRecord.objects.create(
                person=person,
                organization=org,
                **pr_fields,
            )
        else:
            # Create the row first so it carries the target org: refresh_patient_record
            # builds a bare PatientRecord(person=person) when none exists, which would
            # leave organization unset and drop the patient out of every org-scoped query.
            PatientRecord.objects.get_or_create(person=person, organization=org)
            refresh_patient_record(person)

        return new_pid, was_replaced

    def _filter_patient_record_fields(self, pr_data):
        """Drop export keys that no longer exist on PatientRecord.

        An export is a point-in-time artifact; the model moves on. The published
        benchmark cohort predates several hundred commits of schema drift, so
        passing its keys straight into objects.create() raises TypeError on the
        first column that has since been renamed or dropped. Unknown keys are
        dropped with a warning — losing a retired column is recoverable, failing
        the whole import is not.
        """
        valid = {
            f.name for f in PatientRecord._meta.get_fields() if hasattr(f, 'column')
        }
        kept, dropped = {}, []
        for key, value in pr_data.items():
            if key in _PR_SKIP_FIELDS:
                continue
            if key in valid:
                kept[key] = value
            else:
                dropped.append(key)
        if dropped and not self._warned_dropped_fields:
            # Once per run, not once per patient — the set is identical for every
            # row in a single export.
            self._warned_dropped_fields = True
            self.stdout.write(self.style.WARNING(
                f'  Export carries {len(dropped)} field(s) absent from the current '
                f'PatientRecord model; dropping them: {", ".join(sorted(dropped))}'
            ))
        return kept
