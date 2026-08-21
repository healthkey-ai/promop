"""Curated demographic concept sets, resolved by natural key.

`Person.gender_concept`, `race_concept` and `ethnicity_concept` are standard OMOP
FKs, and derivation reads the concept before falling back to `*_source_value`. So
a demographic correction that writes only source text is silently outranked by
whatever concept is already there — both have to move together.

**Curated, not the whole vocabulary.** OMOP's `Race` holds 1,409 concepts and
`Ethnicity` holds 150 nationality-style entries (`Afghan`, `Albanian`, `Angolan`),
which is not the question a clinical form asks. What a picker should offer is the
five OMB race categories and the standard ethnicity pair. Anything a caller sends
is still preserved verbatim in `*_source_value`, so curation narrows the *coded*
answer without discarding what was actually recorded.

**Resolved by `(vocabulary_id, concept_code)`, never by a hardcoded id.** A
concept_id is a number some vocabulary release owns; the natural key is the thing
that stays true. This module exists partly because the opposite approach —
hardcoding 3000963 as a generic lab placeholder — turned every unmapped lab into a
haemoglobin result once a real vocabulary redefined that id.
"""
from omop_core.models import Concept

GENDER_VOCABULARY = 'Gender'
RACE_VOCABULARY = 'Race'
ETHNICITY_VOCABULARY = 'Ethnicity'

# (concept_code, display) in the order a picker should show them.
GENDER_CHOICES = (
    ('F', 'Female'),
    ('M', 'Male'),
    ('U', 'Unknown'),
)

# The five OMB / CDC race categories. Their concept_codes in OMOP's Race
# vocabulary are the single digits 1-5.
RACE_CHOICES = (
    ('1', 'American Indian or Alaska Native'),
    ('2', 'Asian'),
    ('3', 'Black or African American'),
    ('4', 'Native Hawaiian or Other Pacific Islander'),
    ('5', 'White'),
)

ETHNICITY_CHOICES = (
    ('Hispanic', 'Hispanic or Latino'),
    ('Not Hispanic', 'Not Hispanic or Latino'),
)

_KINDS = {
    'gender': (GENDER_VOCABULARY, GENDER_CHOICES),
    'race': (RACE_VOCABULARY, RACE_CHOICES),
    'ethnicity': (ETHNICITY_VOCABULARY, ETHNICITY_CHOICES),
}

# Spellings a caller may send for a curated answer. FHIR, free text and the
# projection's own abbreviations all differ from the OMOP display name, and a
# correction typed as "female" should not fall through to uncoded.
_ALIASES = {
    'gender': {
        'f': 'F', 'female': 'F', 'w': 'F', 'woman': 'F',
        'm': 'M', 'male': 'M', 'man': 'M',
        'u': 'U', 'unknown': 'U', 'other': 'U', 'ambiguous': 'U',
    },
    'race': {
        'white': '5', 'caucasian': '5',
        'black': '3', 'black or african american': '3', 'african american': '3',
        'asian': '2',
        'native hawaiian or other pacific islander': '4',
        'pacific islander': '4', 'native hawaiian': '4',
        'american indian or alaska native': '1',
        'american indian': '1', 'alaska native': '1',
    },
    'ethnicity': {
        'hispanic': 'Hispanic', 'hispanic or latino': 'Hispanic',
        'latino': 'Hispanic', 'latina': 'Hispanic', 'latinx': 'Hispanic',
        'not hispanic': 'Not Hispanic',
        'not hispanic or latino': 'Not Hispanic',
        'non-hispanic': 'Not Hispanic', 'non hispanic': 'Not Hispanic',
    },
}


def choices(kind):
    """The curated (code, display) pairs a picker should offer for `kind`."""
    return _KINDS[kind][1]


def resolve_concept_code(kind, value):
    """Map free text to a curated concept_code, or None if it is not one of them.

    None is a real answer: it means "recorded, but not one of the coded options".
    The caller keeps the raw text in `*_source_value` either way.
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    _vocabulary, options = _KINDS[kind]
    lowered = text.casefold()
    for code, display in options:
        if lowered in (code.casefold(), display.casefold()):
            return code
    return _ALIASES[kind].get(lowered)


def resolve_concept(kind, value):
    """Return the Concept for `value`, or None.

    None when the value is not a curated answer *or* when the vocabulary is not
    loaded — the caller must then clear the stored concept rather than leave a
    stale one outranking the new source value.
    """
    code = resolve_concept_code(kind, value)
    if code is None:
        return None
    vocabulary, _options = _KINDS[kind]
    return Concept.objects.filter(
        vocabulary_id=vocabulary, concept_code=code
    ).order_by('concept_id').first()
