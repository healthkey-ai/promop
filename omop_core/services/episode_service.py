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
from datetime import date
from typing import Any, Iterable, Mapping
from time import sleep as _sleep
from django.db import transaction

from omop_core.models import Concept, Observation, Person
from omop_core.services.pk import next_pk
from omop_core.services.mappings import (
    CONCEPT_EHR_TYPE,
    CONCEPT_TREATMENT_REGIMEN,
    CONCEPT_DRUG_EXPOSURE_FIELD,
)
from omop_oncology.models import Episode, EpisodeEvent
from omop_core.services.therapy_transactions import (
    TherapyLineRejected, lock_therapy_patients, run_therapy_write,
)
from omop_core.signals import suppress_patient_record_refresh

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
    outcome=_UNSET,
    intent=_UNSET,
    discontinuation_reason=_UNSET,
    source_value=None,
    today=None,
    replace_events=False,
    replace_metadata=None,
    disease=None,
    episode=None,
    preserve_end_date_when_none=False,
    overwrite_source_concept=False,
    assert_line=True,
    asserted_by='authoring',
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
        overwrite_source_concept: when True, a provided regimen_source_concept replaces an
            existing episode_source_concept (the CB reverse-sync, authoritative for its own
            patients); default False keeps "fill only if empty" so import paths never clobber
            an asserted source concept. A None concept never clears either way.
        preserve_end_date_when_none: compatibility for callers where None means
            "omitted" (the CB reverse-sync); explicit _UNSET still means leave
            unchanged, and the default preserves explicit None-as-clear behavior.
        drug_exposure_ids: iterable of drug_exposure_id to link via EpisodeEvent.
        outcome: optional outcome string → LOT-{n}-outcome Observation.
        source_value: episode_source_value to store. Defaults to 'LOT-{n}'.
            Callers that record a human name or phase-labelled regimen
            (reverse sync, inference) pass their own value.
        today: fallback for episode_start_date when start_date is None.
        replace_events: when True, the supplied drug_exposure_ids are the full
            membership of the episode and stale EpisodeEvent links are removed.
        assert_line: record that this line happened, even when no composition
            is supplied (#1924). Every canonical authoring path keeps the default;
            a caller that only maintains an existing header may turn it off.
        asserted_by: provenance of the assertion ('authoring', 'reverse-sync',
            'import'), stored, never used for clinical decisions.

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
        # An unfiled line that a writer has now named the (primary) cancer for.
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
        # `overwrite_source_concept=True` (the CB reverse-sync, which IS the source of truth for its own
        # patients) lets a re-resolved regimen replace an existing source concept — without it, editing a
        # line from regimen A to a different resolved regimen B updates episode_object_concept but leaves
        # episode_source_concept on A, and the derivation (which reads the source slot first) keeps
        # reporting A. The default stays "fill only if empty" so the enrich/FHIR import paths never clobber
        # a concept they asserted from the source. A None here is always "not provided, keep what's there"
        # (mirrors preserve_end_date_when_none): the whole-line re-send carries a null concept for every
        # unmapped slug, and clearing on that would destroy an imported regimen concept on an unrelated
        # (e.g. outcome-only) edit — so we never clear, only overwrite with another resolved concept.
        if (regimen_source_concept
                and (overwrite_source_concept or not episode.episode_source_concept_id)
                and episode.episode_source_concept_id != regimen_source_concept.concept_id):
            episode.episode_source_concept = regimen_source_concept
            dirty.append('episode_source_concept')
        if effective_start is not None and episode.episode_start_date != effective_start:
            episode.episode_start_date = effective_start
            dirty.append('episode_start_date')
        if (end_date is not _UNSET
                and not (preserve_end_date_when_none and effective_end is None)
                and episode.episode_end_date != effective_end):
            episode.episode_end_date = effective_end
            dirty.append('episode_end_date')
        if dirty:
            episode.save(update_fields=dirty)

    if assert_line:
        _assert_therapy_line(episode, asserted_by)

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

    for de_id in sorted(desired_exposure_ids):
        ee, _ = EpisodeEvent.objects.get_or_create(
            episode_id=episode.episode_id,
            event_id=de_id,
            episode_event_field_concept=field_concept,
        )
        event_ids.append(ee.pk)

    if replace_metadata is None:
        replace_metadata = replace_events
    if outcome is not _UNSET and outcome:
        _upsert_outcome_observation(person, line_number, outcome, ehr_type_concept, no_match_concept,
                                    obs_date=effective_end or effective_start or today,
                                    line=episode, adopt=adopts_unparented)
    elif outcome is not _UNSET and replace_metadata:
        _delete_outcome_observation(person, line_number, line=episode, adopt=adopts_unparented)

    obs_date = effective_end or effective_start or today
    if intent is not _UNSET and intent:
        _upsert_line_observation(person, line_number, 'intent', intent,
                                 ehr_type_concept, no_match_concept, obs_date=obs_date,
                                 line=episode, adopt=adopts_unparented)
    elif intent is not _UNSET and replace_metadata:
        _delete_line_observation(person, line_number, 'intent', line=episode, adopt=adopts_unparented)

    if discontinuation_reason is not _UNSET and discontinuation_reason:
        _upsert_line_observation(person, line_number, 'discontinuation', discontinuation_reason,
                                 ehr_type_concept, no_match_concept, obs_date=obs_date,
                                 line=episode, adopt=adopts_unparented)
    elif discontinuation_reason is not _UNSET and replace_metadata:
        _delete_line_observation(person, line_number, 'discontinuation', line=episode, adopt=adopts_unparented)

    return TherapyLineEpisodeResult(episode, created, event_ids)


def _assert_therapy_line(episode, source):
    """Record that this Episode is a therapy line that happened (#1924).

    Saving the line again undoes a retraction: the writer is asserting it now.
    Composition is deliberately not recorded here; it is read from the line's
    typed links, so an unstated composition stays unknown.
    """
    from django.utils import timezone
    from omop_oncology.models import TherapyLineAssertion

    assertion, created = TherapyLineAssertion.objects.get_or_create(
        episode=episode, defaults={'source': source},
    )
    if created:
        return assertion
    updates = {}
    if assertion.state != TherapyLineAssertion.ASSERTED:
        updates = {'state': TherapyLineAssertion.ASSERTED, 'retracted_at': None}
    if source and assertion.source != source:
        updates['source'] = source
    if updates:
        for field, value in updates.items():
            setattr(assertion, field, value)
        assertion.save(update_fields=list(updates))
    return assertion


def retract_therapy_line(person, episode):
    """Withdraw the assertion that this line happened, keeping the row for audit.

    A patient or clinician correcting a line entered in error retracts it; the
    composition may also be cleared without retracting, when treatment happened
    but the drugs are unknown. Retracting does not delete exposures: they may be
    imported facts that outlive this line's claim about them.
    """
    from django.utils import timezone
    from omop_oncology.models import TherapyLineAssertion

    if episode is None or episode.person_id != person.pk:
        raise TherapyLineRejected('The therapy line must belong to this patient.')
    assertion, _ = TherapyLineAssertion.objects.get_or_create(
        episode=episode, defaults={'source': 'authoring'},
    )
    if assertion.state != TherapyLineAssertion.RETRACTED:
        assertion.state = TherapyLineAssertion.RETRACTED
        assertion.retracted_at = timezone.now()
        assertion.save(update_fields=['state', 'retracted_at'])
    return assertion


def _line_parent(person, disease, episode):
    """The Disease Episode a line hangs off, and whether it is the primary cancer's.

    A line no one names a cancer for stays unparented: it is the primary
    cancer's, whichever that is when it is read. A line for a named cancer hangs
    off that cancer's Disease Episode; for a cancer other than the primary one
    it must be a cancer on the record, and the Episode vocabulary must be
    loaded, or the line could not be told apart from the primary cancer's.
    """
    from omop_core.services.disease_episodes import (
        disease_episode, disease_key, disease_keys_on_record, disease_slug, primary_disease_ids,
        primary_disease_slug,
    )

    if episode is not None:
        if episode.episode_parent_id is None:
            return None, True
        parent = Episode.objects.filter(episode_id=episode.episode_parent_id).first()
        return parent, parent is None or parent.episode_id in primary_disease_ids(person)
    if not disease:
        return None, True
    slug = disease_slug(disease)
    if slug and disease_key(slug) == disease_key(primary_disease_slug(person)):
        return disease_episode(person, slug), True  # None without the vocabulary: stays unparented
    if not slug or disease_key(slug) not in disease_keys_on_record(person):
        raise TherapyLineRejected(f'This record has no diagnosis of {disease!r} to file a line of therapy under.')
    parent = disease_episode(person, slug)
    if parent is None:
        raise TherapyLineRejected(
            'A line of therapy for a second cancer needs the Episode vocabulary '
            '(Disease First Occurrence, 32528) loaded.'
        )
    return parent, False


def _find_line(person, line_number, parent, is_primary):
    from omop_core.services.disease_episodes import primary_lines, regimen_episodes

    if not is_primary:
        return regimen_episodes(person).filter(
            episode_number=line_number, episode_parent_id=parent.episode_id).first()
    lines = primary_lines(person).filter(episode_number=line_number)
    if parent is not None:
        own = lines.filter(episode_parent_id=parent.episode_id).first()
        if own is not None:
            return own
    return lines.order_by('episode_id').first()


def _episode_field():
    """The CDM field concept a LOT observation's observation_event_id points at."""
    from omop_core.services.disease_episodes import cdm_field_concept

    return cdm_field_concept('episode.episode_id')


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

    field = _episode_field() if line is not None else None
    existing = _line_observations(person, src_value, line, adopt).first()
    if existing:
        if line is not None and (existing.observation_event_id != line.episode_id
                                 or existing.obs_event_field_concept_id != (field.concept_id if field else None)):
            existing.observation_event_id = line.episode_id
            existing.obs_event_field_concept = field
            existing._skip_patient_record_refresh = True
            existing.save(update_fields=['observation_event_id', 'obs_event_field_concept'])
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
        obs_event_field_concept=field,
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

    field = _episode_field() if line is not None else None
    existing = _line_observations(person, src_value, line, adopt).first()
    if existing:
        dirty = []
        if line is not None and existing.observation_event_id != line.episode_id:
            existing.observation_event_id = line.episode_id
            dirty.append('observation_event_id')
        if line is not None and existing.obs_event_field_concept_id != (field.concept_id if field else None):
            existing.obs_event_field_concept = field
            dirty.append('obs_event_field_concept')
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
        obs_event_field_concept=field,
    )
    obs._skip_patient_record_refresh = True
    obs.save()


