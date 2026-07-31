# Versioning & Per-Field OMOP Provenance — Implementation Plan

## Context

The PRomop JAMIA paper (Section "Operational lessons") describes two capabilities that are not yet implemented:

1. **Derivation versioning**: "each PatientRecord row carries a `derivation_version` and a `derived_at` timestamp recording the logic version that produced it; the version is incremented whenever aggregation logic changes, a backfill command re-derives affected records under the new version, and a derivation changelog documents each change."

2. **Per-(row, field) provenance lookup**: "the OMOP rows underlying any projected value are recoverable, and a per-(row, field) lookup back to those source rows is available on demand."

This plan implements both.

---

## Part 1: Derivation Versioning

### What it solves

When aggregation logic changes (e.g., switching hemoglobin from latest-value to mean, changing a lookback window, modifying composite logic like MeetsCRAB), there is currently no way to:
- Know which logic version produced a given PatientRecord row
- Selectively backfill only stale records
- Audit whether a value was produced by old or new logic

### Design

#### 1.1 Model changes (`omop_core/models.py` — `PatientRecord`)

Add two fields:

```python
derivation_version = models.IntegerField(
    default=1,
    help_text="Version of the derivation logic that last computed this row",
)
derived_at = models.DateTimeField(
    null=True, blank=True,
    help_text="Timestamp when this row was last derived from OMOP tables",
)
```

#### 1.2 Version constant (`omop_core/services/patient_record_service.py`)

Add a module-level constant:

```python
DERIVATION_VERSION = 1
```

This constant is bumped (by the developer) whenever aggregation or computation logic changes in any section extractor or in `_compute_derived_fields`.

#### 1.3 Stamp on refresh (`refresh_patient_record`)

At the end of `refresh_patient_record`, before `patient_info.save()`:

```python
patient_info.derivation_version = DERIVATION_VERSION
patient_info.derived_at = timezone.now()
```

#### 1.4 Backfill management command

New command: `python manage.py backfill_patient_records`

Behavior:
- `--version N` — re-derive only records where `derivation_version < N` (default: current `DERIVATION_VERSION`)
- `--all` — re-derive every record regardless of version
- `--batch-size 100` — process in batches to limit memory
- `--dry-run` — report count of stale records without modifying

Implementation: iterate `PatientRecord.objects.filter(derivation_version__lt=target)`, call `refresh_patient_record(record.person)` for each, with progress logging.

#### 1.5 Derivation changelog

Add `DERIVATION_CHANGELOG.md` at repo root:

```markdown
# Derivation Changelog

| Version | Date | Description |
|---------|------|-------------|
| 1       | 2026-07-31 | Baseline — all existing derivation logic |
```

Developers bump `DERIVATION_VERSION` and add a row here whenever they change aggregation logic.

#### 1.6 API exposure

Add `derivation_version` and `derived_at` to the serializer (already covered by `fields = '__all__'`). These are read-only — add to `read_only_fields`.

#### 1.7 TypeScript type

Add to `PatientInfo` interface:

```typescript
derivation_version?: number;
derived_at?: string;
```

---

## Part 2: Per-Field OMOP Provenance Lookup

### What it solves

Given a PatientRecord row and a field name (e.g., `hemoglobin_g_dl`), the system should return the specific OMOP source rows (Measurement, Observation, ConditionOccurrence, etc.) that produced that value — without storing provenance in PatientRecord itself.

### Design philosophy

The paper says provenance is "available on demand" — meaning it is **computed at query time**, not stored. This avoids:
- Doubling storage cost with per-field metadata
- Maintaining provenance data in sync with every refresh
- Schema bloat on a model that already has ~180 fields

Instead, the provenance lookup **re-executes the same query** the section extractor used, but returns the source row IDs and metadata rather than just the derived value.

### Implementation

#### 2.1 Field-to-source registry (`omop_core/services/provenance_registry.py`)

A declarative registry mapping each PatientRecord field to its OMOP source:

```python
@dataclass
class FieldProvenance:
    """Describes how to trace a PatientRecord field back to OMOP."""
    omop_table: str                    # e.g., 'Measurement', 'Observation'
    lookup_strategy: str               # 'loinc', 'snomed', 'concept_id', 'condition', 'drug_exposure'
    concept_codes: list[str] | None    # LOINC/SNOMED codes used in the extractor
    extractor_function: str            # e.g., '_get_laboratory_data'
    selection_rule: str                # 'latest', 'earliest', 'aggregate_mean', 'composite'
    description: str                   # Human-readable explanation

FIELD_PROVENANCE_REGISTRY: dict[str, FieldProvenance] = {
    'hemoglobin_g_dl': FieldProvenance(
        omop_table='Measurement',
        lookup_strategy='loinc',
        concept_codes=['718-7'],
        extractor_function='_get_laboratory_data',
        selection_rule='latest',
        description='Latest hemoglobin measurement by LOINC 718-7',
    ),
    'meets_crab': FieldProvenance(
        omop_table='Measurement,Observation,ConditionOccurrence',
        lookup_strategy='composite',
        concept_codes=None,
        extractor_function='_get_mm_specific_data',
        selection_rule='composite',
        description='Composite: calcium > 11, creatinine > 2, hemoglobin < 10, bone lesions',
    ),
    # ... one entry per OMOP-derived field (128 fields from _OMOP_DERIVED_FIELDS)
}
```

#### 2.2 Provenance query service (`omop_core/services/provenance_service.py`)

A function that, given a person and a field name, returns the OMOP source rows:

