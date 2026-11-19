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
2. A `post_save` receiver (`patient_portal.apps`) sees a submitted response and,
   only when `PROLOG_RECORD_MAPPING_DIRS` is set, schedules the mapping for
   after the submit's transaction commits. A failure is logged and never undoes
   the submission.
3. `apply_response` finds the mapping for the response's survey slug and
   version. None means nothing is written.
4. It writes as the participant's account (`PatientUser.identity`). A response
   with no account behind it writes nothing.
   - **Record fields** go through `apply_patient_record_patch`, the save path of
     the patient's own `PATCH /api/v1/patient-records/me/`: the serializer's
     validation, `user_edited_fields`, the revision history (changed by the
     patient), projection typed 32865 with PATIENT_SELF provenance, recompute,
     and the provisional mirror.
   - **Lines of therapy** go through `author_therapy_line`, with every row typed
     32865 and the line asserted by `questionnaire`. A line's composition stays
     unknown; the answer's label is its source value.
5. Everything for one response is one transaction.

## The rules

A mapping writes only what an answer gives. A skipped or unanswered question, an
answer given instead of a date ("not sure"), an option the mapping does not
list, or a rule whose `when` does not hold writes nothing and clears nothing.
`when` uses PROlog's own condition engine, so a rule's condition means exactly
what the same condition means in `visible_if`.

A date answer is written as its first day; a record rule may name the field that
keeps its precision (`diagnosis_date_precision`). Therapy lines have no date
precision yet, so a month-and-year start is stored as the first of the month.

Applying a response again writes the same values: a retry, or a backfill with
`apply_record_mappings --survey <slug>`, is safe. A later response to the same
survey overwrites what an earlier one wrote, field by field and line by line.

## Checking a mapping

`apply_record_mappings --check` loads every mapping and checks it against the
survey versions it names: every question it reads exists, every option it lists
is offered. Loading refuses a mapping that names a field `PatientRecord` does
not have, or that maps one survey version twice.

## Not yet

- Lines numbered by what is already on the record (a later "I started a new
  treatment" as the next line), and updating the latest line from a later
  answer ("I finished a treatment").
- Fields with no `PatientRecord` column yet; their answers stay in the survey
  tables.
- Date precision on therapy lines.
- Running the mapping on a worker rather than after the submit's commit.
