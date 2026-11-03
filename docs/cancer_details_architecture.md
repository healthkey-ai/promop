# Per-cancer diagnosis details

PRomop stores stage, subtype and biomarker assertions in the existing OMOP
Measurement and Observation tables. A fact belongs to a diagnosis when its event
ID names a ConditionOccurrence and its event-field concept names
`condition_occurrence.condition_occurrence_id` in the CDM vocabulary. The person
on both records must match. Disease Episodes continue to own lines of therapy;
these condition links identify the clinical assertions behind diagnosis cards.

This implements PRomop's D04 support ([#1858](https://github.com/healthkey-ai/promop/issues/1858)).
HealthTree's PHR owns rendering and editing the cards.

## Authoring and imports

The v1 Measurement and Observation APIs accept optional `condition_occurrence`.
For example, a staging fact uses the existing approved stage concept:

```json
{
  "person": 123,
  "measurement_concept": 3022698,
  "measurement_type_concept": 32817,
  "measurement_date": "2026-10-26",
  "measurement_source_value": "21908-9",
  "value_as_string": "IIIA",
  "condition_occurrence": 456
}
```

POST to `/api/v1/measurements/` creates/upserts the fact. PATCH the returned fact
ID to correct its value or diagnosis link. For a component already linked to a
parent Measurement, associate the parent report with the diagnosis; the existing
report relationship is preserved and its components inherit the cancer scope. ETL batches can supply the same link
per row. The ordinary person/organization permissions still apply; the diagnosis
must be non-erroneous and belong to that person. A missing CDM field vocabulary
rejects the link rather than creating an untyped event ID. A link is corrected
by naming another diagnosis; an incorrect assertion can be retired through
`is_erroneous`. Null does not silently turn a linked result into an ambiguous
patient-wide fallback. Legacy API routes retain their existing serializers.

The concept ID in the example is the installed LOINC 21908-9 clinical stage
question. Clients use the deployment's approved FieldConceptMapping/concept
catalog, including the appropriate assay concept for an MRD result. This work
reuses those mappings and the existing CDM columns; it adds no schema or concept
migration and creates no synthetic vocabulary rows. The generated
[field-mapping inventory](field-mapping-inventory/README.md) is the catalog
reference. Stage systems and assay methods are not inferred from a result.

API upsert identity and same-day duplicate checks include the event relationship,
so identical question/date/value combinations for different cancers survive
independently. FHIR sync matching likewise cannot collapse linked facts into an
unlinked clinical event. Existing raw FHIR imports without an explicit resolvable
condition association retain their legacy behavior. An importer that knows the
association supplies the condition ID through the v1 API, including by PATCHing
an existing imported fact. This change does not guess ownership from proximity
in time or backfill relationships on old patient data.

## Reading and refresh

`GET /api/v1/phr/diagnoses/` groups cancer condition events as before and projects
explicitly linked stage, subtype and marker assertions for every cancer. The
latest dated assertion wins within that cancer, across both clinical tables;
components of a condition-linked Measurement report inherit its cancer through
the typed Measurement event relationship. Missing, foreign and cyclic report
links cannot supply diagnosis details;
entered-in-error rows are excluded. Source-keyed unmapped staging assertions are
also readable. Cytogenetic markers are a collection with individual dated clears.
Approved mappings are read on each request, including distinct source keys when
several fields share a concept.

The existing `subtype`, labelled `stage`, and `biomarkers` shapes are retained.
Linked values add their reporting source/date, and `detail_sources` identifies
the source of each projected detail. A cytogenetic collection identifies the
latest assertion affecting its state and includes `contributors` when its
remaining markers originate from multiple assertions. Shared records use the same projection and
then apply the existing selected-diagnosis filter. Reads batch-load clinical rows
and provenance rather than querying once per cancer.

An explicit `PatientRecord:cleared` assertion suppresses the older value for that
cancer. Clear/history identity also includes the typed event relationship.
An absent answer does not clear an earlier result.

PatientRecord remains the compatibility projection of the primary cancer. Its
existing column widths are respected; diagnosis cards retain the full canonical
OMOP value.
Refreshing it filters out another cancer's linked disease-specific facts,
while shared patient-wide lab values remain available. Explicit linked details
then override the primary's legacy fallback, including explicit clears. Existing
PatientRecord and cytogenetic editors project their disease-specific writes to
the primary diagnosis and cannot overwrite a secondary cancer's same-day fact.
Facts with no condition association remain primary-only legacy fallbacks and
never supply secondary cancer cards. All original assertions remain in OMOP.
