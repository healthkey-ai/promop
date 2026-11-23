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
  unmapped code is queued for curators and seen exactly as a lookup would see
  it. A proposed target is never an answer.
- A row whose code does not resolve is not written, and the report says why.
  Neither is one whose mapping files the code in another clinical table: the
  report names the table to send it to.
- A code may be as long as a mapping's (100 characters); the stored source
  value is cut to its column, which is how a mapping finds such rows.
"""
from dataclasses import dataclass, field

from rest_framework.exceptions import ValidationError

from omop_core.mapping import axis_variants
from omop_core.mapping.code_resolution import (
    CLINICAL_TABLES, SOURCE_CODE_MAX, normalize_omop_table, resolve_source_code_variant,
)
from omop_core.models import SourceCodeConceptMapping
from omop_core.services import source_vocabularies

# The lookup endpoint's own: a queue entry made here reads as one made there.
SOURCE_SYSTEM = 'code-mapping-api'

VOCABULARY_FIELD = 'source_vocabulary_id'
TEXT_FIELD = 'source_text'
UNIT_FIELD = 'unit_source_value'

UNMAPPED = 'unmapped'
MISMATCH = 'destination_domain_or_validity_mismatch'
OTHER_TABLE = 'other_table'

TABLE_FOR_MODEL = {model.__name__: table for table, (model, _c, _s) in CLINICAL_TABLES.items()}
MODEL_FOR_TABLE = {table: model for table, (model, _c, _s) in CLINICAL_TABLES.items()}

# A direct vocabulary hit is cached as a mapping filed under whichever table
# the first caller asked about, so for these the concept's domain says where
# the code belongs. Every other mapping's table is a curator's or an
# importer's statement, and it is where a re-point looks for the code's rows.
DIRECT_ORIGIN = 'athena-direct'

VOCABULARY_MAX = SourceCodeConceptMapping._meta.get_field(VOCABULARY_FIELD).max_length

_NO_VOCABULARY = (
    'This field is required. A term with no code system is resolved through '
    'code-mappings/lookup/.'
)
_NO_CODE = 'This field is required: it is the source code the mapping is looked up by.'
_NUL = 'Null characters are not allowed.'
_CONCEPT_SENT = 'Not accepted here: the server sets the concept from the mapping.'
_CONFIDENCE_SENT = (
    'Not accepted here: a row is written at an approved mapping, which is not a '
    'provisional row.'
)
_HOSPITAL_CODES = (
    'Epic and Cerner hospital-local codes require organization context from the '
    'service credential or the X-Provenance-Organization-ID header.'
)


@dataclass
class Plan:
    """What to write, and what was left out and why."""

    rows: list = field(default_factory=list)        # rows for the bulk write, concept set
    positions: list = field(default_factory=list)   # the request index of each of them
    unmapped: list = field(default_factory=list)    # one entry per code and reason


def _text(value):
    """A JSON string or integer as text; anything else is not a code."""
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        return ''
    return str(value).strip()


def _too_long(limit):
    return f'Ensure this field has no more than {limit} characters.'


def row_errors(rows, source_field, concept_field):
    """Per-index errors for what this entrance adds to a row, or None.

    Everything else about a row is the serializer's to judge, on the rows that
    are written. What reaches the resolver is judged here, before it: the
    resolver runs first, and would queue a code no mapping could ever hold.
    """
    errors = []
    for row in rows:
        if not isinstance(row, dict):
            errors.append({'non_field_errors': ['Each row must be an object.']})
            continue
        error = {}
        vocabulary, code = _text(row.get(VOCABULARY_FIELD)), _text(row.get(source_field))
        if not vocabulary:
            error[VOCABULARY_FIELD] = [_NO_VOCABULARY]
        elif '\x00' in vocabulary:
            error[VOCABULARY_FIELD] = [_NUL]
        elif len(vocabulary) > VOCABULARY_MAX:
            error[VOCABULARY_FIELD] = [_too_long(VOCABULARY_MAX)]
        if not code:
            error[source_field] = [_NO_CODE]
        elif '\x00' in code:
            error[source_field] = [_NUL]
        elif len(code) > SOURCE_CODE_MAX:
            error[source_field] = [_too_long(SOURCE_CODE_MAX)]
        if row.get(concept_field) is not None:
            error[concept_field] = [_CONCEPT_SENT]
        if row.get('confidence') is not None:
            error['confidence'] = [_CONFIDENCE_SENT]
        text = row.get(TEXT_FIELD)
        if text is not None and not isinstance(text, str):
            error[TEXT_FIELD] = ['Not a valid string.']
        elif text and '\x00' in text:
            error[TEXT_FIELD] = [_NUL]
        errors.append(error)
    return errors if any(errors) else None


def _home_table(concept, mapping):
    """The clinical table a resolved code's rows are filed in; '' when nothing says.

    The mapping's own table, which is where an approval's re-point and a
    relocation look for the code's rows, so a row written anywhere else would
    be out of their reach (#2386). A cached direct vocabulary hit is the
    exception: see ``DIRECT_ORIGIN``.
    """
    filed = normalize_omop_table(mapping.omop_table) if mapping is not None else ''
    if filed and mapping.origin_system != DIRECT_ORIGIN:
        return filed
    return source_vocabularies.table_for_domain(concept.domain_id) or filed


def plan(rows, *, model_name, source_field, concept_field, organization):
    """Resolve the rows' codes and split them into what is written and what is not.

    ``rows`` have passed :func:`row_errors`. One resolver call per distinct
    code and one variant read per distinct unit of a code that has variants,
    whatever the number of rows.
    """
    table = TABLE_FOR_MODEL[model_name]
    source_max = MODEL_FOR_TABLE[table]._meta.get_field(source_field).max_length
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
            # One ask per code per request, as a lookup's entry is: a provisional sighting.
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
    for index, (key, row) in enumerate(zip(keys, rows)):
        concept, mapping = answer(key, _text(row.get(UNIT_FIELD)))
        entry = None
        if concept is None:
            approved = mapping is not None and mapping.status == 'approved'
            entry = {
                'reason': MISMATCH if approved else UNMAPPED,
                'status': mapping.status if mapping is not None else 'unresolved',
                **({'omop_table': normalize_omop_table(mapping.omop_table)} if approved else {}),
            }
        elif _home_table(concept, mapping) not in ('', table):
            entry = {
                'reason': OTHER_TABLE,
                'status': mapping.status if mapping is not None else 'direct',
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
            continue

        written = {name: value for name, value in row.items()
                   if name not in (VOCABULARY_FIELD, TEXT_FIELD)}
        written[concept_field] = concept.concept_id
        if source_max and len(key[1]) > source_max:
            # A mapping's code can be longer than the column (a HealthKit
            # identifier runs to 55). Rows are stored cut to it, which is how
            # a re-point matches them (``_source_value_match``).
            written[source_field] = key[1][:source_max]
        result.rows.append(written)
        result.positions.append(index)

    result.unmapped = list(report.values())
    return result


def spread(values, positions, size, blank=None):
    """Values for the written rows, laid back onto the request's own positions.

    ``blank`` fills the positions of rows that were not written; a callable is
    called once per position.
    """
    out = [blank() if callable(blank) else blank for _ in range(size)]
    for position, value in zip(positions, values):
        out[position] = value
    return out
