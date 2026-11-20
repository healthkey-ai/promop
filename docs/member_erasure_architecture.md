# Member erasure

Two things a member can ask for delete their data straight away:

| Asked for | What goes | What stays |
|---|---|---|
| **Closing the account** (`DELETE /api/v1/patient-records/me/`, `{"confirm": "DELETE"}`) | The person and everything that hangs off them: the record, every OMOP row, questionnaire responses and answers, documents, journal entries, the invitation, the permission history, and the sign-in | The consent ledger (email, permission history; [consent-v2.md](consent-v2.md)) and the audit trail |
| **Withdrawing a purpose marked `erases_record_on_withdrawal`** (`POST /api/v1/phr/consent/` with `"decision": "withdrawn"`) | The health answers: questionnaire responses and answers, every clinical row, journal entries and documents, and the record's derived fields | The account, the membership (the record's organisation), the sign-up facts on the person and their location (name, year of birth, sex, ethnicity, country, postcode district), the permission history, personal representatives and groups assigned by hand |

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

`record_consent` calls `erase_record` when the decision is `withdrawn` and the
purpose has `erases_record_on_withdrawal`. The flag is data, set per purpose in
the deployment's consent file (`load_consent_texts`) or the admin; a purpose
without it deletes nothing when withdrawn.

## The deletion log

Each erasure is a row in the audit trail (`audit_event`, hash-chained by
`AuditLogMiddleware`) whose `detail` names who and nothing else:

```json
{"erasure": "account", "person_id": 1234, "identity_id": 567}
{"erasure": "record", "person_id": 1234, "purpose": "my_record"}
```

After an account closes, the request's row has no user, so the detail is the
only place the row says who was deleted.

## Not here

- **Backups.** Database backups keep a deleted member until they expire; how
  long is the deployment's backup retention.
- **Add-on apps** with their own tables keyed by a bare person id remove their
  rows themselves, on `post_delete` of `Person`.
- **Ledger purge** six years after the account closes.
