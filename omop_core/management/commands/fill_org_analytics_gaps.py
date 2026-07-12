"""
Management command: fill_org_analytics_gaps

Backfills OMOP and PatientRecord data that common org analytics depend on, with
an initial focus on the gaps tracked in GitHub issue #210 for BMM Foundation:

  - Survival by Subgroup
  - Duration of Response
  - Subgroup Forest Plot
  - Time to First Treatment
  - PFS chart cohort size unexpectedly low (for example n=1 for a 100-patient org)

The command is org-scoped and idempotent. For each selected PatientRecord it:

  1. backfills missing OMOP source rows used by analytics-critical fields;
  2. runs populate_patient_record so those source rows are reprojected;
  3. applies a narrow PatientRecord alias/fallback sync where populate does not;
  4. saves the record so computed therapy metadata stays consistent.

Usage:
    python manage.py fill_org_analytics_gaps --org-slugs bmm-foundation --dry-run
    python manage.py fill_org_analytics_gaps --org-slugs bmm-foundation --confirm
    python manage.py fill_org_analytics_gaps --org-slugs bmm-foundation,synthea-mm --confirm
"""
from __future__ import annotations

from collections import Counter
from datetime import date, datetime

from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError
from django.db import close_old_connections

from omop_core.models import (
    Concept,
    ConceptClass,
    ConditionOccurrence,
    Domain,
    DrugExposure,
    Observation,
    PatientRecord,
    Vocabulary,
)
from omop_core.services.lot_regimens import get_regimen_concept_id_by_name
from omop_core.services.pk import next_pk


_BEST_RESPONSE_RANK = {
    'Stringent Complete Response (sCR)': 6,
    'Complete Response (CR)': 5,
    'Complete Response': 5,
    'Very Good Partial Response (VGPR)': 4,
    'Partial Response (PR)': 3,
    'Partial Response': 3,
    'Minor Response (MR)': 2,
    'Minor Response': 2,
    'Stable Disease (SD)': 1,
    'Stable Disease': 1,
    'Progressive Disease (PD)': 0,
    'Progressive Disease': 0,
}

_BEST_RESPONSE_TO_CODE = {
    'Complete Response': ('182840001', 'Complete Response'),
    'Partial Response': ('182841002', 'Partial Response'),
    'Stable Disease': ('182843004', 'Stable Disease'),
    'Progressive Disease': ('182842009', 'Progressive Disease'),
}


def _sorted_unique_dates(values):
    return sorted({value for value in values if isinstance(value, date)})


def _best_response_from_outcomes(record: PatientRecord):
    outcomes = [
        record.first_line_outcome,
        record.second_line_outcome,
        record.later_outcome,
    ]
    ranked = [(outcome, _BEST_RESPONSE_RANK[outcome]) for outcome in outcomes if outcome in _BEST_RESPONSE_RANK]
    if not ranked:
        return None
    return max(ranked, key=lambda item: item[1])[0]


