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

## While the record is off

Erasing once is not enough: a member who has withdrawn could still send health
data. `patient_portal.consent.record_permitted(person)` is false while the
member's latest answer to a marked purpose is anything but a grant (they
withdrew it, or said no and never agreed) until they give it, and true where
no purpose is marked or the member never answered one. While it is
false, nothing written on the member's behalf is kept:

| Path | While the record is off |
|---|---|
| PROlog account questionnaires (`PROLOG_PARTICIPANT_RESOLVER`, `prolog_participant_id`) | the member resolves to no participant, so the runner refuses to start or continue one (403) |
| Questionnaire record mappings (`survey_record_mapping.apply_response`) | not written, logged at info (a response submitted just before the withdrawal) |
| `PATCH /api/v1/patient-records/me/`, and any patch the member makes (`apply_patient_record_patch`) | 403 for a real change, except their name, which is their account's; an auto-save echoing what was shown is no change |
| PHR writes (`PhrView`: medications, procedures, statements, notes, explanations, shares) | POST, PUT and PATCH 403; DELETE allowed, since removing data never goes against a withdrawal. The consent views stay open (`writes_record = False`), so the member can give the permission again |
| The member's own sync (`/api/fhir/patient-sync/`) and wearable upload | 403 |

Staff and EHR writes are not gated: their basis is the care relationship,
not the member's permission. A deployment that relies on the member's
permission for those too decides it in its own connector.

## The deletion log

Each erasure writes its own row to the audit trail (`audit_event`, signed and
hash-chained), in the erasure's transaction, so it is logged whoever calls it:
a view, staff in the shell, or a command. `method` is `ERASE`, `user_id` is who
asked (`system` when nobody did), `resource_id` the person, and `detail` names
them and nothing of what was held:

```json
{"erasure": "account", "person_id": 1234, "identity_id": 567, "subject": "9f2c..."}
{"erasure": "record", "person_id": 1234, "purpose": "my_record"}
```

`subject` is there only when the sign-in that was closed is a partner's
(Firebase, the PHR host): a keyed digest of its issuer and subject
(`erasure.subject_hash`, under `AUDIT_HMAC_KEY`), never the subject itself.

## A closed account's partner token

A partner's token is issued by the partner and stays valid after the account
it opened is closed, until it expires; and `PartnerAuthentication` opens an
account for any valid token whose subject it does not know. Left alone, the
next request from a tab or an app that was still open would give the member
who just closed their account a new, empty one (#2851).

So a partner sign-in that matches no account is looked up in the deletion log
first (`erasure.closed_recently`): when its account was closed within
`ACCOUNT_CLOSED_BLOCK_SECONDS` (a day by default; `0` turns the check off) the
request is a 401 "This account was closed." and nothing is created. After
that the member can sign up again, as a new member.

- Only a closed sign-in is refused. A withdrawal keeps the account, and an
  admin delete that keeps the sign-in closes none, so both leave the token
  working. An admin delete keeps the sign-in only of an account that is
  staff or holds provider access (a clinician who is also a member); a
  `patient` grant, which an invitation leaves on the sign-in, does not keep
  it.
- A sign-in this deployment issued (`urn:local`) needs no check: its subject
  is a random id that goes with the row.
- The check costs one query, and only for a token whose subject has no
  account: a request from a known member is unchanged.
- Changing `AUDIT_HMAC_KEY` (or `SECRET_KEY`, where it stands in) changes
  the digest, so accounts closed under the old key are no longer refused.

## Not here

- **Backups.** Database backups keep a deleted member until they expire; how
  long is the deployment's backup retention.
- **Add-on apps** with their own tables keyed by a bare person id remove their
  rows themselves, on `post_delete` of `Person`.
- **Ledger purge** six years after the account closes.
