# omop_core/services/episode_service.py
"""Canonical writer for line-of-therapy Episodes.

Every path that materialises a line of therapy into OMOP — the FHIR upload
handler, the synthetic MM/BC enrichment commands, and the PatientRecord→OMOP
reverse sync — funnels its Episode/EpisodeEvent/outcome writes through
``upsert_therapy_line_episode`` so the CDM tagging is identical everywhere:

  * ``episode_concept``      = 32531 Treatment Regimen
  * ``episode_type_concept`` = 32817 EHR
  * ``episode_number``       = the line number (also the idempotency key)
  * ``episode_source_value`` = ``LOT-{n}`` (human-readable mirror)

Idempotency key is ``(person, episode_number)`` — there is exactly one Episode
per line of therapy per person. Previously the FHIR/enrich paths keyed on
``episode_source_value`` while the reverse-sync keyed on
``(person, episode_number, start_date)``; both collapse to this.
"""
import logging

from omop_core.models import Concept, Observation
from omop_core.services.pk import next_pk
from omop_core.services.mappings import (
    CONCEPT_EHR_TYPE,
    CONCEPT_TREATMENT_REGIMEN,
    CONCEPT_DRUG_EXPOSURE_FIELD,
)
from omop_oncology.models import Episode, EpisodeEvent

logger = logging.getLogger('audit')

# Sentinel distinguishing "caller did not supply a value" from "caller
# explicitly passed None (= clear the field)."  Default arguments use this
# so that ``end_date=None`` means "clear", while omitting the argument
# entirely means "leave as stored."
_UNSET = object()


class TherapyLineEpisodeResult:
    """Outcome of upsert_therapy_line_episode.

    episode     — the Episode row (or None if the upsert was skipped)
    created     — True if the Episode row was newly inserted
    event_ids   — EpisodeEvent pks touched (created or pre-existing links)
    """

    __slots__ = ('episode', 'created', 'event_ids',
                 # Attached by author_therapy_line, which also writes the
                 # exposures the episode groups.
                 'drug_exposure_ids', 'drugs_created')

    def __init__(self, episode, created, event_ids):
        self.episode = episode
        self.created = created
        self.event_ids = event_ids

# SNOMED CT response-assessment codes for per-line outcomes. Anything without a
# mapping (e.g. "Very Good Partial Response") still records value_as_string.
OUTCOME_SNOMED_CODES = {
    'Complete Response': '182840001',
    'Partial Response': '182841002',
    'Stable Disease': '182843004',
    'Progressive Disease': '182842009',
}


def _concept(concept_id):
    return Concept.objects.filter(concept_id=concept_id).first()


