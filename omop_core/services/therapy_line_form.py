"""Author the patient's therapy line from the shared form's flat line fields.

`first_line_*`, `second_line_*` and `later_*` are projections of a persisted
therapy line — an `Episode` grouping the DrugExposures given during it, plus its
`LOT-<n>-outcome` observation. Writing the column would be undone by the next
derivation (that is #2106), so the shared form routes these fields here: the
line is authored through the same service the therapy-lines editor uses and
derivation fills every column from it.

Only the first line of each group is a flat field, which is what derivation
writes back: `later_therapy` / `later_date` / `later_outcome` describe the first
3L+ line. Adding a fourth line stays in the therapy-lines editor.

Nothing here writes `user_edited_fields`: the marker means "this column is the
reader's, keep it against derivation", and that is exactly the wrong claim for a
value derivation is supposed to produce. Audit lives where the therapy-lines
editor already puts it — the Episode and its `TherapyLineAssertion`.
"""
from __future__ import annotations

from datetime import date

from django.utils.dateparse import parse_date

from omop_core.services.therapy_errors import MESSAGES, TherapyLineValidationError


#: Flat shared-form field -> (line number, the part of the line it describes).
THERAPY_LINE_FORM_FIELDS = {
    'first_line_therapy': (1, 'therapy'),
    'first_line_date': (1, 'start_date'),
    'first_line_outcome': (1, 'outcome'),
    'second_line_therapy': (2, 'therapy'),
    'second_line_date': (2, 'start_date'),
    'second_line_outcome': (2, 'outcome'),
    'later_therapy': (3, 'therapy'),
    'later_date': (3, 'start_date'),
    'later_outcome': (3, 'outcome'),
}


def _regimen_concept_id(code):
    """The HemOnc regimen concept the catalog row names, or None when unknown.

    The form's options are the catalog's own ``code`` values (see
    ``write_descriptor``), so a submitted value that is not one of them is a
    stale client, not a concept to guess at.
    """
    from omop_core.models import TherapyRegimen

    return (
        TherapyRegimen.objects.filter(code=code)
        .values_list('concept_id', flat=True)
        .first()
    )


def _outcome_aliases(disease):
    """Every spelling of a catalog outcome -> the value the line stores.

    A client may send the code, the value or the label (the therapy-lines
    serializer accepts all three); the stored observation always carries the
    value, which is what derivation reads back.
    """
    from omop_core.services.treatment_catalog import outcomes_for_disease

    return {
        alias: row['value']
        for row in outcomes_for_disease(disease or '')
        for alias in (row['code'], row['value'], row['label'])
    }


def author_lines_from_form(person, changes, *, disease=None, changed_by='system'):
    """Write the submitted line fields as therapy lines.

    Returns ``(applied, rejected)``: the field names written, and a
    ``{field: message}`` map for the ones refused. Messages come from the
    therapy-line catalog, so a refusal reads the same here as in the editor.
    """
    by_line = {}
    for name, value in changes.items():
        spec = THERAPY_LINE_FORM_FIELDS.get(name)
        if spec is not None:
            line_number, part = spec
            by_line.setdefault(line_number, {})[part] = (name, value)

    applied, rejected = [], {}
    wrote = False
    for line_number, parts in sorted(by_line.items()):
        names = [name for name, _value in parts.values()]
        kwargs = {}
        refusal = None

        if 'therapy' in parts:
            name, value = parts['therapy']
            code = value.strip() if isinstance(value, str) else value
            if not code:
                refusal = (names, MESSAGES['regimen_clear_requires_assertion'])
            else:
                concept_id = _regimen_concept_id(code)
                if concept_id is None:
                    refusal = (names, f'Unknown therapy: {code}. Choose one from the list.')
                else:
                    kwargs['regimen_concept_id'] = concept_id

        if refusal is None and 'start_date' in parts:
            name, value = parts['start_date']
            start = value if isinstance(value, date) else parse_date(str(value or ''))
            if start is None:
                refusal = (names, MESSAGES['start_date_required'])
            elif start > date.today():
                refusal = (names, MESSAGES['future_line'])
            else:
                kwargs['start_date'] = start

        if refusal is None and 'outcome' in parts:
            name, value = parts['outcome']
            aliases = _outcome_aliases(disease)
            # Empty is a clear: the writer deletes the outcome observation and
            # derivation reads the line as having no recorded outcome.
            if value in (None, ''):
                kwargs['outcome'] = ''
            elif value not in aliases:
                refusal = (names, 'Select an outcome for this disease.')
            else:
                kwargs['outcome'] = aliases[value]

        if refusal is not None:
            for name in refusal[0]:
                rejected[name] = refusal[1]
            continue

        try:
            from omop_core.services.episode_service import author_therapy_line
            author_therapy_line(
                person, line_number=line_number, disease=disease, replace=True, **kwargs)
        except TherapyLineValidationError as exc:
            # The catalog's text, never the exception's own: the service puts a
            # patient value in `detail` for logs and Python callers only.
            for name in names:
                rejected[name] = MESSAGES[exc.code]
            continue
        wrote = True
        applied.extend(names)

    if wrote:
        from omop_core.services.patient_record_service import refresh_patient_record
        refresh_patient_record(person)
    return applied, rejected
