# Concept to Code Mapping + Graph Visualization

## Context

The code mapping page (`/code-mappings`) currently works in one direction:
source code → concept ("Code to Concept"). Curators browse source codes by
vocabulary tab, see/create mappings to standard OMOP concepts, and run the
suggest pipeline to propose destinations.

We need the reverse direction: **Concept to Code**. Start from standard OMOP
concepts that have `FieldConceptMapping` rows (i.e., concepts that flow into
`PatientRecord` on ingestion) and find/manage source codes that map to each
concept. This gives curators a way to ensure completeness — every clinically
relevant concept should have good source code coverage.

The concept-to-code direction has a manageable number of nodes (~60-80
field-mapped concepts across 5 domains), making it a candidate for graphical
display. A React Flow graph view will complement the table view, showing
concept hierarchy, approved mappings, and proposed mappings visually.

Storage remains `SourceCodeConceptMapping` (SCCM) — mappings created from
either direction are SCCM rows.

---

## Phase 1: Table-based Concept to Code Tab

### 1.1 Database Migration — Trigram Index on SCCM Descriptions

The reverse lexical search needs to trigram-match on `source_code_description`.
Add a GIN index.

**File:** `omop_core/models.py` — add to `SourceCodeConceptMapping.Meta.indexes`:
```python
GinIndex(
    OpClass(Upper('source_code_description'), name='gin_trgm_ops'),
    name='ix_sccm_desc_upper_trgm',
)
```

**File:** `omop_core/migrations/NNNN_sccm_description_trigram_index.py` —
generated via `makemigrations`

Also add `direction` field to `SuggestRun`:
```python
direction = models.CharField(
    max_length=10, default='forward',
    choices=[('forward', 'Forward'), ('reverse', 'Reverse')],
)
```

### 1.2 Backend: Browse Endpoints

**File to modify:** `patient_portal/api/views.py`

**Endpoint 1 — `GET /api/v1/concept-to-code/`**
- Queries `FieldConceptMapping.objects.select_related('concept')`
- Filters: `?domain=` (omop_table), `?search=` (concept_name/field_name),
  `?status=` (FCM status)
- Annotates SCCM counts per concept (approved/proposed/rejected) via subquery
  on `ix_sccm_target_status`
- Returns domain summary tabs with concept counts
- Response includes: field_name, concept_id, concept_name, concept_code,
  vocabulary_id, domain_id, omop_table, fcm_status, sccm_counts,
  total_source_codes

**Endpoint 2 — `GET /api/v1/concept-to-code/<concept_id>/`**
- Returns all SCCM rows where `target_concept_id=concept_id`, using existing
  `_serialize_code_mapping_row`
- Efficient via `ix_sccm_target_status` index

**File to modify:** `patient_portal/api/v1_urls.py` — register both URLs

### 1.3 Backend: Reverse Suggest Pipeline

**New file:** `omop_core/mapping/reverse_suggestions.py`

Retrieval tiers (mirror the forward pipeline in
`omop_core/mapping/suggestions.py`):

1. **`reverse_umls_candidates(concept)`** — concept → vocabulary_id +
   concept_code → UmlsSourceCode lookup → CUI(s) → sibling source codes in
   other vocabularies → match against SCCM rows without a target
2. **`reverse_lexical_candidates(concept_name, concept_synonyms, limit)`** —
   concept name + ConceptSynonym names → trigram search on SCCM
   `source_code_description` (uses new `ix_sccm_desc_upper_trgm` index) →
   candidate unmapped SCCM rows
3. **`reverse_rank_candidates(concept, source_candidates)`** — reversed LLM
   prompt: "Given this standard concept, which source codes match?" Same
   claude-opus-5 call, reversed system prompt
4. **`reverse_retrieval_pool(concept, strategies, lexical_limit)`** — combines
   tiers, applies vector rerank if embeddings available

**New file:** `omop_core/services/reverse_suggest_jobs.py`
- Mirrors `suggest_jobs.py` dispatcher pattern (Celery/Inline/Fake)
- Uses same `SuggestRun` model with `direction='reverse'`
- `execute_reverse_run()` iterates concept_ids, runs reverse retrieval +
  ranking, writes proposed SCCM rows

### 1.4 Backend: Reverse Suggest API

**File to modify:** `patient_portal/api/views.py`

**Endpoint — `POST /api/v1/concept-to-code/suggest/`**
- Accepts `{"concept_ids": [3000963, ...], "strategies": ["umls", "lexical"],
  "limit": 10}`
- Returns 202 with `run_id`, same polling pattern as forward suggest
- Poll via `GET /api/v1/concept-to-code/suggest-runs/<uuid:run_id>/`

**File to modify:** `patient_portal/api/v1_urls.py` — register suggest URLs

### 1.5 Frontend: ConceptToCodeTab Component

**New file:** `frontend/src/components/CodeMappings/ConceptToCodeTab.tsx`

Extracted as a separate component (CodeMappingPage.tsx is already 2320 lines).

Structure:
1. **Domain selector** — horizontal pills: Measurement, Observation, Condition,
   Drug Exposure, Procedure
2. **Concept table** — columns: Concept Name, Concept Code, Vocabulary, Field
   Name, FCM Status, Approved/Proposed/Rejected counts. Clickable rows.
3. **Detail panel** (on concept click) — expands to show:
   - Concept metadata
   - Existing SCCM rows targeting this concept (reuses `CodeMappingRow`
     interface)
   - "Reverse Suggest" button → triggers suggest endpoint
   - Progress indicator (same polling as forward suggest)
