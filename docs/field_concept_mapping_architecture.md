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
| Computed values, such as BMI and wearable aggregates | Derived from inputs or source events; no independent scalar assertion |
| Entered treatment courses and supportive therapy | Dedicated clinical resource writers; Episode/Event and drug facts supply summaries |
| `genetic_mutations` and priority Genomics findings | Canonical Genomics writer and approved parent/component mappings |
| `cytogenetic_markers` | Read-only legacy summary; discrete interactive findings belong to Genomics |
| Language skills, allergies, surveys and the OMOP browser | Dedicated resources with their own contracts |
| Dynamic fields | `PatientRecord.custom_fields` and their definitions/recipes; computed custom fields use formulas |

The descriptor includes structured Genomics cases as well as ordinary direct
fields. Do not infer that every writable field uses the generic scalar writer,
or that the presence of an enum supplies a coded-answer projection.

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
