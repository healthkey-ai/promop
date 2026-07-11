"""
Management command: enrich_synthea_mm_omop_data

Backfills MM-specific OMOP data for the existing SYNTHEA-MM cohort:

  - Ensures each selected patient has a matching ConditionOccurrence row for
    the MM diagnosis from the source FHIR bundle.
  - Ensures each selected patient has at least two ProcedureOccurrence rows by
    adding synthetic MM procedure concepts when the imported bundle only
    contributed zero or one procedure.
  - Refreshes PatientRecord after the OMOP writes so the flat projection picks
    up the new source rows.

This is a staging/data-enrichment utility for the existing SYNTHEA-MM cohort.
It is intentionally idempotent so it can be re-run safely.

Usage:
    python manage.py enrich_synthea_mm_omop_data --confirm
    python manage.py enrich_synthea_mm_omop_data --confirm --bundle /tmp/synthea_mm_100.json
    python manage.py enrich_synthea_mm_omop_data --confirm --person-ids 2481,2482
"""
import json
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db import close_old_connections, transaction

from omop_core.models import (
    ConditionOccurrence, Concept, ConceptClass, Domain, DrugExposure, Location,
    Observation, PatientRecord, Person, ProcedureOccurrence, Vocabulary,
)
from omop_core.services.patient_record_service import refresh_patient_record
from omop_core.services.pk import next_pk
from omop_core.signals import suppress_patient_record_refresh


DEFAULT_BUNDLE = '/tmp/synthea_mm_100.json'
DEFAULT_ORG_SLUGS = 'synthea-mm'
MIN_PROCEDURES_PER_PATIENT = 2

_LOCAL_VOCAB_ID = 'LOCAL'
_IMMUNIZATION_SOURCE_PREFIX = 'IMMUNIZATION:'
_IMMUNIZATION_SPECS = [
    ('140', 'Influenza, seasonal, injectable'),
    ('208', 'COVID-19, mRNA, LNP-S, PF'),
    ('133', 'Pneumococcal conjugate PCV 13'),
]
_MM_PROCEDURE_SPECS = [
    ('SYNTH-MM-BMBX', 'Bone marrow biopsy (synthetic MM enrichment)', 14),
    ('SYNTH-MM-BMASP', 'Bone marrow aspiration (synthetic MM enrichment)', 15),
]
_SPECIMEN_OBS_CODE = '31208-2'
_SPECIMEN_OBS_NAME = 'Specimen Source'
_SPECIMEN_VALUES = [
    'Bone marrow biopsy',
    'Bone marrow aspirate',
]


def _normalize_name(name_parts):
    return ' '.join(part.strip().lower() for part in name_parts if part and part.strip())


def _patient_key_from_resource(patient_resource):
    name = patient_resource.get('name') or [{}]
    first = name[0] if name else {}
    given = _normalize_name(first.get('given', []))
    family = (first.get('family') or '').strip().lower()
    birth_date = (patient_resource.get('birthDate') or '').strip()
    return f'{given}|{family}|{birth_date}'


def _patient_key_from_person(person):
    given = (person.given_name or '').strip().lower()
    family = (person.family_name or '').strip().lower()
    if person.year_of_birth is None:
        birth_date = ''
    else:
        month = f'{person.month_of_birth:02d}' if person.month_of_birth else '01'
        day = f'{person.day_of_birth:02d}' if person.day_of_birth else '01'
        birth_date = f'{person.year_of_birth:04d}-{month}-{day}'
    return f'{given}|{family}|{birth_date}'


def _coerce_date(value, fallback=None):
    if value:
        raw = value[:10] if 'T' in value else value
        try:
            return datetime.strptime(raw, '%Y-%m-%d').date()
        except ValueError:
            pass
    return fallback


