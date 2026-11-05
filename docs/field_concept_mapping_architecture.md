# Field concept mapping architecture

Implemented behavior reviewed against `dev` at `10c5644` on 2026-09-17.
This document explains how UI fields, PatientRecord and OMOP mappings interact.
Future schema, mapping review, repairs and delivery gates belong to the
[field concept mapping plan](field_concept_mapping_plan.md).

## Reference ownership

| Reference | What it owns |
|---|---|
| This architecture | Implemented mapping models, runtime resolution and field ownership |
| [PatientRecord-first writes](patient-record-first-writes.md) | Detailed PATCH, projection, clear and refresh contracts |
| [Generated field/value inventory](field-mapping-inventory/README.md) | Reproducible source rows, mappings, candidates, context and coverage evidence |
| [Mapping plan](field_concept_mapping_plan.md) | Unresolved meanings, proposed capabilities, dependencies and acceptance |

The generated inventory is the field-by-field evidence source
([PR #1284](https://github.com/healthkey-ai/promop/pull/1284), merged). Its
[manifest](field-mapping-inventory/manifest.json) includes source revisions,
field and option identities, existing recipes, candidate evidence and validation
flags; its [coverage report](field-mapping-inventory/coverage.md) states the
export date and omissions. It is incomplete and does not clinically approve
candidates. Deployment-specific recipes must be checked against that deployment's
current mapping and vocabulary tables. Do not copy snapshot concept IDs into a
second manually maintained UI-to-concept table.

## Implemented data model

| Model | Current responsibility |
|---|---|
| `FieldConceptMapping` | Field-level recipe: concept, destination, source key, value kind, units, type, review state and provenance |
| `FieldChoice` | Allowed display values keyed by `(field_name, display)`, with ordering and creator metadata |
| `FieldChoiceCode` | Vocabulary/code aliases for a choice, with a primary flag; this does not independently approve an answer mapping |
| `CustomPatientField` / `FieldFormula` | Dynamic field definitions and computed values using the existing mapping/formula services |
| Therapy reference models | Regimen, component, class and disease/round relationships, resolved by their dedicated authoring paths |

The models are in [omop_core/models.py](../omop_core/models.py). The current choice
schema does not provide stable scoped answer identity or a reviewed
`FieldValueConceptMapping` model. The plan owns that extension.

`FieldConceptMapping.provenance` distinguishes System Generated and Curator
recipes. Approval state is separate from origin: approving a recipe does not
change who supplied it. Migration 0239 backfilled `provenance = 'system_generated'`
on all 314 legacy rows that had blank provenance (created by seed migrations
0156–0227, before the provenance field existed). No blank-provenance rows remain
on staging. The mapping API and curation transfer preserve this distinction. A
recipe's OMOP `type_concept_id` is fact provenance, not this curation-origin label.

## Runtime field ownership

The backend [writable descriptor](../omop_core/services/write_descriptor.py) resolves
field ownership, allowed values and available projections from code and installed
mappings. Frontends consume that contract instead of selecting an OMOP table or
hardcoding a vocabulary ID. Patient authorization still constrains each operation;
a mapping cannot grant permission to edit a record.

| Field category / example | Implemented save or derivation owner |
|---|---|
| Direct scalar clinical field, such as `hemoglobin` | PatientRecord PATCH; a supported descriptor projection can also write OMOP |
| Direct scalar with no usable projection | Saved to PatientRecord with pending-edit protection; no fabricated concept or fact |
| Profile fields, including birth, contact and address values | PatientRecord PATCH and the backend Person/Location projection; `patient_name` is a virtual input |
| Alias, such as `absolute_neutrophile_count` → `anc_thousand_per_ul` | Canonical field is the editor; the alias has no independent write recipe |
| Lab field derived from Measurement, such as `hemoglobin_g_dl` | Routed by its approved `FieldConceptMapping` and that concept's approved `SourceCodeConceptMapping` LOINC aliases (`resolve_lab_code_fields`); `_LOINC_LAB_FIELDS` seeds those rows and decides only for a field with no concept-resolved mapping |
| Computed values, such as BMI and wearable aggregates | Derived from inputs or source events; no independent scalar assertion |
| Entered treatment courses and supportive therapy | Dedicated clinical resource writers; Episode/Event and drug facts supply summaries |
| `genetic_mutations` and priority Genomics findings | Canonical Genomics writer and approved parent/component mappings |
| `cytogenetic_markers` | Read-only legacy summary; discrete interactive findings belong to Genomics |
| Language skills, allergies, surveys and the OMOP browser | Dedicated resources with their own contracts |
| Dynamic fields | `PatientRecord.custom_fields` and their definitions/recipes; computed custom fields use formulas |

The descriptor includes structured Genomics cases as well as ordinary direct
fields. Do not infer that every writable field uses the generic scalar writer,
or that the presence of an enum supplies a coded-answer projection.

### Where a field renders: `FieldConceptMapping.tab`

A field's placement is one value, the patient tab it renders on
(`FieldConceptMapping.tab`, #2328; `FIELD_TAB_CHOICES` is the one list the
mapping row, `CustomPatientField` and the Field Mappings page share). It is
not a layout: a placed field renders last on its tab, on its own row.
`field_descriptor.resolve_tab` reads the mapping's tab, then a custom field's
own, then the tab the code gives a built-in field (`_classify_tab`), and the
descriptor emits the result as `tab`. A field nothing places is `omop`,
unplaced: it keeps its mapping and derivation, no tab renders a control for
it, and it is listed on the mapping page's OMOP (unplaced) tab so it is seen
rather than lost. The column travels with the mapping through
`copy_reference_data` and the curation fixture, and a payload written before
it existed reads as blank. Rendering placed fields on the patient tabs and the
curation control are #2330 and #2329.

### Lab derivation reads the mapping tables

Every reader that turns a LOINC code into a lab field at derivation time (the
lab extractor, the FLC pairing, the SLiM scan, the ANC/ALC/AMC/platelet
selection, the provenance registry and the demo-profile recovery) takes its
routing from `resolve_lab_code_fields` in the
[PatientRecord service](../omop_core/services/patient_record_service.py),
resolved once per refresh. An approved `FieldConceptMapping` with a loaded
concept names each lab field's canonical code; approved global
`SourceCodeConceptMapping` rows whose target is that concept add alias codes,
so a curator's alias selects, converts and attributes a result exactly as the
canonical code does. `_LOINC_LAB_FIELDS` in the same module is the checked-in
inventory of the lab fields and their codes: migration 0240 seeds the mapping
rows from `field_curation_v1.json`, 0285 approves the alias codes and 0327
approves what the seed left proposed. The table decides a field's codes only
while that field has no approved, concept-resolved mapping -- on a fresh
install before `load_athena_vocabularies`, a seeded row whose concept is not
yet loaded carries none -- and a test database built without migrations; `tests/conftest.py`'s
`lab_field_mappings` fixture seeds the rows for a test that must derive through
the tables.

A seeded row resolves its concept when the vocabulary arrives. On a fresh
instance the migrations run before the full vocabulary is loaded, so 0240
saves each row whose concept is not loaded yet with `concept = NULL` and only
its `(vocabulary_id, concept_code)` text (`FieldConceptMapping.concept` is a
db-constrained FK, so nothing else is possible). A bootstrapped production
instance already holds the bootstrap's LOINC subset when 0240 runs, so those
rows resolve at seed time; the SNOMED, NAACCR and similar rows, and any LOINC
code outside the subset, stay NULL until the load. `resolve_lab_code_fields` requires a resolved concept, so the
instance would stay on the extractor table, the alias join would find
nothing, and the editor and `copy_reference_data` would see every seeded row
unresolved. `load_athena_vocabularies` therefore ends every successful
non-dry-run load, `--concepts-only` included, by running
`resolve_field_concept_mappings` in the
[field concept resolution service](../omop_core/services/field_concept_resolution.py):
every mapping with no concept is pointed at the Concept its pair names, in
batches, and the load log reports how many rows resolved and which are still
unresolved (a code outside the load scope, or a HealthKey concept this
instance has not minted). The pair is the row's own text, or, for a row
whose text is blank, the pair the fixture resolved that field to (the three
SCT rows are keyed by `source_value` and carry no code). Each row is saved
as the editor would save it, so the post-save projection backfills a user
edit that was pending on the field; only the FK changes, and text, status,
provenance and `updated_at` are untouched. A row that already points at a
concept is never a candidate, concept 0 included (the "No matching concept"
placeholder the genomics seeds park a field on), so "0 still unresolved"
means every candidate's code was found, not that every field has a real
concept; a second run changes nothing. Each row is locked, re-checked (still
no concept, still the text its pair came from) and saved in an `atomic` block
of its own, in pk order. How long the lock lasts depends on the caller. In
`manage.py resolve_field_concept_mappings`, which holds no transaction, every
block commits and releases its lock before the next, so a curator's concurrent
save waits for one row and a failure keeps the rows already done. In the
loader hook (and under `sync_athena_vocabulary`, the sync's whole load) the
blocks are savepoints: the locks are held until the hook's or the sync's
transaction ends, a curator's save of a row already processed waits for the
rest of the run, and a failure discards the whole pass. The step is best effort: it runs in a savepoint and a failure is
logged with the manual fallback and does not fail the load, because under
`sync_athena_vocabulary` the whole load is one transaction and the hook runs
inside it. The migration bootstrap (`prepare_production_database`) loads at
the 0200 schema baseline, before the curation tables are seeded, and does
not run it. A scheduled sync whose archive was already applied does not
run the loader at all, and one whose archive is new but adds no rows runs it
and rolls it back with the rest, so resolve by hand after such a sync. An
instance that loaded its vocabulary before this hook existed, or seeded or
copied mappings after the load, runs `manage.py resolve_field_concept_mappings`
once (`--dry-run` reports and writes nothing; `--fail-on-unresolved` exits 1
while any mapping that names a concept is still unresolved, so `--dry-run
--fail-on-unresolved` is the check a deploy or cron can run). A failed hook
leaves only a log line, so that check is how an operator finds it. A row whose pair names a concept Athena has since retired
still resolves to it, as `apply_payload` would, and the log names it for
re-curation. `SourceCodeConceptMapping` needs no such pass: its concept FKs
are unconstrained, so the seed stores the id before the Concept exists.

Resolving the FK switches a mapping on. While `concept` is NULL an approved
row is inert: the editor does not offer the field, the curated reader keys on
`(concept_id, source_value)` and never matches, and the post-save projection
returns early. After the pass the roughly 160 approved rows take part in
editing, derivation and the pending-edit projection from the next refresh of
each record. On a fresh instance that changes nothing visible, there being no
patients. On a populated one, records derived earlier were derived without
them; `DERIVATION_VERSION` does not move, because no derivation logic changed,
so `backfill_patient_records` would skip them as current. The command and the
loader log therefore tell the operator to run `backfill_patient_records
--all` whenever a pass resolved anything.

## Scalar save, projection and refresh

Clinical and profile editors PATCH
`/api/v1/patient-records/{person_id}/` (the provider UI also uses the
`/api/patient-info/{person_id}/` alias). The
[serializer](../patient_portal/api/serializers.py) saves validated values and records
pending user edits. Direct saves recompute dependent values from PatientRecord;
they do not run a full OMOP refresh.

Supported built-in or approved curated recipes provide the descriptor's
projection. [project_single_value](../omop_core/services/omop_projection.py) matches
a non-erroneous fact by patient, concept, source key and **current local date**.
Repeated same-day saves reuse that row, including a matching imported row while
retaining its provenance. Earlier facts remain history. A date picker does not
automatically override this event date; structured writers own their dated events.

For scalar Measurement/Observation projection, numbers and booleans use
`value_as_number`; strings and dates use this application's `value_as_string`.
The generic writer clears `value_as_concept_id`. Field-level concept mapping
therefore does not establish coded-answer mapping. Units and fact type come from
the projection recipe; a unit or date companion is not itself a clinical question.

A clear nulls answer columns and records `PatientRecord:cleared` in the source
answer. Readback suppresses the cleared event and preceding facts with the same
concept/source key without deleting history. Occurrence tables cannot represent
negative/null answers this way; unsupported edits remain pending rather than
creating an affirmative diagnosis, procedure or drug administration.

Successful scalar projections acknowledge pending edits. Failed, unmapped and
unsupported projections preserve them. Full OMOP refresh reads supported curated
scalar values and restores pending user values when necessary. Mapping approval
backfills pending edits for that field through the same projection service; it
does not rewrite every historical derived value. See
[signals](../omop_core/signals.py), the
[projection service](../omop_core/services/omop_projection.py) and the
[PatientRecord service](../omop_core/services/patient_record_service.py).

## Structured and vocabulary boundaries

The [therapy architecture](therapy-reference-tables-architecture.md) owns
regimen/component/class catalogs and disease/round scope. Episode-linked intent,
outcome and discontinuation values retain their event context; a planned therapy
must not be treated as administered merely because its label maps to a drug.
[ADR 0002](adr/0002-omop-therapy-types.md) defines class matching and the
limitations of aggregate later-line projections.

The [Genomics architecture](genomics_architecture.md) owns Measurement finding
parents, linked Measurement/Observation components, source-preserving history,
state validation and the frozen catalog. It is implemented, not an unmerged
exception to the field-mapping architecture. Approved recipes govern supported
parents/components; the generic answer-mapping proposal does not replace this
structured writer.

[ADR 0001](adr/0001-vocabulary-source-of-truth.md) separates vocabulary release,
reviewed mapping revision and source catalog/recipe version. Curation uses the
installed concept/domain and preserves portable vocabulary/code plus source
identity. A proposed or even previously approved row in an inventory export is
not a fresh clinical validation of its meaning.

## Curation transfer and fixture seeding

Curation data (mappings, choices, formulas, synonyms, custom fields) can reach a
new instance through three paths:

1. **`copy_curation`** — live database-to-database transfer from a running source
   instance. See [CLAUDE.md](../CLAUDE.md) for usage. Requires `SOURCE_DATABASE_URL`.

2. **`dump_field_curation` / `load_field_curation`** — file-based export and import.
   `dump_field_curation` serialises the curation tables to JSON with a metadata
   envelope (`schema_version`, `exported_at`, `source`). `load_field_curation`
   reads the JSON and applies it via `apply_payload`. Both reuse the proven
   `read_payload`/`apply_payload` from
   [`field_curation_transfer.py`](../omop_core/services/field_curation_transfer.py).
   Supports `--dry-run`, `--prune` and `--tables` (same interface as `copy_curation`).
   Default tables: mappings, custom_fields, choices, formulas, synonyms (no
   code_mappings).

3. **Checked-in fixture** — [`omop_core/data/field_curation_v1.json`](../omop_core/data/field_curation_v1.json)
   contains 314 mappings, 262 choices, 12 formulas and 1 synonym exported from
   staging after the provenance backfill. Migration 0240 loads this fixture
   automatically on `migrate`, so a fresh deployment gets the full curated
   inventory without manual steps. The migration is additive: it filters out rows
   that already exist by natural key, so curator-approved mappings are never
   overwritten. It requires Athena vocabularies to be loaded first for concept
   re-resolution; on a database without vocabularies, mappings land with
   `concept = None` and a warning.

All three paths use natural-key matching, re-resolve concept FKs by
`(vocabulary_id, concept_code)`, and clear user FKs (reviewer, created_by) since
user IDs are instance-specific. Row IDs are never copied.

## Verification and historical evidence

Use the [inventory generator](../omop_core/management/commands/export_field_mapping_inventory.py)
and its documented read-only export to inspect field/value coverage. Runtime
sources above establish behavior; the [plan](field_concept_mapping_plan.md)
records what remains unresolved. These references do not certify a deployment's
vocabulary or migration state.

The former September 11 manual audit, duplicated issue tables and staging receipt
remain in [repository history](https://github.com/healthkey-ai/promop/blob/4302c975dfd0098209c2b0a53e7b06548041cd5b/field_to_concept_mapping.md).
The [initial approval artifact](patient_field_mapping_initial_approvals.json)
retains its original audit notes and filename references as historical evidence;
it is not rewritten by documentation renames or used as current approval status.