def upsert_therapy_line_episode(
    person,
    *,
    line_number,
    regimen_concept=None,
    regimen_source_concept=None,
    start_date=_UNSET,
    end_date=_UNSET,
    drug_exposure_ids=(),
    outcome=None,
    intent=None,
    discontinuation_reason=None,
    source_value=None,
    today=None,
    replace_events=False,
    disease=None,
    episode=None,
):
    """Upsert one line-of-therapy Episode and its links; return the Episode.

    Args:
        person: OMOP Person.
        line_number: LOT number (1, 2, 3…). Becomes episode_number; with the
            cancer it treats, the (person, cancer, line_number) idempotency key.
        disease: the cancer this line treats, as a disease name or slug. None
            means the person's primary cancer (every caller before #1739).
        episode: an existing line to update in place (an edit by episode id);
            it is never re-found by number, which another cancer may share.
        regimen_concept: resolved regimen Concept (HemOnc/RxNav/local). Used as
            episode_object_concept; falls back to concept 0.
        regimen_source_concept: Concept for episode_source_concept (typically the
            same HemOnc concept when it came from the source), else None.
        start_date / end_date: date objects or ``_UNSET`` (= leave as stored).
            An explicit None clears end_date; the required start_date is
            retained when None is supplied.
        drug_exposure_ids: iterable of drug_exposure_id to link via EpisodeEvent.
        outcome: optional outcome string → LOT-{n}-outcome Observation.
        source_value: episode_source_value to store. Defaults to 'LOT-{n}'.
            Callers that record a human name or phase-labelled regimen
            (reverse sync, inference) pass their own value.
        today: fallback for episode_start_date when start_date is None.
        replace_events: when True, the supplied drug_exposure_ids are the full
            membership of the episode and stale EpisodeEvent links are removed.

    Returns a TherapyLineEpisodeResult; result.episode is None if the required
    Treatment Regimen concept is absent.
    """
    tx_regimen_concept = _concept(CONCEPT_TREATMENT_REGIMEN)
    if tx_regimen_concept is None:
        logger.warning(
            '{"event": "episode_upsert_skipped", "reason": "missing_treatment_regimen_concept", "line": %d}',
            line_number,
        )
        return TherapyLineEpisodeResult(None, False, [])
    ehr_type_concept = _concept(CONCEPT_EHR_TYPE) or tx_regimen_concept
    no_match_concept = _concept(0)
    # episode_object_concept is a required (non-null) FK. Prefer the resolved
    # regimen, then the standard "no matching concept" (0), then EHR type — the
    # last two are the historical fallbacks of the enrich and reverse-sync
    # paths respectively, and ehr_type_concept is always present here.
    object_concept = regimen_concept or no_match_concept or ehr_type_concept

    episode_source_value = (source_value or f'LOT-{line_number}')[:50]

    # Resolve sentinels for the create path: _UNSET → None (no date known yet).
    effective_start = None if start_date is _UNSET else start_date
    effective_end = None if end_date is _UNSET else end_date

    parent, adopts_unparented = _line_parent(person, disease, episode)
    if episode is None:
        episode = _find_line(person, line_number, parent, adopts_unparented)
    if episode is not None and parent is not None and episode.episode_parent_id is None and adopts_unparented:
        # A line written before #1739 reached through the primary cancer.
        episode.episode_parent_id = parent.episode_id
        episode.save(update_fields=['episode_parent_id'])
    created = episode is None
    if episode is None:
        episode = Episode(
            episode_id=next_pk(Episode, 'episode_id'),
            person=person,
            episode_concept=tx_regimen_concept,
            episode_object_concept=object_concept,
            episode_type_concept=ehr_type_concept,
            episode_start_date=effective_start or today,
            episode_end_date=effective_end,
            episode_number=line_number,
            episode_parent_id=parent.episode_id if parent else None,
            episode_source_value=episode_source_value,
            episode_source_concept=regimen_source_concept,
        )
        episode.save()
    else:
        # Update fields the caller explicitly supplied (including None = clear).
        # _UNSET means "not supplied" and leaves the stored value untouched.
        dirty = []
        if episode.episode_source_value != episode_source_value:
            episode.episode_source_value = episode_source_value
            dirty.append('episode_source_value')
        if regimen_concept and episode.episode_object_concept_id != regimen_concept.concept_id:
            episode.episode_object_concept = regimen_concept
            dirty.append('episode_object_concept')
        if regimen_source_concept and not episode.episode_source_concept_id:
            episode.episode_source_concept = regimen_source_concept
            dirty.append('episode_source_concept')
        if effective_start is not None and episode.episode_start_date != effective_start:
            episode.episode_start_date = effective_start
            dirty.append('episode_start_date')
        if end_date is not _UNSET and episode.episode_end_date != effective_end:
            episode.episode_end_date = effective_end
            dirty.append('episode_end_date')
        if dirty:
            episode.save(update_fields=dirty)

    field_concept = _concept(CONCEPT_DRUG_EXPOSURE_FIELD) or tx_regimen_concept
    event_ids = []
    desired_exposure_ids = {de_id for de_id in drug_exposure_ids if de_id is not None}
    if replace_events:
        stale = (
            EpisodeEvent.objects
            .filter(
                episode_id=episode.episode_id,
                episode_event_field_concept=field_concept,
            )
            .exclude(event_id__in=desired_exposure_ids)
        )
        stale.delete()

    for de_id in drug_exposure_ids:
        if de_id is None:
            continue
        ee, _ = EpisodeEvent.objects.get_or_create(
            episode_id=episode.episode_id,
            event_id=de_id,
            defaults={'episode_event_field_concept': field_concept},
        )
        event_ids.append(ee.pk)

    if outcome:
        _upsert_outcome_observation(person, line_number, outcome, ehr_type_concept, no_match_concept,
                                    obs_date=effective_end or effective_start or today,
                                    line=episode, adopt=adopts_unparented)
    elif replace_events:
        _delete_outcome_observation(person, line_number, line=episode, adopt=adopts_unparented)

    obs_date = effective_end or effective_start or today
    if intent:
        _upsert_line_observation(person, line_number, 'intent', intent,
                                 ehr_type_concept, no_match_concept, obs_date=obs_date,
                                 line=episode, adopt=adopts_unparented)
    elif replace_events:
        _delete_line_observation(person, line_number, 'intent', line=episode, adopt=adopts_unparented)

    if discontinuation_reason:
        _upsert_line_observation(person, line_number, 'discontinuation', discontinuation_reason,
                                 ehr_type_concept, no_match_concept, obs_date=obs_date,
                                 line=episode, adopt=adopts_unparented)
    elif replace_events:
        _delete_line_observation(person, line_number, 'discontinuation', line=episode, adopt=adopts_unparented)

    return TherapyLineEpisodeResult(episode, created, event_ids)


