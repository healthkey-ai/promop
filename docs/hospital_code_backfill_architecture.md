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

## Governed asynchronous seed

Migration `0276_hospital_code_import` records durable intent to install the
checksum-pinned `healthtree_hospital_codes_20261002_v1.zip` from the governed
HealthTree Drive folder. It does not access the network or write mapping rows.
A Celery worker running the new application revision discovers the queued row
at worker startup and performs the download and import outside deployment.

The artifact contains 691,800 source keys from Alex's bounded unmapped cohort:
621,497 Epic and 70,303 Cerner. It is a ZIP containing a manifest and one
pre-aggregated JSONL member, so production needs no Parquet or `pyarrow`
dependency. Before the first mapping write the worker verifies:

- the migration-pinned archive SHA-256;
- artifact identity and schema version;
- manifest-declared and actual row counts;
- the JSONL member checksum; and
- every row's required shape and Epic/Cerner source identity.

Imports use bounded transactions and a PostgreSQL advisory lock. A receipt in
`hospital_code_import` records queued/running/applied/failed state, task ID,
counts and failure details. Re-delivery is safe: the ordinary hospital-code
upsert preserves curator destinations, status, notes, reviewer and curator
labels while refreshing source evidence. The Celery task acknowledges only
after completion and rejects on worker loss, so replacing a worker redelivers
the idempotent import. Migration `0277` narrowly requeues the v1 receipt that
was interrupted during its first Render staging deployment; it is a no-op for
queued, applied, or explicitly failed receipts.

`scripts/build_hospital_code_seed.py` reproducibly builds the deployment
artifact from Alex's source-name ZIP and Nikita's FHIR code inventory. Nikita's
larger inventory only enriches matching Alex keys; it does not expand the
curation queue.

## Facility context in the governed seed

Alex's code-to-facility evidence is retained in `source_metadata.facilities`,
including facility and parent names, attribution level and method, confidence,
low-confidence reason, and record/patient counts. These are contextual
observations on a global mapping, not PRomop `Organization` foreign keys:
Alex's identifiers mix sites, Epic brands and tenants, attached facilities,
and patient-entered aliases. Treating them all as authoritative hospitals
would create false organization scope and multiply the mapping queue.

The detail API moves that list to `source_evidence.facilities`; list responses
remain compact. Edit Mapping shows the names with level and confidence so a
curator can distinguish, for example, an individual Cerner site from a broad
Epic tenant or a low-confidence connection alias.

Nikita's matching unit evidence preserves raw display, raw quantity code and
system, normalized UCUM when available, validation verdict/reason, and counts.
Alex's per-unit percentiles and suppression state are merged onto the matching
untouched display/code pair. The dialog displays both the original and
normalized forms and retains the percentile mini-graph.
