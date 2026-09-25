"""Reduce a drug source description to the ingredient worth searching on.

Dose and form words are shared by most drug names, so a trigram index asked for
"ASPIRIN 81 MG ORAL TABLET" returns over a million rows that the recheck then
throws away. The ingredient is what actually selects a concept.

Anything left in the key has to appear in the concept name, because word
similarity compares the whole key against an extent of the name. A stray
"CONTAINING" or "FOR" is enough to push the exact match below the threshold,
so the key keeps ingredient words only.
"""
import re

_UNITS: frozenset[str] = frozenset({
    'MG', 'MCG', 'UG', 'G', 'GM', 'KG', 'ML', 'L', 'IU', 'HR',
    'MICROGRAM', 'MICROGRAMS', 'MILLIGRAM', 'MILLIGRAMS', 'GRAM', 'GRAMS',
    'MEQ', 'MMOL', 'MOL', 'UNT', 'UNIT', 'UNITS', 'ACTUAT', 'HOUR', 'HOURS',
    'PERCENT', 'LITRE', 'LITER',
})

_FORMS: frozenset[str] = frozenset({
    'ORAL', 'TABLET', 'TABLETS', 'CAPSULE', 'CAPSULES', 'SOLUTION', 'INJECTION',
    'INJECTABLE', 'SUSPENSION', 'SYRUP', 'ELIXIR', 'CREAM', 'OINTMENT', 'LOTION',
    'PATCH', 'SPRAY', 'POWDER', 'GRANULES', 'SUPPOSITORY', 'INHALANT',
    'INHALATION', 'INHALER', 'TOPICAL', 'OPHTHALMIC', 'OTIC', 'NASAL', 'RECTAL',
    'VAGINAL', 'INTRAVENOUS', 'SUBCUTANEOUS', 'INTRAMUSCULAR', 'PARENTERAL',
    'EXTENDED', 'DELAYED', 'RELEASE', 'PROLONGED', 'SUSTAINED', 'CHEWABLE',
    'COATED', 'FILM', 'PREFILLED', 'SYRINGE', 'VIAL', 'AMPULE', 'AMPOULE',
    'DISINTEGRATING', 'DOSE', 'DOSAGE',
})

# SNOMED names a drug "X-containing product", so these mark a drug as reliably
# as a dose form does.
_PHRASING: frozenset[str] = frozenset({'CONTAINING', 'PRODUCT', 'PRODUCTS'})

# Seeing one of these is what says "this is a drug description". Function words
# are deliberately absent: "Disorder of the liver" must not look like a drug.
DRUG_SIGNALS: frozenset[str] = _UNITS | _FORMS | _PHRASING

_FUNCTION_WORDS: frozenset[str] = frozenset({
    'AND', 'OR', 'AS', 'FOR', 'WITH', 'THE', 'OF', 'PER', 'IN', 'BY', 'ONLY',
    'PRECISELY', 'CONVENTIONAL', 'FORM', 'FORMS',
})

# Dropped from the key, though not all of them mark a drug on their own.
_STOP_WORDS: frozenset[str] = DRUG_SIGNALS | _FUNCTION_WORDS

# Hyphens separate words here, so "Norfloxacin-containing" yields the ingredient.
_SPLIT = re.compile(r'[^A-Z0-9]+')

# Strengths and one or two letter units carry no ingredient, and a key word that
# is absent from the name costs the exact match.
_INGREDIENT = re.compile(r'^[A-Z]{3,}$')


def narrowing_text(source_value: str | None) -> str | None:
    """The ingredient words of a drug name, or None to search the original text.

    None when nothing marks this as a drug description, or when no ingredient
    word survives. Both cases keep the caller on its existing wider search.
    """
    tokens: list[str] = [t for t in _SPLIT.split((source_value or '').upper()) if t]
    if not any(t in DRUG_SIGNALS for t in tokens):
        return None
    kept: dict[str, None] = {
        t: None for t in tokens if _INGREDIENT.match(t) and t not in _STOP_WORDS
    }
    # Deduplicated because the key has to match one extent of the name, and
    # "granisetron (as granisetron hydrochloride)" repeats its ingredient.
    return ' '.join(kept) if kept else None
