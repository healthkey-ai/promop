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
  progress from the definition), `values_dates`, `consent_signature` and
  `consent_date` — PROlog records consent against a response in its own
  `SurveyConsent` table, which this does not attempt to reconstruct, so a
  deployment that needs the attestation kept has to carry it deliberately.
  Answers whose key is not in the template, or whose value does not fit the
  question type, are reported rather than guessed at.

Dry run by default. `--apply` is the only path that writes, and
`--purge-source` needs it too.

    manage.py migrate_surveys_to_prolog                        # report only
    manage.py migrate_surveys_to_prolog --apply                # convert
    manage.py migrate_surveys_to_prolog --survey symptom-check --apply
    manage.py migrate_surveys_to_prolog --apply --purge-source # then let go

It reads the legacy tables with SQL rather than through models, because the
models are gone from this release and the tables are not: that gap is the
upgrade window. Pull this release, convert, check the result, purge, and only
then migrate — which drops the tables, and refuses to while any legacy row is
still there. Converting alone does not clear that guard: conversion copies, and
`--purge-source` is what removes the original.
"""

from __future__ import annotations

import json
import re

from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction

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


#: The retired tables. Written as literals in every query below rather than
#: interpolated: nothing here is dynamic, and a query built by f-string reads
#: like an injection site to anyone auditing it later (bandit B608 agrees).
LEGACY_TEMPLATES = "survey"
LEGACY_RESPONSES = "patient_survey_response"


def _json(value):
    """jsonb comes back decoded through the ORM and as text through a cursor."""
    if isinstance(value, (str, bytes)):
        return json.loads(value)
    return value


def _rows(sql: str, params=()) -> list[dict]:
    with connection.cursor() as cur:
        cur.execute(sql, params)
        columns = [c[0] for c in cur.description]
        return [dict(zip(columns, row)) for row in cur.fetchall()]


def legacy_tables_exist() -> bool:
    return bool(
        _rows(
            "select 1 from information_schema.tables "
            "where table_schema = current_schema() and table_name = %s",
            [LEGACY_RESPONSES],
        )
    )


def slug_for(survey: dict) -> str:
    """The PROlog slug a legacy template converts to.

    One definition, shared by the conversion and the purge — the two of them
    disagreeing about it is how a purge deletes the wrong rows.
    """
    return _key(survey["name"], "survey").replace("_", "-")


def source_for(survey: dict) -> str:
    """What `load_definition` records as the origin of a converted version.

    It is what tells a version this command created apart from one an operator
    loaded, which the purge depends on.
    """
    return f"legacy survey:{survey['id']}"


def input_key_map(survey: dict) -> dict:
    """Legacy input name -> the PROlog question key its answers belong under.

    Built by running the same `_question` the definition is built from, rather
    than by recomputing the key: the two derivations drifting apart is how an
    answer ends up looking for a question that does not exist. They diverge for
    any name that slugifies to nothing — a name with no ASCII alphanumerics —
    where the question gets its positional fallback and a recomputed lookup
    does not.
    """
    mapping = {}
    for page in (_json(survey.get("pages")) or []):
        for qi, inp in enumerate(page.get("inputs") or []):
            question, _ = _question(inp, qi)
            if question:
                mapping[inp.get("name")] = question["key"]
    return mapping


def build_definition(survey: dict) -> tuple[dict, list[str]]:
    """A PROlog definition document for one template, plus what it could not carry."""
    notes: list[str] = []
    sections = []
    for si, page in enumerate(_json(survey.get("pages")) or []):
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
        "slug": slug_for(survey),
        "version": "1.0",
        "status": "draft",
        "default_language": "en",
        "languages": ["en"],
        "title": {"en": survey["title"] or survey["name"]},
        "sections": sections,
    }
    if survey.get("description"):
        doc["intro"] = {"en": survey["description"]}
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
        parser.add_argument(
            "--purge-source",
            action="store_true",
            help=(
                "Delete the legacy rows that have already been converted. Separate from --apply "
                "on purpose: convert, look at the result, then let go of the original."
            ),
        )

    def handle(self, *args, **options):
        from prolog_surveys.definitions.loader import (
            DefinitionError,
            has_errors,
            load_definition,
            validate_definition,
        )
        from prolog_surveys.models import SurveyAnswer, SurveyResponse

        apply = options["apply"]
        names = options.get("survey")
        if options["purge_source"] and not apply:
            raise CommandError(
                "--purge-source deletes legacy rows, so it needs --apply too. "
                "Dry-run first without either."
            )
        if not legacy_tables_exist():
            self.stdout.write(
                "the legacy survey tables are already gone; nothing to migrate"
            )
            return
        all_templates = _rows(
            "select id, name, title, description, pages from survey order by name"
        )
        templates = all_templates
        if names:
            found = {t["name"] for t in templates}
            missing = set(names) - found
            if missing:
                raise CommandError(f"no such survey: {', '.join(sorted(missing))}")
            templates = [t for t in templates if t["name"] in names]
        if not templates:
            self.stdout.write("no surveys to migrate")
            return

        # Two template names can slugify alike ("Symptom check" and
        # "symptom-check"). Converting both would load the second definition
        # over the first as a new version of one survey, silently merging two
        # instruments — so refuse before anything is written and let the
        # operator rename one. Checked against every template, not only the
        # selected ones: converting the pair one at a time merges them just the
        # same.
        collisions: dict[str, list[str]] = {}
        for template in all_templates:
            collisions.setdefault(slug_for(template), []).append(template["name"])
        selected = {slug_for(t) for t in templates}
        clashing = {
            slug: names_
            for slug, names_ in collisions.items()
            if len(names_) > 1 and slug in selected
        }
        if clashing:
            raise CommandError(
                "these templates convert to the same slug: "
                + "; ".join(
                    f"{slug} <- {', '.join(sorted(names_))}"
                    for slug, names_ in sorted(clashing.items())
                )
                + ". Rename one of each pair and re-run."
            )

        totals = {"templates": 0, "responses": 0, "answers": 0, "skipped": 0}
        refused: list[str] = []
        for survey in templates:
            doc, notes = build_definition(survey)
            self.stdout.write(f"\n{survey['name']} -> {doc['slug']}")
            if not doc["sections"]:
                self.stdout.write(self.style.WARNING("  no usable pages; nothing to migrate"))
                continue
            for note in notes:
                self.stdout.write(self.style.WARNING(f"  {note}"))

            responses = _rows(
                "select person_id, values, started_at, completed_at, created_at "
                "from patient_survey_response where survey_id = %s",
                [survey["id"]],
            )
            questions = {q["key"]: q for s in doc["sections"] for q in s["questions"]}
            by_input = input_key_map(survey)

            if not apply:
                # The loader is what refuses a definition, so the dry run has
                # to ask it: without this a report promises a migration that
                # --apply then declines outright, which is the one thing a dry
                # run exists to prevent.
                issues = validate_definition(doc)
                for issue in issues:
                    style = self.style.ERROR if issue.level == "error" else self.style.WARNING
                    self.stdout.write(style(f"  {issue}"))
                if has_errors(issues):
                    self.stdout.write(self.style.ERROR(
                        "  this template would be refused; nothing would migrate"
                    ))
                    refused.append(survey["name"])
                    continue
                carried = skipped = 0
                for response in responses:
                    for name, raw in (_json(response["values"]) or {}).items():
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
                    f"  would migrate {len(responses)} response(s), "
                    f"{carried} answer(s); {skipped} answer(s) could not be carried"
                )
                totals["templates"] += 1
                totals["responses"] += len(responses)
                totals["answers"] += carried
                totals["skipped"] += skipped
                continue

            with transaction.atomic():
                try:
                    result = load_definition(doc, source=source_for(survey))
                except DefinitionError as exc:
                    self.stdout.write(self.style.ERROR(f"  refused: {exc}"))
                    continue
                version = result.version
                for response in responses:
                    # Idempotent: the old model allowed one response per person
                    # per survey, so that pair identifies it here too. A second
                    # run must not give anyone a duplicate.
                    if SurveyResponse.objects.filter(
                        survey_version=version, participant_id=response["person_id"]
                    ).exists():
                        continue
                    migrated = SurveyResponse.objects.create(
                        survey_version=version,
                        participant_id=response["person_id"],
                        language="en",
                        status="submitted" if response["completed_at"] else "in_progress",
                        submitted_at=response["completed_at"],
                    )
                    # `started_at` is auto_now_add, so create() ignores the
                    # value and stamps now: the whole cohort would carry the
                    # time the migration ran instead of when it was answered.
                    started_at = response["started_at"] or response["created_at"]
                    if started_at:
                        SurveyResponse.objects.filter(pk=migrated.pk).update(
                            started_at=started_at
                        )
                    for name, raw in (_json(response["values"]) or {}).items():
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
                self.stdout.write(self.style.SUCCESS(f"  migrated {len(responses)} response(s)"))

        if options["purge_source"]:
            self._purge(templates)

        verb = "migrated" if apply else "would migrate"
        self.stdout.write(
            f"\n{verb} {totals['templates']} template(s), {totals['responses']} response(s), "
            f"{totals['answers']} answer(s); {totals['skipped']} answer(s) not carried"
        )
        if not apply:
            self.stdout.write("dry run; nothing written. Re-run with --apply.")
            if refused:
                raise CommandError(
                    "these templates would be refused as they stand: "
                    + ", ".join(sorted(refused))
                    + ". Fix them before --apply; a scripted upgrade should stop here."
                )
        else:
            self.stdout.write(
                "Definitions are drafts: review each, then activate it deliberately with "
                "`manage.py load_definition ... --activate`. percent_complete, values_dates, "
                "consent_signature and consent_date have no PROlog equivalent and were "
                "not carried."
            )

    def _purge(self, templates):
        """Delete legacy rows whose responses are already in PROlog.

        Kept apart from --apply so the original is still there while the
        conversion is checked. A row is only dropped when *this command*
        converted it: the PROlog response has to belong to a version whose
        source names this template. A PROlog response the same person happens
        to have for a same-named instrument loaded some other way is not a
        counterpart, and deleting the legacy row against it would lose the
        answers it stands for.
        """
        from prolog_surveys.models import SurveyResponse

        for survey in templates:
            converted = set(
                SurveyResponse.objects.filter(
                    survey_version__survey__slug=slug_for(survey),
                    survey_version__source=source_for(survey),
                ).values_list("participant_id", flat=True)
            )
            rows = _rows(
                "select person_id from patient_survey_response where survey_id = %s",
                [survey["id"]],
            )
            unconverted = [r["person_id"] for r in rows if r["person_id"] not in converted]
            if unconverted:
                self.stdout.write(
                    self.style.WARNING(
                        f"  {survey['name']}: {len(unconverted)} response(s) have no PROlog "
                        f"counterpart; left in place"
                    )
                )
            with connection.cursor() as cur:
                if converted:
                    cur.execute(
                        "delete from patient_survey_response "
                        "where survey_id = %s and person_id = any(%s)",
                        [survey["id"], list(converted)],
                    )
                    deleted = cur.rowcount
                else:
                    deleted = 0
                # The template goes only when it had responses and every one of
                # them is now in PROlog. A template nobody ever answered was
                # not converted either, and dropping it here would delete an
                # instrument on the strength of it being empty.
                if rows and not unconverted:
                    cur.execute("delete from survey where id = %s", [survey["id"]])
                    self.stdout.write(f"  {survey['name']}: template deleted")
            self.stdout.write(f"  {survey['name']}: purged {deleted} legacy response(s)")
