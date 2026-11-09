# Consent model v2

What a member has agreed to, kept so that the wording is data, every answer is
history, and the record of permissions outlives the account. Built for the
branded member apps on the PHR API; nothing in it belongs to one deployment.

The legacy `PatientConsent` (one row per type, date overwritten on each save)
stays for `/api/v1/consents/` and the HealthKit data-sharing switch. New
consent work uses this model.

## The data

| Model | Table | Holds |
|---|---|---|
| `ConsentPurpose` | `consent_purpose` | One thing a member answers: a **permission**, a **contact preference** or a **declaration**. Its `answers` set is yes/no or yes/no/ask-me-later; `asks_frequency` with `frequency_options` for a preference that carries a cadence; `stage` tells the app when to ask it; `gated_by_country` with `enabled_countries` keeps it unavailable elsewhere. |
| `ConsentText` | `consent_text` | The wording of a purpose, one row per **version and language**. A published version is never edited or deleted: a wording change is a new version. The current version is the one whose first publication (in any language) is most recent, so a late translation of an older version does not make it current again. |
| `LegalDocument`, `LegalDocumentVersion` | `legal_document`, `legal_document_version` | Terms of service, privacy policy, and the like, versioned the same way. `requires_reacceptance` marks a significant change. |
| `ConsentEvent` | `consent_event` | **Append-only.** One answer: person, purpose, the text version seen, decision (`granted`, `declined`, `ask_later`, `withdrawn`), frequency, who recorded it and from where (`member`, `staff`, `migration`, `system`). The member's position on a purpose is their latest event for it. |
| `DocumentAcceptance` | `document_acceptance` | **Append-only.** Which version of a document the member accepted, and when, with a protected link to the text seen. A member must confirm again when a version marked a significant change has been published since the one they accepted, even if a minor one followed it. |
| `ConsentLedgerEntry` | `consent_ledger_entry` | **Append-only, no foreign keys.** The member's email address, the purpose or document key, version, decision and time, and nothing else. Written in the same transaction as every event and acceptance. Deleting the account, record or person leaves it in place. |

Append-only means the ORM refuses `save()` on an existing row, `delete()`,
and queryset `update()`, `bulk_update()` and `delete()`. Only a cascade from
the person removes events and acceptances; nothing removes ledger entries. The
Django admin shows the three histories read-only, keeps a purpose's or
document's key fixed once created, and makes a published version read-only
and undeletable; deleting a person from the admin still cascades through the
histories. (This guards the application; a database role that can only
insert into these tables is a deployment matter.)

The ledger is keyed on the email address at the time of each entry, which is
the account's address and is not necessarily verified. A member who changes
their address has entries under both; joining them is a question for the
deletion and retention work, not solved here. An account with no email
address cannot record consent (**400**), since there would be nothing to
write to the ledger.

A member's own answers are checked at write time: a withdrawal needs a
standing grant, a gated purpose refuses a member outside its countries, and
accepting a document version already accepted writes nothing new, so a
client retry does not duplicate the history.

## Texts as data

`manage.py load_consent_texts consent.json [--publish]` loads purposes,
documents and their versions from a file a deployment keeps in its own
repository. The load is additive: settings on a purpose or document are
updated, a version that does not exist is added, a version that exists is left
exactly as it is. So legal rewording is a new version in the file and a
deploy, with no code change. The file shape is in the command's docstring.
The admin edits the same rows; a published version's text is read-only there.

A missing translation falls back to English, then to any language, so a
member sees the wording that exists rather than nothing. A purpose with no
published text is listed with `available: false` and `text: null`, and
cannot be answered; the loader warns about every active purpose or document
left in that state. `published_at` in the file must carry a timezone offset.

## Enforcement

```python
from patient_portal.consent import has_consent
if has_consent(person, 'research'): ...
```

True only when the purpose exists and is active, the member's latest answer
is a grant, and, for a gated purpose, the member's record country is listed.
`persons_granting('research')` answers the same question for a cohort in two
queries. `purpose_states(person, language)` gives the full picture for a
screen.

Nothing enforces through these yet: the HealthKit data-sharing switch and
`/api/v1/consents/` still read the legacy `PatientConsent`. The first
consumer is the branded app's screens; wiring the switch to a named purpose is
a change of its own.

The country gate reads `PatientRecord.country` as the record holds it, which
comes from the hospital `Location` on ingest or the patient-info location
edit, and is matched case-insensitively against `enabled_countries` with no
alias handling: a record holding "United Kingdom" does not match `GB`. A
member with no country on record sees every gated purpose as unavailable.
A deployment that gates a purpose must therefore settle where its members'
country comes from (an ISO code on sign-up is the obvious choice) before
enabling the gate.

## The API

All under `/api/v1/phr/`, for the signed-in member and their own record only.
`?lang=xx` or the `Accept-Language` header picks the language.

| | |
|---|---|
| `GET consent/` | Every active purpose with its current text, the member's answer (`decision`, `granted`, `frequency`, `decided_version`, `decided_at`, `outdated`), and every document with `needs_acceptance`. |
| `POST consent/` | `{"decisions": [{"purpose", "decision", "version", "frequency"?}, ...]}`. All are recorded or none. `version` is required (**400** without it) and must be the current one, else **409** and the app refreshes: stale wording is never agreed to. Returns the same as `GET`. |
| `POST consent/documents/` | `{"document", "version"}` accepts the current version of a document; **409** when it is not current. |
| `GET consent/history/` | Every answer and acceptance, newest first. |

A sign-up records the declaration with one `POST`; the first profile records
each permission on its own; settings withdraws with `"decision": "withdrawn"`;
"confirm the new Terms" is `GET` showing `needs_acceptance` and then
`POST consent/documents/`.

Every request to these paths is classified `consent` in the audit trail by
`AuditLogMiddleware`.

## Not here

Account deletion and its deletion log, retention and purge of the ledger (six
years after the account closes), and the access audit log's database role are
separate work. A migration from an earlier consent record calls
`record_consent(..., source='migration', version=..., recorded_at=...)`, which
accepts a non-current version.

Consent is given to a deployment, so the histories are classified as system
data in `instance_data.py` and `copy_patient` does not carry them: a copied
member answers again, or is seeded with `source='migration'`. The history
endpoint is unpaginated; a member's history is a few dozen rows at most.
