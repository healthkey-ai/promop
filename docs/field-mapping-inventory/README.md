# Field and value mapping inventory (#1223)

This is the first reproducible inventory slice of
[`field_concept_mapping_plan.md`](../field_concept_mapping_plan.md).
The [coverage report](coverage.md) summarizes the [manifest](manifest.json).
**#1223 remains open:** remaining CancerBot source/provider reconciliation, dynamic bindings,
retirement history and semantic reconciliation prevent claiming full coverage.
Do not start dependent schema work on the assumption that this export completes
the inventory prerequisite.

## Regenerate

Use the existing Python environment and install the frontend's locked dependencies
with `npm ci` in `frontend/`. Configure `DATABASE_URL` separately for the intended
reference database. Render staging uses `STAGING_DATABASE_URL` from the local
environment, as documented in [Render staging](../render-staging-celery.md).
Never put connection strings in the artifacts or command arguments.

```sh
python manage.py export_field_mapping_inventory \
  --cancerbot-root /path/to/cancerbot \
  --search \
  --output docs/field-mapping-inventory/manifest.json \
  --report docs/field-mapping-inventory/coverage.md
```

The command starts a PostgreSQL `REPEATABLE READ, READ ONLY` transaction and sets
a 45-second per-statement timeout. It queries only the explicitly selected
reference models, concepts, synonyms, relationships and release metadata.
PatientRecord is inspected as **model metadata**, never as database rows.
Reviewer/creator IDs and review/release notes are omitted from the reference
column allowlists. Database failures abort before either output is replaced.

Frontend extraction uses the installed TypeScript parser, without executing app
code. CancerBot extraction uses Python AST, without importing CancerBot. Both
record unsupported expressions as coverage gaps. Static fallback values are
retained separately from runtime catalogs; shared labels are not merged.
Only a direct literal-list reference/call, optionally wrapped in
`to_value_and_label`, counts as a complete static binding. Passing a literal list
through a filter, conditional, merge or other transformation remains unresolved.

For machines without frontend dependencies in the backend checkout, run
`node scripts/inventory-frontend-options.cjs /path/to/promop` in a checkout with
dependencies, save its JSON output, and pass `--frontend-export /path/to/file.json`.
Use the same source revision; the export contains file hashes.

## Manifest contract (version 1)

Each `rows` entry represents a source field, reference option, or static option
fragment. `kind` distinguishes fields from values. Reconciliation preserves
source rows; totals therefore count source occurrences, **not unique clinical
meanings**. Constants and their rendered controls are separate evidence.

- `id`: hash of source, list, typed source key, context and row kind. Labels are
  excluded. Existing FieldChoice IDs remain local to the exported database;
  their limitation is recorded, not presented as portable semantic identities.
- `canonical_value`: explicit type and value; boolean, number, string, blank and
  null remain distinct. Catalog codes are retained as source canonical values;
  compatibility with the destination's display-text API is unreviewed.
- `destination_path`, `scope`, `aliases`, `mapping_role`: proposed context and
  role. Null destination/context means unresolved, not globally applicable.
  `destination_candidates` records existing value-vocabulary references without
  choosing one. Unknown retirement/reviewer metadata remains null.
- `existing_mappings`: existing status, vocabulary/code, FK and write metadata;
  unchanged by export. `validation_flags` identifies FK/code, domain/table,
  screening and read-recipe disagreements for review.
- `candidate_ids`: references into `candidates`; every candidate carries dates,
  domain, code, vocabulary, mechanical checks and explicit non-approval.
  `maps_to` retains all outgoing relationships and their validity, including
  invalid ones as evidence. Relationship targets are also in `candidates`.
- `search_evidence`: existing exact code/FK resolution and, with `--search`,
  case-sensitive exact name/synonym matches. At most 25 lexical matches per
  label are attached; total matches and truncation are recorded. This is not
  exhaustive semantic search. Zero results never creates `no_equivalent`.
- `disposition`, `reason`, `owning_issue`: explicit action for every row.
  `needs_review`, `ambiguous`, `no_equivalent`, `not_applicable`,
  `requires_structured_representation`, and `verified_mapping` are separate
  states. Export never promotes candidates to verified or reviewed mappings.

`reference_tables` retains safe source records and therapy/gene relationships.
`source_revisions` records Git revisions, relevant source-file hashes and dirty
state. Release identities, full vocabulary metadata and version history are
recorded separately. The snapshot date controls validity checks.

