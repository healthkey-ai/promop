"""The answers an update of a revisable questionnaire starts from: the patient's
last answers, with what their record says differently now (PROlog's
PROLOG_REVISION_ANSWERS_PROVIDER, RUN-4).

A record mapping (``survey_record_mapping``) turns answers into the record.
Later questionnaires (a check-in) change the record too; copied back into an
update and submitted again, the patient's last answers would write their old
values over those newer ones. So the last answers are replayed through the
mapping, and where the record now holds something other than they would write,
the update takes it from the record instead. Everything else stays exactly as
the patient answered it:

- a record field differing from what the last answers give becomes the answer
  its rule takes (an option, a date at its stored precision, a number, text).
  A field several questions write (``relapse_count``) cannot be traced to one
  of them and is left alone; so is a rule whose ``when`` the last answers do
  not meet, and a field the last answers give nothing for unless its question
  was left unanswered (the mapping never clears a value, so the record's may
  be one the patient has since taken back);
- each numbered line differing from what the last answers give: its regimen
  (and the names after it), dates, outcome and details, each read from the
  line itself (its own outcome and progression, not the record's summary of
  later lines);
- a last answer the record contradicts, with no single answer for what it says
  now (a treatment no longer received, no end date told; a response since
  assessed, of an unknown kind), is left out, so the question is asked again;
- an answer from the record that its conditions would hide opens its way only
  through questions the patient left unanswered, and only where one answer
  does it (``eq``, ``contains``, an ``in`` the last answer already meets): an
  answer the patient gave is never changed to show one.

PROlog checks every answer returned as one given now and drops what does not
hold (a hidden answer among them).
"""
from __future__ import annotations

import datetime as dt
import re
from collections import Counter
from typing import Any

_PRECISIONS = ('day', 'month', 'year')
# An answer the record contradicts and cannot replace: left out of the update.
ASK_AGAIN: dict = {}


def revision_answers(participant_id, definition: dict[str, Any], previous: dict[str, dict]) -> dict[str, dict]:
    """PROLOG_REVISION_ANSWERS_PROVIDER: ``previous`` with what the patient's
    record says differently now laid over it, for the questionnaire ``definition``."""
    from omop_core.models import PatientRecord, Person
    from patient_portal.consent import record_permitted
    from patient_portal.survey_record_mapping import (
        mapping_for,
        record_patch,
        therapy_lines,
    )

    mapping = mapping_for(definition['slug'], definition['version'])
    person = Person.objects.filter(person_id=participant_id).first() if participant_id is not None else None
    record = PatientRecord.objects.filter(person=person).first() if person else None
    if mapping is None or record is None or not record_permitted(person):
        return previous
    questions = _questions(definition)
    today = dt.date.today()
    # What the last answers write, to tell what has changed in the record since.
    written = record_patch(mapping, previous, today)
    written_lines = {line['index']: line for line in therapy_lines(mapping, previous, today)}
    found = _changed_record_fields(mapping, record, previous, written, questions)
    found.update(_changed_lines(mapping, person, record, previous, written_lines, questions))
    answers = dict(previous)
    for key, answer in found.items():
        if answer is ASK_AGAIN:
            answers.pop(key, None)
        else:
            answers[key] = answer
    _open_the_way(answers, previous, {k for k, v in found.items() if v is not ASK_AGAIN}, questions)
    return answers


# --- values and answers -----------------------------------------------------

def _questions(definition: dict[str, Any]) -> dict[str, dict]:
    """Each question by key, with its section's conditions in front of its own."""
    out = {}
    for section in definition.get('sections', []):
        for q in section.get('questions', []):
            out[q['key']] = {**q, '_conditions': [*(section.get('visible_if') or []), *(q.get('visible_if') or [])]}
    return out


def _answered(answer: dict | None) -> bool:
    return bool(answer) and not answer.get('skipped')


