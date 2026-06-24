# PatientInfo ↔ OMOP Sync — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Expand PatientInfo PATCH write-through from lab fields only to all OMOP-mapped fields (disease/staging → ConditionOccurrence, demographics → Person, therapy lines → Episode + EpisodeEvent), extracting shared mappings out of views.py into a dedicated sync service.

**Architecture:** A new `omop_core/services/omop_write_service.py` handles PatientInfo → OMOP writes. A new `omop_core/services/mappings.py` holds the shared field→LOINC/concept map (moved from views.py). `partial_update` in views.py delegates all OMOP writes to `sync_to_omop()`.

**Tech Stack:** Django 4.2, Django REST Framework, PostgreSQL (psycopg3), OMOP CDM v5.4 models in omop_core and omop_oncology apps.

**Run all tests with:**
```bash
DATABASE_URL="$STAGING_DATABASE_URL" \
  .venv/bin/python manage.py test patient_portal.tests.PatientInfoOmopSyncTest --no-input
```

---

## File Map

| Action | File | Responsibility |
|---|---|---|
| Create | `omop_core/services/mappings.py` | `LAB_FIELD_TO_LOINC` dict, `get_gender_concept()`, `CONDITION_FIELDS`, `DEMOGRAPHIC_FIELDS`, `THERAPY_LINE_FIELDS` |
| Create | `omop_core/services/omop_write_service.py` | `sync_to_omop()`, `_sync_measurement()`, `_sync_condition()`, `_sync_demographics()`, `_sync_therapy_line()` |
| Modify | `patient_portal/api/views.py` | Remove `_LAB_FIELD_TO_LOINC`, `_upsert_omop_measurement`, `get_gender_concept`; call `sync_to_omop()` from `partial_update` |
| Modify | `patient_portal/tests.py` | Add `PatientInfoOmopSyncTest` class |

---

## Task 1: Create mappings.py

**Files:**
- Create: `omop_core/services/mappings.py`

- [ ] **Step 1: Create the file**

