# Therapy Reference Tables Architecture

PROMOP stores curated therapy reference data for regimen selection, component expansion,
and therapy-line authoring. The reference set is loaded from curated CSVs generated from
the therapy spreadsheet. Regimens, components, and classes are linked to standard Athena
OMOP concepts where the curated source has an approved match, scoped to the target
diseases and treatment rounds PROMOP supports.

## Data Model

```text
TherapyRegimen (code, title, concept_id -> Concept)
  |
  +-- TherapyRegimenComponent
        |
        v
      TherapyComponent (code, title, concept_id -> Concept)
        |
        +-- TherapyComponentClassLink
              |
              v
            TherapyClass (code, title, concept_id -> Concept)

DiseaseTherapyRegimen (Disease, TherapyRound, TherapyRegimen)
TherapyRound (code, title)
```

The Django models live in `omop_core/models.py` and were introduced by migration
`0176_therapy_reference_tables.py`.

## Tables

| Model | Table | Purpose |
|---|---|---|
| `TherapyRegimen` | `therapy_regimen` | Pickable therapy/regimen names |
| `TherapyComponent` | `therapy_component` | Component drugs or ingredients |
| `TherapyClass` | `therapy_class` | Component classes |
| `TherapyRegimenComponent` | `therapy_regimen_component` | Regimen-to-component links |
| `TherapyComponentClassLink` | `therapy_component_class` | Component-to-class links |
| `TherapyRound` | `therapy_round` | Line/round vocabulary |
| `DiseaseTherapyRegimen` | `disease_therapy_regimen` | Disease and round filtered regimen options |

Core lookup models use stable slug-style `code`, display `title`, optional standard
Athena OMOP `concept_id`, and `llm_hint`.

## Data Loading

`manage.py generate_therapy_csvs` parses the source spreadsheet and resolves concepts
against the configured database. `manage.py load_therapy_reference_data` loads the
generated CSVs into the reference tables idempotently.

The generated CSVs are:

- `data/therapies_and_components.csv`
- `data/components_and_classes.csv`
- `data/disease_therapy_rounds.csv`

## API

The v1 API exposes reference data for the therapy-line editor:

```text
GET /api/v1/therapy-regimens/
GET /api/v1/therapy-regimens/{code}/
GET /api/v1/therapy-components/
GET /api/v1/therapy-classes/
```

`therapy-regimens` supports disease and round filtering. Regimen detail includes nested
components and classes so the client can populate the picker without making per-component
requests.

## Frontend Contract

`frontend/src/types/therapy.ts` defines the TypeScript response shapes. The therapy line
dialog uses `frontend/src/api/therapyLines.ts` to search, list, and fetch regimen detail.

## Operational Notes

The reference tables are curated lookup data, not patient clinical facts. Patient therapy
lines are authored through the clinical therapy-line endpoints, using these tables as the
selection surface.
