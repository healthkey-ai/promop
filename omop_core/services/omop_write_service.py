# omop_core/services/omop_write_service.py
import logging
from datetime import date, datetime as dt

from omop_core.models import (
    Concept, Measurement, ConditionOccurrence, DrugExposure, ProcedureOccurrence,
)
from omop_core.services.pk import next_pk
from omop_core.services.episode_service import upsert_therapy_line_episode
from omop_oncology.models import Episode, EpisodeEvent
from omop_core.services.mappings import (
    LAB_FIELD_TO_LOINC,
    LAB_FIELD_ALIAS_TO_CANONICAL,
    CONDITION_FIELDS,
    DEMOGRAPHIC_FIELDS,
    THERAPY_LINE_FIELDS,
    THERAPY_LINE_PREFIXES,
    STAGING_MEAS_FIELDS,
    CONCEPT_GENERIC_LAB,
    CONCEPT_LAB_TYPE,
    CONCEPT_EHR_TYPE,
    CONCEPT_TREATMENT_REGIMEN,
    CONCEPT_DRUG_EXPOSURE_FIELD,
    CONCEPT_PATIENT_REPORTED_TYPE,
    get_gender_concept,
)

logger = logging.getLogger('audit')


def _today():
    return date.today()


def sync_to_omop(patient_info, changed_fields: set, today: date = None, changed_data: dict = None) -> None:
    """
    Write PatientRecord changes through to OMOP tables.

    Raises on failure — callers must wrap in transaction.atomic() so that a
    failed OMOP write rolls back the PatientRecord save and the read-model never
    diverges from the OMOP source of truth.

    changed_data: the raw request.data dict, used for fields that may be read-only
    on the serializer (e.g. gender, which is a SerializerMethodField).
    """
    if today is None:
        today = _today()
    if changed_data is None:
        changed_data = {}
    person = patient_info.person
    for field in changed_fields:
        value = getattr(patient_info, field, None)
        if value is None:
            value = changed_data.get(field)
        # Normalize legacy lab field names to their canonical key. dev's #471 dedup moved the
        # legacy aliases (ldh/egfr/creatinine_mg_dl/…) OUT of LAB_FIELD_TO_LOINC into
        # LAB_FIELD_ALIAS_TO_CANONICAL; without this, the (cb-retained) write path would silently
        # skip a PATCH to a legacy name after the dev back-merge. Non-aliases pass through unchanged.
        lab_field = LAB_FIELD_ALIAS_TO_CANONICAL.get(field, field)
        if lab_field in LAB_FIELD_TO_LOINC and value is not None:
            _sync_measurement(person, lab_field, value, today)
    if changed_fields & CONDITION_FIELDS:
        _sync_condition(person, patient_info, today, changed_data)
    if changed_fields & DEMOGRAPHIC_FIELDS:
        _sync_demographics(person, patient_info, changed_data)
    for line_number, prefix in THERAPY_LINE_PREFIXES.items():
        line_fields = {f'{prefix}_{s}' for s in ('therapy', 'start_date', 'end_date', 'outcome', 'intent', 'discontinuation_reason')}
        if changed_fields & line_fields:
            _sync_therapy_line(person, patient_info, line_number, prefix, today)
    for field in changed_fields & set(STAGING_MEAS_FIELDS):
        value = getattr(patient_info, field, None)
        if value is None:
            value = changed_data.get(field)
        # Staging codes pass through verbatim — the reader returns value_as_string as-is.
        _sync_string_measurement(person, STAGING_MEAS_FIELDS[field], value, today)


def _sync_measurement(person, field_name: str, value, today: date) -> None:
    loinc_code, unit, display = LAB_FIELD_TO_LOINC[field_name]
    concept = (
        Concept.objects.filter(concept_code=loinc_code, vocabulary_id='LOINC').first()
        or Concept.objects.filter(concept_id=CONCEPT_GENERIC_LAB).first()
    )
    if concept is None:
        return
    type_concept = Concept.objects.filter(concept_id=CONCEPT_LAB_TYPE).first() or concept
    existing = Measurement.objects.filter(
        person=person,
        measurement_concept=concept,
        measurement_date=today,
    ).first()
    if existing:
        existing.value_as_number = value
        existing._skip_patient_record_refresh = True
        existing.save(update_fields=['value_as_number'])
        del existing._skip_patient_record_refresh
    else:
        new_id = next_pk(Measurement, 'measurement_id')
        m = Measurement(
            measurement_id=new_id,
            person=person,
            measurement_concept=concept,
            measurement_date=today,
            measurement_type_concept=type_concept,
            value_as_number=value,
            measurement_source_value=loinc_code,
            unit_source_value=unit,
        )
        m._skip_patient_record_refresh = True
        m.save()
        del m._skip_patient_record_refresh