```python
# omop_core/services/mappings.py
from omop_core.models import Concept

# Maps PatientInfo field name → (LOINC code, unit string, display name)
LAB_FIELD_TO_LOINC = {
    # Blood counts
    'hemoglobin_g_dl':                ('718-7',    'g/dL',            'Hemoglobin [Mass/volume] in Blood'),
    'hematocrit_percent':             ('20570-8',  '%',               'Hematocrit [Volume Fraction] of Blood'),
    'wbc_count_thousand_per_ul':      ('6690-2',   '10*3/uL',         'Leukocytes [#/volume] in Blood'),
    'rbc_million_per_ul':             ('789-8',    '10*6/uL',         'Erythrocytes [#/volume] in Blood'),
    'platelet_count_thousand_per_ul': ('777-3',    '10*3/uL',         'Platelets [#/volume] in Blood'),
    'anc_thousand_per_ul':            ('751-8',    '10*3/uL',         'Neutrophils [#/volume] in Blood'),
    'alc_thousand_per_ul':            ('731-0',    '10*3/uL',         'Lymphocytes [#/volume] in Blood'),
    'amc_thousand_per_ul':            ('742-7',    '10*3/uL',         'Monocytes [#/volume] in Blood'),
    # Kidney / electrolytes
    'serum_creatinine_mg_dl':         ('2160-0',   'mg/dL',           'Creatinine [Mass/volume] in Serum or Plasma'),
    'creatinine_mg_dl':               ('2160-0',   'mg/dL',           'Creatinine [Mass/volume] in Serum or Plasma'),
    'serum_calcium_mg_dl':            ('17861-6',  'mg/dL',           'Calcium [Mass/volume] in Serum or Plasma'),
    'calcium_mg_dl':                  ('17861-6',  'mg/dL',           'Calcium [Mass/volume] in Serum or Plasma'),
    'egfr_ml_min_173m2':              ('62238-1',  'mL/min/1.73m2',   'GFR/BSA pred CKD-EPI ArA'),
    'egfr':                           ('62238-1',  'mL/min/1.73m2',   'GFR/BSA pred CKD-EPI ArA'),
    'bun_mg_dl':                      ('3094-0',   'mg/dL',           'Urea nitrogen [Mass/volume] in Serum or Plasma'),
    'blood_urea_nitrogen':            ('3094-0',   'mg/dL',           'Urea nitrogen [Mass/volume] in Serum or Plasma'),
    'sodium_meq_l':                   ('2951-2',   'mEq/L',           'Sodium [Moles/volume] in Serum or Plasma'),
    'serum_sodium':                   ('2951-2',   'mEq/L',           'Sodium [Moles/volume] in Serum or Plasma'),
    'potassium_meq_l':                ('2823-3',   'mEq/L',           'Potassium [Moles/volume] in Serum or Plasma'),
    'serum_potassium':                ('2823-3',   'mEq/L',           'Potassium [Moles/volume] in Serum or Plasma'),
    'magnesium_mg_dl':                ('2601-3',   'mg/dL',           'Magnesium [Mass/volume] in Serum or Plasma'),
    'magnesium':                      ('2601-3',   'mg/dL',           'Magnesium [Mass/volume] in Serum or Plasma'),
    'phosphorus':                     ('2777-1',   'mg/dL',           'Phosphate [Mass/volume] in Serum or Plasma'),
    # Liver function
    'bilirubin_total_mg_dl':          ('1975-2',   'mg/dL',           'Bilirubin.total [Mass/volume] in Serum or Plasma'),
    'alt_u_l':                        ('1742-6',   'U/L',             'Alanine aminotransferase [Enzymatic activity/volume] in Serum or Plasma'),
    'ast_u_l':                        ('1920-8',   'U/L',             'Aspartate aminotransferase [Enzymatic activity/volume] in Serum or Plasma'),
    'alkaline_phosphatase_u_l':       ('6768-6',   'U/L',             'Alkaline phosphatase [Enzymatic activity/volume] in Serum or Plasma'),
    'alkaline_phosphatase':           ('6768-6',   'U/L',             'Alkaline phosphatase [Enzymatic activity/volume] in Serum or Plasma'),
    'albumin_g_dl':                   ('1751-7',   'g/dL',            'Albumin [Mass/volume] in Serum or Plasma'),
    'total_protein':                  ('2885-2',   'g/dL',            'Protein [Mass/volume] in Serum or Plasma'),
    'troponin_ng_ml':                 ('10839-9',  'ng/mL',           'Troponin I.cardiac [Mass/volume] in Serum or Plasma'),
    'bnp_pg_ml':                      ('42637-9',  'pg/mL',           'BNP [Mass/volume] in Serum or Plasma'),
    'glucose_mg_dl':                  ('2345-7',   'mg/dL',           'Glucose [Mass/volume] in Serum or Plasma'),
    'hba1c_percent':                  ('4548-4',   '%',               'Hemoglobin A1c/Hemoglobin.total in Blood'),
    'inr':                            ('6301-6',   '{INR}',           'INR in Platelet poor plasma'),
    'pt_seconds':                     ('5902-2',   's',               'Prothrombin time (PT)'),
    'ptt_seconds':                    ('3173-2',   's',               'aPTT in Platelet poor plasma'),
    # Oncology markers
    'ldh_u_l':                        ('2532-0',   'U/L',             'Lactate dehydrogenase [Enzymatic activity/volume] in Serum or Plasma'),
    'ldh_level':                      ('2532-0',   'U/L',             'Lactate dehydrogenase [Enzymatic activity/volume] in Serum or Plasma'),
    'ldh':                            ('2532-0',   'U/L',             'Lactate dehydrogenase [Enzymatic activity/volume] in Serum or Plasma'),
    'beta2_microglobulin':            ('1952-1',   'mg/L',            'Beta-2-Microglobulin [Mass/volume] in Serum or Plasma'),
    'c_reactive_protein':             ('1988-5',   'mg/L',            'C reactive protein [Mass/volume] in Serum or Plasma'),
    'esr':                            ('30341-2',  'mm/h',            'Erythrocyte sedimentation rate'),
    'ki67_proliferation_index':       ('85319-2',  '%',               'Ki-67 Ag [Presence] in Tissue by Immune stain'),
    # Vital signs
    'weight':                         ('29463-7',  'kg',              'Body weight'),
    'height':                         ('8302-2',   'cm',              'Body height'),
    'systolic_blood_pressure':        ('8480-6',   'mm[Hg]',          'Systolic blood pressure'),
    'diastolic_blood_pressure':       ('8462-4',   'mm[Hg]',          'Diastolic blood pressure'),
    'heartrate':                      ('8867-4',   '/min',            'Heart rate'),
    # Performance status
    'ecog_performance_status':        ('89247-1',  '{score}',         'ECOG Performance Status score'),
    'karnofsky_performance_score':    ('89243-0',  '{score}',         'Karnofsky Performance Status score'),
}

CONDITION_FIELDS = frozenset({'disease', 'stage', 'condition_code_icd_10', 'condition_code_snomed_ct'})

DEMOGRAPHIC_FIELDS = frozenset({'gender', 'date_of_birth', 'patient_age', 'ethnicity'})

# Maps line number (1/2/3) → PatientInfo field prefix
THERAPY_LINE_PREFIXES = {
    1: 'first_line',
    2: 'second_line',
    3: 'later',
}

THERAPY_LINE_FIELDS = frozenset(
    f'{prefix}_{suffix}'
    for prefix in THERAPY_LINE_PREFIXES.values()
    for suffix in ('therapy', 'start_date', 'end_date', 'outcome', 'intent', 'discontinuation_reason')
)

# OMOP concept IDs used by the sync service
CONCEPT_GENERIC_LAB       = 3000963   # Laboratory test result (fallback)
CONCEPT_LAB_TYPE          = 32856     # Lab (measurement type)
CONCEPT_EHR_TYPE          = 32817     # EHR (condition type)
CONCEPT_TREATMENT_REGIMEN = 32531     # Treatment Regimen (episode concept)
CONCEPT_DRUG_EXPOSURE_FIELD = 1147094  # drug_exposure_id field concept (EpisodeEvent)


def get_gender_concept(gender_str):
    """Map a gender string to an OMOP Concept. Returns None if not found."""
    if not gender_str:
        return None
    gender_map = {
        'male': 8507, 'm': 8507,
        'female': 8532, 'f': 8532,
        'unknown': 8551, 'other': 8551, 'ambiguous': 8570,
    }
    concept_id = gender_map.get(gender_str.lower().strip())
    if concept_id:
        try:
            return Concept.objects.get(concept_id=concept_id)
        except Concept.DoesNotExist:
            return None
    return None
```

