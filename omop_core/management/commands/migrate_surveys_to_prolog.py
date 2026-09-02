"""Convert this project's own surveys into PROlog definitions and responses.

PROlog replaces `omop_core.Survey` / `PatientSurveyResponse` (decision 9 in
PROlog's requirements). The two models do not have the same shape, so this is a
translation and not a copy, and the differences matter:

* **A PROlog version is immutable; a `Survey` template is not.** A response was
  answered against whatever `pages` said at the time, and nothing recorded what
  that was. Every migrated response is therefore attached to the template *as it
  stands now*. Where a template has been edited since, that is a real
  reinterpretation of somebody's answers — which is exactly the failure the
  immutable-version design exists to prevent, and cannot be undone here.
* **Some fields have nowhere to go**: `percent_complete` (PROlog computes
  progress from the definition), `values_dates`, and `consent_signature`.
  Answers whose key is not in the template, or whose value does not fit the
  question type, are reported rather than guessed at.

Dry run by default. `--apply` is the only path that writes.

    manage.py migrate_surveys_to_prolog                    # report only
    manage.py migrate_surveys_to_prolog --apply
    manage.py migrate_surveys_to_prolog --survey symptom-check --apply
"""

from __future__ import annotations

import re

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from omop_core.models import PatientSurveyResponse, Survey

#: PROlog question and section keys: lower-case, digits, underscores.
_KEY_RE = re.compile(r"[^a-z0-9]+")

#: EpicForm input type -> PROlog question type.
_TYPES = {
    "rating": "scale",
    "select": "single",
    "textarea": "text",
    "text": "text",
}


def _key(value: str, fallback: str) -> str:
    key = _KEY_RE.sub("_", (value or "").strip().lower()).strip("_")[:128]
    if not key or not key[0].isalnum():
        key = f"{fallback}_{key}".strip("_")[:128]
    return key or fallback


def _question(inp: dict, index: int) -> tuple[dict, str | None]:
    """One PROlog question, or (None-ish, reason) when the input cannot become one."""
    raw_type = (inp.get("type") or "text").strip()
    if raw_type not in _TYPES:
        # The renderer fell back to a text box for anything it did not know, so
        # that is what the answers actually are.
        kind = "text"
        note = f"input {inp.get('name')!r}: unknown type {raw_type!r} carried over as text"
    else:
        kind = _TYPES[raw_type]
        note = None

    question: dict = {
        "key": _key(inp.get("name", ""), f"q{index}"),
        "type": kind,
        "text": {"en": inp.get("label") or inp.get("name") or f"Question {index + 1}"},
        # Nothing in the old feature was ever required: the Complete button did
        # not check, so calling them required now would refuse existing answers.
        "required": False,
    }
    data = inp.get("data") or {}
    if kind == "scale":
        question["config"] = {"scale": {"min": 1, "max": int(data.get("maxRating") or 5)}}
    elif kind == "single":
        options = [str(o) for o in (data.get("options") or [])]
        if not options:
            return {}, f"input {inp.get('name')!r}: a select with no options cannot be migrated"
        question["options"] = [
            {"key": _key(o, f"o{i}"), "label": {"en": o}} for i, o in enumerate(options)
        ]
    elif raw_type == "textarea":
        question["config"] = {"multiline": True}
    return question, note


def build_definition(survey: Survey) -> tuple[dict, list[str]]:
    """A PROlog definition document for one template, plus what it could not carry."""
    notes: list[str] = []
    sections = []
    for si, page in enumerate(survey.pages or []):
        questions = []
        for qi, inp in enumerate(page.get("inputs") or []):
            question, note = _question(inp, qi)
            if note:
                notes.append(note)
            if question:
                questions.append(question)
        if not questions:
            notes.append(f"page {page.get('name')!r}: no usable inputs, skipped")
            continue
        sections.append(
            {
                "key": _key(page.get("name", ""), f"s{si}"),
                "title": {"en": page.get("title") or page.get("name") or f"Page {si + 1}"},
                "questions": questions,
            }
        )
    doc = {
        "schema_version": 1,
        "slug": _key(survey.name, "survey").replace("_", "-"),
        "version": "1.0",
        "status": "draft",
        "default_language": "en",
        "languages": ["en"],
        "title": {"en": survey.title or survey.name},
        "sections": sections,
    }
    if survey.description:
        doc["intro"] = {"en": survey.description}
    return doc, notes


