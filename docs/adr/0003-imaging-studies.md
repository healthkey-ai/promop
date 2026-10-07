# ADR 0003 — Imaging studies in OMOP with the Medical Imaging CDM extension

**Status:** Accepted 2026-10-07.
**Issue:** [#1731](https://github.com/healthkey-ai/promop/issues/1731).
**Deciders:** PRomop maintainers, with the Personal Health Record (PHR) team.

## Context

The PHR's Imaging section lists each study a patient has had: its type
(modality), name, date, body part, contrast, performing facility, the
radiologist's impression **verbatim**, the findings as reported, notes, whether
the source has images, an optional link to those images, and the report it came
from. Every value carries its facility and source date.

PRomop had no structured imaging. A radiology `DiagnosticReport` became one
`observation` row with its conclusion cut to 60 characters. `ImagingStudy` was
skipped as unsupported, and uploaded reports were `PatientDocument` files with
a title.

The options considered were:

1. **OMOP plus the OHDSI Medical Imaging CDM (MI-CDM) extension:**
   `procedure_occurrence` for the study, `image_occurrence` for its images, and
   `note`/`note_nlp` for the report.
2. **Plain OMOP 5.4:** `procedure_occurrence`, `note` and `note_nlp`, with
   modality, body site, contrast and the image link as `observation` rows.
3. **A PRomop-local `ImagingStudy` model.**

## Decision

Option 1. Imaging stays in OMOP, where analytics, exports and trial matching
already look. The one new table comes from the OHDSI imaging extension rather
than from PRomop.

| PHR field | Stored as |
|---|---|
| The study | `procedure_occurrence` (concept from the report or study code; `procedure_source_value` keeps the code) |
| Type / modality | `image_occurrence.modality_concept_id` (DICOM vocabulary when loaded) and `modality_source_value` (DICOM codes, e.g. `PT,CT`) |
| Name | the report `note.note_title`, else the procedure concept or source value |
| Date | `procedure_date` = `image_occurrence_date` |
| Body part | `image_occurrence.anatomic_site_concept_id` (SNOMED) and `anatomic_site_source_value` |
| Contrast | `observation` linked to the procedure (`observation_event_id`, field concept 1147082), `observation_source_value='imaging-contrast'`: `value_as_string` is `with`, `without` or `unknown`, and `value_source_value` is what to show (`Gadolinium`, `FDG tracer`, …) |
| Facility | the `visit_occurrence` at a `care_site` named after `DiagnosticReport.performer`, as the PHR reads provenance for every other row |
| Impression | `note` with `note_source_value='rad-impression'`, `note_text` = `DiagnosticReport.conclusion` **byte-for-byte** |
| Notes | `note` with `note_source_value='rad-report'`: `ImagingStudy.note`, else the report's text `presentedForm`, else its narrative |
| Findings | `note_nlp` on the report note, one row per finding as reported (`DiagnosticReport.result` observations, else `conclusionCode`), `offset` = order |
| Has image | the study names a DICOM Study Instance UID or an image link |
| Image link | `image_occurrence.wadors_uri` from `ImagingStudy.endpoint` → `Endpoint.address` |
| Source document | `PatientDocument` (`doc_type=IMAGING`) with `procedure_occurrence_id` set, from the report's `presentedForm.url` |

Notes are linked to the study with the OMOP 5.4 `note_event_id` and
`note_event_field_concept_id` columns, which this change adds to `note`.

### Deviations from MI-CDM, and why

- **One `image_occurrence` per study, not per series.** The PHR shows studies,
  and most clinical feeds report at study level. `image_series_uid` is set only
  when the study has exactly one series.
- **`image_study_uid` is nullable.** A radiology report can arrive without an
  `ImagingStudy`. The study is still recorded, and `has_image` is false.
- **Two local `*_source_value` columns** (`modality_source_value`,
  `anatomic_site_source_value`) keep the source's terms when the DICOM or
  SNOMED vocabularies aren't loaded. This follows OMOP's own source-value
  convention.
- **`image_feature` is not used yet.** Findings are reported text, not
  algorithm or coded features, so they belong in `note_nlp`. `image_feature`
  is the place for coded or AI-derived features later.

### Images

V1 shows the report only. PRomop stores the source's image link
(`wadors_uri`) when the import provides one, and never fetches, copies or
proxies images. A later release can show the images through that link.

### Import

Both FHIR paths, the upload endpoint and the provider sync, call
`omop_core.services.imaging.import_imaging`:
- Each radiology `DiagnosticReport` becomes a study with the `ImagingStudy`
  resources it names. A report is radiology when it has category `RAD` (or
  another HL7 v2-0074 imaging code, or LOINC `LP29684-5`) or names an
  `ImagingStudy`.
- An `ImagingStudy` that no report names becomes a study without a report.
- A study is keyed like other clinical events, by `(procedure_source_value,
  procedure_date)`. Re-importing it rewrites its imaging rows in place.
- An existing procedure row with the same key, from a FHIR `Procedure`, is
  adopted rather than duplicated.
- The radiology report also still lands in `observation`, as before, so
  existing consumers of that row are unchanged.

## Consequences

- The PHR (`GET /api/v1/phr/imaging/`) reads `imaging_studies(person)`. Imaging
  procedures are shown in Imaging, not in Procedures.
- Contrast is read from the study's own words: codes such as "CT CHEST W
  CONTRAST", series descriptions, and agents such as gadolinium or FDG. It is
  `unknown` when they say nothing.
- Copying a patient between instances (`patient_transfer`) carries the
  `image_occurrence`, the report notes (`note_event_id`, resolved through its
  field concept 1147082, like the other event links) and the document's
  `procedure_occurrence_id`. Where the CDM field concept isn't loaded, the
  notes are written without it and the copy can't re-link them.
- Unmapped study codes keep concept 0 with their source value. They are not
  minted under an HK vocabulary, because the study's identity is its report,
  not a coded fact used for matching.
