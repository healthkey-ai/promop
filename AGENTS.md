# AGENTS.md — instructions for coding agents on promop

This is the one instruction file for every coding agent that works on this
repository: Claude Code, Codex, Gemini, Cursor and whatever else people use.
`CLAUDE.md` is only `@AGENTS.md` plus a short Claude Code section, so Claude
Code reads this file through the import; other tools read it directly. Edit
this file, never a copy of it in `CLAUDE.md`. Personal additions that should
not be committed (how to run tests on your machine, your sandbox URLs) go in
`CLAUDE.local.md`, which is gitignored and loads alongside `CLAUDE.md`.

## Two databases in `.env`

| `.env` variable | Database | Render services | Use |
|---|---|---|---|
| `PROD_DATABASE_URL` | **Production** | `promop`, `promop-worker` (branch `main`) | read-only checks and verification |
| `STAGING_DATABASE_URL` | **Staging** | `promop-staging`, `promop-staging-worker` (branch `dev`) | migrations, data fixes, investigation |

`.env` deliberately sets **no `DATABASE_URL`** — the one variable Django reads. Every command
names its database by setting `DATABASE_URL` itself:

| Target | Set |
|---|---|
| Local tests | `DATABASE_URL="postgresql://postgres@localhost:5433/promop_test"` |
| Local development | `DATABASE_URL="postgresql://postgres@localhost:5433/promop_dev"` |
| Staging | `DATABASE_URL="${STAGING_DATABASE_URL:?}"` |
| Production (read-only) | `DATABASE_URL="${PROD_DATABASE_URL:?}"` |