def _get_or_create_vocab(vocabulary_id, vocabulary_name):
    vocab, _ = Vocabulary.objects.get_or_create(
        vocabulary_id=vocabulary_id,
        defaults={
            'vocabulary_name': vocabulary_name,
            'vocabulary_reference': '',
            'vocabulary_version': 'analytics gap backfill',
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


def _get_or_create_concept(*, concept_code, concept_name, vocabulary_id, domain_id, concept_class_id, standard_concept='S', concept_id=None):
    if concept_id is not None:
        concept = Concept.objects.filter(concept_id=concept_id).first()
        if concept:
            return concept
    concept = Concept.objects.filter(concept_code=concept_code, vocabulary_id=vocabulary_id).first()
    if concept:
        return concept
    return Concept.objects.create(
        concept_id=concept_id or next_pk(Concept, 'concept_id'),
        concept_name=concept_name,
        domain=_get_or_create_domain(domain_id),
        vocabulary=_get_or_create_vocab(vocabulary_id, vocabulary_name=vocabulary_id),
        concept_class=_get_or_create_concept_class(concept_class_id),
        standard_concept=standard_concept,
        concept_code=concept_code,
        valid_start_date='1970-01-01',
        valid_end_date='2099-12-31',
    )


def _analytics_type_concept():
    return _get_or_create_concept(
        concept_code='32817',
        concept_name='EHR',
        vocabulary_id='OMOP',
        domain_id='Type Concept',
        concept_class_id='Type Concept',
        concept_id=32817,
    )


def _canonical_start_date(record: PatientRecord, prefix: str):
    return (
        getattr(record, f'{prefix}_start_date', None)
        or getattr(record, f'{prefix}_date', None)
    )


def _backfill_condition_occurrence(record: PatientRecord, type_concept) -> list[str]:
    condition_date = (
        record.diagnosis_date
        or _canonical_start_date(record, 'first_line')
        or _canonical_start_date(record, 'second_line')
        or _canonical_start_date(record, 'later')
        or _canonical_start_date(record, 'supportive_therapy')
    )
    if condition_date is None:
        return []
    if ConditionOccurrence.objects.filter(person=record.person, condition_start_date=condition_date).exists():
        return []
    condition_name = record.disease or record.disease_slug or 'Cancer diagnosis'
    concept_code = f'ANALYTICS-{(record.disease_slug or "generic-diagnosis").upper()[:40]}'
    condition_concept = _get_or_create_concept(
        concept_code=concept_code,
        concept_name=condition_name,
        vocabulary_id='LOCAL',
        domain_id='Condition',
        concept_class_id='Clinical Finding',
    )
    ConditionOccurrence.objects.create(
        condition_occurrence_id=next_pk(ConditionOccurrence, 'condition_occurrence_id'),
        person=record.person,
        condition_concept=condition_concept,
        condition_start_date=condition_date,
        condition_start_datetime=datetime.combine(condition_date, datetime.min.time()),
        condition_type_concept=type_concept,
        condition_source_value=(record.disease_slug or condition_name)[:50],
        condition_source_concept=condition_concept,
    )
    return ['condition_occurrence']


def _drug_concept_for_regimen(regimen_name: str):
    concept_id = get_regimen_concept_id_by_name(regimen_name) if regimen_name else None
    if concept_id:
        concept = Concept.objects.filter(concept_id=concept_id).first()
        if concept:
            return concept
    concept_code = f'REGIMEN-{(regimen_name or "unknown").upper()[:42]}'
    return _get_or_create_concept(
        concept_code=concept_code,
        concept_name=regimen_name or 'Unknown regimen',
        vocabulary_id='LOCAL',
        domain_id='Drug',
        concept_class_id='Ingredient',
    )


def _backfill_regimen_exposures(record: PatientRecord, type_concept) -> list[str]:
    created = []
    therapy_specs = [
        ('first_line', record.first_line_therapy, _canonical_start_date(record, 'first_line'), record.first_line_end_date),
        ('second_line', record.second_line_therapy, _canonical_start_date(record, 'second_line'), record.second_line_end_date),
        ('later', record.later_therapy, _canonical_start_date(record, 'later'), record.later_end_date),
    ]
    for label, regimen_name, start_date, end_date in therapy_specs:
        if not regimen_name or not start_date:
            continue
        if DrugExposure.objects.filter(
            person=record.person,
            drug_exposure_start_date=start_date,
            drug_source_value=(regimen_name or '')[:50],
        ).exists():
            continue
        regimen_concept = _drug_concept_for_regimen(regimen_name)
        DrugExposure.objects.create(
            drug_exposure_id=next_pk(DrugExposure, 'drug_exposure_id'),
            person=record.person,
            drug_concept=regimen_concept,
            drug_exposure_start_date=start_date,
            drug_exposure_start_datetime=datetime.combine(start_date, datetime.min.time()),
            drug_exposure_end_date=end_date or start_date,
            drug_exposure_end_datetime=datetime.combine((end_date or start_date), datetime.min.time()),
            drug_type_concept=type_concept,
            drug_source_value=regimen_name[:50],
            drug_source_concept=regimen_concept,
            sig=f'{label} regimen backfill',
        )
        created.append(f'{label}_drug_exposure')
    return created


def _backfill_best_response_observation(record: PatientRecord, type_concept) -> list[str]:
    desired_response = record.best_response or _best_response_from_outcomes(record)
    if desired_response not in _BEST_RESPONSE_TO_CODE:
        return []
    if Observation.objects.filter(
        person=record.person,
        observation_concept__concept_code__in=[code for code, _ in _BEST_RESPONSE_TO_CODE.values()],
    ).exists():
        return []
    obs_date = (
        record.last_treatment
        or _canonical_start_date(record, 'later')
        or _canonical_start_date(record, 'second_line')
        or _canonical_start_date(record, 'first_line')
        or record.diagnosis_date
    )
    if obs_date is None:
        return []
    concept_code, concept_name = _BEST_RESPONSE_TO_CODE[desired_response]
    response_concept = _get_or_create_concept(
        concept_code=concept_code,
        concept_name=concept_name,
        vocabulary_id='SNOMED',
        domain_id='Observation',
        concept_class_id='Clinical Observation',
    )
    Observation.objects.create(
        observation_id=next_pk(Observation, 'observation_id'),
        person=record.person,
        observation_concept=response_concept,
        observation_date=obs_date,
        observation_datetime=datetime.combine(obs_date, datetime.min.time()),
        observation_type_concept=type_concept,
        value_as_string=concept_name,
        observation_source_value=concept_code,
        observation_source_concept=response_concept,
        value_source_value=concept_name[:50],
    )
    return ['best_response_observation']


def _backfill_omop_rows(record: PatientRecord) -> list[str]:
    type_concept = _analytics_type_concept()
    created = []
    created.extend(_backfill_condition_occurrence(record, type_concept))
    created.extend(_backfill_regimen_exposures(record, type_concept))
    created.extend(_backfill_best_response_observation(record, type_concept))
    return created


def _apply_projection_backfills(record: PatientRecord) -> list[str]:
    changed_fields: list[str] = []

    date_pairs = [
        ('first_line_date', 'first_line_start_date'),
        ('second_line_date', 'second_line_start_date'),
        ('later_date', 'later_start_date'),
        ('supportive_therapy_date', 'supportive_therapy_start_date'),
    ]
    for alias_field, canonical_field in date_pairs:
        alias_value = getattr(record, alias_field)
        canonical_value = getattr(record, canonical_field)
        if canonical_value is None and alias_value is not None:
            setattr(record, canonical_field, alias_value)
            changed_fields.append(canonical_field)
        elif alias_value is None and canonical_value is not None:
            setattr(record, alias_field, canonical_value)
            changed_fields.append(alias_field)

    treatment_start_dates = _sorted_unique_dates([
        record.first_line_start_date,
        record.second_line_start_date,
        record.later_start_date,
        record.supportive_therapy_start_date,
        record.sct_date,
    ])
    treatment_end_dates = _sorted_unique_dates([
        record.first_line_end_date,
        record.second_line_end_date,
        record.later_end_date,
        record.supportive_therapy_end_date,
        record.sct_date,
    ])

    if record.diagnosis_date is None and treatment_start_dates:
        record.diagnosis_date = treatment_start_dates[0]
        changed_fields.append('diagnosis_date')

    if record.best_response is None:
        derived_response = _best_response_from_outcomes(record)
        if derived_response is not None:
            record.best_response = derived_response
            changed_fields.append('best_response')

    last_treatment_candidates = treatment_end_dates or treatment_start_dates
    if record.last_treatment is None and last_treatment_candidates:
        record.last_treatment = last_treatment_candidates[-1]
        changed_fields.append('last_treatment')

    return changed_fields


class Command(BaseCommand):
    help = 'Backfill OMOP rows, repopulate PatientRecords, and repair analytics-critical org data for issue #210 gaps'

    def add_arguments(self, parser):
        parser.add_argument(
            '--org-slugs',
            required=True,
            help='Comma-separated organization slugs to repair',
        )
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='Preview changes without writing them',
        )
        parser.add_argument(
            '--confirm',
            action='store_true',
            help='Persist changes to the database',
        )

    def handle(self, *args, **options):
        dry_run = options['dry_run']
        confirm = options['confirm']
        if not dry_run and not confirm:
            raise CommandError('Pass --dry-run to preview or --confirm to apply changes.')

        org_slugs = [slug.strip() for slug in options['org_slugs'].split(',') if slug.strip()]
        if not org_slugs:
            raise CommandError('At least one org slug is required.')

        records = list(
            PatientRecord.objects
            .filter(organization__slug__in=org_slugs)
            .select_related('person', 'organization')
            .order_by('organization__slug', 'person_id')
        )
        if not records:
            raise CommandError(f'No PatientRecord rows found for orgs: {", ".join(org_slugs)}')

        self.stdout.write(
            f'Processing {len(records)} PatientRecord(s) across {len(set(org_slugs))} org slug(s)...'
        )

        changed_records = 0
        refreshed_records = 0
        change_counter: Counter[str] = Counter()
        source_counter: Counter[str] = Counter()

        for index, record in enumerate(records, 1):
            close_old_connections()
            if dry_run:
                refreshed = record
                created_sources = []
            else:
                created_sources = _backfill_omop_rows(record)
                source_counter.update(created_sources)
                call_command(
                    'populate_patient_record',
                    person_id=record.person_id,
                    force_update=True,
                )
                refreshed = PatientRecord.objects.get(person_id=record.person_id)
                refreshed_records += 1

            changed_fields = _apply_projection_backfills(refreshed)
            if changed_fields:
                changed_records += 1
                change_counter.update(changed_fields)
                if dry_run:
                    self.stdout.write(
                        f'  [{index}/{len(records)}] org={record.organization.slug} '
                        f'person_id={record.person_id} would update: {", ".join(changed_fields)}'
                    )
                else:
                    refreshed.save()
                    self.stdout.write(
                        f'  [{index}/{len(records)}] org={record.organization.slug} '
                        f'person_id={record.person_id} '
                        f'source_rows={created_sources or ["none"]} '
                        f'updated: {", ".join(changed_fields)}'
                    )
            elif dry_run:
                self.stdout.write(
                    f'  [{index}/{len(records)}] org={record.organization.slug} '
                    f'person_id={record.person_id} no-op'
                )

        summary = (
            f'Complete. refreshed={refreshed_records} changed_records={changed_records} '
            f'source_rows={dict(sorted(source_counter.items()))} '
            f'field_updates={dict(sorted(change_counter.items()))}'
        )
        if dry_run:
            self.stdout.write(self.style.WARNING(summary))
        else:
            self.stdout.write(self.style.SUCCESS(summary))