def _delete_line_observation(person, line_number, suffix, line=None, adopt=True):
    _line_observations(person, f'LOT-{line_number}-{suffix}', line, adopt).delete()


def author_therapy_line(person, *, line_number, drugs=_UNSET, **kwargs):
    """Atomically author a line; retry only PostgreSQL lock conflicts in full.

    Bulk clinical updates may acquire a fact before refreshing PatientRecord.
    Never wait for that fact while holding the projection lock: NOWAIT causes
    a full savepoint rollback, releasing this attempt's locks before backoff.
    """
    if drugs is not _UNSET:
        drugs = list(drugs)  # A generator must describe the same intent on retry.
    return run_therapy_write(
        lambda: _author_therapy_line_once(person, line_number=line_number, drugs=drugs, **kwargs),
        backoff=_sleep,
    )


def author_therapy_lines(
    person: Person, lines: Iterable[Mapping[str, Any]], *, asserted_by: str = 'authoring',
) -> list[TherapyLineEpisodeResult] | None:
    """Author several lines of one person as one all-or-nothing write.

    Each entry holds ``author_therapy_line`` kwargs without ``person``.
    Returns None and writes nothing when the Treatment Regimen concept is not loaded.
    """
    # Copied once so every retry replays the same input
    lines = [dict(line) for line in lines]

    # A lock conflict retries the whole batch so a retry never applies half of it
    def once() -> list[TherapyLineEpisodeResult] | None:
        with suppress_patient_record_refresh():
            lock_therapy_patients([person.pk])
            results = []
            for index, line in enumerate(lines):
                try:
                    result = _author_therapy_line_inner(person, asserted_by=asserted_by, **line)
                except TherapyLineRejected as exc:
                    raise TherapyLineRejected(exc.message, index=index) from exc
                if result.episode is None:
                    transaction.set_rollback(True)
                    return None
                results.append(result)
            return results

    return run_therapy_write(once, backoff=_sleep)