def _patient_reported_type():
    """The 'Patient self-report' type concept (32865, vocab 'Type Concept') that patient-authored facts
    carry. Fail closed if it (or a look-alike in another vocab) is not the genuine row — the type is a
    scoping KEY, so a wrong concept here would let a CB edit clobber an imported clinical fact."""
    tc = Concept.objects.filter(
        concept_id=CONCEPT_PATIENT_REPORTED_TYPE, vocabulary_id='Type Concept').first()
    if tc is None:
        raise ValueError(
            f"type concept {CONCEPT_PATIENT_REPORTED_TYPE} (Patient self-report, vocab 'Type Concept') "
            "is not loaded")
    return tc


def _sync_string_measurement(person, loinc_code: str, value, today: date) -> None:
    """Upsert a patient-authored *string*-valued Measurement (a staging code or a normalised receptor
    status) keyed by the LOINC the derivation reads. The value lands in value_as_string (value_as_number
    is cleared, so the staging/receptor readers pick the string). The upsert key is scoped to the
    'Patient self-report' type, so a same-day IMPORTED Lab/EHR fact for the same concept is never
    clobbered (it carries a different type). Mirrors field_write_service's measurement upsert; a
    None/blank value is a no-op (a clear does not retract the prior fact — documented, same as labs)."""
    if value is None or value == '':
        return
    concept = (
        Concept.objects.filter(concept_code=loinc_code, vocabulary_id='LOINC').order_by('concept_id').first()
        or Concept.objects.filter(concept_id=CONCEPT_GENERIC_LAB).first()
    )
    if concept is None:
        return
    type_concept = _patient_reported_type()
    value_str = str(value)[:60]        # Measurement.value_as_string is 60 chars
    source_value = loinc_code[:50]     # measurement_source_value is 50 chars
    # measurement_source_value is part of the KEY, not just a stored field: when several staging LOINC
    # concepts are missing from a partially-loaded vocab they all resolve to the generic sentinel (0),
    # so without the source in the key stage/T/N/M would share one row and overwrite each other. The
    # reader matches on source_value too, so this keeps each field on its own row even in that fallback.
    keys = dict(person=person, measurement_concept=concept, measurement_date=today,
                is_erroneous=False, measurement_type_concept=type_concept,
                measurement_source_value=source_value)
    existing = Measurement.objects.filter(**keys).order_by('measurement_id').first()
    if existing is not None:
        existing.value_as_string = value_str
        existing.value_as_number = None
        existing._skip_patient_record_refresh = True
        existing.save(update_fields=['value_as_string', 'value_as_number'])
        del existing._skip_patient_record_refresh
        return
    m = Measurement(
        measurement_id=next_pk(Measurement, 'measurement_id'),
        value_as_string=value_str,
        **keys,
    )
    m._skip_patient_record_refresh = True
    m.save()
    del m._skip_patient_record_refresh


