# Identity Architecture: OIDC-Based Shared Identity

## Overview

All HealthKey platform services authenticate users via a shared OIDC-based
Identity model. Each service stores a minimal `Identity` record: the
`(issuer, sub)` tuple from the authentication provider. No email, no name,
no password is stored for external identities.

Any new service joining the platform adopts the same Identity model and
token provider interface. The pattern is designed to scale to N services
without coordination ... each service resolves identity independently from
the same JWT.

### Data Ownership

The customer owns all patient data. HealthKey services are software the
customer deploys to manage that data on the customer's behalf. ctomop
stores OMOP clinical records in the customer's database. hk-labs processes
lab uploads and writes results to ctomop. The customer controls the IdP,
the user accounts, and the infrastructure.

HealthKey services never hold data independently of the customer. In
integrated mode, the customer's host app is the authority. In standalone
mode, the customer runs the service directly and owns the local database.

### Two Operating Modes

Every HealthKey service can run in one of two modes:

| Mode | Description |
|---|---|
| **Standalone** | The customer runs a service independently with local users (`iss="urn:local"`). The service has its own login, manages data in its own database. No dependency on other services or external IdPs. Suitable for development, on-prem deployments, or single-tenant use. |
| **Integrated** | Services are embedded into the customer's host application (e.g. ht-phr) via Module Federation. Users authenticate via the customer's IdP (e.g. Firebase). The same JWT is sent to all HealthKey backends. Services communicate via REST APIs using `(issuer, sub)` as the shared identity anchor. |

The Identity model and auth flow are identical in both modes. The only
difference is which providers are listed in `PARTNER_AUTH_PROVIDERS` and
whether cross-service URLs are configured. Switching between modes is a
settings change, not a code change.

In **standalone mode**, each service is fully self-contained:
- Own user registration and login
- Own data storage in the customer's database
- Own admin interface
- No external IdP, no host app, no cross-service calls

In **integrated mode**, services are embedded in the customer's host app:
- The customer's host app (e.g. ht-phr) owns the frontend, IdP, and user accounts
- HealthKey services mount as Module Federation remotes in the host frontend
- The customer's IdP token is forwarded to all HealthKey backends
- hk-labs pushes lab results to ctomop on commit
- ctomop serves lab results to the host frontend directly
- All services resolve the same `(issuer, sub)` from the customer's JWT

### HealthKey Platform Services

| Service | Role | Identity Model | Domain Linkage |
|---|---|---|---|
| **hk-labs** | Lab report upload, extraction, LOINC matching | `accounts.Identity` | `UploadJob.user -> Identity` |
| **ctomop** | OMOP CDM storage, lab results API, patient portal | `patient_portal.Identity` | `PatientUser(identity -> Identity, person -> Person)` |

### Host Applications (Customers)

The customer's host application is not a HealthKey service. It owns the
user base, the IdP, the patient data, and the frontend. It deploys
HealthKey services as federated modules and API backends to manage that
data.

| Host | IdP | Integration |
|---|---|---|
| **ht-phr** (HealthTree) | Firebase | Mounts `labs_remote` (hk-labs) and `labs_results_remote` (ctomop) via Module Federation |

The host adopts the same Identity model pattern so its backend can resolve
the same `(issuer, sub)` tuple when needed (e.g. for user profile storage).
But the host's Identity table is its own.

### Adding a New Service

A new HealthKey service needs:
1. Copy the `Identity` model (same fields, same constraints, same manager)
2. Add `PARTNER_AUTH_PROVIDERS` setting with desired providers
3. Set `AUTH_USER_MODEL` to point at the new Identity
4. Optionally create an app-specific profile model for service-local fields

A shared library (`healthkey-identity`) is planned to replace step 1 with
a pip install. It will provide Identity, IdentityManager, TokenProvider,
PartnerAuthentication, and the Firebase provider out of the box. Until
then, copy from an existing service.

The service then authenticates the same tokens as every other HealthKey
service. Cross-service calls use `(issuer, sub)` as the identity anchor.
No shared database, no shared auth service, no token exchange.

The new service works standalone from day one. Integration with other
services and host apps is additive ... configure the cross-service URLs
and token providers and it joins the platform.

User profile data (email, display name) is read from JWT claims at request
time, never persisted in the service's database (except for local identities).