def _line_parent(person, disease, episode):
    """The Disease Episode a line hangs off, and whether it adopts unparented lines.

    Unparented lines (written before #1739) are the primary cancer's, so only a
    write for the primary cancer may claim them.
    """
    from omop_core.services.disease_episodes import (
        disease_episode, disease_slug, find_disease_episode, primary_disease_slug,
    )

    primary = primary_disease_slug(person)
    if episode is not None:
        parent = Episode.objects.filter(episode_id=episode.episode_parent_id).first() \
            if episode.episode_parent_id else None
        is_primary = parent is None or parent == find_disease_episode(person, primary)
        return parent, is_primary
    slug = disease_slug(disease) if disease else primary
    return disease_episode(person, slug), slug == primary


def _find_line(person, line_number, parent, adopts_unparented):
    from omop_core.services.disease_episodes import regimen_episodes

    lines = regimen_episodes(person).filter(episode_number=line_number)
    if parent is not None:
        found = lines.filter(episode_parent_id=parent.episode_id).first()
        if found is not None or not adopts_unparented:
            return found
    return lines.filter(episode_parent_id__isnull=True).first()


def _line_observations(person, src_value, line, adopt):
    """A line's LOT-{n}-* rows: its own, or (for the primary cancer) a pre-#1739 unlinked one."""
    rows = Observation.objects.filter(person=person, observation_source_value=src_value)
    if line is None:
        return rows
    own = rows.filter(observation_event_id=line.episode_id)
    if own.exists() or not adopt:
        return own
    return rows.filter(observation_event_id__isnull=True)


def _upsert_outcome_observation(person, line_number, outcome, type_concept, no_match_concept, obs_date,
                                line=None, adopt=True):
    src_value = f'LOT-{line_number}-outcome'
    snomed_code = OUTCOME_SNOMED_CODES.get(outcome)
    outcome_concept = (
        Concept.objects.filter(concept_code=snomed_code, vocabulary_id='SNOMED').first()
        if snomed_code else None
    ) or no_match_concept
    if outcome_concept is None or type_concept is None:
        return
    value = outcome[:60]

    existing = _line_observations(person, src_value, line, adopt).first()
    if existing:
        if line is not None and existing.observation_event_id != line.episode_id:
            existing.observation_event_id = line.episode_id
            existing._skip_patient_record_refresh = True
            existing.save(update_fields=['observation_event_id'])
        # Keep OMOP authoritative when an outcome is edited (e.g. PR -> CR);
        # a no-op when the value is unchanged (ingest re-runs stay idempotent).
        dirty = []
        if existing.value_as_string != value:
            existing.value_as_string = value
            dirty.append('value_as_string')
        if existing.observation_concept_id != outcome_concept.concept_id:
            existing.observation_concept = outcome_concept
            dirty.append('observation_concept')
        if obs_date and existing.observation_date != obs_date:
            existing.observation_date = obs_date
            dirty.append('observation_date')
        if dirty:
            existing._skip_patient_record_refresh = True
            existing.save(update_fields=dirty)
        return

    obs = Observation(
        observation_id=next_pk(Observation, 'observation_id'),
        person=person,
        observation_concept=outcome_concept,
        observation_date=obs_date,
        observation_type_concept=type_concept,
        value_as_string=value,
        observation_source_value=src_value,
        observation_event_id=line.episode_id if line is not None else None,
    )
    obs._skip_patient_record_refresh = True
    obs.save()


