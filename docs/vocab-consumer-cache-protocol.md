# Vocabulary Consumer Cache Protocol

How consumer apps (EXACT, SoC, ht-phr, FHIR_Importers) keep their local
vocabulary mirrors in sync with promop's canonical OMOP vocabulary tables.

## Overview

promop publishes vocabulary data through a release-based model:

1. A **VocabularyRelease** record is created each time vocabulary tables are
   loaded or updated (via the `load_athena_vocabularies` management command).
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
| **304 Not Modified** | Your cached ETag matches the latest manifest | Stop. Nothing to do. |
| **200 OK** | New release available. Response body is the release manifest. | Keep the new ETag pending; proceed to Step 2. |
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
    "concept": {
      "algorithm": "sha256",
      "canonicalization": "promop-vocab-ndjson-v1",
      "digest": "<64 lowercase hexadecimal characters>",
      "count": 5200000
    },
    "vocabulary": {
      "algorithm": "sha256",
      "canonicalization": "promop-vocab-ndjson-v1",
      "digest": "<64 lowercase hexadecimal characters>",
      "count": 82
    }
  },
  "status": "published",
  "published_at": "2026-07-28T12:00:00+00:00",
  "notes": "Monthly Athena refresh + HealthKey custom concepts"
}
```

**Key fields for consumers:**

- `id` — the release these snapshots reflect (snapshots are latest-only; see Step 2)
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

An explicit release id is also accepted, but **only if it is the latest
published release** — the race-safe spelling of the line above:

```
GET /api/v1/vocab-releases/<release_id>/snapshot/<table>/
```

> **Snapshots serve the latest release only.** The vocabulary tables are reloaded
> wholesale on each release (they are current-only; historical *row-data* is not
> retained), so a snapshot can only truthfully represent the latest published
> release. Requesting a **non-latest** release returns **`409 Conflict`** — the
> body names the current latest and points back to `/latest/` — rather than
> silently streaming current rows under a stale label. Historical *metadata*
> (manifests, checksums, per-vocabulary versions) stays available via the manifest
> API (`GET /api/v1/vocab-releases/<id>/`); only bulk row-level snapshots are
> latest-only.
>
> Every snapshot response (both `200` and `304`) carries an **`X-Vocab-Release-Id`**
> header naming the release the rows reflect — capture the release id from there
> rather than parsing the `ETag` or the `Content-Disposition` filename.
>
> Consumers resolve the latest manifest and stream by its `id` immediately (see the
> sync example below). If a new release publishes mid-sync, the in-flight id is no
> longer latest and its snapshot returns `409` — treat that as "re-resolve `/latest`
> and retry," never as a fatal error.

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
{"vocabulary_id":"RxNorm","vocabulary_name":"RxNorm","vocabulary_reference":"https://www.nlm.nih.gov/research/umls/rxnorm/","vocabulary_version":"20260706","vocabulary_concept_id":44819104,"is_deprecated":false,"deprecated_date":null,"deprecated_reason":null}
{"vocabulary_id":"SNOMED","vocabulary_name":"Systematic Nomenclature of Medicine - Clinical Terms","vocabulary_reference":"http://www.snomed.org","vocabulary_version":"20260701","vocabulary_concept_id":44819096,"is_deprecated":false,"deprecated_date":null,"deprecated_reason":null}
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
`If-None-Match` to get a `304` if the release manifest has not changed:

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

3. **Verify content:** Hash each data line as specified below and compare the
   lowercase hexadecimal result to `checksums[table].digest`. A matching count
   alone does not detect changed values. Reject the download on either mismatch;
   do not commit a replacement mirror or advance its cached ETag.

### Pinned checksum contract: `promop-vocab-ndjson-v1`

The digest is **SHA-256 of the unfiltered snapshot's emitted data-line bytes**, in
stream order. It is not a hash of a consumer's database layout or a re-serialized
JSON object:

- Decode HTTP transfer/content encodings (for example, gzip), then hash the UTF-8
  NDJSON body. HTTP headers and chunk framing are excluded.
- Every data object is followed by exactly one LF byte (`0x0a`), including the
  final data row. Include those LF bytes. There is no BOM or blank separator.
- **Exclude the complete `__done` sentinel line**, including its LF. An empty table
  therefore has the SHA-256 of zero bytes:
  `e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855`.
- Rows use the fixed column projections in
  [`TABLE_COLUMNS`](../omop_core/services/vocab_snapshot.py), in their listed key
  order, serialized by PostgreSQL `row_to_json(record)::text` (not `jsonb`). No
  driver JSON decoding or Python JSON re-encoding occurs. Nulls remain JSON null,
  strings retain Unicode and JSON escaping, dates use ISO formatting, and numeric
  formatting uses `extra_float_digits=3`. Hash the received bytes without sorting
  object keys, normalizing Unicode, changing whitespace, or round-tripping numbers.
- The projection includes the existing application extensions and exported `id`
  columns. Adding an unrelated database column does not add it to v1. A change to
  the byte contract requires a new canonicalization identifier and publication.

Rows are ordered ascending by the following unique keys. Text keys explicitly
use PostgreSQL `COLLATE "C"` (byte ordering), independent of database locale.

| Table | Ordering key |
|-------|--------------|
| `concept` | `concept_id` (integer) |
| `concept_ancestor` | `id` (integer) |
| `concept_class` | `concept_class_id` (text, C collation) |
| `concept_relationship` | `id` (integer) |
| `concept_synonym` | `id` (integer) |
| `domain` | `domain_id` (text, C collation) |
| `drug_strength` | `id` (integer) |
| `relationship` | `relationship_id` (text, C collation) |
| `source_to_concept_map` | `id` (integer) |
| `vocabulary` | `vocabulary_id` (text, C collation) |

Snapshot responses identify the codec in `X-Vocab-Checksum-Format`. The manifest's
`checksums[table].count` and `row_counts[table]` come from the same ordered scan as
its digest. Publication happens after post-load mapping updates; a checksum scan
failure aborts publication instead of falling back to a count-only fingerprint.
Hashing scans each published table once with a bounded server-side cursor.

**Scope:** The manifest digest covers the whole table. Do not compare a
`?source=HealthKey` or `?source=external` subset to it. A concepts-only load retains
its existing manifest scope: it records only the tables processed by that load.
A table absent from `checksums` has no published digest to verify.

**Existing releases:** Old manifests may contain `count`/`min_ctid`/`max_ctid`
objects (or no checksum). They are not content hashes and are not rewritten.
Consumers requiring byte verification must reject missing/unknown algorithms or
canonicalizations and obtain a newly published release. Never silently downgrade
to count-only verification.

**Live-table drift:** Snapshots still serve current tables, not retained historical
row data. A write after publication can change bytes without changing the count.
A digest mismatch detects this too: discard the download and obtain a consistent
newly published release; do not accept the changed bytes under the old manifest.

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
    # release.id is the latest we just resolved; a 409 here means a newer release
    # published mid-sync — restart from the /latest poll rather than failing.
    stream = GET /api/v1/vocab-releases/{release.id}/snapshot/{table}/
    stage_download(stream)
    verify_sentinel_count_and_sha256(stream, release, table)

atomically_replace_mirror_from_verified_staging()
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
import hashlib
import json
import tempfile
import requests


def sync_table(base_url, token, release, table, local_cursor):
    expected = release["checksums"][table]
    if (expected.get("algorithm") != "sha256" or
            expected.get("canonicalization") != "promop-vocab-ndjson-v1"):
        raise ValueError("Unsupported or missing content checksum; republish required")
    url = f"{base_url}/api/v1/vocab-releases/{release['id']}/snapshot/{table}/"
    with requests.get(url, headers={"Authorization": f"Bearer {token}"},
                      stream=True) as resp, tempfile.TemporaryFile() as staged:
        resp.raise_for_status()  # Re-resolve /latest on 409.
        digest = hashlib.sha256()
        rows = 0
        done = False
        # requests decompresses the body; iter_lines removes the LF. Reattach
        # that one byte, as required by v1, before hashing/staging each data row.
        for line in resp.iter_lines(decode_unicode=False):
            if done or not line:
                raise ValueError("Unexpected data after sentinel or blank line")
            obj = json.loads(line)
            if obj.get("__done") is True:
                if obj["rows"] != rows:
                    raise ValueError("Sentinel row count mismatch")
                done = True
                continue
            data_line = line + b"\n"
            digest.update(data_line)
            staged.write(data_line)
            rows += 1
        if (not done or rows != release["row_counts"][table] or
                rows != expected["count"] or digest.hexdigest() != expected["digest"]):
            raise ValueError("Incomplete or corrupted vocabulary snapshot")

        # Only now load the verified temporary file into a staging table, then
        # atomically replace the mirror. Use a fixed table allowlist and safely
        # quoted identifiers in the loader. Commit/cache the ETag only after all
        # requested tables have passed verification.
        staged.seek(0)
        load_verified_ndjson_into_staging(staged, table, local_cursor)

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
to the release. A release ETag identifies the manifest; it is not a fresh hash of live data.
Consumers verify downloaded bytes against that manifest to detect post-publication
writes as well as corruption.

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