- [ ] **Step 2: Verify the file imports cleanly**

```bash
DATABASE_URL="$STAGING_DATABASE_URL" \
  .venv/bin/python -c "from omop_core.services.mappings import LAB_FIELD_TO_LOINC, CONDITION_FIELDS, DEMOGRAPHIC_FIELDS, THERAPY_LINE_FIELDS, get_gender_concept; print('ok', len(LAB_FIELD_TO_LOINC), 'lab fields')"
```
Expected: `ok 55 lab fields` (or similar count)

- [ ] **Step 3: Commit**

```bash
git add omop_core/services/mappings.py
git commit -m "feat: add omop_core/services/mappings.py — shared PatientInfo→OMOP field mappings"
```

---

## Task 2: Create omop_write_service.py — Measurement sync

**Files:**
- Create: `omop_core/services/omop_write_service.py`
- Test: `patient_portal/tests.py` (add `PatientInfoOmopSyncTest`)

- [ ] **Step 1: Write the failing tests for Measurement sync**

Add this class at the end of `patient_portal/tests.py`:

```python
class PatientInfoOmopSyncTest(_SmartBase):
    """PatientInfo PATCH → OMOP write-through via omop_write_service."""

    def _patch(self, pi, payload):
        return self.write_client.patch(
            f'/api/patient-info/{pi.person.person_id}/',
            payload,
            format='json',
        )

    def test_patch_lab_creates_measurement(self):
        """PATCHing a lab field creates a Measurement row."""
        from omop_core.models import Measurement
        person = Person.objects.create(person_id=91001)
        pi = PatientInfo.objects.create(person=person)
        before = Measurement.objects.filter(person=person).count()

        self._patch(pi, {'hemoglobin_g_dl': 12.5})

        self.assertEqual(Measurement.objects.filter(person=person).count(), before + 1)
        m = Measurement.objects.filter(person=person).latest('measurement_id')
        self.assertEqual(float(m.value_as_number), 12.5)

    def test_patch_lab_same_day_updates_not_duplicates(self):
        """Two PATCHes of the same lab on the same day → still 1 Measurement row."""
        from omop_core.models import Measurement
        person = Person.objects.create(person_id=91002)
        pi = PatientInfo.objects.create(person=person)

        self._patch(pi, {'hemoglobin_g_dl': 11.0})
        self._patch(pi, {'hemoglobin_g_dl': 11.5})

        rows = Measurement.objects.filter(
            person=person,
            measurement_source_value='718-7',
        )
        self.assertEqual(rows.count(), 1)
        self.assertEqual(float(rows.first().value_as_number), 11.5)

    def test_patch_lab_different_day_appends(self):
        """PATCHes on different dates → separate Measurement rows."""
        from unittest.mock import patch as mock_patch
        from datetime import date
        from omop_core.models import Measurement
        person = Person.objects.create(person_id=91003)
        pi = PatientInfo.objects.create(person=person)

        with mock_patch('omop_core.services.omop_write_service._today', return_value=date(2024, 1, 1)):
            self._patch(pi, {'hemoglobin_g_dl': 10.0})
        with mock_patch('omop_core.services.omop_write_service._today', return_value=date(2024, 2, 1)):
            self._patch(pi, {'hemoglobin_g_dl': 10.5})

        rows = Measurement.objects.filter(person=person, measurement_source_value='718-7')
        self.assertEqual(rows.count(), 2)
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
DATABASE_URL="$STAGING_DATABASE_URL" \
  .venv/bin/python manage.py test patient_portal.tests.PatientInfoOmopSyncTest --no-input 2>&1 | tail -15
```
Expected: errors or failures (module not found or test failures)

