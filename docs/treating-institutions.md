# Treating institutions

The General tab provides a searchable treating-institution dropdown. Search by
name, city, state name or postal abbreviation. Select a result, or enter a center
outside the directory and press Enter. Search text is not saved until selected;
Escape or leaving the field restores the saved value. Clear removes the value.
The existing patient edit permissions apply. Values use the existing
`facility_name` write path through PatientRecord and the Person extension field.

The checked-in snapshot in `omop_core/data/treating_institutions.json` comes from
the [NCI designated cancer-center directory](https://www.cancer.gov/research/infrastructure/cancer-centers/find),
retrieved September 14, 2026. It includes 68 clinical/comprehensive locations
(including separate Mayo campuses), excluding basic laboratory centers. This is
a US directory, not an exhaustive list of oncology providers or an assertion of
which treatments a center offers. Each entry retains its source page and location.
The application serves the snapshot without contacting NCI during patient edits.

To refresh, review the NCI directory's clinical and comprehensive entries, retain
separate listed locations and update the snapshot's retrieval date. Keep source
links and flag pediatric-only centers so adult demo assignments exclude them.

Preview missing sample values from the Render staging shell:

```bash
python manage.py backfill_sample_treating_institutions
```

Apply:

```bash
python manage.py backfill_sample_treating_institutions --confirm
```

Only the known synthetic cohorts are selected. Existing names are retained, an
existing value on either PatientRecord or Person fills the other when missing,
and conflicting values or explicitly edited fields are preserved. Otherwise a
deterministic adult-center assignment prefers the patient's state where listed.
These are synthetic demo assignments, not actual care relationships. Both stores
are updated in transactions without a reverse derivation. Rerunning fills only
missing values, so this command also supports future sample imports.
