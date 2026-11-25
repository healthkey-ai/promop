"""The answers an update of a revisable questionnaire starts from: the patient's
record as it stands now, read back through the questionnaire's record mapping
(PROlog's PROLOG_REVISION_ANSWERS_PROVIDER, RUN-4).

A record mapping (``survey_record_mapping``) turns answers into the record.
Once later questionnaires (a check-in) have changed the record, the answers the
patient last gave to this one no longer describe it: copied back into an update
and submitted again, they would write the old values over the newer ones. So an
update starts from the record instead, wherever the mapping says which answer
gives a field, and from the last answers everywhere else:

- a record field becomes the answer its rule takes (an option, a date at its
  stored precision, a number or text); where several options give the same
  value, the last answer is kept if it is one of them, or nothing is said; a
  last answer the record now contradicts, with no answer for what it says
  instead (a treatment no longer received, with no end date told), is left
  out, so the question is asked again rather than answered wrongly;
- each numbered line of therapy becomes its treatment's answers: the regimen
  (and the medicine names after it), its dates, outcome and details;
- an answer from the record that its question's conditions would hide opens
  its way: the earlier answers those conditions name (``eq``, ``in``,
  ``contains``) are set, following the questionnaire's own ``visible_if``.

PROlog checks every answer returned as one given now and drops what does not
hold, so this offers what it can and never needs to be exact about the rest.
"""
from __future__ import annotations

import datetime as dt
import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

_PRECISIONS = ('day', 'month', 'year')
# An answer the record contradicts and cannot replace: left out of the update.
ASK_AGAIN: dict = {}
_LINE_DATES = {'start_date': 'start_date', 'end_date': 'end_date', 'progression_date': 'progression_date'}


def revision_answers(participant_id, definition: dict[str, Any], previous: dict[str, dict]) -> dict[str, dict]:
    """PROLOG_REVISION_ANSWERS_PROVIDER: ``previous`` with what the patient's
    record says now laid over it, for the questionnaire ``definition``."""
    from omop_core.models import PatientRecord, Person
    from patient_portal.consent import record_permitted
    from patient_portal.survey_record_mapping import mapping_for

    mapping = mapping_for(definition['slug'], definition['version'])
    person = Person.objects.filter(person_id=participant_id).first() if participant_id is not None else None
    record = PatientRecord.objects.filter(person=person).first() if person else None
    if mapping is None or record is None or not record_permitted(person):
        return previous
    questions = _questions(definition)
    answers = dict(previous)
    found = _from_record_fields(mapping, record, answers, questions)
    found.update(_from_lines(mapping, person, record, {**answers, **found}, questions))
    for key, answer in found.items():
        if answer is ASK_AGAIN:
            answers.pop(key, None)
        else:
            answers[key] = answer
    _open_the_way(definition, answers, {k: v for k, v in found.items() if v is not ASK_AGAIN})
    return answers


# --- reading one value back -------------------------------------------------

def _questions(definition: dict[str, Any]) -> dict[str, dict]:
    """Each question by key, with its section's conditions in front of its own."""
    out = {}
    for section in definition.get('sections', []):
        for q in section.get('questions', []):
            out[q['key']] = {**q, '_conditions': [*(section.get('visible_if') or []), *(q.get('visible_if') or [])]}
    return out


def _same(a, b) -> bool:
    if isinstance(a, bool) or isinstance(b, bool):
        return a is b
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return a == b
    return str(a).strip().casefold() == str(b).strip().casefold()


def _option_for(values: dict[str, Any], value, current: dict | None) -> dict | None:
    """The option a rule's ``values`` give ``value`` from, or None when that is
    not one option and the current answer is not among those that give it."""
    options = [key for key, given in values.items() if _same(given, value)]
    held = (current or {}).get('option')
    if held in options:
        return current
    if len(options) == 1:
        return {'option': options[0]}
    if not options and held in values:
        return ASK_AGAIN  # the last answer says what the record no longer does
    return None