## Live CancerBot reference export

All 172 public list bindings in `ValueOptions.get_all_options()` are enumerated,
including trial/admin lists. Of these, 25 complete literal lists are covered
directly by static source, and 34 therapy lists use the authoritative staging
catalogs. The user confirmed that the staging therapies, components, classes
and regimen/disease mappings were generated from CancerBot. The export includes
244 regimens, 187 components, 91 classes and 681 disease/round links. It records
picker membership through references to these existing rows; no duplicate
therapy export or new catalog is required.

Twenty planned-therapy lists have catalog coverage with picker context pending:
the staging disease/round link schema does not distinguish planned eligibility
or administration status. The remaining 93 public lists require source/provider
reconciliation. Database-driven lists within that remainder need live reference
coverage; no live CancerBot database connection was configured locally. Partial
literal fragments do not prove the contents of a dynamic list.

`--cancerbot-export` accepts this deliberately narrow envelope:

```json
{
  "schema_version": 1,
  "source_revision": "<CancerBot Git revision>",
  "exported_at": "2026-09-14T00:00:00Z",
  "options": {
    "boneLesions": {
      "options": [{"value": "", "label": "Unknown"}]
    }
  }
}
```

The example illustrates the format, not a complete bone-lesion catalog. Export
the reference option lists from the actual live source. Nested dictionaries of
option arrays retain parent keys, including gene-specific variants/origins.
Each option may contain only scalar `value` and string `label`. Unknown envelope
fields, unknown public list names and extra option fields fail validation.
Partial exports are accepted with missing lists explicitly reported. A supplied
export does not automatically make inventory coverage complete.
Imported list bindings become `covered_by_live_export`, with references to their
live rows and export metadata; provider totals and missing-list counts use those
same updated states. Empty live lists count as exported. Existing static/staging
evidence is preserved, and supplied planned lists leave the pending-picker list.

## SNOMED provenance finding

Repository commit `0335d89edc73ce98630ba5e8ad7503491cff9159` (2026-07-07)
introduced `_get_or_create_snomed_vocabulary()` in
`enrich_breast_cancer_omop_data.py`. Its default version was exactly
`SNOMED CT (synthetic, benchmark seed)`. The neighboring helper could create
concepts with `concept_id=int(concept_code)` and mark them standard.
Commit `bb8d5b099faae3d89c50d498d50c4eee01fb1db2` (2026-08-11) removed that
behavior. Reproduce the source investigation with:

```sh
git log --all --oneline -S 'synthetic, benchmark seed' -- omop_core
git show 0335d89:omop_core/management/commands/enrich_breast_cancer_omop_data.py
git show bb8d5b0 -- omop_core/management/commands/enrich_breast_cancer_omop_data.py
```

The staging snapshot still contains that SNOMED version string, and its
`vocabulary_version_history` records repeated `loaded` entries with that string
from August 18 through August 31. The latest published `vocabulary_release` is
ID 11, Athena `v5.0 29-AUG-26`; `vocab_release` is empty. These are distinct
mechanisms. CTCAE has no vocabulary row in this snapshot.

The old helper is a **possible source** of the metadata, not proof that a
particular staging load or concept came from that helper. Current model semantics
use `Concept.source=NULL` for external vocabulary loads, so the screen honors
that convention rather than requiring a nonexistent `Athena` source string.
Neither NULL source nor an external-range ID proves lineage. Synthetic vocabulary
metadata prevents a candidate from passing the provenance screen. Coordinate
#461/#623 to inspect load archives/manifests and repair provenance before bulk
clinical approval; this inventory changes no vocabulary or mapping rows.

## Remaining acceptance work

1. Reconcile the remaining CancerBot providers, including nested genetic option
   relationships. Reuse the exported staging therapy catalogs and disease/round
   links; resolve planned-picker context without inferring administration. Establish membership,
   aliases, retired values and replacements from seeds/migrations and live data.
2. Resolve dynamic frontend providers and every catalog's destination/context.
   Reconcile all #26 values and the #21 matrix against these source occurrences.
3. Review flagged recipe/domain/code conflicts and exact candidates, add broader
   semantic search evidence where needed, and assign final representation
   dispositions. Preserve unknown, ambiguous and no-equivalent cases.
4. Validate complete source and disposition totals before closing #1223 or
   treating #1224 as unblocked.