---

## OIDC Terminology

| OIDC Claim | Meaning | Example (Firebase) |
|---|---|---|
| `iss` (issuer) | Who issued the token | `https://securetoken.google.com/healthtree-test` |
| `sub` (subject) | Immutable user ID at the issuer | `abc123def456` (Firebase UID) |
| `email` | User's email | `user@example.com` |
| `name` | Display name | `Jane Doe` |

The `(iss, sub)` pair is globally unique and immutable.

---

## Identity Model

Each service stores the same minimal table:

```python
class Identity(AbstractBaseUser, PermissionsMixin):
    issuer = models.CharField(max_length=255)
        # "https://securetoken.google.com/<project>" or "urn:local"
    sub = models.CharField(max_length=255, unique=True)
        # Firebase UID, SAML subject, or UUID for local

    # Only populated for local (iss="urn:local") identities
    email = models.EmailField(blank=True, default="")
    name = models.CharField(max_length=255, blank=True, default="")

    is_active = models.BooleanField(default=True)
    is_staff = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    USERNAME_FIELD = "sub"
    REQUIRED_FIELDS = ["email"]

    objects = IdentityManager()

    class Meta:
        db_table = "identity"
        constraints = [
            models.UniqueConstraint(
                fields=["issuer", "sub"],
                name="uq_identity_issuer_sub",
            ),
        ]

    @property
    def is_local(self) -> bool:
        return self.issuer == "urn:local"
```

`AbstractBaseUser` provides the `password` field. For **local identities**
(`iss="urn:local"`), the password is set and used for Django admin login.
For **external identities** (Firebase, SAML), the password is set to
unusable ... authentication happens via token verification.

Local identities store `email` and `name` because there's no external
JWT to read them from. External identities leave these fields blank.

### Local Provider

Local identities use a synthetic issuer:

| Claim | Value |
|---|---|
| `iss` | `urn:local` |
| `sub` | UUID v4 (generated at creation time, immutable) |

Django admin login works via standard password auth against the Identity
model. No token exchange needed.

---

## Request-Scoped User Data

After token verification, the auth backend attaches claims to the request:

```python
@dataclass
class TokenClaims:
    issuer: str
    sub: str
    email: str
    name: str | None
    raw: dict[str, Any]
```

- `request.user` -> `Identity` model instance (for FK references, permissions)
- `request.auth` -> `TokenClaims` (for user data)

Any view that needs the user's email reads `request.auth.email`, not a
database field.

### Request Auth Normalization

`request.auth` (TokenClaims) is always populated, regardless of auth method:

| Auth Method | `request.user` | `request.auth` |
|---|---|---|
| Firebase token | Identity (external) | TokenClaims from JWT |
| SAML token | Identity (external) | TokenClaims from assertion |
| Session (local) | Identity (local) | TokenClaims synthesized from model fields |
| Service token | Identity (service) | `"service-token"` string |

For session-based auth (Django admin, standalone mode login), a middleware
synthesizes TokenClaims from the Identity model:

```python
class TokenClaimsMiddleware:
    def __call__(self, request):
        if request.user.is_authenticated and request.auth is None:
            request.auth = TokenClaims(
                issuer=request.user.issuer,
                sub=request.user.sub,
                email=request.user.email or None,
                name=request.user.name or None,
                raw={},
            )
        return self.get_response(request)
```

---

## Authentication Flow

```
Client sends: Authorization: Bearer <JWT>
  |
  +- decode_jwt_unverified(token) -> extract iss, sub
  |
  +- Route to provider based on iss:
  |    "https://securetoken.google.com/*" -> FirebaseTokenProvider
  |    "https://login.corp.example.com"   -> CorporateSAMLProvider (future)
  |
  +- Provider.verify(token) -> TokenClaims(issuer, sub, email, name, raw)
  |
  +- Identity.objects.get_or_create(issuer=claims.issuer, sub=claims.sub)
  |
  +- return (identity, claims)
```

No email-based lookup. No provider-specific fields on the model.

### Token Provider Interface

```python
class TokenProvider(abc.ABC):

    @abc.abstractmethod
    def can_handle(self, token, unverified_payload) -> bool:
        """Lightweight routing check. No secrets, no external calls."""

    @abc.abstractmethod
    def verify(self, token) -> TokenClaims | None:
        """Full verification. Returns claims or None."""
```

