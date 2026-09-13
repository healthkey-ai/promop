# Field-value reconciliation

Mapping approval does not authorize rewriting historical facts. The commands
below distinguish a derived PatientRecord refresh from recovering a missing
clinical Measurement. Run tests against a local database. Keep plans containing
clinical values outside the repository and shared logs.

## Review and refresh a bounded read-model scope

`backfill_patient_records` retains its existing version/organization filters and
count-only `--dry-run`. Its explicit plan operations select at most 100 existing
people, with one transaction per person:

```sh
python manage.py backfill_patient_records --person-id 123 --person-id 456 --plan /private/operator/refresh.json
python manage.py backfill_patient_records --apply-plan /private/operator/refresh.json
python manage.py backfill_patient_records --rollback-plan /private/operator/refresh.json
```

Use actual IDs from the reviewed scope. Creating a plan runs the normal refresh
inside transactions that are rolled back. This preview requires a writable
connection; it cannot run in a database read-only transaction. It commits no
PatientRecord changes and does not write or change OMOP clinical facts. Inspect
the plan's exact before/after changes before applying. All read-model fields
affected by the refresh are included; this is a person-scoped operation, not a
claim to update only one field.

Plan files are created exclusively with permission 0600. The command prints
record totals and field counts, never clinical values or patient identifiers.
Plans have an environment-bound HMAC and fingerprints for the service code,
field/choice/formula configuration, and vocabulary release metadata. Do not
transfer them between environments or edit their proposed changes. Generate a
new plan if code, curation, vocabulary releases, or the selected records change.
Application also re-derives and compares the proposed result, so source changes
that alter the result cause a rollback of that record.

Each successful record creates existing RecordRevision and signed AuditEvent
entries in the same transaction as the refresh. Recovery snapshots remain in
the protected audit trail. Repeating application skips completed records, so an
interrupted batch resumes without duplicating those changes. If a later record
conflicts, earlier successful records stay audited. Correct the conflict and
create a new reviewed plan, or roll back the completed portion of the old plan.

Rollback restores the exact stored read-model state, including timestamp
precision. It refuses to overwrite any record changed after application and
does not modify clinical facts. Rollback itself is audited and idempotent. A
rolled-back plan cannot be applied again. Recovery requires its original audit
entries to remain available and pass their integrity check; retain them under
the applicable audit retention policy.

Pending user edits and explicit clears retain the normal refresh protection.
These plan operations do not turn pending edits into historical OMOP facts.
They also cannot repair an erroneous source fact merely because a previous
recipe accepted it.

## Other recovery paths and remaining acceptance

`reconcile_patient_record_projection` retains its separate numeric-fact
inventory and narrowly bounded repair: one person, one field, and an explicitly
attested event date are required for application. It must not use today's date
as a substitute for the event date.

Pending-edit reprojection currently uses the existing mapping/save flow.
Dedicated reviewed plans for pending projection, legacy aliases, old answer
targets, and facts affected by erroneous breast-cancer recipes remain required
for #1231. A source fact with ambiguous clinical meaning needs source evidence;
neither a refresh nor a vocabulary name match establishes that evidence. No
staging reconciliation has been executed as part of this work.
