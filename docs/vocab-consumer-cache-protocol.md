# Vocabulary Consumer Cache Protocol

How consumer apps (EXACT, SoC, ht-phr, FHIR_Importers) keep their local
vocabulary mirrors in sync with promop's canonical OMOP vocabulary tables.

## Overview

promop publishes vocabulary data through a release-based model:

1. A **VocabularyRelease** record is created each time vocabulary tables are
   loaded or updated (via the `load_omop_vocab` management command).
2. The release transitions through `staged → published` (or `retired`).
3. Consumers poll the release API, compare ETags, and download table snapshots
   only when the vocabulary has changed.

This protocol ensures consumers never re-download unchanged data and can
verify download completeness.

---

## Base URL

All endpoints are under `/api/v1/` and require OAuth2 Bearer token
authentication (`ScopedTokenPermission`).

```
Authorization: Bearer <access_token>
```

---

## Step 1: Check for a New Release

**Poll the latest release endpoint.** This is the single check that tells you
whether your local mirror is current.

```
GET /api/v1/vocab-releases/latest/
If-None-Match: "<your-cached-etag>"
```

### Responses

| Status | Meaning | Action |
|--------|---------|--------|
| **304 Not Modified** | Your cached ETag matches — vocab hasn't changed | Stop. Nothing to do. |
| **200 OK** | New release available. Response body is the release manifest. | Save the new ETag, proceed to Step 2. |
| **404 Not Found** | No published releases exist yet. | Retry later. |

### Response body (200)

```json
{
  "id": 7,
  "schema_version": "5.4",
  "scope": ["SNOMED", "RxNorm", "LOINC", "HemOnc"],
  "build_timestamp": "2026-07-28T10:00:00+00:00",
  "athena_version": "v5.0 28-JUL-2026",
  "vocab_versions": {
    "SNOMED": "20260701",
    "RxNorm": "20260706"
  },
  "row_counts": {
    "concept": 5200000,
    "concept_relationship": 18000000,
    "vocabulary": 82
  },
  "checksums": {
    "concept": "sha256:abc123...",
    "vocabulary": "sha256:def456..."
  },
  "status": "published",
  "published_at": "2026-07-28T12:00:00+00:00",
  "notes": "Monthly Athena refresh + HealthKey custom concepts"
}
```

**Key fields for consumers:**

- `id` — use to construct snapshot URLs for a pinned release
- `row_counts` — expected row counts per table (for completeness verification)
- `checksums` — per-table SHA-256 hashes (for integrity verification)
- `published_at` — when this release was made available

### ETag format

ETags look like `"vr-7-a1b2c3d4e5f6"` (release PK + SHA-256 prefix).
Store this value and send it as `If-None-Match` on subsequent polls.

---

## Step 2: Download Table Snapshots

For each vocabulary table you need, stream the snapshot as newline-delimited
JSON (NDJSON):

```
GET /api/v1/vocab-releases/latest/snapshot/<table>/
```

Or pin to a specific release:

```
GET /api/v1/vocab-releases/<release_id>/snapshot/<table>/
```

### Available tables

| Table slug | OMOP CDM table | Description |
|------------|---------------|-------------|
| `concept` | concept | Core concept definitions |
| `concept_ancestor` | concept_ancestor | Ancestor-descendant hierarchy |
| `concept_class` | concept_class | Concept classification metadata |
| `concept_relationship` | concept_relationship | Pairwise concept relationships |
| `concept_synonym` | concept_synonym | Alternate names for concepts |
| `domain` | domain | High-level concept domains |
| `drug_strength` | drug_strength | Drug ingredient strengths |
| `relationship` | relationship | Relationship type definitions |
| `source_to_concept_map` | source_to_concept_map | Source-to-standard mappings |
| `vocabulary` | vocabulary | Vocabulary metadata |

### Response format

- **Content-Type:** `application/x-ndjson`
- **Content-Disposition:** `attachment; filename="<table>_<release_id>.ndjson"`
- Each line is a JSON object with keys matching the database column names
- The **last line** is a sentinel: `{"__done": true, "rows": <count>}`

Example (vocabulary table):

```
{"vocabulary_id":"SNOMED","vocabulary_name":"Systematic Nomenclature of Medicine - Clinical Terms","vocabulary_reference":"http://www.snomed.org","vocabulary_version":"20260701","vocabulary_concept_id":44819096}
{"vocabulary_id":"RxNorm","vocabulary_name":"RxNorm","vocabulary_reference":"https://www.nlm.nih.gov/research/umls/rxnorm/","vocabulary_version":"20260706","vocabulary_concept_id":44819104}
{"__done":true,"rows":2}
```

### Source filter (concept table only)

The concept table supports an optional `?source=` filter:

| Parameter | Rows returned |
|-----------|--------------|
| `?source=HealthKey` | Only locally-authored concepts (`source = 'HealthKey'`) |
| `?source=external` | Only Athena-loaded concepts (`source IS NULL`) |
| _(omitted)_ | All concepts |

The ETag varies by source filter, so each filtered vs. unfiltered request
is cached independently.

