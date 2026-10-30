# Medication role and category

PHR D08 and D18 store medication purpose narratives on individual OMOP
`DrugExposure` events. A drug concept identifies the medicine, not the reason
that a particular patient received a particular prescription or administration.

## Writing attributes

Interactive edits follow [PatientRecord-first writes](patient-record-first-writes.md).
Use `PATCH /api/v1/patient-records/{person_id}/` with the existing patient access
and write authorization. The `medication_contexts` object is keyed by exposure ID:

```json
{"medication_contexts": {"123": {"role": "Induction", "category": "Myeloma treatment"}}}
```

Create/import the drug exposure first. Both attributes are optional strings up to
1,000 characters. Omitted exposures/attributes stay unchanged; null or blank clears
an attribute. Unknown attributes, foreign exposures, and changes to retired
exposures are rejected before saving. Unchanged retired entries echoed by an editor
are accepted. The existing GET detail envelope contains the saved map under
`patient_info.medication_contexts`; PATCH returns the updated record directly.

PatientRecord saves first. The backend then projects only changed attributes to
OMOP, with signals suppressed. The response never requires full derivation or an
OMOP snapshot. If vocabulary, mappings, or projection are unavailable, the saved
edit remains pending and the PHR displays it immediately. Structured pending
protection remains until a later import/explicit refresh confirms the values.
Mapping approval retries pending projection without triggering immediate derive.
Retired exposures are discarded from the current map during later derivation.

The existing PatientRecord revision history records edits. Immediate patient
projection writes Patient self-report facts and PATIENT_SELF provenance; other
authors retain the mapping's type. Mapping backfill uses the approved recipe when
no request author is present. No separate interactive OMOP context action is added.

Each change appends an Observation snapshot. Its `observation_event_id` is the
exposure ID and `obs_event_field_concept` is a valid CDM Metadata concept for
`drug_exposure.drug_exposure_id`. Patient ID and event namespace must both match;
an equal numeric event ID from another table is insufficient. Entered-in-error
facts and exposures are excluded. Latest observation date, datetime, and ID
choose the current snapshot; blank values prevent old values resurfacing.
Long strings and literal note-reference syntax use owned Note storage through
`clinical_text`, without truncation or cross-patient dereferencing.

The mapping migration repairs the old system-generated targets and retains
explicit curator approvals. It persists vocabulary/code selectors when Athena
is not loaded; the writer requires a live standard Observation concept before
writing. Existing patient facts are not rewritten and their stable source keys
remain readable after the correction.

| Field | Target | Source identity |
| --- | --- | --- |
| `drug_exposure.role` | LOINC 104803-2, Therapeutic intent [Interpretation] Narrative | `phr:drug-exposure-role` |
| `drug_exposure.category` | LOINC 104747-1, Medication purpose Narrative | `phr:drug-exposure-category` |

Both targets are standard Observation concepts in the installed Athena release.
LOINC defines [104803-2](https://loinc.org/104803-2) as the reason for prescribing
a medication or performing another action, and [104747-1](https://loinc.org/104747-1)
as the basis for a medication prescription. Here the role describes a medicine's
place in treatment (Induction, Conditioning, Maintenance); category describes its
purpose (Myeloma treatment, Supportive, Supplement). These are narrative values,
not inferred coded diagnoses or standardized phase classifications. Neither
field changes the separate intent of the whole therapy line.

## Reading in the PHR

The medication list and detail summary expose the latest exposure's
`drug_exposure_id`, `role`, and `category`. No value is borrowed from an older
exposure when the latest has none. Every detail `history` entry carries its own
exposure ID and recorded attributes.

Therapy-line medications retain the existing name/date grouping and add an
`exposures` array. Each entry includes `drug_exposure_id`, `start`, `end`, recorded
attributes, and the exposure's existing `source`. A medication-level role or
category appears only when every exposure in that group agrees on a nonblank
value. If roles differ, render the individual entries; do not silently select
one label for the whole group. Only typed DrugExposure EpisodeEvent links are
used to select medications or classify them as part of therapy.

Attributes belong to exposures, not to drug concepts or episode numbers. The
therapy authoring service creates separate exposures for the same medicine on
different lines. If an importer deliberately links one exposure to several
lines, those lines share that exposure's attributes. To assert different roles,
import distinct exposures and link each to its own line.

Shared-record reads use the existing selection of medication groups and therapy
lines; attributes travel with only the selected records. These changes provide
the PRomop backend contract; the HealthTree frontend renders and edits it.
