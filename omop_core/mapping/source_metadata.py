"""Source Metadata as context for the Suggest rankers (#1782, #1786, #1791).

A source code's description is often missing, wrong or generic. The ETL may
send "Vitals" for a code that is clearly something specific, and Epic adds
LOINC 8716-3 "Vital signs" to every vital sign. The Source Metadata the mapping
dialog shows -- usually the FHIR resource that carried the code -- says what it
really is: ``code.text`` "Respirations", a LOINC 9279-1 coding, the components
of a blood pressure, the method, body site or specimen.

:func:`deidentify` passes that JSON to Jev and Anthropic in full, minus what
could identify the patient. It is context only: retrieval does not read it.

What is removed, recursively, wherever it appears:

- references and identity: ``subject``, ``patient``, ``encounter`` and every
  participant reference (``performer``, ``requester`` ...), ``id``,
  ``identifier``, ``meta``, ``contained``, and any ``reference`` key;
- free text that can name a person: the resource narrative (``text`` holding
  ``div``), ``note``, ``comment``, ``valueString``;
- demographics: ``name``, ``telecom``, ``address``, ``birthDate``, ``photo``,
  ``contact`` and the like;
- dates and times: ``effective*``, ``issued``, ``onset*``, ``recordedDate``,
  anything ending ``DateTime``/``Date``/``Instant``/``Period``/``Time``.

``code.text`` and coding displays are strings too, and stay: they are what the
code is called, which is the point.
"""
import json

#: Ceiling on the serialized context, so one oversized resource cannot crowd the
#: candidates out of the ranking prompt.
MAX_JSON = 20_000
MAX_STRING = 1_000
MAX_DEPTH = 10

_DROP_KEYS = frozenset({
    # references and identity
    'subject', 'patient', 'encounter', 'context', 'performer', 'requester',
    'recorder', 'asserter', 'author', 'informationSource', 'participant',
    'sender', 'source', 'basedOn', 'partOf', 'focus', 'hasMember', 'derivedFrom',
    'reasonReference', 'insurance', 'account', 'id', 'identifier', 'meta',
    'contained', 'reference', 'implicitRules',
    # free text that can name a person
    'note', 'comment', 'valueString', 'div',
    # demographics
    'name', 'telecom', 'address', 'birthDate', 'photo', 'contact', 'gender',
    'maritalStatus', 'communication', 'generalPractitioner', 'managingOrganization',
    'multipleBirthBoolean', 'multipleBirthInteger',
    # dates and times not caught by the prefix and suffix rules
    'issued', 'recordedDate', 'authoredOn', 'lastUpdated', 'date', 'period',
    'timestamp', 'start', 'end',
    # curation bookkeeping, not a description of the code
    'facilities',
})
_DROP_PREFIXES = ('effective', 'onset', 'abatement', 'occurrence', 'performed',
                  'deceased', 'recorded', 'issued')
_DROP_SUFFIXES = ('DateTime', 'Date', 'Instant', 'Period', 'Time')


def _dropped(key):
    return (
        key in _DROP_KEYS
        or key.startswith(_DROP_PREFIXES)
        or key.endswith(_DROP_SUFFIXES)
    )


def _scrub(value, depth, list_cap):
    if isinstance(value, str):
        return value[:MAX_STRING]
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    if depth >= MAX_DEPTH:
        return None
    if isinstance(value, list):
        items = (_scrub(v, depth + 1, list_cap) for v in value[:list_cap])
        return [v for v in items if v not in (None, {}, [])]
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            key = str(key)
            # A resource narrative is {"status", "div"}; CodeableConcept.text
            # is a plain string and stays.
            if _dropped(key) or (key == 'text' and isinstance(item, dict)):
                continue
            item = _scrub(item, depth + 1, list_cap)
            if item not in (None, {}, []):
                out[key] = item
        return out
    return None


def deidentify(metadata):
    """*metadata* without patient-identifying fields, bounded; ``{}`` if nothing is left."""
    if not isinstance(metadata, dict):
        return {}
    for list_cap in (50, 10, 3):
        cleaned = _scrub(metadata, 0, list_cap) or {}
        if len(json.dumps(cleaned, ensure_ascii=False, default=str)) <= MAX_JSON:
            return cleaned
    return {}
