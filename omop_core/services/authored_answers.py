"""Record fields whose value is an authored answer, never a formula output.

These fields are attestations (a snapshot-backed answer, or a self-assessment)
whose value means "the patient/clinician said so". A formula cannot tell an
empty list from "never assessed", so reactivating one — for example by importing
a curation export from before the field became an answer — silently converts
absence of evidence into a negative answer.

The producer guard, the evaluator and the export/import path all read this one
set; adding a field here keeps them in step.
"""

AUTHORED_ANSWER_FORMULA_FIELDS = frozenset({
    # Pre-existing conditions: the flag is the answer, not a derivation of an
    # empty category list (#1988).
    'no_pre_existing_conditions',
    # The two derived negatives (their formulas are deactivated; #5666).
    'no_active_infection_status',
    'no_other_active_malignancies',
    # Clinician/patient self-assessment (pregnancy), not a derived verdict.
    'no_pregnancy_or_lactation_status',
})


def is_authored_answer_field(field_name: str) -> bool:
    return field_name in AUTHORED_ANSWER_FORMULA_FIELDS
