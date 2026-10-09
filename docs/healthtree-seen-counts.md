# HealthTree Seen-count import

`import_healthtree_seen_counts` loads the `occurrences` column from a HealthTree
code-frequency CSV into existing source mappings' `occurrence_count` (Seen).
It does not use `resources` and does not create destination mappings.

```sh
python manage.py import_healthtree_seen_counts '/path/to/curehub-codes.csv' --dry-run
python manage.py import_healthtree_seen_counts '/path/to/curehub-codes.csv' --report /tmp/seen-count-report.json
```

For staging, load `DATABASE_URL` from `STAGING_DATABASE_URL` in the local `.env`.
Keep the supplied CSV outside the repository; the import ignores its free-text
columns and emits only aggregate counts.

The entire CSV is validated before writes. Counts must be nonnegative integers
within the database field's range. Duplicate raw vocabulary/code records are
rejected. Surrounding whitespace is stripped, and distinct whitespace variants
are summed within the snapshot. Each run sets the resulting frequency rather
than adding it to the previous value, so reruns are idempotent.

Equivalent standard FHIR URI/OID identifiers are normalized and their occurrence
frequencies summed within the snapshot. For example, ICD10CM and its OID contribute
to the same ICD-10-CM code count. Exact standard vocabulary/code matches then take
priority; the ICD10CM ↔ ICD10 tab grouping supplies an unambiguous fallback. The
export's `(no system)` marker identifies uncoded sources. ICD10 and ICD10CM remain
distinct frequency groups when both have their own counts.
It does not perform fuzzy code matching or collapse arbitrary custom code systems.
Multiple possible alias records without an exact match are reported and left
unchanged.

Only `occurrence_count` is updated. Destinations, candidate alternatives, review
state, notes, and other metadata remain unchanged. Mappings absent from this
snapshot retain their existing counts; unmatched CSV codes are reported without
creating mappings. The report distinguishes exact/alias matches, changed/unchanged
mappings, ambiguous mappings, and unmatched input codes.
