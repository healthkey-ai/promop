"""Reviewed answer labels for boolean columns (#5663, #5676, #5667).

Dependency-free on purpose: `patient_form` and `write_descriptor` both read
it, and `patient_form` already imports `write_descriptor`, so defining the
table in either one would close a cycle.
"""
# Reviewed wording for both answers of a nullable boolean. `Yes`/`No` is the
#: generic pair; a row whose question is phrased as the positive half ("HIV
#: Positive") needs the negative answer spelled out, or the control answers a
#: question the patient was not asked (#5601). Descriptor-driven so the native
#: tab, the shared editor and a host's embedded Trial Details agree.
#:
#: Presentation only: it changes what the control says, never whether it can be
#: written. Naming a computed boolean here would be harmless for the same
#: reason, but the table does not: a pair on a node the reader cannot edit
#: would describe an answer the form does not take
#: (`test_reviewed_answer_words_do_not_collide_with_computed_nodes`).
#:
#: The three negatives that used to exist as separate columns (`no_hiv_status`
#: and its hepatitis siblings) were removed upstream in #2119 because a second
#: stored boolean could contradict the first. These labels are how the negative
#: answer is offered now: the same canonical positive column and two
#: spelled-out answers. Unanswered stays `null` -- the control's empty choice --
#: and is never inferred from or into a negative.
BOOLEAN_ANSWERS = {
    'hiv_status': ('HIV Positive', 'No HIV'),
    'hepatitis_b_status': ('Hepatitis B Positive', 'No Hepatitis B'),
    'hepatitis_c_status': ('Hepatitis C Positive', 'No Hepatitis C'),
    # These two already ask the negative question ("negative test result", "not
    # pregnant or lactating"), so a bare Yes/No inverts what the reader expects
    # (#5676). `False` on the test result is "not negative", which covers a
    # positive result as well as one that was not recorded.
    'pregnancy_test_result': ('Negative', 'Not negative'),
    'no_pregnancy_or_lactation_status': ('Not pregnant or lactating', 'Pregnant or lactating'),
    # The column records the disease, while a trial row may ask "No Plasma Cell
    # Leukemia"; the answer labels keep the fact's own polarity visible.
    'plasma_cell_leukemia': ('Present', 'Not present'),
}
