# Staff sign-in, staff invitations and staff roles

Staff of a deployment can sign in to the admin through their organisation's own
OpenID Connect provider (Microsoft Entra ID, Google Workspace, or any other),
which applies its own two-step verification. Code: `patient_portal/staff_sign_in.py`;
tests: `tests/test_staff_sign_in.py`.

## Settings

| Setting | |
|---|---|
| `STAFF_SIGN_IN_PROVIDERS` | JSON list of providers (below). Empty: no provider sign-in. |
| `STAFF_PASSWORD_SIGN_IN` | `false`: staff and superuser accounts cannot sign in with a password at all, only through a provider. Members are not affected. Default `true`. |
| `STAFF_ROLES` | Comma-separated group names: the roles a staff invitation may give and a staff manager may set. |
| `STAFF_INVITATION_DAYS` | How long an invitation link works. Default 7. |
| `STAFF_SESSION_HOURS` | How long a provider sign-in lasts, from the sign-in (a deadline kept in the session, not the cookie's idle expiry). Default 8, so someone whose provider account is switched off is out within a working day. |

Each provider:

```json
{"key": "microsoft", "label": "Microsoft 365",
 "issuer": "https://login.microsoftonline.com/<tenant id>/v2.0",
 "client_id": "<application (client) id>", "client_secret_env": "STAFF_SIGN_IN_MICROSOFT_SECRET",
 "claims": {"tid": "<tenant id>"}, "require_mfa": true, "address_claim": "preferred_username"}
```

- `issuer`: the provider's OpenID issuer; its discovery document is read from
  `<issuer>/.well-known/openid-configuration` and must name the same issuer,
  with https authorization, token and key endpoints.
- `client_secret_env`: the environment variable holding the client secret (a
  secret manager, never the settings file).
- `claims`: values the ID token must carry exactly. Pin the organisation here:
  Microsoft `{"tid": "<tenant id>"}`, Google `{"hd": "<domain>"}`. A provider
  with none gets a check warning: any account of the provider could then try,
  and only an invitation stops it.
- `require_mfa`: refuse a sign-in whose ID token does not list `mfa` in `amr`.
  For Microsoft Entra ID, also require two-step verification for the app in a
  Conditional Access policy, so the provider asks every time. **Confirm on a
  real tenant that its ID tokens carry `amr`** before relying on it (the audit
  row of each sign-in records `mfa`): reports differ on whether v2.0 ID tokens
  always do. Without it every sign-in is refused, so do not switch staff
  passwords off until one has succeeded.
- `address_claim`: the claim an invitation's address is matched on. `email`
  (the default) counts only when the token says `email_verified` is true, with
  no fallback. For Microsoft use `preferred_username`, the sign-in name the
  tenant owns: Entra's `email` is never verified and can be edited.

The provider's redirect (reply) URI is `<APP_BASE_URL>/admin/sign-in/callback/`,
so `APP_BASE_URL` must be https. System checks stop a deploy whose settings could
not work: an incomplete or non-object entry (`E010`, `E016`), a key listed twice
(`E011`), an http issuer (`E012`), staff passwords off with no provider
(`E013`), a client secret variable that is empty (`E014`), and an http
`APP_BASE_URL` (`E015`). A malformed `STAFF_SIGN_IN_PROVIDERS` is refused when
settings load.

## How a sign-in works

1. `/admin/sign-in/<key>/` (the buttons on the admin's sign-in page) or an
   invitation's link, `/admin/sign-in/invitation/<token>/`, remembers a one-time
   `state`, `nonce` and PKCE verifier in the session and sends the person to the
   provider.
2. `/admin/sign-in/callback/` checks the `state`, exchanges the code (with the
   verifier and the client secret), and checks the ID token: signature against
   the provider's keys, issuer, audience (and `azp` when it names anyone, or
   when there are several audiences), expiry, `nonce`, the pinned `claims`, and
   `mfa` where required. Ten minutes at most from start to callback.
3. The account is the provider's (issuer, `sub`), never an email address.
   Without an invitation, it must exist and still have admin access. With the
   session's invitation (open, for this provider, and the address in
   `address_claim` must be the invited one), the account is made if it is new,
   with no usable password, and given the invitation's access: its staff role,
   replacing any other, or full staff. So an invitation also restores a
   removed staff member, or changes a role, though never the inviter's own: an
   invitation someone sends to their own address is refused when it is made,
   and when it is redeemed.
