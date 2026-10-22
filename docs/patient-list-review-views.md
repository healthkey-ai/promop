# Patient list review views

The patient list provides Doctor, Foundation, and Analyst views. Each keeps its
own clinical filters and sort choice in browser storage, scoped to the signed-in
account. These are display preferences, not access roles. Existing organization
and patient permissions still determine which records can be listed.

All views retain disease, stage, genomics, therapy count, and record update time.
Disease cells also show recorded histology/myeloma subtype and receptor results.

- Doctor adds the latest recorded treatment line, disease status, ECOG, key data
  gaps, and latest clinical result date.
- Foundation adds disease status, location, contact-information availability,
  key data gaps, and latest clinical result date. Contact information being
  recorded does not assert permission to contact the patient.
- Analyst adds disease status, ECOG, organization, location, key data gaps, and
  latest clinical result date.

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
