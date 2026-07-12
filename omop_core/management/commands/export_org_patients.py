"""
Management command: export_org_patients

Downloads all PatientRecord rows for a specified organization along with their
associated Person and OMOP CDM table rows to a JSON or JSONL file.

Usage:
    DATABASE_URL="..." python manage.py export_org_patients --org <slug-or-id>
    DATABASE_URL="..." python manage.py export_org_patients --org <slug-or-id> --output /path/to/file.json
    DATABASE_URL="..." python manage.py export_org_patients --org <slug-or-id> --format jsonl

Output structure (JSON mode):
    {
        "export_metadata": { org_slug, org_name, exported_at, total_patients },
        "patients": [
            {
                "patient_record": { ... },
                "person": { ... },
                "omop": {
                    "visit_occurrences": [...],
                    "condition_occurrences": [...],
                    "drug_exposures": [...],
                    "procedure_occurrences": [...],
                    "measurements": [...],
                    "observations": [...],
                    "visit_details": [...],
                    "deaths": [...],
                    "specimens": [...],
                    "notes": [...],
                    "note_nlp": [...],
                    "condition_eras": [...],
                    "drug_eras": [...],
                    "dose_eras": [...]
                },
                "documents": [...],
                "trial_enrollments": [...],
                "language_skills": [...],
                "survey_responses": [...]
            }
        ]
    }

JSONL mode writes one patient JSON object per line (no metadata wrapper).

Note: all OMOP data for the org is fetched in bulk (one query per table) to avoid
N+1 queries, then grouped by person_id in Python before writing.
"""
import json
from collections import defaultdict
from datetime import date, datetime, timezone
from decimal import Decimal

from django.core.management.base import BaseCommand, CommandError

from omop_core.models import (
    ConditionEra,
    ConditionOccurrence,
    Death,
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
    VisitDetail,
    VisitOccurrence,
)


# ---------------------------------------------------------------------------
# Serialization helpers
# ---------------------------------------------------------------------------

def _serialize(instance):
    """Convert a model instance to a JSON-serializable dict."""
    if instance is None:
        return None
    out = {}
    for field in instance._meta.get_fields():
        if not hasattr(field, 'column'):
            continue  # skip reverse relations / M2M descriptors
        val = getattr(instance, field.attname, None)
        if isinstance(val, (date, datetime)):
            val = val.isoformat()
        elif isinstance(val, Decimal):
            val = float(val)
        out[field.attname] = val
    return out


def _group_by_person(qs):
    """Evaluate a queryset and return a dict mapping person_id → list of rows."""
    groups = defaultdict(list)
    for obj in qs:
        groups[obj.person_id].append(obj)
    return groups


def _group_by_person_singular(qs):
    """Like _group_by_person but stores one row per person (e.g. Death)."""
    groups = {}
    for obj in qs:
        groups[obj.person_id] = obj
    return groups


def _group_note_nlp_by_person(qs):
    """Group NoteNlp rows by the patient on the parent Note."""
    groups = defaultdict(list)
    for obj in qs.select_related('note'):
        if obj.note_id and obj.note and obj.note.person_id:
            groups[obj.note.person_id].append(obj)
    return groups


# ---------------------------------------------------------------------------
# Command
# ---------------------------------------------------------------------------

