"""Resolve each row's source code, then write the rows that are mapped (#2813).

``POST /api/v1/<clinical>/upsert_if_mapped/`` takes the bulk write's rows
without a concept. A producer otherwise asks ``code-mappings/lookup/`` for the
concept and then posts the row: two round trips for a record that arrives on
its own, and a second copy, in every producer, of the rules for what counts as
resolved.

This module decides, per row, what the lookup would have answered and what
follows from it. The write itself is the bulk upsert's, unchanged.

- The source code is the row's source value, the column a mapping finds its
  rows by (``CLINICAL_TABLES``); the row adds ``source_vocabulary_id`` and may
  add ``source_text``. Its ``unit_source_value`` routes it to an axis variant.
- Each distinct code is resolved once, by the lookup's own resolver, so an
  unmapped code is queued for curators and its sighting recorded exactly as a
  lookup would. A proposed target is never an answer.
- A row whose code does not resolve is left out (``skip``) or written at
  concept 0 (``store``), the state an approval's re-point looks for.
- A code whose mapping belongs in another clinical table is never written
  here: the report names the table to send it to.
"""
from dataclasses import dataclass, field

from rest_framework.exceptions import ValidationError

from omop_core.mapping import axis_variants
from omop_core.mapping.code_resolution import (
    CLINICAL_TABLES, normalize_omop_table, resolve_source_code_variant,
)
from omop_core.models import Concept
from omop_core.services import source_vocabularies

# The lookup endpoint's own: a queue entry made here reads as one made there.
SOURCE_SYSTEM = 'code-mapping-api'

SKIP, STORE = 'skip', 'store'
POLICIES = (SKIP, STORE)

VOCABULARY_FIELD = 'source_vocabulary_id'
TEXT_FIELD = 'source_text'
UNIT_FIELD = 'unit_source_value'

UNMAPPED = 'unmapped'
MISMATCH = 'destination_domain_or_validity_mismatch'
OTHER_TABLE = 'other_table'

TABLE_FOR_MODEL = {model.__name__: table for table, (model, _c, _s) in CLINICAL_TABLES.items()}

_NO_VOCABULARY = (
    'This field is required. A term with no code system is resolved through '
    'code-mappings/lookup/.'
)
_NO_CODE = 'This field is required: it is the source code the mapping is looked up by.'
_CONCEPT_SENT = 'Not accepted here: the server sets the concept from the mapping.'
_HOSPITAL_CODES = (
    'Epic and Cerner hospital-local codes require organization context from the '
    'service credential or the X-Provenance-Organization-ID header.'
)


@dataclass
class Plan:
    """What to write, and what was left out and why."""

    rows: list = field(default_factory=list)        # rows for the bulk write, concept set
    positions: list = field(default_factory=list)   # the request index of each of them
    resolved: int = 0                               # request rows written at a mapped concept
    unmapped: list = field(default_factory=list)    # one entry per code and reason


def policy(raw):
    """``?unmapped=``: ``skip`` (the default) or ``store``."""
    value = str(raw or SKIP).strip().lower()
    if value not in POLICIES:
        raise ValidationError({'unmapped': f"Expected one of: {', '.join(POLICIES)}."})
    return value


def _text(value):
    """A JSON string or integer as text; anything else is not a code."""
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        return ''
    return str(value).strip()


def row_errors(rows, source_field, concept_field):
    """Per-index errors for what this entrance adds to a row, or None.

    Everything else about a row is the serializer's to judge, on the rows that
    are written.
    """
    errors = []
    for row in rows:
        if not isinstance(row, dict):
            errors.append({'non_field_errors': ['Each row must be an object.']})
            continue
        error = {}
        if not _text(row.get(VOCABULARY_FIELD)):
            error[VOCABULARY_FIELD] = [_NO_VOCABULARY]
        if not _text(row.get(source_field)):
            error[source_field] = [_NO_CODE]
        if row.get(concept_field) is not None:
            error[concept_field] = [_CONCEPT_SENT]
        text = row.get(TEXT_FIELD)
        if text is not None and not isinstance(text, str):
            error[TEXT_FIELD] = ['Not a valid string.']
        errors.append(error)
    return errors if any(errors) else None