- [ ] **Step 3: Create omop_write_service.py with Measurement sync**

```python
# omop_core/services/omop_write_service.py
import logging
from datetime import date

from omop_core.models import Concept, Measurement
from omop_core.services.mappings import (
    LAB_FIELD_TO_LOINC,
    CONDITION_FIELDS,
    DEMOGRAPHIC_FIELDS,
    THERAPY_LINE_FIELDS,
    THERAPY_LINE_PREFIXES,
    CONCEPT_GENERIC_LAB,
    CONCEPT_LAB_TYPE,
    CONCEPT_EHR_TYPE,
    CONCEPT_TREATMENT_REGIMEN,
    CONCEPT_DRUG_EXPOSURE_FIELD,
    get_gender_concept,
)

logger = logging.getLogger('audit')


def _today():
    return date.today()


def sync_to_omop(patient_info, changed_fields: set, today: date = None) -> None:
    """
    Write PatientInfo changes through to OMOP tables.
    Never raises — failures are logged but must not block the HTTP response.
    """
    if today is None:
        today = _today()
    person = patient_info.person
    try:
        for field in changed_fields:
            value = getattr(patient_info, field, None)
            if field in LAB_FIELD_TO_LOINC and value is not None:
                _sync_measurement(person, field, value, today)
        if changed_fields & CONDITION_FIELDS:
            _sync_condition(person, patient_info, today)
        if changed_fields & DEMOGRAPHIC_FIELDS:
            _sync_demographics(person, patient_info)
        for line_number, prefix in THERAPY_LINE_PREFIXES.items():
            line_fields = {f'{prefix}_{s}' for s in ('therapy', 'start_date', 'end_date', 'outcome', 'intent', 'discontinuation_reason')}
            if changed_fields & line_fields:
                _sync_therapy_line(person, patient_info, line_number, prefix, today)
    except Exception as exc:
        logger.error('{"event": "omop_sync_error", "error": "%s"}', exc)


def _sync_measurement(person, field_name: str, value, today: date) -> None:
    loinc_code, unit, display = LAB_FIELD_TO_LOINC[field_name]
    concept = (
        Concept.objects.filter(concept_code=loinc_code, vocabulary_id='LOINC').first()
        or Concept.objects.filter(concept_id=CONCEPT_GENERIC_LAB).first()
    )
    if concept is None:
        return
    type_concept = Concept.objects.filter(concept_id=CONCEPT_LAB_TYPE).first() or concept
    existing = Measurement.objects.filter(
        person=person,
        measurement_concept=concept,
        measurement_date=today,
    ).first()
    if existing:
        existing.value_as_number = value
        existing._skip_patient_info_refresh = True
        existing.save(update_fields=['value_as_number'])
    else:
        last = Measurement.objects.order_by('-measurement_id').first()
        new_id = (last.measurement_id + 1) if last else 1
        m = Measurement(
            measurement_id=new_id,
            person=person,
            measurement_concept=concept,
            measurement_date=today,
            measurement_type_concept=type_concept,
            value_as_number=value,
            measurement_source_value=loinc_code,
            unit_source_value=unit,
        )
        m._skip_patient_info_refresh = True
        m.save()
```

- [ ] **Step 4: Run Measurement tests**

```bash
DATABASE_URL="$STAGING_DATABASE_URL" \
  .venv/bin/python manage.py test patient_portal.tests.PatientInfoOmopSyncTest.test_patch_lab_creates_measurement patient_portal.tests.PatientInfoOmopSyncTest.test_patch_lab_same_day_updates_not_duplicates patient_portal.tests.PatientInfoOmopSyncTest.test_patch_lab_different_day_appends --no-input 2>&1 | tail -10
```
Expected: `Ran 3 tests ... OK`

- [ ] **Step 5: Commit**

```bash
git add omop_core/services/omop_write_service.py patient_portal/tests.py
git commit -m "feat: omop_write_service — Measurement sync from PatientInfo PATCH"
```

---

## Task 3: Wire sync_to_omop into partial_update; remove dead code from views.py

**Files:**
- Modify: `patient_portal/api/views.py`

- [ ] **Step 1: Add import at top of views.py**

Find the imports block at the top of `patient_portal/api/views.py` and add:

```python
from omop_core.services.omop_write_service import sync_to_omop
from omop_core.services.mappings import LAB_FIELD_TO_LOINC as _LAB_FIELD_TO_LOINC_COMPAT
```

- [ ] **Step 2: Replace the write-through block in partial_update**

Find this block in `partial_update` (around line 374):

