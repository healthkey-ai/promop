"""Tri-state helpers for the pre-existing-condition answer (#1988).

``preexisting_conditions`` is the evidence (the categories a patient or clinician
recorded) and ``no_pre_existing_conditions`` is the answer to a separate question.
The flag cannot be derived from an empty list: an empty list is also what a record
carries before anyone asked. It therefore must not be stored alongside categories —
if both exist the list wins and the flag is invalidated, rather than the reverse.
"""


def condition_categories(value):
    """The recorded category codes, ignoring anything that is not a distinct code."""
    if not isinstance(value, (list, tuple)):
        return []
    return [str(item).strip() for item in value
            if isinstance(item, str) and str(item).strip()]


def contradicts_no_pre_existing_conditions(flag, categories):
    """True when an explicit "none" answer coexists with recorded categories."""
    return flag is True and bool(condition_categories(categories))