def _belongs(concept, mapping, table):
    """Whether a resolved code's rows are filed in ``table``.

    Where the mapping files the code, or else the concept's own domain: a
    lookup followed by a write to the table it named would land here too.
    """
    if mapping is not None and normalize_omop_table(mapping.omop_table) == table:
        return True
    return source_vocabularies.table_for_domain(concept.domain_id) == table


def _home_table(concept, mapping):
    filed = normalize_omop_table(mapping.omop_table) if mapping is not None else ''
    return filed or source_vocabularies.table_for_domain(concept.domain_id)


def plan(rows, *, model_name, source_field, concept_field, organization, unmapped):
    """Resolve the rows' codes and split them into what is written and what is not.

    ``rows`` have passed :func:`row_errors`. One resolver call per distinct
    code and one variant read per distinct unit of a code that has variants,
    whatever the number of rows.
    """
    table = TABLE_FOR_MODEL[model_name]
    keys = [(_text(row[VOCABULARY_FIELD]), _text(row[source_field])) for row in rows]

    if organization is None and any(
            source_vocabularies.hospital_vendor(vocabulary) for vocabulary, _code in keys):
        raise ValidationError({'organization_id': _HOSPITAL_CODES})

    texts = {}
    for key, row in zip(keys, rows):
        if not texts.get(key):
            texts[key] = (row.get(TEXT_FIELD) or '').strip()

    answers = {}
    for key in dict.fromkeys(keys):
        vocabulary, code = key
        concept, mapping, _variant = resolve_source_code_variant(
            source_vocabulary_id=vocabulary, source_code=code, source_text=texts[key],
            omop_table=table, source_system=SOURCE_SYSTEM, organization=organization,
            # One ask per code per batch, as a lookup is: a provisional sighting.
            provisional=True,
        )
        answers[key] = (concept, mapping)

    routed = {}

    def answer(key, unit):
        """The code's answer for a record in ``unit``: an approved variant's concept wins."""
        concept, mapping = answers[key]
        if not unit or mapping is None or not mapping.axis_variant_count:
            return concept, mapping
        if (key, unit) not in routed:
            routed[key, unit] = axis_variants.variant_for(mapping, unit)
        variant = routed[key, unit]
        return (variant.target_concept if variant is not None else concept), mapping

    result = Plan()
    report = {}
    store = unmapped == STORE
    zero_checked = False
    for index, (key, row) in enumerate(zip(keys, rows)):
        concept, mapping = answer(key, _text(row.get(UNIT_FIELD)))
        entry = None
        if concept is None:
            approved = mapping is not None and mapping.status == 'approved'
            entry = {
                'reason': MISMATCH if approved else UNMAPPED,
                'status': mapping.status if mapping is not None else 'unresolved',
                'stored': store,
                **({'omop_table': normalize_omop_table(mapping.omop_table)} if approved else {}),
            }
        elif not _belongs(concept, mapping, table):
            entry = {
                'reason': OTHER_TABLE,
                'status': mapping.status if mapping is not None else 'direct',
                'stored': False,
                'omop_table': _home_table(concept, mapping),
            }

        if entry is not None:
            group = (key, entry['reason'], entry.get('omop_table'))
            if group not in report:
                report[group] = {
                    VOCABULARY_FIELD: key[0], 'source_code': key[1], **entry,
                    'mapping_id': mapping.pk if mapping is not None else None, 'rows': [],
                }
            report[group]['rows'].append(index)
            if not entry['stored']:
                continue
            if not zero_checked:
                _require_concept_zero()
                zero_checked = True
            concept_id = 0
        else:
            concept_id = concept.concept_id
            result.resolved += 1

        written = {name: value for name, value in row.items()
                   if name not in (VOCABULARY_FIELD, TEXT_FIELD)}
        written[concept_field] = concept_id
        result.rows.append(written)
        result.positions.append(index)

    result.unmapped = list(report.values())
    return result


def _require_concept_zero():
    if not Concept.objects.filter(concept_id=0).exists():
        raise ValidationError({'unmapped': (
            'Concept 0 is not loaded on this deployment, so an unmapped row cannot be '
            'stored. Load the vocabulary, or use unmapped=skip.'
        )})


def spread(values, positions, size, blank=None):
    """Values for the written rows, laid back onto the request's own positions.

    ``blank`` fills the positions of rows that were not written; a callable is
    called once per position.
    """
    out = [blank() if callable(blank) else blank for _ in range(size)]
    for position, value in zip(positions, values):
        out[position] = value
    return out
