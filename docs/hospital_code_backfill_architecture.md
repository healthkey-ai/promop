# Hospital source-code backfill architecture

The Code Mapping Epic and Cerner tabs are data-driven. They appear when
`SourceCodeConceptMapping` contains an exact hospital-local source system. The
browse API rolls those rows up to vendor tabs; resolution continues to use the
full Epic OID or Cerner tenant URI so opaque codes from different tenants never
share a resolver key.

## Import contract

`import_hospital_source_codes` imports an internal HealthTree unmapped-code
snapshot. The source artifacts are internal and are never committed: they
contain provider-channel labels and sample Firestore document addresses.

```shell
python manage.py import_hospital_source_codes \
  --codes-file /secure/path/unmapped_source_codes_v2.parquet \
  --units-file /secure/path/unmapped_source_code_value_distribution_v2.parquet \
  --supplemental-csv /secure/path/hospital_codes_with_units.csv \
  --expected-keys 690569
```

The invoking environment needs `pyarrow`; it is intentionally not an
application runtime dependency. `--dry-run` performs parsing, reconciliation,
and an existing-row scan without writing. `--expected-keys` stops before any
write when the artifact does not reconcile to the reviewed inventory size.

The importer:

- accepts only systems positively recognized as Epic or Cerner;
- retains the exact system URI and truncates codes exactly as runtime lookup
  does;
- collapses repeated resource/path rows to the SCCM natural key;
- treats v2 as primary and adds only keys absent from the supplemental CSV;
- takes the maximum existing/imported occurrence count instead of adding
  overlapping snapshots;
- never changes an existing curator label, status, destination, or domain; and
- is idempotent.

The v2 code extract also carries curation evidence. The importer retains its
record/patient/coding summary, category and value-type mix, reference-range
coverage, and median reference-range bounds in `source_metadata`. It retains
the extract's `label_group_records_if_mapped` separately: several Epic sibling
codings can coexist on one Observation, so their individually correct Seen
counts must not be added together in grouped review. The supplied deduplicated
label count is used when present; ordinary mappings without it keep the summed
Seen fallback.

## Units

The v2 `(code, unit)` distribution is stored as raw `source_unit_evidence` on
the mapping. Display and sender-supplied UCUM code remain separate. Counts,
patient/value totals, suppression state, and the supplied min/p5/p25/p50/p75/
p95/max distribution are retained; suppressed cohorts never acquire invented
quantiles. Approval checks normalize units at read time and take the larger
count per unit across imported and live `Measurement` evidence, avoiding
double-counting records that occur in both datasets.

The older CSV is useful for code coverage but its unit rows are a label-level
join repeated for each code, not direct per-code observations. It therefore
supplements missing code keys but does not supply unit evidence.

## Organization scope

The extracts identify vendor tenant systems and broad ingestion channels, not
trustworthy hospital `Organization` records. Backfilled rows remain global
(`organization_id = NULL`) while retaining their exact tenant URI. When ETL
supplies an actual organization identity, it can create organization-scoped
rows without inventing hospital names from `OPEN_EPIC`, `CERNER`, or
`HEALTHEX` channel labels.

The mapping browse response stays compact. `GET /api/v1/code-mappings/{id}/`
adds the structured source evidence only when the curator opens Edit Mapping.
The dialog labels median reference bounds as recognition evidence, not an
authoritative clinical range, and displays a null organization as
`Global / unattributed`.
