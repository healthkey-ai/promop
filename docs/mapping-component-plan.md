# Mapping component: implementation plan and architecture

Status: implemented for the service boundary in issue #1006.  This document is
the as-built architecture and the staged plan for moving the remaining HTTP and
UI orchestration behind it.

## Developer experience

Application code that maps clinical data imports a focused use case from
`omop_core.mapping`:

```python
from omop_core.mapping.code_resolution import resolve_source_code
from omop_core.mapping.suggestions import suggest_source_code
from omop_core.mapping.field import get_all_field_descriptors
from omop_core.mapping.therapy import match_hemonc_regimen_by_name
```

The existing REST surface is deliberately unchanged.  External clients continue
to use `/api/v1/code-mappings/`, `/api/v1/field-mappings/`, and the therapy
endpoints.  In particular, importers use the canonical code-mapping lookup API,
not an internal Python module.

Legacy imports from `omop_core.services.code_mapping`,
`omop_core.services.mapping_suggestions`, and
`omop_core.services.regimen_resolution` remain supported as compatibility
shims.  New code must not add dependencies on them.

## Ownership boundary

`omop_core.mapping` is an application component, not a new persistence
component.  It owns mapping use cases and contracts; the established PRomop
models remain authoritative and no generic mapping table is introduced.

| Area | Canonical module | Existing source of truth |
| --- | --- | --- |
| Source code to OMOP resolution, SCCM lifecycle, clinical re-pointing | `mapping.code_resolution` | `SourceCodeConceptMapping`, `Concept`, clinical OMOP rows |
| Candidate retrieval, ranking, proposal provenance, accuracy | `mapping.suggestions` | SCCM proposal/outcome fields and vocabulary tables |
| Field descriptors, transfer, value coercion | `mapping.field` | `FieldConceptMapping`, field-related models |
| Regimen/drug quarantine and HemOnc resolution | `mapping.therapy` | therapy/regimen models, `RegimenMappingGap`, `Concept` |

The four areas share review and curation concerns, but do not share a single
schema or resolution algorithm.  Therapy and field mappings therefore must not
be routed through the source-code resolver.

## Implemented steps

1. Created the `omop_core.mapping` package with focused code-resolution,
   suggestions, field, and therapy modules.
2. Moved the source-code, suggestion, and therapy-resolution implementations
   to that package.
3. Updated API/FHIR orchestration and mapping management commands to use the
   canonical modules, without changing public routes or database behavior.
4. Retained legacy service imports as tested re-export shims.

## Next staged work

1. Extract the code-mapping and field-mapping HTTP handlers from the large
   `patient_portal.api.views` module into Mapping-owned API modules, keeping
   their current routes as contracts.
2. Group mapping UI screens and shared review controls under a Mapping feature
   directory, preserving existing URLs and user workflows.
3. Move remaining mapping-adjacent callers to canonical imports and remove
   shims only after a published deprecation window for external extensions.

No step creates a new database, Django app, or model ownership boundary unless
the product later requires independent deployment or persistence.
