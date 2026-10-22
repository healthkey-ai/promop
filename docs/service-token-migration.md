# Individual service credentials and verified user attribution

PRomop accepts `SERVICE_AUTH_TOKENS`, a JSON object mapping service IDs to a
`token` and a space-separated `scopes` string. Each credential authenticates as
`urn:service|<service-id>`. The authenticated service identity appears in audit
records and in provenance for lab, FHIR, and ordinary clinical imports.
Credentials do not grant staff privileges. Missing scopes default to
`patient/*.read`; an empty scope grants no access.

Generate separate credentials for Nikita's ETL, Vlad's HK-Labs, and Leonid's
EXACT service (plus Vlad's HT-PHR) on a trusted machine:

```bash
python manage.py generate_service_tokens \
  --service 'etl=patient/*.read system/etl.write' \
  --service 'ht-phr=patient/*.read patient/*.write' \
  --service 'hk-labs=patient/*.read patient/*.write' \
  --service 'exact=patient/*.read' \
  --output /secure/path/promop-service-tokens.json
```

The command creates a new file with mode `0600`, refuses to overwrite an existing
file, and does not print secrets. It does not install the tokens or change the
running server. EXACT starts read-only in this example: confirm its endpoints
before granting `patient/*.write` if it imports clinical data. HK-Labs' write
scope is resource-wide, not restricted to the lab sync endpoint. Static service
tokens retain the existing cross-patient trust model; use organization-linked
OAuth2 clients when organization isolation is required.

1. Set `SERVICE_AUTH_TOKENS` on the PRomop web service to the complete JSON file
   contents. On staging this is **Render's `promop-staging`** environment settings.
   Include existing entries if any; replacing this setting revokes omitted tokens.
2. Deliver only each service's token to its owner through the team's secret
   manager. Store it in that caller's existing PRomop bearer-token setting.
   Calls continue to use `Authorization: Bearer <token>`.
3. Verify each caller's allowed reads and writes and inspect audit/provenance for
   the expected distinct identity. Do not commit or paste secrets into issues,
   PRs, logs, or chat.
4. Keep `SERVICE_AUTH_TOKEN` and `SERVICE_AUTH_SCOPES` during credential migration.
   That token retains the legacy `urn:service|hk-labs-sync` identity and its own
   scopes. Remove it once every consumer has switched to individual credentials.
   Named entries must use distinct tokens, including from the legacy token.

Rotate one service by generating a replacement token and changing that entry
and the corresponding caller's secret. Other services are unaffected. There is
no authentication cache for static service credentials. Configuration changes
must reach all running web processes before rotation/revocation is complete.

## Required caller changes before deploying the identity fix

**Keeping the shared token does not preserve unsigned user attribution.** A
service credential proves the service's identity, not an `actor_iss`/`actor_sub`
identity supplied in the JSON body. This applies to OAuth client-credentials
calls too. Coordinate these changes before deploying the enforcement release:

| Caller operation | Required request |
| --- | --- |
| HK-Labs browser commit attributed to a patient | Forward the user's verified bearer token to `/api/lab-results/sync/`. Omit `person_id` for self-service; an explicit patient requires the authenticated user's write access. |
| ETL, HK-Labs background job, or EXACT clinical import | Send the service bearer, explicit `person_id`, and no `actor_iss`/`actor_sub`. Provenance identifies the service. |
| `/api/persons/find_or_create/` | Authenticate as the end user and supply that same user's issuer/subject. Service credentials cannot provision or resolve a person through claimed login identifiers. |
| `/api/v1/patients/signup/` with OIDC actor fields | Service credentials are rejected; an authenticated staff account can perform explicit administrative provisioning. The local email/password signup flow remains available to authorized callers. |
| Provisioning a patient for an unattended ETL job | Use the established authorized patient-import workflow and retain the returned `person_id`; do not use an asserted login identity as a provisioning shortcut. |

Lab and FHIR service sync requests with either actor field return **403** before
patient resolution or clinical writes. Service sync without `person_id` returns
**400**. User requests bind attribution to the authenticated user and enforce
patient write access. The generic provenance headers/body cannot override a
service's authenticated identity.

There is no compatibility switch restoring unsigned user impersonation. Review
ETL's patient provisioning and background lab imports with the owners before
merging an automatically deployed release. Existing service tokens alone cannot
make those requests safe.
