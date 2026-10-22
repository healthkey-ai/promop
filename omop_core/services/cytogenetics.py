"""Canonical multiple-myeloma cytogenetic marker values."""

import re

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

CANONICAL_CYTOGENETIC_MARKERS = (
    'del17p',
    't(4;14)',
    't(11;14)',
    't(14;16)',
    '1q_gain',
    '1q_amp',
    'hyperdiploidy',
    'del13q',
    'MYC rearrangement',
)

_ALIASES = {
    'del(17p13)': 'del17p',
    'del(17p)': 'del17p',
    'del(13q)': 'del13q',
    'tp53/17p deletion': 'del17p',
    '1q21 amplification': '1q_amp',
    '1q21 gain': '1q_gain',
    'fgfr3/igh translocation t(4;14)': 't(4;14)',
    'maf/igh translocation t(14;16)': 't(14;16)',
    'myc rearrangement': 'MYC rearrangement',
}
_CANONICAL_BY_CASEFOLD = {
    marker.casefold(): marker for marker in CANONICAL_CYTOGENETIC_MARKERS
}
_NO_MARKER_VALUES = {
    'standard risk — no high-risk markers detected',
    'standard risk - no high-risk markers detected',
}


def normalise_cytogenetic_markers(value, *, strict=False) -> str:
    """Return a stable, de-duplicated comma-separated marker list.

    UI writes use ``strict=True`` so PatientRecord never receives an answer the
    chooser cannot represent. Import refreshes are deliberately lossless:
    recognized aliases are normalized while unfamiliar source values survive.
    """
    if value in (None, ''):
        return ''
    parts = value if isinstance(value, (list, tuple)) else re.split(r',\s*(?![^()]*\))', str(value))
    normalized = []
    unknown = []
    for part in parts:
        raw = str(part).strip()
        if not raw or raw.casefold() in _NO_MARKER_VALUES:
            continue
        if raw.casefold().startswith('t('):
            raw = raw.replace(',', ';')
        folded = raw.casefold()
        marker = _ALIASES.get(folded) or _CANONICAL_BY_CASEFOLD.get(folded)
        if marker is None:
            unknown.append(raw)
            marker = raw
        if marker not in normalized:
            normalized.append(marker)
    if strict and unknown:
        raise ValueError(f"Unrecognized cytogenetic marker values: {unknown}")
    return ', '.join(normalized)


def _note_source(row):
    # Include the table: Measurement and Observation IDs can overlap.
    return f'cytogenetics:{row._meta.db_table}:{row.pk}'


def store_cytogenetic_summary(row, value):
    """Return (CDM-width text, note_changed), inside the projector transaction.

    Large selections live in a NOTE tied to this patient and this dated fact.
    The fact holds only a reference, so even a 19-digit NOTE ID fits in 60 chars.
    Same-day edits reuse the note; previous days keep their own full text.
    """
    from omop_core.models import Note
    from omop_core.services.pk import next_pk

    if len(value) <= row._meta.get_field('value_as_string').max_length:
        return value, False
    note = Note.objects.filter(
        person_id=row.person_id, note_source_value=_note_source(row),
    ).order_by('-note_id').first()
    changed = note is None or note.note_text != value
    if note is None:
        note = Note.objects.create(
            note_id=next_pk(Note, 'note_id'), person_id=row.person_id,
            note_date=getattr(row, f'{row._meta.db_table}_date'),
            note_type_concept_id=0, note_source_value=_note_source(row),
            note_text=value,
        )
    elif changed:
        note.note_text = value
        note.save(update_fields=['note_text'])
    return f'[note:{note.pk}]', changed


def read_cytogenetic_summary(row):
    """Read full text only from a NOTE belonging to this patient and fact."""
    from omop_core.models import Note

    value = row.value_as_string
    match = re.fullmatch(r'\[note:(\d+)\]', value or '')
    if not match:
        return value
    # Both the built-in and curated extractors visit the same snapshot row.
    if not hasattr(row, '_cytogenetic_summary_text'):
        text = Note.objects.filter(
            pk=int(match.group(1)), person_id=row.person_id,
            note_source_value=_note_source(row),
        ).values_list('note_text', flat=True).first()
        row._cytogenetic_summary_text = text if text is not None else value
    return row._cytogenetic_summary_text


FIELD = 'cytogenetic_markers'
SOURCE_PREFIX = 'cytogenetic:'
LEGACY_SOURCE = 'mm-cytogenetic-markers'
LEGACY_CONCEPT_CODE = '107675007'
VALUES = CANONICAL_CYTOGENETIC_MARKERS


def selections(value):
    """Validate multiselect input and retain canonical matching tokens."""
    if value in (None, ''):
        return []
    if not isinstance(value, (str, list)) or (isinstance(value, list)
            and any(not isinstance(item, str) for item in value)):
        raise ValueError('Select cytogenetic markers as a list or comma-separated text.')
    normalized = normalise_cytogenetic_markers(value)
    return re.split(r',\s*(?![^()]*\))', normalized) if normalized else []


