# Encryption at rest: HKI-SEC-08

This is the decision and evidence procedure for [#60](https://github.com/healthkey-ai/promop/issues/60).
It covers every store that holds PHI in a promop deployment. HIPAA treats encryption at
rest as an addressable safeguard, and it is a prerequisite for the hosting BAA.

## Decision

**PHI is encrypted at rest by the storage platform (AES-256, provider-managed keys). promop
does not add application-level field encryption to clinical data.**

Application-level encryption of OMOP tables was considered and rejected:

- promop's function is querying clinical data: cohort filters, trigram and vector concept
  retrieval, joins across OMOP tables, and PatientRecord derivation. Encrypted columns
  cannot be indexed, filtered or joined, so each encrypted field removes a feature.
- It does not protect against the realistic threat. Whoever compromises the application
  also holds its decryption key. What platform encryption protects against is exposure of
  the storage media, snapshots and backups.
- It moves key custody, rotation and recovery into this codebase, and into every customer
  deployment of it.

The field-level candidates listed in #60 do not change this:

| Candidate | Finding |
|---|---|
| `ssn`, `insurance_id` | No such fields exist in any model. |
| address, DOB, genetic biomarkers | Queried, joined and derived on. Covered by platform encryption. |
| `FhirInstitutionConnection.access_token` / `refresh_token` | Stored in plain text (renamed in #1086), and nothing writes them yet. These are credentials rather than queried PHI, so they are the one place field encryption fits. Decide that when the institution-connection flow is wired up. |

## Where PHI sits

| Store | Holds | Covered by |
|---|---|---|
| PostgreSQL | All clinical tables, PatientRecord, identities, audit trail | Render: "Render Postgres databases are encrypted at rest using AES-256 data encryption. This applies to both primary and replica instances, along with all backups." ([source](https://render.com/docs/postgresql-creating-connecting)) |
| Redis / Key Value | Celery messages and results (person ids, errors), throttle counters | Render HIPAA-enabled workspace: "All disks and daily snapshots are encrypted at rest." ([source](https://render.com/blog/introducing-hipaa-enabled-workspaces)) |
| File storage (`MEDIA_ROOT`) | PatientDocument uploads, including advance directives | **Not covered.** Ephemeral service filesystem, outside the database and its backups. [#1563](https://github.com/healthkey-ai/promop/issues/1563) |

Sentry, email and LLM calls send data out of the deployment rather than storing it here.
They belong in the sub-processor inventory ([#753](https://github.com/healthkey-ai/promop/issues/753)).

## Key custody

Render holds the keys. It documents no customer-managed key option, so key custody
evidence consists of the provider attestations above, the signed BAA, and the provider's
SOC 2 Type II report. None of these can be read through an API. `SECRET_KEY`,
`AUDIT_HMAC_KEY` and `EXPORT_SIGNING_KEY` sign data; they encrypt nothing, and rotating
them does not touch stored PHI. See [signing key rotation](../signing-key-rotation.md).

For a deployment outside Render, the operator supplies the equivalent for their own
database, key-value store and file storage, for example Cloud SQL or RDS encryption with
provider- or customer-managed KMS keys. The collector below works on any host.

## Evidence collection

`manage.py capture_encryption_evidence` derives the PHI stores from the live settings
and reports what the application can observe about each one. With `--render`, it also
reads the backing Render resources: service disk, Postgres plan, HA and point-in-time
recovery status, Key Value instances, and the owning workspace. It never outputs
hostnames, credentials or connection strings. A failed API read fails the capture.

Run it from the Render shell of each web service (production is not reachable locally),
with a Render API key exported for that session only:

```bash
RENDER_API_KEY=... python manage.py capture_encryption_evidence --render --require-covered
```

`--require-covered` exits 1 if any store holds PHI outside an attested location, if no
Render Postgres resource can be tied to the database, if point-in-time recovery is
unavailable, or if the resources span more than one workspace. Until
[#1563](https://github.com/healthkey-ai/promop/issues/1563) is fixed it fails on the
`files` gap. That failure is correct.

Store each dated JSON output with the manual attestations it lists (the BAA, HIPAA-enabled
workspace status, and the provider's SOC 2 report) in the SOC 2 evidence store. Capture
again after any change to the database, Key Value, disks or storage settings, and at each
recurring control review. Scheduling this and choosing its credential and evidence sink
is tracked in [#1562](https://github.com/healthkey-ai/promop/issues/1562).

## Status against #60

| Acceptance criterion | Status |
|---|---|
| Confirm Render Postgres encryption at rest and document it | Documented here. Confirming it per deployment is the capture above plus the HIPAA workspace attestation. |
| Identify fields that warrant application-level encryption | Done: none of the clinical fields; OAuth tokens to be decided when they are first written. |
| Integrate field encryption if needed | Not needed (see Decision). |
| `SECRET_KEY` rotation procedure | [signing key rotation](../signing-key-rotation.md#secret_key), with `SECRET_KEY_FALLBACKS` support. |
| Backup encryption confirmed | Render's attestation covers "all backups". The capture records PITR availability. |
| HKI-SEC-08 on the pre-production-PHI checklist | This control, gated on a clean `--require-covered` capture and a signed BAA. |
