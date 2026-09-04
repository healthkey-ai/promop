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

**It is only a limit if the counter is shared.** DRF counts throttled requests
in `caches['default']`. Set `CACHE_URL` — or run a Redis `CELERY_BROKER_URL`,
which is used as the cache when no `CACHE_URL` is given — so the count is shared
across workers. Without one it falls back to per-process memory: every rate is
multiplied by the worker count, and a restart forgets them all. `manage.py check
--deploy` reports that as `patient_portal.W005`.

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
default, so a PRomop that runs no surveys loads none: the runner's health
endpoint answers 200 `"status": "ok"` with `active_surveys: 0`, which is a
deployment that simply serves no instrument. `degraded` (503) means something
else — pending migrations, or no `default` theme — and is always worth
investigating. Neither is the container's health check.

```sh
PROLOG_DEFINITION_DIRS=/data/surveys   # *.json instruments, loaded as drafts
PROLOG_THEME_DIRS=/data/themes         # one directory per theme, each with theme.json
PROLOG_LEGAL_DIRS=/data/legal          # privacy.md — served at /s/<slug>/privacy
```

Each is read by the app as a **Django setting**, and `ctomop/settings.py` is
what turns the environment variable into one. A new `PROLOG_*` directory
setting in a PROlog release therefore does nothing here until it is added
there: setting only the environment variable looks right and has no effect,
which is how the legal pages were mounted and served for a while without
appearing.

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

The directory is registered with WhiteNoise (`ctomop/whitenoise.py`), so its
assets get the caching and conditional requests PRomop's own build gets, and the
content-hashed files under `assets/` are cached for a year. WhiteNoise reads the
directory at startup, so replacing a build in place needs a restart — which is
what a deploy does anyway. A path that does not exist, or one with no
`index.html`, fails `manage.py check` (`patient_portal.E004`) rather than
silently serving the portal shell at `/s/<slug>`; definitions mounted with no
runner is a warning (`patient_portal.W004`).

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

### What v0.3.0 added

An **administration console**, in this project's own Django admin at
`/admin/prolog_surveys/survey/`. Everything an administrator previously needed a
shell on a production host for:

- **Add survey** verifies a definition — one the deployment mounts
  (`PROLOG_DEFINITION_DIRS`) or an upload — and loads it as a draft. The
  validator's own output is on the page, and nothing is written until Load.
  Loading requires the add permission for a new survey, change for an existing
  one; a staff session alone is not enough.
- **Activate** and **Archive** on the version's row. Loading never activates,
  and a survey with no active version answers respondents "not available".
- **Publish**, which freezes a version's content. Until then it can be re-loaded
  from a corrected file, and the responses against it are test data — a re-load
  that would discard them asks first.

**It carries a migration.** `prolog_surveys.0002_publish_a_version` renames
`published_at` to `activated_at` and gives the old name to the new freeze
column, so it is **one-way**: an earlier PROlog release running against a
database that has it would stamp the freeze column on every activation. Roll
forward, not back.

### What v0.2.0 added

Five capabilities, all off unless configured, so nothing here behaves
differently until a deployment asks for it:

| | |
| --- | --- |
| `options_source_priority` | pins chosen options to the top of a built-in option source (ordering only — everything else stays answerable) |
| `presentation.language_step` | asks which language *before* the intro, so the intro and consent notice are read in the chosen one |
| `export_translations` | writes a definition's two languages side by side for a reviewer, CSV or Markdown |
| `PROLOG_LEGAL_DIRS` | serves a deployment's own privacy notice at `/s/<slug>/privacy`, on this origin and under the survey's theme |
| `PROLOG_MACHINE_LANGUAGES` | offers a machine-translated language deliberately, with the machine origin disclosed to the respondent |

**A definition using the new keys is refused by 0.1.0, not ignored** — a
question's `config` rejects properties it does not know. So the pin moves before
the definition does, never the other way round.

0.2.1 fixes two things in the legal pages: the notice was linked only from
inside a consent block, so an anonymous survey never showed it, and the renderer
could not render a table or a hard-wrapped list item — both ordinary in a legal
document.

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

### The Surveys tab