def descriptor(*, mapping_approved=False):
    from omop_core.models import Concept, FieldChoice, FieldConceptMapping

    if not mapping_approved and not FieldConceptMapping.objects.filter(
        field_name=FIELD, status='approved', multiple=True,
        vocabulary_id='SNOMED', concept_code=LEGACY_CONCEPT_CODE,
    ).exists():
        return None  # Preserve existing scalar/curator summary recipes.

    entry = {'kind': 'direct', 'writable': True, 'target': 'patient_record',
             'value_kind': 'string', 'multiple': True}
    choices = list(FieldChoice.objects.filter(field_name=FIELD).prefetch_related('codes'))
    preferred = [(choice, next((c for c in choice.codes.all() if c.is_primary), None))
                 for choice in choices]
    concepts = {(c.vocabulary_id, c.concept_code): c for c in Concept.objects.filter(
        vocabulary_id__in={'SNOMED'} | {c.vocabulary_id for _, c in preferred if c},
        concept_code__in={LEGACY_CONCEPT_CODE} | {c.code for _, c in preferred if c},
        standard_concept='S', domain_id='Observation', invalid_reason__isnull=True,
        valid_start_date__lte=timezone.localdate(), valid_end_date__gte=timezone.localdate(),
    )}
    options, recipes = [], {}
    for choice, code in preferred:
        concept = concepts.get((code.vocabulary_id, code.code)) if code else None
        option = {'value': choice.display, 'code': code.code if code else None,
                  'vocabulary': code.vocabulary_id if code else None,
                  'concept_id': concept.pk if concept else None,
                  'display': concept.concept_name if concept else None}
        options.append(option)
        if concept:
            recipes[choice.display] = {
                'omop_table': 'observation', 'concept_id': concept.pk,
                'source_value': SOURCE_PREFIX + choice.display,
                'type_concept_id': 32817, 'value_kind': 'string',
            }
    entry['options'] = options
    entry['projection'] = {
        'omop_table': 'observation', 'choice_projections': recipes,
        'choice_values': [choice.display for choice in choices],
    }
    legacy_concept = concepts.get(('SNOMED', LEGACY_CONCEPT_CODE))
    if legacy_concept:
        entry['projection']['legacy_projection'] = {
            'omop_table': 'observation', 'concept_id': legacy_concept.pk,
            'source_value': LEGACY_SOURCE, 'type_concept_id': 32817,
            'value_kind': 'string',
        }
    return entry


def values_from_rows(rows):
    """Merge dated marker observations with legacy aggregate imports, including clears."""
    from omop_core.services.omop_projection import CLEAR_VALUE

    current = {}
    for row in sorted(rows, key=lambda r: (r.observation_date, r.observation_id), reverse=True):
        source = row.observation_source_value or ''
        if source == LEGACY_SOURCE:
            legacy = selections(read_cytogenetic_summary(row))
            for marker in VALUES:
                current.setdefault(marker, marker in legacy)
            for marker in legacy:
                current.setdefault(marker, True)
            break
        if source.startswith(SOURCE_PREFIX):
            marker = source[len(SOURCE_PREFIX):]
            current.setdefault(marker, row.value_source_value != CLEAR_VALUE and bool(row.value_as_string))
    return [marker for marker in VALUES if current.get(marker)] + sorted(
        marker for marker, present in current.items() if present and marker not in VALUES)


def project_selections(person, value, projection):
    from omop_core.models import Observation, Person
    from omop_core.services.omop_projection import project_single_value

    selected = selections(value)
    recipes = projection['choice_projections']
    # An unavailable mapping for a curated selection must still fail atomically.
    # Unlisted historical text is preserved by the aggregate recipe instead.
    choice_values = set(projection.get('choice_values', recipes))
    if any(marker in choice_values and marker not in recipes for marker in selected):
        return False
    with transaction.atomic():
        Person.objects.select_for_update().get(pk=person.pk)
        rows = list(Observation.objects.filter(person=person, is_erroneous=False).filter(
            Q(observation_source_value__startswith=SOURCE_PREFIX)
            | Q(observation_source_value=LEGACY_SOURCE)))
        previous = values_from_rows(rows)
        removed = set(previous) - set(selected)
        today = timezone.localdate()
        after_pk = max((row.pk for row in rows if row.observation_date == today
                        and row.observation_source_value == LEGACY_SOURCE), default=None)
        legacy_selected = [marker for marker in selected if marker not in recipes]
        if legacy_selected or any(marker not in recipes for marker in removed):
            legacy_recipe = projection.get('legacy_projection')
            if not legacy_recipe:
                return False
            # Replace the legacy set before reasserting the selected coded
            # markers. This also clears removed unlisted values without needing
            # to invent an individual concept mapping for them. The new row
            # must sort after every same-day row it supersedes.
            boundary = max((row.pk for row in rows if row.observation_date == today), default=None)
            if not project_single_value(person, FIELD, ', '.join(legacy_selected) or None,
                                        legacy_recipe, acknowledge_existing=True, after_pk=boundary):
                raise ValueError('Cytogenetic legacy projection failed')
            after_pk = Observation.objects.filter(
                person=person, is_erroneous=False, observation_date=today,
                observation_source_value=LEGACY_SOURCE,
            ).order_by('-observation_id').values_list('pk', flat=True).first()
            removed = set()  # The replacement aggregate clears the previous set.
        # All selected markers get today's affirmative row. Removed markers get
        # a dated clear; earlier rows remain intact as history.
        for marker in [m for m in selected if m in recipes] + sorted(removed):
            if not project_single_value(person, FIELD, marker if marker in selected else None,
                                        recipes[marker], acknowledge_existing=True, after_pk=after_pk):
                raise ValueError('Cytogenetic projection failed')
    return True