def _delete_outcome_observation(person, line_number, line=None, adopt=True):
    _line_observations(person, f'LOT-{line_number}-outcome', line, adopt).delete()


def _upsert_line_observation(person, line_number, suffix, value, type_concept, no_match_concept, obs_date,
                             line=None, adopt=True):
    """Upsert a LOT-{n}-{suffix} Observation (intent, discontinuation, etc.)."""
    src_value = f'LOT-{line_number}-{suffix}'
    obs_concept = no_match_concept
    if obs_concept is None or type_concept is None:
        return
    text = value[:60]

    existing = _line_observations(person, src_value, line, adopt).first()
    if existing:
        dirty = []
        if line is not None and existing.observation_event_id != line.episode_id:
            existing.observation_event_id = line.episode_id
            dirty.append('observation_event_id')
        if existing.value_as_string != text:
            existing.value_as_string = text
            dirty.append('value_as_string')
        if obs_date and existing.observation_date != obs_date:
            existing.observation_date = obs_date
            dirty.append('observation_date')
        if dirty:
            existing._skip_patient_record_refresh = True
            existing.save(update_fields=dirty)
        return

    obs = Observation(
        observation_id=next_pk(Observation, 'observation_id'),
        person=person,
        observation_concept=obs_concept,
        observation_date=obs_date,
        observation_type_concept=type_concept,
        value_as_string=text,
        observation_source_value=src_value,
        observation_event_id=line.episode_id if line is not None else None,
    )
    obs._skip_patient_record_refresh = True
    obs.save()


def _delete_line_observation(person, line_number, suffix, line=None, adopt=True):
    _line_observations(person, f'LOT-{line_number}-{suffix}', line, adopt).delete()


