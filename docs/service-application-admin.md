# Service application administration

Staff users (`is_staff=True`) can open **Org Admin → Service applications**.
Each app has an editable name, owner/contact, description, enabled flag, and
scope grant. Its service ID stays fixed to preserve historical audit identity.
Org administrators, patients, and machine credentials cannot administer tokens.
Session-authenticated mutations enforce CSRF even when other API routes retain
legacy session behavior.

Select an app to create a labeled token with an optional expiry. Copy the secret
immediately: it is shown only in the creation response and is never retrievable
later. The database stores a SHA-256 digest of the randomly generated 384-bit
secret and its last four characters. List/detail/admin pages never expose the
secret or digest. Share it through a secret manager or an expiring link restricted
to its intended recipient.

To rotate without an outage, create a replacement, deliver it, update the caller,
then revoke the old token. An app can have multiple active tokens. Revoking a
token, changing scopes, or disabling an app applies to subsequent authentication
requests immediately; already-authenticated requests may finish. Re-enabling an
app does not restore a revoked token. Last-used timestamps update at most every
five minutes. Creation, edits, and revocations are recorded in the existing audit
trail with the staff identity and application/token request path.

Application grants retain the existing cross-patient service trust model. Use
organization-linked OAuth2 clients for organization isolation. Scoped static
credentials do not permit impersonating a user through unsigned actor fields;
see [the migration guide](service-token-migration.md).

## Seed existing distributed tokens

Migrations seed editable application records for **ETL (Nikita)**, **HT-PHR
(Vlad)**, **HK-Labs (Vlad)**, and **EXACT (Leonid)**. No credentials or credential
hashes are committed to migrations. Import the private file containing the
already-generated tokens against the intended deployment database:

```bash
python manage.py migrate
python manage.py import_service_tokens --file /secure/path/SERVICE_AUTH_TOKENS.json
```

This activates the same tokens already distributed; it does not rotate them.
On first import for an app, scopes come from the file. Re-import does not undo
later revocations, disabled flags, edited scopes, or edited app metadata.
Only hashes and last-four-character suffixes enter the database. The command
prints counts, never secrets. Keep the private file until delivery is complete,
then remove it using your normal secret-handling procedure.

Managed tokens take precedence over `SERVICE_AUTH_TOKENS`. Once an app has any
managed token, its environment tokens cannot bypass revocation or scope changes.
Apps without managed tokens can still use their existing environment grant.
The old shared `SERVICE_AUTH_TOKEN` remains a separate compatibility identity
until it is retired; remove it after all consumers migrate.

The Django admin also lists named applications and token metadata. Token creation
and revocation use the PRomop service-applications page; token records cannot be
hard-deleted through either admin interface.
