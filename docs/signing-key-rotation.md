# Signing Key Rotation

Covers `AUDIT_HMAC_KEY` and `EXPORT_SIGNING_KEY`. Both are independent managed secrets,
deliberately separate from `SECRET_KEY` and from each other — audit tamper-evidence and
FHIR-export signatures are distinct controls, and rotating the Django signing key must not
silently rewrite the trust basis for either. Enforced by `patient_portal/checks.py`
(`patient_portal.E001`/`E002`/`E003`), which errors outside `DEBUG` if a key is missing,
equals `SECRET_KEY`, or equals the other key.

Audit finding PROMOP F20, SOC2 CC7.2.

## What each key signs

| Key | Signs | Consequence of rotation |
|---|---|---|
| `AUDIT_HMAC_KEY` | The hash chain over `AuditEvent` rows | Events written under the old key no longer verify against the new one |
| `EXPORT_SIGNING_KEY` | FHIR export bundle signatures | Previously issued bundles no longer verify |

**Read that table before rotating.** Neither key is a session secret you can cycle freely:
rotation invalidates the verifiability of everything already signed. That is the point of
the control — the signature attests to *which key era* produced the record — but it means
rotation is a deliberate, evidenced act, not routine hygiene.

## Generate

```bash
python -c "import secrets; print(secrets.token_urlsafe(64))"
```

Once per key. Never reuse a value across the two, across environments, or from
`SECRET_KEY`.

## Rotate

1. **Verify the current chain before touching anything**, so a later failure cannot be
   confused with a pre-existing break:

   ```bash
   python manage.py verify_audit_integrity
   ```

   Retain the output. This is the CC7.2 evidence artifact, and it is only meaningful if
   captured *before* the key changes.

2. **Set the new value** in the environment (Render dashboard, GCP secret manager). Do not
   commit it, and do not put it in `.env.example`.

3. **Confirm the configuration check passes** under production-like settings:

   ```bash
   DEBUG=False SECRET_KEY=... AUDIT_HMAC_KEY=... EXPORT_SIGNING_KEY=... \
     ALLOWED_HOSTS=... CORS_ALLOWED_ORIGINS=... \
     python manage.py check --deploy --fail-level ERROR
   ```

   `start.sh` runs this on every deploy, so a misconfigured environment fails the deploy
   rather than starting with a silent `SECRET_KEY` fallback.

4. **Record the rotation** — date, key, operator, reason, and the pre-rotation
   `verify_audit_integrity` output — in the SOC2 evidence store. An auditor asks who
   rotated what and when; the environment variable itself carries no history.

5. **Re-run `verify_audit_integrity` after the deploy.** Events written under the previous
   key will not verify against the new one. That is expected, and the retained
   pre-rotation output is what demonstrates the chain was intact up to the cutover.

## Cadence

Rotate on operator change, suspected exposure, or the cadence the security policy sets —
not on a timer chosen here. Because rotation breaks verification of already-signed
records, an unnecessary rotation costs evidence continuity.

## Staging and development

`DEBUG=True` skips the checks entirely and both keys fall back to `SECRET_KEY`, which keeps
local development ergonomic. Staging runs with `DEBUG=False` and therefore needs both keys
set, distinct, like production.