def _answer_value(question: dict, raw) -> tuple[dict | None, str | None]:
    """The canonical PROlog value for a stored answer, or a reason it cannot be."""
    if raw is None or raw == "":
        return None, None  # unanswered; PROlog simply has no row
    kind = question["type"]
    if kind == "text":
        return {"text": str(raw)}, None
    if kind == "scale":
        try:
            value = int(raw)
        except (TypeError, ValueError):
            return None, f"{question['key']}: {raw!r} is not a scale value"
        scale = question["config"]["scale"]
        if not scale["min"] <= value <= scale["max"]:
            return None, f"{question['key']}: {value} is outside {scale['min']}..{scale['max']}"
        return {"value": value}, None
    if kind == "single":
        wanted = _key(str(raw), "")
        for option in question["options"]:
            if option["key"] == wanted or option["label"]["en"] == raw:
                return {"option": option["key"]}, None
        return None, f"{question['key']}: {raw!r} is not one of its options"
    return None, f"{question['key']}: no rule for type {kind}"


class Command(BaseCommand):
    help = "Convert omop_core surveys and responses into PROlog definitions and responses."

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true", help="Write. Default is dry-run.")
        parser.add_argument("--survey", action="append", help="Template name; repeat to restrict.")

    def handle(self, *args, **options):
        from prolog_surveys.definitions.loader import DefinitionError, load_definition
        from prolog_surveys.models import SurveyAnswer, SurveyResponse

        apply = options["apply"]
        names = options.get("survey")
        templates = Survey.objects.all().order_by("name")
        if names:
            templates = templates.filter(name__in=names)
            missing = set(names) - set(templates.values_list("name", flat=True))
            if missing:
                raise CommandError(f"no such survey: {', '.join(sorted(missing))}")
        if not templates:
            self.stdout.write("no surveys to migrate")
            return

        totals = {"templates": 0, "responses": 0, "answers": 0, "skipped": 0}
        for survey in templates:
            doc, notes = build_definition(survey)
            self.stdout.write(f"\n{survey.name} -> {doc['slug']}")
            if not doc["sections"]:
                self.stdout.write(self.style.WARNING("  no usable pages; nothing to migrate"))
                continue
            for note in notes:
                self.stdout.write(self.style.WARNING(f"  {note}"))

            responses = PatientSurveyResponse.objects.filter(survey=survey).select_related("person")
            questions = {q["key"]: q for s in doc["sections"] for q in s["questions"]}
            by_input = {
                inp.get("name"): _key(inp.get("name", ""), "")
                for page in (survey.pages or [])
                for inp in (page.get("inputs") or [])
            }

            if not apply:
                carried = skipped = 0
                for response in responses:
                    for name, raw in (response.values or {}).items():
                        question = questions.get(by_input.get(name, ""))
                        if question is None:
                            skipped += 1
                            continue
                        value, why = _answer_value(question, raw)
                        if why:
                            skipped += 1
                        elif value is not None:
                            carried += 1
                self.stdout.write(
                    f"  would migrate {responses.count()} response(s), "
                    f"{carried} answer(s); {skipped} answer(s) could not be carried"
                )
                totals["templates"] += 1
                totals["responses"] += responses.count()
                totals["answers"] += carried
                totals["skipped"] += skipped
                continue

            with transaction.atomic():
                try:
                    result = load_definition(doc, source=f"omop_core.Survey:{survey.pk}")
                except DefinitionError as exc:
                    self.stdout.write(self.style.ERROR(f"  refused: {exc}"))
                    continue
                version = result.version
                for response in responses:
                    migrated = SurveyResponse.objects.create(
                        survey_version=version,
                        participant_id=response.person_id,
                        language="en",
                        status="submitted" if response.completed_at else "in_progress",
                        started_at=response.started_at or response.created_at,
                        submitted_at=response.completed_at,
                    )
                    for name, raw in (response.values or {}).items():
                        question = questions.get(by_input.get(name, ""))
                        if question is None:
                            self.stdout.write(
                                self.style.WARNING(f"  {name!r} is not in the template; skipped")
                            )
                            totals["skipped"] += 1
                            continue
                        value, why = _answer_value(question, raw)
                        if why:
                            self.stdout.write(self.style.WARNING(f"  {why}; skipped"))
                            totals["skipped"] += 1
                            continue
                        if value is None:
                            continue
                        SurveyAnswer.objects.create(
                            response=migrated,
                            question_key=question["key"],
                            value=value,
                            option_keys=[value["option"]] if "option" in value else [],
                        )
                        totals["answers"] += 1
                    totals["responses"] += 1
                totals["templates"] += 1
                self.stdout.write(self.style.SUCCESS(f"  migrated {responses.count()} response(s)"))

        verb = "migrated" if apply else "would migrate"
        self.stdout.write(
            f"\n{verb} {totals['templates']} template(s), {totals['responses']} response(s), "
            f"{totals['answers']} answer(s); {totals['skipped']} answer(s) not carried"
        )
        if not apply:
            self.stdout.write("dry run; nothing written. Re-run with --apply.")
        else:
            self.stdout.write(
                "Definitions are drafts: review each, then activate it deliberately with "
                "`manage.py load_definition ... --activate`. percent_complete, values_dates "
                "and consent_signature have no PROlog equivalent and were not carried."
            )