Providers are listed in `PARTNER_AUTH_PROVIDERS` setting. The auth backend
iterates them in order; the first that recognises the token wins.

---

## Deployment Configuration

### Standalone

```python
PARTNER_AUTH_PROVIDERS = []          # no external IdP
CTOMOP_SYNC_URL = ""                 # no cross-service sync (hk-labs)
```

All users are local (`iss="urn:local"`). Service handles its own
registration, login, and data. Works offline, works on-prem, works
in development.

### Integrated

```python
PARTNER_AUTH_PROVIDERS = [
    "apps.accounts.providers.firebase.FirebaseTokenProvider",
]
CTOMOP_SYNC_URL = "https://ctomop.example.com/api/lab-results/sync/"  # hk-labs
CTOMOP_SERVICE_TOKEN = "..."         # for service-to-service calls
```

External users authenticate via Firebase. Local identities remain for
admins and service accounts. Cross-service calls use `(issuer, sub)` or
service tokens.

---

## Per-Service Details

Each HealthKey service owns its Identity table and links it to
service-specific models. The pattern is the same everywhere ... only
the linked models differ.

### hk-labs (Upload Pipeline)

Lab report upload, LLM extraction, LOINC matching, commit to ctomop.

```python
AUTH_USER_MODEL = "accounts.Identity"

# UploadJob.user -> FK(Identity) via settings.AUTH_USER_MODEL
```

- On commit, sends `actor_iss` + `actor_sub` to ctomop. ctomop resolves to Person
- No local person_id storage. ctomop is the source of truth for person linkage
- SimpleJWT for local email/password login (standalone mode)

### ctomop (OMOP CDM + Lab Results)

Clinical data storage (OMOP), lab results, patient portal.

```python
AUTH_USER_MODEL = "patient_portal.Identity"

class PatientUser(models.Model):
    identity = models.OneToOneField(Identity, on_delete=models.CASCADE,
                                    related_name="patient_user")
    person = models.OneToOneField("omop_core.Person", on_delete=models.CASCADE,
                                  related_name="portal_user")
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
```

- Person resolution (write path / sync): `(actor_iss, actor_sub)` → Identity →
  `resolve_or_create_person()` auto-provisions Person + PatientInfo + PatientUser
- Person resolution (read path / API): `Identity → PatientUser → Person` (primary).
  Email fallback on `PatientInfo.email` is restricted to prevent cross-org data
  leaks: org-scoped requests filter by org; non-superusers without org scope
  cannot use the email fallback; superusers retain cross-org access.
- `PatientInfo.email` kept as clinical contact info (demographics, not auth)
- `_ensure_person()` auto-provisions Person + PatientInfo + PatientUser on first login

### Template for New Services

Any new service follows this pattern:

```python
# settings.py
AUTH_USER_MODEL = "<app>.Identity"
PARTNER_AUTH_PROVIDERS = [
    "<app>.providers.firebase.FirebaseTokenProvider",
]

# models.py — copy Identity + IdentityManager from any existing service

# Optional: service-specific profile
class ServiceProfile(models.Model):
    identity = models.OneToOneField(Identity, on_delete=models.CASCADE)
    # ... service-specific fields
```

Cross-service calls pass `(issuer, sub)` in the request body. The receiving
service does `Identity.objects.get_or_create(issuer=..., sub=...)` to resolve
or auto-provision the identity locally.

---

## Cross-Service Flows

These examples use ht-phr as the host app, but the pattern applies to any
host that forwards its IdP token to HealthKey backends.

### Self-Service Upload (Patient Uploads Own Labs)

```
Host frontend (e.g. ht-phr)
  | IdP token: iss=".../healthtree-test", sub="abc123"
  |
  +-> hk-labs backend
  |     Identity.get_or_create(iss, sub) -> identity_id=7
  |     UploadJob.user = identity_id=7
  |     ... extraction, review ...
  |
  +-> hk-labs commit -> POST to ctomop /api/lab-results/sync/
  |     Body: { "actor_iss": "...", "actor_sub": "abc123",
  |             "measurements": [...] }
  |
  +-> ctomop sync endpoint:
        Identity.get_or_create(iss, sub) -> identity_id=12
        _ensure_person(identity) -> Person + PatientInfo + PatientUser
        PatientUser.objects.get(identity_id=12) -> person_id=1042
        Create Measurements for person_id=1042
```

