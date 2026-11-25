"""Questionnaire answers into the patient's own record, through governed mappings.

PROlog keeps a response's answers in its own tables: they are what the person
said, not clinical data (PROlog DEP-7, docs/prolog-surveys.md). A deployment
that wants some of them in the person's record supplies a *record mapping*:
data, reviewed like a survey definition, naming which answers of which survey
version fill which PatientRecord field, and which describe lines of therapy.
Nothing here knows any deployment's questions.

When a response to a mapped survey version is submitted, promop applies its
mapping as the patient:

- record fields through ``apply_patient_record_patch``, the save path of the
  patient's own ``PATCH /api/v1/patient-records/me/``, so every value is
  validated as an edit is, typed patient self-report (32865) with PATIENT_SELF
  provenance, and recorded in the record's revision history;
- lines of therapy through ``author_therapy_line``, typed patient
  self-report and asserted by ``questionnaire``.

A mapping writes only what an answer gives: a skipped or unanswered question,
an option the mapping does not list, or a rule whose ``when`` does not hold
writes nothing, and never clears a field. Applying a response again writes the
same values, so a retry or a backfill is safe; a later response overwrites
what an earlier one wrote.

A mapping file (JSON, ``mapping_version`` 1)::

    {
      "mapping_version": 1,
      "survey": "<survey slug>",
      "versions": ["1.0.0"],                 # the exact versions it was reviewed against
      "record": [
        {"field": "stage", "answer": "q_stage", "values": {"stage_2": "II"}},
        {"field": "diagnosis_date", "answer": "q_diagnosed",
         "precision_field": "diagnosis_date_precision"},
        {"field": "disease", "value": "Example disease",
         "when": [{"question": "q_disease", "op": "contains", "value": "example"}]},
        {"field": "ecog_assessment_date", "submitted_on": true,
         "when": [{"question": "q_activity", "op": "answered"}]}
      ],
      "therapy_lines": [
        {"line": 1, "disease": "Example disease",
         "details": [{"field": "clinical_trial", "answer": "q_trial",
                      "values": {"yes": "yes", "no": "no"}}],
         "regimen": {"answer": "q_treatment", "labels": {"chemo": "Chemotherapy"}},
         "names": "q_treatment_names",
         "start_date": "q_started", "end_date": "q_ended",
         "outcome": {"answer": "q_response", "values": {"complete": "CR"}},
         "progression_date": "q_progressed"}
      ]
    }

Record rules take their value from exactly one of: ``answer`` (a question key;
``values`` maps an option to the value written, and is required for a choice;
a date answer is written as its first day, with its precision in
``precision_field`` when named), ``value`` (a constant), or ``submitted_on``
(the submission's date). ``when`` is a list of PROlog conditions, evaluated
with PROlog's own engine, so it means what ``visible_if`` means.

A line's ``line`` is its number, ``"next"`` (a new line after the person's
last one: a treatment reported later) or ``"latest"`` (their last line: a
treatment reported as finished, or a return after it). A numbered or next line
is written when its ``regimen`` answer is a listed option: the label becomes
the line's source value (with the ``names`` answer, when given, after it), and
its composition stays unknown; a latest line needs no regimen and is written
only when the person has a line. Line dates are written as their first day,
with each date's precision in the line's detail (TherapyLineDetail), beside
what its ``details`` rules give (the same rules as ``record``, naming detail
fields: still receiving, clinical trial, maintenance, expected end). The
detail also names the responses that wrote the line, so applying a response
again updates its own lines and never adds one.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

from django.conf import settings
from django.db import transaction

logger = logging.getLogger(__name__)

MAPPING_VERSION = 1
QUESTIONNAIRE = 'questionnaire'
_RECORD_SOURCES = ('answer', 'value', 'submitted_on')
_LINE_DATES = ('start_date', 'end_date', 'progression_date')
_TARGETS = ('next', 'latest')


class MappingError(ValueError):
    """A mapping file that cannot be used: refused at load, never half-applied."""


# --- loading ----------------------------------------------------------------


def mapping_dirs() -> tuple[str, ...]:
    return tuple(getattr(settings, 'PROLOG_RECORD_MAPPING_DIRS', ()) or ())


@lru_cache(maxsize=4)
def _load(dirs: tuple[str, ...]) -> dict[tuple[str, str], dict[str, Any]]:
    """Every mapping in ``dirs``, by (survey slug, version)."""
    found: dict[tuple[str, str], dict[str, Any]] = {}
    for directory in dirs:
        for path in sorted(Path(directory).glob('*.json')):
            try:
                mapping = json.loads(path.read_text())
            except (OSError, ValueError) as exc:
                raise MappingError(f'{path}: not a readable JSON mapping ({exc})') from exc
            check_mapping(mapping, source=str(path))
            for version in mapping['versions']:
                key = (mapping['survey'], version)
                if key in found:
                    raise MappingError(f'{path}: {key[0]}@{key[1]} is mapped twice')
                found[key] = mapping
    return found


def mappings() -> dict[tuple[str, str], dict[str, Any]]:
    return _load(mapping_dirs())


def mapping_for(slug: str, version: str) -> dict[str, Any] | None:
    return mappings().get((slug, version))


def check_mapping(mapping: Any, *, source: str = 'mapping') -> None:
    """Refuse a mapping whose shape is wrong or that names no real record field."""
    from omop_core.models import PatientRecord

    def fail(message: str):
        raise MappingError(f'{source}: {message}')

    if not isinstance(mapping, dict) or mapping.get('mapping_version') != MAPPING_VERSION:
        fail(f'mapping_version must be {MAPPING_VERSION}')
    if not isinstance(mapping.get('survey'), str) or not mapping['survey']:
        fail('survey must be a survey slug')
    versions = mapping.get('versions')
    if not isinstance(versions, list) or not versions or not all(isinstance(v, str) for v in versions):
        fail('versions must list the survey versions the mapping was reviewed against')
    fields = {f.name for f in PatientRecord._meta.get_fields() if getattr(f, 'concrete', False)}
    for i, rule in enumerate(mapping.get('record', [])):
        where = f'record[{i}]'
        if not isinstance(rule, dict) or rule.get('field') not in fields:
            fail(f'{where}: field must be a PatientRecord field')
        if sum(1 for k in _RECORD_SOURCES if k in rule) != 1:
            fail(f'{where}: give exactly one of answer, value, submitted_on')
        if rule.get('precision_field') is not None and rule['precision_field'] not in fields:
            fail(f'{where}: precision_field must be a PatientRecord field')
        _check_when(rule, where, fail)
    numbers = set()
    detail_fields = _detail_fields()
    for i, line in enumerate(mapping.get('therapy_lines', [])):
        where = f'therapy_lines[{i}]'
        target = line.get('line') if isinstance(line, dict) else None
        if not (target in _TARGETS or (isinstance(target, int) and not isinstance(target, bool) and target >= 1)):
            fail(f'{where}: line must be a line number, "next" or "latest"')
        if target in numbers and target != 'latest':
            fail(f'{where}: line {target} is mapped twice')
        numbers.add(target)
        regimen = line.get('regimen')
        if regimen is None and target != 'latest':
            fail(f'{where}: a line that may be new needs a regimen')
        if regimen is not None and (not isinstance(regimen, dict) or not isinstance(regimen.get('labels'), dict)
                                    or 'answer' not in regimen):
            fail(f'{where}: regimen needs an answer and labels')
        if regimen and (too_long := [k for k, label in regimen['labels'].items()
                                     if len(str(label)) > _source_value_length()]):
            fail(f'{where}: labels longer than a line source value ({_source_value_length()}): {too_long}')
        for j, rule in enumerate(line.get('details', [])):
            if not isinstance(rule, dict) or rule.get('field') not in detail_fields:
                fail(f'{where}.details[{j}]: field must be a line detail ({", ".join(sorted(detail_fields))})')
            if sum(1 for k in _RECORD_SOURCES if k in rule) != 1:
                fail(f'{where}.details[{j}]: give exactly one of answer, value, submitted_on')
            if rule.get('precision_field') is not None and rule['precision_field'] not in detail_fields:
                fail(f'{where}.details[{j}]: precision_field must be a line detail')
            _check_when(rule, f'{where}.details[{j}]', fail)
        outcome = line.get('outcome')
        if outcome is not None and (not isinstance(outcome, dict) or 'answer' not in outcome
                                    or not isinstance(outcome.get('values'), dict)):
            fail(f'{where}: outcome needs an answer and values')
        _check_when(line, where, fail)


def _check_when(rule: dict, where: str, fail) -> None:
    when = rule.get('when', [])
    if not isinstance(when, list) or not all(isinstance(c, dict) for c in when):
        fail(f'{where}: when must be a list of PROlog conditions')


def check_against_definition(mapping: dict[str, Any], definition: dict[str, Any]) -> list[str]:
    """Problems with a mapping against the survey version it maps: unknown
    questions, and options it lists that the question does not offer."""
    from prolog_surveys.engine.visibility import condition_questions, question_by_key

    questions = question_by_key(definition)
    problems: list[str] = []

    def question(key: str | None, where: str):
        if key not in questions:
            problems.append(f'{where}: no question {key!r}')
            return None
        return questions[key]

    def options(key: str, listed, where: str):
        q = question(key, where)
        if q is None:
            return
        offered = {o['key'] for o in q.get('options', [])}
        problems.extend(f'{where}: {key!r} has no option {o!r}' for o in listed if o not in offered)

    def conditions(rule: dict, where: str):
        for cond in rule.get('when', []):
            for key in condition_questions(cond):
                question(key, f'{where}.when')

    def value_rule(rule: dict, where: str):
        if 'answer' in rule:
            if 'values' in rule:
                options(rule['answer'], rule['values'], where)
            else:
                question(rule['answer'], where)
        conditions(rule, where)

    for i, rule in enumerate(mapping.get('record', [])):
        value_rule(rule, f'record[{i}]')
    for i, line in enumerate(mapping.get('therapy_lines', [])):
        where = f'therapy_lines[{i}]'
        if line.get('regimen'):
            options(line['regimen']['answer'], line['regimen']['labels'], f'{where}.regimen')
        for j, rule in enumerate(line.get('details', [])):
            value_rule(rule, f'{where}.details[{j}]')
        if line.get('outcome'):
            options(line['outcome']['answer'], line['outcome']['values'], f'{where}.outcome')
        for key in ('names', *_LINE_DATES):
            if line.get(key):
                question(line[key], f'{where}.{key}')
        conditions(line, where)
    return problems


def _detail_fields() -> set[str]:
    """The line details a mapping may write: TherapyLineDetail's answer columns."""
    from omop_oncology.models import TherapyLineDetail

    own = {'id', 'episode', 'source_references', 'updated_at'}
    return {f.name for f in TherapyLineDetail._meta.get_fields() if getattr(f, 'concrete', False)} - own