def _sync_condition(person, patient_info, today: date, changed_data: dict = None) -> None:
    if changed_data is None:
        changed_data = {}
    disease = getattr(patient_info, 'disease', None) or changed_data.get('disease')
    icd10 = getattr(patient_info, 'condition_code_icd_10', None) or changed_data.get('condition_code_icd_10')
    snomed = getattr(patient_info, 'condition_code_snomed_ct', None) or changed_data.get('condition_code_snomed_ct')

    # `stage` is NOT a condition identifier — it is written as its own staging Measurement (slice 2a).
    # It used to be a source_value fallback here, which meant a staging-only edit (no disease in the
    # adapter) minted a ConditionOccurrence named after the stage code (e.g. 'III') and could link a
    # diseaseless patient. A condition is sourced only from a real diagnosis field now.
    source_value = (disease or icd10 or snomed or '')[:50]
    if not source_value:
        return

    type_concept = Concept.objects.filter(concept_id=CONCEPT_EHR_TYPE).first()
    if type_concept is None:
        return

    # Resolve the condition concept deterministically. The previous
    # `concept_name__icontains(...).first()` had no ordering, so it could pick a
    # status/subtype variant (e.g. "Multiple myeloma in remission") arbitrarily.
    # Prefer an exact, standard concept; fall back to a deterministic substring
    # match ordered by concept_id; else the generic EHR type concept.
    _disease = (disease or '')[:50]
    condition_concept = None
    if _disease:
        _cond = Concept.objects.filter(domain_id='Condition')
        condition_concept = (
            _cond.filter(concept_name__iexact=_disease, standard_concept='S').order_by('concept_id').first()
            or _cond.filter(concept_name__iexact=_disease).order_by('concept_id').first()
            or _cond.filter(concept_name__icontains=_disease).order_by('concept_id').first()
        )
    condition_concept = condition_concept or type_concept

    # Upsert: if a row already exists for this person+source_value, update it
    # rather than appending a duplicate on every PATCH.
    existing_co = ConditionOccurrence.objects.filter(
        person=person,
        condition_source_value=source_value,
    ).first()
    if existing_co:
        existing_co.condition_start_date = today
        existing_co.condition_concept = condition_concept
        existing_co._skip_patient_record_refresh = True
        existing_co.save(update_fields=['condition_start_date', 'condition_concept_id'])
        del existing_co._skip_patient_record_refresh
        return

    new_id = next_pk(ConditionOccurrence, 'condition_occurrence_id')
    co = ConditionOccurrence(
        condition_occurrence_id=new_id,
        person=person,
        condition_concept=condition_concept,
        condition_start_date=today,
        condition_type_concept=type_concept,
        condition_source_value=source_value,
    )
    # Skip the post_save signal that calls refresh_patient_record — the PatientRecord
    # disease field was already saved correctly by the serializer; re-deriving it
    # from OMOP immediately would overwrite the user's selection with the OMOP
    # concept name (which may differ in casing or be unresolved).
    co._skip_patient_record_refresh = True
    co.save()
    del co._skip_patient_record_refresh


def _sync_demographics(person, patient_info, changed_data: dict = None) -> None:
    if changed_data is None:
        changed_data = {}
    update_fields = []

    # 'gender' on PatientRecord is a read-only SerializerMethodField so it may not be
    # updated on the model instance after save; fall back to the raw request value.
    gender_str = getattr(patient_info, 'gender', None) or changed_data.get('gender')
    if gender_str:
        concept = get_gender_concept(gender_str)
        if concept:
            person.gender_concept = concept
            person.gender_source_value = gender_str
            update_fields += ['gender_concept', 'gender_source_value']

    dob = getattr(patient_info, 'date_of_birth', None) or changed_data.get('date_of_birth')
    if dob:
        if isinstance(dob, str):
            try:
                dob = dt.strptime(dob, '%Y-%m-%d').date()
            except ValueError:
                dob = None
    if dob:
        person.year_of_birth = dob.year
        person.month_of_birth = dob.month
        person.day_of_birth = dob.day
        update_fields += ['year_of_birth', 'month_of_birth', 'day_of_birth']

    if update_fields:
        person.save(update_fields=update_fields)


