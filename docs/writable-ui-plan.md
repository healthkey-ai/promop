# Writable UI — plan of record

Status as of 2026-08-23, against `dev` at `cefc2c0`.

This is the working plan for making the patient editor write. It exists so the
work can be picked up in another terminal, or by another person, without
reconstructing the reasoning. Update it as steps land.

---

## The one rule everything follows

`PatientRecord` is a **derived read model**. It has no writable clinical
columns, and `tests/test_patient_record_derive_only_contract.py` enforces that.
An edit is therefore never a PATCH of the projection — it is a write to the OMOP
fact underneath, after which derivation rebuilds the projection.

The server publishes what may be done with each field:

```
GET /api/v1/patient-records/writable-fields/     → 328 fields
```

Each entry carries a `kind`, a `writable` flag, and — when writable — everything
needed to construct the write. **The client never hardcodes a concept id**;
concepts move with vocabulary releases, and a copy in TypeScript would drift
silently and start writing facts against stale concepts.

| `kind` | count | meaning |
|---|---:|---|
| `unmapped` | 130 | no write path yet, grouped by *why* |
| `editable` | 66 | write one OMOP fact; **writable** |
| `authored` | 49 | derived from a grouping; author the resource instead |
| `computed` | 34 | calculated from other fields |
| `alias` | 20 | mirrors a canonical column |
| `profile` | 17 | lives on `Person`; 16 **writable** |
| `selectable` | 12 | a unit carried on the fact it qualifies |

**82 fields are writable.** Everything else renders read-only *with its reason* —
a field that is computed, or mirrors another column, is not "broken", and saying
so is the difference between a UI that looks unfinished and one that explains
itself.

### How a write leaves the client

`writeFieldValue(personId, field, descriptor, value)` dispatches on
`descriptor.target`:

| target | goes to |
|---|---|
| `measurement` / `observation` | `POST /api/v1/{measurements,observations}/`, superseding any same-day row via `is_erroneous` |
| `person` | `PATCH /api/v1/persons/{id}/`, keyed on `descriptor.payload_field` |

