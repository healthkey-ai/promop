# PROlog survey runner in PRomop

PROlog is a survey runner: a declarative JSON instrument, an engine that decides
what a respondent sees next, and a themed front end. It is developed in the
public [healthkey-ai/prolog](https://github.com/healthkey-ai/prolog) repository
and carries no customer content.

**PRomop is its system of record.** PROlog has no database of its own: it is
installed here as the Django app `prolog_surveys`, its tables are created in this
database by this project's migrations, and every response is bound to an
`omop_core.Person`. That architecture is stated in PROlog's
[requirements](https://github.com/healthkey-ai/prolog/blob/dev/docs/requirements.md)
(revision 2026-08-31) and the steps to reach it in its
[migration plan](https://github.com/healthkey-ai/prolog/blob/dev/docs/promop-migration-plan.md).

## What this project provides

| | |
| --- | --- |
| `requirements.txt` | `prolog` pinned by release tag from the public repository. A bump is deliberate — see *Upgrading* below. |
| `INSTALLED_APPS` | `prolog_surveys` |
| `ctomop/urls.py` | `api/v1/prolog/` → the runner's own tree (`health/`, `run/…`) |
| `patient_portal.services.prolog_participant_id` | `PROLOG_PARTICIPANT_RESOLVER` — the signed-in patient's `person_id`, or `None` |
| `patient_portal.services.create_unidentified_person` | `PROLOG_PARTICIPANT_FACTORY` — mints the person for a respondent who is not signed in |
| `REST_FRAMEWORK.DEFAULT_THROTTLE_RATES` | the `run.*` scopes, per hashed client key |
| `RUNNER_DIST` | the built runner front end, when `PROLOG_RUNNER_DIST` names one |

## Every response has a person

PROlog binds a response to a participant at the moment it is created, and the
foreign key is not nullable in normal operation (its DEP-2/RUN-2). There are two
ways that person is found:

**Signed in.** `prolog_participant_id` returns the caller's own `person_id`,
using `patient_person_for` — so it is the same "is this a patient, and which
record is theirs" test the rest of the portal uses. It returns `None` for staff,
providers, service tokens and anonymous callers; a provider trying a survey
therefore never has it recorded against a patient they can see.

**Not signed in.** `create_unidentified_person` mints a `Person` who is not
anyone yet. It is the counterpart to `resolve_or_create_person`, which cannot
serve this case: that function provisions a person *for an `Identity`*, and every
path through it creates a `PatientUser`. A survey respondent has no identity.

The minted person is deliberately three things:

- **Without demographics.** No year of birth, no gender, race or ethnicity
  source values, no name, email or phone. `resolve_or_create_person` writes
  `year_of_birth=1900` and `"unknown"` source values because it is provisioning a
  patient; this row is not a patient, and a placeholder birth year is an
  identifying attribute that is also false.
- **With a `PatientRecord`.** Issue #883 is this same primitive built without
  one: a `Person` created through `find_or_create` has no record, so
  `/api/v1/patient-records/<id>/refresh/` answers 404 and the patient is
  underivable with no API call that can fix it — 39 patients ended that way in
  the 2026-08-31 migration.
- **Not derived.** There is nothing clinical to derive for a person who has just
  been minted, and refresh is expensive.

### Counting

A minted person is a `Person` row like any other, so **any query that counts
patients will count survey respondents too unless it excludes them**. The survey
app records which ones it minted, in a table it owns:

```sql
select * from person p
where not exists (
  select 1 from prolog_surveys_mintedparticipant m
  where m.participant_id = p.person_id and m.identified_at is null
);
```

A row there with `identified_at` unset is a respondent nobody has claimed.
`identified_at` is stamped if that same person later gains an account, after
which they are an ordinary patient and the marker is only history.

The rate at which they can be created is the `run.create` throttle:
30/hour per hashed client key by default. That limit is now also the rate at
which an anonymous caller can add rows to `person`, and should be read that way
when it is set.

## Answers are not clinical data

PROlog never writes OMOP clinical tables (its DEP-7). Answers live in the survey
tables and nothing maps them into `Observation`, `Measurement` or a
`survey_conduct`. That mapping is deferred deliberately: there is no
`survey_conduct` table here, no FHIR `Questionnaire`/`QuestionnaireResponse`, and
the survey-to-OMOP work is issue #903. One existing constraint applies when it is
built: patient-originated rows are typed 32865 "Patient self-report", never 32883
"Survey" — that mislabelling caused #441.

## Serving surveys

No survey is offered until a deployment mounts one. Both directories are empty by
default, so a PRomop that runs no surveys loads none and the runner's health
endpoint reports `degraded` with `active_surveys: 0` — which is correct rather
than broken, and is not the container's health check.

```sh
PROLOG_DEFINITION_DIRS=/data/surveys   # *.json instruments, loaded as drafts
PROLOG_THEME_DIRS=/data/themes         # one directory per theme, each with theme.json
```

Definitions load as drafts and activation is a separate, deliberate step
(`manage.py load_definition <file> --activate`), so a new instrument is never
served by the act of deploying it.

### The runner's front end

`PROLOG_RUNNER_DIST` points at a built runner; unset, only the API is mounted and
no page is served. The runner is a second SPA in a project that already has one,
so it is built with its assets under their own prefix — PRomop's build owns
`/assets/` — and its routes are matched before the catch-all that returns
PRomop's shell:

```sh
# in a prolog checkout
VITE_API_BASE=/api/v1/prolog/run npx vite build --base=/prolog-static/
# then point PROLOG_RUNNER_DIST at frontend/dist
```

A survey is then answered at `/s/<slug>`.

## Upgrading

`requirements.txt` pins PROlog by **release tag**, so an upgrade is a deliberate
step. The app contributes migrations to this database and `start.sh` migrates on
every deploy, so tracking a branch would let a schema change arrive by rebuild.

```sh
# the tags available
git ls-remote --tags https://github.com/healthkey-ai/prolog
# then edit requirements.txt, rebuild, and run:
python manage.py migrate prolog_surveys
python manage.py test patient_portal.tests.PrologSurveyParticipantTest \
                      patient_portal.tests.PrologSurveyInstallationTest
```

The app accepts a range of Django and DRF versions rather than pinning against
this project's, so a PRomop dependency bump does not need a matching PROlog
release. The reverse is not true: PROlog's floor is Django 5.2.6 / DRF 3.15.2 and
Python 3.12, and its ceiling excludes Django 6.

## What is not done yet

- **The participant foreign key is still nullable.** The runner binds every
  response it creates, but the column allows null until a deployment cannot be
  configured without a factory. Rows created before this landed have no person.
- **No promotion path**: when a respondent gives an email, nothing yet attaches
  an `Identity` and `PatientUser` to the person the response is bound to. The
  collision case — the address already belongs to a *different* person — is open
  decision 7 in PROlog's requirements, and the answer there is that merging two
  patient records is a clinical-safety operation, not a survey side effect.
- **PRomop's own survey feature is being retired**, not kept (decision 9). The
  conversion below exists; what has not happened yet is pointing the patient
  portal at the runner and dropping the old models, API and components. Until
  that lands, two survey data models are live in this database.

## Migrating off the old survey feature

`omop_core.Survey` / `PatientSurveyResponse` are replaced by PROlog (decision 9
in PROlog's requirements). `migrate_surveys_to_prolog` translates them:

```sh
python manage.py migrate_surveys_to_prolog            # report only
python manage.py migrate_surveys_to_prolog --apply
python manage.py migrate_surveys_to_prolog --survey symptom-check --apply
```

It is a translation, not a copy, and it says what it could not carry rather than
guessing:

| | |
| --- | --- |
| `rating` | `scale`, 1..`maxRating` |
| `select` | `single`, with its options |
| `textarea` | `text`, multiline |
| `text` and anything unrecognised | `text` — which is what the old renderer showed |
| a value outside a scale, or not one of a select's options | **reported and skipped** |
| a `values` key that is not in the template | **reported and skipped** |
| `percent_complete`, `values_dates`, `consent_signature` | no equivalent; not carried |

Definitions land as **drafts**: review each, then activate deliberately with
`load_definition … --activate`. Responses keep their `Person`, so nothing is
re-parented.

### The one thing a migration cannot fix

A PROlog version is immutable; a `Survey` template was not. A response was
answered against whatever `pages` said at the time, and **nothing recorded what
that was**. Every migrated response is therefore attached to the template as it
stands now. Where a template was edited after answers were given, that is a real
reinterpretation of somebody's data — the failure the immutable-version design
exists to prevent, applied retroactively. Check whether any template was edited
after its first response before trusting migrated answers.

### Still to do

- Point the patient portal's Surveys tab at the runner instead of `SurveyForm`.
- Drop the old models, viewsets, serializers and components once nothing reads
  them — a separate, destructive change.
- Re-verify the **PH.2.1 / PH.3.1.1** conformance criteria against the new model
  before the claim is restated; `docs/phrs-fm-conformance-claim.md` §7 records
  that they are in transition.