def _get_or_create_vocab(vocabulary_id, vocabulary_name):
    vocab, _ = Vocabulary.objects.get_or_create(
        vocabulary_id=vocabulary_id,
        defaults={
            'vocabulary_name': vocabulary_name,
            'vocabulary_reference': '',
            'vocabulary_version': 'synthetic enrichment',
            'vocabulary_concept_id': 0,
        },
    )
    return vocab


def _get_or_create_domain(domain_id):
    domain, _ = Domain.objects.get_or_create(
        domain_id=domain_id,
        defaults={'domain_name': domain_id, 'domain_concept_id': 0},
    )
    return domain


def _get_or_create_concept_class(concept_class_id):
    concept_class, _ = ConceptClass.objects.get_or_create(
        concept_class_id=concept_class_id,
        defaults={'concept_class_name': concept_class_id, 'concept_class_concept_id': 0},
    )
    return concept_class


def _get_or_create_concept(*, concept_code, concept_name, vocabulary_id, domain_id, concept_class_id,
                           standard_concept='S', concept_id=None, create=True):
    if concept_id is not None:
        concept = Concept.objects.filter(concept_id=concept_id).select_related(
            'vocabulary', 'domain', 'concept_class'
        ).first()
        if concept:
            return concept

    concept = Concept.objects.filter(
        concept_code=concept_code,
        vocabulary_id=vocabulary_id,
    ).select_related('vocabulary', 'domain', 'concept_class').first()
    if concept:
        return concept

    if not create:
        return None

    if concept_id is None:
        concept_id = next_pk(Concept, 'concept_id')

    concept = Concept.objects.create(
        concept_id=concept_id,
        concept_name=concept_name,
        domain=_get_or_create_domain(domain_id),
        vocabulary=_get_or_create_vocab(vocabulary_id, vocabulary_name=vocabulary_id),
        concept_class=_get_or_create_concept_class(concept_class_id),
        standard_concept=standard_concept,
        concept_code=concept_code,
        valid_start_date='1970-01-01',
        valid_end_date='2099-12-31',
    )
    return concept


def _get_or_create_location(*, address_line, city, state, postal_code, country):
    location = Location.objects.filter(
        address_1=address_line[:50] if address_line else None,
        city=city[:50] if city else None,
        state=state[:2] if state else None,
        zip=postal_code[:9] if postal_code else None,
        country=country[:100] if country else None,
    ).first()
    if location:
        return location

    location = Location.objects.create(
        location_id=next_pk(Location, 'location_id'),
        address_1=address_line[:50] if address_line else None,
        city=city[:50] if city else None,
        state=state[:2] if state else None,
        zip=postal_code[:9] if postal_code else None,
        country=country[:100] if country else None,
        location_source_value='|'.join(filter(None, [address_line, city, state, postal_code, country]))[:50],
    )
    return location


def _parse_bundle(path):
    bundle_path = Path(path)
    if not bundle_path.exists():
        raise CommandError(f'Bundle file not found: {bundle_path}')
    with bundle_path.open() as f:
        bundle = json.load(f)
    if bundle.get('resourceType') != 'Bundle':
        raise CommandError('Bundle file must have resourceType=Bundle')

    patient_map = {}
    patient_aliases = {}

    def _resolve_patient(ref):
        ref = (ref or '').strip()
        if not ref:
            return ''
        if ref in patient_aliases:
            return patient_aliases[ref]
        if ref.startswith('urn:uuid:'):
            ref = ref[len('urn:uuid:'):]
        if ref in patient_aliases:
            return patient_aliases[ref]
        return ref.split('/')[-1] if '/' in ref else ref

    for entry in bundle.get('entry', []):
        resource = entry.get('resource', {})
        rtype = resource.get('resourceType')
        if rtype == 'Patient':
            pid = resource.get('id', '')
            if not pid:
                continue
            patient_map[pid] = {
                'patient': resource,
                'conditions': [],
                'procedures': [],
            }
            patient_aliases[pid] = pid
            patient_aliases[f'Patient/{pid}'] = pid
            full_url = (entry.get('fullUrl') or '').strip()
            if full_url:
                patient_aliases[full_url] = pid
                if full_url.startswith('urn:uuid:'):
                    patient_aliases[full_url[len('urn:uuid:'):]] = pid
        elif rtype == 'Condition':
            patient_id = _resolve_patient(resource.get('subject', {}).get('reference', ''))
            if patient_id in patient_map:
                patient_map[patient_id]['conditions'].append(resource)
        elif rtype == 'Procedure':
            patient_id = _resolve_patient(resource.get('subject', {}).get('reference', ''))
            if patient_id in patient_map:
                patient_map[patient_id]['procedures'].append(resource)

    return patient_map


