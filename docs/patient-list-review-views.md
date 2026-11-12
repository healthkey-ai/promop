# Patient list review columns

The patient list shows the same base columns to everyone who reaches it, and
decides the columns beside them from the reader's roles (#2463). There is no
view to choose: the Doctor, Foundation and Analyst presets of #1254 are gone,
and a saved preset's clinical filters and sort carry over as the reader's one
saved set. The column headings sort the list: a first click takes the column's natural
direction, a second reverses it. Clinical filters and the sort are kept in
browser storage, scoped to the signed-in account. Existing organization and patient permissions
still determine which records can be listed.

All readers get disease, stage, genomics, therapy count, record update time,
key data gaps and the latest clinical result date. Disease cells also show
recorded histology/myeloma subtype and receptor results.

- Everyone who reaches the list also gets the clinical context: the latest
  recorded treatment line with its dates, and ECOG with its assessment date.
- Staff and org admins, whose work spans an organization's patients, also get
  the outreach columns: location and whether contact information is recorded.
  Contact information being recorded does not assert permission to contact the
  patient. The location and contact filters appear with these columns.
- Organization is added once the list spans more than one organization.
- A reader with no curation access (an analyst or a patient-role grant browsing
  a public demo org, #2460) sees no Name column and no row link, and the
  server withholds every demographic column.

## Meaning of the summaries

The treatment summary selects the highest recorded line number from the stored
PatientRecord therapy fields, including structured later lines. Dates are shown
when recorded. An absent end date is not labeled as active treatment.

Disease status uses the recorded condition clinical status, then recorded
progression. It does not infer a new clinical assessment or attach another
field's date to the status. ECOG displays its own assessment date when available.

Key data gaps cover three fields only: stage, ECOG, and genomic data. They are
not a global completeness score or a recommendation to order tests. ECOG zero
and recorded negative genomic findings count as data. Missing data stays unknown.

Latest clinical result is the latest dated Measurement or Observation with a
recorded answer, excluding erroneous rows, clear markers, and future dates.
It is separate from `updated_at`, which can advance during an administrative
update or backfill. No result produces a null date, never today's date.

## Filtering and performance

Clinical filters cover recorded status, ECOG, key data gaps, result age, treatment
history, subtype/biomarker text, location, and contact-information availability.
Text searches apply on Enter or when the input loses focus. Filtering and sorting
happen on the server before pagination; unknown sort values are placed last.
The filtered bulk-delete action uses the same filters as the list.

Scalar summaries use stored PatientRecord values. Two indexed SQL subqueries
supply latest result dates; serialization does not issue per-patient history
queries or trigger derivation. Demographic opt-outs apply to names, ages, and
locations for other readers, including the new location filter.

Trial-candidate counts are a future addition once saved match results and their
calculation dates are available. No trial eligibility is inferred by these views.