```python
def get_field_provenance(person: Person, field_name: str) -> dict:
    """Return the OMOP source rows that produced a PatientRecord field value.

    Returns:
        {
            "field": "hemoglobin_g_dl",
            "current_value": 12.5,
            "derivation_version": 1,
            "derived_at": "2026-07-31T12:00:00Z",
            "selection_rule": "latest",
            "source_rows": [
                {
                    "table": "Measurement",
                    "id": 4523,
                    "concept_id": 3006322,
                    "concept_name": "Hemoglobin [Mass/volume] in Blood",
                    "value": 12.5,
                    "unit": "g/dL",
                    "date": "2026-06-15",
                    "selected": true  # this is the row that was picked
                },
                {
                    "table": "Measurement",
                    "id": 4401,
                    "concept_id": 3006322,
                    "concept_name": "Hemoglobin [Mass/volume] in Blood",
                    "value": 11.8,
                    "unit": "g/dL",
                    "date": "2026-05-01",
                    "selected": false  # candidate but not selected (not latest)
                }
            ]
        }
    """
```

For each `lookup_strategy`, the service knows how to query:
- `loinc` → `Measurement.objects.filter(person=person, measurement_concept__concept_code__in=codes)` ordered by date desc
- `snomed` → `Observation.objects.filter(person=person, observation_concept__concept_code__in=codes)`
- `condition` → `ConditionOccurrence.objects.filter(person=person, ...)`
- `composite` → calls sub-lookups for each constituent field and merges results

The `selected` flag marks which row(s) the extractor actually used (typically the latest by date).

#### 2.3 API endpoint

Add a new endpoint on `v1_urls.py`:

```
GET /api/v1/patient-records/{person_id}/provenance/{field_name}/
```

Returns the provenance dict from `get_field_provenance()`. Requires authentication and respects existing org-level access controls.

Also add a bulk variant:

```
GET /api/v1/patient-records/{person_id}/provenance/?fields=hemoglobin_g_dl,meets_crab
```

Returns provenance for multiple fields in a single request.

#### 2.4 Registry completeness

The registry should cover all 128 fields in `_OMOP_DERIVED_FIELDS`. Fields in `_LOINC_LAB_FIELDS` can be auto-registered since they have a uniform pattern (LOINC code → Measurement, latest value). Computed fields (BMI, HR status, etc.) reference their constituent fields.

---

## Implementation order

### Phase 1 — Derivation Versioning (Issue #1)

1. Add `derivation_version` and `derived_at` fields to `PatientRecord` model
2. Run `makemigrations` and apply
3. Add `DERIVATION_VERSION` constant to `patient_record_service.py`
4. Stamp version and timestamp in `refresh_patient_record()`
5. Add `DERIVATION_CHANGELOG.md`
6. Create `backfill_patient_records` management command
7. Add `read_only_fields` in serializer
8. Add TypeScript type fields
9. Write tests

### Phase 2 — Per-Field OMOP Provenance Lookup (Issue #2)

1. Create `provenance_registry.py` with the field→source mapping
2. Auto-register `_LOINC_LAB_FIELDS` entries
3. Manually register remaining fields (disease, treatment, biomarkers, etc.)
4. Create `provenance_service.py` with `get_field_provenance()`
5. Add API endpoint and URL routing
6. Write tests (model, API, edge cases)

---

## Files touched

### Phase 1
| File | Change |
|------|--------|
| `omop_core/models.py` | Add `derivation_version`, `derived_at` to `PatientRecord` |
| `omop_core/migrations/` | New migration |
| `omop_core/services/patient_record_service.py` | Add `DERIVATION_VERSION` constant, stamp in `refresh_patient_record()` |
| `omop_core/management/commands/backfill_patient_records.py` | New command |
| `patient_portal/api/serializers.py` | Add `read_only_fields` |
| `frontend/src/types/patient.ts` | Add TS fields |
| `DERIVATION_CHANGELOG.md` | New file |
| `omop_core/tests.py` | Version stamping tests |
| `patient_portal/tests.py` | API read-only tests, backfill command tests |

### Phase 2
| File | Change |
|------|--------|
| `omop_core/services/provenance_registry.py` | New — field→source mapping |
| `omop_core/services/provenance_service.py` | New — `get_field_provenance()` |
| `patient_portal/api/views.py` | Add provenance view |
| `patient_portal/api/v1_urls.py` | Add provenance URL |
| `patient_portal/tests.py` | Provenance API tests |
| `omop_core/tests.py` | Provenance service tests |

---

## Testing strategy

### Derivation versioning
- `test_refresh_stamps_version_and_timestamp` — verify `derivation_version` and `derived_at` are set after refresh
- `test_version_increments_on_logic_change` — mock `DERIVATION_VERSION = 2`, refresh, verify stamp
- `test_backfill_command_updates_stale_records` — create records at version 1, bump constant, run backfill, verify all updated
- `test_backfill_dry_run` — verify dry run doesn't modify records
- `test_derivation_fields_read_only_in_api` — PATCH should not allow changing `derivation_version` or `derived_at`

### Per-field provenance
- `test_provenance_returns_source_measurement` — create Measurement rows, refresh, query provenance, verify source row IDs
- `test_provenance_marks_selected_row` — multiple Measurements for same LOINC, verify latest is `selected: true`
- `test_provenance_composite_field` — query provenance for `meets_crab`, verify multiple source tables returned
- `test_provenance_missing_field` — query provenance for a field with no data, verify empty `source_rows`
- `test_provenance_unknown_field` — query provenance for a non-existent field, verify 404
- `test_provenance_respects_access_control` — patient A cannot query provenance for patient B