A command that forgets fails safe: without `DEBUG=True`, settings refuse to load ("DATABASE_URL
must be set"); with it, Django falls back to a local SQLite file. **Run local tests only with
`DATABASE_URL` pointing at local PostgreSQL** — never leave it exported at staging or production
in a shell you test from, because pytest and `manage.py test` create their test database on
whatever server it names. Never write to production data — production is loaded from a staging
dump, so fix and verify on staging; schema changes reach production only through deploys
(`start.sh` migrates). Details: [Database Selection Rule](#database-selection-rule).

---

## Write Architecture: PatientRecord First

**Read [`docs/patient-record-first-writes.md`](docs/patient-record-first-writes.md)
before changing anything that writes patient data.** It is the architecture
document for interactive edits, and the rules below assume it.

- **A UI edit is written to `PatientRecord` first.** The value lands on the
  record immediately and is tracked in `user_edited_fields`.
- **It then feeds forward into the OMOP tables** as a side effect, through the
  field's descriptor projection (`project_single_value`), when the field has a
  built-in recipe or an approved `FieldConceptMapping`.
- **There is no immediate re-derive back into `PatientRecord`.** Direct UI saves
  do not call `refresh_patient_record()`; the record already holds the value.
  A later sync or derivation pass may round-trip OMOP facts back into the
  record, and `user_edited_fields` is what keeps a pending edit from being
  overwritten when it does.
- **Ingest goes the other way.** The ETL, FHIR sync and bulk OMOP endpoints
  write OMOP facts and derive the record from them. See
  [FHIR Upload Architecture](#fhir-upload-architecture) below.
- **Lab derivation routes through the mapping tables.** Which LOINC code feeds
  which `PatientRecord` lab field is read from the approved, concept-resolved
  `FieldConceptMapping` per field plus its approved `SourceCodeConceptMapping`
  LOINC aliases (`resolve_lab_code_fields` in `patient_record_service.py`),
  once per refresh, by every lab reader: the extractor, the FLC pairing, the
  SLiM scan, the blood-count selection and the provenance registry.
  `_LOINC_LAB_FIELDS` in that module is the inventory those rows are seeded
  from (migrations 0240, 0285, 0327) and decides only for a field with no such
  mapping, so adding a lab field means adding it there **and** to
  `omop_core/data/field_curation_v1.json`: a fresh install seeds the row from
  the fixture (with no concept for a row whose concept is not loaded yet,
  since migrations run before the full vocabulary loads;
  `load_athena_vocabularies` resolves the seeded rows' concepts at the end of
  every successful load -- a scheduled sync whose archive adds nothing does not
  keep it -- and `manage.py resolve_field_concept_mappings` does it for an
  instance that loaded earlier; on a populated instance follow it with
  `backfill_patient_records --all`), while an existing instance
  (0240 is one-shot) derives it from the table until a curator approves it,
  `copy_reference_data` brings it over, or a later migration approves it as
  0327 does. A new alias code is an approved SCCM row, not a table entry. The
  FHIR bundle generators read the table directly: they run before import with
  no database in reach. See
  [field_concept_mapping_architecture.md](docs/field_concept_mapping_architecture.md#lab-derivation-reads-the-mapping-tables).

---

## Project Overview

### Staging deployment

Use Render staging for investigation and verification: service `promop-staging`
in Oregon at https://promop-staging.onrender.com, deployed from `dev`. Cloud Run
access is not a genomics delivery gate. Preserve the existing Cloud Run resource
and integration identifiers in `.github/workflows/deploy-staging.yml` and
`Dockerfile.gcp`.
Use `STAGING_DATABASE_URL` from `.env` for Render staging database access; the Render
staging web and worker processes call that same database `DATABASE_URL`. See
[Two databases in `.env`](#two-databases-in-env).
Never put the database connection string or credentials in Git or tool output.
`render.yaml` declares both production and staging; staging has its own worker
and broker. A Blueprint sync applies infrastructure settings; a code-only
redeploy does not. See `docs/render-staging-celery.md`.

**promop** is a Django + React application that:
- Stores clinical oncology patient data in an OMOP CDM–aligned PostgreSQL schema
- Exposes a DRF REST API consumed by a React TypeScript frontend
- Loads patient data as OMOP rows through the bulk OMOP endpoints, written by a
  separate ETL project that reads FHIR itself; promop's own FHIR R4 Bundle
  upload is **optional** and is not on that path
- Generates synthetic FHIR patient data for testing via a Django management command
- Deploys to Render (backend) with PostgreSQL

**Key tech:**
- Backend: Django 5.x, Django REST Framework, PostgreSQL (psycopg[binary] 3.x)
- Frontend: React 18, TypeScript, Tailwind CSS
- Data: OMOP CDM v5.4, FHIR R4

---

## Stack File Map

| Concern | File(s) |
|---|---|
| OMOP data model | `omop_core/models.py` — `PatientRecord`, `Person`, `PersonLanguageSkill` |
| DB migrations | `omop_core/migrations/` — use standard Django ORM migrations (`makemigrations`) |
| DRF serializers | `patient_portal/api/serializers.py` — `PatientRecordSerializer` (uses `fields = '__all__'`) |
| Backend API views | `patient_portal/api/views.py` — `PatientRecordViewSet`, FHIR upload handler |
| API URL routing | `patient_portal/api/urls.py` (legacy) and `patient_portal/api/v1_urls.py` (current) |
| TypeScript types | `frontend/src/types/patient.ts` — `PatientInfo` interface |
| React UI tabs | `frontend/src/components/PatientInfo/` — `GeneralTab.tsx`, `LabsTab.tsx`, `MultipleMyelomaTab.tsx`, `FollicularLymphomaTab.tsx` |
| Patient detail page | `frontend/src/components/Patient/PatientDetail.tsx` |
| API client | `frontend/src/utils/api.ts` and `frontend/src/api/axios.ts` |
| FHIR bundle generator | `omop_core/management/commands/generate_fhir_bundle.py` — `--disease breast-cancer\|mm\|fl` |
| FHIR bundle importer | `omop_core/management/commands/import_fhir_bundle.py` |
| FHIR upload handler | `patient_portal/api/views.py` — `upload_fhir_bundle` function/view |
| SCT sample data seeder | `omop_core/management/commands/populate_sct_sample_data.py` |
| SCT migration audit | `omop_core/management/commands/audit_sct_history.py` |
| Retired mapping destinations audit | `omop_core/management/commands/audit_retired_mapping_destinations.py` — read-only, exits 1 when any are found |
| Backend tests | `omop_core/tests.py`, `patient_portal/tests.py` |
| React tests | `frontend/src/App.test.tsx`, `frontend/src/components/**/*.test.tsx` |

---

## API URL Versioning

The patient record endpoint exists at two paths with different stability guarantees:

| Path | Status | Use |
|---|---|---|
| `/api/patient-info/` | **Legacy — do not use for new work** | Retained for backwards compatibility with the federation host app (`ht-phr`) and the existing React frontend. Wire-format response keys (`patient_info`, `patient_info_id`) are also frozen. |
| `/api/v1/patient-records/` | **Current — use for all new integrations** | New clients and any existing clients migrating off the legacy path should use this prefix. |

Both paths route to `PatientRecordViewSet` and return identical data. The only difference is the URL prefix. The legacy path will remain functional until all consumers have migrated.

**Rule:** New endpoints, filters, and actions go on `v1_urls.py` only. Do not extend the legacy `urls.py` registration.

---

## Rule: Adding a New Patient Attribute

When adding any new field to `PatientRecord`, you **must** touch layers 1, 2, 3,
5, 7 and 8 below. Do not skip any of them.

Layer 6 applies only to a field that belongs to some diseases and not to
every patient.

Layers 4 and 9 are the built-in FHIR uploader and its synthetic-bundle
generator. **They are optional.** Production data is loaded as OMOP rows by a
separate ETL project that reads FHIR itself, through the bulk OMOP endpoints,
and never through `upload_fhir_bundle`. Touch 4 and 9 only when the field should
also be populated by that uploader (for example, for the synthetic test data).
A field a patient enters in the UI is written to `PatientRecord` first and fed
forward into OMOP, so it needs no FHIR mapping at all; see
[Write Architecture: PatientRecord First](#write-architecture-patientrecord-first).

### 1. Django Model (`omop_core/models.py`)

Add the field to the `PatientRecord` class with `blank=True, null=True` unless there is a specific reason for it to be required.

```python
# Example
new_field = models.TextField(blank=True, null=True, help_text="Description of the field")
```

### 2. Database Migration (`omop_core/migrations/`)

Use standard Django migrations generated by `makemigrations`:

```bash
.venv/bin/python manage.py makemigrations omop_core
```

The migration also adds the field to `patient_record_provisional` and, if derivation
owns it (`omop_core/derived_fields.py`) and it is not JSON, to
`patient_record_confidence`: both tables are generated from `PatientRecord`, so
there is nothing to hand-write, but the migration has three `AddField`s, not one
([provisional data plan](docs/provisional_data_plan.md)).

Then apply to the staging DB before committing, naming it explicitly:
```bash
DATABASE_URL="${STAGING_DATABASE_URL:?STAGING_DATABASE_URL is not set}" \
  .venv/bin/python manage.py migrate
```

<a id="secret-key-on-a-shared-database"></a>
Settings will not load without `SECRET_KEY` either. This command leaves it to
`.env` — deliberately, because against a shared database it has to be **the
deployment's** key. `.env` holds two deployments' DSNs but one `SECRET_KEY`, so it
can be right for at most one of them: confirm it belongs to the deployment you are
writing to. `AUDIT_HMAC_KEY` and
`EXPORT_SIGNING_KEY` fall back to `SECRET_KEY` when unset, so a placeholder writes
audit rows and export signatures the deployed service cannot verify, and
`SECRET_KEY="$SECRET_KEY"` is worse than useless: the shell expands it to empty
before Django starts, and `load_dotenv()` does not override a variable that is
already set, so the command fails for exactly the people whose `.env` would have
worked. If your `.env` has no `SECRET_KEY`, export the deployment's before running.
Against a throwaway local database, `DEBUG=True` skips the guard and overrides
nothing — that is the form the test commands above use.

`start.sh` runs `scripts/prepare-deployment.sh`, which migrates, on every Render deploy, so migrations pushed to `main` are auto-applied in production.

### 3. DRF Serializer (`patient_portal/api/serializers.py`)

`PatientRecordSerializer` uses `fields = '__all__'`, so new model fields are **automatically included** in the API response. However:

- If the field needs **custom serialization** (e.g., computed values, nested data), add an explicit `SerializerMethodField`.
- If the field needs **write validation**, add it to `extra_kwargs`.

### 4. FHIR Upload Loader (`patient_portal/api/views.py` → `upload_fhir_bundle`) — optional

This is the function that parses FHIR Bundles posted to promop's own upload
endpoint. That endpoint is optional: the ETL that loads production data does not
use it. Add a mapping here only when the new field should also be populated by
this uploader; without one, the field is simply not populated from bundles
posted to it.

**Step A — Declare an initializer** at the top of the patient-processing block (so the variable always exists even if no matching FHIR resource is found):

```python
# Near the top of the patient block, alongside other initializations:
new_field_value = None
```

**Step B — Add LOINC/code mapping** in the observation-parsing loop:

```python
# In the for obs in observations: loop:
loinc_code = next(
    (c.get('code') for c in obs.get('code', {}).get('coding', [])
     if c.get('system') == 'http://loinc.org'),
    None
)
# ...existing elif chain...
elif loinc_code == 'XXXXX-X':  # Replace with the actual LOINC code
    new_field_value = (
        obs.get('valueQuantity', {}).get('value')
        or obs.get('valueString')
        or obs.get('valueCodeableConcept', {}).get('text')
    )
```

**Step C — Pass the value to `PatientRecord.objects.create()`** (or the `.save()` update block):

```python
PatientRecord.objects.create(
    person=person,
    # ...existing fields...
    new_field=new_field_value,
)
```

> **Note:** The FHIR upload loader is ~1375 lines. Search for the comment block that matches the clinical category of the new field (e.g., `# --- Biomarkers ---`, `# --- Labs ---`, `# --- Therapy ---`) to find the right place to insert steps A–C.

### 5. Backend API ViewSet (`patient_portal/api/views.py` → `PatientRecordViewSet`)

If the field needs special filtering, ordering, or search, add it to `filterset_fields`, `search_fields`, or `ordering_fields` in the viewset.

### 6. Disease scope (`FieldDiseaseMembership`) — only for a disease-specific field

A field with no membership rows is part of the **base record**, a field of
every patient; that is the default and needs nothing. A field that exists for
one disease, or a named few, gets one `FieldDiseaseMembership` row per Disease
Category code (#2331), written through
`omop_core/services/disease_profiles.py::set_field_diseases`, which refuses a
code that is not a `DiseaseCategoryChoice`. For a new built-in field on the
disease tab, add it to `SEED_DISEASE_FIELDS` there (a drift test holds that
list to `DiseaseTab.tsx`) **and** add the rows in a data migration that
declares its own frozen `FIELD_DISEASE_SEED`, as 0347 does: the seed migration
has already run on every existing instance, and a second test fails until
some migration writes the field. A field an org admin creates takes `diseases`
on `POST /api/v1/custom-patient-fields/` instead; from a patient's disease
tab the Add Field link preselects that record's Disease Category.
See [the architecture](docs/field_concept_mapping_architecture.md#whose-record-a-field-is-part-of-fielddiseasemembership).

### 7. TypeScript Type (`frontend/src/types/patient.ts`)

Add the field to the `PatientInfo` interface. All fields are optional (`?`):

```typescript
// In the appropriate section of the PatientInfo interface:
new_field?: string;         // for TextField / CharField
new_field?: number;         // for IntegerField / FloatField / DecimalField
new_field?: boolean;        // for BooleanField
new_field?: string | null;  // for nullable fields
```

### 8. React UI (`frontend/src/components/PatientInfo/`)

Add the field to the appropriate tab component. Choose the tab that matches the field's clinical category:

| Category | Component |
|---|---|
| Demographics, vitals, disease stage, ECOG/Karnofsky | `GeneralTab.tsx` |
| Labs, blood counts, chemistry, biomarkers | `LabsTab.tsx` |
| Myeloma-specific fields | `MultipleMyelomaTab.tsx` |
| Lymphoma-specific fields | `FollicularLymphomaTab.tsx` |
| CLL-specific fields | Add a `CLLTab.tsx` if needed |
| Breast cancer fields | Add a `BreastCancerTab.tsx` if needed |
| Behavior, lifestyle, socioeconomic | Add a `BehaviorTab.tsx` if needed |

Use the existing `FormField` and `Input`/`Select` UI primitives:

```tsx
// Text / number input:
<FormField label="New Field Label" htmlFor="new_field">
  <Input
    id="new_field"
    value={patient.new_field ?? ''}
    onChange={(e) => onUpdate({ new_field: e.target.value })}
  />
</FormField>

// Select / enum:
<FormField label="New Field Label" htmlFor="new_field">
  <Select
    id="new_field"
    value={patient.new_field ?? ''}
    onChange={(e) => onUpdate({ new_field: e.target.value })}
    options={[
      { value: 'option1', label: 'Option 1' },
      { value: 'option2', label: 'Option 2' },
    ]}
  />
</FormField>
```

Register the new tab in `frontend/src/components/Layout/TabBar.tsx` if a new tab is created. Wire the tab into `frontend/src/components/Patient/PatientDetail.tsx`.

### 9. FHIR Bundle Generator (`omop_core/management/commands/generate_fhir_bundle.py`) — optional

Only when layer 4 was done: add synthetic data generation for the new field. The generator creates FHIR Observation, Condition, or extension resources. Pick the right FHIR resource type:

- **Lab value / measurement** → FHIR `Observation` with a LOINC code
- **Clinical status / boolean** → FHIR `Observation` with a SNOMED code or extension
- **Demographic / free text** → FHIR `Patient` extension or `Observation`

```python
# Example: add an Observation for a new lab value
def _generate_new_field_observation(self, patient_id: str, encounter_id: str) -> dict:
    return {
        "resourceType": "Observation",
        "id": str(uuid.uuid4()),
        "status": "final",
        "category": [{"coding": [{"system": "http://terminology.hl7.org/CodeSystem/observation-category", "code": "laboratory"}]}],
        "code": {
            "coding": [{"system": "http://loinc.org", "code": "XXXXX-X", "display": "New Field Display Name"}],
            "text": "New Field Display Name"
        },
        "subject": {"reference": f"Patient/{patient_id}"},
        "encounter": {"reference": f"Encounter/{encounter_id}"},
        "valueQuantity": {
            "value": round(random.uniform(min_val, max_val), 1),
            "unit": "units",
            "system": "http://unitsofmeasure.org",
            "code": "unit_code"
        }
    }
```

Call the new generator method from the main patient generation loop and add it to the bundle entries list.

---

## Rule: Tests for Every New Attribute

For each new attribute, create tests that cover the model, the API and the
React UI. Add the FHIR upload test only when layer 4 was done. Add tests to the
relevant test files.

### Backend Tests (`omop_core/tests.py` and `patient_portal/tests.py`)

```python
from django.test import TestCase
from rest_framework.test import APITestCase, APIClient
from rest_framework import status
from django.contrib.auth.models import User
from omop_core.models import PatientRecord, Person

class NewFieldModelTest(TestCase):
    """Test that new_field exists in DB and model"""

    def setUp(self):
        self.person = Person.objects.create(person_id=9999)
        self.patient = PatientRecord.objects.create(
            person=self.person,
            new_field="test_value"
        )

    def test_new_field_saved_to_db(self):
        """Verify new_field is persisted to and retrieved from DB"""
        p = PatientRecord.objects.get(person_id=9999)
        self.assertEqual(p.new_field, "test_value")

    def test_new_field_nullable(self):
        """Verify new_field can be null"""
        self.person2 = Person.objects.create(person_id=9998)
        p = PatientRecord.objects.create(person=self.person2)
        self.assertIsNone(p.new_field)


class NewFieldAPITest(APITestCase):
    """Test that new_field is exposed via the REST API"""

    def setUp(self):
        self.user = User.objects.create_user(username='testuser', password='testpass')
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)
        self.person = Person.objects.create(person_id=9997)
        self.patient = PatientRecord.objects.create(
            person=self.person,
            new_field="api_test_value"
        )

    def test_new_field_in_api_response(self):
        """Verify new_field appears in GET /api/patients/{id}/"""
        response = self.client.get(f'/api/patients/{self.patient.id}/')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn('new_field', response.data)
        self.assertEqual(response.data['new_field'], "api_test_value")

    def test_new_field_writable_via_api(self):
        """Verify new_field can be updated via PATCH"""
        response = self.client.patch(
            f'/api/patients/{self.patient.id}/',
            {'new_field': 'updated_value'},
            format='json'
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.patient.refresh_from_db()
        self.assertEqual(self.patient.new_field, 'updated_value')

    def test_fhir_upload_populates_new_field(self):
        """Verify FHIR bundle upload correctly maps to new_field"""
        # Construct a minimal FHIR bundle containing a resource that maps to new_field
        fhir_bundle = {
            "resourceType": "Bundle",
            "type": "collection",
            "entry": [
                {
                    "resource": {
                        "resourceType": "Patient",
                        "id": "test-patient-1",
                        "name": [{"family": "Test", "given": ["Patient"]}],
                        "gender": "female",
                        "birthDate": "1970-01-01"
                    }
                },
                {
                    "resource": {
                        "resourceType": "Observation",
                        "status": "final",
                        "code": {
                            "coding": [{"system": "http://loinc.org", "code": "XXXXX-X"}]
                        },
                        "subject": {"reference": "Patient/test-patient-1"},
                        "valueQuantity": {"value": 42.0, "unit": "units"}
                    }
                }
            ]
        }
        response = self.client.post(
            '/api/fhir/upload/',
            fhir_bundle,
            format='json'
        )
        self.assertIn(response.status_code, [200, 201])
        # Verify the field was stored
        patient = PatientRecord.objects.filter(
            person__family_name='Test'
        ).first()
        self.assertIsNotNone(patient)
        self.assertEqual(patient.new_field, 42.0)
```

### React / Frontend Tests

Create or add to a test file alongside the component, e.g., `frontend/src/components/PatientInfo/LabsTab.test.tsx`:

```tsx
import React from 'react';
import { render, screen, fireEvent } from '@testing-library/react';
import LabsTab from './LabsTab';
import { PatientInfo } from '../../types/patient';

const mockPatient: PatientInfo = {
  id: 1,
  new_field: 42,
};

describe('LabsTab - new_field', () => {
  it('renders new_field value', () => {
    render(<LabsTab patient={mockPatient} onUpdate={jest.fn()} />);
    expect(screen.getByDisplayValue('42')).toBeInTheDocument();
  });

  it('calls onUpdate when new_field changes', () => {
    const onUpdate = jest.fn();
    render(<LabsTab patient={mockPatient} onUpdate={onUpdate} />);
    const input = screen.getByLabelText(/new field label/i);
    fireEvent.change(input, { target: { value: '55' } });
    expect(onUpdate).toHaveBeenCalledWith(expect.objectContaining({ new_field: '55' }));
  });
});
```

### Running Tests

`promop/settings.py` enforces the production configuration when `DEBUG` is not
true, so passing only `DATABASE_URL` exits before the suite starts — under the
Django runner with this exact error:

```
django.core.exceptions.ImproperlyConfigured: Missing required production settings:
  - SECRET_KEY must be set to a strong random value (current value is the insecure default)
```

`DEBUG=True` skips that guard and is what CI uses. A real `SECRET_KEY` satisfies
it instead, but only for `manage.py`: the host checks are skipped for management
commands and not under pytest, so that route additionally needs `ALLOWED_HOSTS`
and `CORS_ALLOWED_ORIGINS`. `DEBUG=True` is the simpler of the two, and the
commands below use it. Worth knowing because the suite does not *fail* without
it, it does not *start*, so the error reads like a broken environment rather
than a missing variable.

```bash
# Backend tests, Django runner — omop_core + patient_portal
DATABASE_URL="postgresql://postgres@localhost:5433/promop_test" DEBUG=True \
  .venv/bin/python manage.py test omop_core patient_portal --verbosity=2 --noinput

# Backend tests, pytest — the tests/ package
DATABASE_URL="postgresql://postgres@localhost:5433/promop_test" DEBUG=True \
  .venv/bin/python -m pytest -q

# Frontend tests (install deps first if needed: cd frontend && npm ci)
cd frontend && npm test -- --run
```

**There are two backend suites, and CI runs both.** Django's test runner
discovers only `omop_core.tests` and `patient_portal.tests`; the `tests/`
package is pytest-based and is invisible to it. CI runs them as isolated
parallel jobs, and the required `Backend tests` check passes only when both do.
The commands above are for running a suite, or part of one, locally — see the
rule below for when that is called for.

### Rule: Feature Branch + PR for Every Code Change

**Never commit directly to `dev` or `main`.** All fixes, enhancements, and refactors must go on a feature branch and land via a pull request.

1. File a GitHub issue describing the work item
2. `git checkout -b <descriptive-branch-name>` before writing any code
3. Commit the work on the feature branch
4. Run the targeted tests for the change (see below)
5. Open a PR targeting `dev`
6. Perform a code review on the PR. **Stop and present the review to the user before merging** — the user must read the review and approve the merge.
   - **Exception — small, local, low-risk fixes** (typos, docstring updates, single-line bug fixes, config tweaks): these may proceed all the way through to a merge into `dev` without waiting for user review, provided the code review found no unfixable issues.
7. **After the PR merges into `dev` successfully, delete the feature branch.** Prefer `gh pr merge --delete-branch`, which removes the remote branch as part of the merge. Then delete the local copy (`git branch -d <branch>`) and remove any worktree created for it (`git worktree remove <path>`). Do not leave merged feature branches lingering locally or on the remote.

### Rule: Unfinished Means Draft, Not a Title Prefix

**A PR that is not ready goes in GitHub's draft state** — `gh pr create --draft`,
or `gh pr ready --undo <number>`. Draft disables the merge button with no
override, which is stronger than any branch protection. A `[WIP]` prefix is
plain text: #1608, #1609 and #1654 all carried one, none were drafts, and all
three merged into `dev` anyway.

The `Merge readiness` check fails on such a title, and on a `do-not-merge`,
`wip` or `draft` label. It re-runs on `edited`/`labeled`, so clearing the marker
clears the failure without a push. `wip` and `draft` must be followed by a
delimiter or end the title, or "Wipe stale staging data" and "Drafting
instructions" would block the queue; the cost is that a bare "wip fix the thing"
passes. Requiring it is one context on ruleset 8217318, which covers `dev`
through `~DEFAULT_BRANCH`.

### Rule: Targeted Tests Before a Push, CI for the Full Suites

**GitHub CI is the gate for the full suites. Do not run the full Django or
pytest suite locally before a push.** CI runs both in parallel on every PR and
they are required checks, so a local full run repeats it serially — about 7
minutes against `promop_test`, which every local session shares, so two
sessions testing at once void each other's results.

Before pushing a change to code, tests, configuration, dependencies or runtime
data, run only:

- **The tests the change touches** — the test files you added or edited, and
  the ones covering the modules you changed:
  ```bash
  DATABASE_URL="postgresql://postgres@localhost:5433/promop_test" DEBUG=True \
    .venv/bin/python -m pytest -q tests/test_mapping_browse.py
  DATABASE_URL="postgresql://postgres@localhost:5433/promop_test" DEBUG=True \
    .venv/bin/python manage.py test patient_portal.tests.SomeTestCase --noinput
  ```
- **For frontend changes:** the affected component tests, plus `npm run lint`
  and `npm run build` (see "Frontend Lint" below — neither `tsc` nor the tests
  catch what lint does).

Do not push if any of those fail. Then **wait for CI to pass before merging**;
a red required check blocks the merge, and a failure there is fixed on the
branch and re-pushed, not reproduced first with a local full run.

Run a full suite locally only when CI cannot answer the question: reproducing a
CI failure that a targeted run does not show, or working on the test
infrastructure itself. Check `pgrep -fl "pytest|manage.py test"` first, run the
two suites sequentially, and run each once.

Documentation-only changes follow the exception under [Documentation-only
changes](#documentation-only-changes) below: review content, links and
`git diff --check`; application suites are not required.

**Local PostgreSQL setup** (one-time, postgresql@18 via Homebrew, port 5433):

The local server must be **PostgreSQL 18 with pgvector**, not the postgresql@14
this project used before. Two requirements force it:

* **pgvector.** `omop_core.models.ConceptEmbedding` declares a `vector(384)`
  column, and the pytest suite runs `--no-migrations` — the test database is
  built by reflecting model state, so `CREATE TABLE concept_embedding` runs
  before any fixture and fails with `type "vector" does not exist` on a server
  without the extension. That is a collection-time error on *every* pytest test,
  not a few. Homebrew's `pgvector` bottle builds only for postgresql@17 and @18,
  so @14 cannot have it.
* **CI parity.** CI's Postgres service is `pgvector/pgvector:0.8.6-pg18`,
  matching the local PostgreSQL major version and providing pgvector. A local
  server without pgvector fails tests that CI passes.

Port **5433**, because Postgres.app's PostgreSQL 14 commonly holds 5432 on this
machine. Nothing needs 5432 — set `DATABASE_URL` to 5433 everywhere locally.

```bash
brew install postgresql@18 pgvector
echo "port = 5433" >> /opt/homebrew/var/postgresql@18/postgresql.conf
brew services start postgresql@18

export PATH="/opt/homebrew/opt/postgresql@18/bin:$PATH"

# Create postgres role and databases (run once)
psql -p 5433 -d postgres \
  -c "CREATE ROLE postgres WITH SUPERUSER CREATEDB CREATEROLE LOGIN;"
psql -p 5433 -U postgres -d postgres \
  -c "CREATE DATABASE promop_test OWNER postgres;" \
  -c "CREATE DATABASE promop_dev OWNER postgres;"

# Enable pg_trgm AND vector on template1 — REQUIRED by the pytest suite.
# pytest runs with --no-migrations, so each test database is cloned from
# template1 and built by reflecting model state: concept's GIN trigram index and
# concept_embedding's vector(384) column are both created during CREATE TABLE,
# before any fixture could enable an extension. On template1 every clone already
# has them. Without pg_trgm: `operator class "gin_trgm_ops" does not exist`.
# Without vector: `type "vector" does not exist`.
psql -p 5433 -U postgres -d template1 \
  -c "CREATE EXTENSION IF NOT EXISTS pg_trgm" \
  -c "CREATE EXTENSION IF NOT EXISTS vector"

# Apply migrations
DATABASE_URL="postgresql://postgres@localhost:5433/promop_test" DEBUG=True \
  .venv/bin/python manage.py migrate --noinput

# Connect (use @18 bin directly — the system psql binary has an OpenSSL crash
# on this machine)
psql -p 5433 -U postgres -d promop_test
```

**Python 3.12 is also required.** `prolog` (the `prolog_surveys` app) declares
`requires-python >=3.12`, as do the pinned numpy/scipy builds, so a 3.11 venv
silently installs neither and Django fails at startup with
`ModuleNotFoundError: No module named 'prolog_surveys'`. CI uses 3.12.

```bash
brew install python@3.12
/opt/homebrew/opt/python@3.12/bin/python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

---

## Roles and what each reaches

`GroupAccess.role` has four values, and **roles are additive**: a user holds one
`GroupAccess` row per role at an org (unique on identity, org and role since
migration 0335, #2416), each row adds its own reach, and no role implies
another. Inviting an existing member with a second role adds a row; revoking a
row removes that role. The helpers in `omop_core/services/access.py` and
`omop_core/authorization.py` decide the reach; the frontend mirrors them in
`frontend/src/utils/roles.ts` for what the UI offers, and never decides alone.

| Role | Reaches |
|---|---|
| `org_admin` (a grant, or an admin path through an org or domain trust) | everything for its orgs: writes and approves mappings, administers access, uploads |
| `doctor` | patients, read and write (`can_access_patient`, `can_write_patient`, `get_visible_orgs`, `has_curation_access`), and **every mapping page read-only** (`has_mapping_read_access`, `_can_view_mappings`): the hub, Code Mapping, field mappings, therapy and disease-category reference, relevance signals, source systems |
| `analyst` | every mapping page, **and writing them** (`has_mapping_write_access`, `_can_manage_mappings`): creating, editing, proposing and uploading code and field mappings, synonyms and choices, Suggest, Lock, Done, priority, axis variants, LOINC axes, SNOMED attributes, concept minting, source-system facility review, rejecting relevance signals -- and PRism analytics, a separate app that shares the identity table. **No individual patient's data**: the patient list is empty, a detail is 404, clinical rows and a patient's source codes are out of reach, aggregated org stats show no org (#2398, #2416). The one exception is a public demo org (`allows_patient_signup`) the grant sits at: its sample patients are listed and CSV-exported to every grant there, whatever the role, a patient's included (#1392), but de-identified for a reader without curation access -- names, age and location withheld on the server as if each patient had opted in (`withhold_identifiers`, #2460) -- and the detail stays 404 as it does for a patient there; the UI shows such a reader no Name column and no row link. |
| `patient` | their own record; never another patient's (see `PatientRecordIsolationTest`) |

So a doctor sees every mapping but creates or changes one only when also
granted `analyst` (a 403 "Mapping curator access required." on every write
verb, and the pages hide their write controls behind `canWriteMappings`), and
an analyst sees a patient only when also granted `doctor`. An analyst is
refused every patient read or write; the UI opens an analyst-only account on
Code Mapping and hides the patient pages. Therapy and disease-category
reference writes stay org_admin-only (`_require_mapping_admin`), as before.
A trust of either kind makes a `doctor` or `org_admin` at the trusted
org or domain an admin of the granting org, and still does so for a verified
account that has never held a grant (a federated login, or a trusted-domain
self-signup once verified); it gives an account whose grants are all `analyst`
or `patient`, or have expired or been revoked, nothing. A grant at a public demo
org (`allows_patient_signup`) is a sandbox credential that expands no
**organization** trust; a **domain** trust is about the verified address and
still applies to it (`get_admin_access_paths`). A self-signup at a public demo org, and a verified
trusted-domain self-signup (#1454), are granted `doctor` rather than the former
`analyst`, because that signup exists to browse patients; grants made before
#2416 are left as they are. Approval stays with
staff and org_admin (`_can_approve_mappings`) for every role, and never for the
curator who proposed that destination (see
[the approver cannot be the proposer](#code-mapping--the-approver-cannot-be-the-proposer)). Gate a new
mapping endpoint's reads with `_can_view_mappings` and its writes with
`_can_manage_mappings` (`concept_to_code._authorize(user, write=True)` on
that module); a mixed-verb view checks both. `AnalystRoleScopeTest` and
`MappingApprovalRoleTest` in `patient_portal/tests.py` hold the roles to this.

Adding a field to PatientRecord is an approval, not a mapping write: `POST
/api/v1/custom-patient-fields/` creates the field with an approved mapping, so
it is gated by `_can_approve_mappings` and an analyst or a doctor gets 403
(#721). The UI offers it to staff and org admins only, outside patient mode:
the Add Field link at the top and bottom of each patient tab
(`AddFieldLink.tsx`, shown with the fields' globes) and the Field Mappings
page's own button, both of which open the mapping dialog in create mode.

---

## Rule: Tests for Every New Feature

**Every new feature must have tests written and run before the work is considered complete.**

- Write tests immediately after implementing the feature — not as a follow-up.
- Tests must be run against local PostgreSQL (see Running Tests above).
- The feature is not done until the tests pass.

**What to test for a new API endpoint or middleware:**
- The happy path (correct input → expected output/status)
- All relevant HTTP methods (GET, POST, PATCH, DELETE) if applicable
- Authentication/authorization edge cases (unauthenticated, wrong scope, etc.)
- Failure cases (invalid input, missing resource, etc.)
- That the feature does NOT fire when it shouldn't (e.g., read-only requests for write-only logic)

**Test placement:**
- Backend feature tests → `patient_portal/tests.py` (or `omop_core/tests.py` for model-level logic)
- New test classes should inherit the appropriate base (`_SmartBase` for OAuth-authenticated tests, `TestCase` for pure model/logic tests)

---

## SCT Fields — Multiple Myeloma (PR #115)

Three fields were added to `PatientRecord` for stem cell transplant tracking:

| Field | Type | Vocabulary |
|---|---|---|
| `stem_cell_transplant_history` | `JSONField` (string list) | `StemCellTransplant` — 3 values: `autologous SCT`, `allogeneic SCT`, `tandem SCT` |
| `sct_date` | `DateField` | — (free date, validated: no future dates) |
| `sct_eligibility` | `JSONField` (string list) | `SctEligibility` — 4 values: `eligible/ineligible for autologous/allogeneic SCT` |

### Vocabulary tables

Both vocabularies live in `omop_core/models.py` as `VocabularyLookup` subclasses and are served via the `/api/vocabularies/` endpoint:

- `/api/vocabularies/stem-cell-transplant/` — `StemCellTransplant`
- `/api/vocabularies/sct-eligibility/` — `SctEligibility`

### FHIR extensions (MM bundle)

The MM FHIR generator (`generate_fhir_bundle --disease mm`) emits three extensions on the `Patient` resource. The upload handler (`views.py`) reads them back:

| Extension URL suffix | Maps to field |
|---|---|
| `mm-sct-history` | `stem_cell_transplant_history` (comma-joined → split on ingest) |
| `mm-sct-date` | `sct_date` (ISO date string, future dates silently dropped on ingest) |
| `mm-sct-eligibility` | `sct_eligibility` (comma-joined → split on ingest) |

On ingest, both comma-split lists are filtered against the live vocabulary tables before storing — unrecognized tokens are discarded.

### Serializer validation

`PatientRecordSerializer` enforces two rules:

- `validate_sct_date` — rejects future dates with HTTP 400
- `validate_sct_eligibility` — rejects contradictory pairs (e.g. `eligible` + `ineligible` for the same transplant type)

### Seeding sample data

These need `SECRET_KEY` as well, from `.env` or exported — the DSN is yours to
supply and this command writes, so the key has to be the one belonging to whatever
it points at. Add `DEBUG=True` instead only when that is a throwaway local database.
See [above](#secret-key-on-a-shared-database).

```bash
# Seed random SCT data onto MM PatientRecord rows that have no SCT data yet
DATABASE_URL="..." python manage.py populate_sct_sample_data

# Overwrite existing values
DATABASE_URL="..." python manage.py populate_sct_sample_data --overwrite
```

---

## Rule: Data Migration Safety (Vocabulary Remapping)

When a migration remaps or truncates a controlled-vocabulary JSONField, follow this checklist **before** applying to any database that has real data.

### 1. Audit first

Run the audit command against the target database to surface any values that the migration does not recognise:

`SECRET_KEY` comes from `.env` here too, and it must be staging's: this runs
against real data. See [above](#secret-key-on-a-shared-database).

```bash
# Staging — named explicitly
DATABASE_URL="${STAGING_DATABASE_URL:?STAGING_DATABASE_URL is not set}" \
  python manage.py audit_sct_history
```

The audit only reads, so it can run against production the same way with
`DATABASE_URL="${PROD_DATABASE_URL:?}"`.

The command exits with code 1 if unrecognized values are found. Resolve them before proceeding — either add a mapping to `_OLD_TO_NEW_SCT` in the migration, or accept that the values will be preserved as-is.

### 2. Write a migration that preserves, not silently drops

The bug pattern to avoid:

```python
# BAD — treats "maps to None" and "not in dict" identically; silently drops unknown values
mapped = _OLD_TO_NEW_SCT.get(v)
if mapped and mapped not in new:
    new.append(mapped)
```

The correct pattern:

```python
# GOOD — distinguishes intentional None from "key not present"
if v not in _OLD_TO_NEW_SCT:
    # Preserve unrecognized string; it will be logged.
    if v not in new:
        new.append(v)
    continue
mapped = _OLD_TO_NEW_SCT[v]
if mapped is not None and mapped not in new:   # None = intentionally cleared
    new.append(mapped)
```

Always log unrecognized values at `WARNING` level (not `ERROR`, not silently) and `bulk_update` rather than sequential `save()`.

### 3. Make replace/truncate operations targeted, not `.all().delete()`

```python
# BAD — destroys any rows added after the migration ran if replayed
Model.objects.all().delete()
for code, title in NEW_VALUES:
    Model.objects.get_or_create(code=code, defaults={'title': title})

# GOOD — deletes only old rows not in the new set; safe on replay
Model.objects.exclude(code__in={code for code, _ in NEW_VALUES}).delete()
for code, title in NEW_VALUES:
    Model.objects.get_or_create(code=code, defaults={'title': title})
```

### 4. Document irreversible reverse migrations

When the original values cannot be reconstructed, say so explicitly:

```python
def reverse_migrate_foo(apps, schema_editor):
    # Original values cannot be recovered after remapping.
    # Rolling back this migration leaves the JSONField in its new-vocabulary state,
    # inconsistent with the restored vocabulary table. Manual correction is required.
    pass
```

### 5. Order of operations for vocabulary truncation

Always remap existing data rows **before** truncating the vocabulary table:

```python
operations = [
    migrations.RunPython(seed_new_vocabulary, reverse_seed),       # 1. add new vocab rows
    migrations.RunPython(remap_existing_data, reverse_noop),       # 2. remap PatientRecord rows
    migrations.RunPython(replace_old_vocabulary, reverse_restore), # 3. truncate old vocab
]
```

---

## Migration Conventions

- **Use `makemigrations`** — let Django generate migration files; don't write them by hand.
- **Apply to staging DB before pushing**: run `migrate` against `promop_dev` to verify before committing.
- **Never apply schema changes manually** to the DB — always go through migrations so Django's state stays in sync.
- **Production migrations run automatically** — `start.sh` calls `scripts/prepare-deployment.sh`, which migrates, on every Render deploy.
- **Rebase on `dev` before creating any migration.** Two feature branches that both
  create a migration off the same base will produce conflicting sequence numbers when
  both merge. Before running `makemigrations`, always:
  ```bash
  git fetch origin dev && git rebase origin/dev
  ```
  This ensures the new migration gets the next available sequence number. CI runs a
  conflict check on every push to `dev`, so a missed conflict is caught immediately
  after merge, but prevention is better than cleanup.

---

## Frontend Lint: fetching in an effect

`npm run lint` runs with `react-hooks/set-state-in-effect`, which **errors** (not
warns) on this shape:

```tsx
const load = useCallback(async () => { setLoading(true); /* ... */ }, []);
useEffect(() => {
  load();          // error: Avoid calling setState() directly within an effect
}, [load]);
```

The rule traces into the callback, so moving the `setState` calls after the
first `await` does **not** satisfy it. Wrap the call instead:

```tsx
useEffect(() => {
  (async () => {
    await load();
  })();
}, [load]);
```

This has broken `dev` twice (fixed in #630 and again after #634). A red `dev`
turns **every open PR red**, because the same job runs on each one — so it costs
far more than the one file it appears in.

Run `npm run lint` before pushing frontend changes; `tsc --noEmit` and the test
suite both pass on the offending shape, so neither catches it. CI's job is
"Frontend lint **& build**" — run `npm run build` too.

## DB / Model Sync Check

To audit whether the DB and model are in sync at any time:

It only reads. Point it at staging (`$STAGING_DATABASE_URL`) or production
(`$PROD_DATABASE_URL`) explicitly. See [above](#secret-key-on-a-shared-database).

```bash
DATABASE_URL="$STAGING_DATABASE_URL" \
  .venv/bin/python manage.py shell -c "
from django.db import connection
from omop_core.models import PatientRecord
cursor = connection.cursor()
cursor.execute(\"SELECT column_name FROM information_schema.columns WHERE table_name='patient_record' ORDER BY column_name\")
db_cols = set(r[0] for r in cursor.fetchall())
model_cols = set(f.column for f in PatientRecord._meta.get_fields() if hasattr(f, 'column'))
print('MISSING from DB:', sorted(model_cols - db_cols))
print('EXTRA in DB:', sorted(db_cols - model_cols))
"
```

Expected output: `MISSING from DB: []` and `EXTRA in DB: []`.

---

## Module Federation (GCP Cloud Run)

promop serves as a Module Federation **remote** (`labs_results_remote`) consumed by the ht-phr host app. There are two Dockerfiles with different build targets:

| File | Build command | Output dir | Purpose |
|------|--------------|------------|---------|
| `Dockerfile` | `npm run build` | `frontend/build/` | Local dev — standalone SPA |
| `Dockerfile.gcp` | `npm run build:remote` | `frontend/dist/remote/` | Cloud Run — federation remote (`remoteEntry.js`) |

**`WHITENOISE_ROOT`** controls which directory WhiteNoise serves at the root URL. It is set via environment variable in each Dockerfile:
- `Dockerfile`: `WHITENOISE_ROOT=/app/frontend/build`
- `Dockerfile.gcp`: `WHITENOISE_ROOT=/app/frontend/dist/remote`

When unset, `settings.py` auto-detects by checking `frontend/dist/remote` first, then `frontend/build`.

**Do not** change `Dockerfile`'s build command to `build:remote` or `Dockerfile.gcp`'s to `build` — they serve different purposes. The deploy workflow (`deploy-staging.yml`) uses `-f Dockerfile.gcp`.

If `remoteEntry.js` returns 500 on staging, check that `WHITENOISE_ROOT` points at `frontend/dist/remote` in `Dockerfile.gcp`.

---

## Copying Data Between Instances

Every model is one of three kinds, listed in `omop_core/services/instance_data.py`.
`tests/test_instance_data.py` fails when a new model has no kind.

| Kind | What | Moved by |
|---|---|---|
| system | accounts, tokens, sessions, tenancy, audit, caches | never copied |
| reference | what is needed to import patients: mappings, lookups, therapy reference, HealthKey concepts | `copy_reference_data` |
| patient | what is known about one person | `copy_patient` |

Both commands **write to `DATABASE_URL`**. Always name the target explicitly —
`DATABASE_URL="$STAGING_DATABASE_URL"` for staging, or a local database — and never production. See [above](#secret-key-on-a-shared-database) for `SECRET_KEY`.

```bash
SOURCE_DATABASE_URL="postgresql://..." .venv/bin/python manage.py copy_reference_data --dry-run
SOURCE_DATABASE_URL="postgresql://..." .venv/bin/python manage.py copy_patient 12345 --org healthtree --dry-run
SOURCE_DATABASE_URL="postgresql://..." .venv/bin/python manage.py copy_patient --filter-org-id 26 --org healthtree
```

Both write to `DATABASE_URL` and open the source read-only. `copy_reference_data`
runs in one transaction; `copy_patient` uses one transaction per patient.
Copy reference data before patients: concepts and therapy regimens resolve by code.

### copy_reference_data

Rows match on a natural key (`code`, `slug`, `(source_vocabulary_id, source_code)`),
never on the source pk, and are overwritten from the source. Links travel as
the related row's natural key. Concepts travel as `(vocabulary_id, concept_code)`:
Athena ids agree across instances, HealthKey-minted ids do not. A concept not
loaded here is cleared and reported.

Reference data with a release loader is not copied: Athena vocabularies
(`load_athena_vocabularies`), UMLS, LOINC classes and survey definitions. Only
HealthKey-minted concepts are copied, under this instance's own ids.

`--prune` deletes field and code mappings the source lacks. It never deletes
lookups or therapy reference rows, because patient data can point at them.

Code mappings steer ingest (`resolve_source_code`), so copying an approved row
changes what a later import resolves to. `occurrence_count`, `first_seen` and
`last_seen` stay local. `reviewer` / `created_by` / `updated_by` / `proposed_by` are cleared:
the same Identity id is a different person on another instance.

The field-mapping tables go through `field_curation_transfer`, whose payload is
also the `dump_field_curation` fixture format. Keep that payload stable.
A field's disease scope (`memberships`, #2331) is replaced from the source,
not merged, with or without `--prune`: a field both instances have takes the
source's scope, base included. A field or a Disease Category that exists only
on this instance keeps what it has.

### copy_patient

Patients are selected on the source by ids, `--filter-*` flags, or both, which
narrows the ids. Filters combine with AND. A run needs at least one id or filter,
so it cannot copy every patient by accident. `--filter-org-id` is the org id on
the source. A failed patient is reported, the rest continue, and the command
exits non-zero.

Every row gets a new id. Foreign keys, plain integer references
(`visit_detail_id`, `episode_id`) and polymorphic ones (`EpisodeEvent.event_id`,
`measurement_event_id`, resolved through the field concept named `table.column`)
follow the copied rows. A missing concept becomes 0 where required, else null.

- Every id comes from this database, `person_id` included: a source id belongs
  to the source instance, where the same number is a different patient. The copy
  is recorded as a `ProvenanceRecord` (`source='copy_patient'`), so copying the
  same patient twice needs `--replace`, which refuses when system data (a login,
  a FHIR connection, an invitation) points at the person here.
- An address that already exists here is reused; only a new one is inserted.
- `PatientRecord` is copied and re-derived, so user edits survive and derived
  therapy ids follow this instance's reference data. It joins `--org`.
- Stored document files are not copied; those rows are skipped and reported.
- Source timestamps are kept, including `auto_now` ones. Revision authors are
  source Identity ids, so they are cleared, except `system`.
- Only some OMOP tables have a pk sequence. Tables without one allocate above
  `MAX(pk)` under a table lock.

---

## Code Mapping Suggest

Suggest proposes a destination concept for queue rows that have none. Two rules
decide what it touches and how long it takes.

### It reads the tab, not the clinical tables

The candidate set is `suggestable_mappings()`: every `proposed` row on the tab
with **no destination yet**, whatever its provenance. A row with nothing in the
destination column has nothing that could be overwritten — an
`open-wearables-seed` or `hk-labs` row waiting for a concept is exactly what
Suggest is for, and staging has 69 of them (67 + 2).

Provenance decides only what **Replace** may re-answer. A row that already has a
destination is revisited only with `resuggest`, and then only if its
`origin_system` is empty or begins with `suggest` — meaning nothing but a
previous Suggest run ever set it. An `HT-One`, `HT-FHIR` or `athena` destination
was asserted by an importer that knew more than the source text does (75,257 of
staging's 85,318 rows), so re-deriving it would spend a model call to make the
answer worse. In practice the ICD-10 and RxNorm tabs return nothing, because
every row on them already has an importer's destination.

Rows are ordered **untried, then gaps before replacements, then by
occurrence**:

1. **Untried before tried**, by the current `suggestion_model_version`. This is
   the primary key *always*, because anything above it starves the queue. A
   declined code keeps no destination, so it stays eligible and, being
   high-occurrence, retakes the front of the very next run — on the staging
   sample *every* code in the top slots was declined, so they would never free
   up. Putting "no destination" above it has the same failure in the replacing
   case: a run would spend its whole budget re-trying declined gaps and never
   reach a replacement. Untried-first means every run advances; declined codes
   come round again once the tab is drained.
2. **No destination before has one**, within untried and within tried alike. An
   empty destination is a gap, a replaceable one is an improvement, and the gap
   is worth the model call first. Without Replace nothing has a destination, so
   this key is constant.
3. **Occurrence**, because that is the order a curator should meet codes in.

### The limit counts codes, not codes per table

A tab maps to one or more clinical tables — the Uncoded tab maps to all five —
but that is only where the rows live. Each queue row carries its own
`omop_table`, so rows are selected, ordered and limited **once, together**.

Selecting per table applied the limit to each, so the Uncoded tab could evaluate
5× the ceiling it was given, and the ordering above would only hold within a
table rather than across the run.

It used to derive the queue instead, by grouping a whole clinical table on
`concept_id = 0` and subtracting every existing mapping. That cost 4-7s per
table per request and, once ingest began queueing every code it met, returned
**zero** rows on both tabs with a real backlog. `manage.py
enqueue_unmapped_source_codes` still does that scan, as the batch job it is: it
creates empty queue rows with blank provenance, which is exactly the state
Suggest looks for. So the split is **enqueue, then suggest**.

### Three retrieval paths, then Anthropic or Jev chooses the winner

```
UMLS CUI bridge ──► exactly one standard concept? ──► done, no ranking call
       │ no
       ▼
   ┌────────────────┐
   │  In parallel:   │
   │  Lexical trigram │──► best N by trigram similarity
   │  Vector cosine   │──► best N by embedding distance
   └────────────────┘
       │
       ▼  merge (union, enrich overlaps with both scores)
       │
       ▼
Anthropic or Jev ─► picks one from the merged pool with confidence levels, or declines
```

UMLS, lexical, and vectors are **three independent retrieval paths** that
contribute candidates to one merged pool. `semantic_candidates()` does a full
cosine similarity search against the `ConceptEmbedding` table — it is real
retrieval, not reranking. Lexical and vector results are often mostly disjoint
because trigram similarity and embedding cosine are different signals.
`vector_rerank()` exists in the code but is unused in the default pipeline.

`rank_candidates_dispatch()` routes the merged pool to the configured ranking
model: **Anthropic** (Claude, the default), **Jev** (Typesafe API), or
**both** (concurrent, highest confidence wins). The ranker scores each candidate
with a qualitative confidence level (high / medium / low, mapped to numeric
values) and either picks a winner or declines. A declined code stays in the
queue for the next run.

### Source Metadata is ranker context, nothing more

A description is often missing or generic: the ETL may send "Vitals" for a code
that is clearly specific, and Epic adds LOINC `8716-3` "Vital signs" to every
vital sign. The Source Metadata the mapping dialog shows (usually the FHIR
resource that carried the code) says what it really is, so both rankers get
that JSON **in full, de-identified**, as `source_metadata` beside the
description (#1782, #1786, #1791). The dialog sends its row's metadata with
suggest-one, falling back to this table's SCCM row; batch runs use each queue
row's own. Retrieval never reads it and no other patient's data is consulted.

`omop_core/mapping/source_metadata.py::deidentify` removes, at any depth, by
key *and* by shape — FHIR carries identity under many names, such as an
extension's `valueIdentifier` or a `collector` reference: every Reference,
Identifier, HumanName, Address, ContactPoint, Annotation and Attachment, any
Patient/Practitioner/RelatedPerson resource, `id`, `meta`, `fullUrl`, the
narrative, `valueString`/`valueMarkdown`, and every date or time field
(case-insensitively). `code.text`, coding displays, Quantity and CodeableConcept
values stay — they are the point. Over 20,000 characters it trims lists and
strings, then keeps only the coded fields; it never drops the code.

Where the time goes, per code:

| Stage | Cost |
|---|---|
| lexical trigram retrieval | ~0.15s for a domain-scoped code, serial (was ~2.5s and **67%** before #1467 — see below) |
| vector cosine retrieval | ~0.3s (query embedding ~25ms + cosine search) |
| ranking call (Anthropic or Jev) | ~3.5s each, but concurrent (`RANK_CONCURRENCY`) and only for codes UMLS did not settle |
| embedding model load | ~5s, **once per gunicorn worker**, on its first Suggest |

### Retrieval is scoped by index, not by filter

Trigram similarity is a *lossy* index operation: the GIN index returns every row
that shares enough three-letter fragments with the source text, and Postgres
then computes an exact similarity for each. On the whole-table indexes that was
30,000-235,000 rows per code, scored and almost all discarded — by the domain and
standard filters for names, by the join to `concept` for synonyms. Both whole-table
indexes were being *used*; the cost was the recheck, so no plan fix could help.

So the rows that can be an answer get indexes of their own (#1467):

| Path | Index | Holds |
|---|---|---|
| names | `ix_concept_trgm_<domain>` — 5 partial GIN indexes on `concept` | standard, active concepts in that domain |
| synonyms | `ix_suggest_syn_<domain>` — 5 partial GIN indexes on `suggest_synonym_term` | the uppercased synonyms of those same concepts |

`suggest_synonym_term` exists because `concept_synonym` has no domain or standing
of its own — those live on `concept` — and a partial index cannot look across a
join. It is derived, never edited: `services.suggest_synonym_terms.refresh()`
deletes what stopped being eligible, then inserts what is missing (that order:
a concept that changed domain would otherwise collide with its own old row).
`load_athena_vocabularies` runs it after every full load; migration `0246`
backfilled instances that already had a vocabulary, and
`manage.py refresh_suggest_synonym_terms` runs it by hand.

One `concept_synonym` row is **one term per semicolon-separated part**, not one
term (#2355). Athena ships LOINC's related names as a single string
("Chemistry; M Protein; M-band; M-spike; Paraprotein; SPEP; …"), and trigram
similarity of a short source text against that whole string is near zero —
"M SPIKE 1" scored 0.09 against it, so the one LOINC that means an M-spike was
never retrieved, while its part "M-SPIKE" scores 0.8. An instance refreshed
before the split holds the joined strings until the next refresh replaces them
with their parts (about 2.3M parts from 104k joined rows on HealthTree).

Measured on staging over the same 28 real queue rows, synonym retrieval went
**32.1s → 2.8s** with identical results on all 28; name retrieval for a
procedure description went from ~1-3s to 25-75ms.

Three rules that keep it honest:

- **The index predicates are the query's filters, literally.** The planner only
  picks a partial index it can prove applies. Change `standard_concept='S',
  invalid_reason IS NULL, domain_id=…` in `lexical_candidates` and the indexes
  silently stop being used.
- **An unbuilt table falls back to whole synonyms.** `is_populated(domain)`
  is false for a test database or a never-refreshed instance, and for a search
  with no domain (ICD-10 searches all of them); retrieval then reads
  `concept_synonym` directly — slow, every synonym, but scored as the whole
  string, so without the per-part split below.
- **`lexical_candidates` still re-checks standing on `concept`.** A term that went
  stale since the last refresh can cost a shortlist slot; it can never put a
  retired concept in front of the ranker.

Drug names need an additional narrowing step because a source such as
"ASPIRIN 81 MG ORAL TABLET" shares dose and form words with many RxNorm names
in the same domain. `source_text.narrowing_text` searches on ingredient words
and falls back to the full source text when that finds no names. Narrowing can
change the candidate set; `tests/test_lexical_narrowing.py` covers its recall
trade-offs.

Cost: ~135 MB for the concept indexes and ~660 MB for the table and its indexes
on a fully loaded instance.

### The run is queued, and how big it may be depends on that

At ~3.5s per code a tab's backlog does not fit in a request, so `POST
/v1/code-mappings/suggest/` returns **202** with a run id and the page polls
`GET /v1/code-mappings/suggest-runs/<id>/`. Progress lives on a `SuggestRun`
row rather than in Celery's task metadata: the counts must survive a worker
restart, the poll can land on any gunicorn worker, and the inline dispatcher has
no result backend to write into.

`omop_core/services/suggest_jobs.py` picks how it runs, on the same rule as
`derivation_jobs.py` — Celery when `CELERY_BROKER_URL` is set, inline otherwise
— and **the ceiling depends on which**:

| Dispatcher | Ceiling | Bounded by |
|---|---|---|
| `CeleryDispatcher` | `QUEUED_MAX_CODES = 50` | `CELERY_TASK_TIME_LIMIT` (900s) |
| `InlineDispatcher` | `INLINE_MAX_CODES = 5` | the request, under `start.sh`'s bare gunicorn (30s default) |

That split is not a nicety. `render.yaml` leaves `CELERY_BROKER_URL`
dashboard-managed on the web service (`sync: false`), so a deployment that has
not pasted the Redis URL in yet **falls back to inline** — and 50 codes inline is
~125s of serial retrieval and a 502, the exact failure this design removes.
Batches measured at 3/9.5s, 5/24.1s, 8/28.7s, 10/38.3s.

The ceiling is a budget for the whole run, not per clinical table: a source
vocabulary can map to five tables, and a per-table limit would let one run
attempt five times its own ceiling.

The ceilings were set when retrieval cost ~2.5s a code. #1467 cut that by an
order of magnitude for domain-scoped codes, so they can be revisited — but
re-measure inline batches first; drug codes and ICD-10 (no domain) did not get
cheaper.

### What the curator is shown

The headline number is **new destinations written** (`SuggestRun.destinations`)
— what the run achieved. Not "rows written": a code the ranker declined is
written too, so the run records that it tried, and counting those would claim
destinations nobody proposed. `done`/`total` is the separate "how far through"
number.

### What the curator decided on, and where

`suggestion_confidence` is live: a later run overwrites it and a moved
destination clears it, so it cannot say what the ranker believed when a curator
saved the row. Every curator save through `_upsert_source_code_mapping`
therefore records two more columns (#2382), read by the accuracy analytics:

- `decision_confidence`, the ranker's confidence in the destination being
  saved. The dialog sends it (`decision_confidence`) when the destination came
  out of a Suggest run in that dialog for the source text as it still reads
  (`rankedConfidence`: the higher score when both rankers scored the concept,
  as the candidate table shows), null when that run never scored it. Null or
  no key keeps the row's live confidence while the destination is unchanged;
  a moved destination without a score records none. The Concept to Code
  propose endpoint sends the preview candidate's.
- `decision_surface`: `code2concept` (the Code to Concept tab's dialog,
  one-click approve and group actions), `concept2code` (the Concept to Code
  tab and its endpoints), or `patientcode` (any save scoped to a patient,
  whatever the payload says). Blank means no curator has saved the row, or a
  machine moved its destination since.

The Mapping Accuracy Analytics page (`/code-mappings/accuracy/analytics`,
linked from the top of the Concept to Code tab, #2383) reads them: one table
per surface plus Overall, rows for every decision and for the 90–100, 80–90,
70–80 and below-70 confidence bands, columns for total, approved, proposed and
rejected counts (the rows' current status), a Scored count (decisions that
began as a ranker suggestion) and the precision, recall and F1 the accuracy
dashboard computes from `suggestion_outcome` over those. The surface and band
describe the last curator save, the outcome the first disposition, the status
the row now; a reverse-Suggest re-point since the save shows as Proposed. A
decision no ranker scored counts in the Overall row only; a row with a blank surface is not a
curator decision and is not counted. One grouped query
(`code_mapping_accuracy_analytics`).

The analytics page is the one place a curator tracks mapping accuracy: the
Code to Concept tab carries no accuracy strip (#2730). Most mapping is done
from Concept to Code, and the strip on Code to Concept read as low recall only
because nearly every reviewed row counted as "Other destination". The History
page (`/code-mappings/accuracy`) that strip linked to still answers at its
address, with no link to it in the UI.

Suggest never writes either column: reverse Suggest's auto-propose goes
through the same writer with `record_decision=False`. A machine write that
moves a destination outside the writer (a code-mapping upload, Athena
destination recovery) clears both, as it clears `suggestion_confidence`. Both
travel with `copy_reference_data`.

### A mapping's confidence: stored, kept, recomputed (#2668)

`SourceCodeConceptMapping.suggestion_confidence` (0-1) is the number a mapping
carries, and what provisional data reads (`code_resolution`: a proposal with a
destination and a confidence at or above `PROVISIONAL_CONFIDENCE_FLOOR`). Only
Code to Concept and Concept to Code propose concepts (the ETL does not), so
those are the writers: Suggest and its group/reverse runs store the ranker's
score, a Concept to Code proposal stores its preview's. **The server stores only
a confidence it produced.** The score endpoint and suggest-one sign what they
computed (`omop_core/mapping/confidence_token.py`: `django.core.signing`, its own
salt, one hour), bound to the source vocabulary, code and table, the destination,
a hash of the source **description** (case and spacing ignored) and the number.
Only a score a ranker stood behind is signed: the score endpoint signs the
destination's own score when a ranker chose it, and suggest-one signs only the
chosen concept's score (the winner's own ranker lists first); never an
alternative a ranker merely scored (Jev scores every candidate, declined or not),
and nothing at all when no ranker chose. The dialog sends that
`suggestion_confidence_token` with its save and `_saved_confidence` stores the
number only when the token verifies against the saved row's source, table,
destination and description. A forged, expired or mismatched token is a 400; a
typed `suggestion_confidence` number is ignored (`null` clears); with no token
the stored value follows the rules below, so an analyst cannot make a
never-ranked mapping yield provisional rows, or have a score be about text of
their choosing. For an **existing** mapping the score endpoint takes the
description and metadata from the stored row, whatever the body sends; for a new
row the body's description is bound and its metadata (client-supplied) is not,
and the dialog keeps Calc off while the source text has an unsaved edit.
**Approval keeps it**; it is cleared when the destination moves without a token
for the new one, when the saved row's source code, source vocabulary or table
changes (it described the machine's pick for that source), or when a file upload
re-points the code. Both Concept to Code writes store the preview's own score,
read server-side from the saved reverse run (`_preview_score`: the client names
the run, never a number, so no token is needed), and only while the run is under
24 hours old and the candidate's description is still what the row says: a
proposal stores its candidate's, and an *apply* that moves an existing proposal
to this concept stores the score the preview gave that row for it (the tab sends
`run_id` with each approve/reject); a candidate the preview did not score, a
stale or mismatched preview, or no run stores none and Calc computes it. It is
separate from `decision_confidence`, the ranker's belief at the moment of a
curator save (#2382), which a Calc for the destination saved also sets.

**Calc** is the dialog's button beside the number: `POST /api/v1/code-mappings/score/`
takes suggest-one's body plus `destination_concept_id`, which must be a valid
standard concept in the domain of the source's table (the check provisional
data applies at read time; the injected concept skips retrieval's filters, so it
is made up front, 400 otherwise). It runs the same retrieval and ranking (queued
and polled like suggest-one, as an `individual` run so the page's "latest run"
never shows it) with that concept in the pool before enrichment and the axis
rules, one selection per ranker and no expansion retry, and returns
`scored: {confidence, ranked, note, token}` in the result event. The number
is returned only when a ranker **chose** the destination, and is that ranker's
score (the higher when both chose it): Jev scores every candidate it is shown,
so a probability for a concept it did not pick is not confidence in it. It is
`null`, with a note, when the concept conflicts with the source's evidence, the
ranking model is unavailable (the retrieval-order stand-in is never scored), or
the rankers declined or preferred another concept. It writes nothing; Save
stores it.

### Which notes are a curator's

`SourceCodeConceptMapping.notes` is one column that Suggest (its rationale),
importers and reconciliations (their receipts) and curators all write, and the
mapping dialog sends it back on every save. `curator_note_at` (#2423) records
that a curator changed the text: the dialog's save stamps it when the note it
sends differs from the stored one, and clears it when the note is emptied. A
machine credential, and every machine write through
`_upsert_source_code_mapping` (`curator_note=False`, the default), never sets
it, and takes it off when it replaces the text. It travels with the note in
`copy_reference_data`. Migration `0334` marked the existing notes of curator-saved rows that do
not begin like a machine-written note.

`with_notes=1` narrows to rows with a mark and a note that is not blank
(`mapping_browse.apply_notes`), on the browse endpoint in every section, the
group-members and group-action endpoints, and the Concept to Code source list,
before paging and counts. Both lists have a "with notes" checkbox, off by
default, and show a yellow sticky note with the text on hover on each such
row (`has_curator_note` on a mapping row, `curator_note` on a source row).

### Co-occurring labs in Edit Mapping

The dialog's Source Evidence ends with the other results in the same panel or
order as the opened code (#2702): albumin and the alpha, beta and gamma
fractions beside "m-spike quantitation" say electrophoresis. `GET
/api/v1/code-mappings/<id>/co-occurring/` (`omop_core/services/co_occurring_labs.py`)
returns the coverage (records naming an order, distinct orders, order names
with counts) and one row per co-occurring source code: description, units,
System, Method, mapping status and the mapping to open. It has the audience of
the mapping's own source evidence: every mapping reader, whatever their
organization, and no service token. It returns codes, order names and counts,
never a patient, a date or a result value.

- **The evidence is the per-patient inventory**, which keeps one sample
  resource per patient and code. Two codes are seen together only when a
  patient's samples for both come from the same order, so the table shows
  co-occurrence and never proves its absence. The OMOP rows hold no order.
- **An order is a `basedOn` reference**, matched within one patient. A display
  with no reference names an order and joins nothing. A record that names no
  order is joined through a DiagnosticReport in the patient's inventory whose
  `result` lists it (`Observation/<id>`, as the ETL stores it), and counted
  apart (`records_in_report`, over the records read). A hospital's mapping
  reads only that hospital's patients.
- **One Observation is one result, listed under one of its own codings**: the
  one in the opened code's code system, else a local code before the LOINC
  beside it. Not under the code of the inventory row that sampled it: a LOINC
  shared by several local codes samples only one of them, and Epic files every
  vital sign under 8716-3 as well. The patient's inventory row for the listed
  code says which code system the ETL filed it under. The opened record's own
  Observation (same resource id; without ids, one listing the opened code) is
  left out.
- **System and Method** are the LOINC axes of the code's current mapping
  (approved or proposed) when it lands on a LOINC, else those of a LOINC the
  hospital sent on the record that the opened record does not also carry,
  which the table marks "H"; blank when neither.
- **The mapping shown** is the opened mapping's hospital's row, then the
  global one; a global opened mapping falls back to a hospital's row, so a
  queued code is never shown as outside the queue.
- **It is bounded and read after the dialog's own load.** A HealthTree
  patient's inventory is about 4,000 stored resources, and reading them is the
  cost: the walk takes the first `MAX_RECORDS` (25) records, those naming an
  order first, looks for results only under `measurement` and `observation`
  and for reports only elsewhere, and joins rather than testing per row (about
  1s there with the rows cached). The walk as a whole has an 8s budget
  (`BUDGET_MS`), spent across its statements; one that outruns it, usually the
  first read after the rows have left the cache, returns `timed_out` and logs
  the mapping id, and the dialog offers Retry instead of an empty answer. The
  count of records naming an order is over every record; the orders and the
  table are over the records read. A code filed as a condition, drug or
  procedure is not walked (`applies: false`) and the dialog shows no section.
- A code in the table links to `/code-mappings?direction=forward&mapping=<id>`,
  which opens that mapping's Edit Mapping dialog in a new tab and then drops
  `mapping` from the address (`mappingLinks.ts`); a code with no queue row is
  plain text, and a reader without mapping write access is told why nothing
  opened.

### A code's rows follow its mapping to another table

A curator who changes a mapping's Domain re-files it under another clinical
table (the move offer from #2347, or the dialog's Domain field). The rows
ingest wrote for that code stayed in the old table, so the patient page --
which lists a code under the table its rows sit in -- kept showing it as
Unmapped there with no mapping that could ever match (#2386). Now a save that
changes `omop_table` **between measurement and observation** relocates the
code's stored rows (`omop_core/mapping/table_relocation.py`); the response's
`relocation` reports what happened.

- Only those two tables. They share the fact shape and nothing points at
  their rows by id but provenance and ownership. A drug exposure composes a
  therapy line (`TherapyLineExposure` cascades), a procedure anchors an
  imaging document, an episode event names a row by bare id: a move between
  any other pair keeps the rows where they are and `relocation.skipped` says
  so.
- A patient-scoped save moves that patient's rows in the request, so the
  Source Codes tab reflects it at once. It also carries the table the code
  was listed under (`rows_omop_table`): a mapping created or saved under
  another table -- an Observation-domain concept chosen for measurement rows
  -- moves that patient's rows into the mapping's table even though the
  mapping itself never changed table (#2393). That holds for a reject too:
  the mapping names the table, and a rejection left under another table is
  invisible there. An unknown `rows_omop_table` is a 400. A save from a mapping tab, and a
  code-mapping upload that re-files a code, move every patient's rows after
  commit (`relocation_jobs`; Celery when a broker is set, inline otherwise).
  Each batch of 500 rows commits on its own, so a task killed at its time
  limit resumes on retry. `manage.py relocate_mapping_rows <ids> --from-table
  <table>` moves what a lost task left behind.
- A row is copied column by column: the core both tables share (person, date,
  datetime, type concept, source value and source concept) plus every column
  both carry under the same name **and shape** (value, unit, qualifier,
  provider, visit, erroneous flag), under a new id from the destination's
  sequence. A measurement's operator and range are dropped. Its
  `ProvenanceRecord` follows it in one statement per batch, a measurement's
  `MeasurementOwnership` goes, the old row is deleted, and the patient's
  record is marked stale as a re-point does.
- The code matches by itself; the import description matches only rows at a
  concept this mapping named (its destination, the concepts it is moving
  off, concept 0), the re-point's rule, so "Glucose" does not drag another
  producer's rows or a hand-corrected one across.
- The rows keep the concept they had. Putting them on the destination is the
  re-point's job -- it also normalizes units, routes axis variants and
  collapses duplicates -- so the concepts moved join
  `deferred_repoint_concept_ids`; an approval's re-point runs after the move
  (the move dispatches it again), and a patient-scoped approval leaves it to
  Generate OMOP as before. An organization-scoped mapping moves only rows its
  hospital wrote.
- The per-patient inventory (`PatientSourceCode`) is re-filed under the new
  table, merging into a row the patient already has there.

### A saved proposal makes provisional rows; the ETL sends no confidence

With `PROVISIONAL_DATA_ENABLED` on, a mapping saved as `proposed` with a
destination and a `suggestion_confidence` at or above
`PROVISIONAL_CONFIDENCE_FLOOR` makes the matching rows provisional for the
patients who hold its code, with no ETL change: a save that changes a proposal's
answer (new, changed, or withdrawn) queues a sweep of its code
(`omop_core/services/provisional_sweep.py`), the bulk OMOP write and the FHIR sync
queue a one-person resolve after ingest (an ETL replay included), and
`manage.py sweep_provisional_rows` covers everything else (an `update()` fires no
signal). All three call `resolve_person_source_codes`, so approval, rejection and
a moved destination behave as in #2530. The automatic ones need a Celery broker
and are coalesced per code and per person (best effort: a cache outage never
fails a save or an ingest); the per-person resolve takes a session advisory lock
and keeps its own short transactions, never one outer one (that deadlocked with
the ETL's keyed write); with no broker nothing runs in a request and the command
is the path. **Only unscoped proposals qualify:
organization-scoped ones (set when the caller passes an organization for Epic or
Cerner code systems) are ignored; confirm HealthTree's ETL does not rely on them
before enabling it there.** Off, nothing is queried, queued or written. See
[the plan](docs/provisional_data_plan.md#as-built-in-2662-the-server-makes-the-rows).

### What the provisional pass reads (#2534)

`patient_record_provisional` is built by `omop_core/services/provisional_record.py`
from provisional rows only, with one rule for every reader: a value is kept only
where `patient_record` has no answer for the field and nobody typed one, and its
confidence is the lowest among the rows that decide it. The phase 1 readers (labs,
blood counts, assertions, vitals, curated field mappings) read provisional
measurements and observations. Two composite readers read the other tables:
the disease fields (`disease`, `disease_slug`, `diagnosis_date`,
`diagnosis_date_precision`, `condition_clinical_status`) from the cancer among the
provisional conditions -- run only when one is a cancer, and only for a record with no
disease yet or the same one, so another cancer's dates never fill a disease the record
names -- and `concomitant_medications` from the provisional drug exposures through the
real reader's own `recent_medications`. Not run, and why: therapy lines (persisted
Episodes, which a proposal must not create), staging/cytogenetics/SCT/cancer details
(curator-written observation keys, not proposal-fed concepts), and every JSON field
(`active_malignancies`, `disease_slugs`, `genetic_mutations`, `prior_procedures`, the
`genomics_*` markers), which has no confidence column. A patient whose approved record
already answers these fields shows nothing new: real beats provisional. The chosen
cancer is the real ranking's, whatever the confidence (a 55% row that wins on recency
gives every disease field 55); a typed disease or slug blocks the whole group, and the
PATCH mirror takes the group back together; a curated phase 1 value for the same field
wins; each reader runs in its own savepoint.

### One patient's full provisional view: `fill_provisional_record`

`manage.py fill_provisional_record <person_id>` (#2696) gives one patient as much
provisional data as the proposals can support, in one resumable command:
**propose** (the codes the patient has *unresolved rows* under: their
organization-unscoped queue rows that are `proposed` with no destination go
through the normal Suggest pipeline, ranker `--ranker jev|anthropic|both`, default
Jev; `--include-inventory` adds the inventory-only codes, those with no clinical
row yet, which are slow, about four seconds each, and only matter for creating
rows from the inventory -- on HealthTree one patient has 533 codes with unresolved
rows and 2,546 inventory-only; `--unresolved-only` is the default, spelled out),
**materialise** (the full per-person resolve over *every* row and *every*
mapping, not the sweep's narrowed `person_scope`: an approved mapping makes rows
real, including rows created for inventory codes that have none; a proposal at or
above `PROVISIONAL_CONFIDENCE_FLOOR` makes them provisional; a proposal below it,
or unscored, leaves them alone; the floor is never overridden, the report counts
what fell below it by band of the real floor and a proposal at or above it whose
destination the resolver will not write separately, and the rows are counted as
real, provisional and left) and **rederive** (the record is rebuilt inline:
`patient_record` from the approved rows only, with the names of the fields that
changed, and `patient_record_provisional` and `patient_record_confidence` from the
proposed ones, with the provisional field count and percentages).
`--tables` and `--limit` narrow only the propose step: every table's rows are
resolved and the record rederived.

- **The resolve is the Generate OMOP path, not a provisional-only pass.** Beyond
  provisional rows it makes the patient's existing unresolved rows real at an
  approved mapping's concept, normalises units for a mapping with a destination
  unit, rewrites deferred re-point concepts, and creates a real row with no value
  for every inventory entry that has an approved mapping and no clinical row.
  `--dry-run` prints an estimate of that (unresolved rows whose code has an
  approved mapping, inventory entries to create). Pre-existing resolver behaviour,
  not changed here: the approved-mapping lookup is by code and table alone (it
  does not filter by organization and takes `.first()` unordered), and axis
  variants are ignored.
- **The log separates what this run changed from what the rows are.** "Changed this
  run" counts rows made real, made provisional, and taken back (and those created
  from inventory); "Unchanged this run" counts rows examined and left as they were;
  "State now" is one grouped query per clinical table: real, provisional
  (carrying a confidence) and unresolved rows. A rerun with nothing to do shows the
  same state with zero changes. The below-floor report covers the codes the run
  looked at (unresolved ones, and codes already provisional), not inventory-only
  codes unless `--include-inventory`.
- **Only rows under a code system the patient holds are proposed for.** A short
  Epic code repeats across hospitals' systems (on HealthTree 58,588 rows match one
  patient's codes by string and 997 are in a system she holds), so for a code the inventory holds, a row under
  another system is never proposed for, in a dry run or a real one (a code with
  unresolved rows and no inventory row says nothing about its system, and the
  resolver then looks it up by code alone, so its unscoped queue rows decide it); a held system
  with no row yet gets its row created first (and counted as one to propose for in
  a dry run, so the dry run's count is the real run's).
- **A code the ranker could not rank is never written.** A ranker that cannot run
  (no key, a 429, a timeout) does not raise: Suggest would write its first
  retrieval hit as the destination. This command calls `suggest_mappings` with
  `skip_fallback=True` (off for every other caller), which leaves such a row, and
  one whose ranking raised, untouched and unstamped; the run stops after the
  batch that had one, with a non-zero exit, and a rerun retries exactly those. A
  ranker's explicit decline is an answer: written, stamped, not retried without
  `--retry-declined`. A one-question preflight catches a missing or rejected key
  before the first batch.
- **Batched and resumable.** Suggest runs in batches (`--batch-size`, 25), so each
  batch's writes land before the next starts; a rerun skips codes already
  answered. Approved rows are never touched, and `--replace` redoes only
  destinations a previous Suggest run set, never an importer's or a curator's.
- **Queue rows it creates** (only for the codes it proposes for, so none for
  inventory-only codes without `--include-inventory`) are born like
  `enqueue_unmapped_source_codes`'s (proposed, no destination, Seen 1), one per code
  system the inventory holds the code under, with the inventory's source metadata and a name from Athena or UMLS,
  so the ranker sees what a curator sees. An HK-* source system, and a row the
  unique constraint (organization, system and code, case sensitive, any table)
  would refuse, are skipped and counted.
- `--dry-run` counts and writes nothing and calls no model; `--skip-propose`,
  `--strategies` narrow it; clinical values are printed only with
  `--show-values`; failures name the step and the exception's class, never its
  text. It refuses, writing nothing, with `PROVISIONAL_DATA_ENABLED` off, and runs
  as a Cloud Run job where `JEV_API_KEY` is in the environment. Code:
  `omop_core/services/fill_provisional.py`; `suggest_mappings` gained optional
  `mapping_ids` and `skip_fallback`.

### Replace re-answers rows, it does not delete them

It used to delete: while the candidate set came from a clinical scan, a deleted
row would be found again and recreated. Now that Suggest reads the tab, a
deleted row is a code that has left the queue for good, taking its
`occurrence_count` and `first_seen` with it.

### Vectors alone are not enough

Vectors retrieve by semantic similarity but the ranking model needs at least
one candidate with a code and name to evaluate. A run with neither UMLS nor
Lexical would report "no candidate concept" for every code. The API rejects
that combination and the checkbox disables itself.

### Curator priority is a tie-breaker, not evidence

Curators rank concepts on the Concept to Code tab (`ConceptMappingPriority`,
one global list, positions 1..N kept dense by
`services.concept_priority.move` under a table lock). The list shows ranked
concepts first on every page and filter. Suggest uses the ranking in two
places, both in `retrieval_pool`:

- **Retained, never added.** `priority_lexical_candidates` scores only the
  prioritized concepts in the code's domain at the same `MIN_TRIGRAM_SCORE`, so
  a ranked concept the best-N cut dropped comes back (`priority_retained`). A
  concept retrieval does not match is not added.
- **Marked for the ranker.** Every prioritized candidate carries
  `curator_priority`. Both prompts (Anthropic's system prompt, Jev's
  instructions and criteria tag) say to use it only between equally good
  matches. A winner that is prioritized gets "Curator priority N." on its note.

The UMLS single-concept shortcut ignores priority: a curated equivalence wins
without a ranking call either way.

A concept whose source codes are all mapped is marked **Done** from its
Priority cell (`ConceptMappingDone`, `POST concept-to-code/done/`, #2388).
Done unranks it, and ranking a Done concept un-does it (`concept_priority.move`),
so the two states are exclusive, the list stays dense and the tie-break never
prefers a finished concept; the list shows Done concepts after every other, in
italics, with "Done" where the number was, and "Not done" returns one to the queue.
It is reference data and travels with `copy_reference_data`.

A curator claims a concept with **Lock** on its row (`ConceptMappingLock`,
`POST`/`DELETE concept-to-code/<id>/lock/`, #2390). Other curators see a lock
icon with the holder's name, may open the concept and review its source codes,
and get 423 on every write for it: propose, apply, Suggest for the concept,
and any mapping save whose destination it is (`concept_lock.check_concept_lock`
in `_upsert_source_code_mapping`). The holder sees Unlock; staff may unlock
too. Explicit and unexpiring, unlike the mapping dialog's per-row edit lock.
System data, never copied.

Any curator leaves a **note** on a concept from the note icon before its Lock
(`ConceptMappingNote`, `PUT`/`DELETE concept-to-code/<id>/note/`, #2411): one
shared note per concept, last save wins, a blank save clears it. The list
returns it, the icon is filled when one exists and shows it on hover. A note
is commentary, not a mapping write, so another curator's lock does not refuse
it. Reference data: it travels with `copy_reference_data`, author cleared.

The tab's domain list has one entry that is not an OMOP domain: **Wearables**
(`domain=wearables`, `concept_to_code.WEARABLES_DOMAIN`, #2656). It lists the
standard concepts that a source code in `WEARABLE_SOURCE_VOCABULARIES` is
mapped to by a proposed or approved mapping, so the HealthKit, Health Connect,
OpenWearables, Garmin and HL7 PHR codes of one metric can be reviewed against
the one concept they should share. It is its own scope: the "Patient field
concepts" choice is set aside (few wearable concepts back a field), while a
disease, the search and "ranked only" still narrow it. A wearable code with no
destination has no concept to appear under, and the local, non-standard
`HK-Wearable` concepts are not listed, as on every other domain. The domain a
curator chooses, Wearables or any other, is saved per account in the browser
(`promop-concept-to-code:<user id>`, as the patient list's filters are) and
restored when the tab next opens. The page mounts the tab only once
`useAuth()` has loaded the account, so the first list request already carries
the saved domain and no choice is filed under no account. A concept's Mapped
Codes lists every code mapped to it, whatever its Seen count, so the list's
counts and the dialog agree (#2686): `concept_to_code_detail` applies
`seen_only` to `mode=available` only, and a platform's own seeded code, which
has Seen 0 until a patient sends it, is listed like any other. The "> 0 only"
checkbox belongs to Suggested Codes alone (the estimate's and Suggest's
`include_zero_seen`), as the Code to Concept page applies Seen > 0 only to its
Unmapped section. Under the Wearables domain it starts unchecked
(`seenOnlyByDefault`), because the codes to map there are the ones with no
sightings; under every other domain it starts checked.

Every concept on the list has a **Category**, the first column, and the list
is grouped by it (#2690). One definition,
`concept_to_code.concept_category(params)`, gives the expression the order,
each row's `category`, the per-category counts and the `category=` filter all
read; it is never stored. Under Wearables it is the metric category of the
concept's wearable mappings: the most frequent non-blank
`SourceCodeConceptMapping.category` among its non-rejected rows in
`WEARABLE_SOURCE_VOCABULARIES`, a tie going to the earlier entry of
`WEARABLE_METRIC_CATEGORIES`. Everywhere else a LOINC concept has its LOINC
class's display name (`LoincCodeClass` by primary key; the class code when the
name is blank), and a LOINC code with no class row, like every other concept,
has its `concept_class_id`. It is a string, `''` for none.

- **Order.** `order=category` (the default) is category, then today's order
  inside it: curator priority, Done last, name. Wearables follows the
  `WEARABLE_METRIC_CATEGORIES` order, every other domain is alphabetical
  without regard to case, and no category comes last. `order=priority` is the
  global ranked list as it was. Anything else is a 400. Priority stays one list
  across categories: a move or a drop sets the global position.
- **Grouping is bounded.** Sorting and counting by a computed value reads the
  whole filtered list, and "All standard concepts" is millions of rows. At or
  under `CONCEPT_CATEGORY_GROUP_LIMIT` (5,000) a category-ordered response is
  `grouped: true` and carries `categories: [{category, count}]` for the whole
  filtered list in display order, from one GROUP BY. Above it, and whenever
  `order=priority`, `grouped` is false, `categories` is null and the rows are
  in priority order; `order` in the response names the order in force and
  `group_limit` the limit. The list is counted once (`_paginate(count=)`), a
  count that does not filter on the category never computes it, and the
  endpoint's query count does not grow with the concepts (8; 7 ungrouped).
- **Filter.** `category=<value>` narrows to one category under the same
  definition (`__none__` for none) and combines with every other filter; an
  unknown value matches nothing.
- **The page.** A grouped list is one list of headers with no pagination: a
  button per category ("Uncategorized" for none) with its size across the
  whole filtered list. The page asks for it with `view=categories` (#2699): the
  same envelope with `results: []`, no row, field, lock or note read (4 queries,
  against 8 with rows), every filter applied to the counts; a list that is not
  grouped returns its page of rows instead, so one request serves both; absent
  or empty is today's list, anything else a 400. **Every category starts
  folded and no concept row is fetched until one is opened**: opening asks for
  `category=<name>` with the current filters, paged within the category by the
  page-size selector, with its own "Pages of <category>" control drawn only when
  the category holds more than a page. Each open category keeps its own page
  (reset by a filter or page-size change), at most six of these requests are in
  flight at once, folding discards the rows, and a write (`revision`) asks
  again for the headers and the open categories only. Collapse all folds all;
  Expand all opens every category, one request each, so it is offered only for
  12 categories or fewer. Remembered open categories are fetched after the
  headers on load, and no others. Drag-to-reorder works among the open
  categories; a drop on a row of another open category still sets the global
  position. A list too long to group says so and how to narrow it.
  Under the Wearables domain the controls row also carries the
  "Download all wearable mappings" CSV/Word buttons (everyone who reads the tab)
  and Share (`canApprove` only), the same components as the Source Code to
  Concept direction's Wearables tab.
- **Remembered.** The per-account object that holds the domain
  (`promop-concept-to-code:<user id>`) is `{"domain", "order"?, "expanded"?}`:
  `order` only when it is `priority`, and `expanded`, the categories the
  curator has opened per domain (`{"wearables": ["Sleep", ""]}`, `""` being
  Uncategorized). A category not listed is folded, so a new account, domain or
  category starts folded. A part of the wrong shape is ignored on its own, and
  an older `collapsed` list is not read.

### The six LOINC axes decide which candidates are the same test

A LOINC code is defined by component, property, time aspect, system, scale and
method; two tests that differ on any one get different codes. For a source in
the Measurement or Observation domain, Suggest (`omop_core/mapping/loinc_axes.py`)
settles the source's axes, then:

- **adds** the LOINC codes whose axes agree, when the component is known
  (retrieval label `axes`),
- **drops** every candidate that conflicts with a settled axis, before the
  ranker sees it, and reports them as `axis_excluded`,
- **orders** the rest by axes matched, then by compatible axes (`Ser` against
  `Ser/Plas`), and sends both sides' axes to Anthropic and Jev.

Source axes come from, strongest first: a curator's entry
(`SourceCodeConceptMapping.loinc_axes`; `null` clears an inferred axis, `''` on
`method_type` means methodless), the source code itself when it is LOINC, a
LOINC coding in the resource's `code.coding`, then the metadata -- a UCUM unit
names a property, `valueQuantity` names scale `Qn`, an exact specimen display
names a system. A unit that fits several properties (`%` is a mass, number,
volume or other fraction; `U/L` is catalytic or arbitrary concentration)
settles the property to that **set**: the properties of the LOINC codes that
give the unit as an example unit, read from the loaded `LoincCodeClass` table
(`loinc_axes.loaded_unit_properties`, cached per process) with
`AMBIGUOUS_UNIT_PROPERTIES` as the fallback. A candidate with any of them is
eligible; a mass concentration for a `%` is excluded. Sets are sorted, so
`U/L` and `IU/L` are one variant, keyed `property=ACnc,CCnc|scale_type=Qn`;
a row stored under the older `unit=%` key is re-keyed on refresh. A set reads
"any of …" in the dialog and "one of …" to the rankers. Free-text method and `effectivePeriod` are hints only: a wrong
constraint hides the right answer. Inference reads the metadata *as stored*,
because de-identification drops `specimen` (a Reference).

The candidate side is `LoincCodeClass`, which holds all six axes from
`Loinc.csv`. A load that predates those columns counts as stale at the same
version (`loinc_release._axes_missing`), so the next `sync_loinc_release`
reloads it.

### SNOMED destinations: hierarchy, attributes and subsumption

SNOMED has no fixed six-part name; a concept is its hierarchy (the semantic
tag -- in OMOP `concept_class_id`) plus defining attributes that differ by
hierarchy ("Has finding site", "Has asso morph", "Has method", ... in
`concept_relationship`), and Athena records "Has status" Defined/Primitive.
`omop_core/mapping/snomed_attributes.py` judges SNOMED candidates for the
Condition, Procedure, Observation and Measurement domains against the
source's **reference concept** -- what the source means at least: a curator's
choice (`SourceCodeConceptMapping.snomed_attributes`), the source code itself
when it is standard SNOMED, else its "Maps to" SNOMED target (ICD-10-CM).

Because SNOMED has subsumption, the rule is not LOINC's "any mismatch is
wrong":

| Candidate vs reference | Outcome |
|---|---|
| the same concept | eligible, first |
| an ancestor | eligible, ranked lower per level |
| a descendant | held back as `more_specific` -- needs curator evidence |
| another hierarchy, a contradicting attribute or side, or outside the reference's lineage (a sibling) | excluded as `conflict` |

A primitive concept's missing attribute is *unknown*, never a conflict.
Laterality lives on body structures; `"Side"` means *lateralisable* and is
not a side. Athena often maps a lateral code to an unlateralised concept
(M17.11 "right knee" -> "Osteoarthritis of knee"), so laterality is also read
from the source text: a one-level child that differs by exactly that side
counts as the reference, and a concept that cannot carry the side is kept but
flagged `laterality_not_captured`. Retrieval adds the reference and its
ancestors up to three levels (label `hierarchy`).

### One source code, several LOINCs: axis variants

A hospital code reported in mg/dL and mmol/L is two LOINC tests.
`omop_core/mapping/axis_variants.py` groups a code's units -- stored
Measurement rows, the imported inventory, per-patient source codes -- by the
axes they imply. Two or more groups set `axis_variant_count` (the queue's blue
"n axis values") and become `SourceCodeAxisVariant` rows, each with its own
destination.

Ingest routes by **unit**, the one per-record value every path has: the lookup
API (an entry with `source_unit` whose variant is approved comes back keyed
`vocab|code|unit`) and FHIR sync both take the variant's concept; every other
record takes the mapping's. Approving a variant re-points only stored rows in
its units (`repoint_clinical_rows(units=...)`). Specimen variation is reported
but cannot be routed.

Counts refresh after each source-code upload for codes seen in two or more
units, and in bulk with `manage.py refresh_axis_variants`. The bulk walk is a
worker job, never a migration step: `--queue` (or migration `0303`) records an
`AxisVariantRefresh` request that `omop_core.refresh_axis_variants` performs.
Migrations 0300 and 0301 ran it inline and could not finish inside the Cloud
Run migrate job's hour on the HealthTree instance (#2021).

The stored unit evidence (`SourceCodeConceptMapping.source_unit_evidence`) is
the instance's own: `--rebuild-evidence` (or migration `0307`) sets
`rebuild_evidence` on the request and the walk first rewrites every code's
units from the source codes on its loaded patients and the stored Measurement
rows (`axis_variants.rebuild_unit_evidence`, entries with `source:
"patient-data"`; a text term's own recorded units, which name no source, are
kept), then refreshes the variants, deleting one the evidence no longer
shows unless a curator decided it. Before the walk, a rebuild also sets every
mapped code's Seen count (`occurrence_count`) in one statement
(`axis_variants.rebuild_seen_counts`) to the sum of its per-patient rows for
its table, per vocabulary and code, so the order-system and result-system
codings of one test no longer share the extract's number (#2121); a code
with no patient rows here keeps its count, and the extract's label-group
figure is cleared on the rows rebuilt. Each per-patient source-code upload
runs the same pass for the codes it wrote and removed, and the lookup
endpoint's sighting (`resolve_source_code_variant(provisional=True)`) adds one
only while the code has no per-patient rows here, reading the rows once they
exist (#2123): a lookup is one ask per code per batch, so that count is
provisional; a per-record caller such as FHIR sync always adds one. The walk resumes after its stored `cursor`, not
an offset, because the key set shrinks as codes are rebuilt. Once an instance
has completed a rebuild, a seed or inventory load writes no unit evidence
over existing rows and does not raise the Seen count of a code that has
per-patient rows here (`axis_variants.rebuilt_seen_keys`; the
`import_healthtree_seen_counts` snapshot skips them too). The hospital code seed used to carry the inventory's
label-level counts on every code with the label (#2041).
A curator asks for the walk again after loading patients from the panel on
the Concept to Code tab (`POST concept-to-code/axis-variant-walk/`, #2050),
which also shows how many patients have source codes since the last walk
began. A rebuild counts the axis evidence it meets (`axis_evidence.Summary`,
stored on the request) so the panel can show whether any axis beyond property
and scale is ever settled or varies across patients (#2051).

### Concept embeddings

`semantic_candidates()` searches the `ConceptEmbedding` table by cosine
distance. Any standard concept without an embedding is invisible to vector
retrieval.

`manage.py build_concept_embeddings` embeds every standard concept (1,523,060
rows, ~2.8 hours on first run). It is **resumable** — it skips already-embedded
concepts, so an incremental run after a vocabulary load only embeds the new
concepts.

After a successful `load_athena_vocabularies` (including `--concepts-only`),
`load_mappings`, `sync_athena_mappings`, `load_umls_release`, `sync_umls_release`,
or any `import_*crossmap*` command,
`build_concept_embeddings` is dispatched automatically. With `CELERY_BROKER_URL`
configured, `omop_core.build_concept_embeddings` runs on a Celery worker;
without it, the command runs inline. Dispatch is deferred until commit and
nested mapping loads dispatch only once, after the outer load succeeds.
Dry runs and failed loads do not dispatch. Workers need sentence-transformers
and access to `BAAI/bge-small-en-v1.5`. `--skip-suggest-embeddings` (or
`--skip-embeddings`) on a loader suppresses the build for that load and its
nested loaders.

`manage.py precompute_suggest_embeddings` is a lighter alternative that embeds
only concepts the suggest queue can currently retrieve. It is available for
diagnostics (`--measure`) but is no longer auto-dispatched, because vector
retrieval needs all standard concepts embedded, not just the queue's candidates.

---

## Code Mapping List — paging for a full read

`GET /api/v1/code-mappings/` (a `system/*.read` service token may read it,
#2340) pages two ways (#2451, #2513, `_CodeMappingPage` in `views.py`):

| Params | Order | Use |
|---|---|---|
| `page=N` (default) | vocabulary, code, id | the curation UI; offset paging, so a row inserted or deleted mid-read shifts later pages |
| `sort=updated_at` + `cursor=<token>` | updated_at, id | a warehouse reading the whole table or a delta, in the shape HealthTree's own export endpoints take: a row written or re-saved mid-read gets a later `updated_at` than any read, so it comes round later in the same run once committed, and a deleted one leaves no gap. The `Link` next URL carries `cursor=`; there is no prev. `page` with either is a 400, and `sort` takes nothing else |

The cursor is `signing.dumps([updated_at, id, digest])` under its own salt:
opaque, and bound to the query by a digest of every parameter but `cursor`,
`limit` and `include_total`, so a token replayed against another
`updated_since` or filter is a 400 rather than a wrong page. Ties on
`updated_at` are real (a bulk writer stamps one clock value on a batch), so
the window is `updated_at >= u AND (updated_at > u OR (updated_at = u AND
id > i))`: the OR alone is a Filter that walks `ix_sccm_updated_at` from its
first entry on every page, and the redundant `>=` is what gives the planner
an index condition to start from. A tie of many rows (a bulk writer stamps
one value) is still walked on each page that falls inside it. A row stamped
inside a long writer transaction can commit after a reader's cursor has
passed its stamp, so the delta's `updated_since` margin must cover the
longest such transaction (`copy_reference_data`, the bulk commands), not
clock skew alone. The cursor survives a `SECRET_KEY` rotation only while the
old key is in `SECRET_KEY_FALLBACKS`. `limit` is honoured on both, 1 to
`CODE_MAPPING_MAX_PAGE_SIZE` (1000; out of range is a 400, not a silent
cap). The ceiling is the request budget: at ~1s per 100 rows on HealthTree,
5,000 would outrun gunicorn's 30s. `updated_since=<RFC 3339 instant>` (offset
required: a bare date or a naive datetime is a 400; a decoded `+` offset is
tolerated) narrows to rows whose `updated_at` is at or after it, for a
nightly delta. The delta is additive and tracks curation: a hard-deleted row
is never seen, so a client reconciles with a periodic full read and uses the
previous run's start time as the next `updated_since`
(`docs/code-mapping-api.md`). `auto_now` is skipped by
`bulk_update`/`.update()`, so every writer that changes a destination,
status, description or source concept sets `updated_at` itself
(`apply_icd10cm_mappings`, `enrich_source_mappings`,
`backfill_icd10_source_info`, `reference_transfer._apply_table`, which
stamps every `auto_now` column with this instance's clock, and the naming
branch of `resolve_source_code`); the Seen counters and unit evidence are
sightings and deliberately do not. `X-Total-Count` counts the filtered set:
on every offset page, and on a cursor read only with `include_total=true`,
since it is one full count of the table. `Link` URLs are absolute, so
behind a proxy they are `https://` only when `SECURE_PROXY_SSL_HEADER` is
trusted: automatic on Render and Cloud Run (`K_SERVICE`),
`TRUST_PROXY_SSL_HEADER=true` elsewhere.

## Code Mapping — Category, Measure Class and Metric Key (Wearables)

Three columns on `SourceCodeConceptMapping` say what a wearable mapping's
**source metric** is. None of them describes the destination concept: one
LOINC concept can be fed by a direct sensor reading from one source and an
algorithmic estimate from another, under different vendor codes.

| Field | What it is | Valid values |
|---|---|---|
| `category` | the metric's category in the wearable mapping proposal | '' or one of `source_vocabularies.WEARABLE_METRIC_CATEGORIES` (20: Activity, Fitness, Heart & Cardiovascular, Blood & Respiratory, Body Composition, Sleep, Mobility & Gait, Nutrition, Symptoms, Reproductive Health, Mental Wellbeing, Self-care, Hearing, Medication & Device Use, Safety, Self-reported, Stress & Autonomic, Environmental, Location, Garmin-specific) |
| `measure_class` | how the source produces the value | '' or one of `WEARABLE_MEASURE_CLASSES` (Direct measurement, Aggregate over interval, Algorithmic estimate, Vendor score, Environmental context, User-entered or device log) |
| `metric_key` | the vendor-neutral key of the metric (`steps`, `resting_hr`), shared by every source code that reports it: the HealthKit identifier, the Health Connect record type, the Garmin key, the OpenWearables code, the HL7 PHR code. It is what ties the wearable vocabularies together, since OpenWearables does not cover every metric. Not indexed: nothing filters on it yet | '' or lowercase snake case, `[a-z][a-z0-9_]*`, at most 64 (`clean_metric_key`) |

- **Where they show.** Only on the Wearables tab (`OpenWearables`, which also
  holds Apple, Garmin, HL7-PHR and HealthConnect rows): Category is the first column, with
  the measure class as a muted second line in the same cell, and Metric is
  the second, both sortable (`category`, `metric_key` on the browse endpoint,
  flat and grouped). Every other tab has neither column, and a Category or
  Metric sort is not sent for one: the page derives the sort it sends from
  the tab it is on. A grouped row names a value only when every member
  agrees (`Min` equal to `Max` in `mapping_rollup`, not `COUNT(DISTINCT)`,
  which cost every tab's grouped page a quarter more). In the Edit Mapping dialog they sit
  in the source block: Category beside BasedOn, then Measure Class and Metric
  Key. All three are in the list's CSV/JSON download.
- **Who sets them.** The wearable seeds, and anyone who can write mappings
  (`_can_manage_mappings`) on a row they may edit, through the mapping save
  (`_upsert_source_code_mapping`): a value outside the list, or a key of
  another shape, is a 400 naming the field. An approved mapping stays with
  org_admin and staff, as for every other field: an analyst's save of one is
  a 400 naming `status`, and the Apple and Garmin rows are seeded approved.
  The dialog offers the two lists
  from `code-mappings/reference/` (`wearable_metric_categories`,
  `wearable_measure_classes`, and `wearable_source_vocabularies` for which
  rows show the fields), sends a field only when the curator changed it, and
  sends none for a row off the Wearables tab. The server does not tie the
  fields to a vocabulary: it validates the value only. The dialog's GET of
  the mapping supplies the stored values when the row it opened from has
  none (a patient page row), and they are applied again whenever the form is
  rebuilt, as are the stored note and unit.
- **Seeds fill them.** `seed_openwearables_mappings`, `seed_hl7_phr_mappings`
  and `seed_wearable_device_mappings` set all three when they create a row,
  from maps kept beside each metric list (`*_CATEGORIES`,
  `*_MEASURE_CLASSES`, `*_METRIC_KEYS`; `tests/test_mapping_category.py`
  holds every seeded code to a valid value of each, and codes sharing a key
  to one category and measure class). `--update-seeded` fills a blank one on
  any row, whatever its status, and never replaces one that is set; the
  device seed does the same on every run. A fill stamps `updated_at`, since
  the list API returns the fields. An OpenWearables metric the proposal gives
  no internal key is keyed by its own code. `seed_hl7_phr_mappings` seeds
  three vocabularies from one table (`PHR_SOURCE_METRICS`: the HL7 PHR code,
  HealthKit identifier and Health Connect record of each wearable metric), as
  proposed `HL7-PHR`, `Apple` and `HealthConnect` rows that carry the metric's
  one key; it skips the HealthKit codes the device seed owns. See
  [wearable-omop-mapping.md](docs/wearable-omop-mapping.md#hl7-phr-hl7-phr-healthkit-and-health-connect-codes).
- **Blank means "not yet described", not "none".** A curator who clears one
  of the three gets it back from the next fill: `load_athena_vocabularies`
  runs the device seed after every load, and `--update-seeded` does it for
  the other two. A value a curator *changed* is never replaced. To say a
  metric has no sensible value, pick the nearest one; there is no way to pin
  a blank.
- **Rollout.** Nothing runs `seed_openwearables_mappings` or
  `seed_hl7_phr_mappings` automatically. After the migration deploys, rows
  those two seeded earlier show a blank Category and Metric until each is run
  with `--update-seeded`; the Apple and Garmin rows are filled by the next
  vocabulary load, or by running `seed_wearable_device_mappings`.
- **They travel with `copy_reference_data`.** A payload written before the
  columns leaves a target row's values alone and creates rows with blanks
  (`_CODE_MAPPING_NOT_CARRIED_WHEN_ABSENT`).

## Code Mapping — Downloading the Wearables Tab

`GET /api/v1/code-mappings/wearables-export/?file_format=csv|docx` returns
every mapping on the Wearables tab as one file, built on the server
(`omop_core/services/wearable_mapping_export.py`). On the Wearables tab, and on the Concept
to Code direction under the Wearables domain (#2699), the page offers it as
"Download all wearable mappings": CSV or Word document
(`WearableMappingsDownload.tsx`). The section Download menus are a different
thing and are unchanged: they write, in the browser, the rows a section has
loaded.

- **The parameter is `file_format`, not `format`.** DRF reads `format` itself
  (`URL_FORMAT_OVERRIDE`) and answers 404 for a name it has no renderer for,
  before the view runs. No value means `csv`; any other value is a 400.
- **Who.** Whoever may read the mapping pages (`_can_view_mappings`), a
  read-only doctor included. It is reference data: no patient value, no
  reviewer, and no `notes`, which hold Suggest's rationale and curators' text.
- **Which rows.** The tab's own, through `mapping_browse.tab_queryset`: every
  vocabulary in `WEARABLE_SOURCE_VOCABULARIES`, in every section, **without
  `rejected` rows and without a hospital's own override of a code** (the file
  is for use outside the app, where that row would read as an unexplained
  second line). No search, provenance or Seen filter applies. One query,
  whatever the row count. The Word document is refused with a 400 above
  `DOCX_MAX_ROWS` (5,000): it is built in memory in the request at about
  1.4 ms a row; the CSV has no limit. Characters XML cannot hold are dropped
  from the Word text, since one in any stored code would fail the file.
- **Source code system.** `sources=Apple,Garmin` (ids, comma-separated or the
  parameter repeated; default none, which is every system) limits the file to
  those systems (`export.clean_sources`; an id that is not on the tab is a
  400 with fixed wording). The choices are `wearable_source_systems` in
  `code-mappings/reference/` (`export.source_systems()`: id and label in
  `SOURCE_LABELS` order). The limit is applied last, after a CDISC Biomedical
  Concept has been linked to each code, so it never changes which BCs a code
  reaches. A limited file's name ends with the lower-cased ids and the Word
  document says "Source code systems: ..." under its title. The CDISC BC
  system is refused with the CDISC Biomedical Concept target (`SourceTargetError`,
  a 400): the target maps *to* those concepts, so the file would be empty; the
  select hides that option under that target. The Download menu
  shows a "Source code system" select beside "Map codes to" (default "All
  source systems") on both Wearables controls; the public vote page has none,
  since its share already decides (below).
- **Order.** Category in `WEARABLE_METRIC_CATEGORIES` order, blank last; then
  metric key, blank last; then source (HealthKit, Health Connect, Garmin,
  OpenWearables, HL7 PHR, then any other vocabulary by id); then source code.
  The labels and that order are `SOURCE_LABELS` in the service: a vocabulary
  added to the tab shows under its id, after the others, until it is listed
  there.
- **CSV.** UTF-8 with a byte order mark (descriptions carry "°C" and "μS",
  which Excel misreads without one) and `\n` line ends. A header row, then one line per mapping: Category, Measure class, Metric
  key, Source, Source code, Description, Source unit (the codes in
  `source_unit_evidence`, joined with "; "), Destination vocabulary,
  Destination code, Destination concept, OMOP concept ID, Domain (the
  destination's, or the one the code is filed under while it has none),
  Destination unit, Status, Provenance (`origin_system`). Text a spreadsheet
  would run as a formula gets a leading apostrophe
  (`disease_field_sets.csv_cell`).
- **Word document.** `python-docx`. Landscape Letter; a title, a line of
  counts, a legend, then a Heading 1 and one table per category that has
  rows ("Uncategorized" last), each heading with its row count. Columns:
  Metric, Source, Source code, Destination, Unit, Status. The table layout is
  fixed at `DOCX_COLUMNS`' widths, so a long HealthKit identifier wraps
  inside its cell at zero-width spaces (`wrappable`); those are in the Word
  text only, never the CSV. A metric's rows read as one Metric cell: the text
  is on the first row and the rules between them are not drawn. That is cell
  borders, not a vertical merge, because the rows are also kept on one page
  (keep-with-next) and LibreOffice misdraws a merged cell under that. The
  header row repeats on each page, no row splits across pages, and the
  footer numbers the pages.
- **The same rows on the same day give the same bytes.** The document
  properties are fixed strings and the archive's member times are the day's,
  so a test can compare two downloads.
- **It is built in the request.** About 1.4 ms a row for the Word document
  (1,000 rows in 1.4 s), far inside gunicorn's 30 s at the tab's size. A tab
  of tens of thousands of rows would need a queued job instead.

Tests: `tests/test_wearable_mapping_export.py`,
`WearableMappingsDownload.test.tsx` and
`CodeMappingPage.wearablesDownload.test.tsx`.

## Code Mapping — Public Voting on Wearable Proposals

A mapping administrator shares a link; people with no account (an HL7 working
group) vote Yes or No, or comment, on the wearable mapping proposals (#2678).
Rules in `omop_core/services/mapping_votes.py`, endpoints in
`patient_portal/api/mapping_votes.py`, on `/api/v1/` only.

**A vote never changes a mapping.** Approval stays with staff and org admins
(`_can_approve_mappings`); votes are advice they read.

**A submission is not a vote. Nothing is recorded until the voter confirms
from the emailed link.** The public page stores a `MappingVoteSubmission` (the
share, the lowercased address, the JSON payload) and a confirmation link is
emailed. Pending submissions are never counted and never shown to a curator;
they live 48 hours and are deleted when confirmed, when expired, when the
email could not be sent, and when their share is revoked or deleted. Only
`POST public/mapping-votes/confirm/` writes `MappingVote` rows, and the page
calls it only from the Confirm button. Loading the page calls
`confirm/preview/`, which changes nothing however often it is called.

**Tokens are never in a URL the server sees.** The links are `/vote#<token>`
and `/vote/confirm#<token>`: the token is the fragment, which a browser sends
to no server, so it is in no access log, Referer, audit row or error report.
The pages send it in a JSON body, and every public endpoint is a `POST` for
that reason, the read of the page included. A token is checked against
`[A-Za-z0-9_-]{20,128}` before any query; anything else is the neutral 404.

| Endpoint | Who | What |
|---|---|---|
| `GET code-mappings/vote-shares/` | `_can_view_mappings` | the shares with confirmed voter counts, `can_manage`, `email_configured`, `base_url_configured`; a share's `url` only for an administrator |
| `POST code-mappings/vote-shares/` | `_can_approve_mappings` | `{title, note?, expires_at?}` -> a share and its public `url` |
| `POST code-mappings/vote-shares/<id>/revoke/` | `_can_approve_mappings` | the link stops opening; confirmed votes are kept |
| `DELETE code-mappings/vote-shares/<id>/` | `_can_approve_mappings` | erases the share with its votes, the addresses on them and anything pending |
| `GET code-mappings/vote-shares/<id>/votes-export/` | `_can_approve_mappings` | CSV, one line per vote, with voter addresses |
| `GET code-mappings/<id>/votes/` | `_can_view_mappings` | a mapping's votes and current tally; `voter_email` only for an administrator |
| `POST public/mapping-votes/` | anyone | `{token}` -> the page: title, note, proposals by category |
| `POST public/mapping-votes/submit/` | anyone | `{token, email, votes: [{mapping, choice, comment}]}` -> 202 |
| `POST public/mapping-votes/confirm/preview/` | anyone | `{token}` -> the pending votes; records nothing |
| `POST public/mapping-votes/confirm/` | anyone | `{token}` -> records the votes, once |

- **Who manages a share.** Creating, revoking and deleting one, the CSV, a
  share's link and a voter's address are for staff and org admins
  (`_can_approve_mappings`), not for everyone who writes mappings: an
  `analyst` grant can be a self-signup at a public demo org, and a share
  sends mail from this deployment and gathers strangers' addresses. An
  analyst or a doctor sees that shares exist, the tallies, the choices and
  the comments. A machine credential reaches none of it
  (`is_machine_request`), whatever its scopes.
- **A share can be limited to some source code systems.** `MappingVoteShare.
  source_vocabularies` (a list of vocabulary ids, empty is every system; set
  by `POST code-mappings/vote-shares/` as `source_vocabularies`, an unknown id
  is a 400) bounds everything the link reaches: the proposals on the page
  (`votable_mappings(share)`), what a voter may vote on (submit, the
  confirmation preview and `confirm` all read the share's own list, so an id
  from another system is the same "not on the page" 400, and a vote already
  pending for one that left is skipped at confirmation), and the public
  download, which the visitor cannot widen. The public page names the systems
  of a limited link (`source_systems`, labels; empty for every system) as
  "Limited to: ...". A stored system that has since left the tab is dropped
  from the public download; a share left with none answers 400 rather than
  becoming every system. `votable_mappings`, `votable_rows` and `clean_votes`
  take the share as a required argument, so none can default to every system. The Share dialog's "Source code system" select
  defaults to all systems and a listed share says "HealthKit, Garmin only".
  The share's own votes CSV is unaffected. Editing a share's systems is not
  offered: make another link.
- **What is put to the vote.** Read live, never snapshotted: rows of the
  `WEARABLE_SOURCE_VOCABULARIES` that are `proposed`, global (`organization`
  null), carry a `metric_key`, and have a destination concept loaded here
  (`mapping_votes.votable_rows`, in the export's order). The metric key is
  what says somebody described the row: ingest queues codes under these
  vocabularies from producer text, and Suggest can give one a destination.
  A proposal approved or rejected later leaves the page; a new one joins it.
- **The public payload is reference data only**: metric key, measure class,
  source, source code, description, destination vocabulary, code and name,
  and the curated destination unit. No notes, reviewer, counts, organization,
  provenance, source-unit evidence (it can come from patient rows), other
  voters' votes, tallies or addresses. `tests/test_mapping_votes.py` asserts
  the exact key set; a new key is a decision, not a side effect of the row
  serializer, which the public code does not use.
- **One vote per share, address and mapping.** A later confirmed submission
  replaces that voter's vote on each mapping it names and leaves the others.
  A vote stores the destination it was cast on (`concept_id`). Once the
  mapping moves to another destination the vote is **stale**: kept, marked,
  and left out of the tally (and counted again if the destination returns).
  At confirmation a proposal that was approved, rejected, deleted, moved or
  lost its metric key since the page was read is skipped and the thank-you
  page says how many. Deleting a mapping deletes its votes
  (`copy_reference_data --prune` included).
- **Confirming.** One transaction, a fixed number of queries whatever the
  number of votes: the proposals are read once and the votes written as one
  upsert in mapping order. A voter's confirmations on a share take turns
  (an advisory lock on the share and address, taken before any row), so two
  submissions naming the same proposals cannot deadlock, and one token
  confirmed twice at once records once.
- **Public views authenticate nobody** (`authentication_classes = []`): no
  session is read or created, no cookie set, no CSRF check, and a stale or
  flagged session changes nothing (`ForcePasswordChangeMiddleware` exempts
  `/api/v1/public/mapping-votes/`; a test holds every route under that prefix
  to these views). They parse and render JSON only: the browsable API would
  draw a form around the visitor's own submission. Every response is
  `Cache-Control: no-store`.
- **One answer, whoever asks.** Unknown, malformed, revoked and expired
  share tokens are the same 404 body; used, expired, malformed and unknown
  confirmation tokens, and one whose share has since closed, are the same
  404 body. A submission gets the same 202 whether the address is new,
  returning, over its own daily limit, or the send failed. A mapping id that
  is not on the page is a 400 that names no id.
- **Refused, not crashed.** JSON nested past the parser's stack is a 400
  (`RecursionError` is caught around `request.data`); text that is not valid
  Unicode (a lone surrogate from a JSON escape) is a 400 in a comment, an
  address, a title or a note; a body over 200 KB is a 413; an expiry is
  converted to UTC and must fall within two years.
- **The confirmation email is fixed wording**: the share's title, the number
  of votes and comments, the link, the expiry. Nothing a voter typed is in
  it, nor the share's note, or anyone could make promop send their text to
  an address of their choosing. The title is one line and may hold no link
  (`http://`, `https://`, `www.`). Comments, addresses and tokens are not
  logged; a failed send logs the kind of error only, since a mail library's
  message names the recipient.
- **The email is sent off the request** (`mapping_vote_jobs.py`, the
  `derivation_jobs` rule): a worker task when `CELERY_BROKER_URL` is set,
  inline otherwise. The task's only argument is the submission's id. The
  confirmation token is made by whoever sends the email
  (`send_confirmation`), stored only as its SHA-256, and so is never in a
  task argument, the broker or a report that quotes them. A job delivered
  twice mails once. A send that fails deletes the submission and its email
  record: an email that never left counts against nobody. The mail backend
  gets this message's own timeout (`MAPPING_VOTE_EMAIL_TIMEOUT`, 10 s),
  passed to the connection, so no other sender's changes.
- **Voter addresses are personal data.** Returned by the votes endpoint and
  the CSV only to staff and org admins. The voting page says beside the
  email field that the address and comments are stored and shown to the
  organisation's mapping administrators, and that votes are advisory.
  **Delete** on a share erases its votes and their addresses; that is how a
  voter's request to be removed is met, and how junk votes go. All four
  models are **system** data in `instance_data.py`: never copied between
  instances.
- **Limits by client address.** Three scopes on `PublicVoteThrottle`,
  env-tunable:

  | Scope | Default | Variable |
  |---|---|---|
  | `mapping_vote_read` | 60/minute | `MAPPING_VOTE_READ_THROTTLE_RATE` |
  | `mapping_vote_submit` | 30/hour | `MAPPING_VOTE_SUBMIT_THROTTLE_RATE` |
  | `mapping_vote_confirm` (preview and confirm) | 60/hour | `MAPPING_VOTE_CONFIRM_THROTTLE_RATE` |

  Submitting is generous on purpose: a working group voting from one meeting
  room is one address. The client address is read where the deployment says,
  `MAPPING_VOTE_PROXY_HOPS`: 0 (the default) is `REMOTE_ADDR`; N is the Nth
  entry from the right of `X-Forwarded-For`, the one the outermost of N
  trusted proxies wrote. **Measure N per platform** (Render, Cloud Run) by
  looking at the header a request arrives with: one too many and the entry
  is whatever the client typed, so a forged header is a fresh bucket; one
  too few and it is a proxy's own address, so every voter shares one bucket.
  DRF's `NUM_PROXIES` is left unset for every other throttle. These count in
  the cache (`CACHE_URL` or a Redis broker, else per worker).
- **Limits on outgoing mail, counted in the database** under advisory locks
  taken in one order (the address, the share, the deployment), so
  simultaneous submissions cannot each read "one below the limit":

  | Limit | Default | Setting | Over it |
  |---|---|---|---|
  | pending submissions per share and address | 3 | `MAPPING_VOTE_MAX_PENDING_PER_EMAIL` | the oldest is replaced |
  | confirmation emails per address per 24 h, all shares | 5 | `MAPPING_VOTE_MAX_EMAILS_PER_DAY` | the usual 202, nothing sent |
  | confirmation emails per share per 24 h | 200 | `MAPPING_VOTE_MAX_EMAILS_PER_SHARE_PER_DAY` | 503 |
  | confirmation emails per 24 h, every address | 500 | `MAPPING_VOTE_MAX_EMAILS_PER_DAY_TOTAL` | 503 |

  The two that concern no particular address answer 503 "Voting is
  temporarily unavailable", rather than tell voters to wait for mail that
  will not come; they are checked first, so an address over its own limit
  gets the same 503 as everyone else. At the pending limit the oldest
  submission gives way, because refusing the new one would let anybody who
  knows a voter's address lock that voter out for two days. Counting is of
  emails sent (`MappingVoteEmail`, a keyed digest of the address, kept when
  its submission or share is deleted), so neither confirming nor deleting a
  share buys more. **An address is counted without its `+tag`**
  (`canonical_email`), so plus-addresses of one inbox share one allowance;
  each still votes under the address as typed. Addresses no mail can reach
  are refused: IP-literal and single-label domains, `localhost`, control
  characters in the local part.
- **Purging.** Nothing in the project schedules jobs. Expired submissions and
  email records past 24 h are deleted on every submission and on every read
  of a voting page; `manage.py purge_expired_vote_submissions` does the same
  for a cron on a deployment nobody is voting on.
- **Where curators see votes.** The list queries are annotated with each
  row's current tally (`with_vote_counts`: three subqueries on the page's own
  query, no query per row; a service token's read is not annotated) and the
  Wearables tab shows "3 yes · 1 no · 2 comments" under the destination. The
  Edit Mapping dialog has a collapsed Votes section on wearable rows
  (`MappingVotesSection.tsx`). Share, on the Wearables tab beside the
  download and only for staff and org admins (`isStaffOrOrgAdmin`), lists the
  shares with Copy link, Download votes (CSV), Revoke and Delete
  (`WearableVoteShare.tsx`), and warns when `email_configured` or
  `base_url_configured` is false.
- **The pages.** `/vote` and `/vote/confirm` are routed in `App.tsx` before
  `useAuth()` is called: that hook asks who is signed in and sends a stranger
  to the login page. They read the token from `location.hash`, use
  `publicApi` only and render everything as text.
- **Deployment.** The links are built from `APP_BASE_URL`, which must be the
  public origin that serves the SPA. Email needs a real backend
  (`MAILGUN_API_KEY` and `MAILGUN_SENDER_DOMAIN`, or an `EMAIL_BACKEND`):
  with the console backend no voter ever receives a link, and the Share
  dialog says so. With a broker, the **worker** sends the mail, so it needs
  the mail variables, `APP_BASE_URL` and `DEFAULT_FROM_EMAIL` as well as the
  web service. Set `MAPPING_VOTE_PROXY_HOPS` on the web service.

Tests: `tests/test_mapping_votes.py`; `PublicVotePage.test.tsx`,
`ConfirmVotesPage.test.tsx`, `WearableVoteShare.test.tsx`,
`MappingVotesSection.test.tsx`, `CodeMappingPage.votes.test.tsx`,
`EditMappingDialog.votes.test.tsx`.

## Code Mapping — The Approver Cannot Be the Proposer

`proposed_by` (on `SourceCodeConceptMapping` and `SourceCodeAxisVariant`) names
the curator behind the destination now on a row, and a curator may not approve
what they proposed (#2726). Rule and wording: `omop_core/services/mapping_approval.py`;
the writer is `_upsert_source_code_mapping`. Full description:
[concept_to_code_architecture.md](docs/concept_to_code_architecture.md#approval-needs-a-second-person-2726).

- **Stamped** by a person's save that creates a row, moves its destination or
  re-opens it from rejected; **kept** by note fixes and un-approvals; **null**
  for machine proposals (Suggest and its group, Replace and reverse runs, a
  Concept to Code propose/apply of a candidate a ranker chose, importers, service tokens) and
  for rows that predate it. A writer that moves a destination for a machine
  clears it (`machine_proposal=True` on `_upsert_source_code_mapping`, or
  `proposed_by = None` in the bulk writers).
- **Refused** with a 403 `{"detail", "code": "self_approval"}` (a DRF `PermissionDenied`), before any
  save, including propose-and-approve in one request. Group approve skips the
  actor's own proposals; a file upload that approves its uploader's proposal is a
  400 with one error per row; axis variants follow the same rule.
- **Switch**: `MAPPING_SEPARATE_APPROVER`, on where `DEBUG` is off, off under
  `DEBUG` unless the variable is set. A one-administrator deployment sets it to
  `false`. A test of the rule sets `settings.MAPPING_SEPARATE_APPROVER = True`.
- Field mappings and custom fields are not covered. Row payloads carry
  `proposed_by_id` (null when off); the pages disable Approve for the proposer.

## Code Mapping Lookup — Unit Conversion

`POST /api/v1/code-mappings/lookup/` accepts optional unit fields per code entry
and returns a `unit_conversion` block on resolved mappings when `source_unit` is
provided.

### Request fields (per entry in `codes`)

| Field | Type | Required | Description |
|---|---|---|---|
| `source_unit` | string | no | UCUM unit code from the source system |
| `quantity` | number | no | Representative quantity value to convert |

### Response: `unit_conversion` (nested in each resolved mapping)

```json
{
  "source_unit": "mg/dL",
  "destination_unit": "mmol/L",
  "destination_unit_concept_id": 8753,
  "destination_unit_source": "mapping",
  "source_quantity": 100.0,
  "converted_quantity": "5.551",
  "property_code": "SCnc",
  "match_type": "convertible"
}
```

| Field | Values |
|---|---|
| `destination_unit_source` | `"mapping"` (from `destination_unit_concept` on the SCCM row) or `"property"` (LOINC property fallback to base unit) |
| `match_type` | `"exact"` (same unit), `"convertible"` (same property group), `"incompatible"` (different groups), `null` (no destination unit) |

The key is backward compatible: callers that omit `source_unit` and `quantity` see
no `unit_conversion` key in the response. Unresolved codes never get the block.

### Implementation

- `omop_core/services/canonical_units.py` — `resolve_mapping_conversion()` computes the conversion using existing `convert()` and `UNIT_GROUPS`
- `patient_portal/api/views.py` — `code_mapping_lookup()` batch-fetches `destination_unit_concept` and attaches the block
- Tests: `patient_portal.tests.CodeMappingLookupUnitConversionTest` (9 cases)

### The destination unit on the Code to Concept list

Every tab's list has a "Dest. unit" column after Destination, so a curator
sees the unit a mapping will store values in before approving it. The cell
shows, in order: the mapping's curated `destination_unit_concept`
(`destination_unit_code`, with `destination_unit_name` on hover); else the
destination concept's `suggested_unit`, muted and marked "suggested", which is
a LOINC example unit and converts nothing until a curator sets a unit; else a
dash. A grouped row names a unit only when every member has the same one
(`mapping_rollup`, the destination's own rule: no unit against a unit is a
disagreement) and says "Mixed" otherwise (`mixed_destination_units`); its
`suggested_unit` is that of a unanimous LOINC Measurement destination
(`concept_unit_info.suggested_unit`). List querysets join
`destination_unit_concept`, so the column costs no query per row; the grouped
query aggregates only the unit's id and reads the page's units in one query
afterwards. The table scrolls sideways rather than clip a column.

In the Edit Mapping dialog, Destination Unit sits among the destination fields
where Destination Status was, and is shown for any row that has a stored unit,
whatever its table. The status is plain text ("Active", or "Retired (reason
U)") on the line of the Mint new concept button. The form's unit is the
mapping's own: the dialog's GET of the mapping replaces what the form opened
with (a patient page row carries no unit) unless the curator has picked or
cleared one since, nothing is matched from the source unit until that GET has
settled, and a save whose GET failed leaves `destination_unit_concept_id` out
rather than send a blank, which the server stores as no unit. Moving an
existing mapping from the conflict offer sends a unit only when the curator
chose one in that dialog.

The dialog's LOINC axes and SNOMED evidence sections open closed
(`SectionToggle`), with a marker on the heading for what they hold (curator
entries as "n set" and "n cleared", on one rule for both). They load and
report to the dialog either way, so Suggest sends the same axes and settings
whether or not they were opened. LOINC axes starts open for a code the queue
flags with axis variants, and opens when the dialog writes into it (the
variants' Suggest, Use on a variant's candidate); a re-read keeps axes typed
and not yet saved.

### Write-time conversion of stored rows

The bulk measurement write, repoint and resolve convert a stored
`value_as_number` and its range **only** to an approved mapping's curated
`destination_unit_concept` (#2157), in `canonical_units.normalize_measurement_units`.
`unit_source_value` keeps the unit the value was sent in; `unit_concept` names
the unit it is stored in.

- **No inferred target.** The `"property"` base-unit fallback above is for the
  lookup response only. Written to a row, g/L and /L do not fit the column:
  246 10\*3/uL is 2.46e11 /L (overflow), 2 ng/mL is 0.000002 g/L (stored as 0).
  A `CanonicalUnitPreference` is a read-time display setting, never a write target.
- **Axis variants are not converted.** A variant has no curated unit, and the
  mapping's was chosen for the mapping's LOINC. Rows in a variant's units stay as sent.
- **Exact or not at all.** When the converted value or either range bound
  does not fit `Decimal(15, 5)` exactly, the row keeps its numbers *and* its
  `unit_concept`: converting one without the other would mislabel the value.
- **Reads prove the unit (#2249).** A UCUM `unit_concept` that names a different,
  convertible unit from `unit_source_value` is only a hint of conversion.
  `canonical_units.stored_unit` / `value_in_source_unit` let the number in
  `value_source_value` decide: equal to the stored value, it is in the source unit;
  equal to its conversion, in `unit_concept`'s. Anything else is unknown and is
  not read, nor are older results of the same test; the lab-results and PHR labs
  APIs label a value with its stored unit and show no number, range or flag for
  an unknown one (`is_unproven`). Conversion writes the number
  as sent into `value_source_value` when it is empty, so its rows stay provable.
- **The ETL cleans units, promop reads only valid ones (#2248).** The ETL keeps
  `unit_source_value` as received and puts the UCUM unit it means in
  `unit_concept`. Blood counts (WBC, ANC, ALC, AMC, platelets;
  `clinical_units.count_unit`) read the text when
  promop knows it, else that concept. promop keeps no table of lab spellings.
  Rows whose text promop can't read are never converted on write.

---

## Deployment

- **`start.sh`** runs `scripts/prepare-deployment.sh` on every deploy, which migrates — so migrations pushed to `main` are auto-applied on next Render deploy. Cloud Run runs the same script as a release-phase job (#1628).
- **Push to `main`** triggers deploy (once Render GitHub App access is granted in dashboard).
- **Admin credentials**: set via `ADMIN_EMAIL` / `ADMIN_PASSWORD` env vars on Render (no hardcoded default)
- **Hostnames and connection strings**: do not commit them — this is a public repo. Service URLs and database hostnames live in `.env` and the Render/GCP dashboards.

<a id="render-api-access"></a>
### Render API access (staging and production)

Both environments are reachable from a local session through the Render REST API, with
`RENDER_API_KEY` from `.env`. Use it — not the dashboard — to set the environment variables a new
feature needs, to redeploy, and to run one-off commands. Look services up by name; do not commit
service IDs, and never print a secret value: pipe it from `.env` into the request body.

| Service | Branch | Gets variables from |
|---|---|---|
| `promop-staging` | `dev` | set directly |
| `promop-staging-worker` | `dev` | `fromService: promop-staging` in `render.yaml`, but the worker holds its own **copy**, refreshed only by a Blueprint sync — set it directly too |
| `promop` | `main` | set directly |
| `promop-worker` | `main` | set directly — `render.yaml` does not link prod worker variables to the web service |

A variable a worker needs (anything Celery tasks read, e.g. `ANTHROPIC_API_KEY` for queued
Suggest runs) must be set on the worker as well as the web service.

```bash
set -a; . ./.env; set +a
API=https://api.render.com/v1
A=(-H "Authorization: Bearer $RENDER_API_KEY" -H "Content-Type: application/json")
sid() { curl -s "${A[@]}" "$API/services?name=$1&limit=5" | jq -r --arg n "$1" '.[].service | select(.name==$n) | .id'; }

# Set a variable (value read from .env, never echoed). 200 = set.
jq -n --arg v "$ANTHROPIC_API_KEY" '{value: $v}' |
  curl -s -o /dev/null -w '%{http_code}\n' -X PUT "${A[@]}" \
    "$API/services/$(sid promop-worker)/env-vars/ANTHROPIC_API_KEY" --data @-

# Setting a variable does not redeploy. Deploy the branch's current commit. 201 = started.
curl -s -X POST "${A[@]}" "$API/services/$(sid promop-worker)/deploys" -d '{}' | jq -r '.id + " " + .status'

# Poll a deploy until it is live (or *_failed / canceled).
curl -s "${A[@]}" "$API/services/$(sid promop-worker)/deploys/<deploy-id>" | jq -r .status

# One-off command against an environment's own database and settings (read-only checks).
jq -n --arg c "python manage.py audit_sct_history" '{startCommand: $c}' |
  curl -s -X POST "${A[@]}" "$API/services/$(sid promop)/jobs" --data @-
```

A redeploy ships the branch's **current** commit, so redeploying `promop`/`promop-worker` releases
whatever is on `main`. A Blueprint sync is still what applies `render.yaml` infrastructure changes.

## Database Selection Rule

**Use local PostgreSQL for development and tests. Use Render databases only for migrations and sync checks.**

| Purpose | DATABASE_URL |
|---|---|
| Running tests | `postgresql://postgres@localhost:5433/promop_test` |
| Local development (manual testing, sync uploads, shell exploration) | `postgresql://postgres@localhost:5433/promop_dev` |
| Staging migrations, writes and sync checks | `$STAGING_DATABASE_URL` |
| Production read-only checks | `$PROD_DATABASE_URL` |

`.env` holds `STAGING_DATABASE_URL` (Render staging) and `PROD_DATABASE_URL` (Render production)
and **no `DATABASE_URL`**, so nothing reaches a remote database unless a command names it — see
[Two databases in `.env`](#two-databases-in-env). Set `DATABASE_URL` on every command, and use
`${VAR:?}` so a missing variable fails instead of expanding to empty.

Production is for reading and verification only. Never write to production data from here —
production is loaded from a staging dump, so fix and verify on staging. Production's *services*
(env vars, deploys) are managed through the Render API: see [Render API access](#render-api-access).

Never use remote databases for running tests — use local PostgreSQL to match CI. Never write
test data into `promop_dev` — use `promop_test` for automated tests.

---

## FHIR Upload Architecture

promop's own FHIR upload endpoint is **optional**. Production data is loaded by a
separate ETL project that reads FHIR itself and writes OMOP rows through the
bulk OMOP endpoints below; it never calls this endpoint. The endpoint remains
for the synthetic bundles `generate_fhir_bundle` produces and for ad hoc loads.
Either way the direction is the same, OMOP first and then derive, which is the
opposite of an interactive edit; see
[Write Architecture: PatientRecord First](#write-architecture-patientrecord-first).

The FHIR upload endpoint in `patient_portal/api/views.py` (`upload_fhir_bundle`):

1. Parses the FHIR Bundle entries by `resourceType`
2. Creates a `Person` record from `Patient` resources
3. Iterates `Observation` resources → writes each to the OMOP `Measurement` table (LOINC concept lookup, falls back to `measurement_source_value`)
4. Iterates `Condition` resources → writes to `ConditionOccurrence`
5. Iterates `MedicationRequest`/`MedicationStatement` → writes to `DrugExposure`, `Episode`, `EpisodeEvent`
6. After all OMOP writes, `refresh_patient_record` is called once to rebuild the `PatientRecord` read model from OMOP data

**`PatientRecord` is never written to directly during FHIR upload.** All clinical data goes into OMOP tables; `PatientRecord` is derived automatically via the `post_save` signal chain.

---

## Deferring the PatientRecord Derivation

Every clinical write re-derives the whole `PatientRecord`, and derivation cost
grows with the rows the person already holds. On a bulk loaded patient that is
12-32s per row level write.

`?skip_refresh=true` suppresses it, on the bulk POST and on the row level
`PATCH` and `DELETE` of the five clinical endpoints. Opt-in: without it every
verb still derives, because existing callers depend on that.

A client that defers must derive afterwards:

```
POST /api/v1/patient-records/{person_id}/refresh/
  202 {"person_id": 123, "task_id": "..."}

GET /api/v1/derivation-status/{task_id}/
  200 {"task_id": "...", "state": "PENDING|STARTED|SUCCESS|FAILURE", "error": null}
```

Both are admin or service-token only.

`202`, not `200`: the derivation is queued on Celery and the record is not
rebuilt yet when the POST returns. Poll the status endpoint for the outcome.
`state` is Celery's own, and a derivation that fails reports `FAILURE` with the
error rather than a success over a stale record — the signal path in
`omop_core/signals.py` swallows failures, this does not.

Result state lives in the Redis result backend and expires after
`CELERY_RESULT_EXPIRES` (default 24h). An id past that, or one that was never
issued, reads as `PENDING`, which is also what Celery reports for an id it has
never seen.

### Running the derivation

`omop_core/services/derivation_jobs.py` picks how:

| `CELERY_BROKER_URL` | Dispatcher | Behaviour |
|---|---|---|
| set | `CeleryDispatcher` | enqueues on transaction commit, worker derives |
| empty | `InlineDispatcher` | derives in the request, before the `202` |

There is deliberately no separate setting for the choice: a dispatcher that can
disagree with the broker leaves every job queued with nothing consuming it.
Inline is what a developer machine with no Redis gets — the wire contract is
the same, so a client needs one path either way. Inline lets a failing
derivation propagate instead of recording it, because the caller is still
holding the request, so an id is only ever issued for a derivation that
already succeeded. That is why the id itself (`inline-<signed uuid>`) is the
completion record: a registry would be process-local, and under several
gunicorn workers the poll would land on a process that never saw the POST. It
is signed with SECRET_KEY so only an id the deployment issued reads SUCCESS,
and it verifies on any worker.
Because it holds the request, it caps the derivation with a 25s
`statement_timeout` — the queued path gets that bound from
`CELERY_TASK_TIME_LIMIT` instead.

Tests swap in `FakeDispatcher` through `use_dispatcher(...)`; nothing needs to
patch Celery or run it eager.

Run a worker with:

```bash
celery -A promop worker --loglevel=info
```

The task is idempotent — derivation clears and rebuilds every field — so a
duplicate call is extra load, not a correctness problem. That is why nothing
deduplicates, and why `CELERY_TASK_ACKS_LATE` is safe.

`CELERY_BROKER_VISIBILITY_TIMEOUT` (default 1800) must stay above
`CELERY_TASK_TIME_LIMIT` (default 900) or Redis decides a still-running task
was lost and hands the same job to a second worker.

Only `refresh/` is async. The signal path and the bulk write still derive
inline.

---

## Bulk OMOP Row Writes

The five OMOP clinical CRUD endpoints (`conditions`, `drug-exposures`, `measurements`,
`observations`, `procedures`) accept a **JSON array** on POST for callers that have
already parsed FHIR into OMOP rows. Single-dict POSTs are unchanged.

Contract: `201` with `{"created": N, "updated": M, "unattributed_matches": K, "ids": [...]}`, one id per
request row in request order. One transaction, all-or-nothing. One batch is one
person (mixed-person → 400). Server assigns PKs via `next_pk_batch`
(client-supplied PKs → 400). Per-index validation errors. Max 1,000 rows
(`OMOP_BULK_MAX_ROWS`) → 413. Provenance comes from the `X-Provenance-Source` /
`X-Provenance-User-Id` headers and applies to the whole batch; no source means no
`ProvenanceRecord`, matching the single-row path.

**The write is idempotent by default** (issue #454). Rows are upserted on the
event identity in `_UPSERT_KEYS`, the same identity
`fhir/sync.py::_upsert_clinical` uses — so an ETL re-run, a retry after a read
timeout, or a re-parse converges instead of duplicating. Semantics per key:
event already present → left in place, its concept updated when it changed and
any stacked historical duplicates collapsed onto the first real row (null
confidence and a mapped concept), or onto the earliest when no candidate holds
an approved mapping; otherwise inserted. Repeats within one batch collapse to a
single row (last occurrence
wins). A content match rewrites only the concept column — every other column is
left as stored, so a re-run cannot quietly overwrite a corrected value — unless
the incoming row carries a source record key: the key is then stamped on the
unkeyed row it matched and the columns the serializer accepts are rewritten, as
below. Inserted rows and key matches get a `ProvenanceRecord` when a source is
sent. `?upsert=false` restores append-only writes for unkeyed rows; a row
carrying a source record key is still matched by key.

The FHIR sync (`_upsert_clinical` and the daily rollup) applies the same
survivor rule. It promotes a provisional row it rewrites only when the sync
resolves a concept and the caller is not patient self-service, and marks the
patient stale for the next derivation (#2581).

| Model | Event identity |
|---|---|
| ConditionOccurrence, DrugExposure, ProcedureOccurrence | `(source_value, date)` |
| Measurement | `(source_value, date, datetime, value_as_number)` |
| Observation | `(source_value, date, datetime, value_as_number, value_as_string, value_source_value)` |

Measurement and Observation diverge on purpose: a patient legitimately has
several distinct results for one analyte on one day, so `(source_value, date)`
alone would delete real results instead of deduping a re-run.

The concept column stays outside every key, which lets a vocabulary load upgrade
a stored row in place instead of stranding a duplicate beside it. The same rule
picks which value columns may join a key: only raw ones. `value_as_concept` is
re-resolved by a vocabulary load just like `observation_concept`, so keying on
it would strand the duplicate the design prevents. `value_source_value` is the
raw text behind that resolution, so it separates two coded answers safely.

A batch that trips a database constraint returns **409** with the driver's first
error line, not 500. The whole batch rolled back, so the caller can retry, and a
500 reads as "the service is down". A value the database refuses after
validation (`DataError`, e.g. `numeric field overflow`) returns **400** with
that first line, on the single-row, bulk create and bulk update writes: the
same request fails the same way, so it is not a retry (#2101).

A row with no `source_value` or no date has no identity and is always inserted.

### Source record key (`source_record_key`, #2453)

The content identity breaks when the producer's code choice changes: a curator
approves a mapping, the ETL picks another code for the same reading, and the
next import no longer matches. A measurement or observation row may therefore
carry the producer's own id for the record (`{issuer}:{facility}/{resourceType}/{id}`),
unique per person and table (a partial unique index, unkeyed rows exempt).
Printable ASCII, no whitespace, at most 512; anything else is a 400.

- **Matched by key first**, entered-in-error rows included. The stored row is
  rewritten from the incoming one: every column the serializer accepts except
  `person`, `is_erroneous` and `erroneous_reason`, so `value_source_value` and
  the unit columns move with the value (a stale one fails the #2249 read check).
  Rewritten measurements are unit-normalized like inserted ones.
- **No key match: the content match, against unkeyed rows only.** Exactly one
  unkeyed row for one incoming key gets the key stamped and is rewritten. Several
  candidates of different attribution, or several incoming keys on one row, is
  ambiguous: nothing is stamped, the last of those rows maps to the stored row as
  an unkeyed row would, the others are inserted, and their request indices come
  back in `key_ambiguous` (always present for these two tables, usually `[]`).
- Keyed rows never merge with each other by content, and a content collapse
  never deletes a keyed row. A key repeated within one batch is one row, last
  occurrence winning.
- `?upsert=false` turns off only the content match. The single-row POST matches
  by key the same way; `bulk_update` may set or change a key (a collision is 409).
- **Concurrent requests with one key converge to one row.** The plan locks the
  keyed rows it found; the INSERT is `ON CONFLICT DO NOTHING`, and a key another
  request inserted in between is re-read and applied as a key match. Any other
  conflict hidden by it (an id taken, the blocking row deleted before the
  re-read) is a 409, safe to retry.
- **A replay overwrites manual edits.** A key match rewrites the row from the
  source, so a curator's `PATCH` to a keyed row lasts only until the producer
  sends that record again, and a column the producer never sends is cleared. Fix
  a keyed row at the source, or mark it entered in error (that flag is kept).
- With `X-Provenance-Organization-ID`, a key held by another hospital's row is a
  409. A key match records provenance when a source is sent, naming the
  hospital only on a row already attributed to it.
- A row relocated between the two tables keeps its key, unless the patient's
  destination table already holds it; the copy then goes without it.

### Source hospital (`X-Provenance-Organization-ID`, #1690)

One ETL credential reads several hospitals, and a hospital-scoped mapping only
repoints rows whose provenance names that hospital. The five clinical endpoints
on `/api/v1/` take the hospital as a header; the legacy `/api/` registration
ignores it. Validation is shared with `code-mappings/lookup/`'s body
`organization_id` (`patient_portal/api/provenance_org.py`).

- **Validated before any write.** 400 for a malformed, empty, unknown or
  inactive id, or for a header without a source (`X-Provenance-Source`, or
  `source` in a single-dict body). 403 when it differs from an org-bound
  credential's organization, or when the caller is neither a machine credential
  nor staff.
- **Attribution, not access.** It is stored on `ProvenanceRecord.organization`.
  Authorization follows the credential, and `PatientRecord.organization` is
  never changed.
- **Hospital-aware matching.** With the header, existing rows are classed by
  the non-null organizations on their provenance
  (`omop_core/services/provenance_attribution.py`):

  | Class | Outcome |
  |---|---|
  | own (exactly this hospital) | matched as above |
  | unattributed | left as stored; each input position counts in `unattributed_matches` |
  | foreign (another hospital, or two) | never touched; the event is inserted when only these hold the key |

  A replay does not add attribution to historical rows. Without the header,
  matching is unchanged, so ETL sends the header on every write.
- **Duplicates collapse only within one attribution**, everywhere rows are
  collapsed: this upsert, `fhir/sync.py`, and `repoint_clinical_rows`. Two
  hospitals' rows for one event are two facts.
- **A row's hospital is set once.** `_record_provenance` never replaces a stored
  organization, and an update that names a second hospital gets 409. The check
  runs with the clinical row locked, before it is read.
- **Scoped repoint needs exclusive attribution**: a row naming two hospitals
  stays where it is.

Three things to know before touching this code:

- **`bulk_create` does not fire `post_save`**, so the `omop_core/signals.py` receivers
  that rebuild `PatientRecord` do not run. The bulk path calls `refresh_patient_record`
  explicitly, once per batch. Any new bulk write path must do the same, or it will land
  rows that the read model never reflects.
- **Query count must stay flat in batch size.** DRF resolves FKs with a per-row
  `queryset.get(pk=...)`; `_prefetch_bulk_related` collapses that to one query per FK
  column. The upsert match is one superset `SELECT` for the whole batch, not the
  per-key query `_upsert_clinical` can afford on a small FHIR compartment.
  `BulkOmopWriteTest` and `BulkOmopUpsertTest` assert this with
  `CaptureQueriesContext` at two batch sizes — treat a failure there as a real
  regression, not a flaky threshold.
- **The collapse path deletes rows, and `post_delete` receivers are live.** The
  whole write block runs inside `suppress_patient_record_refresh()`; without it a
  batch that collapses duplicates fires one refresh per deleted row.

---

## Bulk OMOP Row Deletes

The five clinical endpoints delete in batches, on `/api/v1/` only:

```
POST /api/v1/measurements/bulk_delete/     {"ids": [10605646, 10605649, ...]}
  200 {"deleted": 2, "missing": []}
```

Ids come from a prior read, so there is no matching logic server-side. One
transaction, all-or-nothing. One batch is one person (mixed-person → 400).
Max 5,000 ids (`OMOP_BULK_DELETE_MAX_IDS`) → 413. `?skip_refresh=true` defers
the derivation, admin-gated as on the row level `DELETE`.

**Ids that name no row come back in `missing` rather than failing the batch.**
A retry after a read timeout, or a re-run of an applied batch, has to converge.
Rows the caller cannot reach are reported the same way, so the endpoint cannot
be used to tell an absent id from somebody else's.

`POST`, not `DELETE`, because a `DELETE` with a body gets stripped by
intermediaries. That makes the permission class matter: the viewsets use
`PatientCrudPermission`, which grants a session patient `POST` but denies them
`DELETE`, so the action overrides it with `EtlDeletePermission`: the base
`ScopedTokenPermission`, which evaluated on a `POST` reproduces the `DELETE`
rule, plus the `system/etl.delete` service scope that the row level `DELETE`
also accepts (#1947). `system/etl.write` reaches neither. Per-person write
access is `_authorize_person_write`, shared with the bulk create path.

Three things that are easy to get wrong:

- **Scoping is `_visible_clinical_rows`, not `get_queryset()`.** That queryset
  carries the tenancy filter *and* hides `is_erroneous` rows. Only the first
  half is wanted here, since a row flagged in error is one a reconciliation
  most wants to drop.
- **The lookup is locked inside the transaction.** Without `select_for_update`
  a concurrent row level `PATCH` could move a row to another person between the
  authorization and the delete.
- **`queryset.delete()` fires `post_delete`**, unlike `bulk_create`. The delete
  runs inside `suppress_patient_record_refresh()` with one explicit
  `refresh_patient_record` after it. Without the suppression a 40-row batch
  costs 40 derivations.

`ProvenanceRecord` and `MeasurementOwnership` rows for the deleted ids go too.
Neither has a database cascade: provenance points through a `GenericForeignKey`,
and ownership holds a bare `measurement_id`. Ids are never reused
(`next_pk` only advances its sequence), so the risk is orphan rows rather than
mis-attribution. `EpisodeEvent` is deliberately left alone: its `event_id` is
ambiguous without the field concept, and deleting by id alone would take out
links belonging to another domain.

---

## Bulk OMOP Row Updates

Updates are ~3% of the migration's volume, so this is the smallest of the three
batch entrances. A correction pass still paid one request per row:

```
PATCH /api/v1/measurements/bulk_update/   [{"measurement_id": 106, "value_as_number": 7.5}, ...]
  200 {"updated": 1, "missing": []}
```

Partial per row, through the same serializer and the same `partial=True` as the
row level `PATCH`. One transaction, one person, one refresh, ids reported in
`missing`, `409` on a constraint, per-index validation errors. Max 1,000 rows
(`OMOP_BULK_MAX_ROWS`) → 413. Same locking and same scoping as the delete path.

Rules specific to updates:

- **`person` is rejected in the payload.** Moving a row to another person breaks
  both the batch's single authorization and its single refresh. Use the row
  level `PATCH` for that.
- **Duplicate ids are rejected, not last-wins.** Two partial patches of one row
  in one batch have no defined order.
- **Rows carrying only an id are not counted and get no provenance.** They wrote
  nothing.
- **Errors are re-indexed onto the request rows.** Validation runs over matched
  rows only, so an id that matched nothing would otherwise shift every error
  after it onto the wrong row.

Permissions are the viewset's own, because `PATCH` is open to session patients
at the row level. `_authorize_person_write` on the batch's person is the guard,
and `_visible_clinical_rows` builds the reachable set from the same access
grants the row level path honours: self, verified representative, professional
through `GroupAccess`.

Writes are grouped by patched column set, one `bulk_update` per distinct shape.
A single call takes one column list for every instance, so an ungrouped batch
would write a column back onto rows whose payload never carried it, clobbering
a concurrent change.

**Each row is validated by its own serializer, bound to its instance.** That is
what keeps the batch honest about partial writes: a serializer with no instance
cannot tell a column the caller omitted from one it set to null. The FK caches
are resolved once for the batch (`_bulk_fk_caches`) and shared across the row
serializers, so the query count stays flat in batch size.

### The partial-write trap behind it

`MeasurementSerializer.validate` and `ObservationSerializer.validate` assigned
`value_as_number` and `value_as_string` unconditionally, reading them out of
`attrs` where a partial update had never put them. So a `PATCH` of an unrelated
field wrote `None` over the row value. That was live on the row level `PATCH`,
and `bulk_update` would have amplified it to a thousand rows a request.

Both serializers now decide by what the write carries:

| The write carries | Value columns |
|---|---|
| nothing, source is not an assertion code | untouched |
| nothing, source becomes an assertion code | stored values coerced, both written |
| one value column, source is not an assertion code | only that column written |
| either value column, source is an assertion code | both written, coerced |

An explicitly supplied value is never mixed with the stored sibling.
`coerce_assertion_value` gives `value_as_string` precedence, so feeding the
stored string back in would let it beat a patch that sets the number, and
setting an assertion from 1 to 0 would silently stay true.

---

## Therapy-Line Import

The OMOP row endpoints never write an `Episode`, so an ETL writes lines of
therapy separately, once per person (#1969):

```
POST /api/v1/therapy-lines/bulk_upsert/
  {"person": 16813, "lines": [{"line_number": 3, "start_date": "2021-07-15",
                               "ongoing": true, "regimen": "Dara-CyPd",
                               "outcome": "stringent complete response"}]}
  200 {"person_id": 16813, "created": 1, "updated": 0,
       "lines": [{"line_number": 3, "episode_id": ..., "created": true,
                  "regimen_concept_id": ..., "regimen_match": "unmatched"}]}
```

Code: `patient_portal/api/therapy_line_import.py`, which calls
`episode_service.author_therapy_lines`.

- **Permission.** `EtlWritePermission` plus `_authorize_person_write`, as on the
  bulk row writes. The editor's `POST /v1/therapy-lines/` refuses
  `system/etl.write` on purpose: it is a patient's write.
- **Upsert per line** on (person, cancer, `line_number`) through the editor's own
  writer. Omitted fields are left as stored. `end_date: null`, `ongoing: true`
  and `outcome: null` clear. Lines the request does not name are untouched, and
  so is any composition (drug exposures) a line already has. Assertions are
  recorded with `source='import'`.
- **Regimen labels are never guessed.** A label resolves through the curated
  `TherapyRegimen` code/title, then an exact HemOnc regimen name (not a synonym:
  "R" is lenalidomide in myeloma and rituximab in lymphoma), else it is
  quarantined under HK-Regimen with a `RegimenMappingGap` (`therapy-lines-api`).
  The label is always the line's name. `regimen_concept_id` overrides the label,
  and must be a live HemOnc or HK-Regimen regimen (400 otherwise).
- **Outcomes** match the disease's catalog case-insensitively and are stored in
  its spelling. An unknown outcome is a 400.
- **One transaction, one derivation.** A refused line fails the batch (400,
  naming the line). `?skip_refresh=true` defers the derivation, as on the bulk
  row writes.

---

## FHIR Bundle Generator Architecture

`omop_core/management/commands/generate_fhir_bundle.py`:

- Each clinical concept is a separate method (e.g., `_generate_labs`, `_generate_biomarkers`, `_generate_therapy_lines`)
- Add a new method for the new field's FHIR resource, or extend an existing method
- Call the new method in `_generate_patient_bundle()` and append results to `entries`
- Use realistic random ranges appropriate for the clinical domain (e.g., normal vs. pathological ranges)

Run generator:
```bash
.venv/bin/python manage.py generate_fhir_bundle --count 100 --output data/breast_cancer_fhir_bundle.json
```

---

## Repository conventions

### Environment conventions

- Staging always means Render: https://promop-staging.onrender.com.
- The staging web service is `promop-staging`; its worker is `promop-staging-worker`.
- Do not use the old Google Cloud / Cloud Run staging deployment to investigate or verify staging unless the user explicitly asks for it.
- Render production is `promop` with worker `promop-worker`. The unused legacy Render web service has been retired; do not include it in active readiness work.
- Cloud Run staging remains managed by `.github/workflows/deploy-staging.yml` and `Dockerfile.gcp`. Preserve its existing service, image, bucket and integration identifiers.
- `STAGING_DATABASE_URL` identifies Render staging; verify it matches the active Render web and worker configuration before database operations.
- See `docs/render-staging-celery.md` for Render staging configuration and verification.

### Pull request reviews

- There is no team review process. Do not request team reviewers (including `healthkey`) or configure required team-review rules.
- Request individual reviewers as directed by the user. Lars Burgess is `@larsburgess` on GitHub.
- Merging to `dev` does not require review approval. Individual review requests are optional; required CI checks must still pass.

### Pull request labels

- When an issue has a security-related label, apply the same label to its pull request.

#### Documentation-only changes

- Documentation-only changes do not require application CI or local application
  test suites before push or merge. Review the content and check links and
  `git diff --check` instead.
- This is a standing user preference: do not run pytest, Django, frontend or
  async e2e suites for documentation-only changes, and do not ask the user to
  approve skipping them again.
- For an authorized documentation-only merge, run any lightweight docs-only
  status gates GitHub requires and complete the merge without asking for another
  confirmation. These status gates do not require application test results.
- Do not use `[skip ci]` for a protected-branch PR when it would leave required
  statuses missing and block the merge. Let the existing docs-only detector
  skip the application jobs and satisfy the required status gates instead.
- CI runs a lightweight file-change detector and skips backend, frontend,
  security, and async e2e jobs when every changed path is documentation. Keep
  the named suite jobs so GitHub records them as skipped. The required
  `Application CI` gate passes documentation-only PRs without requiring frontend
  or backend test results; code changes must pass the applicable application
  suites. `Security gates` remains a separate required check.
- Root Markdown/reStructuredText/AsciiDoc, documentation under `docs/`, and
  Markdown documentation under `.github/` qualify. Code, tests, configuration,
  dependency files, and runtime data do not qualify, even when changed alongside
  docs. The three generated crossmap Markdown artifacts under `docs/` are runtime
  data and continue to require CI.
- Unrecognized paths, an unavailable diff, or detector failure retain normal CI.
  GitHub-managed CodeQL scanning is separate from the application CI workflow.

#### Backend CI scope

- Frontend-only changes (including recognized frontend assets/configuration and
  accompanying documentation) skip the backend suites. Backend, dependency,
  runtime-data, shared configuration, mixed, and unknown changes run them.
- Django and pytest run in isolated parallel jobs. The required `Backend tests`
  check succeeds only when both pass; detector failures retain full coverage.
- CI is the gate for the full suites. Before a push, run only the tests the
  change touches (plus `npm run lint` and `npm run build` for frontend work);
  do not run the full Django or pytest suite locally. Wait for the required
  checks before merging. See [Targeted Tests Before a Push, CI for the Full
  Suites](#rule-targeted-tests-before-a-push-ci-for-the-full-suites) above.

#### Migration conflicts

Two branches that each add an `omop_core` migration off the same base are each
green on their own, but once both merge, `dev` has two leaf migrations. Django
then refuses to migrate, the conflict check and the migration-graph tests fail,
and the staging deploy is skipped until someone repairs it. This has broken
`dev` repeatedly (e4ee0a1a, 97d3767a, 5dca221f, #1833, 82e12773, #1871). A PR's
CI result describes the base it ran against, not `dev` at the moment you merge,
so a stale green check does not catch it.

- **Rebase before creating a migration.** `git fetch origin dev && git rebase
  origin/dev`, then run `makemigrations`, so the migration gets the next number
  and depends on the current leaf.
- **Rebase again immediately before merging a PR that adds a migration.** If
  `omop_core/migrations/` on `dev` changed since the branch's last CI run,
  rebase, check the graph, push, and wait for CI to pass on the new head. Do
  not merge on the older green result.
- **Check the graph locally after every rebase.** With `DATABASE_URL` pointed at
  local PostgreSQL and `DEBUG=True`, run
  `.venv/bin/python manage.py makemigrations --check --dry-run`. It fails with
  "Conflicting migrations detected" when there are two leaves.
- **Regenerate an unmerged migration rather than merge around it.** If your
  migration is not on `dev` and has not been applied to staging, delete it,
  rebase, and run `makemigrations` again so it sits on the current leaf.
  Hand-edit its `dependencies` only if it contains data migration code that
  `makemigrations` cannot regenerate.
- **Never renumber or rename a migration that is on `dev` or applied to
  staging.** Django keys on the full name, so a renamed migration looks like it
  never ran and would be applied a second time (see e4ee0a1a). Rejoin the leaves
  with a merge migration instead.
- **When `dev` already has two leaves**, fix it at once on a branch: add one
  empty merge migration whose `dependencies` list both leaves, with a docstring
  naming the PRs that forked the graph (as in
  `0295_merge_source_code_upload_and_facility_review`). Check that nobody has
  already opened the same fix, because two identical merge migrations create a
  new pair of leaves (#1833).
- **Merge migration PRs one at a time.** When several open PRs add
  migrations, merge one, then rebase the next onto the new `dev`, check it, and
  let CI pass before merging it.

#### Documentation naming and ownership

- Use `_architecture.md` for implemented architecture and `_plan.md` for future
  work, delivery status, dependencies and acceptance gates.
- Keep field/value inventories in generated reference artifacts. Architecture
  documents explain behavior; plans link to inventory evidence instead of
  duplicating per-field tables or deployment receipts.