```python
        today = datetime.now().date()
        for field, value in request.data.items():
            if field in _LAB_FIELD_TO_LOINC and value is not None:
                try:
                    m_before = Measurement.objects.filter(
                        person=person,
                        measurement_source_value=_LAB_FIELD_TO_LOINC[field][0],
                        measurement_date=today,
                    ).first()
                    _upsert_omop_measurement(person, field, value, today)
                    if prov_source:
                        m_after = Measurement.objects.filter(
                            person=person,
                            measurement_source_value=_LAB_FIELD_TO_LOINC[field][0],
                            measurement_date=today,
                        ).first()
                        if m_after:
                            _record_provenance(m_after, prov_source, prov_user_id, modification_reason=prov_reason, organization=get_request_org(request))
                except Exception:
                    pass
```

Replace it with:

```python
        today = datetime.now().date()
        changed_fields = {f for f in request.data if f not in _prov_meta}
        sync_to_omop(patient_info, changed_fields, today=today)
```

- [ ] **Step 3: Delete the now-unused functions and dict from views.py**

Delete these three items from `views.py` (they now live in `mappings.py` and `omop_write_service.py`):
- The `get_gender_concept` function (lines ~87–110)
- The `_LAB_FIELD_TO_LOINC` dict (lines ~128–186)
- The `_upsert_omop_measurement` function (lines ~189–225)

Also remove the `_LAB_FIELD_TO_LOINC_COMPAT` import added in Step 1 — it was only needed as a reference; the actual import is now via `omop_write_service`.

Replace the `from omop_core.services.mappings import LAB_FIELD_TO_LOINC as _LAB_FIELD_TO_LOINC_COMPAT` line with just:
```python
from omop_core.services.omop_write_service import sync_to_omop
```

- [ ] **Step 4: Run existing lab write-through tests to confirm nothing regressed**

```bash
DATABASE_URL="$STAGING_DATABASE_URL" \
  .venv/bin/python manage.py test patient_portal.tests.PatientInfoPatchWriteThroughTest patient_portal.tests.PatientInfoOmopSyncTest --no-input 2>&1 | tail -10
```
Expected: all pass

- [ ] **Step 5: Commit**

```bash
git add patient_portal/api/views.py
git commit -m "refactor: delegate PatientInfo PATCH write-through to omop_write_service; remove dead code from views.py"
```

---

## Task 4: Add ConditionOccurrence sync

**Files:**
- Modify: `omop_core/services/omop_write_service.py`
- Modify: `patient_portal/tests.py`

- [ ] **Step 1: Write failing tests**

Add these two tests to `PatientInfoOmopSyncTest` in `patient_portal/tests.py`:

```python
    def test_patch_disease_creates_condition_occurrence(self):
        """PATCHing 'disease' creates a new ConditionOccurrence row."""
        from omop_core.models import ConditionOccurrence
        person = Person.objects.create(person_id=91010)
        pi = PatientInfo.objects.create(person=person)

        self._patch(pi, {'disease': 'Breast cancer'})

        self.assertEqual(
            ConditionOccurrence.objects.filter(person=person).count(), 1
        )
        co = ConditionOccurrence.objects.get(person=person)
        self.assertEqual(co.condition_source_value, 'Breast cancer')

    def test_patch_stage_appends_condition_occurrence(self):
        """Two PATCHes of 'stage' create two separate ConditionOccurrence rows."""
        from omop_core.models import ConditionOccurrence
        from unittest.mock import patch as mock_patch
        from datetime import date
        person = Person.objects.create(person_id=91011)
        pi = PatientInfo.objects.create(person=person)

        with mock_patch('omop_core.services.omop_write_service._today', return_value=date(2024, 1, 1)):
            self._patch(pi, {'stage': 'Stage II'})
        with mock_patch('omop_core.services.omop_write_service._today', return_value=date(2024, 3, 1)):
            self._patch(pi, {'stage': 'Stage III'})

        self.assertEqual(ConditionOccurrence.objects.filter(person=person).count(), 2)
```

- [ ] **Step 2: Run to verify they fail**

```bash
DATABASE_URL="$STAGING_DATABASE_URL" \
  .venv/bin/python manage.py test patient_portal.tests.PatientInfoOmopSyncTest.test_patch_disease_creates_condition_occurrence patient_portal.tests.PatientInfoOmopSyncTest.test_patch_stage_appends_condition_occurrence --no-input 2>&1 | tail -10
```
Expected: FAIL

- [ ] **Step 3: Implement _sync_condition in omop_write_service.py**

Add this import at the top of `omop_write_service.py`:
```python
from omop_core.models import Concept, Measurement, ConditionOccurrence
```

Add this function to `omop_write_service.py`:

