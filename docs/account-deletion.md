# Account deletion

How a member's account and data are removed, what is kept, and how a backup
restore is brought back into line. Companion to [consent model v2](consent-v2.md);
required by the FLF Patient Movement plan (WP5 "Deletion", WP12 "erasure path").

## What happens

Closing an account, or withdrawing the "my record" permission, deletes the
live rows at once, in one transaction, through one service:
`patient_portal.account_deletion.delete_member(person, reason=..., identity=...)`.

| Removed | How |
|---|---|
| PROlog survey responses, answers, invitations, consents; episode events; measurement ownership; group memberships; personal representatives | `omop_core.services.patient_cleanup.delete_omop_clinical_rows`, the rows no cascade reaches. The access rows are keyed by a bare `person_id` and would otherwise grant the member's old representatives and groups access to whoever gets the id next |
| Break-glass grants, and provenance records describing any deleted row or naming the person | explicitly: a provenance row points at its row through a generic key with no cascade, and its patient id is the source system's, not the person id |
| The `Person` and everything that cascades from it: the OMOP clinical rows, `PatientRecord`, `PatientUser`, `ConsentEvent` and `DocumentAcceptance` history, shares, messages | `person.delete()` |
| The login `Identity` | when it is purely a patient login (see below) |

| Kept | Why |
|---|---|
| `ConsentLedgerEntry` rows | they hold the email address and the permission history, no link to the account, and must outlive it ([consent model v2](consent-v2.md)) |
| `AuditEvent` rows | the access audit trail is its own chain |
| The `DeletionLogEntry` written by the deletion | see below |

A deletion that fails rolls back whole: no entry without a deletion, no
deletion without an entry. The person row is locked first, so a request
that arrives after another deleted the account gets 404 and logs nothing.
The session is logged out only after the commit, and the audit trail's
`record_delete` event still names the actor and the person
(`account_deletion.end_session` hands them to `AuditLogMiddleware`, which
would otherwise see a logged-out request).

### Who may send it

The PHR account delete, the legacy `me` delete and both consent POSTs
(decisions and document acceptance) take a session or partner login only
(`InteractiveSessionForWrites` in `api/permissions.py` and
`is_interactive_session`). An OAuth2 access token the member delegated to an
application, a service credential or an HTTP Basic password can be presented
without the member, so none of them may close the account, withdraw the
permission that closes it, or accept a document in the member's name; they
get 403. Reading the consent state with such a credential still works. The
admin path keeps its own staff check.

The deletion runs under `suppress_patient_record_refresh`: the clinical
rows' `post_delete` receivers would otherwise derive the record once per
row, for a record that is deleted with the person.

### The paths

| Path | Reason logged |
|---|---|
| `DELETE /api/v1/phr/account/` with `{"confirm": "DELETE"}` (`api/phr/account.py`) | `account_closed` |
| `DELETE /api/patient-info/me/` (legacy, same body) | `account_closed` |
| `POST /api/v1/phr/consent/` recording `withdrawn` on the record purpose | `permission_withdrawn` |
| `DELETE /api/v1/patient-records/{person_id}/admin-delete/` | `admin` |
| (reserved) | `inactivity` |

On every path the login goes only when it is purely a patient login
(`account_deletion.patient_only_identity`): an Identity that is staff or holds
provider access (`GroupAccess`) keeps its login and its grants, loses its
`PatientUser` link and record, and stays signed in; the log entry then has no
`identity_id`. A clinician who is also a member cannot revoke their own
provider access by withdrawing a permission.

The record purpose is the one whose key is `CONSENT_RECORD_PURPOSE_KEY`
(`my_record` by default). The member's consent view records every decision in
the batch first, then deletes, in the same transaction, so the ledger shows the
withdrawal that ended the account. The response is
`{"account_deleted": true, "deleted_at": ...}` instead of the consent state. A
refused decision anywhere in the batch rolls everything back, the deletion
included. Only the member's own view triggers this: `record_consent` called
with a `staff`, `migration` or `system` source records the withdrawal and
deletes nothing.

## The deletion log

`DeletionLogEntry` (`deletion_log_entry`) is append-only, in the style of
`ConsentLedgerEntry`: `save()` only inserts, and `delete()`, `update()` and
`bulk_update()` are refused by the ORM. It holds:

| Field | |
|---|---|
| `person_id` | the deleted Person id |
| `identity_id` | the deleted Identity id, or null when no login was deleted |
| `reason` | `account_closed`, `permission_withdrawn`, `admin`, `inactivity` |
| `deleted_at` | when |
| `signature` | HMAC-SHA256 over the four fields above |
| `chain_hash` | HMAC-SHA256 over (previous row's `chain_hash` \| this row's `signature`) |

No content, no email: identifiers and a time. The key is `AUDIT_HMAC_KEY`,
falling back to `SECRET_KEY`, as for `AuditEvent`; appends are serialized
under their own advisory lock so concurrent deletions extend one chain.
`account_deletion.verify_deletion_log()` recomputes every signature and link.
A row whose fields changed is `altered` (and `broken`, since its signature
feeds its link); a row whose `chain_hash` alone changed, or that follows a
removed or inserted row, is `broken`, as is the row after it. Unlike the
audit trail the chain cannot be switched off (`AUDIT_HASH_CHAIN_ENABLED` does
not apply) and the table is never pruned: the first row is the anchor.

## Replay after a restore

A backup taken before a deletion restores the deleted rows. The log says what
to remove again:

```bash
DATABASE_URL="${STAGING_DATABASE_URL:?}" python manage.py replay_deletion_log --dry-run
DATABASE_URL="${STAGING_DATABASE_URL:?}" python manage.py replay_deletion_log
```

The command verifies the chain first and refuses to replay a log that does not
verify: exit 1, nothing replayed. `--ignore-chain` replays anyway and exits 0,
with the breaks reported as errors; `--verify-only` just checks. Then, for
every entry, it deletes the Person and the Identity the entry names if they
exist again, each entry in its own transaction, and lists what it removed. It
writes no new entries.

Ids are not reused in normal operation, but a restored sequence could hand a
logged id to a new sign-up. A row created after the entry's time (the
Identity's `created_at`, or the `PatientUser` link's for a Person, which has
no timestamp) is therefore skipped and listed as `skipped`, never deleted. A
Person with no login and a reused id cannot be told apart, so run the replay
immediately after the restore all the same.

Entries written after the backup are themselves lost by the restore. Keep an
export of the table outside the database (`manage.py dumpdata
patient_portal.DeletionLogEntry`) if the restore procedure needs to replay
deletions the backup predates.

The log is classified as system data in `omop_core/services/instance_data.py`:
`copy_patient` and `copy_reference_data` never carry it.
