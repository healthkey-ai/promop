"""Disease Episodes: the cancer a line of therapy treats (#1739).

A line of therapy is a Treatment Regimen Episode (32531). Following the OMOP
Oncology convention, each one hangs off a Disease Episode (32528, "Disease
First Occurrence") through ``episode_parent_id``; the Disease Episode's
``episode_object_concept`` is the cancer's diagnosis concept. One Disease
Episode per cancer, keyed by the same disease slug ``PatientRecord`` derives,
so line numbering is per ``(person, cancer)`` and a second cancer has its own
line 1.

Lines written before this existed have no parent. They belong to the person's
primary cancer: the backfill migration attaches them, and any writer that
reaches one through the primary cancer adopts it on the way.

``PatientRecord``'s flat therapy fields (first/second/later line, line count,
prior therapy) stay the **primary** cancer's: trial eligibility counts prior
lines for the cancer being matched.
"""
from __future__ import annotations

import logging

from django.db.models import Q

from omop_core.models import ConditionOccurrence, Concept, PatientRecord
from omop_core.services.mappings import (
    CONCEPT_DISEASE_FIRST_OCCURRENCE,
    CONCEPT_EHR_TYPE,
)
from omop_core.services.pk import next_pk
from omop_oncology.models import Episode

logger = logging.getLogger('audit')

SOURCE_PREFIX = 'disease:'


def disease_slug(name: str | None) -> str:
    """The slug PatientRecord uses for a disease name (or a slug, unchanged)."""
    from omop_core.services.patient_record_service import _canonicalize_disease, _disease_name_to_slug

    return _disease_name_to_slug(_canonicalize_disease(name.strip())) if name and name.strip() else ''


def slug_of(episode: Episode) -> str:
    """The disease slug a Disease Episode stands for."""
    value = episode.episode_source_value or ''
    return value[len(SOURCE_PREFIX):] if value.startswith(SOURCE_PREFIX) else ''


def primary_disease_slug(person) -> str:
    """The person's primary cancer, as PatientRecord last derived it."""
    record = PatientRecord.objects.filter(person=person).only('disease_slug', 'disease').first()
    if record is None:
        return ''
    return record.disease_slug or disease_slug(record.disease)


def _cancer_condition(person, slug: str):
    """The person's latest condition row for this cancer, if one names it."""
    from omop_core.services.patient_record_service import _usable_concept_name

    rows = (
        ConditionOccurrence.objects.filter(person=person, is_erroneous=False)
        .select_related('condition_concept')
        .order_by('-condition_start_date', '-condition_occurrence_id')
    )
    for row in rows:
        name = (_usable_concept_name(row.condition_concept) if row.condition_concept_id else None) \
            or row.condition_source_value
        if name and disease_slug(name) == slug:
            return row
    return None


def find_disease_episode(person, slug: str) -> Episode | None:
    if not slug:
        return None
    return Episode.objects.filter(
        person=person,
        episode_concept_id=CONCEPT_DISEASE_FIRST_OCCURRENCE,
        episode_source_value=f'{SOURCE_PREFIX}{slug}'[:50],
    ).first()


def disease_episode(person, slug: str, *, start_date=None) -> Episode | None:
    """The cancer's Disease Episode, created on first use.

    None when there is no slug, or when the Episode vocabulary (32528) is not
    loaded; callers then fall back to unparented lines (the pre-#1739 shape).
    """
    if not slug:
        return None
    existing = find_disease_episode(person, slug)
    if existing is not None:
        return existing
    disease_concept = Concept.objects.filter(concept_id=CONCEPT_DISEASE_FIRST_OCCURRENCE).first()
    if disease_concept is None:
        logger.warning('{"event": "disease_episode_skipped", "reason": "missing_disease_episode_concept"}')
        return None
    condition = _cancer_condition(person, slug)
    object_concept = (
        (condition.condition_concept if condition and condition.condition_concept_id else None)
        or Concept.objects.filter(concept_id=0).first()
        or Concept.objects.filter(concept_id=CONCEPT_EHR_TYPE).first()
        or disease_concept
    )
    episode = Episode(
        episode_id=next_pk(Episode, 'episode_id'),
        person=person,
        episode_concept=disease_concept,
        episode_object_concept=object_concept,
        episode_type_concept=Concept.objects.filter(concept_id=CONCEPT_EHR_TYPE).first() or disease_concept,
        episode_start_date=(condition.condition_start_date if condition else None) or start_date,
        episode_source_value=f'{SOURCE_PREFIX}{slug}'[:50],
    )
    if episode.episode_start_date is None:
        from django.utils import timezone

        episode.episode_start_date = timezone.localdate()
    episode.save()
    return episode


def regimen_episodes(person):
    """Every line of therapy the person has, whatever cancer it treats.

    Any Episode that is not a Disease Episode: lines have always been read that
    way, and some written before the Treatment Regimen concept was standard
    carry another episode concept.
    """
    return Episode.objects.filter(person=person).exclude(episode_concept_id=CONCEPT_DISEASE_FIRST_OCCURRENCE)


