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
