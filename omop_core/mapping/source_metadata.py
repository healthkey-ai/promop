"""Source Metadata as context for the Suggest rankers (#1782, #1786, #1791).

A source code's description is often missing, wrong or generic. The ETL may
send "Vitals" for a code that is clearly something specific, and Epic adds
LOINC 8716-3 "Vital signs" to every vital sign. The Source Metadata the mapping
dialog shows -- usually the FHIR resource that carried the code -- says what it
really is: ``code.text`` "Respirations", a LOINC 9279-1 coding, the components
of a blood pressure, the method, body site or specimen.

:func:`deidentify` passes that JSON to Jev and Anthropic in full, minus what
could identify the patient. It is context only: retrieval does not read it.

What is removed, recursively, at any depth -- by key *and* by shape, because
FHIR carries identity under many names (an extension's ``valueIdentifier``, a
``collector`` reference, an Annotation under any key):

- references and identity: any Reference (a dict with ``reference``, or a
  logical one with ``identifier``), Identifier, ``id``, ``meta``, ``fullUrl``,
  and any Patient/Practitioner/RelatedPerson/Person resource;
- people and contact details: HumanName, Address, ContactPoint, Attachment;
- free text that can name a person: Annotation (note, comment ...), the resource
  narrative (``text`` holding ``div``), ``valueString``, ``valueMarkdown``;
- dates and times: any key starting ``effective``/``onset``/``issued`` ... or
  ending date/time/instant/period, case-insensitively.

``code.text``, coding displays, Quantity and CodeableConcept values stay: they
say what the code is, which is the point. An oversized resource is trimmed
progressively, down to its coded fields, never dropped.
"""
import json

#: Ceiling on the serialized context, so one oversized resource cannot crowd the
#: candidates out of the ranking prompt.
MAX_JSON = 20_000
MAX_DEPTH = 10

_DROP_KEYS = frozenset(k.lower() for k in (
    # references and identity
    'subject', 'patient', 'encounter', 'context', 'performer', 'requester',
    'recorder', 'asserter', 'author', 'informationSource', 'participant',
    'sender', 'source', 'basedOn', 'partOf', 'focus', 'hasMember', 'derivedFrom',
    'reasonReference', 'insurance', 'account', 'resultsInterpreter', 'collector',
    'actor', 'practitioner', 'organization', 'location', 'id', 'identifier',
    'meta', 'contained', 'reference', 'implicitRules', 'fullUrl', 'request',
    'response', 'search', 'link',
    # free text that can name a person
    'note', 'comment', 'div', 'authorString', 'authorReference',
    # demographics and contact details
    'name', 'telecom', 'address', 'birthDate', 'photo', 'contact', 'gender',
    'maritalStatus', 'communication', 'generalPractitioner', 'managingOrganization',
    'multipleBirthBoolean', 'multipleBirthInteger',
    # typed values that hold identity or free text
    'valueString', 'valueMarkdown', 'valueIdentifier', 'valueHumanName',
    'valueAddress', 'valueContactPoint', 'valueAttachment', 'valueAnnotation',
    'valueReference', 'valueUri', 'valueUrl', 'valueOid', 'valueUuid', 'valueId',
    'valueBase64Binary', 'valueSignature',
    # dates and times not caught by the prefix and suffix rules
    'issued', 'lastUpdated', 'period', 'timestamp', 'start', 'end',
    # curation bookkeeping, not a description of the code
    'facilities',
))
_DROP_PREFIXES = ('effective', 'onset', 'abatement', 'occurrence', 'performed',
                  'deceased', 'recorded', 'issued', 'authored')
_DROP_SUFFIXES = ('datetime', 'date', 'instant', 'period', 'time')
_PERSON_RESOURCES = {'Patient', 'Practitioner', 'PractitionerRole', 'RelatedPerson',
                     'Person', 'Organization', 'Location'}
# The keys that say what a code is, kept when nothing else fits.
_CODE_KEYS = frozenset({
    'resourceType', 'code', 'coding', 'system', 'display', 'text', 'category',
    'component', 'method', 'bodySite', 'valueCodeableConcept', 'valueQuantity',
    'unit', 'fhir', 'resource', 'resources', 'entry',
})


def _dropped(key):
    key = key.lower()
    return (
        key in _DROP_KEYS
        or key.startswith(_DROP_PREFIXES)
        or key.endswith(_DROP_SUFFIXES)
    )


def _identifying_shape(value):
    """A FHIR datatype or resource that identifies someone, whatever its key."""
    keys = set(value)
    if value.get('resourceType') in _PERSON_RESOURCES:
        return True
    if 'reference' in keys or ('identifier' in keys and 'display' in keys):
        return True                                  # Reference
    if keys & {'family', 'given', 'prefix', 'suffix'}:
        return True                                  # HumanName
    if keys & {'line', 'city', 'postalCode', 'district'}:
        return True                                  # Address
    if keys & {'authorString', 'authorReference'} or ('text' in keys and 'time' in keys):
        return True                                  # Annotation
    if keys & {'contentType', 'data'}:
        return True                                  # Attachment
    # Identifier / ContactPoint: a string value that is not a code or quantity.
    return isinstance(value.get('value'), str) and not keys & {'code', 'unit', 'coding'}


def _scrub(value, depth, list_cap, str_cap, only):
    if isinstance(value, str):
        return value[:str_cap]
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    if depth >= MAX_DEPTH:
        return None
    if isinstance(value, list):
        items = (_scrub(v, depth + 1, list_cap, str_cap, only) for v in value[:list_cap])
        return [v for v in items if v not in (None, {}, [])]
    if isinstance(value, dict):
        if _identifying_shape(value):
            return None
        out = {}
        for key, item in value.items():
            key = str(key)
            # A resource narrative is {"status", "div"}; CodeableConcept.text
            # is a plain string and stays.
            if _dropped(key) or (key == 'text' and isinstance(item, dict)):
                continue
            if only is not None and key not in only:
                continue
            item = _scrub(item, depth + 1, list_cap, str_cap, only)
            if item not in (None, {}, []):
                out[key] = item
        return out
    return None


def deidentify(metadata):
    """*metadata* without patient-identifying fields, bounded; ``{}`` if nothing is left.

    Too large, it is trimmed in steps -- shorter lists, shorter strings, then
    only the coded fields -- so the code's own names always survive.
    """
    if not isinstance(metadata, dict):
        return {}
    steps = ((50, 1000, None), (10, 300, None), (3, 100, None), (3, 100, _CODE_KEYS),
             (1, 60, _CODE_KEYS))
    for list_cap, str_cap, only in steps:
        cleaned = _scrub(metadata, 0, list_cap, str_cap, only) or {}
        if len(json.dumps(cleaned, ensure_ascii=False, default=str)) <= MAX_JSON:
            return cleaned
    return {}