def _norm(value):
    """A value as the record and the mapping can both be compared by."""
    if isinstance(value, (dt.datetime, dt.date)):
        return value.isoformat()[:10]
    if isinstance(value, str):
        text = value.strip()
        return text[:10] if re.fullmatch(r'\d{4}-\d{2}-\d{2}(T.*)?', text) else text.casefold()
    return value


def _same(a, b) -> bool:
    if isinstance(a, bool) or isinstance(b, bool):
        return a is b
    return _norm(a) == _norm(b)


def _option_for(values: dict[str, Any], value, current: dict | None) -> dict | None:
    """The option a rule's ``values`` give ``value`` from. The current answer
    stays if it is one of them; else the one option if there is one; else, if
    the current answer is one the rule knows (so the record contradicts it),
    ASK_AGAIN; else None (nothing to say)."""
    options = [key for key, given in values.items() if _same(given, value)]
    held = (current or {}).get('option')
    if held in options:
        return current
    if len(options) == 1:
        return {'option': options[0]}
    if held in values:
        return ASK_AGAIN
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


# --- record fields ----------------------------------------------------------

def _changed_record_fields(mapping: dict, record, previous: dict, written: dict, questions: dict) -> dict[str, dict]:
    from patient_portal.survey_record_mapping import _holds

    rules = mapping.get('record', [])
    writers = Counter(rule['field'] for rule in rules)
    found: dict[str, dict] = {}
    for rule in rules:
        key, field_name = rule.get('answer'), rule['field']
        if not key or key not in questions or writers[field_name] > 1 or not _holds(rule, previous):
            continue
        now = getattr(record, field_name, None)
        if now is None or now == '':
            continue
        precision_field = rule.get('precision_field')
        now_precision = getattr(record, precision_field, None) if precision_field else None
        if field_name in written:
            if _same(now, written[field_name]) and (not precision_field or written.get(precision_field) in (None, now_precision)):
                continue  # unchanged since the last answers
        elif _answered(previous.get(key)):
            continue  # the last answers give it nothing on purpose; the record's may be stale
        answer = _answer_for(rule, now, now_precision, questions[key], previous.get(key))
        if answer is not None:
            found[key] = answer
    return found


# --- lines of therapy -------------------------------------------------------

def _regimen_answers(line_rule: dict, source_value: str | None) -> dict[str, dict]:
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


def _lines_now(person, disease: str | None) -> dict[int, dict]:
    """A cancer's lines as they stand, by number (the primary cancer's without
    ``disease``): each with its Episode, its detail, and its own outcome and
    progression (the line's LOT-n observations)."""
    from omop_core.services.disease_episodes import primary_disease_slug
    from omop_core.services.episode_service import _line_observations, _line_parent
    from omop_core.services.therapy_errors import TherapyLineValidationError
    from patient_portal.survey_record_mapping import _existing_lines

    if not primary_disease_slug(person):
        disease = None  # as apply_response files a line for a record naming no cancer yet
    try:
        _, primary = _line_parent(person, disease, None)
    except TherapyLineValidationError:
        return {}  # a cancer the record does not have
    lines = {}
    for number, episode in _existing_lines(person, disease).items():
        outcome = _line_observations(person, f'LOT-{number}-outcome', episode, primary).first()
        progression = _line_observations(person, f'LOT-{number}-progression', episode, primary).first()
        lines[number] = {
            'episode': episode,
            'detail': getattr(episode, 'detail', None),
            'outcome': outcome.value_as_string if outcome else None,
            'progression_date': progression.observation_date if progression else None,
        }
    return lines


