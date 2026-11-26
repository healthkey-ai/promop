# Survey record mapping

Questionnaire answers written into the patient's own record, as the patient,
when a deployment says which answers mean what. Code:
`patient_portal/survey_record_mapping.py`. Command: `apply_record_mappings`.

## Why a mapping, and why data

PROlog stores what a person answered and writes nothing clinical (its DEP-7;
[prolog-surveys.md](prolog-surveys.md)). Some answers are, for the person
answering, facts about themselves: when they were diagnosed, the treatment they
had, how active they are this week. A patient-facing deployment wants those in
the record the person sees, and in OMOP, without asking twice.

Which answer means which field is the deployment's knowledge, not promop's: the
questions are its wording, versioned in its definitions. So the mapping is data
the deployment supplies and reviews with its definitions, named per exact
survey version, and promop interprets it. promop holds no question keys, no
wording and no deployment's rules.

## What happens on submit

1. PROlog's submit saves the response with `status=submitted`.
2. A `post_save` receiver (`patient_portal.apps`) sees a response *become*
   submitted (created submitted, or saved with its `status`) and, only when
   `PROLOG_RECORD_MAPPING_DIRS` is set, schedules the mapping for after the
   save commits: on a worker (`patient_portal.apply_record_mapping`) when Celery
   has a broker, inline otherwise. Another save of a submitted response (an
   admin edit, a script) applies nothing, so it never writes old answers over
   newer edits. A failure is logged with the command that retries it and never
   undoes the submission.
3. `apply_response` finds the mapping for the response's survey slug and
   version. None means nothing is written.
4. It writes as the participant's account (`PatientUser.identity`). A response
   with no account behind it writes nothing, and nor does one answered through
   an invitation link (`administration`): the link is its own credential, so
   whoever holds it answers as the person it names, and that is not the
   person's own edit.
   - **Record fields** go through `apply_patient_record_patch`, the save path of
     the patient's own `PATCH /api/v1/patient-records/me/`: the serializer's
     validation, `user_edited_fields`, the revision history (changed by the
     patient), projection typed 32865 with PATIENT_SELF provenance, recompute,
     and the provisional mirror.
   - **Lines of therapy** go through `author_therapy_line`, with every row typed
     32865 and the line asserted by `questionnaire`. A line's composition stays
     unknown; the answer's label is its source value. A line that is already on
     the record from the EHR or a clinician (not self-report, not asserted by a
     questionnaire) is never overwritten by an answer.
5. Everything for one response is one transaction, but each line is written in
   its own savepoint: a line the writer refuses (no start date, a future start,
   an end before the start) is logged and left out, and the record fields and
   the other lines stand. A record that names no cancer yet takes its lines
   unfiled, as the primary cancer's.

## The rules

A mapping writes only what an answer gives. A skipped or unanswered question, an
answer given instead of a date ("not sure"), an option the mapping does not
list, or a rule whose `when` does not hold writes nothing and clears nothing,
unless the mapping says the answer clears the field (below). `when` uses
PROlog's own condition engine, so a rule's condition means exactly what the
same condition means in `visible_if`.

A date answer is written as its first day; a record rule may name the field that
keeps its precision (`diagnosis_date_precision`).

**Clearing.** A date rule may list the options of its question that clear the
field: `"clears": ["not_sure"]`. A patient who gave a date before and now says
they are not sure has it removed, with its precision, but only when the value
is still their own: the last change to the field was theirs, to what it holds
now (`RecordRevision`). A value a clinician or an import gave stays. A line may
clear when the disease came back after it, `"clears": {"progression_date":
["not_sure"]}`, which removes only the progression the patient reported (typed
patient self-report); a clinical one stays. Its start and end dates cannot be
cleared: a line needs its start, and a line with no end is one still received.
`clears` is checked to sit on a date question. A date can also be cleared by
another answer: a record or detail rule with `"value": null` and a `when` (the
patient now says it was never transformed), or a line's `clears_when`, PROlog
conditions that clear its progression (`{"progression_date": [{"question":
"q_back", "op": "eq", "value": "no"}]}`). Either way only the patient's own
value is emptied. A clear never empties what was written after the response
was submitted, so applying an older response again (a retry, a backfill)
leaves newer answers alone.