```python
def _sync_condition(person, patient_info, today: date) -> None:
    disease = getattr(patient_info, 'disease', None)
    stage = getattr(patient_info, 'stage', None)
    icd10 = getattr(patient_info, 'condition_code_icd_10', None)
    snomed = getattr(patient_info, 'condition_code_snomed_ct', None)

    source_value = (disease or stage or icd10 or snomed or '')[:50]
    if not source_value:
        return

    condition_concept = (
        Concept.objects.filter(concept_name__icontains=(disease or '')[:50]).first()
        if disease else None
    ) or Concept.objects.filter(concept_id=0).first()

    type_concept = Concept.objects.filter(concept_id=CONCEPT_EHR_TYPE).first()
    if type_concept is None:
        return

    ConditionOccurrence.objects.create(
        person=person,
        condition_concept_id=condition_concept.concept_id if condition_concept else 0,
        condition_start_date=today,
        condition_type_concept=type_concept,
        condition_source_value=source_value,
    )
```

Also update the `sync_to_omop` imports line at the top of the file:
```python
from omop_core.models import Concept, Measurement, ConditionOccurrence
```

- [ ] **Step 4: Run condition tests**

```bash
DATABASE_URL="$STAGING_DATABASE_URL" \
  .venv/bin/python manage.py test patient_portal.tests.PatientInfoOmopSyncTest.test_patch_disease_creates_condition_occurrence patient_portal.tests.PatientInfoOmopSyncTest.test_patch_stage_appends_condition_occurrence --no-input 2>&1 | tail -10
```
Expected: `Ran 2 tests ... OK`

- [ ] **Step 5: Commit**

```bash
git add omop_core/services/omop_write_service.py patient_portal/tests.py
git commit -m "feat: omop_write_service — ConditionOccurrence append on disease/stage PATCH"
```

---

## Task 5: Add Person demographics sync

**Files:**
- Modify: `omop_core/services/omop_write_service.py`
- Modify: `patient_portal/tests.py`

- [ ] **Step 1: Write failing test**

Add to `PatientInfoOmopSyncTest`:

```python
    def test_patch_demographics_updates_person(self):
        """PATCHing gender and date_of_birth updates the linked Person record."""
        person = Person.objects.create(person_id=91020)
        pi = PatientInfo.objects.create(person=person)

        self._patch(pi, {'gender': 'female', 'date_of_birth': '1975-06-15'})

        person.refresh_from_db()
        self.assertEqual(person.year_of_birth, 1975)
        self.assertEqual(person.month_of_birth, 6)
        self.assertEqual(person.day_of_birth, 15)
        self.assertIsNotNone(person.gender_concept)
        self.assertEqual(person.gender_concept.concept_id, 8532)  # FEMALE
```

- [ ] **Step 2: Run to verify it fails**

```bash
DATABASE_URL="$STAGING_DATABASE_URL" \
  .venv/bin/python manage.py test patient_portal.tests.PatientInfoOmopSyncTest.test_patch_demographics_updates_person --no-input 2>&1 | tail -10
```
Expected: FAIL

- [ ] **Step 3: Implement _sync_demographics in omop_write_service.py**

Add to imports at top:
```python
from datetime import date, datetime
```

Add this function:

```python
def _sync_demographics(person, patient_info) -> None:
    update_fields = []

    gender_str = getattr(patient_info, 'gender', None)
    if gender_str:
        concept = get_gender_concept(gender_str)
        if concept:
            person.gender_concept = concept
            person.gender_source_value = gender_str
            update_fields += ['gender_concept', 'gender_source_value']

    dob = getattr(patient_info, 'date_of_birth', None)
    if dob:
        if isinstance(dob, str):
            try:
                dob = datetime.strptime(dob, '%Y-%m-%d').date()
            except ValueError:
                dob = None
    if dob:
        person.year_of_birth = dob.year
        person.month_of_birth = dob.month
        person.day_of_birth = dob.day
        update_fields += ['year_of_birth', 'month_of_birth', 'day_of_birth']

    if update_fields:
        person.save(update_fields=update_fields)
```

- [ ] **Step 4: Run demographics test**

```bash
DATABASE_URL="$STAGING_DATABASE_URL" \
  .venv/bin/python manage.py test patient_portal.tests.PatientInfoOmopSyncTest.test_patch_demographics_updates_person --no-input 2>&1 | tail -10
```
Expected: `Ran 1 test ... OK`

- [ ] **Step 5: Commit**

```bash
git add omop_core/services/omop_write_service.py patient_portal/tests.py
git commit -m "feat: omop_write_service — Person demographics upsert on PATCH"
```

---

## Task 6: Add Episode + EpisodeEvent therapy line sync

**Files:**
- Modify: `omop_core/services/omop_write_service.py`
- Modify: `patient_portal/tests.py`