def _source_value_length() -> int:
    from omop_oncology.models import Episode

    return Episode._meta.get_field('episode_source_value').max_length


# --- what a response says -----------------------------------------------------


def answer_date(value: dict | None) -> tuple[dt.date, str] | None:
    """A date answer (YYYY-MM-DD, YYYY-MM or YYYY) as its first day and its precision."""
    raw = (value or {}).get('date')
    if not isinstance(raw, str):
        return None
    parts = raw.split('-')
    precision = {1: 'year', 2: 'month', 3: 'day'}.get(len(parts))
    try:
        day = dt.date(int(parts[0]), int(parts[1]) if len(parts) > 1 else 1, int(parts[2]) if len(parts) > 2 else 1)
    except (ValueError, IndexError):
        return None
    return day, precision


def _holds(rule: dict, answers: dict[str, dict]) -> bool:
    from prolog_surveys.engine.visibility import conditions_hold

    return conditions_hold(rule.get('when', []), answers)


def _option(answers: dict[str, dict], key: str) -> str | None:
    option = (answers.get(key) or {}).get('option')
    return option if isinstance(option, str) else None


def record_patch(mapping: dict[str, Any], answers: dict[str, dict], submitted_on: dt.date) -> dict[str, Any]:
    """The PatientRecord fields a response gives, as a PATCH body."""
    return _values(mapping.get('record', []), answers, submitted_on)


