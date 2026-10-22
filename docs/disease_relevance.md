# Disease relevance for extraction services

Issue #1942. A document extractor such as CDEW resolves a source term to a
standard concept with `POST /api/v1/code-mappings/lookup/` (meaning). This page
covers the second layer: **which concepts matter for the patient's disease**
(relevance). The extractor should treat relevance as emphasis only and still
extract every other fact.

## The relevance set

```
GET /api/v1/relevance/?disease=MM[&disease=FL]
GET /api/v1/relevance/?person_id=123
```

```json
{
  "version": "1.2.3f9a0c1d2e4b",
  "diseases": ["MM"],
  "concepts": [
    {"concept_id": 3001234, "name": "Kappa light chains.free", "synonyms": ["..."],
     "vocabulary_id": "LOINC", "concept_code": "...", "domain_id": "Measurement",
     "omop_table": "measurement", "field_name": "kappa_flc",
     "tier": "disease", "diseases": ["MM"]}
  ],
  "unmapped_fields": {"MM": ["involved_uninvolved_ratio"]}
}
```

`disease` takes `BC`, `FL`, `MM`, `CLL` or `MCL`. Names are accepted too, for
example `multiple myeloma`. The parameter is repeatable, and the response is
the union of the sets.

A set is built from approved `FieldConceptMapping` rows only. It contains three
tiers, listed in this order:

| Tier | What |
|---|---|
| `disease` | The fields the disease tab shows (`DiseaseTab.tsx`), plus the genomics priority markers the catalog lists for the disease (#1180). |
| `curated` | Concepts a curator accepted from an absent-from-relevance signal (see below). |
| `common` | Clinical fields every supported cancer shares: general clinical, blood, chemistry and treatment. |

A field in the set that has no approved concept yet is listed in
`unmapped_fields`. This is how much curation is still to do (#1611, #1076), not
an error.

### Version and caching

`version` is `<catalog>.<genomics catalog>.<content hash>`. It changes whenever
anything a response could contain changes: an approval, a rejection, an
accepted signal, or an edit to the field lists. Pin it on every extraction run.
A run is stale when its pinned version differs from the current one.

The `ETag` is the version plus the diseases. Send it back as `If-None-Match`
and you get `304 Not Modified` while nothing has changed.

## Scopes

| Call | Service scope |
|---|---|
| `?disease=` | `system/*.read`. The set is reference data and holds no patient data. |
| `?person_id=` | `system/disease.read`. It reads only the patient's diseases, so the extractor does not need `patient/*.read`, which opens the whole record. `patient/*.read` is also accepted. |
| `POST relevance/signals/` | `system/etl.write`, which CDEW already holds. |

A credential that holds only `system/disease.read` can also read the set by
disease, but cannot reach any other endpoint.

A session or OAuth user can read `?person_id=` only for a patient they can
access.

## Patients with more than one cancer

`?person_id=` returns every supported cancer the record names:

- the primary cancer first (`PatientRecord.disease`);
- then any other diagnosis on the record that maps to a supported cancer.

The extractor gets the union of their sets, and each concept says which of the
patient's diseases it belongs to. This is the decision #1942 asked for. It does
not depend on per-cancer stage and markers (#1858), which concern how the
record is stored, not what is relevant.

If the patient has no supported cancer, `diseases` is `[]` and `concepts` is
`[]`. The extractor then extracts everything without emphasis.

## Absent-from-relevance signals

```
POST /api/v1/relevance/signals/
[{"disease": "MM", "concept_id": 4123456, "document_count": 31,
  "document_total": 100, "period": "2026-10"}]
→ 201 {"created": 1, "updated": 0}
```

A signal reports a concept that resolved cleanly but is not in the disease's
set, with the number of documents it appeared in. The counts are aggregate
only and carry no PHI.

- One object or an array, at most 1,000 per request.
- `document_total` is optional.
- Re-sending the same `(source, disease, concept, period)` updates the counts.
- An unknown concept or an unsupported disease is rejected with 400.

Curators work the queue at:

- `GET /api/v1/relevance/signals/?status=proposed&disease=MM`, for any
  professional role;
- `PATCH /api/v1/relevance/signals/<id>/ {"status": "accepted" | "rejected" | "proposed"}`,
  for staff and org admins, the same people who can approve field mappings.

A decision applies to the `(disease, concept)` pair. Every period's report of
that pair takes it, and so does any later report. Once rejected, a concept stays
rejected until a curator changes it. An accepted concept joins the disease's
set at the `curated` tier, and the version changes.

Signals are `system` data in `instance_data`: they count this instance's own
documents, so they are never copied to another instance.