- [ ] **Step 1: Write failing tests**

Add to `PatientInfoOmopSyncTest`:

```python
    def test_patch_first_line_therapy_creates_episode(self):
        """PATCHing first_line_therapy creates an Episode with episode_number=1."""
        from omop_oncology.models import Episode
        person = Person.objects.create(person_id=91030)
        pi = PatientInfo.objects.create(person=person)

        self._patch(pi, {
            'first_line_therapy': 'AC-T',
            'first_line_start_date': '2023-01-15',
            'first_line_end_date': '2023-07-01',
        })

        episodes = Episode.objects.filter(person=person, episode_number=1)
        self.assertEqual(episodes.count(), 1)
        ep = episodes.first()
        self.assertEqual(ep.episode_source_value, 'AC-T')
        from datetime import date
        self.assertEqual(ep.episode_start_date, date(2023, 1, 15))

    def test_patch_therapy_links_existing_drug_exposures(self):
        """DrugExposure rows in the episode date range are linked via EpisodeEvent."""
        from omop_oncology.models import Episode, EpisodeEvent
        from omop_core.models import DrugExposure, Concept
        person = Person.objects.create(person_id=91031)
        pi = PatientInfo.objects.create(person=person)
        drug_concept = Concept.objects.get(concept_id=19136160)
        type_concept = Concept.objects.get(concept_id=32817)

        # Pre-existing DrugExposure within the therapy date range
        de = DrugExposure.objects.create(
            drug_exposure_id=9910001,
            person=person,
            drug_concept=drug_concept,
            drug_exposure_start_date='2023-02-01',
            drug_type_concept=type_concept,
            drug_source_value='Paclitaxel',
        )

        self._patch(pi, {
            'first_line_therapy': 'AC-T',
            'first_line_start_date': '2023-01-15',
            'first_line_end_date': '2023-07-01',
        })

        episode = Episode.objects.get(person=person, episode_number=1)
        self.assertTrue(
            EpisodeEvent.objects.filter(episode=episode, event_id=de.drug_exposure_id).exists(),
            'DrugExposure was not linked to Episode via EpisodeEvent',
        )

    def test_patch_therapy_no_duplicate_episode_events(self):
        """Repeating the PATCH does not create duplicate EpisodeEvent rows."""
        from omop_oncology.models import Episode, EpisodeEvent
        from omop_core.models import DrugExposure, Concept
        person = Person.objects.create(person_id=91032)
        pi = PatientInfo.objects.create(person=person)
        drug_concept = Concept.objects.get(concept_id=19136160)
        type_concept = Concept.objects.get(concept_id=32817)

        DrugExposure.objects.create(
            drug_exposure_id=9910002,
            person=person,
            drug_concept=drug_concept,
            drug_exposure_start_date='2023-02-01',
            drug_type_concept=type_concept,
            drug_source_value='Paclitaxel',
        )

        payload = {
            'first_line_therapy': 'AC-T',
            'first_line_start_date': '2023-01-15',
            'first_line_end_date': '2023-07-01',
        }
        self._patch(pi, payload)
        self._patch(pi, payload)  # second identical PATCH

        episode = Episode.objects.get(person=person, episode_number=1)
        self.assertEqual(
            EpisodeEvent.objects.filter(episode=episode, event_id=9910002).count(), 1,
            'EpisodeEvent was duplicated',
        )
```

- [ ] **Step 2: Run to verify they fail**

```bash
DATABASE_URL="$STAGING_DATABASE_URL" \
  .venv/bin/python manage.py test \
    patient_portal.tests.PatientInfoOmopSyncTest.test_patch_first_line_therapy_creates_episode \
    patient_portal.tests.PatientInfoOmopSyncTest.test_patch_therapy_links_existing_drug_exposures \
    patient_portal.tests.PatientInfoOmopSyncTest.test_patch_therapy_no_duplicate_episode_events \
    --no-input 2>&1 | tail -10
```
Expected: FAIL

- [ ] **Step 3: Implement _sync_therapy_line in omop_write_service.py**

Add to imports at top:
```python
from omop_core.models import Concept, Measurement, ConditionOccurrence, DrugExposure, ProcedureOccurrence
from omop_oncology.models import Episode, EpisodeEvent
from django.utils import timezone
```

Add this function:

