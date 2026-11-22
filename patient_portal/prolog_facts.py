"""Facts about a patient that a PROlog questionnaire may declare (PROlog DEF-14,
RUN-23), so a question shows only when one holds.

PROlog asks once, when a signed-in patient starts a response to a questionnaire
that declares facts (``PROLOG_FACTS_PROVIDER``), and keeps the answer on the
response. A questionnaire declares the facts it reads by key, with the values
below; promop supplies those it knows from the record. A fact promop cannot
tell (no line yet, or nothing said about it) is left out, and PROlog treats it
as unknown: every condition on it is false.

| Fact | Values | From |
|---|---|---|
| ``latest_therapy_line_response`` | ``assessed``, ``not_assessed`` | The patient's latest line of therapy for their primary cancer: whether its response had been assessed when they last said (``TherapyLineDetail.response_assessed``). |
"""
from __future__ import annotations

from typing import Any, Callable

from omop_core.models import Person


def _latest_therapy_line_response(person: Person) -> str | None:
    from patient_portal.survey_record_mapping import _existing_lines

    lines = _existing_lines(person, None)
    if not lines:
        return None
    detail = getattr(lines[max(lines)], 'detail', None)
    assessed = getattr(detail, 'response_assessed', None)
    if assessed is None:
        return None
    return 'assessed' if assessed else 'not_assessed'


FACTS: dict[str, Callable[[Person], str | None]] = {
    'latest_therapy_line_response': _latest_therapy_line_response,
}


def facts_for(participant_id: Any, definition: dict[str, Any]) -> dict[str, str]:
    """PROLOG_FACTS_PROVIDER: the facts ``definition`` declares that promop
    knows for this patient. Only facts asked for are worked out."""
    wanted = [f['key'] for f in definition.get('facts', []) if f['key'] in FACTS]
    if not wanted:
        return {}
    person = Person.objects.filter(person_id=participant_id).first()
    if person is None:
        return {}
    out = {}
    for key in wanted:
        value = FACTS[key](person)
        if value is not None:
            out[key] = value
    return out
