"""Disease Episodes: the cancer a line of therapy treats (#1739).

A line of therapy is a Treatment Regimen Episode (32531). Following the OMOP
Oncology convention, a line written for a named cancer hangs off a Disease
Episode (32528, "Disease First Occurrence") through ``episode_parent_id``; the
Disease Episode's ``episode_object_concept`` is the cancer's diagnosis concept.
One Disease Episode per cancer (a unique constraint), keyed by the same disease
slug ``PatientRecord`` derives, so line numbering is per ``(person, cancer)``
and a second cancer has its own line 1.

A line no writer filed under a cancer (lines written before #1739, by the bulk
importer or by inference) has no parent, and is the **primary** cancer's,
whichever that is when it is read. So is a line whose Disease Episode names a
cancer no longer on the record (a removed or renamed diagnosis). Nothing is
backfilled: guessing which cancer an old line treated would be wrong exactly
when the primary changes.

``PatientRecord``'s flat therapy fields (first/second/later line, line count,
prior therapy) stay the **primary** cancer's: trial eligibility counts prior
lines for the cancer being matched.
"""
from __future__ import annotations

import logging

from django.db import IntegrityError, transaction
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
# episode_source_value is 50 characters, so a slug is compared on its first 42.
SLUG_LENGTH = 50 - len(SOURCE_PREFIX)


def disease_slug(name: str | None) -> str:
    """The slug PatientRecord uses for a disease name (or a slug, unchanged)."""
    from omop_core.services.patient_record_service import _canonicalize_disease, _disease_name_to_slug

    return _disease_name_to_slug(_canonicalize_disease(name.strip())) if name and name.strip() else ''


def disease_key(slug: str | None) -> str:
    """A slug as a Disease Episode stores it: compare slugs only in this form."""
    return (slug or '')[:SLUG_LENGTH]


def slug_of(episode: Episode) -> str:
    """The disease slug a Disease Episode stands for (in its stored, key form)."""
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
        if name and disease_key(disease_slug(name)) == disease_key(slug):
            return row
    return None


def find_disease_episode(person, slug: str) -> Episode | None:
    if not slug:
        return None
    return Episode.objects.filter(
        person=person,
        episode_concept_id=CONCEPT_DISEASE_FIRST_OCCURRENCE,
        episode_source_value=f'{SOURCE_PREFIX}{disease_key(slug)}',
    ).first()


def disease_keys_on_record(person) -> set[str]:
    """The cancers (any diagnosis, in key form) the person's record names."""
    from omop_core.services.patient_record_service import _usable_concept_name

    keys = set()
    rows = ConditionOccurrence.objects.filter(person=person, is_erroneous=False).select_related('condition_concept')
    for row in rows:
        name = (_usable_concept_name(row.condition_concept) if row.condition_concept_id else None) \
            or row.condition_source_value
        if name:
            keys.add(disease_key(disease_slug(name)))
    return keys


