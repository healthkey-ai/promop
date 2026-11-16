# Sign-up through an invitation: country, age and declarations

How a branded member app (the FLF Patient Movement app first) signs a member
up against promop. Pilot sign-up is **invitation-only**: staff create a
`PatientInvitation` for a person, the member follows the single-use link, and
the app's own form posts the password together with the sign-up answers to
one endpoint, so an account is never created with half its declarations.
The consent side is [Consent model v2](consent-v2.md). (#2596)

## What the form shows

`GET /api/v1/phr/signup/policy/?lang=en` is public and names nobody:

```json
{
  "countries": [{"code": "GB", "name": "United Kingdom", "minimum_age": 18}, ...],
  "purposes": [{"key": "age_declaration", "kind": "declaration", "answers": ["granted", "declined", "withdrawn"],
                "stage": "sign_up", "gated_by_country": false, "enabled_countries": [],
                "text": {"version": "1.0", "language": "en", "title": "...", "summary": "...", "body": "..."}}],
  "documents": [{"key": "terms", "name": "Terms of Service",
                 "text": {"version": "0.4", "language": "en", "title": "...", "summary": "", "body": "..."}}]
}
```

- **`countries`** is the deployment's list, in order, each with the age a
  member there must declare they have reached.
- **`purposes`** are the active consent purposes with `stage == "sign_up"`
  that have a published text: the declarations. A purpose asked later (the
  permissions at the first profile) is not listed here; `GET consent/`
  serves it once the member is signed in. A country-gated purpose carries
  its `enabled_countries` so the form can show it only there.
- **`documents`** are the active legal documents with a published version.

The form records the `version` it showed for each. Wording republished
between the two requests is refused at acceptance (**409**), so a member
never agrees to text they did not see.

## Accepting

`POST /api/v1/patient-invitations/accept/` (public, the token is the
credential) takes, besides `token` and `password`:

| Field | |
|---|---|
| `country` | ISO 3166-1 alpha-2 from `countries`, case-insensitive. Written to the person's OMOP `Location.country`; the record's `country` derives from it. |
| `year_of_birth` | A four-digit year, written to `Person.year_of_birth` with month and day cleared, so the record derives `date_of_birth_precision == "year"` and never a month 1 that was not given. Requires `country`. |
| `consent.decisions` | `[{"purpose", "decision", "version"}, ...]`, as `POST consent/` takes them. Every sign-up declaration available to the member must be present and `granted`; a later-stage purpose may be answered too. |
| `consent.documents` | `[{"document", "version"}, ...]`. Every active document with a published version must be accepted. |
| `lang` | The language the member read; otherwise `Accept-Language`. |

`country` and `year_of_birth` are optional: an invitation to a hospital record
already carries both. The declarations are not: a deployment that has loaded
sign-up purposes or documents refuses an acceptance that omits one (**400**,
naming it), because the declaration *is* the age check. A deployment with
none loaded keeps the old contract, token and password alone.

Everything is one transaction. A refused sign-up leaves no account, no
consent history, no ledger entry and no location; the invitation stays
pending so the member can try again. On success the person is linked to the
new (or claimed) identity, given the `patient` role at the record's
organisation, and the record is derived once from the written facts before
the consent is recorded, so a purpose gated by country sees the country just
given.

## Age

A year of birth cannot prove that a member is of age: someone born 18 years
ago may still be 17. So the check is the member's **declaration**, a
`declaration` purpose on the `sign_up` stage, and the year is support only:

- a year that **cannot** reach the country's minimum age (born 17 or fewer
  years ago where the minimum is 18) is refused whatever is declared;
- the boundary year (born exactly `minimum_age` years ago) is accepted with
  the declaration;
- a year more than 120 years ago, or in the future, is refused.

The minimum age is **per country**: 18 by default, 19 in several Canadian
provinces and some US states. It is configuration, not code
(`patient_portal/signup_policy.py`):

| Setting | |
|---|---|
| `PHR_SIGNUP_COUNTRIES` | JSON list of alpha-2 codes offered. Unset: the pilot list, the UK, the EU and EEA, the US, Canada, Australia and Switzerland. |
| `PHR_SIGNUP_MINIMUM_AGES` | JSON object, code to age, e.g. `{"CA": 19}`. Unset: 18 everywhere. |

## The pilot texts

`patient_portal/data/consent/flf-pilot.json` holds the FLF pilot's purposes
and documents in `load_consent_texts`'s shape: the two sign-up declarations
(`age_declaration`, `sign_up_declaration`), the three permissions
(`my_record`, `community_figures`, `research` with "ask me later"), the two
contact preferences (`flf_updates` with a frequency, `study_contact`), and
the Terms of Service and Privacy Policy. Its wording is a **draft
placeholder** that FLF's approved text replaces, so no version carries a
`published_at`: loading it publishes nothing until `--publish` (staging) or a
date is set per version once FLF sign off.

```bash
DATABASE_URL="${STAGING_DATABASE_URL:?}" \
  .venv/bin/python manage.py load_consent_texts patient_portal/data/consent/flf-pilot.json --publish
```

## Not here

The server-to-server `POST /api/v1/patients/signup/` (an org's trusted app
creating accounts) does not take the sign-up answers yet; open self-sign-up
with email verification is soft-launch work; relationship to FL, preferred
language, reminders and partial postal code are member-profile fields the
plan puts in later work packages.