def _values(rules: list[dict], answers: dict[str, dict], submitted_on: dt.date) -> dict[str, Any]:
    """The field values a list of value rules gives (record fields, or a line's details)."""
    patch: dict[str, Any] = {}
    for rule in rules:
        if not _holds(rule, answers):
            continue
        field_name = rule['field']
        if 'value' in rule:
            patch[field_name] = rule['value']
        elif rule.get('submitted_on'):
            patch[field_name] = submitted_on.isoformat()
        else:
            given = answers.get(rule['answer']) or {}
            if 'values' in rule:
                option = _option(answers, rule['answer'])
                if option in rule['values']:
                    patch[field_name] = rule['values'][option]
            elif (dated := answer_date(given)) is not None:
                day, precision = dated
                patch[field_name] = day.isoformat()
                if rule.get('precision_field'):
                    patch[rule['precision_field']] = precision
            elif 'number' in given:
                patch[field_name] = given['number']
            elif 'text' in given:
                patch[field_name] = given['text']
    return patch


def therapy_lines(mapping: dict[str, Any], answers: dict[str, dict],
                  submitted_on: dt.date | None = None) -> list[dict[str, Any]]:
    """Each line a response gives: its ``target`` (a number, "next" or "latest"),
    the ``author_therapy_line`` keyword arguments, and its ``detail`` fields
    (each date's precision among them)."""
    submitted_on = submitted_on or dt.date.today()
    lines = []
    for index, line in enumerate(mapping.get('therapy_lines', [])):
        if not _holds(line, answers):
            continue
        kwargs: dict[str, Any] = {}
        regimen = line.get('regimen')
        if regimen:
            label = regimen['labels'].get(_option(answers, regimen['answer']))
            if label:
                names = (answers.get(line.get('names') or '') or {}).get('text')
                named = f'{label} ({names})' if names else label
                # The names only when they fit beside the label; they stay in the answers either way.
                kwargs['source_value'] = named if len(named) <= _source_value_length() else label
        if line['line'] != 'latest' and 'source_value' not in kwargs:
            continue  # a line that may be new is written only with its treatment
        detail = _values(line.get('details', []), answers, submitted_on)
        for key in _LINE_DATES:
            if line.get(key) and (dated := answer_date(answers.get(line[key]))) is not None:
                kwargs[key], detail[f'{key}_precision'] = dated
        outcome = line.get('outcome')
        if outcome and (value := outcome['values'].get(_option(answers, outcome['answer']))):
            kwargs['outcome'] = value
        if not kwargs and not detail:
            continue
        if line.get('disease'):
            kwargs['disease'] = line['disease']
        lines.append({'index': index, 'target': line['line'], 'kwargs': kwargs, 'detail': detail})
    return lines


