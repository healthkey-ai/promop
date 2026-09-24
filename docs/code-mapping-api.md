# Code Mapping API

## For ingest developers

Use the Code Mapping lookup API whenever an importer needs to turn an incoming
source code into an OMOP concept. This applies equally to an importer running
in PRomop and one running in another service. Do not embed a local code map,
query Athena directly, or use a suggested target as a clinical destination.

Send each source-code encounter to:

```http
POST /api/v1/code-mappings/lookup/
Authorization: Bearer <service credential>
Content-Type: application/json
```

```json
{
  "codes": [
    {
      "source_vocabulary_id": "CPT4",
      "source_code": "99213",
      "source_text": "Office or other outpatient visit",
      "omop_table": "procedure"
    }
  ]
}
```

`source_vocabulary_id`, `source_code`, and `omop_table` are required for every
entry. `source_text` is optional evidence for suggestions. Supported tables are
`measurement`, `observation`, `condition`, `drug_exposure`, and `procedure`.

Treat `resolved` as the only permission to write the returned concept into an
OMOP clinical row:

```json
{
  "mappings": {
    "CPT4|99213": {
      "status": "approved",
      "resolved": true,
      "target_concept_id": 123,
      "target_concept_code": "99213",
      "target_concept_name": "Office visit",
      "destination_vocabulary_id": "CPT4",
      "mapping_id": 44
    }
  },
  "resolved": 1,
  "unresolved": 0
}
```

An unresolved result is a successful API operation, not an error. Store the
source value according to your normal unmapped-data policy and do not write the
`proposed_target_concept_id` as an OMOP destination. It is only a curator hint:

```json
{
  "status": "proposed",
  "resolved": false,
  "mapping_id": 45,
  "occurrence_count": 18,
  "proposed_target_concept_id": 456
}
```

The endpoint is an ingest encounter operation, not a cacheable reference GET:
responses include `Cache-Control: no-store`. Send one entry for each distinct
code encounter that should count toward review priority. Clients must avoid
retrying a completed request blindly, because a repeated proposed-code lookup
is another recorded encounter.

The Code Mapping UI list and curator CRUD endpoints are different APIs. Browsing
or searching those endpoints never changes `Seen`.

## What SCCM does under the hood

SCCM is the `source_code_concept_mapping` table. Its identity is the source
pair `(source_vocabulary_id, source_code)`, not an OMOP Concept. A row carries
the destination, status, provenance, `occurrence_count`, `first_seen`, and
`last_seen`.

The lookup lifecycle is:

1. Normalize the incoming source code and OMOP table.
2. Return an existing effective SCCM mapping. A curator-approved mapping wins
   over every automatic result.
3. If an existing row is `proposed`, return it as unresolved and atomically
   increment its `occurrence_count` and `last_seen`. Its target remains a
   suggestion, never an effective mapping.
4. On an SCCM miss, look up the code in loaded Athena vocabulary data. This is
   valid for every source vocabulary, including standard CPT4, LOINC, and
   SNOMED concepts. A successful direct match is materialized in SCCM as an
   effective `athena-direct` cache row, so future calls hit SCCM first. It never
   overwrites a pre-existing curator row.
5. If direct resolution fails, call
   `omop_core.mapping.suggestions.suggest_source_code`. That service
   owns all candidate retrieval and ranking strategies, including the
   multi-strategy UMLS/vector/lexical work. Its highest-ranked candidate becomes
   the target of a `proposed` SCCM row only.
6. If there is no suitable suggestion, mint an HK quarantine concept for the
   requested table (`HK-Labs`, `HK-Observation`, `HK-Condition`, `HK-Drug`, or
   `HK-Procedure`) and create a proposed row pointing to it. The result is
   still unresolved until a curator approves it.

The `Seen` value is therefore “times this unresolved proposal was encountered
by the resolver.” It is stored on the SCCM row, not on the source concept or
the destination concept. Approved and Athena-direct cache hits do not increase
it.

Curators approve, edit, reject, or replace proposed mappings in the Code
Mapping UI. Approval makes the row effective and can re-point already stored
clinical rows; that governed decision is why importers must never promote a
proposal themselves.

### SNOMED source identifiers

The FHIR OID `urn:oid:2.16.840.1.113883.6.96` and `SNOMED` identify the same
code system. Crossmap imports and new curator mappings store `SNOMED`; lookup
accepts either spelling and preserves the caller's spelling in response keys.
Source codes and destination concept IDs are not changed by this normalization.

Migration `0260_normalize_snomed_oid_mappings` merges redundant imported OID
rows into approved canonical mappings and normalizes alias-only rows. Only
untouched HT-FHIR proposals with a matching, active standard SNOMED destination
and consistent domain/table are automatically approved. Curator decisions,
machine suggestions, invalid destinations and conflicting duplicates require
review. A retained conflicting OID row continues to govern lookups using that
OID, rather than silently falling through to the canonical row.

Run `python manage.py audit_snomed_oid_mappings` for a read-only JSON preview.
Duplicate merges retain the larger encounter count, earliest first-seen and
latest last-seen timestamps, move candidate/review dependencies, and append the
removed row's metadata to the survivor's notes. The migration does not rewrite
clinical facts. It is irreversible: recovery requires a backup or manual
reconciliation using those snapshots, accounting for subsequent curation.

### Standard SNOMED identities and imported drug crossmaps

A SNOMED code that is already an active OMOP standard concept does not need
an automatic SNOMED-to-RxNorm translation. Crossmap imports prefer its exact
SNOMED identity and its actual domain over the external crossmap destination.
Existing mappings are not overwritten by re-imports.

