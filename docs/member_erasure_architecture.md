# Member erasure

Two things a member can ask for delete their data straight away:

| Asked for | What goes | What stays |
|---|---|---|
| **Closing the account** (`DELETE /api/v1/patient-records/me/`, `{"confirm": "DELETE"}`) | The person and everything that hangs off them: the record, every OMOP row, questionnaire responses and answers, documents, journal entries, the invitation, the permission history, and the sign-in | The consent ledger (email, permission history; [consent-v2.md](consent-v2.md)) and the audit trail |
| **Ending a grant of a purpose marked `erases_record_on_withdrawal`** (`POST /api/v1/phr/consent/` with `"withdrawn"`, or any other answer that replaces a standing `"granted"`) | The health answers: questionnaire responses and answers, every clinical row, journal entries and documents, and the record's derived fields | The account, the membership (the record's organisation), the sign-up facts on the person and their location (name, year of birth, sex, ethnicity, country, postcode district), the permission history, personal representatives and groups assigned by hand |

Both happen in one transaction with the request, so the member never sees the
one without the other. Staff deleting a patient
(`DELETE /api/v1/patient-records/<person_id>/admin-delete/`) takes the same path
as closing the account.

## How

`patient_portal/erasure.py`:

- `close_account(person, identity)` removes the rows the person's cascade does
  not reach (`delete_omop_clinical_rows`: questionnaire responses, which
  `PROTECT` the person; `EpisodeEvent` and `MeasurementOwnership`, which hold
  bare ids; groups and representatives), then the person, then the sign-in.
- `erase_record(person)` deletes every table that hangs off the person except
  `KEPT_ON_WITHDRAWAL` (account, permission and document history, invitation,
  language skills, merge candidates). New clinical tables are therefore erased
  by default, not missed. It then rebuilds the record from what is left,
  carrying over its organisation, and re-derives it once.

`record_consent` calls `erase_record` when an answer ends a standing grant of a
purpose with `erases_record_on_withdrawal`: `withdrawn`, but also `declined` or
`ask_later` after `granted`, from the member, staff or a migration.
Uploaded documents' stored files are deleted once the transaction commits;
a rolled-back erasure keeps them. The flag is data, set per purpose in
the deployment's consent file (`load_consent_texts`) or the admin; a purpose
without it deletes nothing when withdrawn.

## The deletion log

Each erasure writes its own row to the audit trail (`audit_event`, signed and
hash-chained), in the erasure's transaction, so it is logged whoever calls it:
a view, staff in the shell, or a command. `method` is `ERASE`, `user_id` is who
asked (`system` when nobody did), `resource_id` the person, and `detail` names
them and nothing of what was held:

```json
{"erasure": "account", "person_id": 1234, "identity_id": 567}
{"erasure": "record", "person_id": 1234, "purpose": "my_record"}
```

## Not here

- **Backups.** Database backups keep a deleted member until they expire; how
  long is the deployment's backup retention.
- **Add-on apps** with their own tables keyed by a bare person id remove their
  rows themselves, on `post_delete` of `Person`.
- **Ledger purge** six years after the account closes.