def _sync_therapy_line(person, patient_info, line_number: int, prefix: str, today: date) -> None:
    therapy_name = getattr(patient_info, f'{prefix}_therapy', None)
    start_date = getattr(patient_info, f'{prefix}_start_date', None)
    end_date = getattr(patient_info, f'{prefix}_end_date', None)
    outcome = getattr(patient_info, f'{prefix}_outcome', None)
    # The caller (CB reverse-sync) may resolve the therapy slug to an OMOP concept via its own
    # taxonomy crosswalk and pass it as `{prefix}_therapy_concept_id`. Without it the episode's
    # object/source concept stays "no match" (0) and the derivation reads back therapy_id=None /
    # name="Unknown"; with it the resolved regimen concept flows into episode_object_concept and
    # episode_source_concept, so PatientRecord.{prefix}_therapy_id resolves and drug-specific
    # matching sees the regimen. A None/absent id keeps the prior name-only round-trip.
    therapy_concept_id = getattr(patient_info, f'{prefix}_therapy_concept_id', None)

    if not therapy_name:
        return

    if Concept.objects.filter(concept_id=CONCEPT_TREATMENT_REGIMEN).first() is None:
        return

    regimen_concept = (
        Concept.objects.filter(concept_id=therapy_concept_id).first()
        if therapy_concept_id else None
    )
    if therapy_concept_id and regimen_concept is None:
        # The caller resolved the slug to a concept its own taxonomy knows, but that concept row is absent
        # from the package Concept table (vocab/crosswalk skew). Fall through to the name-only episode, but
        # say so — otherwise the regimen silently reads back as "Unknown" despite the caller resolving it.
        logger.warning(
            '{"event": "therapy_concept_not_in_vocab", "line": %d, "concept_id": %s}',
            line_number, therapy_concept_id,
        )

    # Normalise start_date to a date object
    if start_date and isinstance(start_date, str):
        try:
            start_date = dt.strptime(start_date, '%Y-%m-%d').date()
        except ValueError:
            start_date = None

    # This reverse-sync path resolves drug links by date window rather than an
    # explicit id list, so it selects the DrugExposure ids here and hands them
    # to the shared Episode writer.
    field_concept = Concept.objects.filter(concept_id=CONCEPT_DRUG_EXPOSURE_FIELD).first()
    if field_concept is None:
        return

    existing_episode = Episode.objects.filter(person=person, episode_number=line_number).first()
    ep_start = (existing_episode.episode_start_date if existing_episode else None) or start_date
    ep_end = (existing_episode.episode_end_date if existing_episode else None) or end_date

    drug_qs = DrugExposure.objects.filter(person=person)
    if ep_start:
        drug_qs = drug_qs.filter(drug_exposure_start_date__gte=ep_start)
    if ep_end:
        drug_qs = drug_qs.filter(drug_exposure_start_date__lte=ep_end)

    # Exclude drugs already linked to a *different* episode to prevent
    # cross-episode contamination when end_date is absent.
    other_person_episode_ids = list(
        Episode.objects.filter(person=person)
        .exclude(episode_number=line_number)
        .values_list('episode_id', flat=True)
    )
    if other_person_episode_ids:
        other_episode_drug_ids = set(
            EpisodeEvent.objects.filter(
                episode_id__in=other_person_episode_ids,
                episode_event_field_concept=field_concept,
            ).values_list('event_id', flat=True)
        )
        drug_qs = drug_qs.exclude(drug_exposure_id__in=other_episode_drug_ids)

    drug_exposure_ids = list(drug_qs.values_list('drug_exposure_id', flat=True))

    # This path stores the human therapy name in episode_source_value rather
    # than the 'LOT-{n}' mirror, and persists the edited outcome to OMOP as a
    # LOT-{n}-outcome Observation.
    upsert_therapy_line_episode(
        person,
        line_number=line_number,
        # Both slots get the CB-resolved regimen concept: object_concept is the standard concept the
        # episode is about, source_concept is the (patient-asserted) source concept the derivation reads
        # first — a HemOnc Regimen there is reported 'asserted', anything else 'inferred'. None leaves the
        # historical no-match fallback untouched.
        regimen_concept=regimen_concept,
        regimen_source_concept=regimen_concept,
        start_date=start_date,
        end_date=end_date,
        drug_exposure_ids=drug_exposure_ids,
        outcome=outcome,
        source_value=therapy_name,
        today=today,
        # CB has no therapy end_date field, so a None here is "omitted", never "clear the end date".
        preserve_end_date_when_none=True,
        # CB is authoritative for its patients' therapy lines: a re-resolved regimen must replace an
        # existing source concept (else an A→B edit keeps deriving A). Still never clears — a null concept
        # (unmapped slug on the whole-line re-send) leaves any imported concept intact.
        overwrite_source_concept=True,
    )
