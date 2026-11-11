# Provisional data plan

Future work: OMOP rows and `PatientRecord` values written from **proposed**
source-code mappings, marked with the confidence the mapping was proposed at,
so a patient's page can show what the data probably says before a curator has
confirmed it. Tracking issue:
[#2434](https://github.com/healthkey-ai/promop/issues/2434). Prepared
2026-11-10 against `dev` `71a44790`. This document
specifies the work; nothing here is implemented yet. The write architecture it
builds on is [PatientRecord-first writes](patient-record-first-writes.md) and
the resolution rules in `omop_core/mapping/code_resolution.py`.

## Problem

Ingest never acts on a proposed mapping. `resolve_source_code` returns no
concept for a code whose only mapping is `proposed`, so the fact is stored at
concept 0 (FHIR sync, the upload endpoint), left out of the clinical tables
altogether (the ETL, which writes only what `code-mappings/lookup/` resolves),
or never created from the per-patient inventory (Generate OMOP creates rows
only for approved mappings). The patient page shows nothing for the field.

On HealthTree most hospital codes are proposed, not approved: Suggest has
scored them, often at 80-95% confidence, but a curator has not reached them.
Every one of those codes is a lab result or a diagnosis the patient's page
cannot show, and a backlog of thousands of codes means the page stays empty
for months while the answer sits in the queue.

## Definition

A **provisional** fact is an OMOP clinical row, or a `PatientRecord` value,
whose destination concept came from a proposed mapping rather than an approved
one. It is marked by a **confidence**: the ranker's confidence in that
proposal, as a whole-number percentage.

| Where | Column | Null means | A number means |
|---|---|---|---|
| `measurement`, `observation`, `condition_occurrence`, `drug_exposure`, `procedure_occurrence` | `confidence` (small integer, 0-100) | a real row: its concept came from an approved mapping, a direct identity, or a user's own edit | a provisional row: its concept is a proposal at that confidence |
| `patient_record` | `<field>_` beside each OMOP-derived field (small integer, 0-100) | the value is real | the value is provisional at that confidence |

The companion column is the field's name followed by an underscore:
`hemoglobin_g_dl` and `hemoglobin_g_dl_`, `her2_status` and `her2_status_`.
No existing field ends in an underscore, so the pair cannot collide. The
confidence is stored as a percentage, not the 0-1 float Suggest records on
`SourceCodeConceptMapping.suggestion_confidence`: a two-digit number is what
the page shows and what a threshold is set in (#1532), and an integer column
compares exactly. 100 is allowed in the schema but a proposal at 100 is still
a proposal; the page shows it as provisional like any other.

Three invariants hold everywhere:

1. **Real beats provisional.** A field with any real fact is derived from real
   facts only; a provisional row answers a field only when no real row does. A
   user's edit (`user_edited_fields`) is real.
2. **Provisional rows follow their proposal.** Approving the proposal clears
   the confidence (the rows become real). Rejecting it, or re-pointing it
   elsewhere, takes the rows back to concept 0 with no confidence: exactly the
   unresolved row ingest would have written without this feature.
3. **The marker travels with the value.** No read path hands a provisional
   number to a consumer without its confidence beside it, and no path that
   leaves promop (export, analytics, the patient's own view) includes
   provisional data unless it asked for it.

## What is not provisional

- **A user edit.** `project_single_value` writes a real row and clears the
  field's `_` column. A patient or clinician typing a value is a fact, whatever
  the mapping queue says.
- **A direct identity.** LOINC and SNOMED codes, and Athena `Maps to` targets,
  resolve without a mapping and are real (rules 2 and 3a in `code_resolution`).
- **A quarantine placeholder.** An import-minted `HK-*` concept is not a
  destination a provisional row may carry: `_usable_destination` refuses it as
  it does for the promotion path. A proposal whose target is a placeholder
  writes what it writes today.
- **A proposal with no confidence.** An import proposal that nothing ranked
  (`suggestion_confidence` null) carries no number, so no provisional row can
  be written from it. Only a ranked proposal qualifies.
- **A proposal below the floor.** `PROVISIONAL_CONFIDENCE_FLOOR` (default 70,
  the threshold #1532 proposes for auto-approval) is the least confidence a
  provisional row is written at. Below it the code stays unresolved as today.

## Where provisional rows come from

There are four writers of clinical rows from source codes. Each gets an
explicit opt-in; none changes behaviour by default.

| Writer | Today | With provisional data |
|---|---|---|
| Generate OMOP (`_create_missing_clinical_rows`, `resolve_person_source_codes`) | creates and re-points rows for approved mappings only | also creates a row for a ranked proposal at or above the floor, with its confidence; re-resolve promotes or reverts provisional rows as the proposal moved |
| `code-mappings/lookup/` (the ETL) | an unresolved entry carries `proposed_target_concept_id` as curator evidence, which the ETL must not write | with `include_provisional: true` in the request, such an entry also carries `provisional: true` and `confidence`; the ETL writes the row with that `confidence` through the bulk endpoints |
| Bulk OMOP writes (`POST /api/v1/measurements/` etc.) | no `confidence` field | accept `confidence` on a row; refuse it on a row at concept 0 and refuse it from a session user (machine credentials and staff only) |
| FHIR sync and `upload_fhir_bundle` (`resolve_source_code`) | a proposed mapping returns no concept | a new `accept_proposed=True` argument returns the proposal's concept and confidence; sync passes it when the organization or deployment opts in |

`resolve_source_code_variant` already has a `provisional` keyword that means
something else (one lookup sighting per batch, #2123). The new argument is
`accept_proposed`, and the docstring says why the two are different.

Generate OMOP is the first writer to build, because it is the one HealthTree
needs: the per-patient inventory (`PatientSourceCode`) is loaded, the
proposals are ranked, and a curator can run it from the patient page today.
The lookup contract is second: the ETL repository has to change with it. FHIR
sync is third and is gated per organization.

## Approval, rejection and a moved proposal

`repoint_clinical_rows` is the one place rows change concept after the fact,
and it matches rows on the mapping's source value **and** the old destination.
Provisional rows sit at the proposal's concept, so three cases extend it:

- **Approved at the same concept.** No re-point is needed. The approval path
  clears `confidence` on rows matching the source value at that concept, in
  the same statement that marks the records stale.
- **Approved at another concept.** The re-point moves rows from the old
  destination to the new one as today, and also moves the provisional rows at
  the proposed concept (added to the old-destination set, as
  `deferred_repoint_concept_ids` already carries concepts a patient-scoped
  approval left behind) and clears their confidence.
- **Rejected, or the proposal re-pointed by Suggest or a curator.** Provisional
  rows at the previous proposed concept go to concept 0 with confidence
  cleared (`revert_provisional_rows`, in the same module). Rows ingest wrote
  at concept 0 in the first place are untouched. A later Suggest run that
  proposes a new concept writes new provisional rows only when Generate OMOP or
  the ETL runs again: a proposal moving does not itself write rows.

Every one of those marks the patients stale (`derivation_version=0`) and
leaves the derivation to `backfill_patient_records`, as the re-point does.
Collapse of duplicates (`_collapse_duplicates`) treats `confidence` as part of
the identity: a real row and a provisional row for the same event are two
rows until the provisional one is promoted, at which point the next collapse
folds them.

Unit normalization on approval (`normalize_measurement_units`) runs on
promoted rows as on re-pointed ones. Provisional rows are **not** normalized
at write time: the proposal has no curated `destination_unit_concept`, and a
write-time conversion is an approved mapping's privilege (#2157).

## Derivation into `PatientRecord`

`refresh_patient_record` builds one snapshot of a person's rows and runs 26
section extractors over it. Provisional rows enter the snapshot in separate
lists (`snapshot.provisional_measurements`, `snapshot.provisional_observations`,
and so on), never in the lists the extractors read today. The extractors that
can say which rows answer which field read the provisional lists as a
fallback; the ones that cannot do not see them at all.

### Phase 1 readers

| Reader | Field attribution |
|---|---|
| `_get_laboratory_data` and `_latest_loinc_measurement` | `resolve_lab_code_fields` names the code for each lab field |
| blood counts (`_latest_blood_count_measurements`) | `BLOOD_COUNT_FIELDS` |
| `curated_values_from_snapshot` | the approved `FieldConceptMapping` names the concept and source value per field |
| assertion fields (`_latest_assertion_rows`) | the assertion code per field |

For each such field: if a real row answers it, the value is that row's and
`<field>_` is null. Otherwise, if a provisional row answers it, the value is
that row's and `<field>_` is its confidence. Otherwise both are null.
`_clear_derived_fields` clears every `_` column with its field, so a
proposal reverted since the last derivation leaves nothing behind.

### Readers that stay real-only in phase 1

Therapy lines, staging, disease status, cytogenetics, genomics, cancer
details, SCT and the other composite extractors combine many rows into one
answer and cannot attribute a confidence to a field without reworking each
one. They read the real lists only. A later phase can take them one at a
time; the rule for a composite field is the **minimum** confidence of the
provisional rows that joined its answer.

### Computed fields

`recompute_patient_record_fields` computes BMI, scores and aliases from stored
fields. In phase 1 a computed field from a provisional input is itself
provisional at the minimum of its inputs' confidences, carried through the
same `_` column; a formula whose every input is real stays real. Custom
fields (`custom_fields` JSON) get a sibling `custom_fields_confidence` JSON
object keyed by field name, since they have no columns of their own.

### User edits

A PATCH that sets a field writes the value and sets `<field>_` to null in the
same save, before projection. The serializer refuses every `_` column from the
client (`read_only_fields` is extended with every name ending in `_`, computed
from the model so a new field cannot be missed), and `useWritableFields`
excludes them from the writable sweep. Derivation preserves a user-edited
field as today; its `_` stays null because the edit is real.

### Performance

Loading the provisional lists adds no query: the snapshot's five table reads
already fetch every non-erroneous row, and the rows are split in memory by
`confidence`. The lab reader's fallback runs only when the real list left a
field empty. `BulkOmopWriteTest`'s query-count assertions hold.

## Schema

**Clinical tables.** `confidence = PositiveSmallIntegerField(null=True)` on
the five models, with a check constraint `confidence <= 100`, and a partial
index per table on `(person_id) WHERE confidence IS NOT NULL`. The column is
nullable with no default, so the `ADD COLUMN` is a catalogue change on every
table including `measurement`. The partial indexes are built with
`AddIndexConcurrently` in a non-atomic migration: a plain build scans the
whole table under a lock, and the Cloud Run migrate job has an hour (#2021).

**`patient_record`.** One `PositiveSmallIntegerField(null=True)` per field in
`_OMOP_DERIVED_FIELDS` that is not a JSON field, 274 columns, named
`<field>_`. They are declared from that list at class-creation time
(`PatientRecord.add_to_class(f'{name}_', ...)` in a loop at the bottom of
`models.py`, next to the class), not hand-typed: `makemigrations` sees them
as ordinary fields, a field added to the list gets its companion, and a
field removed loses it in the same migration. `tests/test_instance_data.py`
style guard: a test asserts every derived non-JSON field has its companion and
no `_` column is orphaned. The 61 JSON derived fields have no companion in
phase 1 (their readers are composite). Adding 274 nullable columns is one
catalogue change; `patient_record` is a few thousand rows on every instance.

The `PatientRecordSerializer` uses `fields = '__all__'`, so every `_` column
is in every response from the day of the migration, as null. That is the
intent: the frontend's `PatientInfo` type gets
`[key: \`${string}_\`]: number | null | undefined` and reads
`patient[\`${name}_\`]` beside `patient[name]`.

**Bulk write identity.** `confidence` stays outside `_UPSERT_KEYS`, like the
concept column: an ETL re-run after an approval converges on the promoted
row instead of inserting a second one. Only the concept column is rewritten
on a matched row today; `confidence` joins it, and only downward in
certainty: a match may set a row's confidence to null (the caller now has an
approved concept) but never put a number on a real row.

Both commands in [Copying data between instances](../AGENTS.md#copying-data-between-instances)
carry the new columns unchanged: they are columns on models already
classified, so `copy_patient` copies them with the row and
`copy_reference_data` is unaffected.

## Settings

| Setting | Default | Meaning |
|---|---|---|
| `PROVISIONAL_DATA_ENABLED` | `False` | master switch; off, every writer behaves as today and the `_` columns stay null |
| `PROVISIONAL_CONFIDENCE_FLOOR` | `70` | least confidence a provisional row is written at |
| `Organization.accepts_provisional_data` | `False` | FHIR sync for this organization passes `accept_proposed` |

The floor and the auto-approval threshold of #1532 are the same number in
spirit: a proposal at or above the auto-approval threshold would become real,
so the floor only matters for the band between the two. Build the floor first;
#1532 can read it.

## What consumers see

**Patient page (professional roles).** Every field control gets a
`confidence` prop. A provisional value renders with an amber "Provisional
82%" badge after the label and amber text, and the badge's title says the
value came from an unapproved mapping and links to the mapping dialog for its
code (the Source Codes tab already links a code to its mapping). An edit on a
provisional field saves as a real value and the badge goes.

**Patient page (patient role) and PHR APIs.** A patient sees no provisional
values in phase 1: the `/patient-info/me/` serializer nulls every field whose
`_` is set, and the PHR labs API (`patient_portal/api/phr/labs.py`) skips
provisional rows as it skips unproven ones. Showing a patient an unconfirmed
diagnosis is a product decision for a later phase, not a default.

**Patient OMOP tab and Source Codes tab.** A provisional row shows the badge
in its row. The Source Codes tab's "Generate OMOP" banner says how many
provisional rows the run wrote, and the per-code detail reports
`outcome: 'provisional'` with the confidence.

**Lab results API.** A provisional row's entry carries `confidence`, and the
reads that choose "the latest result" prefer a real row over a provisional
one on the same date, as derivation does.

**FHIR export (`fhir_export.py`).** Provisional rows are excluded unless the
caller passes `include_provisional=true`, in which case each resource
carries an extension `provisional-confidence` with the number. Export is the
main way a fact leaves promop, and an unconfirmed fact leaving without its
marker breaks invariant 3.

**PRism and aggregated analytics.** Any query over the clinical tables that
feeds an aggregate adds `confidence IS NULL` unless it opts in. The existing
`is_erroneous=False` filters (61 call sites) are the inventory of readers to
review; each either keeps provisional rows deliberately (the patient page) or
excludes them (anything aggregate or outbound). The review is a checklist in
the implementing PR, not a code change for every site.

**Mapping pages.** The Code to Concept list shows, per proposed row, how many
provisional rows it currently backs, so a curator knows what an approval
promotes and a rejection reverts. #2263 (keep the confidence visible on the
existing-mapping view) is the same number and lands with this.

## Phases

### Phase 0: schema and contract (no behaviour change)

- `confidence` on the five clinical models, constraint and partial indexes.
- `<field>_` companions on `PatientRecord`, generated from
  `_OMOP_DERIVED_FIELDS`, with the orphan guard test.
- Serializer: `_` columns read-only; `confidence` accepted on bulk and single
  row writes from machine credentials and staff, refused at concept 0 and
  from session users; outside the upsert key.
- `PatientInfo` type and `useWritableFields` exclusion.
- `PROVISIONAL_DATA_ENABLED` and `PROVISIONAL_CONFIDENCE_FLOOR` read in
  settings.
- Tests: model round trip, constraint, serializer refusal, bulk write
  acceptance and refusal, upsert convergence on a promoted row.

### Phase 1: Generate OMOP, derivation and the page

- `_create_missing_clinical_rows` and `resolve_person_source_codes` write and
  promote/revert provisional rows behind the switch.
- `repoint_clinical_rows` promotes on approval; `revert_provisional_rows` on
  rejection and on a moved proposal, wired into `_upsert_source_code_mapping`
  and Suggest's re-point.
- Snapshot split; phase 1 readers fall back to provisional rows and set `_`;
  `_clear_derived_fields` clears them; computed fields carry the minimum.
- PATCH clears `_` on edit.
- Frontend badge on `ClinicalField`/`Field`, patient-role nulling, PHR labs
  skip, Source Codes tab outcome.
- Tests: derivation precedence (real beats provisional; provisional fills an
  empty field; user edit wins; revert clears), approval promotion, rejection
  revert, serializer nulling for patients, React badge render and edit.

### Phase 2: the ETL path

- `code-mappings/lookup/` `include_provisional` and the response fields.
- ETL repository change to write `confidence` and to re-send after approval.
- Lab results API and FHIR export handling.
- Mapping-page counts and #2263.

### Phase 3: wider reach

- Composite extractors (therapy lines, staging, genomics) one at a time, each
  with the minimum rule.
- JSON derived fields and `custom_fields_confidence`.
- FHIR sync opt-in per organization.
- Patient-facing display policy.
- #1532 auto-approval reading the same floor.

Each phase is its own PR against `dev`, with a migration only in phase 0;
rebase on `dev` immediately before `makemigrations` and again before merge
(see [Migration conflicts](../AGENTS.md#migration-conflicts)).

## Open decisions

1. **Companion columns versus one JSON column.** The design above follows the
   per-field `_` column. The alternative is one `field_confidence` JSON
   object keyed by field name, like `user_edited_fields`: one column, no
   generated declarations, and composite and JSON fields covered for free.
   The per-field column was chosen because it is queryable (`WHERE
   hemoglobin_g_dl_ IS NOT NULL`) and because the API shape, a key beside
   each key, is what the frontend reads most simply. It costs 274 columns
   and a serializer response that grows by that many nulls.
2. **Revert versus delete on rejection.** The plan reverts a rejected
   proposal's rows to concept 0 rather than deleting them, because the ETL
   would re-send a deleted row on its next run and because a row at concept 0
   is what ingest writes today. A row Generate OMOP created from the
   inventory, which would not otherwise exist, is the one case where a delete
   would be cleaner; it is kept as an unresolved row so that one rule holds.
3. **What a patient sees.** Phase 1 hides provisional values from the patient
   role. Showing them with a badge is a product call that needs a clinician's
   view of the risk of an unconfirmed diagnosis on a patient's own page.
4. **The floor's scope.** One deployment-wide floor in phase 1. A per-organization
   floor, or per domain (a provisional lab value is a smaller claim than a
   provisional condition), is a phase 3 setting if curators ask for it.

## Related

- #1532 auto-approval threshold, #2263 confidence on the existing-mapping
  view, #2123 lookup sightings (the existing `provisional` keyword).
- [Code mapping lookup](code-mapping-api.md), [field concept mapping
  architecture](field_concept_mapping_architecture.md),
  [PatientRecord-first writes](patient-record-first-writes.md).
