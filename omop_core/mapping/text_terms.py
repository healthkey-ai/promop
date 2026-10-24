"""Text terms in the lookup API: a document's wording with no code system (#1944).

An extractor reading clinical documents has "Hemoglobin 10.2 g/dL", not a
hospital code. It still needs the same answer the importers get, from the
same curated mappings and the same Suggest engine, so a term goes through
``resolve_source_code`` under the Uncoded vocabulary (``''``) exactly as a
free-text ``Observation.code.text`` from a FHIR upload does. Nothing here is
a second matcher: the unit, value and specimen the document gives are turned
into the metadata :mod:`loinc_axes` already reads, so a term in mmol/L is
never offered a mass-concentration LOINC, and a term seen in two units is
flagged with axis variants like any hospital code.

What is specific to terms:

* the wording is normalised (whitespace) so one phrasing is one queue row;
* a term that nothing matches is queued with no destination rather than a
  minted placeholder concept, which a document phrasing would only pollute;
* an unresolved entry answers with the axis-filtered candidates, so the
  caller can hold the fact for review with the options in hand;
* the unit and context are stored on the queue row as evidence.

The term itself is never logged.
"""
from django.db import transaction

from omop_core.mapping import loinc_axes
from omop_core.mapping.code_resolution import SOURCE_CODE_MAX, _DOMAIN_FOR_TABLE
from omop_core.services.canonical_units import unit_code

#: The Uncoded vocabulary: a term with no code system.
TEXT_VOCABULARY = ''
#: Recorded by callers on each run; bump when the handling here changes.
TEXT_TERMS_VERSION = 1
CANDIDATES_SHOWN = 10

MATCH_RESOLVED, MATCH_AMBIGUOUS, MATCH_UNRESOLVED = 'resolved', 'ambiguous', 'unresolved'


def matcher_version():
    from omop_core.mapping.suggestions import SUGGESTION_MODEL_VERSION
    return f'text-terms-{TEXT_TERMS_VERSION}/suggest-{SUGGESTION_MODEL_VERSION}'


def normalize_term(text):
    """One phrasing, one queue row: collapse whitespace and cut to the key length."""
    return ' '.join(str(text or '').split())[:SOURCE_CODE_MAX]


def metadata_for(unit=None, quantity=None, specimen=None):
    """The document's context in the shape :func:`loinc_axes.infer` reads.

    A unit settles the property, a value the scale, a specimen the system;
    nothing else is claimed. Empty when the document gave none of them.
    """
    resource = {}
    if unit or quantity is not None:
        value = {}
        if quantity is not None:
            value['value'] = quantity
        if unit:
            value['unit'] = unit
        resource['valueQuantity'] = value
    if specimen:
        resource['specimen'] = {'text': specimen}
    return {'fhir': resource} if resource else {}


def record(mapping, unit, metadata):
    """Keep the unit and context on the term's queue row as evidence.

    The unit joins ``source_unit_evidence``, which the approval unit check and
    axis-variant detection already read, so a term that arrives in a second
    unit is flagged the way a hospital code is. Context is stored once, for
    the curator's Source Metadata panel. Returns the distinct units seen.
    Nothing about the destination changes, approved rows included.
    """
    from omop_core.models import SourceCodeConceptMapping

    if mapping is None:
        return 0
    with transaction.atomic():
        row = (SourceCodeConceptMapping.objects.select_for_update()
               .only('source_unit_evidence', 'source_metadata').get(pk=mapping.pk))
        evidence = [e for e in (row.source_unit_evidence or []) if isinstance(e, dict)]
        fields = []
        if unit:
            code = unit_code(unit) or unit
            entry = next((e for e in evidence if e.get('code') == code), None)
            if entry is None:
                evidence.append({'code': code, 'display': unit, 'count': 1})
            else:
                entry['count'] = int(entry.get('count') or 0) + 1
            row.source_unit_evidence = evidence
            fields.append('source_unit_evidence')
        if metadata and not row.source_metadata:
            row.source_metadata = metadata
            fields.append('source_metadata')
        if fields:
            row.save(update_fields=fields)
    return len({e.get('code') for e in evidence if e.get('code')})


def candidates_for(term, omop_table, metadata, unit=None):
    """The ranked, axis-filtered options for a term nothing resolved.

    The same pool Suggest builds for a queue row (``_prepare``), on lexical
    retrieval alone: a candidate conflicting with an axis the document settled
    is excluded and, for a lab-style term, LOINC leads. Trimmed for the wire.
    """
    from omop_core.mapping.suggestions import CANDIDATE_LIMIT, STRATEGY_LEXICAL, _prepare, source_loinc_axes

    domain_id = _DOMAIN_FOR_TABLE.get(omop_table)
    if not domain_id:
        return []
    source_axes, hints = source_loinc_axes(
        domain_id=domain_id, source_vocabulary_id=TEXT_VOCABULARY, source_code=term,
        metadata=metadata, observed_units=[unit] if unit else None,
    )
    job = _prepare(
        source_code=term, source_vocabulary_id=TEXT_VOCABULARY, source_text=term,
        domain_id=domain_id, strategies=[STRATEGY_LEXICAL], lexical_limit=CANDIDATE_LIMIT,
        source_axes=source_axes or None, axis_hints=hints,
        lab_reason=loinc_axes.lab_signal(TEXT_VOCABULARY, metadata, source_axes),
    )
    return [
        {key: candidate.get(key) for key in (
            'concept_id', 'concept_name', 'concept_code', 'vocabulary_id', 'domain_id',
            'lexical_score', 'axes_matched', 'loinc_axes')}
        for candidate in job['candidates'][:CANDIDATES_SHOWN]
    ]


def annotate(entry, *, mapping, concept, term, omop_table, metadata, unit):
    """Add the term's verdict to its lookup entry and keep its evidence."""
    units_seen = record(mapping, unit, metadata)
    if concept is not None:
        entry['match'] = MATCH_RESOLVED
    else:
        candidates = candidates_for(term, omop_table, metadata, unit)
        entry['match'] = MATCH_AMBIGUOUS if candidates else MATCH_UNRESOLVED
        entry['candidates'] = candidates
    return units_seen