def _existing_lines(person, disease: str | None) -> dict[int, Any]:
    """The person's asserted lines for a cancer (the primary one without ``disease``), by number."""
    from omop_core.services.disease_episodes import primary_lines, regimen_episodes
    from omop_core.services.episode_service import _line_parent

    parent, primary = _line_parent(person, disease, None)
    episodes = primary_lines(person) if primary else regimen_episodes(person).filter(
        episode_parent_id=parent.episode_id)
    episodes = episodes.exclude(assertion__state='retracted').select_related('detail')
    return {e.episode_number: e for e in episodes.order_by('episode_number', 'episode_id')}


def _line_numbers(person, writes: list[dict[str, Any]], response_id: str) -> list[dict[str, Any]]:
    """The writes with their line numbers; a "latest" with no line yet is dropped.

    A number is itself. A line this response already wrote (its ``reference``
    is kept on the line's detail) is that line again, so applying a response
    twice never adds a line. Otherwise "next" follows the person's last line
    for that cancer, and "latest" is their last line other than the one this
    response's "next" wrote.
    """
    known: dict[str | None, dict[int, Any]] = {}
    resolved = []
    for write in writes:
        target = write['target']
        write['reference'] = f'{response_id}:{f"line-{target}" if isinstance(target, int) else target}'
        if isinstance(target, int):
            resolved.append({**write, 'line_number': target})
            continue
        disease = write['kwargs'].get('disease')
        if disease not in known:
            known[disease] = _existing_lines(person, disease)
        lines = known[disease]

        def written_by(reference: str):
            return next((n for n, e in lines.items()
                         if reference in (getattr(getattr(e, 'detail', None), 'source_references', None) or [])), None)

        number = written_by(write['reference'])
        if number is None and target == 'next':
            number = max(lines, default=0) + 1
        elif number is None:  # latest
            own_next = written_by(f'{response_id}:next')
            number = max((n for n in lines if n != own_next), default=None)
        if number is not None:
            resolved.append({**write, 'line_number': number})
    return resolved