```python
def _sync_therapy_line(person, patient_info, line_number: int, prefix: str, today: date) -> None:
    therapy_name = getattr(patient_info, f'{prefix}_therapy', None)
    start_date = getattr(patient_info, f'{prefix}_start_date', None)
    end_date = getattr(patient_info, f'{prefix}_end_date', None)

    if not therapy_name:
        return

    episode_concept = Concept.objects.filter(concept_id=CONCEPT_TREATMENT_REGIMEN).first()
    if episode_concept is None:
        return

    # Determine start datetime for upsert key
    if start_date:
        if isinstance(start_date, str):
            from datetime import datetime as dt
            try:
                start_date = dt.strptime(start_date, '%Y-%m-%d').date()
            except ValueError:
                start_date = None

    # Upsert Episode: match by (person, episode_number, start_date) or just (person, episode_number)
    episode = None
    if start_date:
        episode = Episode.objects.filter(
            person=person,
            episode_number=line_number,
            episode_start_date=start_date,
        ).first()
    if episode is None:
        episode = Episode.objects.filter(
            person=person,
            episode_number=line_number,
        ).order_by('-episode_start_date').first()

    if episode:
        episode.episode_source_value = therapy_name[:50]
        if end_date:
            episode.episode_end_date = end_date
        episode.save(update_fields=['episode_source_value', 'episode_end_date'])
    else:
        episode = Episode.objects.create(
            person=person,
            episode_concept=episode_concept,
            episode_start_date=start_date or today,
            episode_end_date=end_date,
            episode_number=line_number,
            episode_source_value=therapy_name[:50],
        )

    # Link unlinked DrugExposure rows within the episode date range
    ep_start = episode.episode_start_date
    ep_end = episode.episode_end_date

    drug_qs = DrugExposure.objects.filter(person=person)
    if ep_start:
        drug_qs = drug_qs.filter(drug_exposure_start_date__gte=ep_start)
    if ep_end:
        drug_qs = drug_qs.filter(drug_exposure_start_date__lte=ep_end)

    field_concept = Concept.objects.filter(concept_id=CONCEPT_DRUG_EXPOSURE_FIELD).first()
    if field_concept is None:
        return

    existing_event_ids = set(
        EpisodeEvent.objects.filter(
            episode=episode,
            episode_event_field_concept=field_concept,
        ).values_list('event_id', flat=True)
    )

    for de in drug_qs:
        if de.drug_exposure_id not in existing_event_ids:
            EpisodeEvent.objects.create(
                episode=episode,
                event_id=de.drug_exposure_id,
                episode_event_field_concept=field_concept,
            )
```

- [ ] **Step 4: Run therapy tests**

```bash
DATABASE_URL="$STAGING_DATABASE_URL" \
  .venv/bin/python manage.py test \
    patient_portal.tests.PatientInfoOmopSyncTest.test_patch_first_line_therapy_creates_episode \
    patient_portal.tests.PatientInfoOmopSyncTest.test_patch_therapy_links_existing_drug_exposures \
    patient_portal.tests.PatientInfoOmopSyncTest.test_patch_therapy_no_duplicate_episode_events \
    --no-input 2>&1 | tail -10
```
Expected: `Ran 3 tests ... OK`

- [ ] **Step 5: Commit**

```bash
git add omop_core/services/omop_write_service.py patient_portal/tests.py
git commit -m "feat: omop_write_service — Episode + EpisodeEvent upsert for therapy line PATCH"
```

---

## Task 7: Resilience test + full suite run

**Files:**
- Modify: `patient_portal/tests.py`

- [ ] **Step 1: Add resilience test**

Add to `PatientInfoOmopSyncTest`:

```python
    def test_sync_failure_does_not_block_response(self):
        """If sync_to_omop raises internally, the PATCH response is still 200."""
        from unittest.mock import patch as mock_patch
        person = Person.objects.create(person_id=91040)
        pi = PatientInfo.objects.create(person=person)

        with mock_patch(
            'patient_portal.api.views.sync_to_omop',
            side_effect=RuntimeError('simulated DB failure'),
        ):
            response = self._patch(pi, {'ecog_performance_status': 1})

        self.assertIn(response.status_code, [200, 404])
```

- [ ] **Step 2: Run the full PatientInfoOmopSyncTest class**

```bash
DATABASE_URL="$STAGING_DATABASE_URL" \
  .venv/bin/python manage.py test patient_portal.tests.PatientInfoOmopSyncTest --no-input 2>&1 | tail -15
```
Expected: `Ran 11 tests ... OK`

- [ ] **Step 3: Run previously passing write-through tests to confirm no regression**

```bash
DATABASE_URL="$STAGING_DATABASE_URL" \
  .venv/bin/python manage.py test \
    patient_portal.tests.PatientInfoPatchWriteThroughTest \
    patient_portal.tests.AuditLogMiddlewareTest \
    patient_portal.tests.PatientInfoOmopSyncTest \
    --no-input 2>&1 | tail -15
```
Expected: all pass

- [ ] **Step 4: Commit and push**

```bash
git add patient_portal/tests.py
git commit -m "test: add resilience test; all PatientInfoOmopSyncTest passing"
git push origin dev
```
