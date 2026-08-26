# Therapy Reference Tables & Regimen Picker — Implementation Plan

## Context

PROMOP needs reference tables for therapies (regimens), their component drugs,
and drug classes so that curators can edit therapy lines by picking from a
regimen list. The source spreadsheet (`Therapies_Therapy Comp_Therapy Types.xlsx`,
Rev.11) contains 239 therapies, 181 components, and 88 categories with disease
and therapy-line associations.

CancerBot has a similar model structure that we are aligning with, but PROMOP
uses Athena OMOP concept_ids (HemOnc for regimens/classes, RxNorm for drug
ingredients) rather than internal-only codes.

Staging already has HemOnc loaded (5,888 Regimens, 820 Components, 508
Component Classes) and RxNorm (148,875 concepts) in the Concept table. We
auto-resolve concept_ids by name-matching against these during CSV generation.

---

## Data Model

Three core tables + two join tables + one disease-round linking table:

```
TherapyRegimen (code, title, concept_id → Concept)
    │
    └── TherapyRegimenComponent (join: regimen_id, component_id)
            │
TherapyComponent (code, title, concept_id → Concept)
    │
    └── TherapyComponentClass (join: component_id, class_id)
            │
TherapyClass (code, title, concept_id → Concept)

DiseaseTherapyRegimen (disease, round, therapy → TherapyRegimen)
```

All three core tables have:
- `code` (unique, slug-style from spreadsheet)
- `title` (human-readable display name)
- `concept_id` (nullable FK to `Concept` — HemOnc or RxNorm)

---

## Issues

| # | Title | Scope | Depends on |
|---|-------|-------|------------|
| #763 | Create Django models for therapy reference tables | `omop_core/models.py` + migration | — |
| #767 | Parse therapy spreadsheet and generate seed CSVs | Management command + CSVs | #763 |
| #764 | Create management command to load therapy reference CSV data | Management command | #763, #767 |
| #765 | Add API endpoints for therapy reference data | Views + URLs | #763 |
| #766 | Add frontend TypeScript types for therapy reference data | `frontend/src/types/therapy.ts` | #765 |

---

## Implementation Order

All issues on a single branch, one PR:

| Step | Issue | Description |
|------|-------|-------------|
| 1 | #763 | Models + migration |
| 2 | #767 | Parse spreadsheet → CSVs |
| 3 | #764 | Load CSVs → DB management command |
| 4 | #765 | API endpoints |
| 5 | #766 | Frontend types |

---

## Models (Issue #763)

1. **TherapyRegimen** — `code`, `title`, `concept_id` (FK Concept, nullable), `llm_hint`
2. **TherapyComponent** — `code`, `title`, `concept_id` (FK Concept, nullable), `llm_hint`
3. **TherapyClass** — `code`, `title`, `concept_id` (FK Concept, nullable), `llm_hint`
4. **TherapyRegimenComponent** — `regimen` (FK), `component` (FK); unique_together
5. **TherapyComponentClassLink** — `component` (FK), `therapy_class` (FK); unique_together
6. **TherapyRound** — `code`, `title`
7. **DiseaseTherapyRegimen** — `disease` (FK Disease), `round` (FK TherapyRound), `regimen` (FK TherapyRegimen); unique_together

DB table names: `therapy_regimen`, `therapy_component`, `therapy_class`,
`therapy_regimen_component`, `therapy_component_class`, `therapy_round`,
`disease_therapy_regimen`

---

## API Endpoints (Issue #765)

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/v1/therapy-regimens/` | List all regimens (filterable by `?disease=MM&round=first_line_therapy`) |
| GET | `/api/v1/therapy-regimens/<code>/` | Single regimen with nested components and classes |
| GET | `/api/v1/therapy-components/` | List all components |
| GET | `/api/v1/therapy-classes/` | List all classes |

Regimen detail response shape:
```json
{
  "code": "r_chop",
  "title": "R-CHOP",
  "concept_id": 35805028,
  "components": [
    {
      "code": "rituximab",
      "title": "Rituximab",
      "concept_id": 35803296,
      "classes": [
        {"code": "anti_cd20_monoclonal_antibody", "title": "Anti-CD20 Monoclonal Antibody", "concept_id": 912007}
      ]
    }
  ]
}
```

---

## Critical Files

| File | Changes |
|------|---------|
| `omop_core/models.py` | Add 7 new models |
| `omop_core/migrations/` | Schema migration for new tables |
| `omop_core/management/commands/generate_therapy_csvs.py` | New — spreadsheet parser + concept resolver |
| `omop_core/management/commands/load_therapy_reference_data.py` | New — CSV loader |
| `data/therapies_and_components.csv` | Generated — therapy→component links with concept_ids |
| `data/components_and_classes.csv` | Generated — component→class links with concept_ids |
| `data/disease_therapy_rounds.csv` | Generated — disease+round→therapy links |
| `patient_portal/api/views.py` | Therapy reference API views |
| `patient_portal/api/v1_urls.py` | URL routing for new endpoints |
| `frontend/src/types/therapy.ts` | TypeScript interfaces |

---

## Verification

```bash
# Generate CSVs (requires staging DB for concept resolution)
DATABASE_URL="${STAGING_DATABASE_URL:-$DATABASE_URL}" \
  .venv/bin/python manage.py generate_therapy_csvs \
    --input ~/Downloads/Therapies_Therapy\ Comp_Therapy\ Types.xlsx

# Load into test DB
DATABASE_URL="postgresql://postgres@localhost:5432/promop_test" \
  .venv/bin/python manage.py load_therapy_reference_data

# Run tests
DATABASE_URL="postgresql://postgres@localhost:5432/promop_test" \
  .venv/bin/python manage.py test omop_core patient_portal --verbosity=2 --noinput

# Verify API
curl -s localhost:8000/api/v1/therapy-regimens/?disease=MM | python -m json.tool | head -30
```
