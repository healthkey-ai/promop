"""Refusals the therapy-line endpoint may show the client, named by a stable code.

The authoring service refuses bad input with messages written for the person
editing the line: which field is wrong and what to do about it. Those messages
must reach the client, but an exception's own text must not: a ``ValueError``
raised deeper in the stack can carry a patient value or an internal detail, and
CodeQL's ``py/stack-trace-exposure`` rule rightly flags ``str(exc)`` in a
response.

So the service raises :class:`TherapyLineValidationError` with a code from
:data:`MESSAGES`, and the API layer answers with the catalog's text for that
code. The response never serializes anything derived from the exception; any
detail the service attaches (an id, a submitted name) stays in the exception's
string, for logs and Python callers.
"""

#: code -> the message the client sees. Wording is what the therapy-line dialog
#: shows the user, so it says what to do, not what went wrong internally.
MESSAGES = {
    'line_not_owned': 'The therapy line must belong to this patient.',
    'line_not_found': 'The therapy line must belong to this person and line number.',
    'line_number_invalid': 'line_number must be a positive integer.',
    'unknown_disease': 'This record has no diagnosis of that cancer to file a line of therapy under.',
    'second_cancer_vocabulary': (
        'A line of therapy for a second cancer needs the Episode vocabulary '
        '(Disease First Occurrence, 32528) loaded.'
    ),
    'vocabulary_missing': 'The EHR type and drug-exposure field vocabulary must be loaded.',
    'vocabulary_lost': 'The treatment-regimen vocabulary disappeared during authoring.',
    'end_before_start': 'end_date cannot precede start_date.',
    'start_date_required': 'A therapy line requires a known start_date; no date is fabricated.',
    'future_line': 'A future therapy line is planned, not received treatment.',
    'unknown_regimen': 'Unknown regimen concept_id.',
    'regimen_clear_requires_assertion': (
        'Clearing a regimen requires the unknown-regimen assertion workflow.'
    ),
    'unknown_drug': 'Unknown drug concept_id.',
    'drug_exposure_id_invalid': 'drug_exposure_id must be a positive integer.',
    'duplicate_drug_source': 'Duplicate drug source_value; use distinct administration IDs.',
    'drug_not_on_line': 'drug_exposure_id must identify an active drug already linked to this line.',
    'ambiguous_drug_source': 'Ambiguous drug source_value; supply drug_exposure_id for each administration.',
    'duplicate_drug_exposure_id': 'Duplicate drug_exposure_id in the submitted composition.',
    'drug_dates_invalid': 'Drug dates require a known start_date and end_date cannot precede it.',
    'future_drug': 'A future drug administration is planned, not received treatment.',
}

#: code -> the request field the client should highlight, when there is one.
FIELDS = {
    'line_not_found': 'line_number',
    'line_number_invalid': 'line_number',
    'unknown_disease': 'disease',
    'second_cancer_vocabulary': 'disease',
    'end_before_start': 'end_date',
    'start_date_required': 'start_date',
    'future_line': 'start_date',
    'unknown_regimen': 'regimen_concept_id',
    'regimen_clear_requires_assertion': 'regimen_concept_id',
    'unknown_drug': 'drugs',
    'drug_exposure_id_invalid': 'drugs',
    'duplicate_drug_source': 'drugs',
    'drug_not_on_line': 'drugs',
    'ambiguous_drug_source': 'drugs',
    'duplicate_drug_exposure_id': 'drugs',
    'drug_dates_invalid': 'drugs',
    'future_drug': 'drugs',
}

#: What the client is told when a write lost the retry race. Static, so it can
#: be returned as-is; the exception carrying it is never serialized.
CONFLICT_MESSAGE = (
    'The treatment history is being edited concurrently. No changes were saved; retry the request.'
)


class TherapyLineValidationError(ValueError):
    """A refusal the client should see.

    ``code`` is a key of :data:`MESSAGES`. ``detail`` is an optional value for
    logs and Python callers (the offending id or name); it becomes part of the
    exception's string and nothing else.
    """

    def __init__(self, code, detail=None):
        if code not in MESSAGES:
            raise KeyError(f'Unknown therapy-line refusal code {code!r}')
        self.code = code
        self.detail = detail
        message = MESSAGES[code]
        if detail is not None:
            message = f'{message} [{detail}]'
        super().__init__(message)


def refusal_payload(exc):
    """The response body for a :class:`TherapyLineValidationError`.

    Everything in it comes from the catalog. The code is matched by equality
    against the catalog's own keys, so the value that is serialized is the
    catalog's string, not the attribute read off the exception.
    """
    code = next((known for known in MESSAGES if known == exc.code), None)
    if code is None:
        return None
    payload = {'detail': MESSAGES[code], 'code': code}
    field = FIELDS.get(code)
    if field is not None:
        payload['field'] = field
    return payload