def disease_episode(person, slug: str, *, start_date=None) -> Episode | None:
    """The cancer's Disease Episode, created on first use.

    None when there is no slug, or when the Episode vocabulary (32528) is not
    loaded. Two writers creating it at once get the same row: the second
    insert hits the unique constraint and reads the first one back.
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
        episode_source_value=f'{SOURCE_PREFIX}{disease_key(slug)}',
    )
    if episode.episode_start_date is None:
        from django.utils import timezone

        episode.episode_start_date = timezone.localdate()
    try:
        with transaction.atomic():
            episode.save()
    except IntegrityError:
        return find_disease_episode(person, slug)
    return episode


def regimen_episodes(person):
    """Every line of therapy the person has, whatever cancer it treats.

    Any Episode that is not a Disease Episode: lines have always been read that
    way, and some written before the Treatment Regimen concept was standard
    carry another episode concept.
    """
    return Episode.objects.filter(person=person).exclude(episode_concept_id=CONCEPT_DISEASE_FIRST_OCCURRENCE)


def primary_disease_ids(person, slug: str | None = None) -> set[int]:
    """Disease Episodes whose lines are the primary cancer's: its own, and any
    naming a cancer no longer on the record (a removed or renamed diagnosis)."""
    slug = primary_disease_slug(person) if slug is None else slug
    diseases = list(Episode.objects.filter(person=person, episode_concept_id=CONCEPT_DISEASE_FIRST_OCCURRENCE))
    if not diseases:
        return set()
    on_record = disease_keys_on_record(person)
    primary = disease_key(slug)
    return {
        e.episode_id for e in diseases
        if (primary and slug_of(e) == primary) or slug_of(e) not in on_record
    }


def primary_lines(person, slug: str | None = None):
    """The primary cancer's lines: unparented, or filed under its Disease Episode.

    ``slug`` is the primary cancer when the caller has just derived it (a
    refresh); otherwise the one PatientRecord last stored.
    """
    return regimen_episodes(person).filter(
        Q(episode_parent_id__isnull=True) | Q(episode_parent_id__in=primary_disease_ids(person, slug))
    )


def other_cancer_line_ids(person) -> set[int]:
    """Lines that belong to a cancer other than the primary one."""
    others = regimen_episodes(person).filter(episode_parent_id__isnull=False).exclude(
        episode_parent_id__in=primary_disease_ids(person))
    return set(others.values_list('episode_id', flat=True))


def _regimen_name(line: Episode, drugs: list[str]) -> str | None:
    """A line's regimen: its named regimen concept, else its drugs, never the LOT-n label."""
    regimen = line.episode_object_concept
    if regimen is not None and regimen.concept_id and regimen.concept_id not in (
        CONCEPT_EHR_TYPE, CONCEPT_DISEASE_FIRST_OCCURRENCE,
    ):
        return regimen.concept_name
    source = (line.episode_source_value or '').strip()
    if source and not source.upper().startswith('LOT-'):
        return source
    return ' + '.join(drugs) or None


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
    primary_ids = primary_disease_ids(person, primary_slug)
    primary = next((e for e in diseases.values() if slug_of(e) == disease_key(primary_slug)), None)
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

    from omop_core.models import DrugExposure
    from omop_oncology.models import EpisodeEvent

    events = EpisodeEvent.objects.filter(episode_id__in=[line.episode_id for line in lines])
    exposures = {
        d.drug_exposure_id: d for d in DrugExposure.objects.filter(
            person=person, is_erroneous=False, drug_exposure_id__in=[e.event_id for e in events],
        ).select_related('drug_concept')
    }
    drug_names: dict = {}
    for event in events:
        exposure = exposures.get(event.event_id)
        if exposure is None:
            continue
        name = (exposure.drug_concept.concept_name if exposure.drug_concept_id else None) or exposure.drug_source_value
        if name and name not in drug_names.setdefault(event.episode_id, []):
            drug_names[event.episode_id].append(name)

    def owner(line):
        if line.episode_parent_id in diseases and line.episode_parent_id not in primary_ids:
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
        return {
            'episode_id': line.episode_id,
            'line': n,
            'regimen': _regimen_name(line, drug_names.get(line.episode_id, [])),
            'start_date': line.episode_start_date,
            'end_date': line.episode_end_date,
            'outcome': values.get(f'LOT-{n}-outcome'),
            'intent': values.get(f'LOT-{n}-intent'),
            'discontinuation_reason': values.get(f'LOT-{n}-discontinuation'),
        }

    result = []
    for key, group in groups.items():
        disease = diseases.get(key)
        is_primary = key == (primary.episode_id if primary else None)
        result.append({
            'slug': primary_slug if is_primary else slug_of(disease),
            'disease_episode_id': key,
            'primary': is_primary,
            'lines': [entry(line) for line in group],
        })
    result.sort(key=lambda g: (not g['primary'], g['slug']))
    return result


def cdm_field_concept(field: str) -> Concept | None:
    """The OMOP CDM field concept named ``table.column`` (e.g. ``episode.episode_id``).

    Athena codes these CDM###, older imports use ``table.column`` as the code;
    the name is ``table.column`` in both. An ``*_event_id`` column is paired
    with one so readers (and patient copies) know which table it points at.
    """
    return (
        Concept.objects.filter(vocabulary_id='CDM', invalid_reason__isnull=True)
        .filter(Q(concept_code=field) | Q(concept_name=field))
        .order_by('concept_id').first()
    )
