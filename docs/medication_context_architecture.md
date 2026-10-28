# Medication role and category

PHR D08 and D18 store medication purpose narratives on individual OMOP
`DrugExposure` events. A drug concept identifies the medicine, not the reason
that a particular patient received a particular prescription or administration.

## Writing attributes

`GET /api/v1/drug-exposures/{drug_exposure_id}/context/` returns the currently
recorded `role` and `category`. `PATCH` accepts either or both fields as strings
(up to 1,000 characters), with the same patient access and write authorization
as the underlying clinical row. Unknown keys are rejected. Omitted fields stay
unchanged; null or blank explicitly clears a value. An empty result means no
attribute has been recorded. A returned empty string means it was cleared.

```json
{"role": "Induction", "category": "Myeloma treatment"}
```

Create/import the drug exposure first, then patch its context. Provenance headers
supported by the v1 clinical endpoints also apply here. The response is the full
current context for that exposure. The legacy clinical API has no context action.

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
