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
from omop_core.services.therapy_transactions import (
    TherapyLineRejected, TherapyLineWriteConflict,
)


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


def _regimen_concept_id(value):
    """The HemOnc regimen concept a submitted therapy names, or None.

    The descriptor's option `code` is the catalog code, but a client may echo
    the record's derived display ("VRd") or the concept id itself back at us.
    All three name the same regimen, so all three are accepted; anything else is
    a stale client, not a concept to guess at.
    """
    from django.db.models import Q

    from omop_core.models import TherapyRegimen

    code = value.strip() if isinstance(value, str) else value
    if not code:
        return None
    rows = TherapyRegimen.objects.filter(concept__isnull=False)
    if isinstance(code, str) and code.isdigit():
        row = rows.filter(concept_id=int(code)).first()
        if row is not None:
            return row.concept_id
    row = rows.filter(Q(code=code) | Q(title=code)).first()
    return row.concept_id if row is not None else None


def _same_line_value(value, current):
    """Whether a submitted line value is the record's own value echoed back.

    The native editor PATCHes the whole GET representation, so every line field
    arrives on every save — as the derived display name, or null. Treating that
    as an instruction would rewrite the line on each unrelated edit (and refuse,
    since the display name is not a catalog code).
    """
    if value in (None, '') and current in (None, ''):
        return True
    if isinstance(current, date):
        parsed = value if isinstance(value, date) else parse_date(str(value or ''))
        return parsed == current
    return str(value) == str(current or '')


def changed_line_values(changes, record):
    """The submitted line fields that actually differ from the record."""
    return {
        name: value for name, value in changes.items()
        if name in THERAPY_LINE_FORM_FIELDS
        and not _same_line_value(value, getattr(record, name, None))
    }


def _line_has_drugs(person, line_number):
    from omop_core.services.mappings import CONCEPT_DRUG_EXPOSURE_FIELD
    from omop_oncology.models import Episode, EpisodeEvent

    episode_ids = Episode.objects.filter(
        person=person, episode_number=line_number,
        episode_concept_id=32531,
    ).values_list('episode_id', flat=True)
    return EpisodeEvent.objects.filter(
        episode_id__in=list(episode_ids),
        episode_event_field_concept_id=CONCEPT_DRUG_EXPOSURE_FIELD,
    ).exists()


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


def author_lines_from_form(person, changes, *, disease=None):
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
                elif _line_has_drugs(person, line_number):
                    # The line's composition is owned by the therapy-lines
                    # editor: asserting a different regimen here would leave the
                    # stored drug exposures describing the old one.
                    refusal = (names, 'This line already has a recorded composition; '
                                      'change its therapy in the therapy-lines editor.')
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
            result = author_therapy_line(
                person, line_number=line_number, disease=disease, replace=True, **kwargs)
        except TherapyLineValidationError as exc:
            # The catalog's text, never the exception's own: the service puts a
            # patient value in `detail` for logs and Python callers only.
            for name in names:
                rejected[name] = MESSAGES[exc.code]
            continue
        except TherapyLineWriteConflict as exc:
            for name in names:
                rejected[name] = str(exc)
            continue
        except TherapyLineRejected as exc:
            for name in names:
                rejected[name] = exc.message
            continue
        if result is None or result.episode is None:
            # The treatment-regimen vocabulary is not loaded: the service
            # answered "nothing written", which must not read as a save.
            for name in names:
                rejected[name] = MESSAGES['vocabulary_missing']
            continue
        wrote = True
        applied.extend(names)

    if wrote:
        from omop_core.models import PatientRecord
        from omop_core.services.patient_record_service import refresh_patient_record
        # A marker left by an older direct write would pin the old value and
        # hide the line that was just authored; the line owns these columns now.
        stale = set(applied)
        record = PatientRecord.objects.get(person=person)
        pending = set(record.user_edited_fields or [])
        if pending & stale:
            record.user_edited_fields = sorted(pending - stale)
            record.save(update_fields=['user_edited_fields'])
        refresh_patient_record(person)
    return applied, rejected
