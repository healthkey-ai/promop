# Security settings and DEBUG

`DEBUG=True` enables Django diagnostics; it does not relax the controls below.
Staging may enable it explicitly. It still exposes tracebacks to clients: the
operator must establish whether staging contains real patient data before
choosing it. This change does not establish staging's data classification.

Deployments are detected by `RENDER=true`, `RENDER_EXTERNAL_URL`, or
`RENDER_EXTERNAL_HOSTNAME`. On other platforms set `ENVIRONMENT=staging` or
`ENVIRONMENT=production` (any value outside `local`, `development`, `dev`, and
`test` is treated as deployed). A local environment label cannot cancel Render
markers. Unmarked legacy processes with `DEBUG=False` retain startup validation.

Deployed web processes require a nonempty, non-default `SECRET_KEY`,
`DATABASE_URL`, resolved `ALLOWED_HOSTS`, and nonempty `CORS_ALLOWED_ORIGINS`,
even with debug enabled. Render's hostname is added to the host allowlist.
Workers require the secret and database only. Existing build/maintenance command
exemptions remain; `check --deploy` validates the full web configuration.

| Environment variable | Default independent of DEBUG |
| --- | --- |
| `ALLOWED_HOSTS` | Empty, plus Render's supplied hostname |
| `CORS_ALLOWED_ORIGINS` | Empty |
| `CORS_ALLOW_ALL_ORIGINS` | `false` |
| `CORS_ALLOW_CREDENTIALS` | `true` (restricted by the origin allowlist) |
| `ENABLE_BASIC_AUTH` | `false` |
| `FIREBASE_SKIP_REVOCATION_CHECK` | `false` |
| `FIREBASE_PROJECT_ID` | Empty; configure explicitly for Firebase |
| `PHR_AUDIENCE` | Empty; PHR tokens fail closed until configured |
| `PHR_BASE_URL` | Empty; configure explicitly for PHR |
| `SESSION_COOKIE_SECURE`, `CSRF_COOKIE_SECURE` | `true` |
| `SECURE_HSTS_SECONDS` | `31536000` |
| `SECURE_HSTS_INCLUDE_SUBDOMAINS`, `SECURE_HSTS_PRELOAD` | `true` |
| `SECURE_CONTENT_TYPE_NOSNIFF` | `true` |
| `X_FRAME_OPTIONS` | `DENY` |
| `ALLOWED_REDIRECT_URI_SCHEMES` | `https` (comma-separated) |
| `SECURE_SSL_REDIRECT` | `false`; Render redirects at its edge |
| `TRUST_PROXY_SSL_HEADER` | `true` on Render, otherwise `false` |

Proxy trust enables Django's `SECURE_PROXY_SSL_HEADER` with
`HTTP_X_FORWARDED_PROTO,https`. Enable it only behind a proxy that strips
client-supplied values. See [Django's proxy-header requirements](https://docs.djangoproject.com/en/5.2/ref/settings/#secure-proxy-ssl-header).
Deployments without an HTTPS-redirecting edge should set `SECURE_SSL_REDIRECT=true`.
The obsolete `SECURE_BROWSER_XSS_FILTER` setting was removed: Django 5.2 does
not implement it, so reporting it would imply a protection that does not exist.

For local HTTP development, copy `.env.example`, which explicitly configures
localhost hosts/origins, non-secure cookies, no HSTS, and HTTP OAuth redirects.
Set PHR/Firebase identifiers explicitly if using those providers. Basic auth,
all-origin CORS, and skipped revocation checks require separate opt-ins;
changing `DEBUG` never enables them. Keep local overrides out of deployment
configuration. Explicit wildcard hosts and all-origin CORS remain possible in
staging; the deployment guard still requires the origin configuration.

Run this in the deployed web service shell (for staging, Render's
`promop-staging`):

```bash
python manage.py check --deploy --fail-level ERROR
```

`patient_portal.I001` prints JSON with the effective controls and presence flags
for identity-provider configuration; it never dumps secrets, URLs, or audiences.
`patient_portal.W006` highlights permissive authentication/origin/redirect
choices. Django's own checks report debug tracebacks, insecure cookies, HSTS,
and other HTTP controls. Audit/export key separation and shared throttle-cache
checks also run on debug-enabled deployments. Warnings remain warnings so
operators can choose staging diagnostics deliberately; configuration errors
still fail startup. `start.sh` already runs this command before migrations,
so the report also appears in deployment logs.

### Browser authentication and retired OAuth clients (#141)

The SPA uses the existing Django login/logout endpoints and server-side sessions.
Session cookies are HttpOnly, SameSite=Lax, and Secure by default (local HTTP
requires the documented `SESSION_COOKIE_SECURE=false` override). The SPA no
longer requests `offline_access`, exchanges authorization codes, stores bearer
credentials, or retries API calls with refresh tokens. Old browser credentials
are removed from sessionStorage/localStorage on startup and before API calls.
The legacy `/auth/callback` URL returns to the session-authenticated app, or login
when no session exists. Users who only had browser tokens must sign in again.

The OAuth validator rejects authorization requests, token grants (including
refresh), and already-issued access tokens for the retired `ctomop-smart-app`
client. This takes effect immediately on backend deployment without a database
migration or waiting for 30-day refresh tokens to expire. Other SMART and service
OAuth clients retain their existing behavior.

If a deployment previously built the SPA with a custom `VITE_OAUTH_CLIENT_ID`,
add that ID to the backend's comma-separated `OAUTH2_RETIRED_BROWSER_CLIENT_IDS`
environment variable **before deploying this change**. The built-in client ID is
always retired. Remove the old `VITE_OAUTH_CLIENT_ID` and
`VITE_OAUTH_REDIRECT_URI` frontend settings; they are no longer used.
