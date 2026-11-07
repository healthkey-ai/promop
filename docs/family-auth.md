# Family auth: PROMOP as identity host

phr installations sign their users in against **PROMOP accounts** (phr#43,
phr `docs/identity-architecture.md`, "promop-hosted"). PROMOP serves the
HealthKey family's auth contract, the one phr has always exposed, and issues
its RS256 tokens for its own `Identity` rows. phr keeps no credentials.

## Endpoints — `/api/v1/family/auth/`

PROMOP's own `/api/v1/auth/login/` is its session login, so the contract
lives under `family/`. A phr installation sets its API base to
`https://<promop>/api/v1/family` and calls the same paths as on itself.

| | Request | Response |
|---|---|---|
| `POST login/` | `{email, password}` | `{access, refresh}`; 401 for any bad credential (no enumeration); 403 `email_not_verified` for a self-signup that hasn't verified |
| `POST refresh/` | `{refresh}` | a new `{access, refresh}`; the old refresh token is spent |
| `POST logout/` | `{refresh}` | 205; revokes it (never fails) |
| `GET me/` | bearer access | the user, phr's `AppUser` shape |
| `GET jwks/` | — | RFC 7517 key set (RS256) |
| `POST introspect/` | `{token}` | RFC 7662 `{active, ...claims}`, from the identity as it is now |

Sign-up is PROMOP's public org sign-up, `POST /api/v1/orgs/<slug>/patient-signup/`
(1 phr installation = 1 Organization with `allows_patient_signup`). It creates
the account, Person and record, and emails a verification link; login is
refused until it is followed.

Credentials are checked exactly as PROMOP's own login checks them
(`EmailBackend`: local accounts, lockout).

## Tokens

- **access** (`FAMILY_AUTH_ACCESS_SECONDS`, 15 min): `iss` = `FAMILY_AUTH_ISSUER`
  (this PROMOP's https origin), `aud` = `FAMILY_AUTH_AUDIENCE` (default
  `promop-api`), `sub` = the Identity's uid, `user_id`, `email`,
  `email_verified`, `identity_level` (`ial1` verified, `ial0` not), `claims`
  {`ADMIN`: staff, `MEDICAL_RECORDS`: the `MEDICAL_RECORDS` group}, `person_id`.
- **refresh** (`FAMILY_AUTH_REFRESH_SECONDS`, 7 days): `aud` = the issuer (only
  PROMOP reads it), one row in `family_auth.RefreshToken`. Each refresh spends
  it; presenting a spent one again revokes every live refresh token of that
  identity.

PROMOP accepts its own access tokens first in the DRF chain
(`FamilyTokenAuthentication`): the token names the Identity directly, so a
phr user is the same account as on PROMOP's pages — no partner identity, no
email linking. Other issuers fall through to the existing authenticators.

## Configuration

```
FAMILY_AUTH_PRIVATE_KEY   PEM, \n-escaped on one line (required)
FAMILY_AUTH_PUBLIC_KEY    optional; derived from the private key
FAMILY_AUTH_ISSUER        https://<this promop> (required)
FAMILY_AUTH_AUDIENCE      promop-api[,<sibling>…]
FAMILY_AUTH_THROTTLE_RATE 30/minute per client address (login, refresh, introspect)
CORS_ALLOWED_ORIGINS      + every phr installation's origin
```

Without key and issuer the endpoints answer 404 and no family token is accepted.

## Siblings

labs, soc and exact verify the family token the way they verify phr's: set
their `PHR_ISSUER` to `FAMILY_AUTH_ISSUER` and `PHR_JWKS_URL` to
`https://<promop>/api/v1/family/auth/jwks/` (or `PHR_INTROSPECT_URL` to
`…/introspect/`).
