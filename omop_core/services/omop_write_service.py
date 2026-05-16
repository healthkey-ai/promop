# omop_core/services/omop_write_service.py
import logging
from datetime import date, datetime as dt

from omop_core.models import (
    Concept, Measurement, ConditionOccurrence, DrugExposure, ProcedureOccurrence,
)
from omop_oncology.models import Episode, EpisodeEvent
from omop_core.services.mappings import (
    LAB_FIELD_TO_LOINC,
    CONDITION_FIELDS,
    DEMOGRAPHIC_FIELDS,
    THERAPY_LINE_FIELDS,
    THERAPY_LINE_PREFIXES,
    CONCEPT_GENERIC_LAB,
    CONCEPT_LAB_TYPE,
    CONCEPT_EHR_TYPE,
    CONCEPT_TREATMENT_REGIMEN,
    CONCEPT_DRUG_EXPOSURE_FIELD,
    get_gender_concept,
)

logger = logging.getLogger('audit')


def _today():
    return date.today()


def sync_to_omop(patient_info, changed_fields: set, today: date = None, changed_data: dict = None) -> None:
    """
    Write PatientInfo changes through to OMOP tables.
    Never raises — failures are logged but must not block the HTTP response.

    changed_data: the raw request.data dict, used for fields that may be read-only
    on the serializer (e.g. gender, which is a SerializerMethodField).
    """
    if today is None:
        today = _today()
    if changed_data is None:
        changed_data = {}
    person = patient_info.person
    try:
        for field in changed_fields:
            value = getattr(patient_info, field, None)
            if value is None:
                value = changed_data.get(field)
            if field in LAB_FIELD_TO_LOINC and value is not None:
                _sync_measurement(person, field, value, today)
        if changed_fields & CONDITION_FIELDS:
            _sync_condition(person, patient_info, today, changed_data)
        if changed_fields & DEMOGRAPHIC_FIELDS:
            _sync_demographics(person, patient_info, changed_data)
        for line_number, prefix in THERAPY_LINE_PREFIXES.items():
            line_fields = {f'{prefix}_{s}' for s in ('therapy', 'start_date', 'end_date', 'outcome', 'intent', 'discontinuation_reason')}
            if changed_fields & line_fields:
                _sync_therapy_line(person, patient_info, line_number, prefix, today)
    except Exception as exc:
        logger.error('{"event": "omop_sync_error", "error": "%s"}', exc)


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
        existing._skip_patient_info_refresh = True
        existing.save(update_fields=['value_as_number'])
    else:
        last = Measurement.objects.order_by('-measurement_id').first()
        new_id = (last.measurement_id + 1) if last else 1
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
        m._skip_patient_info_refresh = True
        m.save()


def _sync_condition(person, patient_info, today: date, changed_data: dict = None) -> None:
    if changed_data is None:
        changed_data = {}
    disease = getattr(patient_info, 'disease', None) or changed_data.get('disease')
    stage = getattr(patient_info, 'stage', None) or changed_data.get('stage')
    icd10 = getattr(patient_info, 'condition_code_icd_10', None) or changed_data.get('condition_code_icd_10')
    snomed = getattr(patient_info, 'condition_code_snomed_ct', None) or changed_data.get('condition_code_snomed_ct')

    source_value = (disease or stage or icd10 or snomed or '')[:50]
    if not source_value:
        return

    type_concept = Concept.objects.filter(concept_id=CONCEPT_EHR_TYPE).first()
    if type_concept is None:
        return

    condition_concept = (
        Concept.objects.filter(concept_name__icontains=(disease or '')[:50]).first()
        if disease else None
    ) or type_concept

    last = ConditionOccurrence.objects.order_by('-condition_occurrence_id').first()
    new_id = (last.condition_occurrence_id + 1) if last else 1

    ConditionOccurrence.objects.create(
        condition_occurrence_id=new_id,
        person=person,
        condition_concept=condition_concept,
        condition_start_date=today,
        condition_type_concept=type_concept,
        condition_source_value=source_value,
    )


def _sync_demographics(person, patient_info, changed_data: dict = None) -> None:
    if changed_data is None:
        changed_data = {}
    update_fields = []

    # 'gender' on PatientInfo is a read-only SerializerMethodField so it may not be
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

    if not therapy_name:
        return

    episode_concept = Concept.objects.filter(concept_id=CONCEPT_TREATMENT_REGIMEN).first()
    if episode_concept is None:
        return

    # episode_type_concept and episode_object_concept are required FKs on Episode
    ehr_concept = Concept.objects.filter(concept_id=CONCEPT_EHR_TYPE).first() or episode_concept

    # Normalise start_date to a date object
    if start_date and isinstance(start_date, str):
        try:
            start_date = dt.strptime(start_date, '%Y-%m-%d').date()
        except ValueError:
            start_date = None

    # Upsert Episode: match by (person, episode_number, start_date) or just (person, episode_number)
    episode = None
    if start_date:
        episode = Episode.objects.filter(
            person=person,
            episode_number=line_number,
            episode_start_date=start_date,
        ).first()
    if episode is None:
        episode = Episode.objects.filter(
            person=person,
            episode_number=line_number,
        ).order_by('-episode_start_date').first()

    if episode:
        episode.episode_source_value = therapy_name[:50]
        if end_date:
            episode.episode_end_date = end_date
        episode.save(update_fields=['episode_source_value', 'episode_end_date'])
    else:
        last_ep = Episode.objects.order_by('-episode_id').first()
        new_ep_id = (last_ep.episode_id + 1) if last_ep else 1
        episode = Episode.objects.create(
            episode_id=new_ep_id,
            person=person,
            episode_concept=episode_concept,
            episode_object_concept=ehr_concept,
            episode_type_concept=ehr_concept,
            episode_start_date=start_date or today,
            episode_end_date=end_date,
            episode_number=line_number,
            episode_source_value=therapy_name[:50],
        )

    # Link unlinked DrugExposure rows within the episode date range
    ep_start = episode.episode_start_date
    ep_end = episode.episode_end_date

    drug_qs = DrugExposure.objects.filter(person=person)
    if ep_start:
        drug_qs = drug_qs.filter(drug_exposure_start_date__gte=ep_start)
    if ep_end:
        drug_qs = drug_qs.filter(drug_exposure_start_date__lte=ep_end)

    field_concept = Concept.objects.filter(concept_id=CONCEPT_DRUG_EXPOSURE_FIELD).first()
    if field_concept is None:
        return

    existing_event_ids = set(
        EpisodeEvent.objects.filter(
            episode_id=episode.episode_id,
            episode_event_field_concept=field_concept,
        ).values_list('event_id', flat=True)
    )

    for de in drug_qs:
        if de.drug_exposure_id not in existing_event_ids:
            EpisodeEvent.objects.get_or_create(
                episode_id=episode.episode_id,
                event_id=de.drug_exposure_id,
                defaults={'episode_event_field_concept': field_concept},
            )
