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
| PROlog survey responses, answers, invitations, consents | `omop_core.services.prolog_cleanup.delete_prolog_data_for_persons` (a response protects its participant, so these go first) |
| Therapy episode events | explicitly: `EpisodeEvent.episode_id` is a bare integer no cascade covers |
| The `Person` and everything that cascades from it: the OMOP clinical rows, `PatientRecord`, `PatientUser`, `ConsentEvent` and `DocumentAcceptance` history, shares, messages | `person.delete()` |
| The login `Identity` | when one is handed in (see below) |

| Kept | Why |
|---|---|
| `ConsentLedgerEntry` rows | they hold the email address and the permission history, no link to the account, and must outlive it ([consent model v2](consent-v2.md)) |
| `AuditEvent` rows | the access audit trail is its own chain |
| The `DeletionLogEntry` written by the deletion | see below |

A deletion that fails rolls back whole: no entry without a deletion, no
deletion without an entry. The session is logged out only after the commit.

### The paths

| Path | Reason logged | Identity deleted |
|---|---|---|
| `DELETE /api/v1/phr/account/` with `{"confirm": "DELETE"}` (`api/phr/account.py`) | `account_closed` | the member's own |
| `DELETE /api/patient-info/me/` (legacy, same body) | `account_closed` | the member's own |
| `POST /api/v1/phr/consent/` recording `withdrawn` on the record purpose | `permission_withdrawn` | the member's own |
| `DELETE /api/v1/patient-records/{person_id}/admin-delete/` | `admin` | the linked login, unless it also holds provider access (`GroupAccess`) |
| (reserved) | `inactivity` | — |

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
`account_deletion.verify_deletion_log()` recomputes every signature and link:
an altered row is reported as `altered`, a removed or inserted row as `broken`
on the row that follows the gap.

## Replay after a restore

A backup taken before a deletion restores the deleted rows. The log says what
to remove again:

```bash
DATABASE_URL="${STAGING_DATABASE_URL:?}" python manage.py replay_deletion_log --dry-run
DATABASE_URL="${STAGING_DATABASE_URL:?}" python manage.py replay_deletion_log
```

The command verifies the chain first and refuses to replay a log that does not
verify (`--ignore-chain` overrides, `--verify-only` just checks; exit 1 on a
break). Then, for every entry, it deletes the Person and the Identity the entry
names if they exist again, each entry in its own transaction, and lists what
it removed. It writes no new entries. Run it immediately after the restore,
before new sign-ups: ids are not reused in normal operation, but a restored
sequence could hand a logged id to a new account.

Entries written after the backup are themselves lost by the restore. Keep an
export of the table outside the database (`manage.py dumpdata
patient_portal.DeletionLogEntry`) if the restore procedure needs to replay
deletions the backup predates.

The log is classified as system data in `omop_core/services/instance_data.py`:
`copy_patient` and `copy_reference_data` never carry it.