hk-labs never stores a person_id. ctomop resolves identity to Person on its side.

### On-Behalf-Of Upload (Navigator Uploads for Patient)

```
Host frontend
  | IdP token: sub="nav789" (navigator)
  | Target patient: person_id=1042
  |
  +-> hk-labs commit -> POST to ctomop /api/lab-results/sync/
  |     Body: { "actor_iss": "...", "actor_sub": "nav789",
  |             "person_id": 1042,
  |             "measurements": [...] }
  |
  +-> ctomop sync endpoint:
        Validate: actor has write access to person_id=1042
        Create Measurements for person_id=1042
```

### Direct ctomop Read (Lab Results Display)

```
Host frontend
  | IdP token: sub="abc123"
  |
  +-> ctomop backend (via labs_results_remote federation module)
        Identity.get_or_create(iss, sub) -> identity_id=12
        PatientUser.objects.get(identity_id=12) -> person_id=1042
        Return lab results for person_id=1042
```

### Standalone Mode

Each service operates independently with local identities (`iss="urn:local"`).
No host app, no external IdP.

When `CTOMOP_SYNC_URL` is empty, hk-labs stores upload metadata locally
and does not push to ctomop. Lab results stay in hk-labs only.

When a standalone hk-labs is configured to point at a standalone ctomop,
it sends `(urn:local, sub)` on commit. ctomop auto-provisions a matching
local identity and Person if needed.

---

## Identity Records

### Integrated Mode

Same user, multiple databases, no data divergence. The host app (ht-phr)
and HealthKey services each resolve the same `(issuer, sub)` independently.

```
Host IdP (Source of Truth, e.g. Firebase)
  UID: "abc123", Email: "jane@example.com"
  iss: "https://securetoken.google.com/healthtree-test"
                         |
      +------------------+------------------+
      |                  |                  |
  Host DB (ht-phr)   hk-labs DB         ctomop DB
  +------------+    +------------+    +------------+
  | Identity   |    | Identity   |    | Identity   |
  |  id: 3     |    |  id: 7     |    |  id: 12    |
  |  iss: fb.. |    |  iss: fb.. |    |  iss: fb.. |
  |  sub: abc..|    |  sub: abc..|    |  sub: abc..|
  |  email: "" |    |  email: "" |    |  email: "" |
  +-----+------+    +-----+------+    +-----+------+
        |                 |                 |
  (host-specific)   +-----+------+    +-----+------+
                    | UploadJob  |    |PatientUser |
                    |  user -----+    | identity---+
                    +------------+    | person ----+
                                      +------------+
                                             |
                                      +------+-----+
                                      | Person     |
                                      |  id: 1042  |
                                      +------------+
```

External Identity rows store only `(issuer, sub)`. The internal `id` differs
per database (auto-increment), used only for local FK references.

### Standalone Mode

```
  hk-labs (standalone)             ctomop (standalone)
  +------------------+             +------------------+
  | Identity         |             | Identity         |
  |  iss: urn:local  |             |  iss: urn:local  |
  |  sub: "a1b2c3.." |             |  sub: "d4e5f6.." |
  |  email: jane@..  |             |  email: jane@..  |
  |  password: ****  |             |  password: ****  |
  +-----+------------+             +-----+------------+
        |                                |
  +-----+------+                   +-----+------+
  | UploadJob  |                   |PatientUser |
  |  user -----+                   | identity---+
  +------------+                   | person ----+
                                   +------------+
  No sync to ctomop.                     |
  Results stay local.              +-----+------+
                                   | Person     |
                                   |  id: 1042  |
                                   +------------+
```

Each service has its own users, its own data. They can optionally be
connected by configuring `CTOMOP_SYNC_URL`, at which point hk-labs pushes
to ctomop using the `(urn:local, sub)` identity anchor.

---

## Adding a New Auth Provider

1. New `TokenProvider` subclass with `can_handle()` and `verify()`:

