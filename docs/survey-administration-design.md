# Managing surveys in PRomop — a design

> **Status:** proposed, 2026-09-03. For review before any of it is built.
>
> **Artifacts this describes:** [`prolog-surveys.md`](prolog-surveys.md) — how the runner is installed here · [PROlog's administration manual](https://github.com/healthkey-ai/prolog/blob/dev/docs/administration.md) — what an administrator does today, from a terminal · [`../patient_portal/api/views.py`](../patient_portal/api/views.py) — the read-only endpoints this would extend

Today a survey is administered from a shell: `validate_definition`, `load_definition`, `--activate`, `export_responses`. That is fine for one instrument run by the people who deployed it, and it is the wrong shape for a platform running several, for different audiences, administered by people who do not have a shell.

This describes the console that replaces it. It is deliberately **not** a survey builder.

---

## 1. The thing being managed is not a row

The instinct is CRUD: create a survey, edit it, delete it. That fights the engine hard enough to be worth stating plainly.

A PROlog instrument is a **validated, immutable, versioned definition**. Questions, branching rules and option keys are data, checked against a schema and a set of semantic rules — a DAG that cannot reference forward, options that cannot be unreachable, translations that cannot be half-done. A published version cannot change: a response records *which version it answered*, and "what did question 7 say?" must have one answer forever. This is why `/surveys/` is read-only, and why the retired feature's POST path was not carried over — a second write path would be a second engine, with its own idea of what a valid instrument is.

So the console manages a **lifecycle**, not a record:

```
upload  →  validate  →  draft  →  active  →  archived
             (never       (not      (one at    (kept, still
              written)    public)   a time)     readable)
```

**Editing is uploading a new version.** The UI should say so in those words rather than offering an Edit button that turns out to mean something else. Where a change is not answer-affecting — a theme, an effective date, an audience — it is not a version change and the UI can offer it directly.

### What that means for "delete"

There is no delete. An instrument with responses cannot be removed without removing what people told you; one without responses can be archived, which is the same thing minus the destruction. The console offers **Archive**, and says what it does.

---

## 2. Who administers what

| | Sees | May do |
| --- | --- | --- |
| **Staff** | every instrument | everything, including instruments scoped to any organisation |
| **Org admin** | instruments their organisation owns | upload, validate, activate, archive, export — for their own organisation only |
| **Analyst / doctor** | — | nothing here; response *data* reaches them through the existing exports |
| **Patient** | the Surveys tab | answer |

`IsStaffOrOrgAdmin` and `get_admin_orgs` already express this, including trust expansion, so the console reuses them rather than inventing a second rule. An org admin **may not** touch a platform-wide instrument, and the UI should not show them the buttons rather than failing them at the API.

---

## 3. Audience: the part that does not exist yet

Nothing in PROlog or PRomop can say *who a survey is for*. A survey is active or it is not, and the Surveys tab lists every active instrument to every patient. Three audiences are wanted:

| Audience | Means | Who resolves it |
| --- | --- | --- |
| **Everyone** | any respondent, signed in or not | nobody — this is today's behaviour, and what an anonymous public survey needs |
| **An organisation** | patients with a `PatientRecord` in that org | PRomop |
| **A group** | members of a `PatientGroup` (already org-scoped, already rule-managed or manual) | PRomop |

### Where it lives

The split follows the one that already works for participants. **PROlog gets a hook, PRomop gets the meaning.**

- PROlog: an instrument may declare `participation.audience: "host"`, and the runner asks a configured resolver — `PROLOG_AUDIENCE_RESOLVER`, alongside the participant resolver — whether *this* participant may start or resume *this* instrument. The runner knows nothing about organisations or groups, which keeps the public repository free of a customer's org model.
- PRomop: a `SurveyAudience` row per instrument — `everyone | organization | group`, with the FK — edited in this console, and a resolver that reads it.

Enforcement is **server-side on create and on resume**, not a filter on the Surveys tab. A tab that merely hides an instrument is a UI convenience; the runner refusing to create a response for somebody outside the audience is the actual control.

### Three cases the design has to answer, not discover

1. **An anonymous instrument cannot be group-scoped.** A respondent who is not signed in has no `PatientRecord` and no group, so an audience of anything but *everyone* is unanswerable by definition. The console should refuse that combination at the point of setting it, with the reason.
2. **Audience changes mid-fieldwork.** Narrowing an audience must not delete or invalidate responses already given by people now outside it — they answered in good faith. In-progress responses from newly-excluded respondents should be allowed to *finish* rather than being cut off mid-survey; the audience gates starting, not completing.
3. **A patient in two organisations.** Membership is a set, not a scalar: the audience is satisfied when any of the patient's records matches, which is the same rule `can_access_patient` already uses in the other direction.

---

## 4. The screens

### 4.1 Instruments — the list

One row per survey (not per version): title, slug, the active version and when it was published, audience, response count, and whether anything is in progress. Filters for status and audience. This is the landing page, and for most visits the only one.

Response counts are the number people actually want and the most expensive thing on the page; they should be an aggregate query, not a per-row count.

### 4.2 One instrument

Three sections on one page, because they are read together:

- **Versions** — every version with its status, checksum, when it was published and archived, and how many responses are bound to it. Activating a draft archives whatever was active; the button says that.
- **Audience** — the setting from §3, and, when it is not *everyone*, how many patients currently match. That number is what makes the setting real.
- **Responses** — counts by status, first and last response, and the export buttons. Two exports, never joined: responses without email addresses, contacts without answers.

### 4.3 Upload and verify

The flow that replaces the shell, and the one worth getting right:

1. **Drop a JSON file.** It is sent to a validation endpoint that **never persists** — the same `validate_definition` the command runs.
2. **Show what came back**, distinguishing what the engine distinguishes: **errors** refuse the file; **warnings** do not — "this option can never be selected", "this language is machine-translated". Show them all, not the first.
3. **Show what is about to change** before anything is written: is this a new instrument or a new version of an existing one; what the slug and version are; if that version already exists and differs, that it will be refused because a published version is immutable.
4. **Load as draft.** Never straight to active — activation is a separate, deliberate press, which is the rule the command line already enforces with `--activate`.

A definition that fails validation should be re-uploadable without losing the page, because fixing it is a loop.

### 4.4 Activation

A confirmation that names the consequences rather than asking "are you sure": which version is being activated, which one this archives, whether the instrument's translations are reviewed, and whether an audience is set. **Activating an instrument whose non-default languages are still machine-translated is refused** unless the deployment has opted into that (`PROLOG_MACHINE_LANGUAGES`), in which case the confirmation says that respondents will see a disclosure.

### 4.5 Translations

A version's languages and their status, with the side-by-side export (`export_translations`) as a download. A reviewer's corrections come back as a file; **importing them is not in this design** — a round trip has to decide what happens when the definition moved on since the export, and that deserves its own thinking.

---

## 5. API

All under the existing v1 prefix, all `IsStaffOrOrgAdmin`, all scoped by `get_admin_orgs` for a non-staff administrator.

| | |
| --- | --- |
| `GET /api/v1/admin/surveys/` | the list, with active version, audience and response counts |
| `GET /api/v1/admin/surveys/<slug>/` | versions, audience, response summary |
| `POST /api/v1/admin/surveys/validate/` | a definition in, issues out, **nothing written** |
| `POST /api/v1/admin/surveys/` | load a definition as a draft |
| `POST /api/v1/admin/surveys/<slug>/versions/<version>/activate/` | with `allow_unreviewed` only for staff |
| `POST /api/v1/admin/surveys/<slug>/versions/<version>/archive/` | |
| `PUT /api/v1/admin/surveys/<slug>/audience/` | everyone / organisation / group |
| `GET /api/v1/admin/surveys/<slug>/responses.csv` | the existing export, over HTTP |
| `GET /api/v1/admin/surveys/<slug>/translations.csv?language=es` | the review sheet |

The existing read-only `/surveys/` and `/survey-responses/` stay as they are: they are the v1 data contract, and this is an administration surface. Two audiences, two surfaces.

## 6. What has to be built where

| Where | What |
| --- | --- |
| **PROlog** | `PROLOG_AUDIENCE_RESOLVER` and the create/resume check; `participation.audience` in the schema; nothing about organisations or groups |
| **PRomop** | `SurveyAudience` model + migration; the resolver reading it; the admin endpoints; the console |
| **Neither** | a survey builder. Definitions are authored as files and reviewed like code — that is what makes them diffable, testable and revertible |

## 7. Decisions to settle before building

1. **Is a builder wanted eventually?** This design assumes not, and assumes upload. If FLF or another customer expects to edit questions in a browser, that is a different and much larger product, and the immutability rules are what it would have to fight.
2. **Who owns an instrument?** The design assumes an instrument belongs to an organisation (or to the platform). PROlog has no concept of ownership, so this is a PRomop column — and it is what makes an org admin's view coherent.
3. **Does an org admin get `--allow-unreviewed`?** Proposed: no. It exists for previewing machine translations, and it is exactly the setting that lets unreviewed clinical wording reach a patient.
4. **Scheduling.** PROlog has invitations and repeat administrations; this design does not surface them. Worth deciding whether the console covers "invite these people on this date" or stays a library of instruments.
