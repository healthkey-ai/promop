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

_ROMAN = {'1': 'I', '2': 'II', '3': 'III', '4': 'IV',
          'i': 'I', 'ii': 'II', 'iii': 'III', 'iv': 'IV'}

_TRAILING_PAREN = re.compile(r'\s*\(([^()]*)\)\s*$')
_FULL_STAGE = re.compile(r'stage\s*(1|2|3|4|i{1,3}|iv)\b', re.IGNORECASE)
_PARTIAL_STAGE = re.compile(r'stage\s*([1-4])\s*([abc])\b', re.IGNORECASE)


def _without_semantic_tag(text: str) -> str:
    match = _TRAILING_PAREN.search(text)
    if match and match.group(1).strip().lower() in SEMANTIC_TAGS:
        return text[:match.start()].strip()
    return text


def normalise_stage_label(value):
    """Return the reviewed stage label for a stored/derived staging value.

    Only an overall stage is rewritten; TNM codes, "ISS II", "Rai 0" and other
    disease-specific systems are returned untouched apart from a semantic tag.
    """
    if not isinstance(value, str):
        return value
    text = _without_semantic_tag(value.strip())
    match = _FULL_STAGE.fullmatch(text)
    if match:
        return f'Stage {_ROMAN[match.group(1).lower()]}'
    partial = _PARTIAL_STAGE.fullmatch(text)
    if partial:
        return f'Stage {_ROMAN[partial.group(1)]}{partial.group(2).upper()}'
    return text