4. **Search** — filters concept list

**File to modify:** `frontend/src/components/CodeMappings/CodeMappingPage.tsx`

Add a top-level toggle above the existing vocabulary tabs:
```tsx
const [viewMode, setViewMode] = useState<
  "source-to-concept" | "concept-to-code"
>("source-to-concept");
// Render ConceptToCodeTab when concept-to-code is selected
```

### 1.6 Tests

**New file:** `tests/test_concept_to_code.py`
- `ConceptToCodeListTest` — domain filtering, search, SCCM count annotation,
  pagination
- `ConceptToCodeDetailTest` — SCCM rows for a concept, query efficiency
- `ReverseSuggestTest` — reverse lexical candidates, reverse UMLS candidates,
  suggest endpoint 202, polling

**New file:** `frontend/src/components/CodeMappings/ConceptToCodeTab.test.tsx`
- Domain selector renders and switches
- Concept list loads and displays
- SCCM counts shown
- Detail panel expands on click
- Reverse suggest triggers correctly

---

## Phase 2: React Flow Graph Visualization

### 2.1 Install React Flow

```bash
cd frontend && npm install @xyflow/react
```

Consider lazy loading with `React.lazy()` since this is admin-only.

### 2.2 Backend: Combined Graph Endpoint

**File to modify:** `patient_portal/api/views.py`

**Endpoint — `GET /api/v1/concept-to-code/<concept_id>/graph/`**

Merges two existing API calls into one:
- Ancestors (1-2 levels) from `ConceptAncestor` (reuses
  `_query_concept_graph()`)
- SCCM rows targeting this concept
- Returns combined payload for graph rendering

### 2.3 Frontend: Graph Components

**New file:** `frontend/src/components/CodeMappings/ConceptGraphNodes.tsx`

Custom React Flow node components:
- `ConceptNode` — blue card: concept_name, concept_code, vocabulary badge
- `AncestorNode` — gray card: parent concept name, relationship
- `SourceCodeNode` — green (approved) / amber (proposed) / red (rejected):
  source_code, description, status badge

**New file:** `frontend/src/components/CodeMappings/useConceptGraphData.ts`

Custom hook:
- Takes `concept_id`, fetches `/concept-to-code/<id>/graph/`
- Converts to React Flow `Node[]` and `Edge[]`
- Applies dagre layout (hierarchical top-to-bottom)
- Returns `{ nodes, edges, loading }`

**New file:** `frontend/src/components/CodeMappings/ConceptGraph.tsx`

Main graph component using `@xyflow/react`:
- Renders nodes and edges from `useConceptGraphData`
- Edge types: hierarchy (dashed gray), approved mapping (solid green), proposed
  mapping (dashed amber)
- Pan/zoom enabled
- Click-to-select: clicking a node calls `onSelectConcept` callback
- "Suggest Source Codes" button appears when concept selected → triggers
  reverse suggest → new nodes animate in

### 2.4 Toggle Integration

**File to modify:** `frontend/src/components/CodeMappings/ConceptToCodeTab.tsx`

Add Table/Graph toggle within the Concept to Code tab:
```tsx
const [graphView, setGraphView] = useState(false);
// Toggle button with table/graph icons
// selectedConcept state shared between both views
```

### 2.5 Graph Tests

**New file:** `frontend/src/components/CodeMappings/ConceptGraph.test.tsx`
- Renders concept, ancestor, and source code nodes
- Distinguishes approved vs proposed edge styles
- Click-to-select fires callback
- Suggest button triggers reverse suggest

---

## Key Files Reference

| Concern | File |
|---|---|
| Existing forward suggest pipeline | `omop_core/mapping/suggestions.py` |
| Existing suggest job dispatching | `omop_core/services/suggest_jobs.py` |
| Existing code mapping views | `patient_portal/api/views.py` (lines ~10200+) |
| Existing concept graph query | `patient_portal/api/views.py` → `_query_concept_graph()` |
| v1 URL registration | `patient_portal/api/v1_urls.py` |
| SCCM model + indexes | `omop_core/models.py` (lines ~1805-2040) |
| FieldConceptMapping model | `omop_core/models.py` (lines ~3600-3698) |
| ConceptAncestor model | `omop_core/models.py` (line ~714) |
| Frontend code mapping page | `frontend/src/components/CodeMappings/CodeMappingPage.tsx` |
| Frontend routing | `frontend/src/App.tsx` |

## Verification

### Phase 1
1. Run migration, verify trigram index exists:
   `\di ix_sccm_desc_upper_trgm` in psql
2. Browse endpoint:
   `GET /api/v1/concept-to-code/?domain=measurement` returns field-mapped
   concepts with SCCM counts
3. Detail endpoint:
   `GET /api/v1/concept-to-code/<id>/` returns SCCM rows
4. Reverse suggest:
   `POST /api/v1/concept-to-code/suggest/` returns 202, poll completes, new
   SCCM rows created
5. Frontend: toggle to "Concept to Code", select domain, click concept, see
   mappings, run suggest
6. Run both backend test suites + frontend tests

### Phase 2
1. Graph renders with concept nodes, ancestor hierarchy, and source code edges
2. Toggle between table and graph preserves selection
3. Suggest from graph adds new proposed mapping nodes
4. React Flow pan/zoom works smoothly
5. All tests pass