def lines_for(person, parent: Episode | None, *, include_unparented: bool):
    """The lines of one cancer. Unparented lines count as the primary cancer's."""
    if parent is None:
        return regimen_episodes(person).filter(episode_parent_id__isnull=True)
    scope = Q(episode_parent_id=parent.episode_id)
    if include_unparented:
        scope |= Q(episode_parent_id__isnull=True)
    return regimen_episodes(person).filter(scope)


def primary_lines(person, slug: str | None = None):
    """The primary cancer's lines: parented to its Disease Episode, or unparented.

    ``slug`` is the primary cancer when the caller has just derived it (a
    refresh); otherwise the one PatientRecord last stored.
    """
    slug = primary_disease_slug(person) if slug is None else slug
    return lines_for(person, find_disease_episode(person, slug), include_unparented=True)


def other_cancer_line_ids(person) -> set[int]:
    """Lines that belong to a cancer other than the primary one."""
    primary = find_disease_episode(person, primary_disease_slug(person))
    others = regimen_episodes(person).filter(episode_parent_id__isnull=False)
    if primary is not None:
        others = others.exclude(episode_parent_id=primary.episode_id)
    return set(others.values_list('episode_id', flat=True))


def attach_unparented_lines(person) -> int:
    """Attach the person's unparented lines (and their LOT observations) to the primary cancer.

    The backfill for lines written before #1739. Returns how many lines moved;
    0 when the person has no primary cancer or the vocabulary is not loaded.
    """
    from omop_core.models import Observation

    orphans = list(regimen_episodes(person).filter(episode_parent_id__isnull=True))
    if not orphans:
        return 0
    parent = disease_episode(person, primary_disease_slug(person),
                             start_date=min(e.episode_start_date for e in orphans))
    if parent is None:
        return 0
    for line in orphans:
        line.episode_parent_id = parent.episode_id
        line.save(update_fields=['episode_parent_id'])
        Observation.objects.filter(
            person=person, observation_event_id__isnull=True,
            observation_source_value__in=[f'LOT-{line.episode_number}-{s}'
                                          for s in ('outcome', 'intent', 'discontinuation')],
        ).update(observation_event_id=line.episode_id)
    return len(orphans)


def lines_by_disease(person) -> list[dict]:
    """Each cancer's lines of therapy, primary first, read straight from the Episodes.

    ``[{'slug', 'disease_episode_id', 'primary', 'lines': [{'episode_id',
    'line', 'regimen', 'start_date', 'end_date', 'outcome', 'intent',
    'discontinuation_reason'}]}]``. Unparented lines are the primary cancer's.
    """
    from omop_core.models import Observation

    primary_slug = primary_disease_slug(person)
    diseases = {
        e.episode_id: e for e in Episode.objects.filter(
            person=person, episode_concept_id=CONCEPT_DISEASE_FIRST_OCCURRENCE,
        )
    }
    primary = next((e for e in diseases.values() if slug_of(e) == primary_slug), None)
    lines = list(
        regimen_episodes(person).filter(episode_number__isnull=False)
        .select_related('episode_object_concept', 'episode_source_concept').order_by('episode_number')
    )
    observations = {
        (o.observation_event_id, o.observation_source_value): o.value_as_string
        for o in Observation.objects.filter(
            person=person, is_erroneous=False, observation_source_value__startswith='LOT-',
        ).order_by('observation_date', 'observation_id')
    }

    def owner(line):
        if line.episode_parent_id in diseases:
            return line.episode_parent_id
        return primary.episode_id if primary else None

    groups: dict = {}
    for line in lines:
        groups.setdefault(owner(line), []).append(line)

    def entry(line):
        n = line.episode_number
        own = {key[1]: value for key, value in observations.items() if key[0] == line.episode_id}
        legacy = {key[1]: value for key, value in observations.items() if key[0] is None} if (
            owner(line) == (primary.episode_id if primary else None)) else {}
        values = {**legacy, **own}
        regimen = line.episode_object_concept
        return {
            'episode_id': line.episode_id,
            'line': n,
            'regimen': regimen.concept_name if regimen and regimen.concept_id else (line.episode_source_value or None),
            'start_date': line.episode_start_date,
            'end_date': line.episode_end_date,
            'outcome': values.get(f'LOT-{n}-outcome'),
            'intent': values.get(f'LOT-{n}-intent'),
            'discontinuation_reason': values.get(f'LOT-{n}-discontinuation'),
        }

    result = []
    for key, group in groups.items():
        disease = diseases.get(key)
        slug = slug_of(disease) if disease else primary_slug
        result.append({
            'slug': slug,
            'disease_episode_id': key,
            'primary': slug == primary_slug,
            'lines': [entry(line) for line in group],
        })
    result.sort(key=lambda g: (not g['primary'], g['slug']))
    return result