def _date_answer(day, precision: str | None, question: dict | None) -> dict | None:
    """A stored date as a date answer, no finer than it was told nor than the question takes."""
    if day is None:
        return None
    if isinstance(day, str):
        try:
            day = dt.date.fromisoformat(day[:10])
        except ValueError:
            return None
    allowed = ((question or {}).get('config') or {}).get('precision') or list(_PRECISIONS)
    told = precision if precision in _PRECISIONS else 'day'
    usable = [p for p in _PRECISIONS[_PRECISIONS.index(told):] if p in allowed]
    if not usable:
        return None
    return {'date': {'day': day.isoformat(), 'month': day.strftime('%Y-%m'), 'year': day.strftime('%Y')}[usable[0]]}


def _answer_for(rule: dict, value, precision, question: dict | None, current: dict | None) -> dict | None:
    """The answer a value rule (``answer`` + ``values``, a date, a number or text) takes ``value`` from."""
    if value is None or value == '' or not question:
        return None
    if 'values' in rule:
        return _option_for(rule['values'], value, current)
    if question.get('type') == 'date' or isinstance(value, (dt.date, dt.datetime)):
        return _date_answer(value, precision, question)
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return {'number': value}
    return {'text': str(value)}


def _from_record_fields(mapping: dict, record, answers: dict, questions: dict) -> dict[str, dict]:
    found: dict[str, dict] = {}
    for rule in mapping.get('record', []):
        key = rule.get('answer')
        if not key or key not in questions:
            continue  # a constant or a submission date says nothing about an answer
        value = getattr(record, rule['field'], None)
        precision = getattr(record, rule['precision_field'], None) if rule.get('precision_field') else None
        answer = _answer_for(rule, value, precision, questions.get(key), found.get(key) or answers.get(key))
        if answer is not None:
            found[key] = answer
    return found


# --- lines of therapy -------------------------------------------------------

def _regimen_answers(line_rule: dict, source_value: str | None, current: dict) -> dict[str, dict]:
    """The treatment's option, from the label the mapping wrote (and the names after it)."""
    regimen = line_rule.get('regimen') or {}
    if not source_value or not regimen.get('answer'):
        return {}
    for option, label in regimen.get('labels', {}).items():
        if source_value == label:
            return {regimen['answer']: {'option': option}}
        match = re.fullmatch(re.escape(label) + r' \((.+)\)', source_value)
        if match:
            out = {regimen['answer']: {'option': option}}
            if line_rule.get('names'):
                out[line_rule['names']] = {'text': match.group(1)}
            return out
    return {}


def _lines_now(person, record, disease: str | None) -> dict[int, dict]:
    """A cancer's lines as the record reads them now, by number (the primary
    cancer's without ``disease``): each with its Episode, the record's derived
    fields (outcome, when it came back: the primary cancer's only) and its detail."""
    from omop_core.services.disease_episodes import primary_disease_slug
    from omop_core.services.episode_service import _line_parent
    from omop_core.services.therapy_errors import TherapyLineValidationError
    from patient_portal.survey_record_mapping import _existing_lines

    if not primary_disease_slug(person):
        disease = None  # as apply_response files a line for a record naming no cancer yet
    try:
        _, primary = _line_parent(person, disease, None)
    except TherapyLineValidationError:
        return {}  # a cancer the record does not have
    episodes = _existing_lines(person, disease)
    derived: dict[int, dict] = {}
    if primary:
        try:
            from omop_core.api.lines import lines_of_therapy

            for entry in lines_of_therapy(record, {}) or []:
                if isinstance(entry.get('line'), int):
                    derived.setdefault(entry['line'], entry)
        except Exception:  # noqa: BLE001 - a derivation that fails leaves the Episodes' own dates
            logger.exception('survey_record_revision: lines of therapy could not be derived for person %s',
                             person.person_id)
    return {
        number: {'episode': episode, 'entry': derived.get(number, {}), 'detail': getattr(episode, 'detail', None)}
        for number, episode in episodes.items()
    }


