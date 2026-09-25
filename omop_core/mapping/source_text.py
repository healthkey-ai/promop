"""Reduce a drug source description to the part worth searching on.

Dose and form words are shared by most RxNorm names, so a trigram index asked
for "ASPIRIN 81 MG ORAL TABLET" returns over a million rows that the recheck
then throws away. The ingredient and its strength are what actually select a
concept.
"""
import re

_UNITS: frozenset[str] = frozenset({
    'MG', 'MCG', 'UG', 'G', 'GM', 'KG', 'ML', 'L', 'MEQ', 'MMOL', 'MOL',
    'UNT', 'UNIT', 'UNITS', 'IU', 'ACTUAT', 'HR', 'HOUR', 'HOURS', 'PERCENT',
})

_FORMS: frozenset[str] = frozenset({
    'ORAL', 'TABLET', 'TABLETS', 'CAPSULE', 'CAPSULES', 'SOLUTION', 'INJECTION',
    'INJECTABLE', 'SUSPENSION', 'SYRUP', 'ELIXIR', 'CREAM', 'OINTMENT', 'LOTION',
    'GEL', 'PATCH', 'SPRAY', 'POWDER', 'GRANULES', 'SUPPOSITORY', 'INHALANT',
    'INHALATION', 'INHALER', 'TOPICAL', 'OPHTHALMIC', 'OTIC', 'NASAL', 'RECTAL',
    'VAGINAL', 'INTRAVENOUS', 'SUBCUTANEOUS', 'INTRAMUSCULAR', 'EXTENDED',
    'DELAYED', 'RELEASE', 'PROLONGED', 'SUSTAINED', 'CHEWABLE', 'COATED', 'FILM',
    'PREFILLED', 'SYRINGE', 'PEN', 'VIAL', 'AMPULE', 'AMPOULE', 'KIT', 'PACK',
    'DOSE', 'DISINTEGRATING', 'PRODUCT', 'FORM',
})

NOISE_TOKENS: frozenset[str] = _UNITS | _FORMS

# Hyphens stay inside a token so "L-THYROXINE" and "5-FU" survive intact.
_SPLIT = re.compile(r'[\s/,;()\[\]]+')

# Digits alone are a poor narrowing key, so keep a real word as the anchor.
_HAS_WORD = re.compile(r'[A-Z]{3,}')


def narrowing_text(source_value: str | None) -> str | None:
    """The distinctive part of a drug name, or None when narrowing would not help.

    None means the caller should search the original text: nothing was dropped,
    or what is left is too thin to select on.
    """
    tokens: list[str] = [t for t in _SPLIT.split((source_value or '').upper()) if t]
    kept: list[str] = [t for t in tokens if t not in NOISE_TOKENS]
    if not kept or len(kept) == len(tokens):
        return None
    narrowed = ' '.join(kept)
    return narrowed if _HAS_WORD.search(narrowed) else None
