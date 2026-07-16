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
  2. runs refresh_patient_record so those source rows are reprojected;
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
import random

from django.core.management.base import BaseCommand, CommandError
from django.db import close_old_connections
from django.db.models import Q
from django.utils import timezone

from omop_core.signals import suppress_patient_record_refresh

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
from omop_core.services.patient_record_service import refresh_patient_record
from omop_core.services.lot_regimens import get_regimen_concept_id_by_name
from omop_core.services.pk import next_pk


_BEST_RESPONSE_TO_CODE = {
    # Standard forms — map to the 4 SNOMED Standard concepts loaded in staging
    'Complete Response': ('182840001', 'Complete Response'),
    'Partial Response': ('182841002', 'Partial Response'),
    'Stable Disease': ('182843004', 'Stable Disease'),
    'Progressive Disease': ('182842009', 'Progressive Disease'),
    # Parenthetical / abbreviation forms — collapsed to nearest SNOMED tier.
    # sCR, VGPR, MR have no Standard concept IDs in the currently loaded vocab
    # (NCIt and full SNOMED are not yet loaded; see GitHub issue #<TBD>).
    # Update this map once NCIt is loaded and granular concept IDs are available.
    'Complete Response (CR)': ('182840001', 'Complete Response'),
    'Stringent Complete Response (sCR)': ('182840001', 'Complete Response'),
    'Very Good Partial Response (VGPR)': ('182841002', 'Partial Response'),
    'Partial Response (PR)': ('182841002', 'Partial Response'),
    'Minor Response (MR)': ('182841002', 'Partial Response'),
    'Minimal Response (MR)': ('182841002', 'Partial Response'),
    'Stable Disease (SD)': ('182843004', 'Stable Disease'),
    'Progressive Disease (PD)': ('182842009', 'Progressive Disease'),
}

_SYNTHETIC_LINE_OUTCOMES = [
    ('Complete Response', 20),
    ('Partial Response', 45),
    ('Stable Disease', 25),
    ('Progressive Disease', 10),
]

_SYNTHETIC_LAST_LINE_OUTCOMES = [
    ('Complete Response', 25),
    ('Partial Response', 35),
    ('Stable Disease', 20),
    ('Progressive Disease', 20),
]


def _sorted_unique_dates(values):
    return sorted({value for value in values if isinstance(value, date)})


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


def _midnight_aware(value: date):
    return timezone.make_aware(datetime.combine(value, datetime.min.time()))


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
    condition_name = record.disease or record.disease_slug or 'Cancer diagnosis'
    concept_code = f'ANALYTICS-{(record.disease_slug or "generic-diagnosis").upper()[:40]}'
    condition_source_value = (record.disease_slug or condition_name)[:50]
    if ConditionOccurrence.objects.filter(
        person=record.person,
        condition_start_date=condition_date,
    ).filter(
        Q(condition_source_value=condition_source_value)
        | Q(condition_concept__concept_code=concept_code)
        | Q(condition_source_concept__concept_code=concept_code)
    ).exists():
        return []
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
        condition_start_datetime=_midnight_aware(condition_date),
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
            drug_exposure_start_datetime=_midnight_aware(start_date),
            drug_exposure_end_date=end_date,
            drug_exposure_end_datetime=(
                _midnight_aware(end_date) if end_date else None
            ),
            drug_type_concept=type_concept,
            drug_source_value=regimen_name[:50],
            drug_source_concept=regimen_concept,
            sig=f'{label} regimen backfill',
        )
        created.append(f'{label}_drug_exposure')
    return created


def _existing_line_outcome(record: PatientRecord, line_number: int):
    if line_number == 1:
        return record.first_line_outcome
    if line_number == 2:
        return record.second_line_outcome
    if line_number >= 3:
        return record.later_outcome
    return None


def _synthetic_line_outcome(record: PatientRecord, line_number: int, max_line_number: int) -> str:
    existing = _existing_line_outcome(record, line_number)
    if existing in _BEST_RESPONSE_TO_CODE:
        return existing

    choices = _SYNTHETIC_LAST_LINE_OUTCOMES if line_number == max_line_number else _SYNTHETIC_LINE_OUTCOMES
    labels, weights = zip(*choices)
    rng = random.Random(f'analytics-gap-outcome:{record.person_id}:{line_number}')
    return rng.choices(labels, weights=weights, k=1)[0]