Migration `0261_prefer_standard_snomed_identities` corrects untouched imported
proposals whose selected RxNorm destination is missing, nonstandard or invalid.
It approves the matching active standard SNOMED concept, corrects domain/table
metadata, retains imported candidates and encounter counts, and snapshots the
old mapping in notes. Approved/rejected mappings, human edits, machine-suggestion
decisions, locks and conflicting non-RxNorm candidates are protected. The
resolver applies the same eligibility rules when it encounters a remaining
crossmap proposal. Preview with `python manage.py audit_snomed_crossmap_identities`.

Standard status belongs to a concept, not to the SNOMED vocabulary as a whole.
Nonstandard or inactive SNOMED sources are not automatically self-mapped.
An automatic Observation identity is usable for an Observation request, not a
Drug request. Device identities remain approved reference mappings, but have
no supported clinical table in this application and cannot resolve as drug
exposures. Incompatible requests return `resolved: false`, the actual mapping
domain/table, and `unresolved_reason: "destination_domain_or_validity_mismatch"`.
Clinical facts are not moved between tables by either migration.

### Why curation lives in SCCM

SCCM can represent uncoded source text without requiring a source Concept, and
stores review state, provenance, and encounter counts independently of Athena
vocabulary releases. `ConceptRelationship` remains the vocabulary graph; it
cannot substitute for a source-code curation record. See the
[architecture record](superpowers/plans/2026-08-30-code-mapping-direction.md) for
source/destination design rationale and [semantic retrieval](semantic-retrieval.md)
for the maintained suggestion pipeline and embedding setup.

### Live Suggest candidates

The main mapping page requests `include_activity: true` when posting to
`/api/v1/code-mappings/suggest/`, then polls
`/api/v1/code-mappings/suggest-runs/<run_id>/?include_activity=1` once per second.
The opt-in POST response includes activity for inline runs that finish before
the response is sent. Queued runs expose activity while the worker runs.

For each source mapping, `candidates` events contain the `mapping_id`, source
identifiers, `strategy` (`umls`, `lexical`, or `semantic`), and all candidates
returned by that retrieval stage. Semantic candidates include `vector_distance`
(cosine distance; lower is closer). Empty candidate lists record completed
searches with no matches. Enabled searches continue after a unique UMLS match;
the unique curated match retains winner precedence. `ranked` events include the
winner and full ranking pool, including additional query-expansion candidates.
`result` events indicate whether the destination was saved.

After the run finishes, choosing another candidate patches the existing mapping
with `destination_concept_id` and `status: "proposed"`. The mapping still awaits
review. Its original suggested target remains available for accuracy tracking;
approving an alternative records an overridden suggestion. Candidate selection
is disabled while the run is writing and for preview-only results.

### Individual mapping preview

The mapping dialog posts to `/api/v1/code-mappings/suggest-one/` with `async: true`
to obtain a preview run ID and initial activity. It polls the existing run URL
with `include_activity=1` and displays candidate stages immediately below Suggest,
above the destination search box. Clicking a candidate fills the unsaved form
while remaining searches continue. The final winner only fills the form if the
curator has not already chosen or entered a destination. Closing the dialog or
changing the source invalidates its outstanding responses.

Preview runs do not write mappings, review outcomes, or suggestion provenance.
The normal mapping submit action saves the chosen destination. Preview runs are
excluded from the latest batch-run link. Requests without `async: true` retain
the synchronous response, which now also includes the complete candidate pool.
As with batch runs, live polling requires the queued dispatcher; inline execution
returns all completed stages in the initial response.

### Repair nonstandard SNOMED crossmap destinations from local relationships

Migration `0262_reconcile_snomed_local_relationships` follows the identity repair.
It starts with untouched imported SNOMED proposals whose RxNorm destination is
missing or nonstandard, locates the source in `concept`, and reads its current
outgoing `Maps to` rows from `concept_relationship`. It uses each environment's
installed tables: there is no bundled mapping data, network lookup, export, or
instance-specific ID list. Counts therefore depend on local vocabulary coverage.

A single active standard destination becomes approved. Multiple active standard
destinations replace the old candidates and remain proposed with no selected
target, for curator review. No valid destination leaves the row unchanged. Only
direct SNOMED relationships are used; indirect RxNorm recovery is deferred.
Retired source codes can be resolved through their still-current relationships.
Missing relationship targets are reported as incomplete evidence rather than
approving from an incomplete target set. Destinations in deprecated vocabularies,
locally authored concepts, and invalid/nonstandard destinations are excluded.

Approval uses the destination's actual domain. Domains without clinical storage
support (including Device) retain a blank table and cannot resolve as Drug.
The resolver also leaves the multiple-destination queue for curator selection.
Reimporting the HealthTree artifact preserves the repaired candidate set.

Approved/rejected mappings, human attribution, locks, suggestion decisions and
archived suggestion reviews are protected. The migration rechecks row eligibility
under locks in bounded transactions. It preserves encounter counts, descriptions,
source provenance and review history; original mapping/candidate definitions and
relationship evidence are recorded in notes before replacement. Only SCCM and
its destination candidates are written; concepts, relationships and clinical
facts are unchanged. Repeated execution does not modify repaired rows. This is
irreversible automatically: use the audit snapshots or a backup for manual
recovery rather than overwriting later curation.

Preview without writes or locks:

```sh
python manage.py audit_snomed_relationship_mappings
```
