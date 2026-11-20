# Member erasure

Two things a member can ask for delete their data straight away:

| Asked for | What goes | What stays |
|---|---|---|
| **Closing the account** (`DELETE /api/v1/patient-records/me/`, `{"confirm": "DELETE"}`) | The person and everything that hangs off them: the record, every OMOP row, questionnaire responses and answers, documents, journal entries, the invitation, the permission history, and the sign-in | The consent ledger (email, permission history; [consent-v2.md](consent-v2.md)) and the audit trail |
| **Ending a grant of a purpose marked `erases_record_on_withdrawal`** (`POST /api/v1/phr/consent/` with `"withdrawn"`, or any other answer that replaces a standing `"granted"`) | The health answers: questionnaire responses and answers, every clinical row, journal entries, documents and their files, messages with providers, and the record's derived fields | The account, the membership (the record's organisation), the person's name, year of birth (age checks when a permission is given again) and country, the permission history (current and legacy). Taken off the person: sex, race, ethnicity, birth day and month, and the location's address and postcode (unless another person shares it). Personal representatives and every group membership go too: they are access to a record that no longer exists |

Both happen in one transaction with the request, so the member never sees the
one without the other. Staff deleting a patient
(`DELETE /api/v1/patient-records/<person_id>/admin-delete/`) takes the same path
as closing the account.

## How

`patient_portal/erasure.py`:

- Both start from `omop_core/services/patient_cleanup.py`:
  `delete_unkeyed_rows` (questionnaire responses, which `PROTECT` the person;
  `EpisodeEvent` and `MeasurementOwnership`, which hold bare ids) and
  `delete_provenance` (`ProvenanceRecord` points at its row through a generic
  key that no cascade follows).
- `close_account(person, identity)` runs `delete_omop_clinical_rows` (the
  above, all the person's provenance, groups and representatives), then
  deletes the person, then the sign-in.
- `erase_record(person)` empties `withdrawal_tables()`: every table that hangs
  off the person, or off a kept one (a member's messages hang off their
  account), except `KEPT_ON_WITHDRAWAL` (account, permission and document
  history, legacy consents, invitation, language skills, merge candidates).
  New clinical tables are therefore erased by default, not missed, and
  `tests/test_member_erasure.py` pins the list so each new one is a decision.
  It then rebuilds the record from what is left, carrying over its
  organisation, and re-derives it once.

`record_consent` calls `erase_record` when an answer ends a standing grant of a
purpose with `erases_record_on_withdrawal`: `withdrawn`, but also `declined` or
`ask_later` after `granted`, from the member or staff. Only an answer that
becomes the member's position does it: one back-dated behind the grant ends
nothing, and a `migration` never erases (it replays history; nobody is
withdrawing now). The member's consent endpoints (`ConsentView`,
`ConsentDocumentsView`) take `MemberWritePermission`: a session or partner
sign-in may write, an OAuth2 or service token needs a write scope, so a token
that may only read cannot withdraw.
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