Editors must call the router rather than choosing a writer themselves.
`writable` alone does not say *where* a value goes — treating every writable
field as an OMOP fact is what sent profile edits to the observation endpoint
(fixed in #633).

---

## Where we are

### Done

| | |
|---|---|
| Descriptor endpoint + `useWritableFields` | covers all 328 fields |
| `writeFieldValue` router, `writeClinicalFact`, `writeProfileField` | #633 |
| Provider editor sends the edit, not the record | #627 |
| Federated view uses the same write path | #632 |
| Fail-closed when the descriptor can't be fetched | #632 |
| `clinicalTransport` so the remote uses the host's client | #632 |
| BloodTab, LabsTab converted | #622 and earlier |
| TreatmentTab converted (read-only, all 26 derived) | #637 |
| GeneralTab converted — first tab spanning both write targets | #645 |
| DiseaseTab converted; approved concept mappings now make fields writable | #647 |
| `POST /api/v1/therapy-lines/` — author a line | #639 |
| Therapy-line dialog with RxNorm picker | #641 |
| Regimen naming no longer mislabels a combination | #643 |

### Tab status

| tab | state | fields | writable |
|---|---|---:|---:|
| BloodTab | descriptor-driven | 23 | 23 |
| LabsTab | descriptor-driven | 32 | 29 |
| TreatmentTab | descriptor-driven | 26 | 0 — all authored |
| GeneralTab | descriptor-driven | 30 | 16 |
| DiseaseTab | descriptor-driven | 82 | 18 |
| **BehaviorTab** | **not converted** | 27 | **1** |
| WearableTab | not converted | 20 | 0 — all computed |

**1 writable field is unreachable today** (Step 3: `insurance_type` on
BehaviorTab): it sits on an unconverted tab, so editing it PATCHes the projection
and returns 405. That is the headline number this project is closing — 32 before
Step 1, 16 after it.

---

## Remaining work

Steps are independent — take them in any order, one PR each. **Tick a box when
its PR merges**, and note the PR number beside it, so anyone picking this up
sees the real state rather than the intended one.

- [x] **Step 1** — Convert GeneralTab (16 writable) — #645
- [x] **Step 2** — Convert DiseaseTab (15 writable, +3 SCT) — #647
- [ ] **Step 3** — Convert BehaviorTab (1 writable)
- [ ] **Step 4** — Convert WearableTab (0 writable, read-only)
- [ ] **Step 5** — Surface 13 writable fields no tab shows
- [ ] **Step 6** — Concept assignment for the 133 unmapped fields (#595, curation)

### Step 1 — Convert GeneralTab (16 writable)

The most valuable single step, and the only one covering **both** write targets.

- `target: person` — `gender`, `race`, `ethnicity`, `city`, `country`, `region`,
  `email`
- `target: measurement` — `weight`, `height`, `systolic_blood_pressure`,
  `diastolic_blood_pressure`, `heartrate`
- also writable: `ecog_performance_status`, `karnofsky_performance_score`,
  `stage`, `histologic_type`

Watch for:
- Demographics are **selectable**: the descriptor carries curated `options` for
  gender/race/ethnicity. Render those, not a free-text box, and send the
  `value` — the server resolves the concept.
- `date_of_birth` is `profile` but **not** writable (`fill_if_empty`): the
  endpoint fills a blank and silently leaves an existing value alone, so a box
  that appeared to accept a correction would lie about the outcome.
- Vitals need a result date, like BloodTab's.

**Done when**: every writable field on the tab saves and re-derives; every other
field renders read-only with its reason; the descriptor failing to load leaves
the tab read-only rather than editable.

### Step 2 — Convert DiseaseTab (15 writable)

Biomarkers and staging: `estrogen_receptor_status`, `progesterone_receptor_status`,
`her2_status`, `androgen_receptor_status`, `ki67_proliferation_index`,
`beta2_microglobulin`, `stage`, `tumor_stage`, `nodes_stage`,
`distant_metastasis_stage`, `histologic_type`, `bone_only_metastasis_status`,
`largest_lymph_node_size`, `report_interpretation`, `test_specimen_type`.

71 fields total, so most render read-only. Note `tnbc_status` is computed from
the three receptor statuses — both hosts derive it client-side in
`handleFieldChange`; leave that alone, it is display-only.

### Step 3 — Convert BehaviorTab (1 writable)

`insurance_type` only. Small, but the tab currently offers 27 boxes of which 26
cannot save.

### Step 4 — Convert WearableTab (0 writable)

Everything is a 30-day aggregate over device readings. Same shape as
TreatmentTab: read-only with one explanation, since a clinician does not type a
median. Consider what "upload device data" should link to.

### Step 5 — Surface 13 writable fields that no tab shows

Writable, mapped, and invisible:

- `target: person` (9): `facility_name`, `phone_number`, `postal_code`,
  `latitude`, `longitude`, `validated`, `validated_by`, `validation_date`,
  `suppress_demographics_for_others`
- `target: measurement` (4): `lymph_node_status`, `metastasis_status`,
  `pd_l1_combined_positive_score`, `pd_l1_ic_percentage`

The person fields are a profile/settings surface rather than a clinical tab. The
four measurements belong on DiseaseTab — fold them into step 2.

### Step 6 — The 133 unmapped fields

| group | count | what is missing |
|---|---:|---|
| `needs-concept-set` | 131 | a concept assignment per field |
| `wearable-metadata` | 2 | device provenance, not a clinical fact |

This is issue **#595** — the concept-mapping interface (`/field-mappings`) that
lets a curator assign concepts.

**As of #647 this is wired up**: `build_writable_field_descriptor` reads approved
`FieldConceptMapping` rows and emits an `editable` entry from each, so a field
that gains a complete mapping becomes writable with no further UI work. Before
that the table recorded decisions and nothing acted on them — its own docstring
said it "does NOT make the field writable".

A row has to carry a concept, an `omop_table` this can write to, and a
`source_value` for derivation to match on. Short of that it stays advisory, and
`makes_field_writable` on the mapping API says which side of the line it is on.

**This step is now curation, not code.**

---

## How to verify a conversion

1. **Unit** — mock the descriptor, assert a writable field renders a box and a
   non-writable one renders read-only *with its reason*; assert the tab fails
   closed when the descriptor fetch rejects.
2. **Live** — start the backend against `promop_dev`, edit a value, confirm the
   OMOP row lands and the canonical column *and its aliases* re-derive. The
   payload logic passing is not the same as the round trip working.
3. **Both suites, and the build.**

```bash
DATABASE_URL="postgresql://postgres@localhost:5432/promop_test" DEBUG=True \
  .venv/bin/python -m pytest -q
DATABASE_URL="postgresql://postgres@localhost:5432/promop_test" \
  .venv/bin/python manage.py test omop_core patient_portal --noinput
cd frontend && npm run lint && npx tsc --noEmit && npm test -- --run && npm run build
```

Clean up anything written to `promop_dev` afterwards.

---

## Traps already hit

Each of these cost real time. They are recorded so they cost nobody else.

- **Do not PATCH the record back.** Writing an OMOP fact re-derives the
  canonical column *and every alias and computed column downstream*. A payload
  captured before the write is stale, and the server reads a stale value as an
  attempted write to a read-only field — refusing the whole request while the
  OMOP row has already landed. Send only what changed, minus everything the
  descriptor knows, minus lifecycle columns. (#627)
- **Failing closed has to mean the whole save.** An empty descriptor correctly
  stops the OMOP writes, but it also makes `!(f in descriptors)` match
  everything — so the degraded path PATCHes the entire record. A descriptor you
  could not fetch means you cannot tell an OMOP-mapped column from one the
  record owns, so no PATCH is safe. (#632)
- **`writable` does not say where.** 16 writable fields target `person`, and
  routing them to `writeClinicalFact` posted an Observation with an undefined
  concept and never touched `Person` — without throwing. (#633)
- **`person_field` is prose, not a payload key.** It reads
  `"gender_concept + gender_source_value"`. Use `payload_field`. A key the
  endpoint ignores returns 200 and changes nothing.
- **The federated view is a second client.** It renders the same tab components
  through a host-injected axios instance. Anything reaching for the app's
  singleton breaks there — use `clinicalTransport`.
- **`npm run lint` is the only thing that catches `set-state-in-effect`.** It is
  an error, not a warning, and a red `dev` reddens every open PR at once. See
  the CLAUDE.md section. CI runs lint **and build**.
- **Do not `cat >` over an existing test file.** It silently destroys tests;
  check whether the file exists first.

---

## Open issues

- **#595** — concept-mapping interface, for the 131 `needs-concept-set` fields
- **#628** — closed by #633
- **#642** — closed by #643
