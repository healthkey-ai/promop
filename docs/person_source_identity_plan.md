# Person source identity — plan

Issue: #1913. Consumer: the HealthTree ETL (etl#42 thread, `healthkey-ai/etl`).

## The gap

An importer holds a source id for a patient. promop has nowhere to put it.
`POST /api/persons/find_or_create/` refuses machine callers, and
`POST /api/persons/provision/` refuses `actor_iss`/`actor_sub`, so the only key
an importer and promop share is the email address.

That rule stays. `patient_portal/person_adoption.py` states why: only
`Identity.email_verified_at` is proof of an address, and a server-to-server
caller's assertion is not, because the importer reads the address out of a
vendor export without verifying it either. Binding a `PatientUser` on an
unverified account id would hand one patient's history to another login, and
every later path short-circuits on that link.

What is missing is a durable answer to a different question: *which patient is
this importer writing about*, as opposed to *who may log in as them*.

## Why email is not enough

Measured on 2026-10-20 in the **HealthTree Cloud Run deployment**, read only. That deployment, not Render staging, is where this ETL imports, so it is
the only place the coverage question can be answered. CLAUDE.md reserves the
word staging for Render's `promop-staging`, which holds none of this ETL's data
and which no DSN in this working copy can reach, so it was not measured. Every
figure and conclusion below is scoped to the HealthTree deployment.

| Count | Value |
|---|---|
| `person` rows | 14,796 |
| with an email | 1,421 |
| without an email | 13,375 |
| claimed (`patient_user`) | 25 (19 with an email, 6 without) |
| addresses held by more than one person | 1 group, 2 persons |
| persons with `patient_source_code` rows, so ETL-imported | 3,685 |
| of those, no email | 2,299 (62%) |

So verified-email adoption reaches at most 1,386 of the 3,685 imported
patients. The other 2,299 have no address on either side of the match and stay
forked from the patient's login however the adoption rules improve.

The ETL reports the mirror image at the source: 10,120 of 10,169 warehouse
users carry an email, near-unique. The coverage is lost on the way in, not at
the source, because `provision` was called without one.

## Design

Agreed in #1913 with the ETL.

### `PersonSourceIdentifier`

| Field | Purpose |
|---|---|
| `organization` | tenant owning the namespace |
| `namespace` | source identifier system, e.g. `urn:healthtree:account` |
| `subject` | opaque account or patient id, exact match |
| `person` | the Person it addresses |
| `created_by`, `created_at` | attribution |

Unique on `(organization, namespace, subject)`. One Person may carry several
identifiers. A Firebase uid may be stored as a subject; it must never create an
`Identity` or a `PatientUser`, and never authorise a login.

### `POST /api/v1/person-resolutions/`

Atomic resolve-or-provision, because lookup-then-provision is a race the ETL
loses in practice: its import tasks are mapped and run concurrently over one
cohort.

1. identifier exists → its Person, claimed or not, after the tenant check
2. no identifier, one eligible unclaimed Person for the address → bind and
   return
3. nothing → create Person, PatientRecord and identifier in one transaction
4. ambiguous → 409, create nothing

Email stays optional evidence for the bootstrap in (2). Once an identifier
exists, a changed or absent address never redirects it.

Resolution returns claimed Persons. The ETL needs this and it belongs in the
grant, not in email knowledge: its authoritative flow re-enumerates each patient
and issues deletes for rows the warehouse no longer has, so a patient who logs
in would otherwise stop being refreshed and gain a duplicate instead.

### Backfill

The ETL holds a complete `(actor_iss, actor_sub) -> person_id` index for every
Person it provisioned, including the email-less ones. That index, not an
email sweep, is what reaches the 2,299. Accept it as an explicit binding call,
reject rebinding an identifier to another Person, and check write authority over
the target.

## Authorization

The namespace grant belongs to the integration, not to a rotating credential,
so the warehouse and FHIR workers can share it.

`ServiceCredential` (`patient_portal/service_tokens.py:36`) carries only
`service_id` and `scope`, so a static token has no organization; org scoping
comes from `ApplicationOrganization` (`omop_core/models.py:280`), which binds an
OAuth2 Application. The ETL names an org-bound machine credential as its gating
dependency.

It gates step 2, and reusing `_resolve_provision_org`
(`patient_portal/api/views.py:6175`) does not avoid that. That helper takes a
slug from the body and resolves it; it authorizes nothing, as its own comment
says: "There is no check that the caller may assign an organization at all ...
the day it lands this endpoint must not start accepting a slug naming somebody
else's tenant." `EtlProvisionPermission` narrows the endpoint to the ETL grant
and staff, and a static ETL token carries no organization, so any holder could
name any tenant.

`provision` tolerates that because it only stamps an organization on a record it
just created. Resolution cannot: the key is `(organization, namespace, subject)`
and the answer is a `person_id`, so an unauthorized slug reads another tenant's
patient out of their own namespace. The acceptance gate "cross-tenant resolution
is denied" is therefore unmeetable until the grant is bound.

So step 2a, before the endpoint ships: give a machine credential an
organization. The cheap form is an optional `organization` key per grant in
`SERVICE_AUTH_TOKENS`, validated in `patient_portal/service_tokens.py` (which
today rejects any key but `token` and `scopes`), carried on `ServiceCredential`
and returned by `get_request_org`. That is the same binding #1764 proposes and
it needs no new auth model. `ApplicationOrganization` already covers OAuth
clients, so an org-linked OAuth application is the alternative.

## Delivery

1. **Harden `provision`** — a claimed or ambiguous address answers 409 and
   creates nothing, instead of minting another Person. Shipped with this plan.
2. **Bind a machine credential to an organization** (step 2a above).
   Prerequisite for anything below it, not a follow-up.
3. `PersonSourceIdentifier`, the resolution endpoint, and the binding call for
   the ETL's existing index.
4. Migrate both ETL flows onto `urn:healthtree:account`.

Acceptance: repeated and concurrent imports converge on one Person; import after
login adoption keeps working; a missing or changed email never redirects an
identifier; cross-tenant resolution is denied; ambiguous bootstrap is a 409;
rebinding is rejected; machine resolution never changes login ownership; a
source identifier keeps an apparently empty signup placeholder from being
deleted while an importer still addresses it.

Out of scope, by the ETL's own request: merge and dedup APIs. Existing
duplicates are resolved by re-import on their side.

## Correction owed

`docs/identity-architecture.md` describes unsigned cross-service actor
provisioning and several identities per Person. Both contradict what the code
now enforces and `PatientUser`'s one-to-one constraints.