# --- applying -----------------------------------------------------------------


@dataclass
class Applied:
    response_id: str
    fields: list[str] = field(default_factory=list)
    lines: list[int] = field(default_factory=list)
    retracted: list[int] = field(default_factory=list)


def apply_response(response) -> Applied | None:
    """Write what a submitted response gives into its participant's record, as
    the participant. None when its survey version has no mapping, when it was
    answered through an invitation link, or when nobody holds the participant's
    account (no patient to write as)."""
    from omop_core.api.serializers import outcome_aliases
    from omop_core.models import PatientRecord, Person
    from omop_core.services.disease_episodes import primary_disease_slug
    from omop_core.services.episode_service import author_therapy_line
    from omop_core.services.omop_projection import patient_self_report_type
    from omop_core.services.patient_record_service import refresh_patient_record
    from omop_core.services.therapy_errors import TherapyLineValidationError
    from omop_core.signals import suppress_patient_record_refresh
    from patient_portal.api.views import apply_patient_record_patch
    from patient_portal.models import PatientUser

    version = response.survey_version
    mapping = mapping_for(version.survey.slug, version.version)
    if mapping is None or not response.submitted_at:
        return None
    if response.administration_id is not None:
        # An invitation link is its own credential: whoever holds it answers as
        # the person it names, so its answers are not that person's own edit.
        logger.info('survey_record_mapping: response %s came through an invitation link; not written', response.pk)
        return None
    person = Person.objects.filter(person_id=response.participant_id).first()
    account = PatientUser.objects.filter(person=person).select_related('identity').first() if person else None
    if account is None:
        logger.info('survey_record_mapping: response %s has no patient account to write as', response.pk)
        return None
    from patient_portal.consent import record_permitted
    if not record_permitted(person):
        logger.info('survey_record_mapping: response %s: the record is off (permission withdrawn); not written',
                    response.pk)
        return None

    answers = {a.question_key: a.value for a in response.answers.all()}
    submitted_on = response.submitted_at.date()
    patch = record_patch(mapping, answers, submitted_on)
    lines = therapy_lines(mapping, answers, submitted_on)
    applied = Applied(str(response.pk), sorted(patch))
    with transaction.atomic():
        record, _ = PatientRecord.objects.get_or_create(person=person)
        if patch:
            apply_patient_record_patch(person, record, patch, actor=account.identity)
        if lines:
            record.refresh_from_db()
            # The outcome a mapping names (a code, value or label) as the record's
            # disease catalogue stores it, as the therapy-lines API accepts it; one
            # the catalogue lacks is left out rather than written.
            aliases = outcome_aliases(record.disease)
            if not primary_disease_slug(person):
                # A record that names no cancer yet: a line filed under none is the
                # primary cancer's, whichever the record names later.
                for line in lines:
                    line['kwargs'].pop('disease', None)
            with suppress_patient_record_refresh():
                for write in _line_numbers(person, lines, str(response.pk)):
                    kwargs = dict(write['kwargs'])
                    if 'outcome' in kwargs:
                        outcome = aliases.get(kwargs.pop('outcome'))
                        if outcome is not None:
                            kwargs['outcome'] = outcome
                    if not _writable_line(person, {'line_number': write['line_number'], 'disease': kwargs.get('disease')}):
                        logger.warning('survey_record_mapping: response %s line %s is not the patient\'s; left as it is',
                                       response.pk, write['line_number'])
                        continue
                    try:
                        with transaction.atomic():
                            result = author_therapy_line(
                                person, line_number=write['line_number'], asserted_by=QUESTIONNAIRE,
                                type_concept_id=patient_self_report_type(), **kwargs,
                            )
                            if result.episode is not None:
                                _write_detail(result.episode, write['detail'], write['reference'])
                    except TherapyLineValidationError as exc:
                        # A line the writer refuses (no start date, a future one, an
                        # end before the start, a cancer the record does not have) is
                        # left out; the rest of the response stands.
                        logger.warning('survey_record_mapping: response %s line %s refused: %s',
                                       response.pk, write['line_number'], exc)
                        continue
                    if result.episode is not None:
                        applied.lines.append(write['line_number'])
            refresh_patient_record(person)
        if getattr(response, 'revises_id', None):
            # An update (PROlog's revisable surveys) started from the record as it
            # stood (survey_record_revision): a numbered treatment it no longer
            # gives is one the patient took out, so their own line is retracted.
            applied.retracted = _retract_removed_lines(person, mapping, lines, response)
            if applied.retracted:
                refresh_patient_record(person)
    logger.info(
        'survey_record_mapping: response %s wrote fields %s and lines %s; retracted lines %s',
        applied.response_id, applied.fields, applied.lines, applied.retracted,
    )
    return applied