def _author_therapy_line_once(
    person,
    *,
    line_number,
    drugs=_UNSET,
    start_date=_UNSET,
    end_date=_UNSET,
    regimen_concept_id=_UNSET,
    outcome=_UNSET,
    intent=_UNSET,
    discontinuation_reason=_UNSET,
    source_value=_UNSET,
    replace=False,
    disease=None,
    episode=None,
    asserted_by='authoring',
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

    Idempotent in both halves. The Episode keys on ``(person, cancer, line_number)``.
    Private TherapyLineExposure metadata proves which facts this writer owns.
    Existing administrations are identified by drug_exposure_id; legacy source
    names resolve only when unambiguous within this line. Concept/date edits
    update only exclusive owned facts. Imports are never adopted, and shared
    facts are copied before edits.

    Signal suppression is internalised: every DrugExposure save and Observation
    delete fires post_save/post_delete, each of which would trigger a full
    PatientRecord refresh. The suppression defers that to the single
    refresh_patient_record call the caller makes after this returns.

    Args:
        person: OMOP Person.
        line_number: LOT number (1, 2, 3…).
        drugs: iterable of concept_id/source_value, with drug_exposure_id for
            existing administrations and optional individual start_date/end_date.
            Omitted drug dates preserve existing facts; only new facts default
            to the line's dates. A line-date edit never rewrites administration dates.
        start_date / end_date: dates; omission preserves, None clears end_date.
            A new line must have a known start_date: no clinical date is invented.
        regimen_concept_id: resolved regimen concept, used as
            ``episode_object_concept`` and asserted via ``episode_source_concept``.
        outcome: optional outcome string → ``LOT-{n}-outcome`` Observation.
        source_value: ``episode_source_value``; defaults to ``LOT-{n}``.
        replace: submitted fields replace their previous state; omitted fields
            are preserved. Removed exclusive owned facts are retained as
            entered-in-error, never deleted. Imported/shared facts are untouched.
        disease: the cancer the line treats (name or slug); None = primary.
        episode: the line being edited, updated in place rather than re-found
            by number (which another cancer's line may share).
        asserted_by: source recorded on the TherapyLineAssertion
            ('authoring' for the editor, 'import' for an ETL feed).

    Returns a TherapyLineEpisodeResult with two extra attributes attached:
    ``drug_exposure_ids`` and ``drugs_created``.
    """
    with suppress_patient_record_refresh():
        # Match the projection-before-person order of patient edits. Serializing
        # all authoring for one person also protects the find/create line key.
        lock_therapy_patients([person.pk])
        result = _author_therapy_line_inner(
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
            asserted_by=asserted_by,
        )
        if result.episode is None:
            transaction.set_rollback(True)
        return result


def _author_therapy_line_inner(
    person,
    *,
    line_number,
    drugs=_UNSET,
    start_date=_UNSET,
    end_date=_UNSET,
    regimen_concept_id=_UNSET,
    outcome=_UNSET,
    intent=_UNSET,
    discontinuation_reason=_UNSET,
    source_value=_UNSET,
    replace=False,
    disease=None,
    episode=None,
    asserted_by='authoring',
):
    from omop_core.models import DrugExposure
    from omop_oncology.models import TherapyLineExposure

    if _concept(CONCEPT_TREATMENT_REGIMEN) is None:
        return TherapyLineEpisodeResult(None, False, [])
    ehr_type = _concept(CONCEPT_EHR_TYPE)
    if ehr_type is None or _concept(CONCEPT_DRUG_EXPOSURE_FIELD) is None:
        raise TherapyLineRejected('The EHR type and drug-exposure field vocabulary must be loaded.')
    if not isinstance(line_number, int) or line_number < 1:
        raise TherapyLineRejected('line_number must be a positive integer.')

    if episode is not None:
        try:
            episode = Episode.objects.select_for_update(nowait=True).get(
                pk=episode.pk, person=person, episode_concept_id=CONCEPT_TREATMENT_REGIMEN,
                episode_number=line_number,
            )
        except Episode.DoesNotExist as exc:
            raise TherapyLineRejected('The therapy line must belong to this person and line number.') from exc
    else:
        parent, primary = _line_parent(person, disease, None)
        episode = _find_line(person, line_number, parent, primary)
        if episode is not None:
            episode = Episode.objects.select_for_update(nowait=True).get(pk=episode.pk)

    start = (episode.episode_start_date if episode else None) if start_date is _UNSET else start_date
    end = (episode.episode_end_date if episode else None) if end_date is _UNSET else end_date
    if start and end and end < start:
        raise TherapyLineRejected('end_date cannot precede start_date.')
    if source_value is _UNSET:
        source_value = episode.episode_source_value if episode else None
    if regimen_concept_id is _UNSET:
        regimen = episode.episode_source_concept if episode else None
        object_regimen = episode.episode_object_concept if episode else None
    else:
        if regimen_concept_id is not None and regimen_concept_id <= 0:
            raise TherapyLineRejected(f'Unknown regimen concept_id {regimen_concept_id}')
        regimen = _concept(regimen_concept_id) if regimen_concept_id else None
        if regimen_concept_id and regimen is None:
            raise TherapyLineRejected(f'Unknown regimen concept_id {regimen_concept_id}')
        if regimen is None and episode is not None and (
            episode.episode_source_concept_id or episode.episode_object_concept_id not in (
                None, 0, CONCEPT_EHR_TYPE, CONCEPT_TREATMENT_REGIMEN,
            )
        ):
            # Unknown-regimen assertions need #1924's explicit representation.
            # Until then refuse, rather than silently retain A or invent a code.
            raise TherapyLineRejected('Clearing a regimen requires the unknown-regimen assertion workflow.')
        object_regimen = regimen

    linked = list(DrugExposure.objects.filter(
        person=person, is_erroneous=False,
        drug_exposure_id__in=EpisodeEvent.objects.filter(
            episode_id=episode.pk if episode else None,
            episode_event_field_concept_id=CONCEPT_DRUG_EXPOSURE_FIELD,
        ).values('event_id'),
    ))
    composition_supplied = drugs is not _UNSET
    validated = {}
    for drug in drugs if composition_supplied else ():
        concept_id = drug.get('concept_id') if isinstance(drug, dict) else drug
        concept = _concept(concept_id)
        if concept is None or concept_id <= 0:
            raise TherapyLineRejected(f'Unknown drug concept_id {concept_id}')
        values = drug if isinstance(drug, dict) else {'concept_id': drug}
        ref = values.get('drug_exposure_id')
        if ref is not None and (not isinstance(ref, int) or isinstance(ref, bool) or ref <= 0):
            raise TherapyLineRejected('drug_exposure_id must be a positive integer.')
        raw = values.get('source_value')
        raw = (raw or concept.concept_name or '')[:50]
        key = ('id', ref) if ref is not None else ('source', raw)
        if key in validated and (ref is not None or validated[key][0] != values):
            raise TherapyLineRejected(f'Duplicate drug source_value {raw!r}; use distinct administration IDs.')
        validated[key] = (values, concept, raw)
    if not start:
        raise TherapyLineRejected('A therapy line requires a known start_date; no date is fabricated.')
    if start > date.today():
        raise TherapyLineRejected('A future therapy line is planned, not received treatment.')

    # Establish the line before exposures. Any later failure rolls back this
    # whole service call, including direct (non-HTTP) callers.
    result = upsert_therapy_line_episode(
        person, line_number=line_number, regimen_concept=object_regimen,
        regimen_source_concept=regimen, overwrite_source_concept=True,
        start_date=start, end_date=end, outcome=outcome, intent=intent,
        discontinuation_reason=discontinuation_reason, source_value=source_value,
        disease=disease, episode=episode,
        replace_events=replace and composition_supplied,
        replace_metadata=replace,
        asserted_by=asserted_by,
    )
    if result.episode is None:
        raise TherapyLineRejected('The treatment-regimen vocabulary disappeared during authoring.')
    line = result.episode
    if not composition_supplied:
        # A line interval is not every administration's interval. Preserve exact
        # links/IDs/dates on metadata-only edits, including imported repeat doses.
        result.drug_exposure_ids = [de.pk for de in linked]
        result.drugs_created = 0
        return result
    # Link INSERT/UPDATE takes the same fact-row lock via the migration trigger,
    # including bulk imports and the generic EpisodeEvent API. Lock in PK order
    # before checking exclusivity; a link cannot appear between check and edit.
    fact_ids = set(TherapyLineExposure.objects.filter(episode=line).values_list('drug_exposure_id', flat=True))
    fact_ids.update(de.pk for de in linked)
    locked_facts = {de.pk: de for de in DrugExposure.objects.select_for_update(nowait=True).filter(
        pk__in=fact_ids,
    ).order_by('pk')}
    linked = [locked_facts[de.pk] for de in linked
              if de.pk in locked_facts and not locked_facts[de.pk].is_erroneous]
    exposure_ids = []
    created = 0

    used_ids = set()
    for values, concept, raw in validated.values():
        ref = values.get('drug_exposure_id')
        if ref is not None:
            existing = next((de for de in linked if de.pk == ref), None)
            if existing is None:
                raise TherapyLineRejected('drug_exposure_id must identify an active drug already linked to this line.')
        else:
            candidates = [de for de in linked if de.drug_source_value == raw]
            if not candidates:
                candidates = [entry.drug_exposure for entry in
                    TherapyLineExposure.objects.filter(episode=line, source_value=raw)
                    .select_related('drug_exposure')]
            if len(candidates) > 1:
                raise TherapyLineRejected('Ambiguous drug source_value; supply drug_exposure_id for each administration.')
            existing = candidates[0] if candidates else None
        if existing is not None:
            if existing.pk in used_ids:
                raise TherapyLineRejected('Duplicate drug_exposure_id in the submitted composition.')
            used_ids.add(existing.pk)
            if ref is not None:
                raw = values.get('source_value', existing.drug_source_value)
            elif 'source_value' not in values:
                raw = existing.drug_source_value
        owned = TherapyLineExposure.objects.filter(
            episode=line, drug_exposure_id=existing.pk if existing else None).first()
        drug_start = values.get('start_date', existing.drug_exposure_start_date if existing else start)
        drug_end = values.get('end_date', existing.drug_exposure_end_date if existing else end)
        if not drug_start or (drug_end and drug_end < drug_start):
            raise TherapyLineRejected('Drug dates require a known start_date and end_date cannot precede it.')
        if drug_start > date.today():
            raise TherapyLineRejected('A future drug administration is planned, not received treatment.')
        shared = existing is not None and EpisodeEvent.objects.filter(
            event_id=existing.pk, episode_event_field_concept_id=CONCEPT_DRUG_EXPOSURE_FIELD,
        ).exclude(episode_id=line.pk).exists()
        if existing is not None and existing.person_id != person.pk:
            raise TherapyLineRejected('Exposure ownership does not match the patient.')
        if existing is not None and (existing.drug_concept_id == concept.pk
                       and existing.drug_source_value == raw
                       and existing.drug_exposure_start_date == drug_start
                       and existing.drug_exposure_end_date == drug_end
                       and not existing.is_erroneous):
            exposure_ids.append(existing.pk)
            continue
        if existing is None or shared or owned is None:
            existing = DrugExposure.objects.create(
                drug_exposure_id=next_pk(DrugExposure, 'drug_exposure_id'), person=person,
                drug_concept=concept, drug_exposure_start_date=drug_start, drug_exposure_end_date=drug_end,
                drug_type_concept=ehr_type, drug_source_value=raw,
            )
            if owned is not None:
                owned.drug_exposure = existing
                owned.source_value = raw or ''
                owned.save(update_fields=['drug_exposure', 'source_value'])
            else:
                TherapyLineExposure.objects.create(episode=line, source_value=raw or '', drug_exposure=existing)
            created += 1
        else:
            updates = dict(drug_concept_id=concept.pk, drug_source_value=raw,
                           drug_exposure_start_date=drug_start, drug_exposure_end_date=drug_end,
                           is_erroneous=False, erroneous_reason=None)
            dirty = [key for key, value in updates.items() if getattr(existing, key) != value]
            if dirty:
                for key, value in updates.items():
                    setattr(existing, key, value)
                existing.save(update_fields=dirty)
            if owned.source_value != (raw or ''):
                owned.source_value = raw or ''
                owned.save(update_fields=['source_value'])
        exposure_ids.append(existing.pk)

    links = upsert_therapy_line_episode(
        person, line_number=line_number, episode=line, source_value=line.episode_source_value,
        drug_exposure_ids=exposure_ids, replace_events=replace,
        asserted_by=asserted_by,
    )
    if replace:
        for owned in TherapyLineExposure.objects.filter(episode=line).exclude(
            drug_exposure_id__in=exposure_ids,
        ).select_related('drug_exposure'):
            old = owned.drug_exposure
            shared = EpisodeEvent.objects.filter(
                event_id=old.pk, episode_event_field_concept_id=CONCEPT_DRUG_EXPOSURE_FIELD,
            ).exclude(episode_id=line.pk).exists()
            if old.person_id != person.pk:
                raise TherapyLineRejected('Exposure ownership does not match the patient.')
            if not shared and not old.is_erroneous:
                old.is_erroneous = True
                old.erroneous_reason = 'Superseded by an explicit therapy-line edit.'
                old.save(update_fields=['is_erroneous', 'erroneous_reason'])
    result.event_ids = links.event_ids
    result.drug_exposure_ids = exposure_ids
    result.drugs_created = created
    return result