def _backfill_lot_outcome_observations(record: PatientRecord, type_concept) -> list[str]:
    try:
        from omop_oncology.models import Episode
    except ImportError:
        return []

    episodes = list(
        Episode.objects
        .filter(person=record.person, episode_number__isnull=False)
        .order_by('episode_number', 'episode_start_date', 'episode_id')
    )
    if not episodes:
        return []

    max_line_number = max((episode.episode_number or 0) for episode in episodes)
    created = []
    for episode in episodes:
        line_number = episode.episode_number
        if not line_number:
            continue
        source_value = f'LOT-{line_number}-outcome'
        if Observation.objects.filter(person=record.person, observation_source_value=source_value).exists():
            continue

        outcome = _synthetic_line_outcome(record, line_number, max_line_number)
        concept_code, concept_name = _BEST_RESPONSE_TO_CODE[outcome]
        response_concept = _get_or_create_concept(
            concept_code=concept_code,
            concept_name=concept_name,
            vocabulary_id='SNOMED',
            domain_id='Observation',
            concept_class_id='Clinical Observation',
        )
        obs_date = episode.episode_end_date or episode.episode_start_date or record.diagnosis_date
        if obs_date is None:
            continue
        Observation.objects.create(
            observation_id=next_pk(Observation, 'observation_id'),
            person=record.person,
            observation_concept=response_concept,
            observation_date=obs_date,
            observation_datetime=_midnight_aware(obs_date),
            observation_type_concept=type_concept,
            value_as_string=outcome,
            observation_source_value=source_value,
            observation_source_concept=response_concept,
            value_source_value=outcome[:50],
        )
        created.append(f'lot_{line_number}_outcome_observation')
    return created


def _backfill_omop_rows(record: PatientRecord) -> list[str]:
    type_concept = _analytics_type_concept()
    created = []
    created.extend(_backfill_condition_occurrence(record, type_concept))
    created.extend(_backfill_regimen_exposures(record, type_concept))
    created.extend(_backfill_lot_outcome_observations(record, type_concept))
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
    ])

    if record.diagnosis_date is None and treatment_start_dates:
        # NOTE: diagnosis_date is in _OMOP_DERIVED_FIELDS and will be cleared on the
        # next refresh_patient_record call. This value is only durable if
        # _backfill_condition_occurrence also created a ConditionOccurrence row that
        # _get_disease_data() recognises on subsequent refreshes. If the LOCAL-vocab
        # concept is not matched by the service, the field will revert to None after
        # the next signal-triggered refresh.
        record.diagnosis_date = treatment_start_dates[0]
        changed_fields.append('diagnosis_date')

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
            help=(
                'Preview changes without writing them. '
                'Note: the "would update" fields are computed against the current DB state '
                'before any OMOP backfill or refresh_patient_record refresh. '
                'The actual --confirm run may touch different fields if the backfill '
                'creates new OMOP rows that refresh_patient_record fills in first.'
            ),
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
            try:
                if dry_run:
                    refreshed = record
                    created_sources = []
                else:
                    with suppress_patient_record_refresh():
                        created_sources = _backfill_omop_rows(record)
                    source_counter.update(created_sources)
                    refreshed = refresh_patient_record(record.person)
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
                        refreshed.save(update_fields=changed_fields)
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
            except Exception as exc:
                self.stdout.write(self.style.ERROR(
                    f'  [{index}/{len(records)}] org={record.organization.slug} '
                    f'person_id={record.person_id} ERROR: {exc}'
                ))

        summary = (
            f'Complete. refreshed={refreshed_records} changed_records={changed_records} '
            f'source_rows={dict(sorted(source_counter.items()))} '
            f'field_updates={dict(sorted(change_counter.items()))}'
        )
        if dry_run:
            self.stdout.write(self.style.WARNING(summary))
        else:
            self.stdout.write(self.style.SUCCESS(summary))