```python
class HospitalSAMLProvider(TokenProvider):
    ISSUER = "https://login.hospital.example.com"

    def can_handle(self, token, unverified):
        return (unverified or {}).get("iss") == self.ISSUER

    def verify(self, token):
        claims = verify_saml_token(token)
        return TokenClaims(
            issuer=self.ISSUER,
            sub=claims["sub"],
            email=claims.get("email"),
            name=claims.get("name"),
            raw=claims,
        )
```

2. Add to `PARTNER_AUTH_PROVIDERS` in whichever service(s) should accept it.

3. Nothing else. The Identity model, `get_or_create`, and all downstream code
   work unchanged.

### Multi-Provider Identity Linking

One human may authenticate via multiple providers. These create separate
Identity rows. Linking is solved at the `PatientUser` level: point two
Identity rows at the same Person.

```python
PatientUser.objects.create(identity=second_identity, person=existing_person)
```

Build the linking UI when a concrete second-provider deployment appears.

---

## App-Specific Data

The Identity model is deliberately thin. App-specific data lives in
separate tables:

| App | Local Data | Where |
|---|---|---|
| ht-phr (host) | `identity_level` (IAL1/IAL2) | `IdentityProfile` |
| ht-phr (host) | `is_admin`, `has_medical_records` | JWT custom claims (not stored) |
| hk-labs | Upload history | `UploadJob.user -> Identity` |
| ctomop | Person link | `PatientUser.identity -> Identity` |
| ctomop | Patient demographics | `PatientInfo` (clinical, not auth) |
| ctomop | Consent, messages | `PatientConsent`, `PatientMessage` via `PatientUser` |
| ctomop | Org membership | Via OAuth2 Application scoping |

Host apps store their own app-specific data in their own tables. The
platform doesn't prescribe what hosts keep locally.

---

## GDPR Erasure (Right to Deletion)

The customer owns the patient data, so the customer's host app initiates
deletion and fans out to HealthKey services to erase their copies:

```
Patient requests deletion (via host app UI or admin action)
  |
  +-> Customer's host (e.g. ht-phr):
  |     Delete its own Identity + profile data
  |     Revoke IdP account (e.g. Firebase admin SDK)
  |
  +-> hk-labs:
  |     UploadJob.user -> SET NULL (preserve audit trail, anonymize actor)
  |     Delete Identity
  |
  +-> ctomop:
        PatientUser -> soft-delete (is_active=False) or hard delete
        Person -> anonymize (zero out demographics, keep measurements
                 for aggregate research if consented, else delete)
        PatientInfo -> delete (contains PII)
        Identity -> delete
```

- Host calls each HealthKey service via service-to-service API with
  `(issuer, sub)` of the identity to delete. Each service erases its
  local copy of the patient's data.
- 30-day grace period before hard deletion (Identity.is_active=False blocks login)
- In standalone mode, the customer runs the service directly, so it
  handles the full cascade locally

---

## Cross-Service Communication

Services communicate via REST APIs. The caller identifies itself and/or the
target user using the `(issuer, sub)` tuple:

| Pattern | Payload Fields | Example |
|---|---|---|
| Self-service (user acts on own data) | `actor_iss`, `actor_sub` | Patient uploads own labs |
| On-behalf-of (actor writes for another) | `actor_iss`, `actor_sub`, `person_id` | Navigator uploads for patient |
| Service-to-service (no user context) | `Authorization: Bearer <service-token>` | Scheduled sync job |

The receiving service always resolves identity locally. No shared database,
no token exchange, no identity service dependency.

---

## Decisions

1. **Shared library** ... A `healthkey-identity` package will provide
   Identity, IdentityManager, TokenProvider, PartnerAuthentication, and
   built-in providers (Firebase, local). Currently duplicated across repos.
   The interface is stable, extraction is next.

## Open Questions

1. **ServiceTokenAuthentication** ... service-to-service auth uses a pre-shared
   Bearer token mapped to a superuser. Options:
   - Keep as-is (service tokens are not user identities)
   - Create a service Identity with `iss="urn:service:<name>"`, `sub="<role>"`
   - Use OAuth2 client_credentials flow (existing in ctomop)

2. **Shared library scope** ... What goes in `healthkey-identity` vs stays
   per-service? Candidates: Identity model, IdentityManager, TokenProvider
   base, PartnerAuthentication, FirebaseTokenProvider, TokenClaims,
   decode_jwt_unverified, provider registry. App-specific profile models
   and service-specific providers stay per-service.
