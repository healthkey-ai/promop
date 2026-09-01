# Code Mapping Redesign — Source Vocabulary Tabs + ConceptRelationship Mirroring

**Branch:** `feat/code-mapping-source-vocab-tabs`
**Status:** In progress

---

## Why SourceCodeConceptMapping Is the Source of Truth (Not ConceptRelationship)

`concept_relationship` is the OMOP-standard table for pairwise concept
relationships (`Maps to`, `Mapped from`, etc.). It would be natural to ask: why
not just use it as the single source of truth for all mappings?

There are five reasons `SourceCodeConceptMapping` (SCCM) must remain primary,
with `concept_relationship` (CR) serving as a downstream mirror:

### 1. Uncoded source codes have no concept — CR requires both sides

CR's schema is `(concept_id_1, concept_id_2, relationship_id)` — both FK
columns are non-nullable. A large share of real-world mappings start from
**uncoded free text**: a paper lab report that says "M-PROTEIN" or a clinician's
note that says "bilateral mastectomy." These have no source concept in any
vocabulary — there is no `concept_id_1` to write. SCCM handles this with a
nullable `source_concept` FK and a plain `source_code` CharField. CR cannot
represent it at all.

### 2. CR lacks curation workflow columns

A mapping goes through a lifecycle: proposed by an import or suggest engine,
reviewed by a curator, approved or rejected. CR has none of the columns needed
for this: no `status`, no `reviewer`, no `reviewed_at`, no `origin_system`, no
`occurrence_count`, no `first_seen`/`last_seen`. Adding all of them to CR would
mean 10+ nullable columns on every Athena-loaded row (millions of rows) that
would always be NULL — a significant schema extension to a standard OMOP table
for metadata that only HealthKey-written rows use.

### 3. CR is Athena's table — extending it creates upgrade friction

Every OMOP vocabulary refresh drops and reloads CR from Athena CSV files. Custom
columns must survive that process. Each added column is a migration that must be
re-applied after every vocab load, and any Athena schema change risks conflicts.
Keeping our curation metadata in a separate table (SCCM) means Athena loads
never touch our work and our schema never fights theirs.

### 4. FHIR importers need source-code-level lookup, not concept-level

`resolve_source_code()` looks up by `(source_vocabulary_id, source_code)` — the
raw code string from an incoming FHIR bundle. CR is keyed by concept IDs.
Resolving through CR would require an extra join through `concept` to go from
code string → concept_id → CR row → target concept. SCCM gives a direct
indexed lookup on the raw code.

### 5. SCCM records provenance that CR cannot

SCCM tracks which ingest channel first raised a mapping (`origin_system`), how
many times the code has been seen (`occurrence_count`), and the full approval
chain (`created_by`, `reviewer`, `reviewed_at`). These are essential for
prioritising the curation queue. CR has no place for any of this without the
column bloat described in point 2.

### The mirror relationship

CR is not ignored — it is a **downstream mirror**. When a mapping with both
source and destination concepts is approved, we write a `Maps to` row to CR so
that external OMOP tools (Atlas, Usagi, data-quality dashboards) can see our
curated mappings in the standard place. The two tables serve complementary
roles with a defined data flow:

```
Athena vocab load ──→ concept_relationship ──→ sync into SCCM
Curator approval  ──→ SCCM (status=approved) ──→ mirror to concept_relationship
FHIR import       ──→ resolve_source_code()  ──→ reads SCCM
Lab import        ──→ resolve_source_code()  ──→ reads SCCM
```

---

## Why Athena Mappings Must Be Imported into SCCM

This is the **critical precursor step** that makes everything else work.

Athena ships millions of `Maps to` relationships in `concept_relationship` —
ICD-10-CM → SNOMED, RxNorm → RxNorm, CPT4 → SNOMED, etc. These are exactly
the mappings that FHIR importers, lab importers, and `resolve_source_code()`
need when they encounter an ICD-10 code or an NDC and need to know what
standard OMOP concept it maps to.

**The problem:** `resolve_source_code()` reads from SCCM, not from CR. It does
a direct indexed lookup by `(source_vocabulary_id, source_code)` — the raw
code string from an incoming bundle or lab report. CR is keyed by concept IDs,
not code strings, so resolving through it would require an extra join through
`concept` on every lookup. And the FHIR importer should not have to know
whether a mapping came from Athena or from a curator — it should just ask "what
does ICD10CM:E11.65 mean?" and get an answer.

**The solution:** `sync_athena_mappings` imports every Athena `Maps to`
relationship for source vocabularies we receive codes in (ICD-10-CM, ICD-9-CM,
CPT4, RxNorm, NDC, HemOnc, etc.) into SCCM as pre-approved rows with
`origin_system='athena'` and `source='Athena'`. This gives every importer a
single table to resolve from, whether the mapping was loaded from Athena or
curated by a human.