4. The session lasts `STAFF_SESSION_HOURS`, and is marked as a provider
   sign-in.

Every outcome is an `auth` audit event (`detail.staff_sign_in`: `signed_in`
with whether `mfa` was in `amr`, `refused` with a reason code, or `cancelled`),
with no token, address or claim. An invitation link's token is redacted from
the request log, the audit trail and Sentry, as only its hash is stored.

## Staff roles in the admin

Two kinds of staff account, kept apart on purpose:

- **Role staff**: in a group named in `STAFF_ROLES`, and **not** `is_staff`.
  They reach the admin through their role, and only in a session a provider
  signed in (`admin.site.has_permission` is widened to them by
  `staff_sign_in.admin_site_access`): a role account that also holds a password
  gets no admin from a password sign-in. So role staff sign in only through a
  provider; the admin's own password form never admits them. Everywhere
  else, the API included, they are ordinary accounts with no patient data,
  because across promop's API `is_staff` means full access. A staff
  invitation gives a role.
- **Full staff**: `is_staff` (or superuser), promop's full access. Only a
  superuser invites one ("Full staff access").

Roles are Django groups, named in `STAFF_ROLES` and set up by the deployment.
promop adds two permissions on `Identity` for them:

- `support_member_accounts`: the account actions on members, never on staff of
  either kind: "Email password reset link", "Require password change at next
  login", "Withdraw a permission on the member's behalf" (one member: choose
  the permission, then `record_consent` as `staff`, with its consequences),
  and "Send the invitation again" on patient invitations (only a person's
  latest, uncancelled invitation, to the address their record holds). A
  patient invitation's token is shown to superusers only: it is the whole
  credential for claiming the member's account.
- `manage_staff`: invite staff with a role (Staff invitations → Add), cancel an
  invitation, set another role staff member's role (only to a `STAFF_ROLES`
  group; the account's other groups are kept; never their own), and "Remove
  staff access" from role staff (full staff: a superuser). Each is in the
  admin's history.

Whatever its permissions, only a superuser changes an account's address,
staff or superuser status, Premium, or password, adds or deletes an account
(`IdentityAdmin`), changes a patient invitation, or sees or changes a group
(the admin's `Group` pages are superuser-only), so no role can widen its own
access or take an account over. Never give a role a permission on
`auth.group` or `auth.permission`. Health data stays behind its own model
permissions (`omop_core.PatientRecord` and the rest): grant none of them to a
role that must not read it.

The model permissions each kind of role needs besides the custom one, or the
admin shows it an empty index:

| Role | Permissions |
|---|---|
| Member support | `patient_portal.view_identity`, `patient_portal.support_member_accounts`, `patient_portal.view_patientinvitation` |
| Staff manager | `patient_portal.view_identity`, `patient_portal.change_identity` (the role field only), `patient_portal.manage_staff` |

Support actions are one at a time where they are heavy or irreversible
("Withdraw a permission", "Send the invitation again"), and every support and
manager action is in the admin's history (`LogEntry`) naming the account it
acted on. "Send the invitation again" acts on any organisation's invitation
in the deployment, unlike the API's per-organisation invite. "Remove staff
access" removes the staff roles only; groups and permissions a superuser gave
outside them stay, as a role change keeps them.

## Turning passwords off for staff

Set up the providers, check that a real sign-in succeeds (and, with
`require_mfa`, that its audit row says `mfa`), and invite every staff member
first; then set `STAFF_PASSWORD_SIGN_IN=false`. From then on a staff or
superuser password opens nothing, a wrong one counts nothing (no lockout of
staff by others), and a staff session that began with a password is signed out
at its next request (`StaffPasswordSessionMiddleware`). Keep a break-glass path
outside the web sign-in, such as `manage.py` through the deployment's own
access controls.

A staff member invited as a member at the same address: a provider-made staff
account is never claimed (its holder gets a separate member account); a local
(password) staff account is, as before, refused with "an account already
exists", because two local accounts on one address would break password
sign-in for both (`EmailBackend` then signs neither in).

**Before switching staff passwords off,** split any local account that is both
staff and a member (has a `PatientUser`): with passwords off it can no longer
sign in, and a provider sign-in makes a different account with no record. Give
the member their own account first.
