"""Shared validation/normalization for clinical assessment inputs.

The native serializer and the shared patient form must apply the same rules to
GELF criteria, FLIPI factors, and tumor grade. Keeping the rules here prevents
the shared path from accepting a value the native PATCH would reject.
"""
from omop_core.services import flipi
from omop_core.services.sample_disease_profiles import GELF_FACTORS
from omop_core.services.write_refusal import WriteRefused


def normalize_gelf_criteria_options(value):
    """Return the canonical comma-joined GELF keys, or raise ValueError."""
    if value is None:
        return None
    selected = {part.strip() for part in str(value).split(',') if part.strip()}
    if selected - GELF_FACTORS.keys():
        raise WriteRefused('Select recognized GELF criteria.')
    return ','.join(key for key in GELF_FACTORS if key in selected)


def normalize_flipi_score_options(value):
    """Return the canonical comma-joined FLIPI factors, or raise ValueError."""
    selected = flipi.parse_factors(value)
    return None if selected is None else ','.join(selected)


def normalize_tumor_grade(value):
    """Return the canonical tumor grade, or raise ValueError."""
    return flipi.normalize_grade(value)


_VALIDATORS = {
    'gelf_criteria_options': normalize_gelf_criteria_options,
    'flipi_score_options': normalize_flipi_score_options,
    'tumor_grade': normalize_tumor_grade,
}


def normalize_clinical_field(name, value):
    """Apply the field's shared validator, or return the value unchanged."""
    validator = _VALIDATORS.get(name)
    return validator(value) if validator else value