Standard vocabularies (SNOMED, LOINC) are excluded — their concepts are already
standard and self-resolve, so importing their `Maps to` edges would create
millions of identity mappings that add no value.

The command is **re-runnable**: `get_or_create` means existing SCCM rows
(including curator-curated ones) are never overwritten. Run it after every
vocabulary refresh to pick up new Athena mappings.

```bash
# After a vocabulary load:
DATABASE_URL="..." python manage.py sync_athena_mappings

# Dry run to see what would be created:
DATABASE_URL="..." python manage.py sync_athena_mappings --dry-run
```

---

## Context

The Code Mapping page currently organizes tabs by **destination vocabulary**
(SNOMED, LOINC, HK-Labs, etc.). This is backwards — curators think about what
arrived (ICD-10, LOINC, CPT4), not where it landed. Additionally, approved
mappings are stored only in SCCM, never written to CR (the OMOP-standard place
for `Maps to` relationships). And existing Athena-provided mappings in CR are
invisible to the curator.

**Goals:**
1. **Import Athena mappings into SCCM** — so all importers have a single lookup table
2. Switch tabs to **source vocabulary** (ICD-10-CM, ICD9CM, CPT4, RxNorm, etc.)
3. Add **ATHENA MAPPED** section showing Athena-provided mappings alongside curated ones
4. **Mirror approved mappings to CR** — when a mapping with both concepts is approved, write a `Maps to` row
5. **Suggest on all tabs** — not just HK-* vocabularies
6. Add provenance columns to CR for HealthKey-written rows

---

## Phase 1: Database Changes

### 1A. Add provenance columns to ConceptRelationship

**File:** `omop_core/models.py:635`

Add these nullable columns (Athena rows remain untouched):

| Column | Type | Purpose |
|---|---|---|
| `source` | CharField(50, null, blank) | `NULL`=Athena, `'HealthKey'`=curated |
| `origin_system` | CharField(50, null, blank) | `'curator'`, `'suggest'`, `'fhir-upload'` |
| `status` | CharField(20, null, blank) | `'proposed'`, `'approved'`, `'rejected'` |
| `notes` | TextField(null, blank) | Curator reasoning |
| `reviewer` | FK(User, null, SET_NULL) | Who approved |
| `reviewed_at` | DateTimeField(null, blank) | When approved |
| `updated_at` | DateTimeField(null, blank) | Last modification time |

No `created_by` or `created_at` — the high-stakes decision is reviewer
approval, and the creator may be an automated suggest engine or a person; the
reviewer identity is what matters for audit.

### 1B. Data migration: mirror existing approved SCCM rows to CR

For each SCCM where `status='approved'` AND `source_concept IS NOT NULL` AND
`target_concept IS NOT NULL`:
- `get_or_create` a CR row (`Maps to` + reverse `Mapped from`)
- Set provenance: `source='HealthKey'`, `status='approved'`

### 1C. Management command: sync_athena_mappings (critical precursor)

**File:** `omop_core/management/commands/sync_athena_mappings.py`

Imports all Athena `Maps to` relationships from CR into SCCM for source
vocabularies we receive codes in. This is the step that gives FHIR importers,
lab importers, and `resolve_source_code()` access to Athena's mapping
knowledge through the single lookup table they already read from.

- Scans CR for `relationship_id='Maps to'` AND `source IS NULL` (Athena rows)
- Filters to source vocabularies: ICD10CM, ICD9CM, CPT4, RxNorm, NDC, HemOnc, etc.
- Creates SCCM rows with `status='approved'`, `origin_system='athena'`, `source='Athena'`
- `get_or_create`: existing curator-curated SCCM rows are never overwritten
- Re-runnable after every vocabulary refresh
- Excludes SNOMED/LOINC (standard concepts self-resolve)

---

## Phase 2: Backend API Changes

### 2A. Reference endpoint — add `source_vocabulary_tabs`

### 2B. List endpoint — add `mapping_origin` field

### 2C. Suggest endpoint — accept `source_vocabulary_id`

### 2D. Approval → mirror to CR

---

## Phase 3: Frontend Changes

### 3A. Tabs by source vocabulary
### 3B. Three-section layout (ATHENA MAPPED / UNMAPPED / MAPPED)
### 3C. Suggest enabled on all tabs
### 3D. Updated tab labels and aria

---

## Critical Files

| File | Changes |
|---|---|
| `omop_core/models.py:635` | Add 7 nullable provenance columns to ConceptRelationship |
| `omop_core/migrations/` | Schema migration + data migration |
| `omop_core/management/commands/sync_athena_mappings.py` | New command |
| `omop_core/services/source_vocabularies.py` | Tab ordering constants, labels |
| `patient_portal/api/views.py` | Reference, suggest, approval mirroring |
| `omop_core/services/mapping_suggestions.py` | Source vocab filtering |
| `frontend/src/components/CodeMappings/CodeMappingPage.tsx` | Tab logic, sections |
