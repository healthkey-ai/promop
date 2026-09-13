# Patient genomics

The authoritative description is [genomics_architecture.md](../genomics_architecture.md).
It covers the Genomics dialog/list, five-disease priority catalog, approved
PatientRecord mappings, OMOP linked facts, API, migrations, verification and expert
questions, including CDEW/NGS integration boundaries.

Do not build new integrations from the historical qualifier-only encoding.

## Detailed synthetic sample data (#1232)

`populate_genomics_sample_data` writes through the same variant service as the
Genomics editor, then refreshes each affected PatientRecord once. It supports BC,
MM, FL, MCL and CLL and includes synthetic specimen/report IDs, laboratory,
collection and interpretation dates, origin/source class and classification
context. Sequence findings include paired DNA/protein examples where available,
VAF with explicit percent units, coverage, and selected reference annotations.
FISH findings use clone fraction (including zero for absent findings); karyotypes
omit sequence-specific values. Dates, quantities and report identities are
generated examples, not clinical evidence or a representative patient cohort.

Unsupported annotations remain empty. The catalog's legacy `PALB1` label is
preserved without assigning a PALB2 transcript or reference locus. Genomic HGVS
is supplied only for the checked [BRAF c.1799T>A example](https://www.ncbi.nlm.nih.gov/clinvar/RCV001248834/)
and [TP53 c.743G>A example](https://www.ncbi.nlm.nih.gov/clinvar/RCV000013150.22/).
The ESR1 c.1610A>G example is paired with p.Tyr537Cys, as reported in
[this ESR1 sequencing study, Table 2](https://pmc.ncbi.nlm.nih.gov/articles/PMC12547707/).
Reference annotations use GRCh38/RefSeq; they do not establish a clinical
classification for a synthetic finding.

Staging is Render (`promop-staging`). In its shell, where `DATABASE_URL` is
already configured, preview a specific sample patient before writing:

```sh
python manage.py populate_genomics_sample_data --patient 123 --overwrite --dry-run --verbosity 2
python manage.py populate_genomics_sample_data --patient 123 --overwrite
```

Replace `123` with the intended sample person's ID (an email also works).
For a scoped batch, use `--org <slug> --disease BC --count 5`; `--all` selects
every eligible patient within those filters. Without a size option, the command
selects 10% of eligible patients (at least one). Existing variants are skipped
unless `--overwrite` is specified; overwrite retires all that patient's current
variant facts and replaces them with a newly generated set. It does not enrich
the existing findings in place. Random previews and subsequent writes can differ.
Dry runs perform no writes; verbosity 2 prints complete JSON payloads.

When running from a local checkout, explicitly select staging without printing
credentials (use the absolute `.env` path when working in an isolated worktree):

```sh
set -a
source /Users/adam/promop/.env
set +a
DATABASE_URL="$STAGING_DATABASE_URL" /Users/adam/promop/.venv/bin/python manage.py populate_genomics_sample_data --patient 123 --overwrite --dry-run --verbosity 2
```

Load the OMOP vocabulary and apply migrations through the genomics component
seeds before writing. `seed_genomics_catalog` can seed missing mappings while
preserving curator decisions. Writes still require approved mappings for every
used field. Both legacy table/column CDM codes and Athena CDM codes whose standard
concept name is `measurement.measurement_id` support reads, edits and deletes.
Patient and event-table boundaries remain enforced.

Each patient's variant writes are atomic: a failure also rolls back that
patient's overwrite. Successful patients are refreshed, failures are reported,
and any write failure causes a nonzero command exit with committed counts.
