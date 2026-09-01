# ETL Code Mapping Integration Design

## Context

The ETL FHIR parser carries two static JSON files for cross-vocabulary concept
resolution:

- `cpt_to_snomed_map.json` (7,034 entries)
- `snomed_to_rxnorm_map.json` (42,127 entries)

These have been imported into promop's `SourceCodeConceptMapping` table via the
`import_etl_cross_maps` management command. This document proposes how the ETL
should transition from static files to querying promop's API.

## New Endpoint

```
POST /api/v1/code-mappings/lookup/
```

Accepts up to 1,000 `(source_vocabulary_id, source_code)` pairs and returns
approved mappings. See the view docstring for request/response format.

## Integration into CtomopApiOmopWriter

### Step 1: Add lookup method to CtomopClient

```python
# In ctomop_client.py
def lookup_approved_code_mappings(self, codes: list[dict]) -> dict:
    """Batch lookup approved code mappings from promop.

    Args:
        codes: List of dicts with 'source_vocabulary_id' and 'source_code'.

    Returns:
        Dict keyed by 'VOCAB|CODE' with mapping details or None.
    """
    resp = self.session.post(
        f'{self.base_url}/api/v1/code-mappings/lookup/',
        json={'codes': codes},
    )
    resp.raise_for_status()
    return resp.json()['mappings']
```

### Step 2: Per-run cache in CtomopApiOmopWriter

Follow the existing `_resolve_concepts` caching pattern:

```python
class CtomopApiOmopWriter:
    def __init__(self, ...):
        self._code_mapping_cache: dict[str, dict | None] = {}

    def _resolve_cross_map(self, vocab_id: str, code: str) -> int | None:
        key = f'{vocab_id}|{code}'
        if key not in self._code_mapping_cache:
            # Batch pending lookups periodically (e.g., every 500 codes)
            self._flush_code_mapping_batch()
        hit = self._code_mapping_cache.get(key)
        return hit['target_concept_id'] if hit else None
```

### Step 3: Batch pending lookups

Collect codes during a processing pass, then flush in batches of 1,000:

```python
def _flush_code_mapping_batch(self):
    if not self._pending_code_lookups:
        return
    batch = self._pending_code_lookups[:1000]
    self._pending_code_lookups = self._pending_code_lookups[1000:]
    result = self.client.lookup_approved_code_mappings(batch)
    self._code_mapping_cache.update(result)
```

## Migration Path

1. **Phase 1 (now):** Import static JSON into promop via `import_etl_cross_maps`.
   ETL continues using static files. Curators can review/update mappings in
   the Mapping Hub.

2. **Phase 2:** Add `lookup_approved_code_mappings()` to `CtomopClient`. ETL
   calls the API first, falls back to static JSON for any unresolved codes.
   This validates the API path in production without risk.

3. **Phase 3:** Remove static JSON fallback once the API path is proven
   reliable. The JSON files become seed data only (imported once, then
   maintained via the Mapping Hub UI).

## Performance Considerations

- The lookup endpoint uses a single database query with `Q` objects, so
  response time scales with DB index performance, not batch size.
- The `ix_sccm_source_code` index on `(source_vocabulary_id, source_code)`
  covers the lookup query.
- A full ETL run (~50K unique codes) would require ~50 API calls at 1,000
  codes per batch. At ~50ms per call, that's ~2.5 seconds total — negligible
  compared to the overall ETL runtime.
- The per-run cache ensures each unique code is looked up at most once per
  DAG execution.