def _retract_removed_lines(person, mapping: dict[str, Any], given: list[dict[str, Any]], response) -> list[int]:
    """Retract the patient's own lines that the mapping numbers (for the cancer
    each rule names) and an update did not give, and that this questionnaire
    wrote (one of the patient's responses to it is among the line's sources).
    A line another questionnaire added (a check-in's next line, perhaps after
    the update started) is not this update's to take out; nor is a line from
    the EHR or a clinician (_writable_line). Retracting keeps the row for audit."""
    from prolog_surveys.models import SurveyResponse

    from omop_core.services.episode_service import retract_therapy_line
    from omop_core.signals import suppress_patient_record_refresh

    from omop_core.services.therapy_errors import TherapyLineValidationError

    # By the rule that gave each line: apply_response may drop a line's cancer
    # (a record naming none yet), so the line's own fields cannot say which rule.
    kept = {line['index'] for line in given}
    removed = [(rule.get('disease'), rule['line']) for index, rule in enumerate(mapping.get('therapy_lines', []))
               if isinstance(rule.get('line'), int) and index not in kept]
    from omop_core.services.disease_episodes import primary_disease_slug

    if not primary_disease_slug(person):
        # As the lines were written: a record naming no cancer yet files them all
        # under the primary cancer, whichever it becomes.
        removed = [(None, number) for _, number in removed]
    existing: dict[str | None, dict[int, Any]] = {}
    mine: set[str] | None = None
    retracted = []
    with suppress_patient_record_refresh():
        for disease, number in removed:
            if disease not in existing:
                try:
                    existing[disease] = _existing_lines(person, disease)
                except TherapyLineValidationError:
                    existing[disease] = {}  # a cancer the record does not have: nothing to take out
            episode = existing[disease].get(number)
            if episode is None or not _writable_line(person, {'line_number': number, 'disease': disease}):
                continue
            if mine is None:
                mine = {str(pk) for pk in SurveyResponse.objects.filter(
                    participant_id=person.person_id, survey_version__survey=response.survey_version.survey,
                ).values_list('pk', flat=True)}
            sources = getattr(getattr(episode, 'detail', None), 'source_references', None) or []
            if not any(reference.split(':', 1)[0] in mine for reference in sources):
                continue
            retract_therapy_line(person, episode)
            retracted.append(number)
    return retracted