**Adding up.** A numeric field may be the sum of several answers:
`{"field": "relapse_count", "sum": [{"answer": "q_after_first", "values":
{"yes_once": 1, "no": 0}}, …]}`. Each term maps an option to a number and may
have its own `when`; the field is written when at least one term counts. A
questionnaire that asks the same thing after each treatment gives the total
this way, where separate rules would let the last one win. A sum totals what
the response itself answers, so it suits a questionnaire that restates the
whole history each time (an FL profile), not one that asks about part of it.
Each term counts a whole number, or `null` for an answer that leaves the total
unknown ("not sure how many"): then the field is not written.

## Lines

A line rule names its line by number, as `"next"` (a new line after the
person's last: a treatment reported at a later questionnaire) or as `"latest"`
(their last line: a treatment reported as finished, or a return after it). A
numbered or next line needs its treatment (`regimen`); a latest one updates only
what its answers give, and is dropped when the person has no line yet.

A line's Episode keeps whole dates. `TherapyLineDetail`, one per line, keeps
what the Episode cannot: each date's precision (filled from the date answers),
and the line's own answers with no OMOP row yet, written by the line's `details`
rules: still receiving, a clinical trial, maintenance (whether, which medicine,
its dates), the expected end. The PHR therapy view returns them with the line.

The detail also lists the responses that wrote the line. Applying a response
again resolves each of its rules to the line it wrote, so a retry never adds a
line, and its "latest" stays the line that was latest before its own "next".

Applying a response again writes the same values: a retry, or a backfill with
`apply_record_mappings --survey <slug>`, is safe. A later response to the same
survey overwrites what an earlier one wrote, field by field and line by line.

## Checking a mapping

`apply_record_mappings --check` loads every mapping and checks it against the
survey versions it names: every question it reads exists, every option it lists
is offered. Loading refuses a mapping that is not readable JSON, names a field
`PatientRecord` does not have, or maps one survey version twice, and the system
check `patient_portal.E007` runs the load at startup, so a broken file fails
`check` and the deploy rather than every submission.

## Updating answers (revisable questionnaires)

PROlog's revisable questionnaires (RUN-4) let a patient update what they
submitted: a new response, which this mapping applies like any other. Two
things keep an update from undoing what has happened since:

- **It starts from the last answers, with what the record says differently
  now** (`survey_record_revision.revision_answers`, PROlog's
  `PROLOG_REVISION_ANSWERS_PROVIDER`). The last answers are replayed through
  the mapping; where the record now holds something other than they would
  write (a check-in finished a treatment, recorded its response), that answer
  is read back from the record: the option, date (at its stored precision),
  number or text its rule takes, and for a numbered line its regimen and
  names, dates, outcome and details, each from the line itself (its own
  LOT-n outcome and progression, never the record's summary of later lines).
  Everything unchanged stays exactly as the patient answered it. Nothing is
  guessed: a field several questions write (`relapse_count`) is left alone,
  as is a rule whose `when` the last answers do not meet and a field the last
  answers give nothing for (the mapping never clears a value, so the record's
  may be one taken back), unless that question was left unanswered. A last
  answer the record contradicts, with no single answer for what it says now,
  is left out and asked again. An answer from the record that its question's
  conditions would hide opens its way only through questions left unanswered
  and only where one answer does it; an answer the patient gave is never
  changed. PROlog checks every answer as one given now. With the record's
  permission withdrawn, the update starts from the last answers.
- **What an update takes out goes.** Submitting an update (a response with
  `revises`) retracts the patient's own lines that the mapping numbers, that
  this questionnaire wrote (one of the patient's responses to it is among the
  line's sources), and that the update no longer gives: they took the
  treatment out. A line another questionnaire added (a check-in's, perhaps
  after the update started) is not the update's to take out, nor is a line
  from the EHR or a clinician. Retracting keeps the row for audit. A record
  field an update no longer gives is not cleared.

## Not yet

- Fields with no `PatientRecord` column yet; their answers stay in the survey
  tables.
- Maintenance as drug exposures inside the line: it is the line's detail until
  its medicines are mapped to concepts.
