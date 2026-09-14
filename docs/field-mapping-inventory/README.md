# Field and value mapping inventory (#1223)

This is the first reproducible inventory slice of
[`field_concept_mapping_plan.md`](../field_concept_mapping_plan.md).
The [coverage report](coverage.md) summarizes the [manifest](manifest.json).
**#1223 remains open:** destination semantics, missing fields, retirement history
and semantic reconciliation remain unfinished.
Do not start dependent schema work on the assumption that this export completes
the inventory prerequisite.

## Regenerate

The refreshed manifest contains **7,767 source occurrences**: 420 fields and
7,347 values. All 172 CancerBot public bindings have source accounting:
118 reference-backed lists, 51 deterministic static lists, and three trial-search
exclusions. There are zero public lists awaiting live reference access and zero
planned lists awaiting source eligibility. These counts are not clinical approval.

All source occurrences now have an implementation-routing disposition and owner;
the [coverage report](coverage.md#implementation-destination-accounting) separates
existing fields, managed catalogs, frontend/source consumers and retained
historical evidence. Eight CancerBot routes still need MCL destination fields.
Knowing a source's consumer does not resolve its clinical meaning or create that
runtime field. The plan's [1.3 release gates](../field_concept_mapping_plan.md#13-release-gates)
track inventory acceptance and the dependent implementation and rollout work.

[cancerbot-reference.json](cancerbot-reference.json) contains the 44 allowlisted
reference tables captured in one read-only transaction, reviewed provider source,
file hashes and source revision. [cancerbot-options.json](cancerbot-options.json)
contains 4,105 reconstructed option occurrences across 118 public lists. Source
code is parsed, never imported or executed. The pinned provider hash requires
explicit review when CancerBot changes. The recorded checkout revision is not
an assertion about CancerBot's deployed application revision.

Replay the checked-in reference snapshot without any database access:

```sh
python manage.py export_cancerbot_reference_options \
  --inventory docs/field-mapping-inventory/manifest.json \
  --cancerbot-root /path/to/cancerbot \
  --snapshot docs/field-mapping-inventory/cancerbot-reference.json \
  --output /tmp/cancerbot-options.json

python manage.py import_field_inventory_reference_options \
  --inventory docs/field-mapping-inventory/manifest.json \
  --cancerbot-export /tmp/cancerbot-options.json \
  --output /tmp/field-manifest.json \
  --report /tmp/field-coverage.md
```

The merge keeps reference/candidate evidence and source identities, handles
partial and empty exports, and retains removed options with
`source_presence=absent_from_latest_export`. Absence does not prove retirement.
A changed label is flagged for review. Stable identities retain reconciliation
metadata; per-binding export dates/revisions distinguish partial refreshes.
A changed source revision invalidates the reviewed routing contract until its
definitions are reviewed again. Replays preserve existing dispositions while
assigning new rows to the reviewed routes only when the source revision matches.

For a fresh CancerBot read, privately configure `CANCERBOT_DATABASE_URL`, omit
`--snapshot`, and add `--snapshot-output /tmp/cancerbot-reference.json`. No DSN
is accepted as a command argument. The command queries only named reference
columns; patient/trial data, reviewer identities and free-text notes are excluded.
Duplicate IDs/codes, missing columns and orphan non-null FKs abort the export.
Nullable source links remain explicit; they never create blank catalog records.
Provider membership and labels are reproduced; database-specific text collation
is not certified by Python ordering.

The older `reconcile_field_inventory_sources` command remains available for
pre-live-export snapshots. It deliberately refuses snapshots containing live
metadata; use reference import or the full exporter below for the current data.

Use the existing Python environment and install the frontend's locked dependencies
with `npm ci` in `frontend/`. Configure `DATABASE_URL` separately for the intended
reference database. Render staging uses `STAGING_DATABASE_URL` from the local
environment, as documented in [Render staging](../render-staging-celery.md).
Never put connection strings in the artifacts or command arguments.

```sh
python manage.py export_field_mapping_inventory \
  --cancerbot-root /path/to/cancerbot \
  --cancerbot-export docs/field-mapping-inventory/cancerbot-options.json \
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
Direct literal-list bindings and transformations fully resolved by the bounded
static interpreter count as complete source bindings. A literal fragment inside
an unsupported filter, conditional or transformation remains unresolved.

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
- `destination_route_keys`: references into `destination_crosswalk.bindings` by
  public option-list name. Source routing records disease, line and representation
  conflicts separately from immutable source scope; it does not approve aliases
  or replace canonical values. All 172 bindings have entries, including eight
  missing MCL destinations. Shared literal rows may have several routes.
- `implementation_contract`: source/consumer routing status, destinations,
  missing fields, implementation owners and unresolved representation context.
  This annotation leaves typed source identities and existing mapping records
  intact. It never constitutes semantic approval or proof of retirement.

`reference_tables` retains safe source records and therapy/gene relationships.
`source_revisions` records Git revisions, relevant source-file hashes and dirty
state. Release identities, full vocabulary metadata and version history are
recorded separately. The snapshot date controls validity checks.

`base_descriptors` captures the current backend's base writable descriptors and
211 options, with no patient-specific authorization or patient reads. Lookup
model names must belong to the exported reference-table allowlist. Custom-field
definitions are included without creator identity. The frontend parser retains
nested gene groups, criterion storage keys/labels and General-tab helper calls;
reviewed dynamic providers have pinned source hashes and remain unresolved on
drift. Descriptor options take precedence over frontend fallback values.

`source_history` indexes CancerBot migration definitions without executing them.
Field renames/removals and historical choices are source evidence, not value
equivalence. Reviewed catalog rules retain conditional i/s/t replacements,
component-only St. John's Wort deduplication and removal of NSAIDs as a regimen.
The manifest also retains 394 historical option occurrences: 309 pinned literal
seed entries, 69 declared schema choices and 16 catalog-scoped old-code rules.
These are additional source occurrences, not additions to the current catalogs.
Legacy gene/variant strings remain unapproved, and the source PALB1 spelling is
preserved. Each migration digest is stored as a separate `path`/`sha256` record.
All 98 data-migration operations have a recorded definition scope: 51 source-scope
reviews, 20 literal-seed operations, 21 no-ops and six catalog-rule operations.
`definition_coverage_complete` reports this bounded source review; the broader
history `complete` flag remains false. Pinned source hashes invalidate reviews
on drift. Delegated loader dependencies are fingerprinted without importing or
running them. No execution receipt, historical row membership or clinical alias
is inferred. In particular, the historical class-concept clearing operation is
superseded by the retained PRomop regimen/component/class architecture and must
not be replayed.

`priority_field_coverage` implements the per-disease audit in
[#1311](https://github.com/healthkey-ai/promop/issues/1311). All 42 effective
priority gene/marker fields have approved storage recipes across 51 disease
memberships. The current parent code resolves to Observation, so the established
Measurement-parent writer uses concept 0. The report distinguishes supported
source-only storage from compatible standard parent candidates and incomplete
recipes. It does not approve exact variants or gene answer concepts.

## Live CancerBot reference export

The live export covers all database-backed lists, including the 34 therapy
lists previously approximated from staging and the 20 separate planned-therapy
lists. Original staging catalogs and picker memberships remain independent
manifest evidence, while each public binding now references actual reconstructed
CancerBot source rows. See the [therapy import audit](therapy-import-audit.md)
for the verified catalog differences and existing import defects.

The capture includes 197 variants, six genes and eight gene/origin connections.
Nested parent keys and composite source codes remain intact. CancerBot's source
PALB1 naming is preserved as source evidence; current PRomop Genomics uses the
reviewed PALB2 naming correction. Do not silently rename source identities or
approve variants by label. The frozen Genomics catalog owns runtime findings.

CancerBot has 94 PlannedTherapy entries and 175 eligibility links, separately
from its 239 Therapy regimens. Planned source eligibility does not establish a
mapping to a PRomop regimen or administration status. The existing PRomop
therapy, component, class, disease/round tables and mapping APIs remain authoritative
for PRomop. This inventory never writes to them.

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

1. Reconcile each catalog's destination/context and recorded semantic conflicts.
   Reconcile all #26 values and the #21 matrix against the source occurrences.
2. Reconcile the recorded source aliases, retirement and replacement evidence.
   Missing live membership must remain distinct from a reviewed retirement.
3. Review recipe/domain/code conflicts, add semantic search evidence beyond exact
   lexical candidates, and assign final representation decisions and owners.
4. Resolve vocabulary provenance with #461/#623 before bulk approval. Reference
   availability and a standard flag do not establish clinical correctness.
5. Correct therapy import defects through existing managed catalogs (#1230),
   preserving curator decisions. Keep planned and administered sources distinct.

All associated issues remain open. The plan requires completing this inventory
before dependent choice schema work. Earlier feature work remains preserved on
`feat/field-value-concept-mappings` and must be reviewed against current dev.