The portal's Surveys tab now lists what the runner serves and links into it.
`GET /api/v1/prolog-surveys/` returns, for the calling patient, each active
survey inside its effective window with `not_started` / `in_progress` /
`completed` and the `/s/<slug>` to open. The runner is entered by link and has
no list endpoint of its own; it does not need one, because its tables are in
this database and the portal reads them directly rather than calling itself over
HTTP.

Starting a survey is a real navigation out of the portal, so the tab renders
links rather than buttons. A signed-in patient is resolved by
`PROLOG_PARTICIPANT_RESOLVER`, so their response binds to their own `Person`
rather than a minted one.

The old `SurveyForm` renderer is gone — nothing could reach it once the tab
switched, and an unreachable renderer with its own passing test suite is worse
than no renderer.

**Migrate before you switch a deployment.** The tab lists PROlog surveys only,
so any response still living in `PatientSurveyResponse` becomes invisible until
`migrate_surveys_to_prolog --apply` has run.

### Upgrading a deployment that has old survey data

`omop_core.Survey` and `PatientSurveyResponse` are gone from this release; their
tables are not, until you migrate. That gap is the upgrade window, and the order
matters:

```sh
python manage.py migrate prolog_surveys                # 0. create PROlog's tables
python manage.py migrate_surveys_to_prolog             # 1. see what would move
python manage.py migrate_surveys_to_prolog --apply     # 2. move it
#    ... check the result in the portal ...
python manage.py migrate_surveys_to_prolog --apply --purge-source  # 3. let go of the original
python manage.py migrate                               # 4. drop the tables
```

Steps 2 and 3 are separate on purpose: the original is still there while you
check the conversion. Converting twice is safe — a response already in PROlog is
not duplicated — and `--purge-source` deletes only the rows *this command*
converted, matched on the version's recorded source rather than on the slug, so
a partial conversion leaves the rest alone and an instrument that happens to
share a slug is not mistaken for a counterpart. It deletes, so it needs
`--apply` as well; a template nobody answered is never dropped.

Step 3 is not optional. Conversion copies rather than moves, so the guard in
step 4 still counts the originals until they are purged.

Step 0 is not redundant either, on a deployment coming from a release that never
had `prolog_surveys`: the converter writes through PROlog's models, and step 2
dies with `relation "prolog_surveys_survey" does not exist` until they have
tables. Step 1 is pure and works either way.

**Migration `0201` refuses to run while anything is unconverted.** `start.sh`
migrates on every deploy, so a deployment that skipped the steps above fails
loudly instead of dropping patient-entered data.

### What the removal changed beyond the models

**The API paths survive.** `/surveys/` and `/survey-responses/` still answer on
both the `/api/` and `/api/v1/` prefixes, now backed by PROlog, so a reader of
the v1 contract keeps working. Two differences:

- **They are read-only.** A PROlog instrument is a validated, versioned
  definition loaded with `load_definition`, not a row created over HTTP; and
  answering goes through the runner at `/api/v1/prolog/run/…`, which owns
  visibility, validation and cascade invalidation. A second write path here
  would be a second engine, which is what replacing the old feature was for.
- **The shapes are close, not identical.** `/surveys/` returns a version, with
  `name` and `slug` both the survey's slug and the whole `definition` alongside;
  there is no `pages`. `/survey-responses/` keeps `person`, `status`,
  `started_at`, `completed_at` and a `values` map keyed by question — derived
  from the answer rows rather than stored as a blob.

`/api/v1/prolog-surveys/` remains the tab's own endpoint: it answers "what
should *I* do next", which is a different question from "what exists".

**Org export carries `survey_responses` again**, sourced from PROlog. Each entry
names its instrument by `survey_slug` and `survey_version`, because a response
belongs to an immutable version: an import attaches to the same one, or **skips
and says which instrument it would have needed**, rather than hanging answers off
a different set of questions. Re-running an import does not duplicate a
response.

An organisation purge deletes the PROlog rows for those people.
- Re-verify the **PH.2.1 / PH.3.1.1** conformance criteria against the new model
  before the claim is restated; `docs/phrs-fm-conformance-claim.md` §7 records
  that they are in transition.