def _from_lines(mapping: dict, person, record, answers: dict, questions: dict) -> dict[str, dict]:
    from omop_core.api.serializers import outcome_aliases

    by_disease: dict[str | None, dict[int, dict]] = {}
    aliases = outcome_aliases(record.disease)
    found: dict[str, dict] = {}
    for line_rule in mapping.get('therapy_lines', []):
        number = line_rule.get('line')
        if not isinstance(number, int):
            continue  # "next"/"latest" lines are a check-in's: they follow the record, not an answer
        disease = line_rule.get('disease')
        if disease not in by_disease:
            by_disease[disease] = _lines_now(person, record, disease)
        now = by_disease[disease].get(number)
        if now is None:
            continue
        episode, entry, detail = now['episode'], now['entry'], now['detail']
        current = {**answers, **found}
        found.update(_regimen_answers(line_rule, episode.episode_source_value, current))
        # The line's own dates first (written as each answer is applied); the
        # record's derivation, refreshed after, for the date it alone holds.
        dates = {
            'start_date': episode.episode_start_date or entry.get('start_date'),
            'end_date': episode.episode_end_date or entry.get('end_date'),
            'progression_date': entry.get('progression_date'),
        }
        for key, field_name in _LINE_DATES.items():
            question_key = line_rule.get(key)
            if question_key:
                answer = _date_answer(dates[field_name], getattr(detail, f'{field_name}_precision', None),
                                      questions.get(question_key))
                if answer is not None:
                    found[question_key] = answer
        outcome = line_rule.get('outcome')
        if outcome and entry.get('outcome'):
            stored = aliases.get(entry['outcome'], entry['outcome'])
            values = {option: aliases.get(value, value) for option, value in outcome['values'].items()}
            answer = _option_for(values, stored, {**answers, **found}.get(outcome['answer']))
            if answer is not None:
                found[outcome['answer']] = answer
        if detail is not None:
            for rule in line_rule.get('details', []):
                key = rule.get('answer')
                if not key or key not in questions:
                    continue
                value = getattr(detail, rule['field'], None)
                precision = getattr(detail, f"{rule['field']}_precision", None)
                answer = _answer_for(rule, value, precision, questions.get(key), {**answers, **found}.get(key))
                if answer is not None:
                    found[key] = answer
    return found


# --- opening the way to an answer ---------------------------------------------

def _settable(condition: dict, answers: dict) -> dict | None:
    """The answer that makes a simple condition hold, or None (one this cannot make true)."""
    key, op = condition.get('question'), condition.get('op')
    if not key:
        return None
    if op == 'eq':
        return {'option': condition['value']}
    if op == 'in':
        held = (answers.get(key) or {}).get('option')
        return {'option': held if held in condition['values'] else condition['values'][0]}
    if op == 'contains':
        options = list((answers.get(key) or {}).get('options') or [])
        return {'options': [*options, condition['value']]}
    return None


def _satisfy(conditions: list[dict], answers: dict, questions: dict, protected: set[str], depth: int = 0) -> bool:
    from prolog_surveys.engine.visibility import conditions_hold

    if depth > len(questions):
        return False  # a definition is a DAG, so this is never reached; it bounds a malformed one
    for condition in conditions:
        if conditions_hold([condition], answers):
            continue
        if 'any' in condition:
            for branch in condition['any']:
                trial = dict(answers)
                if _satisfy([branch], trial, questions, protected, depth + 1):
                    answers.clear()
                    answers.update(trial)
                    break
            else:
                return False
            continue
        key = condition.get('question')
        answer = _settable(condition, answers)
        if answer is None or key in protected or key not in questions:
            return False  # the record said otherwise, or nothing could make it hold
        answers[key] = answer
        if not _satisfy(questions[key]['_conditions'], answers, questions, protected, depth + 1):
            return False
    return True


def _open_the_way(definition: dict, answers: dict, found: dict[str, dict]) -> None:
    """Set the earlier answers that show each answer the record gave, in
    presentation order, never changing an answer the record gave itself."""
    questions = _questions(definition)
    protected = set(found)
    for key in questions:
        if key in found:
            trial = dict(answers)
            if _satisfy(questions[key]['_conditions'], trial, questions, protected):
                answers.clear()
                answers.update(trial)
