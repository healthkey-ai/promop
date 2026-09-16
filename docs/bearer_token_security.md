# Legacy bearer-token security plan

## Status and decision

This document resolves the tension between #1085 and #1144. Implementation is
tracked in #1156 and staging rollout is tracked in #1157.

- #1085 correctly removed the shared service token's unconditional bypass of
  `ScopedTokenPermission`. Authentication must not imply authorization.
- #1144 correctly reports that staging ETL must create and update clinical data
  across patients. A read-only credential makes that integration unusable.
- Persisting `is_staff=True` on the shared service identity is rejected. It
  grants unrelated administrative powers and bypasses narrower checks on views
  using `IsAdminUser` or inline staff checks.
- Giving the ETL `patient/*.write` is also rejected. SMART write scopes are
  resource-wide and cover destructive operations the importer does not need.

The interim design keeps the legacy identity non-staff and adds a custom
`system/etl.write` capability. That capability is accepted only by permission
classes deliberately placed on these #1144 surfaces:

- person `find_or_create` and demographic patch;
- clinical condition, drug, measurement, observation, and procedure
  create/update/bulk-create/bulk-update endpoints; and
- code-mapping lookup.

The capability is valid only for `POST`, `PUT`, and `PATCH`. Ordinary scoped
permissions do not recognize it. In particular, the clinical `bulk_delete`
actions use the ordinary permission even though they are implemented as POST,
so the ETL capability cannot delete through them. Defaults remain read-only and
production is not broadened.

## Why this is not a SMART scope plus an HTTP-method grant

Scopes and methods describe different dimensions, but combining them does not
produce endpoint-level least privilege. `patient/*.write` means every patient
write surface, while an HTTP method says only how a request is transported.
Some destructive APIs use POST because request bodies on DELETE are unreliable;
therefore allowing POST while denying DELETE would still allow bulk deletion.

The custom capability is instead attached to the small set of intended API
surfaces. Its permission implementation additionally rejects DELETE. This gives
the compatibility credential semantic endpoint authorization, not merely a
verb filter. Per-service credentials and stored grants in #568 remain the target
architecture.

## Threat model

The bearer value is a shared secret. Any holder authenticates as the same
`urn:service` / `hk-labs-sync` identity. Until #568 replaces that model, assume
compromise of one consumer compromises the credential.

| Threat | Control |
| --- | --- |
| A holder calls an unrelated write endpoint | Only ETL-marked permissions accept `system/etl.write` |
| A holder deletes through HTTP DELETE | The ETL capability never accepts DELETE |
| A holder calls a POST-based bulk delete | Bulk-delete actions retain ordinary scoped permission and reject the ETL capability |
| A holder reaches staff or organization administration | The identity remains non-staff and has no org-admin grant |
| Configuration is missing | The default is only `patient/*.read` |
| Configuration is empty or malformed | Unknown or absent scopes fail closed |

This does not solve two known problems:

- #568: all consumers still share one token and one audit identity.
- #147: some service flows trust caller-supplied actor identity fields.

The target state is one credential per service mapped to a distinct identity
and grant, followed by removal of caller-asserted identity for identity-bearing
writes.

## Authorization profiles

### Default and production-safe profile

```text
SERVICE_AUTH_SCOPES=patient/*.read
```

This permits patient-data reads on scoped endpoints and denies all writes.
Production remains on this behavior unless its integrations are separately
inventoried and granted only what they need.

### Staging ETL profile

```text
SERVICE_AUTH_SCOPES=patient/*.read system/etl.write
```

This supports the operations reported in #1144 without granting the broad
`patient/*.write` scope. The identity remains non-staff, so staff-only formula,
organization, trust, invitation, and patient-administration surfaces remain
denied.

### Full denial

Set `SERVICE_AUTH_SCOPES` to an empty string. This is the emergency revocation
option when rotating or investigating the shared credential.

## Request evaluation

For a request authenticated by `ServiceTokenAuthentication`:

1. HMAC-safe comparison validates the bearer value.
2. The request receives the existing non-staff service identity and sentinel.
   Authentication repairs any pre-existing staff or superuser flags on that
   hardcoded identity before returning it.
3. An ordinary `ScopedTokenPermission` evaluates only its existing SMART scopes.
4. An explicitly ETL-enabled permission may additionally accept
   `system/etl.write`, but only for POST, PUT, or PATCH.
5. Destructive overrides such as POST-based bulk delete retain the ordinary
   permission, so the custom capability is rejected before endpoint code runs.

OAuth2, partner, and session authorization behavior is unchanged. The custom
ETL capability is accepted only for the legacy service-token authentication
path, not OAuth tokens that happen to assert the same string.

## Deployment and verification

The Render blueprint declares the staging ETL profile on `promop-staging`.
`SERVICE_AUTH_TOKEN` remains a dashboard-managed secret. The worker does not
serve HTTP and does not need this grant. Render blueprint environment changes
must be synced; an ordinary code deploy does not apply them automatically.

After deployment, run smoke requests with the staging service credential:

1. `GET /api/persons/?limit=1` returns 200.
2. `POST /api/persons/find_or_create/` returns a domain response, not 403.
3. A representative clinical bulk create/update returns a domain response, not
   an authorization failure.
4. `POST /api/v1/code-mappings/lookup/` returns a domain response, not 403.
5. `DELETE` of a nonexistent clinical resource returns 403.
6. `POST /api/v1/measurements/bulk_delete/` returns 403.
7. Organization creation, admin-delete, lab sync, and FHIR sync return 403.

Do not log or paste the bearer value into issue or deployment output.

## Rollback

Restore `SERVICE_AUTH_SCOPES=patient/*.read` (or set it empty), then restart the
web service. This immediately returns the ETL writes to HTTP 403 without
changing the secret or database. The code change is backward-safe because its
default remains the read-only behavior introduced for #1085.

## Tests required before merge

- Existing SMART scope matrix across every ordinary permission subclass.
- Custom capability matrix proving it works only on ETL-enabled permissions and
  only for POST, PUT, and PATCH.
- Real-bearer integration proving PATCH succeeds while row DELETE, POST-based
  bulk delete, admin-delete, and unrelated writes fail.
- Real-bearer code-mapping lookup coverage.
- Regression proving the service identity remains non-staff.
- Blueprint regression proving only staging receives the ETL capability and
  production is not broadened.
- Clean migrations and focused/full backend suites on an isolated PostgreSQL
  instance with `pg_trgm` and `vector` enabled.

## Follow-up sequence

1. Merge this bounded compatibility fix for #1144 while preserving #1085.
2. Implement #568 so each service token maps to its own identity and grant.
3. Implement #147 so identity-bearing writes cannot trust unsigned actor fields.
4. Rotate the shared token after consumers move to distinct credentials, then
   remove the legacy settings and sentinel path.
