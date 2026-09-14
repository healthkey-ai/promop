# PRomop Genomics: Implemented Architecture

As implemented on `dev` through `25133b7` ([#1249](https://github.com/healthkey-ai/promop/pull/1249), [#1259](https://github.com/healthkey-ai/promop/pull/1259)), updated 2026-09-14 with the read-only schema audit below. Remaining requirements and deployment work are in [the implementation plan](genomics_implementation.md).

## Storage decision

Genomic findings use one OMOP Measurement parent with linked Measurement and Observation components. PatientRecord is a refreshed projection of those facts. The genomics writer does not use G-CDM's GENOMIC_TEST, TARGET_GENE, VARIANT_OCCURRENCE or VARIANT_ANNOTATION tables. The installed omop_genomics app has no current model classes.

The design retains this approach because the catalog includes gene variants, structural abnormalities and genome-wide patterns, and findings can arrive without a known procedure or specimen. G-CDM adoption is not planned. Test, specimen and scope support is a separate future phase within existing CDM tables; it is not implemented in the finding writer.

This is an application design using OMOP CDM tables with local conventions. In particular, Measurement.value_as_string is an application extension and NOTE references use an application encoding. Standard-table storage alone does not establish complete CDM conformance or compatibility with every external analytical tool.

## Data flow and ownership

```text
Shared Genomics dialog / PatientRecord PATCH / finding CRUD API
  → patient authorization, validation and approved field mappings
  → transaction with PatientRecord row lock
    → finding Measurement
    → linked component Measurements / Observations
    → owned NOTE for overflow or literal reference-shaped text
  → refresh PatientRecord
    → genetic_mutations and 42 genomics_<marker> lists
  → findings plus disease-specific empty priority rows
```

The canonical writer is genomics.py. save_variant retains the parent ID on edit and marks recognized old components erroneous before writing their replacements. Unrelated linked facts survive an edit. delete_variant marks the parent and linked Measurement/Observation rows erroneous rather than deleting them. NOTE rows are retained; the deletion path does not give them a separate erroneous state.

Writes and refresh run atomically under the patient-record lock with automatic signal refresh suppressed. patient_record_service.py builds a patient snapshot, reads finding parents, overlays linked components and caches the genomics projection within that snapshot. Repeated findings retain separate parent IDs; they are not collapsed by gene or test date. The general list currently has no explicit size cap or clinical-significance filter.

## Parents, components and vocabulary

General parents use source LOINC 81252-9. Priority parents use their approved mapping's source value, initially `genomics:<marker>`, with 81252-9 retained as the mapping's portable vocabulary code. The gene is stored in qualifier_source_value. Parent value_as_string takes the first nonempty value of variant, variant_name, genomic_dna_change and amino_acid_change. transcript_dna_change is a separate component and does not automatically fill the legacy variant string.

Components point to the parent through measurement_event_id or observation_event_id, with the corresponding event-field concept identifying measurement.measurement_id. Patient identity and event type matter because IDs may overlap between OMOP tables. Writes, component reads, supersession and deletion share `_measurement_event_concepts()`: both literal CDM table/column codes and Athena CDM codes whose standard concept name is `measurement.measurement_id` are recognized. Writes require an active concept; existing links remain readable. This was completed in [PR #1233](https://github.com/healthkey-ai/promop/pull/1233).

Approved FieldConceptMapping rows govern priority parents and populated or explicitly cleared components. Component field names are `genetic_mutations.<field>`. Arbitrary-gene parents use the generic parent recipe; their components still require approved mappings. A standard concept must match the destination domain. Runtime resolution supports LOINC and Maps to; when an appropriate standard concept is unavailable, the writer uses concept 0 and preserves the source identifier. No standard concept ID is invented.

The repository's domain regression fixture records 81252-9 in Observation. Parents intentionally remain Measurements for event linkage, with concept 0 when the installed vocabulary has no suitable Measurement-domain standard concept. Component storage follows the approved mapping's table. The following is the implemented recipe inventory, not a fresh audit of an installed vocabulary.

| Component | Vocabulary code or local source | Storage/value |
| --- | --- | --- |
| gene | LOINC 48018-6 | Measurement text |
| interpretation | LOINC 53037-8 | Measurement text |
| genome_assembly | LOINC 62374-4 | Measurement text |
| transcript_reference_sequence_id | LOINC 51958-7 | Measurement text |
| amino_acid_change | LOINC 48005-3 | Measurement text |
| genomic_source_class | LOINC 48002-0 | Measurement text |
| chromosome | LOINC 48000-4 | Measurement text |
| cytogenetic_location | LOINC 48001-2 | Measurement text |
| allelic_frequency | LOINC 81258-6 | Measurement number and unit |
| genomic_dna_change | LOINC 81290-9 | Observation when resolved; frozen seed fallback is Measurement |
| variant_analysis_method_type | LOINC 81304-8 | Observation when resolved; frozen seed fallback is Measurement |
| variant_category | LOINC 83005-9 | Observation when resolved; frozen seed fallback is Measurement |
| variant_name | LOINC 81253-7; source genomics:variant_name | Observation text; v3 promotion of exact untouched seeds |
| variant_description, origin | `genomics:<field>` | Observation text |
| assessment, specimen_id, specimen_type, collection_date, report_id, laboratory | `genomics:<field>` | Observation text |
| interpretation_date, classification_framework, evidence_source, genomic_reference_sequence_id, zygosity | `genomics:<field>` | Observation text |
| status | LOINC 69548-6; source genomics:status | Measurement text |
| clone_fraction | genomics:clone_fraction | Measurement number and unit; no LOINC recipe |
| transcript_dna_change | LOINC 48004-6; source genomics:transcript_dna_change | Measurement text |
| coverage_depth | LOINC 82121-5; source genomics:coverage_depth | Observation when resolved; migration fallback is Measurement |
| amino_acid_change_type | LOINC 48006-1; source genomics:amino_acid_change_type | Measurement text |

The effective runtime registry corrects the four Observation fallbacks shown above, but the writer ultimately uses the persisted mapping table. A vocabulary load after initial seeding therefore needs a mapping audit; changing FIELDS does not repair existing recipes. See [domain regression tests](../tests/test_genomics_loinc_domains.py) and [recipe/audit tests](../tests/test_genomics_recipes.py).

Recipe v3 adds portable variant-name metadata without changing the frozen v1 catalog or rewriting patient facts. Migration 0231 and seed retries promote only the exact original approved seed shape, with no reviewer and no curator provenance; blank provenance alone is insufficient. Nonmatching recipes need explicit curation. Reads and edits recognize historical local keys, portable component codes and current curator source values. The shared resolver accepts one active source and one active standard target (direct or Maps to); missing, ambiguous and unsupported-domain resolution retains concept 0 and raw source.

Reads retain legacy gene-specific Measurements and mappings with withdrawn approval. Legacy qualifier/value concepts can supply origin and interpretation; linked components overlay them. Editing converts a legacy parent to the current parent recipe and clears the old qualifier/value concepts. Legacy hardcoded origin/interpretation IDs remain compatibility behavior, not portable examples for new integrations.

## Finding state and validation

| State | Representation | Meaning in the finding contract |
| --- | --- | --- |
| Present | status: "present" | An asserted finding |
| Absent | status: "absent" | Explicitly tested and not found within the assay's scope |
| Indeterminate | status: "indeterminate" | Inconclusive state; includes legacy no-call/not-tested compatibility without asserting a negative |
| Unknown | No finding row; empty named list | No recorded result; never an implicit negative |

Explicit stored status governs the effective state. With no stored status, legacy assessment supplies present/absent/indeterminate; no_call, not_tested and unrecognized nonempty assessments project as indeterminate while preserving their raw source text. Only a finding with neither state nor assessment keeps the historical present default. Empty lists remain unknown. New status/assessment contradictions are rejected. Legacy assessment-only edits update the effective status; state-only transitions supersede inherited assessment components while retaining erroneous history. Unchanged unrecognized source assessments survive unrelated edits. No bulk state backfill runs.

For absent findings, explicitly supplied nonempty amino_acid_change, allelic_frequency, genomic_dna_change, transcript_reference_sequence_id, transcript_dna_change, amino_acid_change_type and zygosity are rejected. Inherited values for those fields are cleared on an absent edit. Method, specimen, report, laboratory, dates, interpretation and coverage context remain accepted. Raw variant/name strings may describe the tested scope and are preserved. Clone fraction applicability and other source-specific constraints still require clinical/source review; no thresholds are inferred.

VAF and clone fraction are independent numeric components. Each accepts % (0–100) or 1 (0–1), defaults an omitted unit to %, preserves zero and rejects values requiring more than five decimal places. Coverage depth is a nonnegative finite number on the finding. Origin and genomic source class are independent; transcript and genomic DNA changes are independent; protein change and protein change type are independent.

Text input is limited to 10,000 characters and the gene identifier to 50. Unknown payload fields, invalid dates and invalid numbers are rejected. A missing interactive test_date defaults to today. mutation aliases variant, and assay_method aliases variant_analysis_method_type.

Detected-marker summaries use the effective present state, and refresh clears stale summaries after absence or indeterminate transitions. The TP53 disruption aggregate retains its legacy present-status/assessment checks pending clinical agreement on combined TP53/del(17p) scope. It is computed with any(...), so no qualifying evidence still produces False, including an empty record. The stored status contract does not by itself establish correct unknown/negative semantics for every downstream eligibility consumer.

## Asserted and derived findings

Every canonical finding in the general and named lists has `provenance: "asserted"`. The row ID identifies its stored Measurement parent. An existing finding can echo this unchanged server metadata during a dedicated, general-list or named-list edit. Supplying provenance on create, changing it to `derived`, or supplying `derivation_version`/`derived_at` on any write is rejected atomically. Projection metadata is never stored as an asserted component.

Both complex-karyotype derivation functions still return `None`. An asserted finding of any status takes precedence. The derivation boundary receives copies of canonical finding/component projections, excludes derived rows, and never reads previously derived PatientRecord lists. A future function must return `DerivedFinding(finding, derivation_version)` without a stored parent ID. Projection adds `provenance: "derived"`, the explicit rule version and `derived_at`; synthetic tests exercise this envelope without implementing a clinical threshold. Derived rows stay outside `genetic_mutations` and are never persisted to OMOP as observations.

The field-provenance registry describes the general list and all 42 named lists as Measurement parents with linked Measurement/Observation components. Its lookup reuses canonical projection, event identity and owned-text resolution, excluding other patients, wrong event types, erroneous rows and unrelated attributes. A projection-only derived row has no stored assertion to return. Composite field provenance can trace its genomic inputs too.

## Long text and schema

Both model `value_as_string` fields are `CharField(max_length=60)`. `0222_genomics_text_values` is a no-op on fresh installations. No implemented migration/backfill narrows a formerly widened database. A recorded migration name cannot reveal which historical contents of that edited migration ran.

The [schema audit command](../omop_core/management/commands/audit_genomics_schema.py) reads the actual PostgreSQL column types, limits, total rows, counts exceeding 60 characters, maximum lengths and the recorded migration timestamp. It resolves the same tables as the application's search path and counts all shared facts, including non-genomics and erroneous rows. It does not retrieve patient IDs or source strings. PostgreSQL rejects the audit if row-security policies would filter its counts; the command does not bypass those policies. One database-enforced read-only, repeatable-read transaction provides a consistent snapshot; each statement defaults to a 30-second timeout.

In the intended deployment's configured shell, run:

```sh
python manage.py audit_genomics_schema --environment render-staging --check > genomics-schema-audit.json
```

`--environment` is an evidence label, not a connection selector. `--database` selects a configured Django alias (default: `default`). For local operational access, explicitly select the documented `STAGING_DATABASE_URL` as `DATABASE_URL`; never let an unspecified `.env` URL select the target. Credentials and connection URLs are not included in the report.

The report uses `report_version: 1`. `columns_match` means both selected columns are exactly `varchar(60)`. With `--check`, a missing, unsupported, narrower or wider column exits nonzero after writing the diagnostic JSON. Without `--check`, completed mismatch reports return normally for inventory collection. Database failures/timeouts exit nonzero without a partial report; the error includes only the exception class. Operators can set `--statement-timeout-ms` from 1 to 300000 and rerun safely. The audit performs no schema repair or NOTE rewriting. Width verification does not certify NOTE ownership, vocabulary or historical migration operations; the report marks those limits explicitly.

Observed Render staging evidence from [2026-09-14 03:17 UTC](https://github.com/healthkey-ai/promop/issues/1237#issuecomment-5658503062):

| Column | Actual type | Rows inspected | Values over 60 characters | Maximum length |
| --- | --- | ---: | ---: | ---: |
| Measurement.value_as_string | varchar(60) | 320,262 | 0 | 36 |
| Observation.value_as_string | varchar(60) | 314,262 | 0 | 60 |

Those observed columns require no narrowing. Other deployment inventories and any evidenced backfill remain #1237. A future shared-column conversion must first support every affected reader, including non-genomics consumers; changing their values to NOTE references without compatible readback would lose usable text.

[clinical_text.py](../omop_core/services/clinical_text.py) provides the shared owned-NOTE encoding used by the genomics adapter. Values longer than 60 characters, and literal source text ending in `[note:<digits>]`, are stored in an immutable NOTE. The fact contains only `[note:<id>]`, which fits even a 19-digit ID. Notes bind the patient, exact fact table/ID (`note_source_value = genomics:<table>:<id>`) and finding context (`note_title = genomics:overflow:<parent_id>`). Reads require all three; narrative text is returned once and never interpreted recursively. Missing, malformed, wrong-owner or wrong-context references remain literal source text. Edits create new owned notes as needed; superseded and deleted findings retain their historical notes.

For existing parent-only overflow notes, compatibility reads require the same patient, finding ID and clinical date, the exact historical 60-character prefix/reference encoding, and a unique reference among the patient's active snapshot facts. Ambiguous legacy references remain source text for reconciliation; the old format cannot prove original component ownership beyond this consistency evidence. Saving a safely resolved legacy finding writes the new owned encoding and retains its old notes. Resolving previously ambiguous/history-only references and reconciling widened deployment columns remains [#1237](https://github.com/healthkey-ai/promop/issues/1237).

The snapshot batches all candidate NOTE IDs into one patient-scoped query and caches the result for both parent and component projection. This avoids per-component lookups. The encoding is a local application convention, not a formal NOTE event relationship.

## Catalog and presentation

The immutable genomics_catalog_v1.json contains 42 marker keys and 26 initial components, sourced from CancerBot commit a840f8d9af2c35477e2b6b76f981b29f02c21eec. Five later component recipes bring the implemented component inventory to 31. The catalog is packaged migration data, not a runtime CancerBot dependency.

Every marker has a concrete JSON list field genomics_<key> on PatientRecord. The full finding projection is genetic_mutations.

| Disease | Priority keys, prefixed with genomics_ for the field |
| --- | --- |
| BC | brca1, brca2, pik3ca, tp53, esr1, palb1 |
| MM | tp53, kras, nras, braf, myc, fam46c, dis3, xbp1, del17p, t414, t1114, t1416, gain1q, hyperdiploidy, chromothripsis, igh |
| FL | bcl2, ezh2, kmt2d, crebbp, bcl6 |
| MCL | tp53, myc, kmt2d, notch1, notch2, nsd2, cdkn2a, smarca4, ccnd1, del17p, t1114, bcl2_amplification, complex_karyotype, complex_karyotype_excl_t1114, atm_atr, notch1_notch2 |
| CLL | tp53, notch1, sf3b1, atm, del17p, del11q, del13q, trisomy12 |

Exact structural aliases take precedence over generic gene matching. A TP53 mutation does not imply del(17p). Combined ATM/ATR and NOTCH1/NOTCH2 assertions are preserved rather than split. PALB1 and source examples remain pending expert review; seeded storage approval does not validate their clinical nomenclature.

Standalone and federated views share GenomicsTab.tsx. It provides a list, detail dialog, add/edit, confirmed deletion, read-only handling and keyboard row activation. Empty priority rows are presentation only. Changing disease changes suggested rows without removing stored findings. The dialog provides one editable finding-status control; the legacy source assessment is displayed separately. It exposes independent clone fraction/unit, VAF/unit, transcript DNA change, genomic DNA change, protein change/type and coverage depth controls. Original variant text is separate from transcript DNA change. Absent edits clear and disable inapplicable variant components. The list shows effective state and labels unsaved priority placeholders Unknown. The effective registry in [genomics_components.py](../omop_core/services/genomics_components.py) supplies all 31 components to the writer, mapping inventory and serializer validation, including the five later additions. Historical seed fixtures and persisted curator recipes remain unchanged.

Genomics owns all new interactive discrete finding entry, including overlapping cytogenetic markers (#1242). The Disease tab shows `cytogenetic_markers` as a read-only legacy summary; cytogenetic risk classification remains separate. The writable descriptor cannot reenable summary editing through a curator mapping. PatientRecord PATCH rejects changed summaries (including the historical `cytogenic_markers` alias); unchanged autosave echoes are ignored without writing facts or pending edits. Retained internal legacy projectors support import/history compatibility, not interactive authoring.

The Genomics tab exposes original legacy results through `GET /api/v1/patient-records/{person_id}/genomics-legacy-cytogenetics/`. This patient-authorized, read-only endpoint includes historical individual and aggregate Observation/Measurement rows, original text and source tokens, stored dates, selection clears and erroneous rows. It uses 50-row keyset pages ordered by date, table and ID, with batched NOTE resolution restricted to the owning patient and fact. Unresolvable references remain visible without disclosing another fact’s text. Cache-only or pending legacy summaries are also shown, explicitly without a date.

Reconciliation is source-preserving and manual: compare original legacy evidence with current findings before entering another finding. Historical rows are not converted, rewritten or automatically merged with genomics lists; deselection is editing history, never an explicit negative test, and dates are never synthesized. Exact original text distinguishes gain from amplification when the source does; ambiguous “1q21 gain/amplification” is no longer normalized to gain. The compatibility summary can normalize known aliases, while the history view retains raw source evidence.

## API contract

| Endpoint | Operations |
| --- | --- |
| /api/v1/patient-records/{person_id}/genomics/ | GET finding list; POST finding |
| /api/v1/patient-records/{person_id}/genomics/{variant_id}/ | GET, PATCH, DELETE finding |
| /api/v1/patient-records/{person_id}/genomics-catalog/?disease=BC | GET disease catalog and marker writability |
| /api/v1/patient-records/{person_id}/genomics-legacy-cytogenetics/?cursor=... | GET original legacy cytogenetic history, 50 rows per page |
| /api/v1/patient-records/{person_id}/ | PATCH general list or named marker lists |

Named-list PATCH replaces only the named marker. An empty list clears its findings and leaves it unknown. Explicit absence requires an entry:

```http
PATCH /api/v1/patient-records/{person_id}/
Content-Type: application/json

{"genomics_del17p":[{"status":"absent","variant_analysis_method_type":"FISH","test_date":"2026-09-01"}]}
```

The named field supplies the catalog gene and marker identity. General POST requires a gene; use marker_key to identify a specific catalog abnormality. IDs must belong to the patient and, for named edits, the marker; duplicates are rejected. Changed general-list and named-list replacements cannot be mixed in one request. Unchanged full-record projection echoes are ignored. List replacement is not an incremental import contract.

Patient/representative, clinician, organization and service-token authorization uses the existing patient access and SMART scope checks in views.py and permissions.py. Dedicated CRUD, general-list replacement and named-list edits share actor selection: type 32865 for self/representative writes and 32817 otherwise. An edit records that type on both the current parent and replacement components. Service calls default to 32817 and can explicitly supply another type. This actor type is distinct from the asserted/derived projection envelope; automated extraction provenance remains a separate receiving gate.

## Installation, verification and integration limits

Migration 0223_priority_genomic_fields adds the 42 lists; 0224_seed_genomics_mappings seeds the initial 68 recipes; 0226_seed_genomics_status_component and 0227_seed_genomics_v2_components add five recipes. 0228_merge_cytogenetics_and_genomics creates the NOTE sequence. Apply the complete current migration graph, including its merge migrations.

manage.py seed_genomics_catalog seeds all 73 recipes idempotently and promotes exact untouched variant-name seeds to v3, preserving curator decisions including rejections. It does not repair other existing domain assignments. manage.py audit_genomics_domains audits all 31 component recipes, distinguishes intentionally local fields, and exits nonzero for missing approvals, absent/unresolved/ambiguous vocabulary or domain/concept mismatches. It prints source and standard domains plus proposed repairs, without changing any mappings or patient data. Required unmapped, type and CDM event concepts must exist before writes.

After loading vocabulary, run the audit on that deployment. For each proposed repair, review the field in the field-mapping curation interface, set the installed standard concept and matching OMOP table, retain the source key and independent value/unit settings, and record the decision before approving it. Rerun the audit and retain its output with the vocabulary version. Curation applies to future writes; migrating existing facts between tables requires a separately reviewed data repair. Passing fixture tests does not certify a deployment vocabulary.

Existing coverage is in test_genomics_crud.py, test_genomics_catalog.py, test_genetic_mutation_roundtrip.py, the domain tests and shared frontend component tests. It covers CRUD, status, numeric components, NOTE round trips, approval, repeated findings, marker isolation and projection stubs. Merged validation is linked from the implementation plan. The schema audit has dedicated PostgreSQL tests for narrow and widened columns, missing/unsupported types, source preservation, database-enforced read-only access and sanitized failures. Only the recorded staging observation above is deployment-schema evidence; installed-vocabulary completeness remains a separate operational audit.

Interactive CRUD is implemented. A published versioned import contract, source-finding idempotency/reconciliation, extraction provenance and a missing source-date policy are not implemented by this writer. Specimen/report identifiers are per-finding text, not linked test/specimen resources. No test scope, scope-derived absence, whole-test import or full-sequencing projection policy is implemented. Those delivery boundaries and the remaining findings work are in the plan.

## Source files and operational boundaries

- [Canonical writer](../omop_core/services/genomics.py), [PatientRecord projection](../omop_core/services/patient_record_service.py), [catalog](../omop_core/services/genomics_catalog.py), [frozen fixture](../omop_core/data/genomics_catalog_v1.json).
- [Shared Genomics UI](../frontend/src/components/PatientInfo/tabs/GenomicsTab.tsx), [patient API](../patient_portal/api/views.py), [mapping inventory](../omop_core/services/field_descriptor.py), [mapping serializers](../patient_portal/api/serializers.py).
- [Sample data command](../omop_core/management/commands/populate_genomics_sample_data.py) seeds demonstration findings; sample variants are not expert validation of clinical nomenclature.
- Staging is Render: `https://promop-staging.onrender.com`, web service `promop-staging`, worker `promop-staging-worker`. Follow [Render staging configuration](render-staging-celery.md). Never inherit a remote database URL for local tests.

The old root architecture, handoff, short pointer and genetic-mutations implementation notes are superseded by this document and the implementation plan. Historical PR status and test totals are not current verification evidence. Strict exports must explicitly preserve the local text and linkage conventions; generic FHIR sync is not a validated genomics graph adapter.

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

For local testing, explicitly set `DATABASE_URL` to an isolated local database. For staging operations, use the configured Render shell or the documented staging connection; never inherit an unspecified `.env` database URL.

Load the OMOP vocabulary and apply migrations through the genomics component
seeds before writing. `seed_genomics_catalog` can seed missing mappings while
preserving curator decisions. Writes still require approved mappings for every
used field. Both legacy table/column CDM codes and Athena CDM codes whose standard
concept name is `measurement.measurement_id` support reads, edits and deletes.
Patient and event-table boundaries remain enforced.

Each patient's variant writes are atomic: a failure also rolls back that
patient's overwrite. Successful patients are refreshed, failures are reported,
and any write failure causes a nonzero command exit with committed counts.
