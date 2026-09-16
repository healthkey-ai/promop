# Sample patient disease status

On the Render staging shell, preview and apply the disease-status backfill:

```bash
python manage.py backfill_sample_patient_disease_status
python manage.py backfill_sample_patient_disease_status --confirm
```

The default scope is `synthea-bc`, `synthea-mm`, `synthea-fl`,
`abc-foundation`, and `bbc-foundation`. Use `--org-slugs synthea-fl` to select
a subset, or `--limit 10` for a smoke test. Other organizations are rejected.
`--dry-run` prevents writes even when `--confirm` is also supplied.

Existing PatientRecord statuses are preserved. For gaps, the command reuses
OMOP clinical status or an earlier sample fallback, then recorded progression.
Only remaining gaps receive deterministic synthetic values from active,
remission, relapse, stable, and progressing. These values provide demo variety;
they are not inferred clinical assessments or estimates of disease prevalence.

Generated fallbacks are OMOP Observations with source
`sample-patient-disease-status`, qualifier `synthetic fallback`, and unmapped
concept ID 0. Preserved progression uses qualifier `preserved progression`.
The command updates PatientRecord directly without a full refresh. Import-time
derivation can recover the fallback; condition status takes precedence when
available. Rerunning the command preserves values and creates no duplicate facts.

The breast cancer, myeloma, and follicular lymphoma enrichment commands also
complete missing disease status. Their `generate_import_enrich_synthea_*`
wrappers inherit this behavior. No schema migration is required.