### ETag / conditional requests on snapshots

Snapshot responses include an ETag. On subsequent requests, send
`If-None-Match` to get a `304` if the data hasn't changed:

```
GET /api/v1/vocab-releases/latest/snapshot/concept/
If-None-Match: "vr-7-a1b2c3d4e5f6"
```

→ `304 Not Modified` (no body, fast)

---

## Step 3: Verify Download Completeness

After downloading a table snapshot, verify you received all rows:

1. **Check the sentinel line:** The last line of the NDJSON stream is
   `{"__done": true, "rows": N}`. If this line is missing, the download was
   truncated (network error, server restart, etc.). Retry the download.

2. **Check row count:** Compare the sentinel's `rows` value against the
   `row_counts` field from the release manifest (Step 1). They should match
   for unfiltered downloads.

3. **Check checksum (optional):** Compute a SHA-256 over the downloaded rows
   and compare against the `checksums` field from the release manifest.

---

## Recommended Polling Strategy

### Frequency

- **Production:** Poll `GET /api/v1/vocab-releases/latest/` every **6 hours**.
  Vocabulary updates are infrequent (typically monthly Athena refreshes +
  ad-hoc HealthKey concept additions).
- **Staging/dev:** Poll every **1 hour** or on-demand.

### Algorithm

```
stored_etag = load_from_local_storage()  # None on first run

response = GET /api/v1/vocab-releases/latest/
           headers: { If-None-Match: stored_etag }

if response.status == 304:
    log("Vocab unchanged, skipping sync")
    return

if response.status == 404:
    log("No published releases yet")
    return

release = response.json()
new_etag = response.headers["ETag"]

for table in TABLES_I_NEED:
    stream = GET /api/v1/vocab-releases/{release.id}/snapshot/{table}/
    write_to_local_db(stream)
    verify_sentinel(stream)

save_to_local_storage(new_etag)
log(f"Vocab synced to release {release.id}")
```

### Which tables to download

Not every consumer needs every table. Choose based on your use case:

| Consumer | Tables needed |
|----------|--------------|
| **EXACT** (eligibility) | concept, concept_relationship, concept_ancestor, vocabulary |
| **SoC** (standard-of-care) | concept, concept_relationship, vocabulary |
| **ht-phr** (patient portal) | concept, concept_synonym, vocabulary, domain, concept_class |
| **FHIR_Importers** | concept, concept_relationship, source_to_concept_map, vocabulary |

### Loading into a local database

The NDJSON rows have keys matching OMOP CDM column names. A typical load
pattern:

```python
import json
import requests

def sync_table(base_url, token, release_id, table, local_cursor):
    url = f"{base_url}/api/v1/vocab-releases/{release_id}/snapshot/{table}/"
    resp = requests.get(url, headers={"Authorization": f"Bearer {token}"}, stream=True)
    resp.raise_for_status()

    local_cursor.execute(f"TRUNCATE {table}")  # or use a staging table

    rows = 0
    for line in resp.iter_lines(decode_unicode=True):
        if not line:
            continue
        obj = json.loads(line)
        if obj.get("__done"):
            assert obj["rows"] == rows, f"Expected {obj['rows']} rows, got {rows}"
            break
        columns = ", ".join(obj.keys())
        placeholders = ", ".join(["%s"] * len(obj))
        local_cursor.execute(
            f"INSERT INTO {table} ({columns}) VALUES ({placeholders})",
            list(obj.values()),
        )
        rows += 1
```

For large tables (concept, concept_relationship), consider using PostgreSQL
`COPY` with a temporary file or `psycopg.copy` for better performance.

---

## Error Handling

| Status | Meaning | Consumer action |
|--------|---------|-----------------|
| **304** | Not Modified | Skip sync — your cache is current |
| **400** | Unknown table name | Fix the table slug in your request |
| **401** | Missing or invalid token | Refresh your OAuth2 token and retry |
| **404** | Release not found (or no published releases) | Retry later; the release may have been retired |
| **5xx** | Server error | Retry with exponential backoff (max 3 retries) |
| **Truncated stream** (no `__done` sentinel) | Network interruption | Retry the full download |

---

## Important Notes

### Release semantics

The `release_id` in the snapshot URL gates access (only published releases
are accessible) and provides the ETag for cache validation. The snapshot
data is the **current live table state**, not a point-in-time snapshot tied
to the release. In practice, vocabulary table contents only change when a
new release is published, so the ETag accurately signals data freshness.

### Cache-Control

Snapshot responses include `Cache-Control: private, max-age=86400` (24 hours).
Consumers should respect this — once you've downloaded a snapshot for a given
ETag, you don't need to re-download for 24 hours even without conditional
requests.

### Authentication

All endpoints require OAuth2 Bearer token authentication. Tokens are
issued via the standard OAuth2 client credentials flow. Contact the
promop team for client credentials.

### Concurrency

The snapshot endpoint uses PostgreSQL server-side cursors and streams rows
in batches of 1,000. Multiple concurrent consumers are supported, but each
open stream holds a database connection. Avoid polling more frequently than
recommended.