class Command(BaseCommand):
    help = 'Backfill MM ConditionOccurrence rows and add synthetic ProcedureOccurrence rows for SYNTHEA-MM.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--bundle',
            default=DEFAULT_BUNDLE,
            help='Path to the MM FHIR bundle used to seed condition/procedure dates.',
        )
        parser.add_argument(
            '--org-slugs',
            default=DEFAULT_ORG_SLUGS,
            help='Comma-separated organization slugs to scope the cohort to.',
        )
        parser.add_argument(
            '--person-ids',
            default='',
            help='Comma-separated person_ids — overrides --org-slugs cohort selection.',
        )
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='Print planned changes without writing.',
        )
        parser.add_argument(
            '--confirm',
            action='store_true',
            help='Required to actually write synthetic data.',
        )
        parser.add_argument(
            '--min-procedures',
            type=int,
            default=MIN_PROCEDURES_PER_PATIENT,
            help='Minimum ProcedureOccurrence rows to retain per selected patient.',
        )

    def handle(self, *args, **options):
        dry_run = options['dry_run']
        confirm = options['confirm']
        if not dry_run and not confirm:
            raise CommandError('Re-run with --confirm to write synthetic MM enrichment rows.')

        patient_map = _parse_bundle(options['bundle'])

        org_slugs = [s.strip() for s in options['org_slugs'].split(',') if s.strip()]
        if options['person_ids']:
            person_ids = [int(x) for x in options['person_ids'].split(',') if x.strip()]
            cohort = list(PatientRecord.objects.filter(person_id__in=person_ids).select_related('person'))
        else:
            cohort = list(
                PatientRecord.objects.filter(organization__slug__in=org_slugs)
                .select_related('person')
                .order_by('person_id')
            )

        if not cohort:
            raise CommandError('No matching SYNTHEA-MM patients found.')

        self.stdout.write(
            f'Enriching {len(cohort)} SYNTHEA-MM patient(s) from {options["bundle"]}...'
        )

        bundle_by_key = {
            _patient_key_from_resource(entry['patient']): entry
            for entry in patient_map.values()
        }

        condition_creates = 0
        procedure_creates = 0
        refreshed = 0
        touched_person_ids = []

        ehr_type = _get_or_create_concept(
            concept_code='32817',
            concept_name='EHR',
            vocabulary_id='OMOP',
            domain_id='Type Concept',
            concept_class_id='Type Concept',
            concept_id=32817,
            create=not dry_run,
        )

        with suppress_patient_record_refresh():
            for idx, record in enumerate(cohort, 1):
                person = record.person
                bundle_key = _patient_key_from_person(person)
                source = bundle_by_key.get(bundle_key)
                if source is None:
                    self.stdout.write(
                        f'  [{idx}/{len(cohort)}] person_id={person.person_id} skipped (no bundle match)'
                    )
                    continue

                patient_resource = source['patient']
                diagnosis_date = record.diagnosis_date
                if diagnosis_date is None:
                    diagnosis_date = datetime.utcnow().date()
                condition_resource = source['conditions'][0] if source['conditions'] else None
                condition_date = None
                condition_code = None
                condition_name = 'Multiple Myeloma'
                clinical_status = None

                if condition_resource:
                    condition_codeable = condition_resource.get('code', {}) or {}
                    codings = condition_codeable.get('coding') or []
                    first_coding = codings[0] if codings else {}
                    condition_code = first_coding.get('code')
                    condition_name = (
                        condition_codeable.get('text')
                        or first_coding.get('display')
                        or condition_name
                    )
                    condition_date = _coerce_date(
                        condition_resource.get('onsetDateTime')
                        or condition_resource.get('recordedDate')
                        or condition_resource.get('assertedDate'),
                        diagnosis_date,
                    )
                    clinical_status = (
                        (condition_resource.get('clinicalStatus') or {})
                        .get('coding', [{}])[0]
                        .get('code')
                    )
                else:
                    condition_date = diagnosis_date

                if condition_code:
                    condition_concept = _get_or_create_concept(
                        concept_code=condition_code,
                        concept_name=condition_name,
                        vocabulary_id='SNOMED',
                        domain_id='Condition',
                        concept_class_id='Clinical Finding',
                        create=not dry_run,
                    )
                else:
                    condition_concept = _get_or_create_concept(
                        concept_code='SYNTH-MM-CONDITION',
                        concept_name='Multiple myeloma (synthetic enrichment)',
                        vocabulary_id=_LOCAL_VOCAB_ID,
                        domain_id='Condition',
                        concept_class_id='Clinical Finding',
                        create=not dry_run,
                    )

                with transaction.atomic():
                    existing_condition = ConditionOccurrence.objects.filter(
                        person=person,
                        condition_concept=condition_concept,
                        condition_start_date=condition_date,
                    ).first() if condition_concept else None
                    if existing_condition is None and condition_concept is not None:
                        if not dry_run:
                            ConditionOccurrence.objects.create(
                                condition_occurrence_id=next_pk(ConditionOccurrence, 'condition_occurrence_id'),
                                person=person,
                                condition_concept=condition_concept,
                                condition_start_date=condition_date,
                                condition_start_datetime=datetime.combine(condition_date, datetime.min.time()),
                                condition_type_concept=ehr_type,
                                condition_source_value='multiple myeloma',
                                condition_status_source_value=clinical_status,
                            )
                        condition_creates += 1

                    existing_procs = list(
                        ProcedureOccurrence.objects.filter(person=person)
                        .select_related('procedure_concept')
                        .order_by('procedure_date', 'procedure_occurrence_id')
                    )
                    existing_proc_keys = {
                        (proc.procedure_source_value or (proc.procedure_concept.concept_code if proc.procedure_concept else ''), proc.procedure_date)
                        for proc in existing_procs
                        if proc.procedure_date is not None
                    }

                    for proc_resource in source['procedures']:
                        codeable = proc_resource.get('code', {}) or {}
                        codings = codeable.get('coding') or []
                        first_coding = codings[0] if codings else {}
                        proc_code = first_coding.get('code')
                        proc_name = codeable.get('text') or first_coding.get('display') or 'FHIR Procedure'
                        proc_date = _coerce_date(
                            proc_resource.get('performedDateTime')
                            or proc_resource.get('performedPeriod', {}).get('start'),
                            condition_date,
                        )
                        if not proc_code or proc_date is None:
                            continue

                        proc_concept = _get_or_create_concept(
                            concept_code=proc_code,
                            concept_name=proc_name,
                            vocabulary_id='SNOMED',
                            domain_id='Procedure',
                            concept_class_id='Procedure',
                            create=not dry_run,
                        )
                        proc_key = (proc_code[:50], proc_date)
                        if proc_key in existing_proc_keys:
                            continue
                        if not dry_run:
                            ProcedureOccurrence.objects.create(
                                procedure_occurrence_id=next_pk(ProcedureOccurrence, 'procedure_occurrence_id'),
                                person=person,
                                procedure_concept=proc_concept,
                                procedure_date=proc_date,
                                procedure_datetime=datetime.combine(proc_date, datetime.min.time()),
                                procedure_type_concept=ehr_type,
                                procedure_source_value=proc_code[:50],
                                procedure_source_concept=proc_concept,
                            )
                        existing_proc_keys.add(proc_key)
                        procedure_creates += 1

                    current_count = ProcedureOccurrence.objects.filter(person=person).count() if not dry_run else len(existing_procs)
                    remaining = max(0, options['min_procedures'] - current_count)
                    if remaining:
                        synthetic_specs = _MM_PROCEDURE_SPECS[:remaining]
                        for proc_code, proc_name, offset_days in synthetic_specs:
                            proc_date = condition_date + timedelta(days=offset_days)
                            proc_concept = _get_or_create_concept(
                                concept_code=proc_code,
                                concept_name=proc_name,
                                vocabulary_id=_LOCAL_VOCAB_ID,
                                domain_id='Procedure',
                                concept_class_id='Procedure',
                                create=not dry_run,
                            )
                            proc_key = (proc_code, proc_date)
                            if proc_key in existing_proc_keys:
                                continue
                            if not dry_run:
                                ProcedureOccurrence.objects.create(
                                    procedure_occurrence_id=next_pk(ProcedureOccurrence, 'procedure_occurrence_id'),
                                    person=person,
                                    procedure_concept=proc_concept,
                                    procedure_date=proc_date,
                                    procedure_datetime=datetime.combine(proc_date, datetime.min.time()),
                                    procedure_type_concept=ehr_type,
                                    procedure_source_value=proc_code,
                                    procedure_source_concept=proc_concept,
                                )
                            existing_proc_keys.add(proc_key)
                            procedure_creates += 1

                touched_person_ids.append(person.person_id)
                self.stdout.write(
                    f'  [{idx}/{len(cohort)}] person_id={person.person_id} '
                    f'condition={"yes" if existing_condition is None else "no-op"} '
                    f'procedures={ProcedureOccurrence.objects.filter(person=person).count() if not dry_run else len(existing_proc_keys)}'
                )

        if dry_run:
            self.stdout.write(self.style.SUCCESS(
                f'Dry run complete. condition_creates={condition_creates} procedure_creates={procedure_creates}'
            ))
            return

        self.stdout.write(f'\nRefreshing PatientRecord for {len(touched_person_ids)} patients...')
        for person_id in touched_person_ids:
            close_old_connections()
            person = Person.objects.get(person_id=person_id)
            refresh_patient_record(person)
            refreshed += 1

        # ----------------------------------------------------------------
        # Completeness validation: report null critical fields per patient
        # ----------------------------------------------------------------
        _CRITICAL_MM_FIELDS = [
            'diagnosis_date',
            'stage',
            'first_line_therapy',
            'stem_cell_transplant_history',
            'hemoglobin_g_dl',
            'serum_calcium_mg_dl',
            'beta2_microglobulin',
            'monoclonal_protein_serum',
            'kappa_flc',
            'lambda_flc',
            'clonal_plasma_cells',
            'ecog_performance_status',
            'smoking_status',
            'wearable_last_sync_at',
        ]
        self.stdout.write('\nCompleteness check:')
        total_missing = 0
        perfect = 0
        for person_id in touched_person_ids:
            try:
                record = PatientRecord.objects.get(person_id=person_id)
            except PatientRecord.DoesNotExist:
                continue
            missing = [
                f for f in _CRITICAL_MM_FIELDS
                if not getattr(record, f, None)
            ]
            if missing:
                total_missing += len(missing)
                self.stdout.write(
                    f'  person_id={person_id} missing {len(missing)} field(s): {", ".join(missing)}'
                )
            else:
                perfect += 1
        self.stdout.write(
            f'  {perfect}/{len(touched_person_ids)} patients have all {len(_CRITICAL_MM_FIELDS)} '
            f'critical fields populated. Total null fields across cohort: {total_missing}.'
        )

        self.stdout.write(self.style.SUCCESS(
            f'Done. condition_creates={condition_creates} procedure_creates={procedure_creates} '
            f'refreshed={refreshed}'
        ))
