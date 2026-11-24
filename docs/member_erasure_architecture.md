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
{"erasure": "account", "person_id": 1234, "identity_id": 567}
{"erasure": "record", "person_id": 1234, "purpose": "my_record"}
```

## After a restore from a backup

A database restored from a backup has every member who was erased since the
backup was taken, and has lost the log rows that said so (#2852). Two
commands close that gap (`patient_portal/erasure_replay.py`):

```bash
# On a schedule, to somewhere outside the database:
python manage.py export_erasures --output erasures-$(date +%F).jsonl

# After a restore, before the application is opened again:
python manage.py replay_erasures erasures.jsonl --since 2026-11-20T03:00:00Z --dry-run
python manage.py replay_erasures erasures.jsonl --since 2026-11-20T03:00:00Z
```

- **The export** is the deletion log as JSON lines: each `ERASE` row with
  what its signature covers, and the signature. Ids and who asked, nothing of
  what was held. `--since` limits it. Nothing in the project schedules it,
  and an erasure made after the last export is in no file: export as often
  as the gap you can accept. It does not write over an existing file without
  `--overwrite`, and writes a new one whole or not at all: after a restore
  the database's log is shorter than the last export, and a scheduled run
  must not replace the file that is about to be replayed. **Stop the export
  job before restoring.**
- **Every line must verify** under this deployment's `AUDIT_HMAC_KEY` (or
  `SECRET_KEY`, where it stands in), or the file is refused whole and
  nothing is replayed: a file cannot be written to erase somebody. After a
  key rotation, run the replay with the key the entries were written under.
- **An entry the restored database already holds is left alone.** It was
  applied before the backup, and what the member entered afterwards is
  theirs.
- **`--since` is required**: the time the backup was taken, with its
  offset. It keeps the replay off old entries the audit retention
  (`prune_audit_events`) has removed from the database, which would
  otherwise look lost. Entries older than it are counted in the output, with
  the newest one's time, and not looked at. When unsure, give an earlier
  time: an entry the database holds is never replayed, so early costs
  nothing and late leaves erasures undone.
- **A closed account is closed again** with `close_account`: the person,
  the record, every row about them, and the sign-in when the entry names it.
- **A withdrawal is put back** as well as its erasure: when the restored
  permission reads granted, a `withdrawn` answer is recorded at the entry's
  own time (source `system`), which ends the grant and what depends on it
  and erases the record; otherwise `erase_record` runs alone. When the
  withdrawal cannot be recorded (the purpose has no published text, or the
  member has no address for the ledger) the record is erased all the same
  and the output names the person: their permission still reads granted
  and staff must record the withdrawal. An entry naming a permission that
  is not defined here is skipped.
- **Never against a newer row.** A restore sets the id sequences back, so
  an erased person's id can be given to a new sign-up. A person whose
  sign-in link was made after the entry is skipped and listed. A person with
  no sign-in (loaded by an ETL, deleted by staff) cannot be told from a
  reused id and is skipped unless `--include-unlinked` says nothing has been
  written since the restore. A member who has answered the permission since
  the entry keeps their record. The sign-in an entry names goes only while
  it is still that person's.
- **Safe to run twice.** Each replayed entry leaves a row in the deletion
  log (`{"erasure": "replay", "replay_of": "<signature>"}`) beside the row
  the erasure itself writes; a second run finds it and does nothing.
- One transaction per entry. An entry that fails is rolled back and listed
  with the kind of error, the rest go on, and the command exits 1; run it
  again once the cause is fixed. The output names person ids only.

What a restore loses besides (permission answers and ledger entries written
after the backup, other than the withdrawals above) is not rebuilt here.

## Not here

- **Backups themselves.** A backup keeps a deleted member until it expires;
  how long is the deployment's backup retention.
- **Add-on apps** with their own tables keyed by a bare person id remove their
  rows themselves, on `post_delete` of `Person`.
- **Ledger purge** six years after the account closes.
