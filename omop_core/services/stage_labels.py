"""The reviewed label for a staging value (#2618).

A staging fact often arrives as a SNOMED concept whose display carries the
semantic tag — "Stage 4 (qualifier value)" — and the derivation copied that
display into `PatientRecord.stage`. The record page, trial matching and the
download report then showed the tag, and the Arabic numeral disagreed with the
Roman one clinicians read.

The mapping lives in one dependency-free module so the derivation and any
writer agree; the data migration carries its own frozen copy.
"""
import re

#: SNOMED semantic tags. They say what kind of concept this is, not what the
#: patient has, so they never belong in the record.
SEMANTIC_TAGS = frozenset({
    'qualifier value', 'finding', 'disorder', 'situation', 'event',
    'observable entity', 'procedure', 'body structure',
    'morphologic abnormality', 'clinical finding', 'social context',
    'physical object', 'organism', 'substance', 'specimen',
    'pharmaceutical / biologic product', 'record artifact',
    'environment or geographical location', 'qualifier',
})

_ROMAN = {'0': '0', '1': 'I', '2': 'II', '3': 'III', '4': 'IV',
          'i': 'I', 'ii': 'II', 'iii': 'III', 'iv': 'IV'}

_TRAILING_PAREN = re.compile(r'\s*\(([^()]*)\)\s*$')
# The reviewed label family, with an optional AJCC prefix. A leading descriptor
# ("American Joint Committee on Cancer stage IA") and a value the importer
# truncated before its closing parenthesis both reduce to the label.
_STAGE_PREFIX = re.compile(
    r'^\s*(?:american joint committee on cancer\s+)?stage\s*'
    r'(?P<number>iv|iii|ii|i|0|[1-4])\s*(?P<suffix>[abc])?$',
    re.IGNORECASE,
)


def _without_semantic_tags(text: str) -> str:
    while True:
        match = _TRAILING_PAREN.search(text)
        if not match or match.group(1).strip().lower() not in SEMANTIC_TAGS:
            return text
        text = text[:match.start()].strip()


def normalise_stage_label(value):
    """Return the reviewed stage label for a stored/derived staging value.

    Only an overall stage is rewritten; TNM codes, "ISS II", "Rai 0" and other
    disease-specific systems are returned untouched apart from a semantic tag.
    """
    if not isinstance(value, str):
        return value
    text = _without_semantic_tags(value.strip())
    # The FHIR import truncates a long display at 60 characters, so a trailing
    # "(" may never close; it is still a tail, not a stage.
    text = re.sub(r'\s*\([^)]*$', '', text).strip()
    match = _STAGE_PREFIX.match(text)
    if match:
        suffix = (match.group('suffix') or '').upper()
        return f"Stage {_ROMAN[match.group('number').lower()]}{suffix}"
    return text