def _write_detail(episode, detail: dict[str, Any], reference: str) -> None:
    """The line's detail: what this response says (a field it does not give
    keeps its value), and that this response wrote the line."""
    from omop_oncology.models import TherapyLineDetail

    row, _ = TherapyLineDetail.objects.select_for_update().get_or_create(episode=episode)
    for name, value in detail.items():
        setattr(row, name, value)
    if reference not in row.source_references:
        row.source_references = [*row.source_references, reference]
    row.save()


def _writable_line(person, line: dict[str, Any]) -> bool:
    """Whether a mapping may write this line: a new one, or one the patient
    reported (typed patient self-report, or asserted by a questionnaire). A line
    from the EHR or a clinician is never overwritten by an answer."""
    from omop_core.services.episode_service import _find_line, _line_parent
    from omop_core.services.omop_projection import PATIENT_SELF_REPORT_TYPE
    from omop_core.services.therapy_errors import TherapyLineValidationError

    try:
        parent, primary = _line_parent(person, line.get('disease'), None)
    except TherapyLineValidationError:
        return True  # the writer refuses it, and says why
    existing = _find_line(person, line['line_number'], parent, primary)
    if existing is None:
        return True
    asserted = getattr(getattr(existing, 'assertion', None), 'source', None)
    return existing.episode_type_concept_id == PATIENT_SELF_REPORT_TYPE or asserted == QUESTIONNAIRE


def run_response(response_id) -> None:
    """Apply a response's mapping by id (the task, or inline): a failure is
    logged with the command that retries it, and never raised."""
    from prolog_surveys.models import SurveyResponse

    try:
        response = SurveyResponse.objects.select_related('survey_version__survey').get(pk=response_id)
    except SurveyResponse.DoesNotExist:
        # Erased after it was submitted (the member closed their account or
        # withdrew their record's permission): there is nothing to write.
        logger.info('survey_record_mapping: response %s no longer exists; nothing to write', response_id)
        return
    try:
        apply_response(response)
    except Exception:
        logger.exception(
            'survey_record_mapping: response %s was not written to the record; '
            'retry with `manage.py apply_record_mappings --response %s`', response_id, response_id,
        )


def on_response_saved(sender, instance, created, update_fields=None, **kwargs):
    """post_save of a PROlog response: when a response to a mapped survey
    becomes submitted (created submitted, or saved with its status), apply its
    mapping once the save commits, on a worker when there is one. Any other save
    of a submitted response (an admin edit, a script) applies nothing, so it
    never writes old answers over newer edits; ``apply_record_mappings`` does
    that on purpose."""
    from django.conf import settings as django_settings
    from prolog_surveys.models import ResponseStatus

    if instance.status != ResponseStatus.SUBMITTED:
        return
    if not created and (update_fields is None or 'status' not in update_fields):
        return
    if not mapping_dirs():
        return
    response_id = str(instance.pk)

    def apply():
        if getattr(django_settings, 'CELERY_BROKER_URL', ''):
            from patient_portal.tasks import apply_record_mapping_task

            apply_record_mapping_task.delay(response_id)
        else:
            run_response(response_id)

    transaction.on_commit(apply)


def reset_cache() -> None:
    """Forget loaded mappings (tests, and a deployment that changed its files)."""
    _load.cache_clear()


__all__ = [
    'Applied', 'MappingError', 'answer_date', 'apply_response', 'check_against_definition', 'run_response',
    'check_mapping', 'mapping_for', 'mappings', 'on_response_saved', 'record_patch', 'reset_cache',
    'therapy_lines',
]