class Command(BaseCommand):
    help = 'Export all PatientRecord rows and associated OMOP data for a given organization'

    def add_arguments(self, parser):
        parser.add_argument(
            '--org',
            required=True,
            help='Organization slug (e.g. "utah-rhtp") or integer primary key',
        )
        parser.add_argument(
            '--output',
            default=None,
            help='Output file path (default: <org-slug>_patients_export.<format>)',
        )
        parser.add_argument(
            '--format',
            choices=['json', 'jsonl'],
            default='json',
            dest='fmt',
            help='Output format: json (single document) or jsonl (one object per line). Default: json',
        )

    def handle(self, *args, **options):
        org_key = options['org']
        fmt = options['fmt']

        # ------------------------------------------------------------------
        # 1. Resolve organization
        # ------------------------------------------------------------------
        try:
            if org_key.isdigit():
                org = Organization.objects.get(pk=int(org_key))
            else:
                org = Organization.objects.get(slug=org_key)
        except Organization.DoesNotExist:
            raise CommandError(f"Organization not found: {org_key!r}")

        output_path = options['output'] or f"{org.slug}_patients_export.{fmt}"

        self.stdout.write(f"Org:    {org.name!r}  (slug={org.slug!r}, id={org.pk})")
        self.stdout.write(f"Output: {output_path}  (format={fmt})")
        self.stdout.write('')

        # ------------------------------------------------------------------
        # 2. Load PatientRecords for this org
        # ------------------------------------------------------------------
        patient_records = list(
            PatientRecord.objects.filter(organization=org).select_related('person')
        )
        total = len(patient_records)
        if total == 0:
            self.stdout.write(self.style.WARNING('No patients found for this organization — nothing to export.'))
            return

        self.stdout.write(f"Found {total} PatientRecord row{'s' if total != 1 else ''}.")
        self.stdout.write('')

        person_ids = [pr.person_id for pr in patient_records]

        # ------------------------------------------------------------------
        # 3. Bulk-fetch all OMOP / related tables (one query per table)
        # ------------------------------------------------------------------
        omop_tables = [
            ('visit_occurrences',      VisitOccurrence,         _group_by_person,          {'person_id__in': person_ids}),
            ('condition_occurrences',  ConditionOccurrence,     _group_by_person,          {'person_id__in': person_ids}),
            ('drug_exposures',         DrugExposure,            _group_by_person,          {'person_id__in': person_ids}),
            ('procedure_occurrences',  ProcedureOccurrence,     _group_by_person,          {'person_id__in': person_ids}),
            ('measurements',           Measurement,             _group_by_person,          {'person_id__in': person_ids}),
            ('observations',           Observation,             _group_by_person,          {'person_id__in': person_ids}),
            ('visit_details',          VisitDetail,             _group_by_person,          {'person_id__in': person_ids}),
            ('deaths',                 Death,                   _group_by_person_singular, {'person_id__in': person_ids}),
            ('specimens',              Specimen,                _group_by_person,          {'person_id__in': person_ids}),
            ('notes',                  Note,                    _group_by_person,          {'person_id__in': person_ids}),
            ('note_nlp',               NoteNlp,                 _group_note_nlp_by_person, {'note__person_id__in': person_ids}),
            ('condition_eras',         ConditionEra,            _group_by_person,          {'person_id__in': person_ids}),
            ('drug_eras',              DrugEra,                 _group_by_person,          {'person_id__in': person_ids}),
            ('dose_eras',              DoseEra,                 _group_by_person,          {'person_id__in': person_ids}),
        ]
        related_tables = [
            ('documents',         PatientDocument,        _group_by_person),
            ('trial_enrollments', PatientTrialEnrollment, _group_by_person),
            ('language_skills',   PersonLanguageSkill,    _group_by_person),
            ('survey_responses',  PatientSurveyResponse,  _group_by_person),
        ]

        fetched_omop = {}
        fetched_related = {}

        self.stdout.write('Fetching OMOP tables:')
        total_omop_rows = 0
        for key, Model, grouper, filters in omop_tables:
            qs = Model.objects.filter(**filters)
            grouped = grouper(qs)
            row_count = (
                sum(len(v) for v in grouped.values())
                if isinstance(grouped, defaultdict)
                else len(grouped)
            )
            total_omop_rows += row_count
            fetched_omop[key] = grouped
            label = f'  {key}'
            self.stdout.write(f'{label:<30} {row_count:>8,} rows')

        self.stdout.write('')
        self.stdout.write('Fetching related tables:')
        total_related_rows = 0
        for key, Model, grouper in related_tables:
            qs = Model.objects.filter(person_id__in=person_ids)
            grouped = grouper(qs)
            row_count = (
                sum(len(v) for v in grouped.values())
                if isinstance(grouped, defaultdict)
                else len(grouped)
            )
            total_related_rows += row_count
            fetched_related[key] = grouped
            label = f'  {key}'
            self.stdout.write(f'{label:<30} {row_count:>8,} rows')

        # ------------------------------------------------------------------
        # 4. Assemble per-patient records
        # ------------------------------------------------------------------
        self.stdout.write('')
        self.stdout.write('Assembling patient records...')

        def _rows(store, pid):
            """Return serialized list or single dict depending on grouper type."""
            data = store.get(pid)
            if data is None:
                return []
            if isinstance(data, list):
                return [_serialize(obj) for obj in data]
            # singular (e.g. Death)
            return _serialize(data)

        patient_docs = []
        report_interval = max(1, total // 10)  # report ~10 times

        for i, pr in enumerate(patient_records, start=1):
            pid = pr.person_id

            row = {
                'patient_record': _serialize(pr),
                'person': _serialize(pr.person),
                'omop': {
                    'visit_occurrences':     _rows(fetched_omop['visit_occurrences'], pid),
                    'condition_occurrences': _rows(fetched_omop['condition_occurrences'], pid),
                    'drug_exposures':        _rows(fetched_omop['drug_exposures'], pid),
                    'procedure_occurrences': _rows(fetched_omop['procedure_occurrences'], pid),
                    'measurements':          _rows(fetched_omop['measurements'], pid),
                    'observations':          _rows(fetched_omop['observations'], pid),
                    'visit_details':         _rows(fetched_omop['visit_details'], pid),
                    'death':                 _rows(fetched_omop['deaths'], pid),
                    'specimens':             _rows(fetched_omop['specimens'], pid),
                    'notes':                 _rows(fetched_omop['notes'], pid),
                    'note_nlp':              _rows(fetched_omop['note_nlp'], pid),
                    'condition_eras':        _rows(fetched_omop['condition_eras'], pid),
                    'drug_eras':             _rows(fetched_omop['drug_eras'], pid),
                    'dose_eras':             _rows(fetched_omop['dose_eras'], pid),
                },
                'documents':         _rows(fetched_related['documents'], pid),
                'trial_enrollments': _rows(fetched_related['trial_enrollments'], pid),
                'language_skills':   _rows(fetched_related['language_skills'], pid),
                'survey_responses':  _rows(fetched_related['survey_responses'], pid),
            }
            patient_docs.append(row)

            if i % report_interval == 0 or i == total:
                self.stdout.write(f'  [{i:>{len(str(total))}}/{total}] assembled')

        # ------------------------------------------------------------------
        # 5. Write output file
        # ------------------------------------------------------------------
        self.stdout.write('')
        self.stdout.write(f'Writing {output_path} ...')

        if fmt == 'jsonl':
            with open(output_path, 'w') as fh:
                for row in patient_docs:
                    fh.write(json.dumps(row, default=str) + '\n')
        else:
            export_doc = {
                'export_metadata': {
                    'org_slug': org.slug,
                    'org_name': org.name,
                    'exported_at': datetime.now(tz=timezone.utc).isoformat(),
                    'total_patients': total,
                    'total_omop_rows': total_omop_rows,
                    'total_related_rows': total_related_rows,
                },
                'patients': patient_docs,
            }
            with open(output_path, 'w') as fh:
                json.dump(export_doc, fh, indent=2, default=str)

        # ------------------------------------------------------------------
        # 6. Summary
        # ------------------------------------------------------------------
        self.stdout.write('')
        self.stdout.write(self.style.SUCCESS(f'Export complete → {output_path}'))
        self.stdout.write(f'  Patients:          {total:>8,}')
        self.stdout.write(f'  OMOP rows:         {total_omop_rows:>8,}')
        self.stdout.write(f'  Related rows:      {total_related_rows:>8,}')
        self.stdout.write(f'  Total rows:        {total_omop_rows + total_related_rows:>8,}')
