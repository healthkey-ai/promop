# Provisional data plan

Future work: OMOP rows and `PatientRecord` values written from **proposed**
source-code mappings, marked with the confidence the mapping was proposed at,
so a patient's page can show what the data probably says before a curator has
confirmed it. Tracking issue:
[#2434](https://github.com/healthkey-ai/promop/issues/2434). Prepared
2026-11-10 against `dev` `71a44790`, revised 2026-11-12 to keep `patient_record` unchanged
and put provisional data in a parallel pair of tables. This document
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

A **provisional** fact is an OMOP clinical row, or a patient-level value,
whose destination concept came from a proposed mapping rather than an approved
one. It is marked by a **confidence**: the ranker's confidence in that
proposal, as a whole-number percentage (0-100). The percentage is not the 0-1
float Suggest records on `SourceCodeConceptMapping.suggestion_confidence`: a
two-digit number is what the page shows and what a threshold is set in
(#1532), and an integer compares exactly. A proposal at 100 is still a
proposal.

The design keeps `patient_record` exactly as it is: a flat table of approved
data, read by everything that reads it today. Provisional data lives in a
**parallel pair of tables with the same column names**, so a consumer chooses
the table, not a column convention.

| Table | Holds | Null means | Read by |
|---|---|---|---|
| `patient_record` (unchanged) | approved data only | no approved answer | every existing consumer, with no change |
| `patient_record_provisional` | the best-known picture: the approved value where there is one, the proposal's value where only a proposal answers | no answer of either kind | a foundation or page that wants provisional data |
| `patient_record_confidence` | one percentage per field, under the same column name | the value in the provisional table is approved (or absent) | the badge, and any reader that must know which values to trust |
| `measurement`, `observation`, `condition_occurrence`, `drug_exposure`, `procedure_occurrence` | one new nullable `confidence` column | a real row: its concept came from an approved mapping, a direct identity, or a user's own edit | derivation, the OMOP tab, the lab APIs |

The `confidence` column on the five clinical tables stays on the row, not in a
parallel table: it is what approval clears and rejection reverts, and a
second table there would double every join on the largest tables.

Three invariants hold everywhere:

1. **Real beats provisional.** A field with an approved answer takes it; a
   provisional row answers a field only when no approved row does. A user's
   edit (`user_edited_fields`) is approved.
2. **Provisional rows follow their proposal.** Approving the proposal clears
   the confidence (the rows become real). Rejecting it, or re-pointing it
   elsewhere, takes the rows back to concept 0 with no confidence: exactly the
   unresolved row ingest would have written without this feature.
3. **`patient_record` never contains a provisional value.** Its derivation
   reads only rows with a null `confidence`. Every consumer that reads it
   today, including the legacy `patient-info` path, the federation host,
   PRism and FHIR export, is safe without being changed; the ones that read
   the clinical tables directly are the review list below.

## What is not provisional

- **A user edit.** `project_single_value` writes a real row, and the edit is
  mirrored into the provisional table with its confidence cleared. A patient
  or clinician typing a value is a fact, whatever the mapping queue says.
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

## Derivation

`refresh_patient_record` builds one snapshot of a person's rows and runs 26
section extractors over it. That pass is **unchanged**, with one rule added:
the snapshot's lists hold only rows whose `confidence` is null. Without that
filter a provisional row would become an approved value in `patient_record`,
which breaks invariant 3, so the filter and its test are the first change in
phase 1.

Provisional rows are split out of the same five table reads, in memory, into
`snapshot.provisional_*` lists. No query is added, so `BulkOmopWriteTest`'s
query-count assertions hold. A person with no provisional rows skips the
second pass entirely.

### The provisional pass

After the real record is derived, a second pass builds the person's row in
`patient_record_provisional`:

1. Start from the real `PatientRecord` values, all of them: demographics,
   user-entered fields, and everything derived. The provisional row is a full
   picture, so a consumer reads one table.
2. Run the **phase 1 readers** over a snapshot made of the provisional lists
   only, and keep a value only for a field that the real record leaves empty
   and that is not in `user_edited_fields`. The extractors are reused
   unchanged; the field-name allowlist, not a new code path, is what limits
   the pass.
3. Write the percentage of each kept value into `patient_record_confidence`
   under the same column name. A field whose value came from a real row has a
   null there.

Both new rows are written in the same transaction as `patient_record`, so
`derivation_version` and `derived_at` describe all three, and
`backfill_patient_records` rebuilds all three. A stale-marking
(`derivation_version=0`) therefore also covers the provisional pair.

### Phase 1 readers

| Reader | Field attribution |
|---|---|
| `_get_laboratory_data` and `_latest_loinc_measurement` | `resolve_lab_code_fields` names the code for each lab field |
| blood counts (`_latest_blood_count_measurements`) | `BLOOD_COUNT_FIELDS` |
| `curated_values_from_snapshot` | the approved `FieldConceptMapping` names the concept and source value per field |
| assertion fields (`_latest_assertion_rows`) | the assertion code per field |

### Readers that stay real-only in phase 1

Therapy lines, staging, disease status, cytogenetics, genomics, cancer
details, SCT and the other composite extractors combine many rows into one
answer and cannot attribute a confidence to a field without reworking each
one. The provisional pass does not run them: those columns in the provisional
table equal `patient_record`. A later phase can take them one at a time; the
rule for a composite field is the **minimum** confidence of the provisional
rows that joined its answer. JSON-typed fields are in this group, so the
confidence table has no column for them until then.

### Computed fields

`recompute_patient_record_fields` computes BMI, scores and aliases from stored
fields. On the provisional row it runs again over the overlaid values. A
computed field whose inputs include a provisional value is itself provisional
at the minimum of its inputs' confidences. That needs a declared input list
per computed field, which the formula evaluator holds for formulas but the
hand-coded computations do not; phase 1 writes the list for the ones that
read a phase 1 field and leaves the rest equal to `patient_record`.
Custom fields (`custom_fields` JSON) are phase 3.

### User edits

A PATCH to `patient_record` writes the value as today. Because direct saves do
not call `refresh_patient_record()`, the same save also writes the edited
fields into the provisional row and nulls their confidence, so the provisional
picture never shows a proposal where a person has just typed a fact. The
edit is also protected on the next derivation by `user_edited_fields`, as the
real value is. The confidence table is read-only to every client.

A page that shows the provisional picture must **not** use it as the PATCH
baseline. Native autosave sends its whole GET representation, and a
provisional value echoed back would be saved as an approved one. The page
keeps the real record as the baseline, diffs edits against it, and sends only
changed fields; a frontend test sends an untouched form and asserts an empty
PATCH.

### Performance

The provisional pass costs one extra row upsert per table and no extra
snapshot query. Staging holds about 3,000 patient records in 21 MB, so a full
provisional row per person is small there. The HealthTree instance has its
own database and was not measured; the first task in phase 0 is to count its
`patient_record` rows. If the count is large enough that two more wide tables
matter, the provisional table can be made sparse (a row only for a person
with at least one provisional value, the read API falling back to the real
record) at the cost of a coalesce for anyone reading it in SQL.

## Schema

**Clinical tables.** `confidence = SmallIntegerField(null=True)` on the five
models (migration `0339`), with a check constraint `0 <= confidence <= 100`, and
a partial index per table on `(person_id) WHERE confidence IS NOT NULL`. The
column is nullable with no default, so the `ADD COLUMN` is a catalogue change on
every table including `measurement`. It is a signed `smallint` on purpose:
`PositiveSmallIntegerField` adds a `>= 0` check to the `ADD COLUMN` statement,
and a check on an added column scans the table under an exclusive lock. The
range check is added `NOT VALID` and validated in a second statement, which
blocks no writes. The partial indexes are built with
`AddIndexConcurrently` in a non-atomic migration: a plain build scans the
whole table under a lock, and the Cloud Run migrate job has an hour (#2021).

**`patient_record_provisional`.** One row per person, primary key
`person` (one-to-one). Every concrete `PatientRecord` column except the
identity key and lifecycle columns is cloned with the same name and type, at
class-creation time from `PatientRecord._meta` (`field.clone()` in a loop
beside the class), not hand-typed. Constraints, indexes and the `organization`
foreign key are carried only where a reader needs them (tenancy filtering uses
`organization`). `makemigrations` sees the clones as ordinary fields, so a
field added to `PatientRecord` gets its provisional twin in the same
migration, three `AddField` operations instead of one.

**`patient_record_confidence`.** One row per person, primary key `person`.
One nullable `PositiveSmallIntegerField` per non-JSON field in
`_OMOP_DERIVED_FIELDS`, 274 today, named exactly as the field is. Generated
the same way. Phase 3 adds the JSON-typed derived fields.

A parity test fails the build when the three tables drift: the provisional
table must have every cloned column of `patient_record`, and the confidence
table must have exactly the derived non-JSON fields. The model pair is
classified in `omop_core/services/instance_data.py` like `PatientRecord`:
derived and re-derived after `copy_patient`, not copied
([Copying data between instances](../AGENTS.md#copying-data-between-instances)).
`tests/test_instance_data.py` fails until they are.

Creating the two tables needs no lock on existing data, and no backfill
migration: `backfill_patient_records --all` populates them, and until a person
is derived the API serves the real record for the provisional view.

**Bulk write identity.** `confidence` stays outside `_UPSERT_KEYS`, like the
concept column: an ETL re-run after an approval converges on the promoted
row instead of inserting a second one. Only the concept column is rewritten
on a matched row today; `confidence` joins it, and only toward certainty: a
match may set a row's confidence to null (the caller now has an approved
concept) but never put a number on a real row.

## API

The provisional picture is a separate read-only resource, so the current
`/api/v1/patient-records/` contract does not change and the legacy path is not
touched:

```
GET /api/v1/patient-records-provisional/{person_id}/
GET /api/v1/patient-records-provisional/            (list, same tenancy as the real one)
  200 {<every patient_record field, from the provisional row>,
       "confidence": {"hemoglobin_g_dl": 82, "her2_status": 91}}
```

`confidence` lists only the fields with a number, so a fully approved person
returns an empty object. Tenancy and role checks reuse
`PatientRecordViewSet.get_queryset`; the patient role is refused (403) in
phase 1. A person not yet derived returns the real record with an empty
`confidence`. Foundations that need it in bulk read the two tables by column
name through the same endpoint's list form, or directly, joined on `person`.

## Settings

| Setting | Default | Meaning |
|---|---|---|
| `PROVISIONAL_DATA_ENABLED` | `False` | master switch; off, every writer behaves as today (a clinical write carrying a confidence number is refused with 400; null is always accepted) and the provisional pair is neither written nor served |
| `PROVISIONAL_CONFIDENCE_FLOOR` | `70` | least confidence a provisional row is written at |
| `Organization.accepts_provisional_data` | `False` | FHIR sync for this organization passes `accept_proposed` |

The floor and the auto-approval threshold of #1532 are the same number in
spirit: a proposal at or above the auto-approval threshold would become real,
so the floor only matters for the band between the two. Build the floor first;
#1532 can read it.

## What consumers see

**Existing consumers of `patient_record`.** Nothing changes: the legacy
`patient-info` path, the federation host (`ht-phr`), PRism, FHIR export and
every PATCH read and write approved data only. This is the main reason for the
split.

**Patient page (professional roles).** The page loads the provisional view and
shows each provisional value with an amber "Provisional 82%" badge after the
label, with a title saying the value came from an unapproved mapping and a link
to the mapping dialog for its code (the Source Codes tab already links a code
to its mapping). The control reads the badge from `confidence[name]`. An edit
on a provisional field saves to the real endpoint as an approved value, and
the badge goes. A per-user "show provisional" toggle returns the page to the
approved-only view.

**Patient page (patient role) and PHR APIs.** A patient sees no provisional
values in phase 1: the role cannot reach the provisional endpoint, and the PHR
labs API (`patient_portal/api/phr/labs.py`) skips provisional rows as it skips
unproven ones. Showing a patient an unconfirmed diagnosis is a product
decision for a later phase.

**Patient OMOP tab and Source Codes tab.** A provisional row shows the badge
in its row. The "Generate OMOP" banner says how many provisional rows the run
wrote, and the per-code detail reports `outcome: 'provisional'` with the
confidence.

**Lab results API.** A provisional row's entry carries `confidence`, and the
reads that choose "the latest result" prefer a real row over a provisional
one on the same date, as derivation does.

**A foundation that wants provisional data.** It reads
`patient_record_provisional` for values and `patient_record_confidence` for
the trust level, joined on `person`, with identical column names in both, or
the API above. It reads `patient_record` for approved data only.

**Readers of the clinical tables.** `patient_record` is safe, but anything that
reads `measurement`, `observation`, `condition_occurrence`, `drug_exposure` or
`procedure_occurrence` directly would count a provisional row as a fact. The
existing `is_erroneous=False` filters (61 call sites) are the inventory to
review. Each either keeps provisional rows deliberately (the patient page, the
OMOP tab) or adds `confidence IS NULL` (anything aggregate or outbound, and
FHIR export, which excludes them unless `include_provisional=true` and then
marks each with a `provisional-confidence` extension). The review is a checklist
in the PR for phase 1, not a change at every site.

**Mapping pages.** The Code to Concept list shows, per proposed row, how many
provisional rows it currently backs, so a curator knows what an approval
promotes and a rejection reverts. #2263 (keep the confidence visible on the
existing-mapping view) is the same number and lands with this.

## Phases

### Phase 0: schema and contract (no behaviour change)

- Count `patient_record` rows on the HealthTree instance and settle full versus
  sparse provisional rows.
- `confidence` on the five clinical models, constraint and partial indexes.
- `patient_record_provisional` and `patient_record_confidence`, generated from
  `PatientRecord` and `_OMOP_DERIVED_FIELDS`, with the parity test and the
  `instance_data` classification.
- Serializers: `confidence` accepted on bulk and single row writes from machine
  credentials and staff, refused at concept 0 and from session users; outside
  the upsert key.
- `PROVISIONAL_DATA_ENABLED` and `PROVISIONAL_CONFIDENCE_FLOOR` read in
  settings.
- Tests: model round trip, constraint, serializer refusal, bulk write
  acceptance and refusal, upsert convergence on a promoted row, table parity.

### Phase 1: Generate OMOP, derivation and the page

- Snapshot filter: `patient_record` derives from null-confidence rows only.
  Written and tested first.
- `_create_missing_clinical_rows` and `resolve_person_source_codes` write and
  promote/revert provisional rows behind the switch.
- `repoint_clinical_rows` promotes on approval; `revert_provisional_rows` on
  rejection and on a moved proposal, wired into `_upsert_source_code_mapping`
  and Suggest's re-point.
- The provisional pass, the two table writes, the PATCH mirror, and the
  `patient-records-provisional` endpoint.
- Frontend: badge on `ClinicalField`/`Field`, real record as PATCH baseline,
  show-provisional toggle, PHR labs skip, Source Codes tab outcome.
- Tests: `patient_record` unchanged by a provisional row; derivation
  precedence (real beats provisional; provisional fills an empty field; user
  edit wins; revert clears); approval promotion; rejection revert; patient role
  refused; an untouched form sends an empty PATCH; React badge render and edit.

### Phase 2: the ETL path

- `code-mappings/lookup/` `include_provisional` and the response fields.
- ETL repository change to write `confidence` and to re-send after approval.
- Lab results API and FHIR export handling.
- Mapping-page counts and #2263.

### Phase 3: wider reach

- Composite extractors (therapy lines, staging, genomics) one at a time, each
  with the minimum rule.
- JSON derived fields and custom fields in the confidence table.
- FHIR sync opt-in per organization.
- Patient-facing display policy.
- #1532 auto-approval reading the same floor.

Each phase is its own PR against `dev`, with a migration in phase 0 only;
rebase on `dev` immediately before `makemigrations` and again before merge
(see [Migration conflicts](../AGENTS.md#migration-conflicts)).

## Alternatives considered

- **Companion columns on `patient_record`** (a `<field>_` beside each field).
  Rejected: it widens the flat table by 274 columns, and the provisional value
  would sit in the approved column, so any reader that ignored the companion
  would show a proposal as a fact.
- **One JSON confidence map on `patient_record`.** Rejected for the same
  second reason, and it loses the same-column-name property in SQL.
- **A database view over the real table.** Rejected: the frozen `patient_info`
  view (#448) shows how a view that mirrors the record drifts the first time a
  column is added. Generated tables with a parity test fail loudly instead.

## Open decisions

1. **Full versus sparse provisional rows.** Full means one row per person and
   a one-table read; sparse saves storage on a large instance. Settled by the
   row count in phase 0.
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
5. **Default view for professionals.** The plan shows the provisional view by
   default with a toggle to approved-only. The opposite default is safer for a
   clinician who does not read the badge, and is a one-line change.

## Related

- #1532 auto-approval threshold, #2263 confidence on the existing-mapping
  view, #2123 lookup sightings (the existing `provisional` keyword).
- [Code mapping lookup](code-mapping-api.md), [field concept mapping
  architecture](field_concept_mapping_architecture.md),
  [PatientRecord-first writes](patient-record-first-writes.md).