def _changed_lines(mapping: dict, person, record, previous: dict, written_lines: dict, questions: dict) -> dict[str, dict]:
    from omop_core.api.serializers import outcome_aliases

    aliases = outcome_aliases(record.disease)
    by_disease: dict[str | None, dict[int, dict]] = {}
    found: dict[str, dict] = {}

    def current(key):
        return found.get(key) or previous.get(key)

    for index, line_rule in enumerate(mapping.get('therapy_lines', [])):
        number = line_rule.get('line')
        if not isinstance(number, int):
            continue  # "next"/"latest" lines are a check-in's: they follow the record, not an answer
        disease = line_rule.get('disease')
        if disease not in by_disease:
            by_disease[disease] = _lines_now(person, disease)
        now = by_disease[disease].get(number)
        if now is None:
            continue
        episode, detail = now['episode'], now['detail']
        given = written_lines.get(index)
        kwargs, given_detail = (given['kwargs'], given['detail']) if given else ({}, {})

        if not given or episode.episode_source_value != kwargs.get('source_value'):
            found.update(_regimen_answers(line_rule, episode.episode_source_value))
        for key, value in (('start_date', episode.episode_start_date), ('end_date', episode.episode_end_date),
                           ('progression_date', now['progression_date'])):
            question_key = line_rule.get(key)
            if not question_key or value is None:
                continue
            precision = getattr(detail, f'{key}_precision', None)
            if given and _same(kwargs.get(key), value) and given_detail.get(f'{key}_precision') in (None, precision):
                continue
            answer = _date_answer(value, precision, questions.get(question_key))
            if answer is not None:
                found[question_key] = answer
        outcome = line_rule.get('outcome')
        if outcome and now['outcome']:
            stored = aliases.get(now['outcome'], now['outcome'])
            said = kwargs.get('outcome')
            if not given or said is None or not _same(aliases.get(said, said), stored):
                values = {option: aliases.get(value, value) for option, value in outcome['values'].items()}
                answer = _option_for(values, stored, current(outcome['answer']))
                if answer is not None:
                    found[outcome['answer']] = answer
        if detail is None:
            continue
        for rule in line_rule.get('details', []):
            key = rule.get('answer')
            if not key or key not in questions:
                continue
            value = getattr(detail, rule['field'], None)
            if value is None or (given and rule['field'] in given_detail and _same(given_detail[rule['field']], value)):
                continue
            precision = getattr(detail, rule['precision_field'], None) if rule.get('precision_field') else None
            answer = _answer_for(rule, value, precision, questions.get(key), current(key))
            if answer is not None:
                found[key] = answer
    return found


# --- opening the way to an answer ---------------------------------------------

def _settable(condition: dict, answers: dict) -> dict | None:
    """The one answer that makes a simple condition hold, or None."""
    key, op = condition.get('question'), condition.get('op')
    if not key:
        return None
    if op == 'eq':
        return {'option': condition['value']}
    if op == 'in':
        held = (answers.get(key) or {}).get('option')
        if held in condition['values']:
            return answers[key]
        return {'option': condition['values'][0]} if len(condition['values']) == 1 else None
    if op == 'contains':
        options = list((answers.get(key) or {}).get('options') or [])
        return {'options': [*options, condition['value']]}
    return None


def _satisfy(conditions: list[dict], answers: dict, questions: dict, fixed: set[str], depth: int = 0) -> bool:
    from prolog_surveys.engine.visibility import conditions_hold

    if depth > len(questions):
        return False  # a definition is a DAG, so this is never reached; it bounds a malformed one
    for condition in conditions:
        if conditions_hold([condition], answers):
            continue
        if 'any' in condition:
            for branch in condition['any']:
                trial = dict(answers)
                if _satisfy([branch], trial, questions, fixed, depth + 1):
                    answers.clear()
                    answers.update(trial)
                    break
            else:
                return False
            continue
        key = condition.get('question')
        answer = _settable(condition, answers)
        if answer is None or key in fixed or key not in questions:
            return False  # the patient answered it, or the record did, or no one answer does it
        answers[key] = answer
        if not _satisfy(questions[key]['_conditions'], answers, questions, fixed, depth + 1):
            return False
    return True


def _open_the_way(answers: dict, previous: dict, changed: set[str], questions: dict) -> None:
    """Show each answer the record changed, through questions the patient left
    unanswered only; never changing an answer they or the record gave."""
    fixed = {key for key, answer in previous.items() if _answered(answer)} | changed
    for key in questions:
        if key in changed:
            trial = dict(answers)
            if _satisfy(questions[key]['_conditions'], trial, questions, fixed):
                answers.clear()
                answers.update(trial)