def author_therapy_line(
    person,
    *,
    line_number,
    drugs=(),
    start_date=None,
    end_date=None,
    regimen_concept_id=None,
    outcome=None,
    intent=None,
    discontinuation_reason=None,
    source_value=None,
    replace=False,
    disease=None,
    episode=None,
):
    """Record a line of therapy: its drug exposures, its Episode, and the links.

    A line of therapy is not one row. It is a set of DrugExposures grouped by an
    Episode through EpisodeEvent, and every therapy field on ``PatientRecord`` --
    ``first_line_therapy``, ``therapy_lines_count``, the intents, the outcomes,
    ``treatment_refractory_status`` -- is read back out of that grouping by
    regimen inference. There is no column to write.

    That is three POSTs with concept ids a browser has no business knowing, and
    ``Episode`` additionally needs a client-supplied primary key,
    ``episode_object_concept`` and ``episode_type_concept``. Doing it here means
    a caller sends what a clinician knows -- which line, which drugs, which dates
    -- and the CDM tagging stays identical to every other path, because the
    grouping still goes through ``upsert_therapy_line_episode``.

    Idempotent in both halves. The Episode keys on ``(person, cancer, line_number)``
    (the cancer is ``disease``, the primary one when omitted; #1739), and
    a drug exposure keys on ``(drug_source_value, drug_exposure_start_date)`` --
    the same identity the bulk write path and FHIR ingest use, so re-sending a
    line converges instead of stacking duplicates.

    Signal suppression is internalised: every DrugExposure save and Observation
    delete fires post_save/post_delete, each of which would trigger a full
    PatientRecord refresh. The suppression defers that to the single
    refresh_patient_record call the caller makes after this returns.

    Args:
        person: OMOP Person.
        line_number: LOT number (1, 2, 3…).
        drugs: iterable of ``{'concept_id': int, 'source_value': str|None}``.
        start_date / end_date: ``date`` objects or None.
        regimen_concept_id: resolved regimen concept, used as
            ``episode_object_concept`` and asserted via ``episode_source_concept``.
        outcome: optional outcome string → ``LOT-{n}-outcome`` Observation.
        source_value: ``episode_source_value``; defaults to ``LOT-{n}``.
        replace: when True, the submitted drugs and outcome are the complete
            edited state of the line, so stale EpisodeEvent/outcome rows are
            removed.
        disease: the cancer the line treats (name or slug); None = primary.
        episode: the line being edited, updated in place rather than re-found
            by number (which another cancer's line may share).

    Returns a TherapyLineEpisodeResult with two extra attributes attached:
    ``drug_exposure_ids`` and ``drugs_created``.
    """
    from omop_core.models import DrugExposure
    from omop_core.signals import suppress_patient_record_refresh

    with suppress_patient_record_refresh():
        return _author_therapy_line_inner(
            person,
            line_number=line_number,
            drugs=drugs,
            start_date=start_date,
            end_date=end_date,
            regimen_concept_id=regimen_concept_id,
            outcome=outcome,
            intent=intent,
            discontinuation_reason=discontinuation_reason,
            source_value=source_value,
            replace=replace,
            disease=disease,
            episode=episode,
        )


def _author_therapy_line_inner(
    person,
    *,
    line_number,
    drugs=(),
    start_date=None,
    end_date=None,
    regimen_concept_id=None,
    outcome=None,
    intent=None,
    discontinuation_reason=None,
    source_value=None,
    replace=False,
    disease=None,
    episode=None,
):
    from omop_core.models import DrugExposure

    ehr_type = _concept(CONCEPT_EHR_TYPE)
    exposure_ids = []
    created = 0

    for drug in drugs:
        concept_id = drug.get('concept_id') if isinstance(drug, dict) else drug
        concept = _concept(concept_id)
        if concept is None:
            # A drug whose concept is not loaded cannot be written, and silently
            # dropping it would produce a line that looks complete and infers the
            # wrong regimen. Refuse the whole line instead.
            raise ValueError(f'Unknown drug concept_id {concept_id}')

        raw = (drug.get('source_value') if isinstance(drug, dict) else None)
        raw = (raw or concept.concept_name or '')[:50]

        existing = DrugExposure.objects.filter(
            person=person,
            drug_source_value=raw,
            drug_exposure_start_date=start_date,
        ).order_by('drug_exposure_id').first()
        if existing is not None:
            exposure_ids.append(existing.drug_exposure_id)
            continue

        exposure = DrugExposure(
            drug_exposure_id=next_pk(DrugExposure, 'drug_exposure_id'),
            person=person,
            drug_concept=concept,
            drug_exposure_start_date=start_date,
            drug_exposure_end_date=end_date,
            drug_type_concept=ehr_type,
            drug_source_value=raw,
        )
        exposure.save()
        exposure_ids.append(exposure.drug_exposure_id)
        created += 1

    regimen_concept = _concept(regimen_concept_id) if regimen_concept_id else None
    result = upsert_therapy_line_episode(
        person,
        line_number=line_number,
        regimen_concept=regimen_concept,
        # Asserted, not inferred: the caller named this regimen, and derivation
        # reads episode_source_concept to decide which of the two it is.
        regimen_source_concept=regimen_concept,
        start_date=start_date,
        end_date=end_date,
        drug_exposure_ids=exposure_ids,
        outcome=outcome,
        intent=intent,
        discontinuation_reason=discontinuation_reason,
        source_value=source_value,
        replace_events=replace,
        disease=disease,
        episode=episode,
    )
    result.drug_exposure_ids = exposure_ids
    result.drugs_created = created
    return result
